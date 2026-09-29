#!/usr/bin/env python3
"""Receipt-gated two-node controller for DeepSeek V4 Flash Vision Exp."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable


PROJECT = "deepseek-v4-flash-vision-exp"
MODEL_ID = PROJECT
OWNER = "deepseek-v4-flash-vision-exp-two-node"
REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK_ROOT = Path("/Users/operator/.codex/locks/dgx-spark-two-node")
LOCK_HISTORY = Path("/Users/operator/.codex/locks/dgx-spark-two-node-history")
LOCK_SCRIPT = REPO_ROOT / "scripts/dgx-spark-deployment-lock.py"
DEFAULT_TOKEN_FILE = LOCK_ROOT.parent / f"{LOCK_ROOT.name}.{OWNER}.token"
DEFAULT_JOURNAL = Path("/Users/operator/.codex/state/deepseek-v4-flash-vision-exp/journal.jsonl")
DEFAULT_GATEWAY_SECRET = Path("/Users/operator/.config/deepseek-v4-flash-vision-exp/gateway.token")
REMOTE_HELPER = "/usr/local/libexec/run-deepseek-v4-flash-vision-exp.sh"
HOSTS = {
    "head": ("dgx-head", "192.0.2.30"),
    "worker": ("dgx-worker", "192.0.2.31"),
}
CANARY_PNG = "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAIUlEQVR4nGP8z0AaYCJRPcOoBmIAE1GqkMCoBmIAyaEEAEAuAR9UPEsJAAAAAElFTkSuQmCC"


class ControllerError(RuntimeError):
    pass


class Completed:
    def __init__(self, returncode: int, stdout: str, stderr: str):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class SubprocessRunner:
    def run(self, argv: Iterable[str], *, input_text=None, timeout=30) -> Completed:
        try:
            result = subprocess.run(list(argv), input=input_text, text=True, capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ControllerError(f"bounded command failed: {exc}") from exc
        return Completed(result.returncode, result.stdout, result.stderr)

    def run_supervised(self, argv: Iterable[str], *, input_text: str, timeout: float,
                       lease_check: Callable[[], object]) -> Completed:
        with tempfile.TemporaryFile(mode="w+") as stdout, tempfile.TemporaryFile(mode="w+") as stderr:
            process = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                       text=True, start_new_session=True)
            assert process.stdin is not None
            process.stdin.write(input_text)
            process.stdin.close()
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        raise ControllerError("supervised remote action timed out")
                    lease_check()
                    time.sleep(2)
                stdout.seek(0); stderr.seek(0)
                return Completed(process.returncode, stdout.read(), stderr.read())
            except BaseException:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=5)
                raise


def _private_external(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    try:
        resolved.relative_to(REPO_ROOT.resolve())
    except ValueError:
        pass
    else:
        raise ControllerError(f"{label} must be outside the repository")
    return resolved


class Journal:
    def __init__(self, path: Path):
        self.path = _private_external(path, "journal")

    def append(self, event: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            row = {"recorded_at": datetime.now(timezone.utc).isoformat(), **event}
            os.write(descriptor, (json.dumps(row, sort_keys=True) + "\n").encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _json_result(result: Completed, context: str) -> dict:
    if result.returncode:
        raise ControllerError(f"{context} failed: {result.stderr.strip() or result.stdout.strip()}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ControllerError(f"{context} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ControllerError(f"{context} returned a non-object")
    return value


class LockAuthority:
    def __init__(self, *, runner, token_file=DEFAULT_TOKEN_FILE, owner=OWNER,
                 lock_root=LOCK_ROOT, lock_script=LOCK_SCRIPT, lock_history=LOCK_HISTORY):
        self.runner = runner
        self.token_file = _private_external(Path(token_file), "token")
        self.owner, self.lock_root = owner, Path(lock_root)
        self.lock_script, self.lock_history = Path(lock_script), Path(lock_history)
        self.task_id: str | None = None

    def _invoke(self, verb: str, *extra: str) -> dict:
        argv = [sys.executable, str(self.lock_script)]
        if self.lock_root != LOCK_ROOT:
            argv += ["--root", str(self.lock_root)]
        argv += [verb, *extra]
        return _json_result(self.runner.run(argv, timeout=30), f"lock {verb}")

    def guard(self) -> dict:
        value = self._invoke("guard", "--owner", self.owner, "--token-file", str(self.token_file))
        if value.get("state") != "ACTIVE" or value.get("owner") != self.owner:
            raise ControllerError("active lease guard failed")
        return value

    def acquire(self, task_id: str, purpose: str) -> dict:
        self.token_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        value = self._invoke("acquire", "--owner", self.owner, "--task-id", task_id,
                             "--purpose", purpose, "--lease-seconds", "1800",
                             "--token-file", str(self.token_file))
        if value.get("state") != "ACTIVE":
            raise ControllerError("lease acquisition failed")
        self.task_id = task_id
        return value

    def heartbeat(self) -> dict:
        return self._invoke("heartbeat", "--owner", self.owner, "--token-file", str(self.token_file))

    def release(self) -> dict:
        if self.task_id is None:
            raise ControllerError("cannot release a lease without its task binding")
        return self._invoke(
            "release", "--owner", self.owner, "--token-file", str(self.token_file),
            "--operation-id", self.task_id,
        )


class _Lease:
    def __init__(self, authority: LockAuthority, task_id: str, purpose: str):
        self.authority, self.task_id, self.purpose = authority, task_id, purpose
        self.stop = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._beat, daemon=True)

    def _beat(self):
        while not self.stop.wait(30):
            try: self.authority.heartbeat()
            except BaseException as exc: self.error = exc; self.stop.set()

    def __enter__(self):
        self.authority.acquire(self.task_id, self.purpose); self.thread.start(); return self

    def check(self):
        if self.error: raise ControllerError(f"lease heartbeat failed: {self.error}")
        return self.authority.guard()

    def __exit__(self, exc_type, _exc, _traceback):
        self.stop.set(); self.thread.join(timeout=5); self.check()
        if exc_type is None:
            self.authority.release()


class ClusterController:
    def __init__(self, *, authority: LockAuthority, runner, journal: Journal, task_id: str):
        self.authority, self.runner, self.journal, self.task_id = authority, runner, journal, task_id
        self._active_lease = None

    def _guard(self):
        return self._active_lease.check() if self._active_lease else self.authority.guard()

    @contextlib.contextmanager
    def _lease(self, purpose: str, under_active: bool):
        if under_active:
            self._guard(); yield; return
        lease = _Lease(self.authority, self.task_id, purpose)
        with lease:
            self._active_lease = lease
            try: yield
            finally: self._active_lease = None

    @staticmethod
    def _ssh(role: str, remote: list[str]) -> list[str]:
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                "-o", "StrictHostKeyChecking=yes", f"operator@{HOSTS[role][1]}",
                shlex.join(remote)]

    def _verify_target(self, role: str) -> None:
        result = self.runner.run(self._ssh(role, ["sh", "-c", "hostname; whoami"]), timeout=15)
        if result.returncode or result.stdout.split() != [HOSTS[role][0], "operator"]:
            raise ControllerError(f"{role} target identity mismatch")

    def _mutate(self, role: str, verb: str, transaction: str, *, gateway_token: str | None = None,
                request_body_sha256: str | None = None,
                control_body_sha256: str | None = None,
                max_model_len: int | None = None) -> dict:
        guard = self._guard()
        self._verify_target(role)
        guard = self._guard()
        receipt = {
            "schema": "deepseek-v4-flash-vision-receipt-v1", "project": PROJECT,
            "role": role, "verb": verb, "operation_id": transaction,
            "lease_generation": str(guard.get("lease_generation", "")), "nonce": uuid.uuid4().hex,
            "not_after": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "trace_nonce": hashlib.sha256(transaction.encode()).hexdigest(),
        }
        if max_model_len is not None:
            if verb != "start" or type(max_model_len) is not int or max_model_len not in (32768, 65536, 131072, 262144, 524288):
                raise ControllerError("invalid context length")
            receipt["max_model_len"] = max_model_len
        if gateway_token is not None: receipt["gateway_token"] = gateway_token
        if request_body_sha256 is not None: receipt["request_body_sha256"] = request_body_sha256
        if control_body_sha256 is not None: receipt["control_body_sha256"] = control_body_sha256
        self.journal.append({"event": "RECEIPT_ISSUED", "operation_id": transaction,
                             "role": role, "verb": verb, "nonce": receipt["nonce"]})
        result = self.runner.run_supervised(
            self._ssh(role, [REMOTE_HELPER, "--receipt-stdin", "--role", role, "--verb", verb]),
            input_text=json.dumps(receipt, separators=(",", ":")) + "\n",
            timeout=1300 if verb == "start" else 180,
            lease_check=self._guard,
        )
        self._guard()
        value = _json_result(result, f"{verb} {role}")
        if value.get("terminal") != "succeeded" or value.get("operation_id") != transaction:
            raise ControllerError(f"{verb} {role} returned an invalid terminal receipt")
        self.journal.append({"event": "RECEIPT_TERMINAL", "operation_id": transaction,
                             "role": role, "verb": verb, "sha256": value.get("sha256")})
        return value

    def start(self, transaction_id=None, *, _under_active=False, max_model_len=32768) -> dict:
        transaction = transaction_id or uuid.uuid4().hex
        if type(max_model_len) is not int or max_model_len not in (32768, 65536, 131072, 262144, 524288):
            raise ControllerError("invalid context length")
        with self._lease("start Vision ranks worker-first", _under_active):
            try:
                worker = self._mutate("worker", "start", transaction, max_model_len=max_model_len)
                head = self._mutate("head", "start", transaction, max_model_len=max_model_len)
            except BaseException:
                for role in ("head", "worker"):
                    try: self._mutate(role, "stop", transaction)
                    except BaseException: pass
                raise
        return {"transaction_id": transaction, "worker": worker, "head": head}

    def stop(self, transaction_id=None, *, _under_active=False) -> dict:
        transaction = transaction_id or uuid.uuid4().hex
        with self._lease("stop Vision ranks head-first", _under_active):
            head = self._mutate("head", "stop", transaction)
            worker = self._mutate("worker", "stop", transaction)
        return {"transaction_id": transaction, "head": head, "worker": worker}

    @staticmethod
    def _read_secret() -> str:
        path = _private_external(DEFAULT_GATEWAY_SECRET, "gateway secret")
        if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise ControllerError("Vision gateway secret is missing or unsafe")
        token = path.read_text().strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token):
            raise ControllerError("Vision gateway secret format is invalid")
        return token

    def production_activate_gateway(self, transaction_id: str, *, _under_active=False) -> dict:
        with self._lease("activate Vision production gateway", _under_active):
            head = self._mutate("head", "production-activate-gateway", transaction_id,
                                gateway_token=self._read_secret())
        return {"transaction_id": transaction_id, "head": head}

    def staging_activate_gateway(self, transaction_id: str, *, _under_active=False) -> dict:
        with self._lease("activate Vision loopback staging gateway", _under_active):
            head = self._mutate("head", "staging-activate-gateway", transaction_id,
                                gateway_token=self._read_secret())
        return {"transaction_id": transaction_id, "head": head}

    def production_deactivate_gateway(self, transaction_id: str, *, _under_active=False) -> dict:
        with self._lease("deactivate Vision production gateway", _under_active):
            head = self._mutate("head", "production-deactivate-gateway", transaction_id)
        return {"transaction_id": transaction_id, "head": head}

    def fail_close_head(self, transaction_id: str, *, _under_active=False) -> dict:
        with self._lease("fail close Vision head and expose maintenance", _under_active):
            proof = self._mutate(
                "head", "fail-close-head", transaction_id,
                gateway_token=self._read_secret(),
            )
        required = {"model_id": MODEL_ID, "head_fail_closed": True,
                    "maintenance_active": True, "recovery_id": transaction_id}
        if any(proof.get(key) != value for key, value in required.items()):
            raise ControllerError("head fail-close proof is incomplete")
        return {**required, "sha256": proof.get("sha256")}

    def _chat(self, token: str, messages: list) -> tuple[str, str]:
        body = json.dumps({"model": MODEL_ID, "messages": messages,
                           "temperature": 0, "max_tokens": 24,
                           "chat_template_kwargs": {"thinking": False, "enable_thinking": False}}).encode()
        request = urllib.request.Request(
            "http://192.0.2.30:11434/v1/chat/completions", data=body, method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=120) as response:
            value = json.loads(response.read(1024 * 1024))
        return str(value["choices"][0]["message"]["content"]), hashlib.sha256(body).hexdigest()

    def acceptance_health(self, transaction_id: str, *, _under_active=False) -> dict:
        with self._lease("prove native Vision health", _under_active):
            token = self._read_secret()
            prompt = "Identify the dominant pixel color in the attached image. Reply with one color word."
            control, control_request_sha256 = self._chat(
                token, [{"role": "user", "content": prompt}]
            )
            image, image_request_sha256 = self._chat(token, [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{CANARY_PNG}"}},
            ]}])
            head = self._mutate(
                "head", "acceptance-rank-proof", transaction_id,
                request_body_sha256=image_request_sha256,
                control_body_sha256=control_request_sha256,
            )
            worker = self._mutate(
                "worker", "acceptance-rank-proof", transaction_id,
                request_body_sha256=image_request_sha256,
                control_body_sha256=control_request_sha256,
            )
        passed = "red" in image.lower() and "red" not in control.lower()
        proof = {"healthy": passed, "model_id": MODEL_ID, "native_vision": passed,
                 "canary_passed": passed, "head_rank": head.get("terminal") == "succeeded",
                 "worker_rank": worker.get("terminal") == "succeeded"}
        self.journal.append({"event": "NATIVE_VISION_CANARY", "operation_id": transaction_id,
                             **proof, "image_sha256": hashlib.sha256(base64.b64decode(CANARY_PNG)).hexdigest()})
        return proof
