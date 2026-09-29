from __future__ import annotations

import shlex
import subprocess
import threading
from dataclasses import dataclass

from .config import NodeConfig


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


# Bound simultaneous handshakes across adapters and snapshots; OpenSSH
# MaxStartups can otherwise drop lifecycle probes while the dashboard polls.
_SSH_SLOTS = threading.BoundedSemaphore(4)


class FixedSSH:
    def run(self, node: NodeConfig, remote_argv: list[str], *, timeout: int = 10) -> CommandResult:
        if not remote_argv or any(not isinstance(value, str) or "\x00" in value for value in remote_argv):
            raise ValueError("invalid fixed SSH argv")
        command = [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "StrictHostKeyChecking=yes", "-o", "ClearAllForwardings=yes",
            f"{node.user}@{node.address}", shlex.join(remote_argv),
        ]
        with _SSH_SLOTS:
            result = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
        return CommandResult(result.returncode, result.stdout, result.stderr)

    def _copy(self, source: str, destination: str, *, timeout: int) -> CommandResult:
        if any(not isinstance(value, str) or not value or "\x00" in value for value in (source, destination)):
            raise ValueError("invalid scp path")
        command = [
            "scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "StrictHostKeyChecking=yes", "-o", "ClearAllForwardings=yes",
            source, destination,
        ]
        with _SSH_SLOTS:
            result = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
        return CommandResult(result.returncode, result.stdout, result.stderr)

    def put(self, node: NodeConfig, local_path: str, remote_path: str, *, timeout: int = 60) -> CommandResult:
        return self._copy(local_path, f"{node.user}@{node.address}:{remote_path}", timeout=timeout)

    def get(self, node: NodeConfig, remote_path: str, local_path: str, *, timeout: int = 60) -> CommandResult:
        return self._copy(f"{node.user}@{node.address}:{remote_path}", local_path, timeout=timeout)

    def verify_role(self, node: NodeConfig) -> None:
        result = self.run(node, ["sh", "-c", "hostname; whoami"], timeout=15)
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if result.returncode or lines != [node.hostname, node.user]:
            raise RuntimeError(f"node role verification failed: {node.role}")
