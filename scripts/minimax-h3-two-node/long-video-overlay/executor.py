"""Ray-backed vLLM-Omni diffusion executor spanning two physical hosts."""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Callable
from typing import Any

from vllm.logger import init_logger
from vllm.v1.engine.exceptions import EngineDeadError
from vllm_omni.diffusion.data import DiffusionOutput
from vllm_omni.diffusion.executor.abstract import DiffusionExecutor

from .worker import RayDiffusionWorker

logger = init_logger(__name__)


def _required_env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value


def _require_ray_auth() -> None:
    if os.environ.get("RAY_AUTH_MODE") != "token":
        raise RuntimeError("RAY_AUTH_MODE=token is required")
    token_path = _required_env("RAY_AUTH_TOKEN_PATH")
    token_stat = os.stat(token_path)
    if not stat.S_ISREG(token_stat.st_mode):
        raise RuntimeError("Ray token path is not a regular file")
    if stat.S_IMODE(token_stat.st_mode) != 0o600:
        raise RuntimeError("Ray token file must have mode 0600")


def _rank_evidence_dir() -> str:
    value = _required_env("H3_RANK_EVIDENCE_DIR")
    if not os.path.isabs(value):
        raise RuntimeError("H3_RANK_EVIDENCE_DIR must be absolute")
    return value


