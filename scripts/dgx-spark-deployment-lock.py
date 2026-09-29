#!/usr/bin/env python3
"""Fail-closed deployment lease shared by all two-node DGX Spark tasks."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_ROOT = Path("/Users/operator/.codex/locks/dgx-spark-two-node")
SCHEMA_VERSION = 1
CLUSTER_ID = "dgx-head+dgx-worker"


class LockError(RuntimeError):
    pass


class LifecycleMutex:
    def __init__(self, root: Path):
        self.path = root.parent / f".{root.name}.lifecycle.lock"
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.stream = self.path.open("a+", encoding="utf-8")
        os.chmod(self.path, 0o600)
        fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *_):
        fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        self.stream.close()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def history_dir(root: Path) -> Path:
    return root.parent / f"{root.name}-history"


def append_release_record(root: Path, operation_id: str, phase: str, value: dict) -> tuple[Path, str]:
    directory = history_dir(root)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    path = directory / f"release-{operation_id}-{phase}.json"
    if path.exists():
        raise LockError(f"release record already exists: {path}")
    payload = {"schema_version": 1, "operation_id": operation_id, "phase": phase, **value}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["payload_sha256"] = hashlib.sha256(canonical).hexdigest()
    atomic_json(path, payload)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def release_record_path(root: Path, operation_id: str, phase: str) -> Path:
    return history_dir(root) / f"release-{operation_id}-{phase}.json"


def read_release_record(root: Path, operation_id: str, phase: str) -> dict | None:
    path = release_record_path(root, operation_id, phase)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        claimed = payload.pop("payload_sha256")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        if token_hash(canonical.decode()) != claimed:
            raise ValueError("payload hash mismatch")
        payload["payload_sha256"] = claimed
        return payload
    except Exception as exc:
        raise LockError(f"invalid immutable release record {path}: {exc}") from exc


def pending_release(root: Path) -> dict | None:
    directory = history_dir(root)
    if not directory.exists():
        return None
    candidates: list[dict] = []
    for path in directory.glob("release-*-intent.json"):
        name = path.name
        operation_id = name[len("release-") : -len("-intent.json")]
        intent = read_release_record(root, operation_id, "intent")
        if intent and not release_record_path(root, operation_id, "completion").exists():
            candidates.append(intent)
    if len(candidates) > 1:
        raise LockError("UNKNOWN lock state: multiple pending release intents")
    return candidates[0] if candidates else None


def tombstone_path(root: Path, operation_id: str) -> Path:
    return root.parent / f".{root.name}.release-{operation_id}"


def maybe_fault(args: argparse.Namespace, point: str) -> None:
    if getattr(args, "test_fault_after", None) != point:
        return
    if os.environ.get("DGX_LOCK_TEST_MODE") != "1" or args.root == DEFAULT_ROOT:
        raise LockError("test fault injection is forbidden for the production lock")
    os._exit(97)


def validate_release_capability(intent: dict, owner: str, token_file: Path) -> None:
    if intent.get("owner") != owner:
        raise LockError(f"release owner is {intent.get('owner')!r}, not {owner!r}")
    if token_hash(read_token(token_file)) != intent.get("token_sha256"):
        raise LockError("token mismatch")


def validate_stale_retirement_proof(path: Path, verifier_key_path: Path, data: dict) -> tuple[str, str]:
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise LockError("stale retirement proof is missing or unsafe")
    try:
        proof = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise LockError("invalid stale retirement proof") from exc
    evidence_path = Path(str(proof.get("evidence_path", "")))
    if (
        not evidence_path.is_absolute() or evidence_path.is_symlink()
        or not evidence_path.is_file() or evidence_path.stat().st_mode & 0o022
    ):
        raise LockError("stale retirement evidence artifact is missing or unsafe")
    evidence_bytes = evidence_path.read_bytes()
    evidence_sha256 = hashlib.sha256(evidence_bytes).hexdigest()
    try:
        evidence = json.loads(evidence_bytes)
    except Exception as exc:
        raise LockError("invalid stale retirement evidence artifact") from exc
    if (
        verifier_key_path.is_symlink() or not verifier_key_path.is_file()
        or verifier_key_path.stat().st_mode & 0o077
    ):
        raise LockError("stale retirement verifier key is missing or unsafe")
    verifier_key = verifier_key_path.read_text(encoding="utf-8").strip()
    if len(verifier_key) < 32:
        raise LockError("stale retirement verifier key is invalid")
    signature_payload = f"{evidence_sha256}:{data['owner']}:{data['lease_generation']}".encode()
    expected_hmac = hmac.new(verifier_key.encode(), signature_payload, hashlib.sha256).hexdigest()
    try:
        sampled_at = parse_time(str(evidence.get("sampled_at")))
    except Exception as exc:
        raise LockError("stale retirement evidence timestamp is invalid") from exc
    valid = (
        proof.get("schema") == "dgx-stale-retirement-proof-v1"
        and proof.get("cluster_id") == CLUSTER_ID
        and proof.get("owner") == data["owner"]
        and proof.get("lease_generation") == data["lease_generation"]
        and proof.get("evidence_sha256") == evidence_sha256
        and hmac.compare_digest(str(proof.get("verifier_hmac_sha256", "")), expected_hmac)
        and evidence.get("schema") == "dgx-stale-retirement-evidence-v1"
        and evidence.get("cluster_id") == CLUSTER_ID
        and evidence.get("owner") == data["owner"]
        and evidence.get("lease_generation") == data["lease_generation"]
        and evidence.get("observed_local_model_state") in {"ACTIVE_EXCLUSIVE", "ALL_STOPPED"}
        and evidence.get("remote_attestation_absent") is True
        and timedelta(0) <= utc_now() - sampled_at <= timedelta(minutes=15)
    )
    if not valid:
        raise LockError("stale retirement proof does not bind the live lease and safe state")
    return str(path), hashlib.sha256(path.read_bytes()).hexdigest()


def read_metadata(root: Path) -> dict:
    path = root / "owner.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise LockError(f"UNKNOWN lock state: cannot read {path}: {exc}") from exc
    required = {
        "schema_version",
        "cluster_id",
        "owner",
        "task_id",
        "purpose",
        "created_at",
        "heartbeat_at",
        "lease_seconds",
        "lease_generation",
        "token_sha256",
    }
    missing = sorted(required - data.keys())
    if missing or data["schema_version"] != SCHEMA_VERSION or data["cluster_id"] != CLUSTER_ID:
        raise LockError(f"UNKNOWN lock state: invalid metadata, missing={missing}")
    return data


def read_token(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except Exception as exc:
        raise LockError(f"cannot read token file {path}: {exc}") from exc
    if not token:
        raise LockError(f"empty token file {path}")
    return token


def validate_owner(data: dict, owner: str, token_file: Path) -> None:
    if data["owner"] != owner:
        raise LockError(f"lock owner is {data['owner']!r}, not {owner!r}")
    if token_hash(read_token(token_file)) != data["token_sha256"]:
        raise LockError("token mismatch")


def expiry(data: dict) -> datetime:
    return parse_time(data["heartbeat_at"]) + timedelta(seconds=int(data["lease_seconds"]))


def require_live(data: dict, now: datetime) -> None:
    if now >= expiry(data):
        raise LockError(
            f"STALE lock owned by {data['owner']!r}; expired at {iso(expiry(data))}; "
            "automatic takeover is forbidden"
        )


def cmd_acquire(args: argparse.Namespace) -> dict:
    root: Path = args.root
    token_file: Path = args.token_file
    with LifecycleMutex(root):
        root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        release = pending_release(root)
        if release:
            raise LockError(
                f"RECOVERY_REQUIRED pending release operation {release['operation_id']!r}; "
                "automatic takeover is forbidden"
            )
        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            data = read_metadata(root)
            state = "STALE" if utc_now() >= expiry(data) else "ACTIVE"
            raise LockError(f"{state} lock already owned by {data['owner']!r} for {data['purpose']!r}")
        token_created = False
        token = secrets.token_urlsafe(32)
        try:
            token_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError as exc:
                raise LockError(f"token file already exists: {token_file}") from exc
            token_created = True
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(token + "\n")
            now = utc_now()
            data = {
                "schema_version": SCHEMA_VERSION,
                "cluster_id": CLUSTER_ID,
                "owner": args.owner,
                "task_id": args.task_id,
                "purpose": args.purpose,
                "created_at": iso(now),
                "heartbeat_at": iso(now),
                "lease_seconds": args.lease_seconds,
                "lease_generation": secrets.token_hex(16),
                "expires_at": iso(now + timedelta(seconds=args.lease_seconds)),
                "token_sha256": token_hash(token),
                "controller_host": socket.gethostname(),
                "controller_pid_at_acquire": os.getpid(),
            }
            atomic_json(root / "owner.json", data)
            return {"state": "ACTIVE", **data}
        except Exception:
            if token_created:
                token_file.unlink(missing_ok=True)
            shutil.rmtree(root)
            raise


def cmd_status(args: argparse.Namespace) -> dict:
    root: Path = args.root
    with LifecycleMutex(root):
        release = pending_release(root)
        if release:
            return {
                "state": "RECOVERY_REQUIRED",
                "cluster_id": CLUSTER_ID,
                "release_operation_id": release["operation_id"],
                "owner": release["owner"],
                "task_id": release["task_id"],
                "lease_generation": release["lease_generation"],
            }
        if not root.exists():
            return {"state": "UNLOCKED", "cluster_id": CLUSTER_ID}
        data = read_metadata(root)
        state = "STALE" if utc_now() >= expiry(data) else "ACTIVE"
        return {"state": state, **data}


def cmd_guard(args: argparse.Namespace) -> dict:
    with LifecycleMutex(args.root):
        data = read_metadata(args.root)
        validate_owner(data, args.owner, args.token_file)
        require_live(data, utc_now())
        return {
            "state": "ACTIVE",
            "owner": data["owner"],
            "lease_generation": data["lease_generation"],
            "expires_at": iso(expiry(data)),
        }


def cmd_heartbeat(args: argparse.Namespace) -> dict:
    with LifecycleMutex(args.root):
        data = read_metadata(args.root)
        now = utc_now()
        validate_owner(data, args.owner, args.token_file)
        require_live(data, now)
        data["heartbeat_at"] = iso(now)
        data["expires_at"] = iso(now + timedelta(seconds=int(data["lease_seconds"])))
        atomic_json(args.root / "owner.json", data)
        return {
            "state": "ACTIVE",
            "owner": data["owner"],
            "lease_generation": data["lease_generation"],
            "expires_at": data["expires_at"],
        }


def cmd_release(args: argparse.Namespace) -> dict:
    with LifecycleMutex(args.root):
        operation_id = args.operation_id or secrets.token_hex(16)
        if not re.fullmatch(r"[A-Za-z0-9._-]{3,128}", operation_id):
            raise LockError("invalid release operation id")
        completion = read_release_record(args.root, operation_id, "completion")
        if completion:
            if args.token_file.exists():
                validate_release_capability(completion, args.owner, args.token_file)
                args.token_file.unlink()
            completion_path = release_record_path(args.root, operation_id, "completion")
            return {
                "state": "UNLOCKED",
                "released_owner": completion["owner"],
                "release_operation_id": operation_id,
                "release_receipt": str(completion_path),
                "release_receipt_sha256": hashlib.sha256(completion_path.read_bytes()).hexdigest(),
            }

        intent = read_release_record(args.root, operation_id, "intent")
        if intent:
            validate_release_capability(intent, args.owner, args.token_file)
            common = {
                key: intent[key]
                for key in (
                    "cluster_id", "owner", "task_id", "purpose", "token_sha256",
                    "lease_generation", "acquired_at", "last_heartbeat_at",
                )
            }
            for key in (
                "stale_retirement", "stale_retirement_proof",
                "stale_retirement_proof_sha256",
            ):
                if key in intent:
                    common[key] = intent[key]
            intent_path = release_record_path(args.root, operation_id, "intent")
            intent_sha256 = hashlib.sha256(intent_path.read_bytes()).hexdigest()
        else:
            other_pending = pending_release(args.root)
            if other_pending:
                raise LockError(
                    f"RECOVERY_REQUIRED pending release operation {other_pending['operation_id']!r}"
                )
            data = read_metadata(args.root)
            now = utc_now()
            validate_owner(data, args.owner, args.token_file)
            stale_retirement = bool(getattr(args, "stale_retirement_proof", None))
            if stale_retirement:
                if not args.stale_retirement_verifier_key:
                    raise LockError("explicit stale retirement requires a verifier key")
                if now < expiry(data):
                    raise LockError("explicit stale retirement requires an expired lease")
                proof_path, proof_sha256 = validate_stale_retirement_proof(
                    args.stale_retirement_proof, args.stale_retirement_verifier_key, data
                )
            else:
                require_live(data, now)
            common = {
                "cluster_id": data["cluster_id"],
                "owner": data["owner"],
                "task_id": data["task_id"],
                "purpose": data["purpose"],
                "token_sha256": data["token_sha256"],
                "lease_generation": data.get(
                    "lease_generation", "legacy-" + data["token_sha256"][:32]
                ),
                "acquired_at": data["created_at"],
                "last_heartbeat_at": data["heartbeat_at"],
            }
            if stale_retirement:
                common.update({
                    "stale_retirement": True,
                    "stale_retirement_proof": proof_path,
                    "stale_retirement_proof_sha256": proof_sha256,
                })
            intent_path, intent_sha256 = append_release_record(
                args.root, operation_id, "intent", {**common, "authorized_at": iso(now)}
            )
        maybe_fault(args, "after-intent")

        tombstone = tombstone_path(args.root, operation_id)
        if args.root.exists():
            if tombstone.exists():
                raise LockError(f"release tombstone already exists: {tombstone}")
            os.replace(args.root, tombstone)
        elif not tombstone.exists() and not read_release_record(args.root, operation_id, "commit"):
            raise LockError("UNKNOWN release state: active root and tombstone are both absent")
        maybe_fault(args, "after-rename")

        commit = read_release_record(args.root, operation_id, "commit")
        if not commit:
            append_release_record(
                args.root,
                operation_id,
                "commit",
                {
                    **common,
                    "intent_path": str(intent_path),
                    "intent_sha256": intent_sha256,
                    "committed_at": iso(utc_now()),
                    "tombstone_path": str(tombstone),
                },
            )
        maybe_fault(args, "after-commit")

        if tombstone.exists():
            shutil.rmtree(tombstone)
        maybe_fault(args, "after-tombstone-cleanup")

        commit_path = release_record_path(args.root, operation_id, "commit")
        completion_path, completion_sha256 = append_release_record(
            args.root,
            operation_id,
            "completion",
            {
                **common,
                "released_at": iso(utc_now()),
                "terminal_state": "UNLOCKED",
                "intent_path": str(intent_path),
                "intent_sha256": intent_sha256,
                "commit_path": str(commit_path),
                "commit_sha256": hashlib.sha256(commit_path.read_bytes()).hexdigest(),
            },
        )
        maybe_fault(args, "after-completion")
        args.token_file.unlink(missing_ok=True)
        maybe_fault(args, "after-token-cleanup")
        return {
            "state": "UNLOCKED",
            "released_owner": args.owner,
            "release_operation_id": operation_id,
            "release_receipt": str(completion_path),
            "release_receipt_sha256": completion_sha256,
        }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    sub = result.add_subparsers(dest="command", required=True)
    acquire = sub.add_parser("acquire")
    acquire.add_argument("--owner", required=True)
    acquire.add_argument("--task-id", required=True)
    acquire.add_argument("--purpose", required=True)
    acquire.add_argument("--lease-seconds", type=int, default=1800, choices=range(60, 86401))
    acquire.add_argument("--token-file", type=Path, required=True)
    for name in ("guard", "heartbeat"):
        command = sub.add_parser(name)
        command.add_argument("--owner", required=True)
        command.add_argument("--token-file", type=Path, required=True)
    release = sub.add_parser("release")
    release.add_argument("--owner", required=True)
    release.add_argument("--token-file", type=Path, required=True)
    release.add_argument("--operation-id")
    release.add_argument(
        "--stale-retirement-proof",
        type=Path,
        help="explicit original-capability stale retirement with a bound positive safe-state proof",
    )
    release.add_argument(
        "--stale-retirement-verifier-key",
        type=Path,
        help="mode-0600 key used by the trusted live-state verifier",
    )
    release.add_argument(
        "--test-fault-after",
        choices=(
            "after-intent", "after-rename", "after-commit", "after-tombstone-cleanup",
            "after-completion", "after-token-cleanup",
        ),
        help=argparse.SUPPRESS,
    )
    sub.add_parser("status")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        handler = globals()[f"cmd_{args.command}"]
        print(json.dumps(handler(args), indent=2, sort_keys=True))
        return 0
    except LockError as exc:
        print(f"deployment-lock: {exc}", file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
