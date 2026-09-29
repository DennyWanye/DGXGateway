#!/usr/bin/env python3
"""Loopback-only Mac authority for explicit MiniMax H3 profile switching."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import tempfile
import threading
import time
from typing import Any


PUBLIC_PROFILES = {
    "heretic-ref2va-v1",
    "heretic-ref2va-hmnsfw-v1",
    "10eros-fl2va-v1",
}
OWNER = "minimax-h3-multi-profile"
HEAD = "operator@192.0.2.41"
WORKER = "operator@192.0.2.31"
REMOTE_ATTESTATION = "/run/minimax-h3-two-node/mac-lock-attestation.json"
PROFILE_OPERATION_LEASE_SECONDS = 1800
LIFECYCLE_SSH_TIMEOUT_SECONDS = 3000


def private_token(path: Path) -> bytes:
    if path.is_symlink():
        raise RuntimeError("control token is unsafe")
    metadata = path.stat()
    raw = path.read_bytes().rstrip(b"\r\n")
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600 or len(raw) < 16:
        raise RuntimeError("control token is unsafe")
    return raw


class Authority:
    def __init__(
        self,
        repo_root: Path,
        lock_root: Path,
        queue_url: str = "http://127.0.0.1:8188/queue",
        owner: str = OWNER,
    ):
        self.repo_root = repo_root
        self.lock_root = lock_root
        self.queue_url = queue_url
        self.owner = owner
        self.lock_cli = repo_root / "scripts/dgx-spark-deployment-lock.py"
        self.operation_lock = threading.Lock()

    @staticmethod
    def run(argv: list[str], *, input_bytes: bytes | None = None, timeout: int = 30) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            argv,
            input=input_bytes,
            # subprocess.run creates its own PIPE when input is supplied;
            # passing both input= and stdin=PIPE is rejected by Python.
            stdin=None if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=timeout,
        )

    def ssh(self, target: str, command: str, *, input_bytes: bytes | None = None, timeout: int = 30):
        return self.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", target, command],
            input_bytes=input_bytes,
            timeout=timeout,
        )

    def status(self) -> dict[str, Any]:
        result = self.ssh(
            HEAD,
            "hostname; whoami; sudo -n /usr/local/sbin/minimax-h3-control status",
            timeout=30,
        ).stdout.decode("utf-8", "replace").splitlines()
        if result[:2] != ["dgx-head", "operator"] or len(result) < 3:
            raise RuntimeError("head role verification failed")
        document = json.loads(result[-1])
        return {
            "state": document.get("state"),
            "profile": document.get("profile"),
            "healthy": document.get("healthy", False),
            "profile_identity": document.get("profile_identity"),
        }

    def queue_idle(self) -> bool:
        try:
            output = self.run(["curl", "-fsS", self.queue_url], timeout=5).stdout
            value = json.loads(output)
            # This endpoint is called by the currently executing ComfyUI
            # control node, so one queue_running item is expected. ComfyUI's
            # executor is serial: pending work is the unsafe condition.
            return isinstance(value, dict) and not value.get("queue_pending")
        except Exception:
            return False

    @staticmethod
    def lifecycle_command(action: str, profile: str, state: str) -> str:
        if action == "switch" and state != "READY":
            return (
                "sudo -n /usr/local/sbin/minimax-h3-control start "
                f"--profile {profile} --llm-already-quiesced"
            )
        command = f"sudo -n /usr/local/sbin/minimax-h3-control {action}"
        if action == "switch":
            command += f" --profile {profile}"
        return command

    @staticmethod
    def borrowed_lifecycle_command(action: str, profile: str, state: str) -> str:
        if action == "stop":
            return (
                "sudo -n /usr/local/sbin/minimax-h3-control stop "
                "--preserve-llm-quiesce"
            )
        if action == "switch" and state != "READY":
            return (
                "sudo -n /usr/local/sbin/minimax-h3-control start "
                f"--profile {profile} --llm-already-quiesced "
                "--preserve-llm-quiesce-on-failure"
            )
        return (
            "sudo -n /usr/local/sbin/minimax-h3-control switch "
            f"--profile {profile}"
        )

    def _lock(self, command: str, token_path: Path, *extra: str) -> dict[str, Any]:
        output = self.run(
            [
                "python3", str(self.lock_cli), "--root", str(self.lock_root), command,
                "--owner", self.owner, "--token-file", str(token_path), *extra,
            ],
            timeout=30,
        ).stdout
        return json.loads(output)

    def _lock_status(self) -> dict[str, Any]:
        output = self.run(
            [
                "python3", str(self.lock_cli), "--root", str(self.lock_root), "status",
            ],
            timeout=30,
        ).stdout
        return json.loads(output)

    def _borrowed_lock_snapshot(self, token_path: Path) -> dict[str, Any]:
        guarded = self._lock("guard", token_path)
        status = self._lock_status()
        if (
            guarded.get("state") != "ACTIVE"
            or status.get("state") != "ACTIVE"
            or status.get("owner") != self.owner
            or guarded.get("lease_generation") != status.get("lease_generation")
        ):
            raise RuntimeError("borrowed lease identity mismatch")
        token = token_path.read_text(encoding="utf-8").strip()
        if hashlib.sha256(token.encode()).hexdigest() != status.get("token_sha256"):
            raise RuntimeError("borrowed lease token mismatch")
        return status

    def _sync_attestation(self, value: dict[str, Any]) -> None:
        selected = {key: value[key] for key in (
            "schema_version", "cluster_id", "owner", "task_id", "purpose", "token_sha256",
            "lease_generation", "expires_at"
        )}
        selected["state"] = "ACTIVE"
        payload = (json.dumps(selected, sort_keys=True, separators=(",", ":")) + "\n").encode()
        program = (
            "import os,pathlib,sys,tempfile;"
            "p=pathlib.Path(sys.argv[1]);p.parent.mkdir(parents=True,exist_ok=True);"
            "fd,t=tempfile.mkstemp(prefix=p.name+'.',dir=p.parent);"
            "os.fchmod(fd,0o600);"
            "f=os.fdopen(fd,'wb');f.write(sys.stdin.buffer.read());f.flush();os.fsync(f.fileno());f.close();"
            "os.replace(t,p);os.chown(p,0,0)"
        )
        command = f"sudo -n python3 -c {shlex.quote(program)} {shlex.quote(REMOTE_ATTESTATION)}"
        installed: list[str] = []
        try:
            for target in (HEAD, WORKER):
                self.ssh(target, command, input_bytes=payload)
                installed.append(target)
        except Exception:
            # The caller performs token-matched cleanup on both hosts.  Keep
            # this list local so a partial install can never be mistaken for
            # an all-host commit.
            raise

    def _clear_attestation(self, token_sha256: str) -> None:
        program = (
            "import json,pathlib,sys;"
            "p=pathlib.Path(sys.argv[1]);expected=sys.argv[2];"
            "exists=p.exists();"
            "(_ for _ in ()).throw(SystemExit('unsafe attestation path')) if p.is_symlink() else None;"
            "value=json.loads(p.read_text()) if exists else {};"
            "(_ for _ in ()).throw(SystemExit('attestation token mismatch')) "
            "if exists and value.get('token_sha256')!=expected else None;"
            "p.unlink() if exists else None;"
            "(_ for _ in ()).throw(SystemExit('attestation removal not verified')) if p.exists() else None"
        )
        command = (
            f"sudo -n python3 -c {shlex.quote(program)} "
            f"{shlex.quote(REMOTE_ATTESTATION)} {shlex.quote(token_sha256)}"
        )
        errors: list[Exception] = []
        for target in (HEAD, WORKER):
            try:
                self.ssh(target, command)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise RuntimeError("remote lock attestation cleanup was not verified") from errors[0]

    def _confirm_borrowed_attestation(
        self, token_path: Path, *, attempts: int = 3, delay_seconds: float = 2
    ) -> None:
        """Require a fresh remote attestation while tolerating brief SSH stalls."""
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                self._sync_attestation(self._borrowed_lock_snapshot(token_path))
                return
            except Exception as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(delay_seconds)
        raise RuntimeError("borrowed lock attestation refresh failed") from last_error

    def mutate(self, action: str, profile: str) -> dict[str, Any]:
        if action not in {"switch", "stop"} or profile not in PUBLIC_PROFILES:
            raise ValueError("unsupported operation")
        if not self.operation_lock.acquire(blocking=False):
            raise BlockingIOError("operation busy")
        try:
            if not self.queue_idle():
                raise BlockingIOError("generation busy")
            for target, hostname in ((HEAD, "dgx-head"), (WORKER, "dgx-worker")):
                observed = self.ssh(target, "hostname; whoami").stdout.decode().splitlines()
                if observed != [hostname, "operator"]:
                    raise RuntimeError("cluster role verification failed")
            with tempfile.TemporaryDirectory(prefix="dgx-h3-profile-lock-") as directory:
                token_path = Path(directory) / "token"
                acquired = self._lock(
                    "acquire", token_path,
                    "--task-id", f"comfy-profile-{int(time.time())}",
                    "--purpose", f"explicit ComfyUI H3 {action} {profile}",
                    "--lease-seconds", str(PROFILE_OPERATION_LEASE_SECONDS),
                )
                release_allowed = True
                lifecycle_started = False
                stop_heartbeat: threading.Event | None = None
                thread: threading.Thread | None = None
                try:
                    self._sync_attestation(acquired)
                    stop_heartbeat = threading.Event()
                    heartbeat_failed = threading.Event()

                    def heartbeat() -> None:
                        while not stop_heartbeat.wait(30):
                            try:
                                refreshed = self._lock("heartbeat", token_path)
                                self._sync_attestation(refreshed)
                            except Exception:
                                heartbeat_failed.set()
                                return

                    thread = threading.Thread(target=heartbeat, daemon=True)
                    thread.start()
                    try:
                        current = self.status()
                        command = self.lifecycle_command(action, profile, str(current.get("state")))
                        lifecycle_started = True
                        result = self.ssh(HEAD, command, timeout=LIFECYCLE_SSH_TIMEOUT_SECONDS)
                        terminal = result.stdout.decode("utf-8", "replace").strip().splitlines()[-1]
                        if heartbeat_failed.is_set():
                            raise RuntimeError("lock attestation heartbeat failed")
                        return {"terminal": terminal, **self.status()}
                    except Exception:
                        if lifecycle_started:
                            # A transport failure is ambiguous.  Release only
                            # when the remote state independently proves a
                            # terminal condition that is safe to leave behind.
                            try:
                                release_allowed = self.status().get("state") in {
                                    "READY", "STOPPED", "FAILED_SAFE_STOPPED"
                                }
                            except Exception:
                                release_allowed = False
                        raise
                    finally:
                        if stop_heartbeat is not None:
                            stop_heartbeat.set()
                        if thread is not None:
                            thread.join(timeout=2)
                finally:
                    if release_allowed:
                        # Never release the unique Mac lock unless the matching
                        # remote proof is gone from both nodes.  If cleanup
                        # fails, both leases simply expire fail-closed.
                        self._clear_attestation(acquired["token_sha256"])
                        self._lock("release", token_path)
        finally:
            self.operation_lock.release()

    def mutate_borrowed(
        self, action: str, profile: str, token_path: Path
    ) -> dict[str, Any]:
        if action not in {"switch", "stop"} or profile not in PUBLIC_PROFILES:
            raise ValueError("unsupported operation")
        if not self.operation_lock.acquire(blocking=False):
            raise BlockingIOError("operation busy")
        clear_allowed = False
        snapshot: dict[str, Any] | None = None
        stop_refresh = threading.Event()
        thread: threading.Thread | None = None
        try:
            if not self.queue_idle():
                raise BlockingIOError("generation busy")
            for target, hostname in ((HEAD, "dgx-head"), (WORKER, "dgx-worker")):
                observed = self.ssh(target, "hostname; whoami").stdout.decode().splitlines()
                if observed != [hostname, "operator"]:
                    raise RuntimeError("cluster role verification failed")
            snapshot = self._borrowed_lock_snapshot(token_path)
            self._sync_attestation(snapshot)

            def refresh() -> None:
                while not stop_refresh.wait(30):
                    try:
                        self._sync_attestation(self._borrowed_lock_snapshot(token_path))
                    except Exception:
                        # A busy model loader can briefly delay management SSH.
                        # Keep retrying; the final confirmation below remains
                        # fail-closed before the lifecycle result is accepted.
                        continue

            thread = threading.Thread(target=refresh, daemon=True)
            thread.start()
            current = self.status()
            command = self.borrowed_lifecycle_command(
                action, profile, str(current.get("state"))
            )
            result = self.ssh(HEAD, command, timeout=LIFECYCLE_SSH_TIMEOUT_SECONDS)
            self._confirm_borrowed_attestation(token_path)
            terminal = result.stdout.decode("utf-8", "replace").strip().splitlines()[-1]
            observed = self.status()
            clear_allowed = observed.get("state") in {
                "READY", "STOPPED", "FAILED_SAFE_STOPPED"
            }
            if not clear_allowed:
                raise RuntimeError("borrowed lifecycle did not reach a safe terminal state")
            return {"terminal": terminal, **observed}
        finally:
            stop_refresh.set()
            if thread is not None:
                thread.join(timeout=2)
            if clear_allowed and snapshot is not None:
                self._clear_attestation(snapshot["token_sha256"])
            self.operation_lock.release()


class Server(ThreadingHTTPServer):
    def __init__(self, token: bytes, authority: Authority):
        self.token = token
        self.authority = authority
        super().__init__(("127.0.0.1", 19082), Handler)


class Handler(BaseHTTPRequestHandler):
    server: Server
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        return

    def authorized(self) -> bool:
        value = self.headers.get("Authorization", "")
        return value.startswith("Bearer ") and hmac.compare_digest(value[7:].encode(), self.server.token)

    def reply(self, code: int, value: dict[str, Any]) -> None:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if not self.authorized():
            self.reply(401, {"error": "unauthorized"})
        elif self.path != "/status":
            self.reply(404, {"error": "not_found"})
        else:
            try:
                self.reply(200, self.server.authority.status())
            except Exception:
                self.reply(503, {"error": "controller_unavailable"})

    def do_POST(self):
        if not self.authorized():
            self.reply(401, {"error": "unauthorized"})
            return
        action = self.path.removeprefix("/")
        if action not in {"switch", "stop"}:
            self.reply(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if not 0 < length <= 1024 or self.headers.get_content_type() != "application/json":
                raise ValueError
            value = json.loads(self.rfile.read(length))
            if set(value) != {"profile"} or value["profile"] not in PUBLIC_PROFILES:
                raise ValueError
            self.reply(200, self.server.authority.mutate(action, value["profile"]))
        except BlockingIOError:
            self.reply(409, {"error": "busy"})
        except ValueError:
            self.reply(422, {"error": "invalid_request"})
        except Exception:
            self.reply(500, {"error": "operation_failed"})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--lock-root", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--queue-url", default="http://127.0.0.1:8188/queue")
    args = parser.parse_args()
    token = private_token(args.token_file)
    Server(token, Authority(args.repo_root, args.lock_root, args.queue_url)).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