class RayDiffusionExecutor(DiffusionExecutor):
    """Run one diffusion worker on each Spark through a pre-started Ray cluster."""

    uses_multiproc = True

    def _init_executor(self) -> None:
        import ray
        from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy

        self._ray = ray
        self._rpc_lock = __import__("threading").RLock()
        self._closed = False
        self._is_failed = False
        self._failure_callbacks: list[Callable[[], None]] = []
        self._actors: list[Any] = []

        world_size = int(self.od_config.num_gpus)
        if world_size != 2:
            raise ValueError(f"This executor requires exactly two GPUs on two hosts; got {world_size}")

        _require_ray_auth()
        evidence_dir = _rank_evidence_dir()
        head_ip = _required_env("H3_HEAD_IP")
        worker_ip = _required_env("H3_WORKER_IP")
        shared_gid = os.environ.get("NCCL_IB_GID_INDEX", "3")
        head_gid = _required_env("H3_HEAD_GID_INDEX", shared_gid)
        worker_gid = _required_env("H3_WORKER_GID_INDEX", shared_gid)
        master_port = int(_required_env("H3_MASTER_PORT", "29500"))
        ray_address = _required_env("H3_RAY_ADDRESS", f"{head_ip}:6379")
        startup_timeout = float(os.environ.get("H3_WORKER_START_TIMEOUT", "2400"))

        if not ray.is_initialized():
            ray.init(
                address=ray_address,
                namespace="minimax-h3-2x",
                ignore_reinit_error=True,
                _node_ip_address=head_ip,
            )

        live_nodes = {
            node["NodeManagerAddress"]: node["NodeID"]
            for node in ray.nodes()
            if node.get("Alive")
        }
        missing = [ip for ip in (head_ip, worker_ip) if ip not in live_nodes]
        if missing:
            raise RuntimeError(
                f"Ray cluster is missing required node address(es) {missing}; live nodes={sorted(live_nodes)}"
            )

        env_keys = (
            "NCCL_NET",
            "NCCL_IB_DISABLE",
            "NCCL_IB_HCA",
            "NCCL_IB_GID_INDEX",
            "NCCL_IB_ADDR_FAMILY",
            "NCCL_IB_ROCE_VERSION_NUM",
            "NCCL_SOCKET_IFNAME",
            "GLOO_SOCKET_IFNAME",
            "NCCL_CROSS_NIC",
            "NCCL_CUMEM_ENABLE",
            "NCCL_NVLS_ENABLE",
            "NCCL_IGNORE_CPU_AFFINITY",
            "NCCL_DEBUG",
            "FLASHINFER_DISABLE_VERSION_CHECK",
            "VLLM_WORKER_MULTIPROC_METHOD",
            "VLLM_OMNI_VIDEO_SYNC_TIMEOUT",
            "PYTORCH_CUDA_ALLOC_CONF",
        )
        actor_env = {key: os.environ[key] for key in env_keys if key in os.environ}
        actor_env.update(
            {
                "H3_HEAD_IP": head_ip,
                "H3_WORKER_IP": worker_ip,
                "H3_MASTER_PORT": str(master_port),
                "H3_RANK_EVIDENCE_DIR": evidence_dir,
            }
        )

        remote_worker = ray.remote(num_gpus=1, max_restarts=0, max_task_retries=0)(RayDiffusionWorker)
        actors_by_rank: dict[int, Any] = {}
        # Rank 1 must publish a stable pre-rendezvous marker before rank 0.
        # Worker construction is lazy so rank 1 cannot block waiting for rank 0.
        for rank, node_ip in ((1, worker_ip), (0, head_ip)):
            node_actor_env = dict(actor_env)
            node_actor_env["NCCL_IB_GID_INDEX"] = head_gid if rank == 0 else worker_gid
            actor = remote_worker.options(
                name=f"minimax-h3-diffusion-rank-{rank}-{os.getpid()}",
                scheduling_strategy=NodeAffinitySchedulingStrategy(
                    node_id=live_nodes[node_ip],
                    soft=False,
                ),
                runtime_env={"env_vars": node_actor_env},
            ).remote(
                rank=rank,
                world_size=world_size,
                od_config=self.od_config,
                master_addr=head_ip,
                master_port=master_port,
            )
            actors_by_rank[rank] = actor
            marker = ray.get(actor.pre_rendezvous_marker.remote(), timeout=30)
            if marker.get("rank") != rank or marker.get("event") != "PRE_RENDEZVOUS_RUNNING":
                raise RuntimeError(f"Invalid rank {rank} pre-rendezvous marker: {marker}")
            if rank == 1:
                stable_since = time.monotonic()
                while time.monotonic() - stable_since < 15:
                    time.sleep(1)
                    marker = ray.get(actor.pre_rendezvous_marker.remote(), timeout=5)
                    if marker.get("event") != "PRE_RENDEZVOUS_RUNNING":
                        raise RuntimeError(f"Rank 1 marker did not remain stable: {marker}")

        self._actors = [actors_by_rank[0], actors_by_rank[1]]
        try:
            ready = ray.get([actor.start.remote() for actor in self._actors], timeout=startup_timeout)
        except BaseException:
            self._mark_failed()
            self.shutdown()
            raise

        ranks = sorted(int(item["rank"]) for item in ready)
        hosts = {int(item["rank"]): item["hostname"] for item in ready}
        if ranks != [0, 1] or not all(item.get("distributed") for item in ready):
            self.shutdown()
            raise RuntimeError(f"Invalid distributed worker readiness: {ready}")
        logger.info("MiniMax H3 two-node workers ready: %s", hosts)

    @property
    def is_dead(self) -> bool:
        return self._closed or self._is_failed

    def _mark_failed(self) -> None:
        if self._is_failed:
            return
        self._is_failed = True
        for callback in self._failure_callbacks:
            try:
                callback()
            except Exception:
                logger.exception("failure callback raised")

    def _ensure_open(self) -> None:
        if self.is_dead:
            raise EngineDeadError("MiniMax H3 two-node diffusion executor is not healthy")

    def register_failure_callback(self, callback: Callable[[], None]) -> None:
        self._failure_callbacks.append(callback)

    def collective_rpc(self, *args, **kwargs):
        with self._rpc_lock:
            return self._collective_rpc(*args, **kwargs)

    def _collective_rpc(
        self,
        method: str,
        timeout: float | None = None,
        args: tuple = (),
        kwargs: dict | None = None,
        unique_reply_rank: int | None = None,
        exec_all_ranks: bool = False,
        request_id: str | None = None,
    ) -> Any:
        self._ensure_open()
        kwargs = kwargs or {}
        execute_all = unique_reply_rank is None or exec_all_ranks

        refs = []
        for rank, actor in enumerate(self._actors):
            should_execute = execute_all or unique_reply_rank is None or rank == unique_reply_rank
            refs.append(
                actor.execute.remote(
                    method=method,
                    args=args,
                    kwargs=kwargs,
                    output_rank=unique_reply_rank,
                    execute=should_execute,
                    request_id=request_id,
                )
            )

        try:
            replies = self._ray.get(refs, timeout=timeout)
        except BaseException:
            self._mark_failed()
            raise

        failures = [reply for reply in replies if not reply.get("ok")]
        if failures:
            if not all(item.get("error") == "H3 request canceled at a collective safe boundary" for item in failures):
                self._mark_failed()
            details = "; ".join(
                f"rank {item['rank']}: {item.get('error_type', 'Error')}: {item.get('error')}"
                for item in failures
            )
            traces = "\n\n".join(item.get("traceback", "") for item in failures if item.get("traceback"))
            raise RuntimeError(f"Distributed RPC {method!r} failed: {details}\n{traces}")

        if unique_reply_rank is not None:
            return replies[unique_reply_rank].get("result")
        return [reply.get("result") for reply in replies]

    def execute_request(self, scheduler_output: Any) -> Any:
        from vllm_omni.diffusion.worker.utils import BatchRunnerOutput, RunnerOutput

        outputs = []
        for new_req in scheduler_output.scheduled_new_reqs:
            try:
                result = self.collective_rpc(
                    "execute_model",
                    args=(new_req.req, self.od_config, scheduler_output.kv_prefetch_job),
                    unique_reply_rank=0,
                    exec_all_ranks=True,
                    request_id=new_req.request_id,
                )
                if not isinstance(result, DiffusionOutput):
                    raise RuntimeError(f"Unexpected response type {type(result)!r}")
            except Exception as exc:
                result = DiffusionOutput.from_exception(exc)
            outputs.append(
                RunnerOutput(
                    request_id=new_req.request_id,
                    step_index=None,
                    finished=True,
                    result=result,
                )
            )
        return BatchRunnerOutput.from_list(outputs)

    def execute_batch(self, scheduler_output: Any) -> Any:
        if len(scheduler_output.scheduled_new_reqs) <= 1:
            return self.execute_request(scheduler_output)
        return self.collective_rpc(
            "execute_model_batch",
            args=(scheduler_output, self.od_config),
            unique_reply_rank=0,
            exec_all_ranks=True,
        )

    def execute_step(self, scheduler_output: Any) -> Any:
        return self.collective_rpc(
            "execute_stepwise",
            args=(scheduler_output,),
            unique_reply_rank=0,
            exec_all_ranks=True,
        )

    def check_health(self) -> None:
        self._ensure_open()
        if not self._rpc_lock.acquire(blocking=False):
            return  # RPC owns the actors; its result reports failures.
        try:
            self._check_idle_health()
        finally:
            self._rpc_lock.release()

    def _check_idle_health(self) -> None:
        self._ensure_open()
        try:
            ready = self._ray.get([actor.ping.remote() for actor in self._actors], timeout=10)
        except BaseException as exc:
            self._mark_failed()
            raise EngineDeadError("MiniMax H3 distributed worker health check failed") from exc
        if sorted(item.get("rank") for item in ready) != [0, 1]:
            self._mark_failed()
            raise EngineDeadError(f"Unexpected worker health response: {ready}")

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._actors:
            return
        try:
            refs = [actor.shutdown.remote() for actor in self._actors]
            self._ray.get(refs, timeout=30)
        except Exception:
            logger.warning("Graceful two-node worker shutdown failed", exc_info=True)
        finally:
            for actor in self._actors:
                try:
                    self._ray.kill(actor, no_restart=True)
                except Exception:
                    pass
            self._actors = []
