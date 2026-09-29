from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import re
import shutil
import sqlite3
import tarfile
import tempfile
from pathlib import Path

from .config import load_config
from .store import OperationStore


FORMAT = "dgx-gateway-export-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def export_state(config_path: Path, output: Path) -> dict:
    config = load_config(config_path)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dgx-gateway-export-") as directory:
        root = Path(directory) / "dgx-gateway-export"
        root.mkdir()
        database = root / "gateway.sqlite3"
        source = sqlite3.connect(config.state_dir / "gateway.sqlite3")
        destination = sqlite3.connect(database)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        shutil.copy2(config_path, root / "gateway.toml")
        files = {
            "gateway.sqlite3": _sha256(database),
            "gateway.toml": _sha256(root / "gateway.toml"),
        }
        manifest = {
            "format": FORMAT,
            "schema_version": OperationStore.SCHEMA_VERSION,
            "files": files,
        }
        (root / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        with tarfile.open(output, "w:gz") as archive:
            archive.add(root, arcname=root.name)
    return {"archive": str(output), "sha256": _sha256(output), **manifest}


def _safe_extract(archive_path: Path, directory: Path) -> Path:
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        expected_prefix = "dgx-gateway-export/"
        for member in members:
            if member.issym() or member.islnk() or member.name.startswith("/"):
                raise ValueError("unsafe archive member")
            if member.name != "dgx-gateway-export" and not member.name.startswith(expected_prefix):
                raise ValueError("unexpected archive layout")
            target = (directory / member.name).resolve()
            if directory.resolve() not in target.parents and target != directory.resolve():
                raise ValueError("archive path traversal")
        archive.extractall(directory, filter="data")
    return directory / "dgx-gateway-export"


def verify_archive(archive_path: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="dgx-gateway-verify-") as directory:
        root = _safe_extract(archive_path.resolve(), Path(directory))
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("format") != FORMAT:
            raise ValueError("unsupported export format")
        if manifest.get("schema_version") != OperationStore.SCHEMA_VERSION:
            raise ValueError("unsupported schema version")
        if set(manifest.get("files", {})) != {"gateway.sqlite3", "gateway.toml"}:
            raise ValueError("unexpected export files")
        for name, expected in manifest["files"].items():
            if _sha256(root / name) != expected:
                raise ValueError(f"checksum mismatch: {name}")
        from .config import load_config

        archived_config = load_config(root / "gateway.toml")
        if tuple(node.hostname for node in archived_config.nodes) != ("dgx-head", "dgx-worker"):
            raise ValueError("archived config cluster roles are invalid")
        database = sqlite3.connect(f"file:{root / 'gateway.sqlite3'}?mode=ro", uri=True)
        try:
            result = database.execute("PRAGMA integrity_check").fetchone()[0]
            schema = database.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            tables = {
                row[0]
                for row in database.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            triggers = {
                row[0]
                for row in database.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            }
        finally:
            database.close()
        if result != "ok":
            raise ValueError("SQLite integrity check failed")
        if schema != (str(OperationStore.SCHEMA_VERSION),):
            raise ValueError("SQLite metadata schema mismatch")
        if not {"metadata", "operations", "events"}.issubset(tables):
            raise ValueError("SQLite schema closure is incomplete")
        if "operation_outcome_immutable" not in triggers:
            raise ValueError("SQLite outcome immutability trigger is missing")
        return {"valid": True, "archive_sha256": _sha256(archive_path), **manifest}


def import_state(config_path: Path, archive_path: Path) -> dict:
    verification = verify_archive(archive_path)
    config = load_config(config_path)
    database = config.state_dir / "gateway.sqlite3"
    if database.exists():
        raise FileExistsError("refusing to overwrite existing Gateway state")
    config.state_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dgx-gateway-import-") as directory:
        root = _safe_extract(archive_path.resolve(), Path(directory))
        temporary = config.state_dir / ".gateway.sqlite3.importing"
        shutil.copy2(root / "gateway.sqlite3", temporary)
        temporary.replace(database)
    return {"imported": str(database), **verification}


@contextmanager
def creative_admission_lock(config):
    """Serialize cross-process maintenance with durable creative admission."""
    config.state_dir.mkdir(parents=True, exist_ok=True)
    with (config.state_dir / "creative-admission.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@contextmanager
def creative_admission(config):
    """Hold through initial QUEUED persistence, before starting its worker."""
    with creative_admission_lock(config):
        database = sqlite3.connect(f"file:{config.state_dir / 'gateway.sqlite3'}?mode=ro", uri=True)
        try:
            row = database.execute("SELECT value FROM metadata WHERE key='mutation_fenced'").fetchone()
        finally:
            database.close()
        if row is not None and row[0] == "1":
            raise BlockingIOError("Gateway mutation admission is quiesced for maintenance")
        yield


def _settled_video_requests(job, *, only_unsubmitted=False, allow_started=False):
    """Prove the local submission loop is idle, not that remote work has ended."""
    shots = job.get("shots")
    if not isinstance(shots, list) or not shots:
        return False
    phases = {"NOT_SUBMITTED": {"QUEUED", "FAILED", "CANCELED"}}
    if not only_unsubmitted:
        phases.update({"UNKNOWN": {"SUBMISSION_UNKNOWN"}, "RECEIVED": {"SUCCEEDED", "FAILED"},
                       "REJECTED": {"FAILED"}})
        if allow_started:
            phases["REQUEST_STARTED"] = {"SUBMISSION_UNKNOWN"}
    for shot in shots:
        if not isinstance(shot, dict):
            return False
        request_id = shot.get("request_id")
        if (not isinstance(request_id, str) or len(request_id) != 32
                or any(character not in "0123456789abcdef" for character in request_id)):
            return False
        phase, state = shot.get("submission_phase"), shot.get("state")
        if not isinstance(phase, str) or not isinstance(state, str) or state not in phases.get(phase, set()):
            return False
    return True


def _active_creative_jobs(config, *, recovery_restart=False):
    active = []
    paths = list((config.state_dir / "creative-jobs").glob("*.json"))
    paths.extend((config.state_dir / "video-jobs").glob("*.json"))
    paths.extend((config.state_dir / "media-jobs").glob("*.json"))
    paths.extend((config.state_dir / "avatar-jobs").glob("*.json"))
    for path in paths:
        sidecar = re.fullmatch(r"([a-f0-9]{32})\.cooperative-cancel\.json", path.name)
        if path.parent == config.state_dir / "video-jobs" and sidecar:
            # Cancellation acknowledgements are evidence, not ownership journals.
            # Their original job is still checked below, including active state.
            parent = path.with_name(sidecar[1] + ".json")
            if path.is_symlink() or parent.is_symlink() or not parent.is_file():
                raise RuntimeError(f"video cancellation evidence has no regular parent job: {path.name}")
            continue
        try:
            job = json.loads(path.read_text())
            if not isinstance(job, dict) or not isinstance(job.get("state"), str):
                raise ValueError("invalid job document")
        except (ValueError, OSError) as exc:
            raise RuntimeError(f"creative job state unreadable: {path.name}") from exc
        if path.parent == config.state_dir / "video-jobs":
            never_acquired = (
                job.get("resource_not_acquired") is True
                or (not job.get("start_operation_id") and "resource_not_acquired" not in job)
            )
            if (job["state"] == "BLOCKED" and job.get("cleanup_required") is False
                    and job.get("memory_released") is True and never_acquired
                    and _settled_video_requests(job, only_unsubmitted=True)):
                continue
            if (recovery_restart and job["state"] == "SUBMISSION_UNKNOWN"
                    and job.get("cleanup_required") is True and _settled_video_requests(job)):
                # Reload only the control plane. Preserve the request journal,
                # UNKNOWN result and cleanup owner for explicit reconciliation.
                continue
            if (recovery_restart and job["state"] == "SUBMISSION_UNKNOWN"
                    and job.get("runtime_model") == "h3-ring4"
                    and job.get("execution_mode") == "ring4_four_node"
                    and job.get("cleanup_required") is False
                    and _settled_video_requests(job, allow_started=True)
                    and path.with_suffix(".cancel").is_file()):
                # A durable user cancellation is itself an explicit recovery
                # boundary: reload the control plane so the Ring4 hard-cancel
                # handler can run, but never infer cancellation for a live or
                # partially submitted journal.
                continue
            if (recovery_restart and job["state"] == "SUBMISSION_UNKNOWN"
                    and job.get("runtime_model") == "h3-ring4"
                    and job.get("execution_mode") == "ring4_four_node"
                    and job.get("cleanup_required") is False
                    and _settled_video_requests(job)
                    and all(shot.get("artifact") or (
                        shot.get("submission_phase") == "UNKNOWN"
                        and isinstance(shot.get("ring_job_id"), str)
                        and re.fullmatch(r"[A-Za-z0-9._:-]{8,80}", shot["ring_job_id"])
                    ) for shot in job["shots"])):
                # Reload an idle observer while the known remote job computes.
                # No model stop or inference replay is authorized by this fence.
                continue
        if job["state"] not in {"SUCCEEDED", "FAILED", "INTERRUPTED", "CANCELED"} or job.get("cleanup_required"):
            if recovery_restart and job["state"] in {"FAILED", "INTERRUPTED", "CANCELED"} and job.get("cleanup_required"):
                continue
            active.append(job.get("job_id", path.stem))
    return active


def set_mutation_fence(config_path: Path, enabled: bool, *, allow_creative_recovery: bool = False, recovery_restart: bool = False) -> dict:
    """Atomically fence operations and refuse to abandon creative ownership."""
    config = load_config(config_path)
    with creative_admission_lock(config):
        if enabled and not allow_creative_recovery:
            active_jobs = _active_creative_jobs(config, recovery_restart=recovery_restart)
            if active_jobs:
                # Do not fence a live job's later stop operation on refusal.
                raise RuntimeError(f"creative job {active_jobs[0]} is active or requires reconciliation")
        store = OperationStore(config.state_dir / "gateway.sqlite3")
        try:
            current = store.set_mutation_fence(enabled)
        finally:
            store.close()
    if enabled and current is not None:
        raise RuntimeError(f"operation {current.operation_id} is active")
    return {"mutation_fenced": enabled, "idle": current is None}


def main() -> None:
    parser = argparse.ArgumentParser(description="DGX Gateway state maintenance")
    subparsers = parser.add_subparsers(dest="command", required=True)
    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("--config", type=Path, required=True)
    export_parser.add_argument("--output", type=Path, required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--archive", type=Path, required=True)
    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("--config", type=Path, required=True)
    import_parser.add_argument("--archive", type=Path, required=True)
    fence_parser = subparsers.add_parser("set-mutation-fence")
    fence_parser.add_argument("--config", type=Path, required=True)
    fence_parser.add_argument("--enabled", choices=("true", "false"), required=True)
    fence_parser.add_argument("--allow-creative-recovery", action="store_true", help="Only for starting an already stopped control plane to reconcile durable jobs")
    fence_parser.add_argument("--recovery-restart", action="store_true", help="Permit failed creative cleanup owners and settled UNKNOWN video journals; never live submission loops")
    args = parser.parse_args()
    if args.command == "export":
        result = export_state(args.config, args.output)
    elif args.command == "verify":
        result = verify_archive(args.archive)
    elif args.command == "import":
        result = import_state(args.config, args.archive)
    else:
        result = set_mutation_fence(args.config, args.enabled == "true", allow_creative_recovery=args.allow_creative_recovery, recovery_restart=args.recovery_restart)
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
