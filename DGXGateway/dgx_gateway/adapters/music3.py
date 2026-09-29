from __future__ import annotations

import hashlib
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..comfy_identity import COMFY_SERVICE, COMFY_UNIT, LEGACY_COMFY_UNIT
from ..config import GatewayConfig
from ..domain import ObservedModel
from ..operations import OperationContext
from ..ssh import FixedSSH


class ComfyAdapter:
    model_id = COMFY_SERVICE
    unit = f"{COMFY_UNIT}.service"
    legacy_unit = f"{LEGACY_COMFY_UNIT}.service"

    def __init__(self, config: GatewayConfig, ssh: FixedSSH | None = None):
        self.config = config
        self.ssh = ssh or FixedSSH()
        self.head = next(node for node in config.nodes if node.role == "head")

    def _run(self, argv: list[str], timeout: int = 30):
        return self.ssh.run(self.head, argv, timeout=timeout)

    def _http_ok(self, path: str, timeout: float = 5) -> bool:
        try:
            with urllib.request.urlopen(
                f"http://{self.head.address}:{self.config.music3_port}{path}", timeout=timeout
            ) as response:
                return response.status == 200
        except Exception:
            return False

    def _runtime_probe(self) -> dict:
        units = (self.unit, self.legacy_unit)
        with ThreadPoolExecutor(max_workers=4) as executor:
            unit_futures = [
                executor.submit(
                    lambda unit=unit: self._run(
                        ["systemctl", "--user", "show", unit, "-p", "LoadState", "-p", "ActiveState", "--value"],
                        timeout=7,
                    )
                )
                for unit in units
            ]
            listener_future = executor.submit(
                lambda: self._run(["ss", "-H", "-ltn", "sport = :8188"], timeout=7)
            )
            process_future = executor.submit(
                lambda: self._run(
                    ["pgrep", "-f", "[m]ain.py.*--port 8188"], timeout=7
                )
            )
            unit_results = [future.result() for future in unit_futures]
            listener = listener_future.result()
            process = process_future.result()
        if any(result.returncode not in {0, 1, 4} for result in unit_results) or listener.returncode != 0 or process.returncode not in {0, 1}:
            raise RuntimeError("ComfyUI runtime probe failed")
        unit_active = False
        unit_load = "not-found"
        for result in unit_results:
            unit_lines = {line.strip() for line in result.stdout.splitlines() if line.strip()}
            if "active" in unit_lines:
                unit_active = True
                unit_load = "loaded"
            elif unit_load == "not-found":
                unit_load = next((line for line in unit_lines if line in {"loaded", "not-found", "masked"}), "unknown")
        return {
            "unit_active": unit_active,
            "unit_load": unit_load,
            "listener": bool(listener.stdout.strip()),
            "process": process.returncode == 0,
        }

    def observe(self) -> ObservedModel:
        try:
            probe = self._runtime_probe()
        except Exception:
            return ObservedModel(self.model_id, "UNKNOWN")
        active = probe["unit_active"]
        healthy = active and probe["listener"] and probe["process"] and self._http_ok("/queue") and self._http_ok("/object_info")
        if healthy:
            return ObservedModel(self.model_id, "HEALTHY")
        if not active and not probe["listener"] and not probe["process"]:
            return ObservedModel(self.model_id, "STOPPED")
        return ObservedModel(self.model_id, "PARTIAL")

    def drain(self, context: OperationContext) -> None:
        context.guard()
        if self.observe().state not in {"HEALTHY", "STOPPED"}:
            raise RuntimeError("ComfyUI drain precondition is ambiguous")
        if self.observe().state == "HEALTHY":
            request = urllib.request.Request(
                f"http://{self.head.address}:{self.config.music3_port}/dgx/drain",
                data=b"{}", headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                state = json.load(response)
            if state.get("drained") is not True:
                raise RuntimeError("ComfyUI admission did not drain")

    def stop(self, context: OperationContext) -> None:
        context.guard()
        for unit in (self.unit, self.legacy_unit):
            result = self._run(["systemctl", "--user", "stop", unit], timeout=60)
            if result.returncode not in {0, 5}:
                raise RuntimeError("ComfyUI transient unit failed to stop")

    def retire(self, context: OperationContext) -> None:
        self.stop(context)

    def stopped_proof(self, context: OperationContext) -> dict:
        context.guard()
        probe = self._runtime_probe()
        if probe["unit_active"] or probe["listener"] or probe["process"]:
            raise RuntimeError("ComfyUI stopped proof unavailable")
        payload = json.dumps(
            {"model_id": self.model_id, "unit": self.unit, "probe": probe},
            sort_keys=True,
        ).encode()
        return {
            "model_id": self.model_id,
            "stopped": True,
            "lease_generation": context.lease_generation,
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    def start(self, context: OperationContext, profile: str | None = None) -> None:
        del profile
        context.guard()
        root = Path(self.config.music3_root)
        result = self._run(
            [
                "systemd-run", "--user", "--collect", f"--unit={COMFY_UNIT}",
                "--property=Restart=no", f"--property=WorkingDirectory={root}",
                "--setenv=PYTHONUNBUFFERED=1", "--setenv=HF_HUB_OFFLINE=1",
                "--setenv=TRANSFORMERS_OFFLINE=1", str(self.config.music3_python),
                str(root / "main.py"), "--listen", "0.0.0.0", "--port",
                str(self.config.music3_port), "--disable-auto-launch",
            ],
            timeout=30,
        )
        if result.returncode:
            raise RuntimeError("ComfyUI transient unit failed to start")

    def health(self, context: OperationContext) -> dict:
        import time

        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            context.guard()
            if self.observe().state == "HEALTHY":
                return {"healthy": True}
            time.sleep(2)
        return {"healthy": False}


Music3Adapter = ComfyAdapter
