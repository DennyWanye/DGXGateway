from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.request
from dataclasses import dataclass

from ..config import GatewayConfig
from ..domain import ObservedModel
from ..operations import OperationContext
from ..ssh import FixedSSH
from .loader import load_module
from .paired_probe import (
    PairedComposeProbeSpec,
    paired_probe_state,
    probe_paired_compose_target,
)


@dataclass(frozen=True)
class VisionAdapterSpec:
    model_id: str = "deepseek-v4-flash-vision-exp"
    compose_project: str = "deepseek-v4-flash-vision-exp"
    compose_service: str = "vllm-vision"
    systemd_unit: str = "deepseek-v4-flash-vision-exp.service"
    controller_module: str = "dgx_gateway_deepseek_vision_controller"
    controller_path: str = "scripts/control-deepseek-v4-flash-vision-exp.py"
    production_port: int = 11434


class DeepSeekVisionAdapter:
    model_id = VisionAdapterSpec.model_id
    context_profiles = {"32K": 32768, "64K": 65536, "128K": 131072, "256K": 262144, "512K": 524288}
    profiles = tuple(context_profiles)
    default_profile = "256K"
    STOPPED_PROOF_TIMEOUT_SECONDS = 45
    STOPPED_PROOF_POLL_SECONDS = 1

    def __init__(
        self,
        config: GatewayConfig,
        ssh: FixedSSH | None = None,
        *,
        spec: VisionAdapterSpec | None = None,
    ):
        self.config = config
        self.ssh = ssh or FixedSSH()
        self.spec = spec or VisionAdapterSpec()
        self._module = None
        self._probe_spec = PairedComposeProbeSpec(
            project=self.spec.compose_project,
            service=self.spec.compose_service,
            model_id=self.spec.model_id,
            unit=self.spec.systemd_unit,
            internal_port=config.deepseek_vision_internal_port,
        )

    def _controller(self, context: OperationContext):
        if self._module is None:
            self._module = load_module(
                self.spec.controller_module,
                self.config.repo_root / self.spec.controller_path,
            )
        module = self._module
        runner = module.SubprocessRunner()
        authority = module.LockAuthority(
            runner=runner,
            token_file=context.token_path,
            owner="dgx-cluster-gateway",
            lock_root=self.config.lock_root,
            lock_script=self.config.lock_cli,
            lock_history=self.config.lock_root.parent
            / f"{self.config.lock_root.name}-history",
        )
        return module.ClusterController(
            authority=authority,
            runner=runner,
            journal=module.Journal(self.config.deepseek_vision_journal),
            task_id=context.operation_id,
        )

    def _probe(self) -> dict[str, dict]:
        return probe_paired_compose_target(
            self.ssh, self.config.nodes, self._probe_spec
        )

    def _read_token(self) -> str | None:
        secret = self.config.deepseek_vision_gateway_secret
        try:
            if secret.is_symlink() or not secret.is_file() or secret.stat().st_mode & 0o077:
                return None
            token = secret.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return token if len(token) >= 16 else None

    def _api_model(self) -> dict | None:
        token = self._read_token()
        if token is None:
            return None
        head = next(node for node in self.config.nodes if node.role == "head")
        try:
            request = urllib.request.Request(
                f"http://{head.address}:{self.spec.production_port}/v1/models",
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status != 200:
                    return None
                declared = int(response.headers.get("Content-Length", "0") or 0)
                if declared > 1024 * 1024:
                    return None
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    return None
                payload = json.loads(raw)
        except Exception:
            return None
        models = payload.get("data") if isinstance(payload, dict) else None
        identities = {
            item.get("id") for item in models or () if isinstance(item, dict)
        }
        if identities != {self.model_id}:
            return None
        return next(item for item in models if item.get("id") == self.model_id)

    def _api_identity(self) -> bool:
        return self._api_model() is not None

    def _request_metrics(self) -> tuple[float, float]:
        head = next(node for node in self.config.nodes if node.role == "head")
        result = self.ssh.run(
            head,
            [
                "curl",
                "-fsS",
                "--max-time",
                "5",
                f"http://127.0.0.1:{self.config.deepseek_vision_internal_port}/metrics",
            ],
            timeout=8,
        )
        if result.returncode:
            raise RuntimeError("DeepSeek Vision internal metrics endpoint is unhealthy")
        if len(result.stdout.encode("utf-8")) > 2 * 1024 * 1024:
            raise RuntimeError("DeepSeek Vision metrics response is too large")
        values: dict[str, float] = {}
        for name in ("vllm:num_requests_running", "vllm:num_requests_waiting"):
            matches = re.findall(
                rf"^{re.escape(name)}(?:\{{[^}}]*\}})?\s+([0-9.eE+-]+)\s*$",
                result.stdout,
                flags=re.MULTILINE,
            )
            if not matches:
                raise RuntimeError(f"DeepSeek Vision metric {name} is missing")
            values[name] = sum(float(item) for item in matches)
        return values["vllm:num_requests_running"], values["vllm:num_requests_waiting"]

    def observe(self) -> ObservedModel:
        try:
            state = paired_probe_state(self._probe())
        except Exception:
            return ObservedModel(self.model_id, "UNKNOWN")
        if state == "RUNNING":
            model = self._api_model()
            profile = next((name for name, tokens in self.context_profiles.items()
                            if model and model.get("max_model_len") == tokens), None)
            return ObservedModel(self.model_id, "HEALTHY" if model and profile else "PARTIAL", profile)
        return ObservedModel(self.model_id, state)

    def drain(self, context: OperationContext) -> None:
        context.guard()
        observed = self.observe()
        if observed.state == "STOPPED":
            return
        if observed.state != "HEALTHY":
            raise RuntimeError("DeepSeek Vision drain precondition is ambiguous")
        deadline = time.monotonic() + self.config.deepseek_vision_drain_timeout_seconds
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
        raise TimeoutError("DeepSeek Vision request drain timed out")

    def stop(self, context: OperationContext) -> None:
        controller = self._controller(context)
        controller.production_deactivate_gateway(
            context.operation_id, _under_active=True
        )
        controller.stop(context.operation_id, _under_active=True)

    def retire(self, context: OperationContext) -> None:
        errors: list[str] = []
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
            raise RuntimeError(
                "DeepSeek Vision retirement incomplete: " + "; ".join(errors)
            )

    def fail_close_head(self, context: OperationContext) -> dict:
        """Fail closed locally when global worker cleanup cannot be proven."""
        context.guard()
        proof = self._controller(context).fail_close_head(
            context.operation_id, _under_active=True
        )
        expected = {
            "model_id": self.model_id,
            "head_fail_closed": True,
            "maintenance_active": True,
            "recovery_id": context.operation_id,
        }
        if not isinstance(proof, dict) or any(proof.get(k) != v for k, v in expected.items()):
            raise RuntimeError("DeepSeek Vision head fail-close proof is invalid")
        public = dict(expected)
        if isinstance(proof.get("sha256"), str) and re.fullmatch(
            r"[0-9a-f]{64}", proof["sha256"]
        ):
            public["sha256"] = proof["sha256"]
        return public

    def stopped_proof(self, context: OperationContext) -> dict:
        deadline = time.monotonic() + self.STOPPED_PROOF_TIMEOUT_SECONDS
        while True:
            context.guard()
            probe = self._probe()
            if paired_probe_state(probe) == "STOPPED":
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("DeepSeek Vision stopped proof unavailable")
            time.sleep(self.STOPPED_PROOF_POLL_SECONDS)
        canonical = json.dumps(probe, sort_keys=True, separators=(",", ":")).encode()
        return {
            "model_id": self.model_id,
            "stopped": True,
            "lease_generation": context.lease_generation,
            "sha256": hashlib.sha256(canonical).hexdigest(),
        }

    def start(self, context: OperationContext, profile: str | None = None) -> None:
        # Missing historical profile is the original 32K rollback baseline.
        profile = profile or "32K"
        if profile not in self.context_profiles:
            raise ValueError("invalid Vision context profile")
        controller = self._controller(context)
        controller.start(context.operation_id, _under_active=True,
                         max_model_len=self.context_profiles[profile])
        controller.production_activate_gateway(context.operation_id, _under_active=True)
        if self.observe().profile != profile:
            raise RuntimeError("Vision runtime context does not match requested profile")

    def health(self, context: OperationContext) -> dict:
        context.guard()
        if self.observe().state != "HEALTHY":
            return {"healthy": False, "model_id": self.model_id}
        proof = self._controller(context).acceptance_health(
            context.operation_id, _under_active=True
        )
        required = {
            "healthy": True,
            "model_id": self.model_id,
            "native_vision": True,
            "canary_passed": True,
            "head_rank": True,
            "worker_rank": True,
        }
        if not isinstance(proof, dict) or any(proof.get(k) != v for k, v in required.items()):
            return {"healthy": False, "model_id": self.model_id}
        return dict(required)
