"""Two independent two-Spark MiniMax H3 services managed as one Gateway model.

The four physical hosts are partitioned into pair-a (rank0/rank1) and pair-b
(rank2/rank3).  Each service is a separate world=2 SGLang process group and
therefore may execute one video at the same time as the other pair.  This is a
new lifecycle identity; it never aliases or partially reuses the Ring4 ranks.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import subprocess
import time

from ..config import GatewayConfig, NodeConfig
from ..domain import ObservedModel
from ..operations import OperationContext
from ..ssh import CommandResult, FixedSSH
from .h3_ring import docker_run_detached, _inspect_state


MODEL_ID = "h3-dual-pair"
PROFILES = ("fl2va-official", "ref2va-official", "ref2va-heretic")
KIT_NAME = "h3-ring-kit-v2"
API_PORT = 30021
HEALTH_TIMEOUT_SECONDS = 3600
START_TIMEOUT_SECONDS = 90
STOP_TIMEOUT_SECONDS = 90
RENDER_TIMEOUT_SECONDS = 180


@dataclass(frozen=True)
class PairRank:
    rank: int
    node: NodeConfig


@dataclass(frozen=True)
class PairSpec:
    pair_id: str
    ranks: tuple[PairRank, PairRank]
    config_name: str
    node_prefix: str
    container_prefix: str

    @property
    def head(self) -> PairRank:
        return self.ranks[0]


PAIR_A = PairSpec(
    "pair-a",
    (
        PairRank(0, NodeConfig("head", "dgx-head", "192.0.2.41", "operator")),
        PairRank(1, NodeConfig("worker", "dgx-worker", "192.0.2.42", "operator")),
    ),
    "pair-a-cluster.json",
    "pair-a-node-r",
    "h3-pair-a-r",
)
PAIR_B = PairSpec(
    "pair-b",
    (
        PairRank(0, NodeConfig("rank2", "dgx-rank2", "192.0.2.39", "operator")),
        PairRank(1, NodeConfig("rank3", "dgx-rank3", "192.0.2.40", "operator")),
    ),
    "pair-b-cluster.json",
    "pair-b-node-r",
    "h3-pair-b-r",
)
PAIRS = (PAIR_A, PAIR_B)


def kit_path(node: NodeConfig) -> str:
    return f"/home/{node.user}/{KIT_NAME}"


def container_name(pair: PairSpec, rank: int) -> str:
    return f"{pair.container_prefix}{rank}"


def deployment_id(operation_id: str, profile: str, pair_id: str) -> str:
    prefix = {
        "fl2va-official": "fl",
        "ref2va-official": "ref",
        "ref2va-heretic": "heretic",
    }[profile]
    raw = re.sub(r"[^A-Za-z0-9_.-]", "", operation_id)[:40] or "op"
    return f"dual-{pair_id}-{prefix}-{raw}"


class H3DualPairAdapter:
    model_id = MODEL_ID
    profiles = PROFILES
    default_profile = "ref2va-official"

    def __init__(self, config: GatewayConfig, ssh: FixedSSH | None = None):
        self.config = config
        self.ssh = ssh or FixedSSH()
        self.last_profile = self.default_profile

    def _run(self, node: NodeConfig, argv: list[str], timeout: int = 10) -> CommandResult:
        return self.ssh.run(node, argv, timeout=timeout)

    def _inspect(self, pair: PairSpec, rank: PairRank) -> tuple[str, str | None, str | None]:
        result = self._run(
            rank.node,
            [
                "docker", "inspect", "-f",
                '{{.State.Running}} {{index .Config.Labels "h3.profile"}} {{index .Config.Labels "h3.deployment"}}',
                container_name(pair, rank.rank),
            ],
            timeout=30,
        )
        return _inspect_state(result)

    def _api_up(self, pair: PairSpec) -> bool:
        result = self._run(
            pair.head.node,
            ["sh", "-c", f"ss -H -ltn '( sport = :{API_PORT} )' || true"],
            timeout=15,
        )
        return result.returncode == 0 and f":{API_PORT}" in result.stdout and "127.0.0.1" in result.stdout

    def _logs(self) -> dict[str, str]:
        logs: dict[str, str] = {}
        for pair in PAIRS:
            for rank in pair.ranks:
                result = self._run(
                    rank.node,
                    ["docker", "logs", "--tail", "80", container_name(pair, rank.rank)],
                    timeout=20,
                )
                value = f"{result.stdout}{result.stderr}".strip()
                logs[f"{pair.pair_id}-r{rank.rank}"] = value[-4000:]
        return logs

    def observe(self) -> ObservedModel:
        try:
            states = {
                (pair.pair_id, rank.rank): self._inspect(pair, rank)
                for pair in PAIRS for rank in pair.ranks
            }
        except Exception:
            return ObservedModel(self.model_id, "UNKNOWN", self.last_profile)
        values = tuple(value[0] for value in states.values())
        profiles = {value[1] for value in states.values() if value[0] == "running"}
        profile = next(iter(profiles)) if len(profiles) == 1 else self.last_profile
        if all(value == "absent" for value in values):
            return ObservedModel(self.model_id, "STOPPED")
        if all(value == "running" for value in values) and len(profiles) == 1:
            try:
                if all(self._api_up(pair) for pair in PAIRS):
                    self.last_profile = profile
                    return ObservedModel(self.model_id, "HEALTHY", profile)
            except Exception:
                pass
        return ObservedModel(self.model_id, "PARTIAL", profile)

    def drain(self, context: OperationContext) -> None:
        context.guard()
        state = self.observe().state
        if state == "STOPPED":
            return
        if state != "HEALTHY":
            raise RuntimeError("dual-pair H3 drain requires two healthy pair services")

    def _verify_rank(self, rank: PairRank) -> None:
        result = self._run(rank.node, ["sh", "-c", "hostname; whoami"], timeout=15)
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if result.returncode or lines != [rank.node.hostname, rank.node.user]:
            raise RuntimeError(f"dual-pair H3 identity mismatch: {rank.node.role}")

    def _stop_all(self, context: OperationContext) -> None:
        errors: list[str] = []
        for pair in PAIRS:
            for rank in pair.ranks:
                context.guard()
                name = container_name(pair, rank.rank)
                try:
                    result = self._run(rank.node, ["docker", "stop", "--time", "60", name], timeout=STOP_TIMEOUT_SECONDS)
                    if result.returncode and "No such container" not in (result.stderr + result.stdout):
                        errors.append(f"{pair.pair_id}-r{rank.rank}:{result.stderr.strip() or result.stdout.strip()}")
                except (subprocess.TimeoutExpired, OSError) as exc:
                    errors.append(f"{pair.pair_id}-r{rank.rank} stop:{exc}")
                try:
                    result = self._run(rank.node, ["docker", "rm", "-f", name], timeout=STOP_TIMEOUT_SECONDS)
                    if result.returncode and "No such container" not in (result.stderr + result.stdout):
                        errors.append(f"{pair.pair_id}-r{rank.rank} rm:{result.stderr.strip() or result.stdout.strip()}")
                except (subprocess.TimeoutExpired, OSError) as exc:
                    errors.append(f"{pair.pair_id}-r{rank.rank} rm:{exc}")
        if errors:
            raise RuntimeError("dual-pair H3 stop incomplete: " + "; ".join(errors))

    def stop(self, context: OperationContext) -> None:
        self._stop_all(context)

    def retire(self, context: OperationContext) -> None:
        self.stop(context)

    def stopped_proof(self, context: OperationContext) -> dict:
        context.guard()
        states = {
            f"{pair.pair_id}-r{rank.rank}": self._inspect(pair, rank)[0]
            for pair in PAIRS for rank in pair.ranks
        }
        if any(state != "absent" for state in states.values()):
            raise RuntimeError("dual-pair H3 containers are not all absent")
        proof = {
            "model_id": self.model_id,
            "stopped": True,
            "operation_id": context.operation_id,
            "lease_generation": context.lease_generation,
            "pairs": states,
        }
        encoded = json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()
        return {**proof, "sha256": hashlib.sha256(encoded).hexdigest()}

    def _render(self, pair: PairSpec, rank: PairRank, profile: str, deploy: str) -> str:
        root = kit_path(rank.node)
        argv = [
            "python3", f"{root}/scripts/profile_node.py", "render", profile,
            "--cluster", f"{root}/config/{pair.config_name}",
            "--node", f"{root}/config/{pair.node_prefix}{rank.rank}.json",
            "--deployment-id", deploy,
            "--world", "2",
            "--container-prefix", pair.container_prefix,
        ]
        result = self._run(rank.node, argv, timeout=RENDER_TIMEOUT_SECONDS)
        if result.returncode:
            raise RuntimeError(
                f"{pair.pair_id} rank {rank.rank} render failed: {result.stderr.strip() or result.stdout.strip()}"
            )
        return result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""

    def start(self, context: OperationContext, profile: str | None = None) -> None:
        context.guard()
        chosen = profile or self.last_profile
        if chosen not in PROFILES:
            raise ValueError("invalid dual-pair H3 profile")
        observed = self.observe()
        if observed.state == "HEALTHY" and observed.profile == chosen:
            return
        for pair in PAIRS:
            for rank in pair.ranks:
                self._verify_rank(rank)
        self._stop_all(context)
        rendered: dict[tuple[str, int], str] = {}
        for pair in PAIRS:
            deploy = deployment_id(context.operation_id, chosen, pair.pair_id)
            for rank in pair.ranks:
                rendered[(pair.pair_id, rank.rank)] = self._render(pair, rank, chosen, deploy)
        # Both workers must join before either head begins model initialization.
        order = [(pair, pair.ranks[1]) for pair in PAIRS] + [(pair, pair.ranks[0]) for pair in PAIRS]
        try:
            for pair, rank in order:
                context.guard()
                argv = docker_run_detached(rendered[(pair.pair_id, rank.rank)])
                result = self._run(rank.node, argv, timeout=START_TIMEOUT_SECONDS)
                if result.returncode:
                    raise RuntimeError(
                        f"{pair.pair_id} rank {rank.rank} start failed: {result.stderr.strip() or result.stdout.strip()}"
                    )
        except BaseException:
            self._stop_all(context)
            raise
        self.last_profile = chosen

    def health(self, context: OperationContext) -> dict:
        deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
        last = "dual-pair H3 warmup timeout"
        while time.monotonic() < deadline:
            context.guard()
            observed = self.observe()
            if observed.state == "HEALTHY":
                return {
                    "healthy": True,
                    "model_id": self.model_id,
                    "profile": observed.profile,
                    "execution_slots": [pair.pair_id for pair in PAIRS],
                    "endpoints": {pair.pair_id: f"ssh://{pair.head.node.address}/127.0.0.1:{API_PORT}/v1" for pair in PAIRS},
                }
            if observed.state == "STOPPED":
                return {"healthy": False, "model_id": self.model_id, "reason": "dual-pair ranks exited", "logs": self._logs()}
            last = f"dual-pair H3 state {observed.state}"
            time.sleep(2)
        return {"healthy": False, "model_id": self.model_id, "reason": last, "logs": self._logs()}
