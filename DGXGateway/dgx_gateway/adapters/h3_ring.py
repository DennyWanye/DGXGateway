"""Four-Spark MiniMax H3 Ring4 V2. New identity; never the two-node minimax-h3 adapter."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import shlex
import subprocess
import time

from ..config import GatewayConfig, NodeConfig
from ..domain import ObservedModel
from ..operations import OperationContext
from ..ssh import CommandResult, FixedSSH


MODEL_ID = "h3-ring4"
PROFILES = (
    "fl2va-official",
    "ref2va-official",
    "ref2va-heretic",
    "hybrid-experimental",
)
KIT_NAME = "h3-ring-kit-v2"
API_PORT = 30021
CONTAINER_PREFIX = "h3-ring-serve-r"
HEALTH_TIMEOUT_SECONDS = 3600
START_TIMEOUT_SECONDS = 90
STOP_TIMEOUT_SECONDS = 90
RENDER_TIMEOUT_SECONDS = 180


@dataclass(frozen=True)
class RankSpec:
    rank: int
    node: NodeConfig


RANKS: tuple[RankSpec, ...] = (
    RankSpec(0, NodeConfig("rank0", "dgx-head", "192.0.2.41", "operator")),
    RankSpec(1, NodeConfig("rank1", "dgx-worker", "192.0.2.42", "operator")),
    RankSpec(2, NodeConfig("rank2", "dgx-rank2", "192.0.2.39", "operator")),
    RankSpec(3, NodeConfig("rank3", "dgx-rank3", "192.0.2.40", "operator")),
)
START_ORDER = tuple(reversed(RANKS))
STOP_ORDER = RANKS


def kit_path(spec: RankSpec) -> str:
    return f"/home/{spec.node.user}/{KIT_NAME}"


def deployment_id(operation_id: str, profile: str) -> str:
    prefix = {
        "fl2va-official": "p0",
        "ref2va-official": "p1",
        "ref2va-heretic": "p2",
        "hybrid-experimental": "p3",
    }[profile]
    raw = re.sub(r"[^A-Za-z0-9_.-]", "", operation_id)[:48] or "op"
    return f"{prefix}-{raw}"


def docker_run_detached(rendered: str) -> list[str]:
    parts = shlex.split(rendered.strip())
    if len(parts) < 2 or parts[0] != "docker" or parts[1] != "run":
        raise RuntimeError("profile_node.py render did not return docker run")
    parts = [part for part in parts if part != "--rm"]
    if "-d" in parts or "--detach" in parts:
        return parts
    return ["docker", "run", "-d", *parts[2:]]


def _inspect_state(result: CommandResult) -> tuple[str, str | None, str | None]:
    if result.returncode:
        if result.returncode == 255:
            raise RuntimeError("h3 ring rank SSH failed")
        return "absent", None, None
    tokens = result.stdout.strip().split()
    running = tokens[0] if tokens else ""
    profile = tokens[1] if len(tokens) > 1 and tokens[1] in PROFILES else None
    deploy = tokens[2] if len(tokens) > 2 else None
    if running == "true":
        return "running", profile, deploy
    if running == "false":
        return "exited", profile, deploy
    if not result.stdout.strip():
        return "absent", None, None
    return "absent", None, None


class H3RingAdapter:
    model_id = MODEL_ID
    profiles = PROFILES
    default_profile = PROFILES[0]

    def __init__(self, config: GatewayConfig, ssh: FixedSSH | None = None):
        self.config = config
        self.ssh = ssh or FixedSSH()
        self.last_profile = PROFILES[0]
        self.last_deployment: str | None = None

    def _run(self, node: NodeConfig, argv: list[str], timeout: int = 10) -> CommandResult:
        return self.ssh.run(node, argv, timeout=timeout)

    def _inspect(self, spec: RankSpec) -> tuple[str, str | None, str | None]:
        result = self._run(
            spec.node,
            [
                "docker",
                "inspect",
                "-f",
                '{{.State.Running}} {{index .Config.Labels "h3.profile"}} {{index .Config.Labels "h3.deployment"}}',
                f"{CONTAINER_PREFIX}{spec.rank}",
            ],
            timeout=30,
        )
        return _inspect_state(result)

    def _api_up(self) -> bool:
        result = self._run(
            RANKS[0].node,
            ["sh", "-c", f"ss -H -ltn '( sport = :{API_PORT} )' || true"],
            timeout=15,
        )
        if result.returncode:
            return False
        return f":{API_PORT}" in result.stdout and "127.0.0.1" in result.stdout

    def _rank_logs(self) -> dict[str, str]:
        logs: dict[str, str] = {}
        for spec in RANKS:
            result = self._run(
                spec.node,
                ["docker", "logs", "--tail", "80", f"{CONTAINER_PREFIX}{spec.rank}"],
                timeout=20,
            )
            text = f"{result.stdout}{result.stderr}".strip()
            logs[str(spec.rank)] = text[-4000:]
        return logs

    def observe(self) -> ObservedModel:
        try:
            states = {spec.rank: self._inspect(spec) for spec in RANKS}
        except Exception:
            return ObservedModel(self.model_id, "UNKNOWN")
        values = tuple(states[spec.rank][0] for spec in RANKS)
        profiles = {states[spec.rank][1] for spec in RANKS if states[spec.rank][0] == "running"}
        deploys = {states[spec.rank][2] for spec in RANKS if states[spec.rank][0] == "running"}
        only = next(iter(profiles)) if len(profiles) == 1 else None
        profile = only if only in PROFILES else self.last_profile
        if len(deploys) == 1:
            self.last_deployment = next(iter(deploys))
        if all(state == "absent" for state in values):
            return ObservedModel(self.model_id, "STOPPED")
        if all(state == "running" for state in values):
            try:
                if self._api_up():
                    self.last_profile = profile
                    return ObservedModel(self.model_id, "HEALTHY", profile)
            except Exception:
                return ObservedModel(self.model_id, "PARTIAL", profile)
            return ObservedModel(self.model_id, "PARTIAL", profile)
        return ObservedModel(self.model_id, "PARTIAL", profile)

    def drain(self, context: OperationContext) -> None:
        context.guard()
        state = self.observe().state
        if state == "STOPPED":
            return
        if state != "HEALTHY":
            raise RuntimeError("h3 ring drain requires a healthy four-rank runtime")

    def _verify_rank(self, spec: RankSpec) -> None:
        result = self._run(spec.node, ["sh", "-c", "hostname; whoami"], timeout=15)
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if result.returncode or lines != [spec.node.hostname, spec.node.user]:
            raise RuntimeError(f"h3 ring rank verification failed: {spec.node.role}")

    def _stop_all(self, context: OperationContext) -> None:
        errors: list[str] = []
        for spec in STOP_ORDER:
            context.guard()
            name = f"{CONTAINER_PREFIX}{spec.rank}"
            try:
                result = self._run(
                    spec.node,
                    ["docker", "stop", "--time", "60", name],
                    timeout=STOP_TIMEOUT_SECONDS,
                )
                if result.returncode and "No such container" not in (result.stderr + result.stdout):
                    errors.append(f"rank{spec.rank}:{result.stderr.strip() or result.stdout.strip()}")
            except (subprocess.TimeoutExpired, OSError) as exc:
                errors.append(f"rank{spec.rank} stop:{exc}")
            try:
                removed = self._run(spec.node, ["docker", "rm", "-f", name], timeout=STOP_TIMEOUT_SECONDS)
                if removed.returncode and "No such container" not in (removed.stderr + removed.stdout):
                    errors.append(f"rank{spec.rank} rm:{removed.stderr.strip() or removed.stdout.strip()}")
            except (subprocess.TimeoutExpired, OSError) as exc:
                errors.append(f"rank{spec.rank} rm:{exc}")
        if errors:
            raise RuntimeError("h3 ring stop incomplete: " + "; ".join(errors))

    def stop(self, context: OperationContext) -> None:
        self._stop_all(context)

    def retire(self, context: OperationContext) -> None:
        self.stop(context)

    def stopped_proof(self, context: OperationContext) -> dict:
        context.guard()
        states = {spec.rank: self._inspect(spec)[0] for spec in RANKS}
        if any(state != "absent" for state in states.values()):
            raise RuntimeError("h3 ring ranks are not all absent")
        proof = {
            "model_id": self.model_id,
            "stopped": True,
            "operation_id": context.operation_id,
            "lease_generation": context.lease_generation,
            "ranks": {str(rank): state for rank, state in states.items()},
        }
        encoded = json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()
        return {**proof, "sha256": hashlib.sha256(encoded).hexdigest()}

    def _render(self, spec: RankSpec, profile: str, deploy: str) -> str:
        argv = [
            "python3",
            f"{kit_path(spec)}/scripts/profile_node.py",
            "render",
            profile,
            "--deployment-id",
            deploy,
            "--world",
            "4",
        ]
        if profile == "hybrid-experimental":
            argv.append("--allow-experiment")
        result = self._run(spec.node, argv, timeout=RENDER_TIMEOUT_SECONDS)
        if result.returncode:
            raise RuntimeError(
                f"rank {spec.rank} render failed: {result.stderr.strip() or result.stdout.strip()}"
            )
        line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        return line

    def start(self, context: OperationContext, profile: str | None = None) -> None:
        context.guard()
        chosen = profile or self.last_profile
        if chosen not in PROFILES:
            raise ValueError("invalid h3 ring profile")
        observed = self.observe()
        if observed.state == "HEALTHY" and observed.profile == chosen:
            return
        deploy = deployment_id(context.operation_id, chosen)
        for spec in RANKS:
            context.guard()
            self._verify_rank(spec)
        self._stop_all(context)
        try:
            for spec in START_ORDER:
                context.guard()
                rendered = self._render(spec, chosen, deploy)
                argv = docker_run_detached(rendered)
                result = self._run(spec.node, argv, timeout=START_TIMEOUT_SECONDS)
                if result.returncode:
                    raise RuntimeError(
                        f"rank {spec.rank} start failed: {result.stderr.strip() or result.stdout.strip()}"
                    )
        except BaseException:
            self._stop_all(context)
            raise
        self.last_profile = chosen
        self.last_deployment = deploy

    def health(self, context: OperationContext) -> dict:
        deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
        last_error = "h3 ring warmup timeout"
        while time.monotonic() < deadline:
            context.guard()
            observed = self.observe()
            if observed.state == "HEALTHY":
                return {
                    "healthy": True,
                    "model_id": self.model_id,
                    "profile": observed.profile,
                    "endpoint": f"http://127.0.0.1:{API_PORT}/v1",
                }
            if observed.state == "STOPPED":
                return {
                    "healthy": False,
                    "model_id": self.model_id,
                    "reason": "h3 ring ranks are absent after start",
                    "rank_logs": self._rank_logs(),
                }
            try:
                states = {spec.rank: self._inspect(spec)[0] for spec in RANKS}
            except (subprocess.TimeoutExpired, RuntimeError, OSError) as exc:
                # Unified-memory GB10 loads can starve sshd for minutes.
                # Aborting warmup here previously retired a still-loading ring.
                last_error = f"h3 ring inspect deferred: {exc}"
                time.sleep(2)
                continue
            if all(state in {"absent", "exited"} for state in states.values()):
                return {
                    "healthy": False,
                    "model_id": self.model_id,
                    "reason": "h3 ring ranks are absent or exited after start",
                    "rank_states": {str(rank): state for rank, state in states.items()},
                    "rank_logs": self._rank_logs(),
                }
            last_error = f"h3 ring state {observed.state}"
            time.sleep(2)
        return {"healthy": False, "model_id": self.model_id, "reason": last_error}
