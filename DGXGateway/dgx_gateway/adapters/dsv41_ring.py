"""Four-Spark switchless Ring DeepSeek-V4.1-Flash. New identity; never mia-dsv41-ablit."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import time

from ..config import GatewayConfig, NodeConfig
from ..domain import ObservedModel
from ..operations import OperationContext
from ..ssh import CommandResult, FixedSSH


MODEL_ID = "dsv41-ring-tp4"
SERVED_MODEL = "deepseek-v4.1-flash-ring"
CONTAINER = "dsv41-ring-tp4"
OWNER_LABEL = "step-by-step-20260917"
PACKAGE_NAME = "DeepSeek-V41-4DGX-Ring-Package"
STAGE = "smoke32k"
PROFILES = ("smoke32k", "text256k", "text512k")
API_PORT = 8891
HEALTH_TIMEOUT_SECONDS = {"smoke32k": 1200, "text256k": 3600, "text512k": 10800}
CANARY_TIMEOUT_SECONDS = {"smoke32k": 90, "text256k": 600, "text512k": 900}
DRAIN_TIMEOUT_SECONDS = 120
STOP_TIMEOUT_SECONDS = 90
START_TIMEOUT_SECONDS = 90


@dataclass(frozen=True)
class RankSpec:
    rank: int
    node: NodeConfig

    @property
    def package(self) -> str:
        return f"/home/{self.node.user}/{PACKAGE_NAME}"


RANKS: tuple[RankSpec, ...] = (
    RankSpec(0, NodeConfig("rank0", "dgx-head", "192.0.2.41", "operator")),
    RankSpec(1, NodeConfig("rank1", "dgx-worker", "192.0.2.42", "operator")),
    RankSpec(2, NodeConfig("rank2", "dgx-rank2", "192.0.2.39", "operator")),
    RankSpec(3, NodeConfig("rank3", "dgx-rank3", "192.0.2.40", "operator")),
)
START_ORDER = tuple(reversed(RANKS))
STOP_ORDER = RANKS
HEAD = RANKS[0]


def parse_request_metrics(text: str) -> tuple[float, float]:
    running = waiting = None
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name = line.split("{", 1)[0].split(" ", 1)[0]
        if name in {"vllm:num_requests_running", "vllm_num_requests_running"}:
            running = float(line.split()[-1])
        elif name in {"vllm:num_requests_waiting", "vllm_num_requests_waiting"}:
            waiting = float(line.split()[-1])
    if running is None or waiting is None:
        raise RuntimeError("vLLM request metrics unavailable")
    return running, waiting


def _inspect_state(result: CommandResult) -> tuple[str, str | None]:
    if result.returncode:
        if result.returncode == 255:
            raise RuntimeError("ring rank SSH failed")
        return "absent", None
    tokens = result.stdout.strip().split()
    running = tokens[0] if tokens else ""
    label = tokens[1] if len(tokens) > 1 else ""
    stage = tokens[2] if len(tokens) > 2 and tokens[2] in PROFILES else None
    if running == "true" and label == OWNER_LABEL:
        return "running", stage
    if running == "false" and label == OWNER_LABEL:
        return "exited", stage
    if not result.stdout.strip():
        return "absent", None
    return "foreign", None


class Dsv41RingAdapter:
    model_id = MODEL_ID
    profiles = PROFILES
    default_profile = STAGE

    def __init__(self, config: GatewayConfig, ssh: FixedSSH | None = None):
        self.config = config
        self.ssh = ssh or FixedSSH()
        self.last_profile = STAGE

    def _run(self, node: NodeConfig, argv: list[str], timeout: int = 10) -> CommandResult:
        return self.ssh.run(node, argv, timeout=timeout)

    def _inspect(self, spec: RankSpec) -> tuple[str, str | None]:
        result = self._run(
            spec.node,
            [
                "docker",
                "inspect",
                "-f",
                '{{.State.Running}} {{index .Config.Labels "local.dsv41-ring.owner"}} {{index .Config.Labels "local.dsv41-ring.stage"}}',
                CONTAINER,
            ],
        )
        return _inspect_state(result)

    def _rank_states(self) -> dict[int, tuple[str, str | None]]:
        return {spec.rank: self._inspect(spec) for spec in RANKS}

    def _served_ready(self) -> bool:
        script = (
            "set -a; . /srv/dsv41-ring/secrets/api.env; set +a; "
            "curl -fsS --max-time 5 "
            '-H "Authorization: Bearer $VLLM_API_KEY" '
            f"http://127.0.0.1:{API_PORT}/v1/models"
        )
        result = self._run(HEAD.node, ["sh", "-c", script], timeout=15)
        if result.returncode:
            return False
        payload = json.loads(result.stdout)
        names = [item.get("id") for item in payload.get("data", [])]
        return SERVED_MODEL in names

    def observe(self) -> ObservedModel:
        try:
            states = self._rank_states()
        except Exception:
            return ObservedModel(self.model_id, "UNKNOWN")
        values = tuple(states[spec.rank][0] for spec in RANKS)
        stages = {states[spec.rank][1] for spec in RANKS if states[spec.rank][0] == "running"}
        only = next(iter(stages)) if len(stages) == 1 else None
        profile = only if only in PROFILES else self.last_profile
        if all(state == "absent" for state in values):
            return ObservedModel(self.model_id, "STOPPED")
        if all(state == "running" for state in values):
            try:
                if self._served_ready():
                    self.last_profile = profile
                    return ObservedModel(self.model_id, "HEALTHY", profile)
            except Exception:
                return ObservedModel(self.model_id, "PARTIAL", profile)
            return ObservedModel(self.model_id, "PARTIAL", profile)
        return ObservedModel(self.model_id, "PARTIAL", profile)

    def _metrics(self) -> tuple[float, float]:
        result = self._run(
            HEAD.node,
            ["curl", "-fsS", "--max-time", "5", f"http://127.0.0.1:{API_PORT}/metrics"],
        )
        if result.returncode:
            raise RuntimeError("ring metrics probe failed")
        return parse_request_metrics(result.stdout)

    def drain(self, context: OperationContext) -> None:
        context.guard()
        state = self.observe().state
        if state == "STOPPED":
            return
        if state != "HEALTHY":
            raise RuntimeError("ring drain requires a healthy four-rank runtime")
        deadline = time.monotonic() + DRAIN_TIMEOUT_SECONDS
        idle = 0
        while time.monotonic() < deadline:
            context.guard()
            running, waiting = self._metrics()
            idle = idle + 1 if running == waiting == 0 else 0
            if idle >= 3:
                return
            time.sleep(1)
        raise TimeoutError("ring in-flight requests did not drain")

    def _ringctl(self, spec: RankSpec, action: str, timeout: int, stage: str = STAGE) -> CommandResult:
        package = spec.package
        argv = [
            "python3",
            f"{package}/scripts/ringctl.py",
            "--config",
            f"{package}/config/cluster.json",
            action,
            "--rank",
            str(spec.rank),
            "--maintenance-approved",
        ]
        if action == "start":
            argv += ["--stage", stage]
        return self._run(spec.node, argv, timeout=timeout)

    def _verify_rank(self, spec: RankSpec) -> None:
        result = self._run(spec.node, ["sh", "-c", "hostname; whoami"], timeout=15)
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if result.returncode or lines != [spec.node.hostname, spec.node.user]:
            raise RuntimeError(f"ring rank verification failed: {spec.node.role}")

    def _stop_all(self, context: OperationContext) -> None:
        errors: list[str] = []
        for spec in STOP_ORDER:
            context.guard()
            result = self._ringctl(spec, "stop", STOP_TIMEOUT_SECONDS)
            if result.returncode:
                errors.append(f"rank{spec.rank}:{result.stderr.strip() or result.stdout.strip()}")
        if errors:
            raise RuntimeError("ring stop incomplete: " + "; ".join(errors))

    def stop(self, context: OperationContext) -> None:
        self._stop_all(context)

    def retire(self, context: OperationContext) -> None:
        self.stop(context)

    def stopped_proof(self, context: OperationContext) -> dict:
        context.guard()
        states = self._rank_states()
        if any(state != "absent" for state, _stage in states.values()):
            raise RuntimeError("ring ranks are not all absent")
        proof = {
            "model_id": self.model_id,
            "stopped": True,
            "operation_id": context.operation_id,
            "lease_generation": context.lease_generation,
            "ranks": {str(rank): state for rank, (state, _stage) in states.items()},
        }
        encoded = json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()
        return {**proof, "sha256": hashlib.sha256(encoded).hexdigest()}

    def start(self, context: OperationContext, profile: str | None = None) -> None:
        context.guard()
        chosen = profile or self.last_profile
        if chosen not in PROFILES:
            raise ValueError("invalid ring profile")
        observed = self.observe()
        if observed.state == "HEALTHY" and observed.profile == chosen:
            return
        for spec in RANKS:
            context.guard()
            self._verify_rank(spec)
        self._stop_all(context)
        try:
            for spec in START_ORDER:
                context.guard()
                result = self._ringctl(spec, "start", START_TIMEOUT_SECONDS, chosen)
                if result.returncode:
                    raise RuntimeError(
                        f"rank {spec.rank} start failed: {result.stderr.strip() or result.stdout.strip()}"
                    )
        except BaseException:
            self._stop_all(context)
            raise
        self.last_profile = chosen

    def _canary(self) -> str:
        timeout = CANARY_TIMEOUT_SECONDS.get(self.last_profile, 90)
        script = (
            "python3 -c \""
            "import json,urllib.request; from pathlib import Path;"
            "key=Path('/srv/dsv41-ring/secrets/api.env').read_text().split('=',1)[1].strip();"
            "req=urllib.request.Request("
            f"'http://127.0.0.1:{API_PORT}/v1/chat/completions',"
            "data=json.dumps({'model':'" + SERVED_MODEL + "','messages':"
            "[{'role':'user','content':'17乘以19等于多少？只输出数字。'}],"
            "'max_tokens':256,'temperature':0}).encode(),"
            "headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'});"
            f"print(json.loads(urllib.request.urlopen(req,timeout={timeout}).read())"
            "['choices'][0]['message']['content'].strip())"
            "\""
        )
        result = self._run(HEAD.node, ["sh", "-c", script], timeout=timeout + 30)
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()[:200]
            raise RuntimeError("ring canary request failed" + (f": {detail}" if detail else ""))
        return result.stdout.strip()

    def health(self, context: OperationContext) -> dict:
        deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS.get(self.last_profile, 1200)
        last_error = "ring warmup timeout"
        while time.monotonic() < deadline:
            context.guard()
            if self.observe().state == "HEALTHY":
                try:
                    passed = self._canary() == "323"
                except RuntimeError as exc:
                    last_error = str(exc)
                    time.sleep(5)
                    continue
                if passed:
                    return {
                        "healthy": True,
                        "model_id": self.model_id,
                        "canary_passed": True,
                        "served_model": SERVED_MODEL,
                        "tp": 4,
                        "profile": self.last_profile,
                        "endpoint": f"http://127.0.0.1:{API_PORT}/v1",
                    }
                last_error = "ring canary answer mismatch"
                time.sleep(5)
                continue
            time.sleep(2)
        return {"healthy": False, "model_id": self.model_id, "reason": last_error}
