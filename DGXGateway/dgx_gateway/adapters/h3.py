from __future__ import annotations

import hashlib
import json

from ..config import GatewayConfig, paired_nodes
from ..domain import ObservedModel
from ..operations import OperationContext
from ..ssh import FixedSSH
from .loader import load_module


class H3Adapter:
    model_id = "minimax-h3"
    profiles = ("heretic-ref2va-v1", "heretic-ref2va-hmnsfw-v1", "10eros-fl2va-v1")

    def __init__(self, config: GatewayConfig, ssh: FixedSSH | None = None):
        self.config = config
        self.ssh = ssh or FixedSSH()
        self._module = None
        self.last_profile = "10eros-fl2va-v1"

    def _authority(self):
        if self._module is None:
            self._module = load_module(
                "dgx_gateway_h3_profile_controller",
                self.config.repo_root / "ComfyUI/scripts/h3-profile-controller.py",
            )
        return self._module.Authority(
            self.config.repo_root,
            self.config.lock_root,
            queue_url=self.config.h3_queue_url,
            owner="dgx-cluster-gateway",
        )

    def _control_binary_missing(self) -> bool:
        try:
            head = next(node for node in self.config.nodes if node.role == "head")
            result = self.ssh.run(
                head, ["test", "-x", "/usr/local/sbin/minimax-h3-control"], timeout=10
            )
        except Exception:
            return False
        return result.returncode == 1

    def observe(self) -> ObservedModel:
        if self._control_binary_missing():
            return ObservedModel(self.model_id, "STOPPED")
        try:
            status = self._authority().status()
        except Exception:
            return ObservedModel(self.model_id, "UNKNOWN", self.last_profile)
        profile = status.get("profile") or self.last_profile
        self.last_profile = profile
        state = status.get("state")
        if state == "READY" and status.get("healthy") is True:
            return ObservedModel(self.model_id, "HEALTHY", profile)
        if state == "STOPPED":
            return ObservedModel(self.model_id, "STOPPED", profile)
        return ObservedModel(self.model_id, "PARTIAL", profile)

    def drain(self, context: OperationContext) -> None:
        context.guard()
        observed = self.observe()
        if observed.state == "PARTIAL":
            # Recovery is still serialized by the Gateway lease and remote
            # generation lock. Require a dead API and an empty client queue.
            local = self.ssh.run(next(node for node in self.config.nodes if node.role == "head"), ["sudo", "-n", "/usr/local/libexec/minimax-h3-action", "status-local"], timeout=10)
            if local.returncode == 0 and json.loads(local.stdout).get("api") == "STOPPED":
                queue = self._authority().run(["curl", "-fsS", self.config.h3_queue_url], timeout=5)
                state = json.loads(queue.stdout)
                if queue.returncode == 0 and isinstance(state, dict) and state.get("queue_running") == [] and state.get("queue_pending") == []:
                    return
        if observed.state not in {"HEALTHY", "STOPPED"}:
            raise RuntimeError("H3 drain precondition is ambiguous")

    def stop(self, context: OperationContext) -> None:
        self._authority().mutate_borrowed("stop", self.last_profile, context.token_path)

    def retire(self, context: OperationContext) -> None:
        self.stop(context)

    def _probe_stopped(self) -> dict:
        if self._control_binary_missing():
            return {
                "head": {"returncode": 0, "stdout": "retired-dual-node-control"},
                "worker": {"returncode": 0, "stdout": "retired-dual-node-control"},
            }
        evidence = {}
        commands = {
            "head": ["sudo", "-n", "/usr/local/libexec/minimax-h3-action", "assert-h3-stopped"],
            "worker": ["sudo", "-n", "/usr/local/libexec/minimax-h3-worker-action", "assert-stopped"],
        }
        for node in paired_nodes(self.config.nodes):
            result = self.ssh.run(node, commands[node.role], timeout=30)
            evidence[node.role] = {"returncode": result.returncode, "stdout": result.stdout}
            if result.returncode:
                raise RuntimeError(f"H3 {node.role} stopped proof failed")
        return evidence

    def stopped_proof(self, context: OperationContext) -> dict:
        context.guard()
        evidence = self._probe_stopped()
        canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
        return {
            "model_id": self.model_id,
            "stopped": True,
            "lease_generation": context.lease_generation,
            "sha256": hashlib.sha256(canonical).hexdigest(),
        }

    def start(self, context: OperationContext, profile: str | None = None) -> None:
        selected = profile or self.last_profile
        if selected not in self.profiles:
            raise ValueError("unknown H3 profile")
        self._authority().mutate_borrowed("switch", selected, context.token_path)
        self.last_profile = selected

    def health(self, context: OperationContext) -> dict:
        context.guard()
        observed = self.observe()
        return {
            "healthy": observed.state == "HEALTHY",
            "profile": observed.profile,
        }
