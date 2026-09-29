from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..config import GatewayConfig, paired_nodes
from ..domain import ObservedModel
from ..operations import OperationContext
from ..ssh import FixedSSH
from .loader import load_module


class DeepSeekAdapter:
    model_id = "deepseek-v4-flash"
    text_fence_qualified = False
    STOPPED_PROOF_TIMEOUT_SECONDS = 45
    STOPPED_PROOF_POLL_SECONDS = 1

    def __init__(self, config: GatewayConfig, ssh: FixedSSH | None = None):
        self.config = config
        self.ssh = ssh or FixedSSH()
        self._module = None

    def _controller(self, context: OperationContext):
        if self._module is None:
            self._module = load_module(
                "dgx_gateway_deepseek_controller",
                self.config.repo_root / "scripts/control-deepseek-v4-flash-dspark.py",
            )
        module = self._module
        runner = module.SubprocessRunner()
        authority = module.LockAuthority(
            runner=runner,
            token_file=context.token_path,
            owner="dgx-cluster-gateway",
            lock_root=self.config.lock_root,
            lock_script=self.config.lock_cli,
            lock_history=self.config.lock_root.parent / f"{self.config.lock_root.name}-history",
        )
        return module.ClusterController(
            authority=authority,
            runner=runner,
            journal=module.Journal(self.config.deepseek_journal),
            task_id=context.operation_id,
        )

    def _probe(self) -> dict:
        def probe_node(node):
            result = self.ssh.run(
                node,
                [
                    "sh", "-c",
                    "hostname; whoami; sudo -n docker ps --filter label=com.docker.compose.project=deepseek-v4-flash-dspark --format '{{.Names}} {{.Status}}'; sudo -n docker ps --filter name=^/deepseek-v4-flash-gateway$ --format 'LEGACY_GATEWAY {{.Names}} {{.Status}}'; ss -H -ltn '( sport = :11434 or sport = :18888 )' || true",
                ],
                timeout=9,
            )
            return node.role, {
                "ok": result.returncode == 0,
                "stdout": result.stdout,
                "stderr_sha256": hashlib.sha256(result.stderr.encode()).hexdigest(),
            }
        targets = paired_nodes(self.config.nodes)
        with ThreadPoolExecutor(max_workers=len(targets)) as executor:
            return dict(executor.map(probe_node, targets))

    def _api_health(self) -> bool:
        try:
            secret = self.config.deepseek_gateway_secret
            if secret.is_symlink() or not secret.is_file() or secret.stat().st_mode & 0o077:
                return False
            token = secret.read_text(encoding="utf-8").strip()
            if len(token) < 16:
                return False
            request = urllib.request.Request(
                "http://192.0.2.30:11434/health",
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                return 200 <= response.status < 300
        except Exception:
            return False

    def _request_metrics(self) -> tuple[float, float]:
        head = next(node for node in self.config.nodes if node.role == "head")
        result = self.ssh.run(
            head,
            [
                "curl",
                "-fsS",
                "--max-time",
                "5",
                "http://127.0.0.1:18888/metrics",
            ],
            timeout=8,
        )
        if result.returncode:
            raise RuntimeError("DeepSeek internal metrics endpoint is unhealthy")
        text = result.stdout
        if len(text.encode("utf-8")) > 2 * 1024 * 1024:
            raise RuntimeError("DeepSeek metrics response is too large")
        values = {}
        for name in ("vllm:num_requests_running", "vllm:num_requests_waiting"):
            matches = re.findall(
                rf"^{re.escape(name)}(?:\{{[^}}]*\}})?\s+([0-9.eE+-]+)\s*$",
                text,
                flags=re.MULTILINE,
            )
            if not matches:
                raise RuntimeError(f"DeepSeek metric {name} is missing")
            values[name] = sum(float(item) for item in matches)
        return (
            values["vllm:num_requests_running"],
            values["vllm:num_requests_waiting"],
        )

    def observe(self) -> ObservedModel:
        try:
            probe = self._probe()
        except Exception:
            return ObservedModel(self.model_id, "UNKNOWN")
        if not all(item["ok"] for item in probe.values()):
            return ObservedModel(self.model_id, "UNKNOWN")
        head = probe["head"]["stdout"]
        worker = probe["worker"]["stdout"]
        head_running = "deepseek-v4-flash-dspark" in head and "Up" in head
        worker_running = "deepseek-v4-flash-dspark" in worker and "Up" in worker
        gateway_running = "LEGACY_GATEWAY deepseek-v4-flash-gateway Up" in head
        if head_running and worker_running and self._api_health():
            return ObservedModel(self.model_id, "HEALTHY")
        if not head_running and not worker_running and not gateway_running and ":18888" not in head:
            return ObservedModel(self.model_id, "STOPPED")
        return ObservedModel(self.model_id, "PARTIAL")

    @staticmethod
    def _probe_is_stopped(probe: dict) -> bool:
        if not all(item["ok"] for item in probe.values()):
            return False
        head = probe["head"]["stdout"]
        worker = probe["worker"]["stdout"]
        head_running = "deepseek-v4-flash-dspark" in head and "Up" in head
        worker_running = "deepseek-v4-flash-dspark" in worker and "Up" in worker
        gateway_running = "LEGACY_GATEWAY deepseek-v4-flash-gateway Up" in head
        return not head_running and not worker_running and not gateway_running and ":18888" not in head

    def drain(self, context: OperationContext) -> None:
        context.guard()
        observed = self.observe()
        if observed.state == "STOPPED":
            return
        if observed.state != "HEALTHY":
            raise RuntimeError("DeepSeek drain precondition is ambiguous")
        deadline = time.monotonic() + self.config.deepseek_drain_timeout_seconds
        consecutive_idle = 0
        while time.monotonic() < deadline:
            context.guard()
            running, waiting = self._request_metrics()
            if running == 0 and waiting == 0:
                consecutive_idle += 1
                if consecutive_idle >= 2:
                    return
            else:
                consecutive_idle = 0
            time.sleep(1)
        raise TimeoutError("DeepSeek request drain timed out")

    def _text_runtime_identity(self, *, admission_open: bool | None) -> dict:
        # Fixed read-only inventory: never expose environment, command line or auth.
        code = '''import json,subprocess,socket,getpass,pathlib
def run(*a):
 return subprocess.check_output(a,text=True).strip()
ids=run('sudo','-n','docker','ps','--no-trunc','--filter','label=com.docker.compose.project=deepseek-v4-flash-dspark','--format','{{.ID}}').splitlines()
containers=[json.loads(run('sudo','-n','docker','inspect','--format','{"id":{{json .Id}},"startedAt":{{json .State.StartedAt}},"restartCount":{{.RestartCount}},"pid":{{.State.Pid}},"running":{{.State.Running}}}',cid)) for cid in ids]
gateway=run('sudo','-n','docker','ps','--no-trunc','--filter','name=^/deepseek-v4-flash-gateway$','--format','{{.ID}}').splitlines()
print(json.dumps({'hostname':socket.gethostname(),'user':getpass.getuser(),'bootId':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'containers':containers,'gateway':gateway,'upstream':run('ss','-H','-ltn','sport = :18888'),'lan':run('ss','-H','-ltn','sport = :11434')}))
'''
        identities = {}
        gateway_id = None
        for node in paired_nodes(self.config.nodes):
            result = self.ssh.run(node, ["python3", "-c", code], timeout=12)
            if result.returncode or len(result.stdout) > 16384:
                raise RuntimeError("text runtime identity unavailable")
            value = json.loads(result.stdout)
            expected_host = {"head": "dgx-head", "worker": "dgx-worker"}[node.role]
            if value["hostname"] != expected_host or value["user"] != "operator":
                raise RuntimeError("text runtime target identity mismatch")
            ids = value["containers"]
            if (len(ids) != 1 or not isinstance(ids[0], dict)
                    or not re.fullmatch(r"[a-f0-9]{64}", ids[0].get("id", ""))
                    or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T[0-9:.]+Z", ids[0].get("startedAt", ""))
                    or type(ids[0].get("restartCount")) is not int or ids[0]["restartCount"] < 0
                    or type(ids[0].get("pid")) is not int or ids[0]["pid"] <= 0
                    or ids[0].get("running") is not True):
                raise RuntimeError("text runtime requires one pinned running model instance per node")
            if node.role == "head":
                listeners = [line.split()[3] for line in value["upstream"].splitlines()]
                if listeners != ["127.0.0.1:18888"]:
                    raise RuntimeError("text backend is not exclusively bound to loopback")
                if admission_open is True or (admission_open is None and value["gateway"]):
                    if len(value["gateway"]) != 1 or not re.fullmatch(r"[a-f0-9]{64}", value["gateway"][0]):
                        raise RuntimeError("text gateway identity unavailable")
                    gateway_id = value["gateway"][0]
                    if admission_open is True and not value["lan"]:
                        raise RuntimeError("text gateway admission is not open")
                elif value["gateway"] or value["lan"]:
                    raise RuntimeError("text gateway admission is still open")
            elif value["upstream"] or value["gateway"] or value["lan"]:
                raise RuntimeError("worker exposes an unexpected text admission endpoint")
            identities[node.role] = {"bootId": value["bootId"], "containers": ids}
        if set(identities) != {"head", "worker"}:
            raise RuntimeError("both text runtime nodes must be verified")
        return {"nodes": identities, "gateway_container_id": gateway_id}

    def capture_text_runtime(self, context: OperationContext) -> dict:
        context.guard()
        result = self._text_runtime_identity(admission_open=True)
        context.guard()
        return result

    def fence_text_requests(self, context: OperationContext, expected_identity: dict) -> dict:
        if not self.text_fence_qualified:
            raise RuntimeError("text admission fence is not qualified")
        context.guard()
        before = self._text_runtime_identity(admission_open=None)
        if (before["nodes"] != expected_identity["nodes"]
                or before["gateway_container_id"] not in (None, expected_identity["gateway_container_id"])):
            raise RuntimeError("text runtime changed before admission fence")
        context.guard()
        # Parent text lease IDs have a prefix; the remote transaction protocol
        # accepts exactly 32 hex characters. Keep authority on the original
        # context and derive only the helper transaction ID, bound to its lease.
        transaction_id = hashlib.sha256(json.dumps(
            ["text-admission-fence-v1", context.operation_id, context.lease_generation],
            separators=(",", ":"),
        ).encode()).hexdigest()[:32]
        receipt = self._controller(context).production_fence_gateway(
            transaction_id, expected_identity["gateway_container_id"], _under_active=True)
        deadline = time.monotonic() + self.config.deepseek_drain_timeout_seconds
        while True:
            context.guard()
            after = self._text_runtime_identity(admission_open=False)
            if after["nodes"] != expected_identity["nodes"]:
                raise RuntimeError("text model runtime changed during drain")
            if self._request_metrics() == (0, 0):
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("text backend did not drain after admission closed")
            time.sleep(1)
        context.guard()
        return {"admission_closed": True, "inflight_zero": True,
                "runtime_identity": expected_identity, "lease_generation": context.lease_generation,
                "receipt_sha256": hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()}

    def stop(self, context: OperationContext) -> None:
        controller = self._controller(context)
        controller.production_deactivate_gateway(context.operation_id, _under_active=True)
        controller.stop(context.operation_id, _under_active=True)

    def retire(self, context: OperationContext) -> None:
        errors = []
        controller = self._controller(context)
        for callback in (
            lambda: controller.production_deactivate_gateway(
                context.operation_id, _under_active=True
            ),
            lambda: controller.stop(context.operation_id, _under_active=True),
        ):
            try:
                callback()
            except Exception as exc:
                errors.append(str(exc))
        if errors and self.observe().state != "STOPPED":
            raise RuntimeError("DeepSeek retirement incomplete: " + "; ".join(errors))

    def stopped_proof(self, context: OperationContext) -> dict:
        deadline = time.monotonic() + self.STOPPED_PROOF_TIMEOUT_SECONDS
        while True:
            context.guard()
            probe = self._probe()
            if self._probe_is_stopped(probe):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("DeepSeek stopped proof unavailable")
            time.sleep(self.STOPPED_PROOF_POLL_SECONDS)
        canonical = json.dumps(probe, sort_keys=True, separators=(",", ":")).encode()
        return {
            "model_id": self.model_id,
            "stopped": True,
            "lease_generation": context.lease_generation,
            "sha256": hashlib.sha256(canonical).hexdigest(),
        }

    def start(self, context: OperationContext, profile: str | None = None) -> None:
        del profile
        controller = self._controller(context)
        controller.start(context.operation_id, _under_active=True)
        controller.production_activate_gateway(context.operation_id, _under_active=True)

    def health(self, context: OperationContext) -> dict:
        context.guard()
        return {"healthy": self.observe().state == "HEALTHY"}
