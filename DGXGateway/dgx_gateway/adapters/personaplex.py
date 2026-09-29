from __future__ import annotations

import hashlib
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from ..config import GatewayConfig
from ..domain import ObservedModel
from ..operations import OperationContext
from ..ssh import FixedSSH


class PersonaPlexAdapter:
    model_id = "personaplex"
    unit = "dgx-gateway-personaplex.service"
    port = 8998

    def __init__(self, config: GatewayConfig, ssh: FixedSSH | None = None):
        self.config = config
        self.ssh = ssh or FixedSSH()
        self.head = next(node for node in config.nodes if node.role == "head")
        self.root = config.personaplex_root
        self.assets = self.root / "models/personaplex-7b-v1"
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _run(self, argv, timeout=15):
        return self.ssh.run(self.head, argv, timeout=timeout)

    def _request(self, path, method="GET"):
        req = urllib.request.Request(f"http://{self.head.address}:{self.port}{path}", method=method)
        with self.http.open(req, timeout=5) as response:
            return json.load(response)

    def _runtime_probe(self):
        commands = [
            ["systemctl", "--user", "show", self.unit, "-p", "ActiveState", "-p", "MainPID", "-p", "ControlGroup"],
            ["ss", "-H", "-ltn", "sport = :8998"],
            ["pgrep", "-f", "[m]oshi.server"],
        ]
        with ThreadPoolExecutor(max_workers=3) as pool:
            unit, listener, process = list(pool.map(self._run, commands))
        if unit.returncode not in (0, 1, 4) or listener.returncode or process.returncode not in (0, 1):
            raise RuntimeError("PersonaPlex runtime probe failed")
        fields = dict(line.split("=", 1) for line in unit.stdout.splitlines() if "=" in line)
        state = fields.get("ActiveState", "unknown")
        if state == "unknown":
            raise RuntimeError("PersonaPlex unit state is unknown")
        return {"unit_state": state, "main_pid": fields.get("MainPID", "0"),
                "cgroup": fields.get("ControlGroup", ""), "listener": bool(listener.stdout.strip()),
                "process": process.returncode == 0}

    def _stopped(self, probe):
        return (probe["unit_state"] in ("inactive", "failed") and probe["main_pid"] == "0"
                and not probe["cgroup"] and not probe["listener"] and not probe["process"])

    def observe(self):
        try:
            probe = self._runtime_probe()
            if self._stopped(probe):
                return ObservedModel(self.model_id, "STOPPED")
            if probe["unit_state"] == "active" and probe["main_pid"] != "0" and probe["listener"]:
                health = self._request("/healthz")
                if health.get("model") == self.model_id and health.get("ready"):
                    return ObservedModel(self.model_id, "HEALTHY")
            return ObservedModel(self.model_id, "PARTIAL")
        except Exception:
            return ObservedModel(self.model_id, "UNKNOWN")

    def drain(self, context: OperationContext):
        context.guard()
        probe = self._runtime_probe()
        if self._stopped(probe):
            return
        if probe["unit_state"] != "active" or probe["main_pid"] == "0":
            raise RuntimeError("PersonaPlex drain ownership is ambiguous")
        self._request("/drain", "POST")
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            context.guard()
            state = self._request("/healthz")
            if state.get("draining") and state.get("active_sessions") == 0:
                return
            time.sleep(0.5)
        raise RuntimeError("PersonaPlex active session did not finish within drain timeout")

    def stop(self, context: OperationContext):
        context.guard()
        result = self._run(["systemctl", "--user", "stop", self.unit], timeout=60)
        if result.returncode not in (0, 5):
            raise RuntimeError("PersonaPlex owned unit failed to stop")

    def retire(self, context: OperationContext):
        self.stop(context)

    def stopped_proof(self, context: OperationContext):
        context.guard()
        probe = self._runtime_probe()
        if not self._stopped(probe):
            raise RuntimeError("PersonaPlex process/listener/cgroup absence is unproven")
        proof = {"model_id": self.model_id, "stopped": True, "unit": self.unit,
                 "operation_id": context.operation_id, "lease_generation": context.lease_generation,
                 "host": self.head.hostname, "observed_at": datetime.now(timezone.utc).isoformat(), "checks": probe}
        proof["sha256"] = hashlib.sha256(json.dumps(proof, sort_keys=True).encode()).hexdigest()
        return proof

    def start(self, context: OperationContext, profile: str | None = None):
        context.guard()
        if profile is not None:
            raise ValueError("PersonaPlex does not define alternate profiles")
        self.stopped_proof(context)
        files = [self.root / ".venv/bin/python", self.assets / "model.safetensors",
                 self.assets / "tokenizer-e351c8d8-checkpoint125.safetensors",
                 self.assets / "tokenizer_spm_32k_3.model", self.assets / "voices/NATF0.pt",
                 self.assets / "dist/index.html"]
        for path in files:
            if self._run(["test", "-s", str(path)]).returncode:
                raise RuntimeError(f"PersonaPlex required local asset missing: {path.name}")
        if self._run(["grep", "-q", "DGX_GATEWAY_PERSONAPLEX_V1", str(self.root / "moshi/moshi/server.py")]).returncode:
            raise RuntimeError("PersonaPlex lifecycle runtime patch is missing")
        result = self._run([
            "systemd-run", "--user", "--collect", "--unit=dgx-gateway-personaplex",
            "--property=Restart=no", "--property=KillMode=control-group", "--property=TimeoutStopSec=30",
            f"--property=WorkingDirectory={self.root}", "--setenv=PYTHONUNBUFFERED=1",
            "--setenv=HF_HUB_OFFLINE=1", "--setenv=TRANSFORMERS_OFFLINE=1",
            f"--setenv=PYTHONPATH={self.root / 'moshi'}",
            str(files[0]), "-m", "moshi.server", "--host", "0.0.0.0", "--port", str(self.port),
            "--static", str(self.assets / "dist"), "--voice-prompt-dir", str(self.assets / "voices"),
            "--tokenizer", str(files[3]), "--mimi-weight", str(files[2]), "--moshi-weight", str(files[1]),
        ], timeout=30)
        if result.returncode:
            raise RuntimeError("PersonaPlex owned unit failed to start")

    def health(self, context: OperationContext):
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            context.guard()
            if self.observe().state == "HEALTHY":
                return {"healthy": True, "model_id": self.model_id, "endpoint": f"http://{self.head.address}:{self.port}", "runtime": self._request("/healthz")}
            time.sleep(2)
        return {"healthy": False, "reason": "PersonaPlex warmup timeout"}
