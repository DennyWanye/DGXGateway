from __future__ import annotations

import json
import re
import subprocess
import threading
from pathlib import Path


class LeaseError(RuntimeError):
    pass


class GatewayLease:
    OWNER = "dgx-cluster-gateway"

    def __init__(
        self,
        *,
        lock_cli: Path,
        lock_root: Path,
        token_dir: Path,
        operation_id: str,
        purpose: str,
        lease_seconds: int = 1800,
    ):
        if not re.fullmatch(r"[A-Za-z0-9._-]{3,128}", operation_id):
            raise ValueError("invalid operation id")
        if not 60 <= lease_seconds <= 86400:
            raise ValueError("lease_seconds must be between 60 and 86400")
        self.lock_cli = Path(lock_cli)
        self.lock_root = Path(lock_root)
        self.token_dir = Path(token_dir)
        self.operation_id = operation_id
        self.purpose = purpose
        self.lease_seconds = lease_seconds
        self.token_path = self.token_dir / f"{operation_id}.token"
        self.lease_generation: str | None = None
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_error: LeaseError | None = None

    def _run(self, *args: str) -> dict:
        command = [str(self.lock_cli), "--root", str(self.lock_root), *args]
        result = subprocess.run(command, text=True, capture_output=True, timeout=30)
        if result.returncode != 0:
            message = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "lock command failed"
            raise LeaseError(message)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise LeaseError("lock command returned invalid JSON") from exc

    def acquire(self) -> dict:
        self.token_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        result = self._run(
            "acquire",
            "--owner", self.OWNER,
            "--task-id", self.operation_id,
            "--purpose", self.purpose,
            "--lease-seconds", str(self.lease_seconds),
            "--token-file", str(self.token_path),
        )
        self.lease_generation = result["lease_generation"]
        return result

    def guard(self) -> dict:
        result = self._run(
            "guard", "--owner", self.OWNER, "--token-file", str(self.token_path)
        )
        if self.lease_generation and result.get("lease_generation") != self.lease_generation:
            raise LeaseError("lease generation changed")
        self.lease_generation = result["lease_generation"]
        return result

    def heartbeat(self) -> dict:
        result = self._run(
            "heartbeat", "--owner", self.OWNER, "--token-file", str(self.token_path)
        )
        if self.lease_generation and result.get("lease_generation") != self.lease_generation:
            raise LeaseError("lease generation changed")
        self.lease_generation = result["lease_generation"]
        return result

    def release(self) -> dict:
        result = self._run(
            "release",
            "--owner", self.OWNER,
            "--token-file", str(self.token_path),
            "--operation-id", self.operation_id,
        )
        self.stop_heartbeat()
        return result

    def start_heartbeat(self, interval_seconds: float | None = None) -> None:
        if self._heartbeat_thread:
            raise LeaseError("heartbeat already started")
        interval = interval_seconds or max(10.0, self.lease_seconds / 3)
        self._heartbeat_stop.clear()

        def run() -> None:
            while not self._heartbeat_stop.wait(interval):
                try:
                    self.heartbeat()
                except LeaseError as exc:
                    self._heartbeat_error = exc
                    self._heartbeat_stop.set()

        self._heartbeat_thread = threading.Thread(
            target=run, name=f"gateway-lease-{self.operation_id}", daemon=True
        )
        self._heartbeat_thread.start()

    def check_supervision(self) -> None:
        if self._heartbeat_error:
            raise self._heartbeat_error
        self.guard()

    def stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()
        if self._heartbeat_thread and self._heartbeat_thread is not threading.current_thread():
            self._heartbeat_thread.join(timeout=5)
        self._heartbeat_thread = None


from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ParentLeaseDelegation:
    """Internal immutable action-bound capability; deliberately has no release()."""
    operation_id: str
    parent_job_id: str
    owner_id: str
    lease_epoch: int
    lease_generation: str
    token_path: Path
    guard: Callable[[], None]
    validate_binding: Callable[[], None]
    before_destructive: Callable[[str], dict]
    store: Any

    def check_supervision(self):
        self.guard()
        self.validate_binding()

    def borrow(self):
        self.check_supervision()
        return {'lease_generation': self.lease_generation}
