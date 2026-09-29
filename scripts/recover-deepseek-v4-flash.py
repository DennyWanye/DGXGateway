#!/usr/bin/env python3
"""Bounded, head-local fallback for an already-armed cutover transaction."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.request
from typing import Any, Callable


STATE_DIR = pathlib.Path("/var/lib/deepseek-v4-flash-fallback")
ROOT_UID = 0
ARMED = STATE_DIR / "armed.json"
RUNNING = STATE_DIR / "running.json"
COMMITTED = STATE_DIR / "committed.json"
FALLBACK_LIFECYCLE_LOCK = pathlib.Path("/run/lock/deepseek-v4-flash-fallback.lifecycle.lock")
FALLBACK_TIMER = "deepseek-v4-flash-fallback.timer"
TIMER_DROPIN = pathlib.Path(
    "/etc/systemd/system/deepseek-v4-flash-fallback.timer.d/deadline.conf"
)
GATEWAY_CAPABILITY_DIR = pathlib.Path("/run/deepseek-v4-flash-dspark")
GATEWAY_PENDING = GATEWAY_CAPABILITY_DIR / "gateway-authorized.pending"
GATEWAY_RUNNING = GATEWAY_CAPABILITY_DIR / "gateway-authorized.running"
EXACT_NEW_SYSTEM_UNIT = "deepseek-v4-flash-gateway.service"
COMPOSE_ENV = "/etc/deepseek-v4-flash-dspark.env"
COMPOSE_FILE = "/opt/deepseek-v4-flash-dspark/docker-compose.dspark.yml"
COMPOSE_PROJECT = "deepseek-v4-flash-dspark"
COMPOSE_SERVICE = "vllm-dspark"
OLLAMA_UNIT = "ollama.service"
SECOND_LEGACY_UNIT = "dgx-codex-production.service"
OLLAMA_URL = "http://192.0.2.30:11434"


class Runner:
    def run(self, *argv: str, check: bool = True) -> str:
        command = list(argv)
        if command[0] == "user-systemctl":
            command = [
                "runuser",
                "-u",
                "operator",
                "--",
                "env",
                "XDG_RUNTIME_DIR=/run/user/1000",
                "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
                "systemctl",
                "--user",
                *command[1:],
            ]
        elif command == ["assert-ollama-socket"]:
            result = subprocess.run(
                ["ss", "-H", "-ltnp", "sport = :11434"],
                check=True,
                text=True,
                capture_output=True,
                timeout=10,
            )
            owners = [line for line in result.stdout.splitlines() if line.strip()]
            if len(owners) != 1 or "ollama" not in owners[0]:
                raise RuntimeError("Ollama is not the sole observed 11434 owner")
            return result.stdout
        result = subprocess.run(
            command,
            check=check,
            text=True,
            capture_output=True,
            timeout=120,
        )
        return result.stdout


def _request(path: str, *, body: dict[str, Any] | None = None, timeout: int) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        OLLAMA_URL + path,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def ollama_oracle(model: str, expected_version: str) -> None:
    deadline = time.monotonic() + 120
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            version = _request("/api/version", timeout=3)
            if version.get("version") != expected_version:
                raise RuntimeError("restored Ollama version differs from frozen preimage")
            break
        except Exception as exc:  # bounded readiness retry
            last_error = exc
            time.sleep(2)
    else:
        raise RuntimeError(f"Ollama version oracle timed out: {last_error}")

    tags = _request("/api/tags", timeout=10)
    if model not in {item.get("name") for item in tags.get("models", [])}:
        raise RuntimeError("frozen Ollama model is absent")
    generated = _request(
        "/api/generate",
        body={
            "model": model,
            "prompt": "Reply with exactly: fallback-ok",
            "stream": False,
            "keep_alive": 0,
            "options": {"temperature": 0, "num_ctx": 2048},
        },
        timeout=240,
    )
    if generated.get("done") is not True or not str(generated.get("response", "")).strip():
        raise RuntimeError("bounded Ollama generation oracle failed")


def _validate(config: dict[str, Any], hostname: str) -> None:
    expected_keys = {
        "schema_version",
        "state",
        "transaction_id",
        "head_hostname",
        "old_model",
        "old_ollama_version",
        "legacy_units",
    }
    transaction_id = config.get("transaction_id")
    if (
        set(config) != expected_keys
        or config.get("schema_version") != 1
        or config.get("state") != "FALLBACK_ARMED"
        or not isinstance(transaction_id, str)
        or len(transaction_id) != 32
        or any(character not in "0123456789abcdef" for character in transaction_id)
    ):
        raise ValueError("invalid or unarmed fallback transaction")
    if hostname != "dgx-head" or config.get("head_hostname") != hostname:
        raise ValueError("fallback is restricted to dgx-head")
    if config.get("old_model") != "qwen38-q4-256k:latest":
        raise ValueError("old model differs from frozen rollback oracle")
    if config.get("old_ollama_version") != "0.32.15":
        raise ValueError("old Ollama version differs from frozen rollback oracle")
    units = config.get("legacy_units")
    if not isinstance(units, dict) or set(units) != {OLLAMA_UNIT, SECOND_LEGACY_UNIT}:
        raise ValueError("legacy unit preimage is incomplete")
    for value in units.values():
        if not isinstance(value, dict) or set(value) != {"enabled", "active"}:
            raise ValueError("legacy unit preimage has unexpected fields")
        if not all(isinstance(value[key], bool) for key in ("enabled", "active")):
            raise ValueError("legacy unit preimage must contain booleans")
    if units[OLLAMA_UNIT] != {"enabled": True, "active": True}:
        raise ValueError("frozen Ollama production preimage must be enabled and active")


def _restore_enablement(runner: Runner, unit: str, enabled: bool) -> None:
    runner.run("user-systemctl", "enable" if enabled else "disable", unit)


def recover(
    config: dict[str, Any],
    runner: Runner,
    oracle: Callable[[str, str], None],
    *,
    hostname: str,
) -> None:
    _validate(config, hostname)
    units = config["legacy_units"]

    # Freeze both legacy contenders before releasing the new production socket.
    runner.run("user-systemctl", "stop", SECOND_LEGACY_UNIT, check=False)
    runner.run("user-systemctl", "stop", OLLAMA_UNIT, check=False)
    runner.run("systemctl", "stop", EXACT_NEW_SYSTEM_UNIT, check=False)
    runner.run("docker", "stop", "--time", "30", "deepseek-v4-flash-gateway", check=False)
    runner.run(
        "docker",
        "compose",
        "--env-file",
        COMPOSE_ENV,
        "--file",
        COMPOSE_FILE,
        "--project-name",
        COMPOSE_PROJECT,
        "stop",
        "--timeout",
        "30",
        COMPOSE_SERVICE,
    )
    remaining = runner.run(
        "docker", "compose", "--env-file", COMPOSE_ENV, "--file", COMPOSE_FILE,
        "--project-name", COMPOSE_PROJECT, "ps", "-q", COMPOSE_SERVICE,
    ).strip()
    exact_running = runner.run(
        "docker", "ps", "--filter", f"label=com.docker.compose.project={COMPOSE_PROJECT}",
        "--filter", f"label=com.docker.compose.service={COMPOSE_SERVICE}", "--format", "{{.ID}}",
    ).strip()
    if remaining or exact_running:
        raise RuntimeError("DeepSeek project service remained active after bounded stop")

    _restore_enablement(runner, OLLAMA_UNIT, units[OLLAMA_UNIT]["enabled"])
    runner.run("user-systemctl", "restart", OLLAMA_UNIT)
    oracle(config["old_model"], config["old_ollama_version"])
    runner.run("assert-ollama-socket")

    # Only after the accepted listener/oracles pass may the second preimage run.
    _restore_enablement(runner, SECOND_LEGACY_UNIT, units[SECOND_LEGACY_UNIT]["enabled"])
    if units[SECOND_LEGACY_UNIT]["active"]:
        runner.run("user-systemctl", "start", "--no-block", SECOND_LEGACY_UNIT, check=False)
    else:
        runner.run("user-systemctl", "stop", SECOND_LEGACY_UNIT, check=False)
    runner.run("sleep", "1")
    runner.run("assert-ollama-socket")


def _atomic_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _require_root() -> None:
    if os.geteuid() != ROOT_UID:
        raise PermissionError("fallback lifecycle changes require root")


def _read_capability(path: pathlib.Path, *, hostname: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"fallback capability is absent or unsafe: {path}")
    info = path.stat()
    if stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != ROOT_UID:
        raise RuntimeError("armed transaction must be root-owned mode 0600")
    value = json.loads(path.read_text())
    _validate(value, hostname)
    return value


def _assert_transaction(value: dict[str, Any], transaction_id: str) -> None:
    if value.get("transaction_id") != transaction_id:
        raise RuntimeError("fallback transaction mismatch")


def _atomic_text(path: pathlib.Path, value: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    os.replace(temporary, path)
    os.chmod(path, mode)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _deadline_text(deadline_epoch: float) -> str:
    deadline = datetime.datetime.fromtimestamp(deadline_epoch, datetime.timezone.utc)
    return (
        "[Timer]\n"
        "OnCalendar=\n"
        f"OnCalendar={deadline.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
    )


def _activate_timer(runner: Runner, deadline_epoch: float) -> None:
    _atomic_text(TIMER_DROPIN, _deadline_text(deadline_epoch))
    runner.run("systemctl", "daemon-reload")
    runner.run("systemctl", "enable", FALLBACK_TIMER)
    # An already-active timer does not consume a changed absolute drop-in until
    # it is restarted; enable --now alone would leave the old deadline armed.
    runner.run("systemctl", "restart", FALLBACK_TIMER)
    if (
        runner.run("systemctl", "is-enabled", FALLBACK_TIMER, check=False).strip()
        != "enabled"
        or runner.run("systemctl", "is-active", FALLBACK_TIMER, check=False).strip()
        != "active"
    ):
        raise RuntimeError("fallback timer did not arm at the refreshed deadline")


def _prove_timer_disabled(runner: Runner) -> None:
    enabled = runner.run("systemctl", "is-enabled", FALLBACK_TIMER, check=False).strip()
    active = runner.run("systemctl", "is-active", FALLBACK_TIMER, check=False).strip()
    if enabled != "disabled" or active != "inactive" or TIMER_DROPIN.exists():
        raise RuntimeError("fallback timer disarm proof failed")


def arm(
    config: dict[str, Any],
    runner: Runner,
    *,
    hostname: str,
    deadline_epoch: float | None = None,
) -> None:
    _require_root()
    _validate(config, hostname)
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_DIR, 0o700)
    if RUNNING.exists():
        raise RuntimeError("cannot arm while recovery is running")
    if ARMED.exists():
        current = _read_capability(ARMED, hostname=hostname)
        _assert_transaction(current, config["transaction_id"])
        if current != config:
            raise RuntimeError("same transaction has different fallback preimage")
    else:
        _atomic_json(ARMED, config)
    os.chmod(ARMED, 0o600)
    if ARMED.stat().st_uid != ROOT_UID:
        raise RuntimeError("armed transaction is not root-owned")
    _activate_timer(runner, deadline_epoch or time.time() + 600)


def refresh(
    transaction_id: str,
    runner: Runner,
    *,
    hostname: str,
    deadline_epoch: float | None = None,
) -> None:
    _require_root()
    config = _read_capability(ARMED, hostname=hostname)
    _assert_transaction(config, transaction_id)
    _activate_timer(runner, deadline_epoch or time.time() + 600)


def quiesce(transaction_id: str, runner: Runner, *, hostname: str) -> None:
    _require_root()
    config = _read_capability(ARMED, hostname=hostname)
    _assert_transaction(config, transaction_id)
    for unit in (SECOND_LEGACY_UNIT, OLLAMA_UNIT):
        runner.run("user-systemctl", "stop", unit, check=False)
        runner.run("user-systemctl", "disable", unit)
        if runner.run("user-systemctl", "is-active", unit, check=False).strip() not in {
            "inactive",
            "failed",
            "",
        }:
            raise RuntimeError(f"legacy unit did not quiesce: {unit}")


def _disable_timer(runner: Runner) -> None:
    runner.run("systemctl", "disable", "--now", FALLBACK_TIMER, check=False)
    TIMER_DROPIN.unlink(missing_ok=True)
    runner.run("systemctl", "daemon-reload")
    _prove_timer_disabled(runner)


def disarm(transaction_id: str, runner: Runner, *, hostname: str) -> None:
    _require_root()
    if RUNNING.exists():
        running = _read_capability(RUNNING, hostname=hostname)
        _assert_transaction(running, transaction_id)
        raise RuntimeError("cannot commit while fallback recovery is running")
    config = _read_capability(ARMED, hostname=hostname)
    _assert_transaction(config, transaction_id)
    _atomic_json(
        COMMITTED,
        {"schema_version": 1, "transaction_id": transaction_id, "committed_at": time.time()},
    )
    ARMED.unlink()
    _disable_timer(runner)


def cancel(transaction_id: str, runner: Runner, *, hostname: str) -> None:
    """Cancel only after an exact, successful recovery terminal exists."""
    _require_root()
    success_path = STATE_DIR / "recovery-succeeded.json"
    if not success_path.is_file():
        raise RuntimeError("cannot cancel fallback without recovery success")
    success = json.loads(success_path.read_text())
    if success.get("transaction_id") != transaction_id:
        raise RuntimeError("rollback recovery transaction mismatch")
    for capability in (ARMED, RUNNING):
        if capability.exists():
            value = _read_capability(capability, hostname=hostname)
            _assert_transaction(value, transaction_id)
            capability.unlink()
    _disable_timer(runner)


def gateway_start(transaction_id: str, runner: Runner, *, hostname: str) -> None:
    """Mint one capability, start once, and reject condition-skipped starts."""
    _require_root()
    if hostname != "dgx-head":
        raise RuntimeError("gateway start is restricted to dgx-head")
    if len(transaction_id) != 32 or any(
        character not in "0123456789abcdef" for character in transaction_id
    ):
        raise ValueError("invalid gateway transaction ID")
    GATEWAY_CAPABILITY_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(GATEWAY_CAPABILITY_DIR, 0o700)
    if GATEWAY_RUNNING.is_file() and not GATEWAY_PENDING.exists():
        running = json.loads(GATEWAY_RUNNING.read_text(encoding="utf-8"))
        active = runner.run(
            "systemctl", "show", EXACT_NEW_SYSTEM_UNIT, "--property=ActiveState", "--value",
            check=False,
        ).strip()
        if running == {"schema_version": 1, "transaction_id": transaction_id} and active == "active":
            return
    if GATEWAY_PENDING.exists() or GATEWAY_RUNNING.exists():
        raise RuntimeError("stale gateway authorization capability exists")
    _atomic_json(
        GATEWAY_PENDING,
        {"schema_version": 1, "transaction_id": transaction_id},
    )
    runner.run("systemctl", "reset-failed", EXACT_NEW_SYSTEM_UNIT, check=False)
    runner.run("systemctl", "start", EXACT_NEW_SYSTEM_UNIT)
    active = runner.run(
        "systemctl", "show", EXACT_NEW_SYSTEM_UNIT, "--property=ActiveState", "--value"
    ).strip()
    if active != "active" or GATEWAY_PENDING.exists() or not GATEWAY_RUNNING.is_file():
        raise RuntimeError("condition-skipped gateway start did not reach ActiveState=active")
    running = json.loads(GATEWAY_RUNNING.read_text())
    if running != {"schema_version": 1, "transaction_id": transaction_id}:
        raise RuntimeError("gateway running capability transaction mismatch")


def gateway_fence(transaction_id: str, container_id: str, runner: Runner, *, hostname: str,
                  timeout: float = 1200, clock=time.monotonic, sleep=time.sleep) -> dict:
    """Close admission gracefully; never escalate to TERM, KILL or model stop.

    Docker's retained die event supplies the exit code even for --rm containers.
    The caller separately proves model identity and zero running/waiting requests.
    """
    _require_root()
    if hostname != "dgx-head":
        raise RuntimeError("gateway fence is restricted to dgx-head")
    for value, length in ((transaction_id, 32), (container_id, 64)):
        if len(value) != length or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("invalid gateway fence identity")
    if runner.run("systemctl", "show", EXACT_NEW_SYSTEM_UNIT,
                  "--property=Restart", "--value").strip() != "no":
        raise RuntimeError("gateway automatic restart prevents admission fencing")
    receipt_path = GATEWAY_CAPABILITY_DIR / ("fence-" + container_id + ".json")
    if GATEWAY_CAPABILITY_DIR.is_symlink() or not GATEWAY_CAPABILITY_DIR.is_dir():
        raise RuntimeError("gateway fence receipt directory is unavailable")
    directory_info = GATEWAY_CAPABILITY_DIR.stat()
    if directory_info.st_uid != ROOT_UID or stat.S_IMODE(directory_info.st_mode) & 0o077:
        raise RuntimeError("gateway fence receipt directory is not private")
    record = None
    if receipt_path.exists() or receipt_path.is_symlink():
        info = receipt_path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != ROOT_UID or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 8192:
            raise RuntimeError("gateway fence receipt is not a private root-owned file")
        record = json.loads(receipt_path.read_text())
        if (not isinstance(record, dict) or record.get("schema_version") != 1
                or record.get("container_id") != container_id
                or record.get("phase") not in ("INTENT", "CLOSED")
                or not isinstance(record.get("transaction_id"), str)
                or len(record["transaction_id"]) != 32
                or any(c not in "0123456789abcdef" for c in record["transaction_id"])
                or not isinstance(record.get("since"), str)
                or not record["since"].isascii() or not record["since"].isdigit()
                or not 1 <= len(record["since"]) <= 12
                or not 0 < int(record["since"]) <= int(time.time()) + 1
                or (record["phase"] == "CLOSED" and
                    (type(record.get("exit_code")) is not int or record["exit_code"] != 0))):
            raise RuntimeError("gateway fence receipt identity is invalid")
    def active_id():
        return runner.run("docker", "ps", "--no-trunc", "--filter",
                          "name=^/deepseek-v4-flash-gateway$", "--format", "{{.ID}}").strip()
    active = active_id()
    if active and active != container_id:
        raise RuntimeError("a different gateway appeared before fence")
    if active:
        expected = container_id + "|/deepseek-v4-flash-gateway|true|no"
        identity = runner.run("docker", "inspect", "--format",
                              "{{.Id}}|{{.Name}}|{{.State.Running}}|{{.HostConfig.RestartPolicy.Name}}",
                              container_id).strip()
        if identity != expected or (record and record["phase"] == "CLOSED"):
            raise RuntimeError("gateway container identity changed before fence")
        if record is None:
            record = {"schema_version": 1, "container_id": container_id, "transaction_id": transaction_id,
                      "since": str(int(time.time()) - 1), "phase": "INTENT"}
            _atomic_json(receipt_path, record)
        # Repeating QUIT on the same still-running instance is a graceful query
        # continuation, never a restart or force-stop fallback.
        runner.run("docker", "exec", container_id, "nginx", "-s", "quit")
    elif record is None:
        raise RuntimeError("closed gateway lacks this operation's durable fence intent")
    deadline = clock() + timeout
    while True:
        active = active_id()
        if not active:
            break
        if active != container_id:
            raise RuntimeError("a different gateway appeared during fence")
        if clock() >= deadline:
            raise RuntimeError("gateway graceful drain deadline; model and ownership retained")
        sleep(1)
    if runner.run("ss", "-H", "-ltn", "sport = :11434").strip():
        raise RuntimeError("LAN admission listener remained after graceful fence")
    if record["phase"] != "CLOSED":
        events = runner.run("docker", "events", "--since", record["since"], "--until", str(int(time.time()) + 1),
                            "--filter", "container=" + container_id, "--filter", "event=die",
                            "--format", "{{json .}}")
        normal_exit = False
        for line in events.splitlines():
            event = json.loads(line)
            actor = event.get("Actor", {})
            if actor.get("ID") == container_id and str(actor.get("Attributes", {}).get("exitCode")) == "0":
                normal_exit = True
        if not normal_exit:
            raise RuntimeError("graceful gateway exit lacks a matching zero-exit receipt")
        record.update(phase="CLOSED", exit_code=0)
        _atomic_json(receipt_path, record)
    return {"admission_closed": True, "gateway_container_id": container_id,
            "graceful_exit": True, "transaction_id": transaction_id,
            "original_transaction_id": record["transaction_id"]}


def gateway_stop(transaction_id: str, runner: Runner, *, hostname: str) -> None:
    """Stop only the exact DeepSeek gateway and remove matching capabilities."""
    _require_root()
    if hostname != "dgx-head":
        raise RuntimeError("gateway stop is restricted to dgx-head")
    if len(transaction_id) != 32 or any(
        character not in "0123456789abcdef" for character in transaction_id
    ):
        raise ValueError("invalid gateway transaction ID")
    for capability in (GATEWAY_PENDING, GATEWAY_RUNNING):
        if capability.exists():
            value = json.loads(capability.read_text(encoding="utf-8"))
            capability_transaction = value.get("transaction_id") if isinstance(value, dict) else None
            if value.get("schema_version") != 1 or not isinstance(capability_transaction, str) or \
                    len(capability_transaction) != 32 or any(
                        character not in "0123456789abcdef"
                        for character in capability_transaction
                    ):
                raise RuntimeError(f"gateway capability is malformed: {capability}")
    runner.run("systemctl", "stop", EXACT_NEW_SYSTEM_UNIT, check=False)
    runner.run("docker", "rm", "--force", "deepseek-v4-flash-gateway", check=False)
    active = runner.run(
        "systemctl", "show", EXACT_NEW_SYSTEM_UNIT, "--property=ActiveState", "--value",
        check=False,
    ).strip()
    if active == "active":
        raise RuntimeError("gateway unit remained active after stop")
    for capability in (GATEWAY_PENDING, GATEWAY_RUNNING):
        if capability.exists():
            capability.unlink()


def recover_main() -> int:
    capability = RUNNING if RUNNING.exists() else ARMED
    if not capability.exists():
        raise RuntimeError("no armed fallback capability")
    config = _read_capability(capability, hostname=socket.gethostname())
    if COMMITTED.exists():
        committed = json.loads(COMMITTED.read_text())
        if committed.get("transaction_id") == config["transaction_id"]:
            return 0
    if capability == ARMED:
        os.replace(ARMED, RUNNING)
        directory = os.open(STATE_DIR, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    started = time.time()
    _atomic_json(
        STATE_DIR / "recovery-started.json",
        {"transaction_id": config.get("transaction_id"), "started_at": started},
    )
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    def terminated(signum, _frame):
        raise RuntimeError(f"fallback recovery terminated by signal {signum}")
    signal.signal(signal.SIGTERM, terminated)
    try:
        recover(config, Runner(), ollama_oracle, hostname=socket.gethostname())
    except Exception as exc:
        _atomic_json(
            STATE_DIR / "recovery-failed.json",
            {
                "transaction_id": config.get("transaction_id"),
                "failed_at": time.time(),
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        raise
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
    _atomic_json(
        STATE_DIR / "recovery-succeeded.json",
        {
            "transaction_id": config["transaction_id"],
            "finished_at": time.time(),
            "elapsed_seconds": round(time.time() - started, 3),
        },
    )
    RUNNING.unlink()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")
    arm_parser = sub.add_parser("arm")
    arm_parser.add_argument("--config-json", required=True)
    for command in ("refresh", "quiesce", "disarm", "cancel", "gateway-start", "gateway-stop"):
        child = sub.add_parser(command)
        child.add_argument("--transaction-id", required=True)
    fence = sub.add_parser("gateway-fence")
    fence.add_argument("--transaction-id", required=True)
    fence.add_argument("--container-id", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command is None:
        return recover_main()
    runner = Runner()
    hostname = socket.gethostname()
    if args.command == "arm":
        arm(json.loads(args.config_json), runner, hostname=hostname)
    elif args.command == "refresh":
        refresh(args.transaction_id, runner, hostname=hostname)
    elif args.command == "quiesce":
        quiesce(args.transaction_id, runner, hostname=hostname)
    elif args.command == "disarm":
        disarm(args.transaction_id, runner, hostname=hostname)
    elif args.command == "gateway-start":
        gateway_start(args.transaction_id, runner, hostname=hostname)
    elif args.command == "gateway-stop":
        gateway_stop(args.transaction_id, runner, hostname=hostname)
    elif args.command == "gateway-fence":
        print(json.dumps(gateway_fence(args.transaction_id, args.container_id, runner, hostname=hostname), sort_keys=True))
    else:
        cancel(args.transaction_id, runner, hostname=hostname)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"deepseek fallback failed: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
