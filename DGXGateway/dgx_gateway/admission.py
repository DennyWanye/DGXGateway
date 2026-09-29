from __future__ import annotations

import hashlib
import json
import os
import fcntl
import base64
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
import argparse
import secrets
from pathlib import Path

from .ssh import FixedSSH


REMOTE_AUTHORITY_READ = r'''
import json, os, pathlib, pwd, socket, sys
expected_host, expected_user, path_text = sys.argv[1:]
if socket.gethostname() != expected_host or pwd.getpwuid(os.getuid()).pw_name != expected_user:
    raise SystemExit(75)
path = pathlib.Path(path_text)
if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
    raise SystemExit(76)
print(path.read_text())
'''


REMOTE_AUTHORITY_CAS = r'''
import base64, fcntl, json, os, pathlib, pwd, socket, sys, tempfile
expected_host, expected_user, path_text, expected_epoch, expected_id, expected_phase, payload64 = sys.argv[1:]
if socket.gethostname() != expected_host or pwd.getpwuid(os.getuid()).pw_name != expected_user:
    raise SystemExit(75)
path = pathlib.Path(path_text)
payload = json.loads(base64.b64decode(payload64).decode())
path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(path.parent, 0o700)
lock_path = path.parent / '.authority-cas.lock'
with lock_path.open('a+') as lock_stream:
    os.chmod(lock_path, 0o600)
    fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise SystemExit(76)
        current = json.loads(path.read_text())
        if int(expected_epoch) == 0 and current == payload:
            print(json.dumps(current, sort_keys=True)); raise SystemExit(0)
        if (int(current.get('epoch', 0)), current.get('active_authority_id'), current.get('phase')) != (int(expected_epoch), expected_id, expected_phase):
            raise SystemExit(77)
    elif int(expected_epoch) != 0:
        raise SystemExit(78)
    fd, temporary = tempfile.mkstemp(prefix='.authority.', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(payload, stream, sort_keys=True, indent=2); stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory_fd)
        finally: os.close(directory_fd)
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass
print(json.dumps(payload, sort_keys=True))
'''


class AdmissionError(RuntimeError):
    pass


class MutationAdmission:
    SCHEMA = "dgx-gateway-authority-v1"
    HANDOFF_SCHEMA = "dgx-gateway-authority-handoff-v1"

    def __init__(self, config):
        self.config = config
        self.epoch_path = config.state_dir / "authority-epoch.json"

    def _remote_record(self) -> dict:
        if not self.config.authority_remote_fence_enabled:
            return {
                "schema": "dgx-gateway-remote-authority-v1",
                "cluster_id": "dgx-head+dgx-worker",
                "epoch": None,
                "phase": "TEST_DISABLED",
            }
        head = next(node for node in self.config.nodes if node.role == "head")
        result = FixedSSH().run(
            head,
            ["python3", "-c", REMOTE_AUTHORITY_READ, head.hostname, head.user, str(self.config.authority_remote_fence_path)],
            timeout=10,
        )
        if result.returncode:
            raise AdmissionError("remote authority fence is unavailable")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise AdmissionError("remote authority fence is invalid") from exc

    @staticmethod
    def _read_secret(path: Path) -> str:
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise AdmissionError("authority credential is missing or unsafe")
        value = path.read_text(encoding="utf-8").strip()
        if len(value) < 32:
            raise AdmissionError("authority credential is invalid")
        return value

    @staticmethod
    def _safe_digest(path: Path) -> str:
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o022:
            raise AdmissionError("authority handoff artifact is missing or unsafe")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def inspect(self) -> dict:
        if not self.config.mutation_enabled:
            return {"allowed": False, "reason": "mutation_disabled"}
        try:
            if (
                self.config.authority_record.is_symlink()
                or not self.config.authority_record.is_file()
                or self.config.authority_record.stat().st_mode & 0o077
            ):
                raise AdmissionError("authority record is missing or unsafe")
            record = json.loads(self.config.authority_record.read_text(encoding="utf-8"))
            credential = self._read_secret(self.config.authority_credential)
            if record.get("schema") != self.SCHEMA:
                raise AdmissionError("authority schema mismatch")
            if record.get("cluster_id") != "dgx-head+dgx-worker":
                raise AdmissionError("authority cluster mismatch")
            if record.get("phase") != "ACTIVE":
                raise AdmissionError("authority is disabled")
            if record.get("active_authority_id") != self.config.authority_id:
                raise AdmissionError("authority id mismatch")
            expected = hashlib.sha256(credential.encode()).hexdigest()
            if record.get("credential_sha256") != expected:
                raise AdmissionError("authority credential mismatch")
            if self.config.authority_id.startswith("agentos-"):
                self._verify_handoff(record)
            epoch = int(record.get("epoch", 0))
            if epoch < 1:
                raise AdmissionError("authority epoch is invalid")
            previous = 0
            if self.epoch_path.exists():
                previous = int(json.loads(self.epoch_path.read_text(encoding="utf-8"))["epoch"])
            if epoch < previous:
                raise AdmissionError("authority epoch rollback detected")
            if self.config.authority_remote_fence_enabled:
                remote = self._remote_record()
                if (
                    remote.get("schema") != "dgx-gateway-remote-authority-v1"
                    or remote.get("cluster_id") != "dgx-head+dgx-worker"
                    or remote.get("phase") != "ACTIVE"
                    or remote.get("epoch") != epoch
                    or remote.get("active_authority_id") != self.config.authority_id
                    or remote.get("credential_sha256") != expected
                ):
                    raise AdmissionError("remote single-writer authority fence mismatch")
            return {"allowed": True, "authority_id": self.config.authority_id, "epoch": epoch}
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise AdmissionError("authority admission unavailable") from exc

    def _verify_handoff(self, record: dict) -> None:
        handoff_path = self.config.authority_record.parent / "handoff.json"
        if handoff_path.is_symlink() or not handoff_path.is_file() or handoff_path.stat().st_mode & 0o077:
            raise AdmissionError("AgentOS authority handoff receipt is missing or unsafe")
        handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
        required = {
            "schema", "cluster_id", "from_authority_id", "to_authority_id",
            "disabled_authority_record_sha256", "prior_release_completion_sha256",
            "export_archive_sha256", "authority_epoch", "chain_sha256",
        }
        if set(handoff) != required or handoff["schema"] != self.HANDOFF_SCHEMA:
            raise AdmissionError("AgentOS authority handoff schema mismatch")
        if handoff["cluster_id"] != "dgx-head+dgx-worker":
            raise AdmissionError("AgentOS authority handoff cluster mismatch")
        if handoff["to_authority_id"] != self.config.authority_id:
            raise AdmissionError("AgentOS authority handoff target mismatch")
        if handoff["from_authority_id"] != "mac-dgx-gateway":
            raise AdmissionError("AgentOS authority handoff source mismatch")
        if int(handoff["authority_epoch"]) != int(record.get("epoch", 0)):
            raise AdmissionError("AgentOS authority handoff epoch mismatch")
        if record.get("handoff_chain_sha256") != handoff.get("chain_sha256"):
            raise AdmissionError("active authority record is not bound to the handoff chain")
        for key in (
            "disabled_authority_record_sha256", "prior_release_completion_sha256",
            "export_archive_sha256",
            "chain_sha256",
        ):
            value = handoff.get(key)
            if not isinstance(value, str) or len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
                raise AdmissionError("AgentOS authority handoff digest is invalid")
        canonical = json.dumps(
            {key: handoff[key] for key in sorted(handoff) if key != "chain_sha256"},
            sort_keys=True, separators=(",", ":"),
        ).encode()
        if not secrets.compare_digest(handoff["chain_sha256"], hashlib.sha256(canonical).hexdigest()):
            raise AdmissionError("AgentOS authority handoff chain mismatch")
        root = self.config.authority_record.parent
        artifacts = {
            "disabled_authority_record_sha256": root / "disabled-authority.json",
            "prior_release_completion_sha256": root / "prior-release-completion.json",
            "export_archive_sha256": root / "state-export.tar.gz",
        }
        for field, artifact in artifacts.items():
            if not secrets.compare_digest(handoff[field], self._safe_digest(artifact)):
                raise AdmissionError("AgentOS authority handoff artifact digest mismatch")
        disabled = json.loads((root / "disabled-authority.json").read_text(encoding="utf-8"))
        if (
            disabled.get("phase") != "DISABLED"
            or disabled.get("active_authority_id") != "mac-dgx-gateway"
            or int(disabled.get("epoch", 0)) + 1 != int(record.get("epoch", 0))
        ):
            raise AdmissionError("old Mac authority disable record is invalid")
        completion = json.loads((root / "prior-release-completion.json").read_text(encoding="utf-8"))
        if completion.get("terminal_state") != "UNLOCKED":
            raise AdmissionError("prior release completion is not terminal UNLOCKED")

    def assert_allowed(self) -> dict:
        result = self.inspect()
        if not result.get("allowed"):
            raise AdmissionError(str(result.get("reason", "mutation denied")))
        self.epoch_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"epoch": result["epoch"]}, sort_keys=True) + "\n"
        descriptor, name = tempfile.mkstemp(prefix=".authority-epoch.", dir=self.epoch_path.parent)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.epoch_path)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass
        return result


def _atomic_json(path: Path, value: dict) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _atomic_copy(source: Path, target: Path) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with source.open("rb") as input_stream, os.fdopen(descriptor, "wb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, target)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _remote_cas(config, expected: dict | None, payload: dict) -> None:
    if not config.authority_remote_fence_enabled:
        return
    head = next(node for node in config.nodes if node.role == "head")
    encoded = base64.b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
    result = FixedSSH().run(
        head,
        [
            "python3", "-c", REMOTE_AUTHORITY_CAS, head.hostname, head.user,
            str(config.authority_remote_fence_path),
            str(expected.get("epoch", 0) if expected else 0),
            str(expected.get("active_authority_id", "") if expected else ""),
            str(expected.get("phase", "") if expected else ""),
            encoded,
        ],
        timeout=15,
    )
    if result.returncode:
        raise AdmissionError("remote authority compare-and-swap failed")


def bootstrap_remote_authority(config, lock_token: Path) -> dict:
    if config.authority_id != "mac-dgx-gateway":
        raise AdmissionError("only the Mac authority may bootstrap the remote fence")
    if not config.authority_remote_fence_enabled:
        raise AdmissionError("remote authority fence is disabled")
    guard = subprocess.run(
        [str(config.lock_cli), "--root", str(config.lock_root), "guard", "--owner", "dgx-cluster-gateway", "--token-file", str(lock_token)],
        text=True, capture_output=True, timeout=10,
    )
    if guard.returncode:
        raise AdmissionError("remote authority bootstrap requires the active Gateway lock capability")
    record = json.loads(config.authority_record.read_text(encoding="utf-8"))
    credential = MutationAdmission._read_secret(config.authority_credential)
    payload = {
        "schema": "dgx-gateway-remote-authority-v1",
        "cluster_id": "dgx-head+dgx-worker",
        "epoch": int(record["epoch"]),
        "phase": "ACTIVE",
        "active_authority_id": config.authority_id,
        "credential_sha256": hashlib.sha256(credential.encode()).hexdigest(),
    }
    _remote_cas(config, None, payload)
    return {"remote_fence": "ACTIVE", "epoch": payload["epoch"], "authority_id": config.authority_id}


def activate_agentos_handoff(
    config, release_completion: Path, export_archive: Path, agentos_credential: Path,
    *, lease_factory=None,
) -> dict:
    """Atomically fence the Mac writer before activating AgentOS in the same authority root."""
    if config.authority_id != "mac-dgx-gateway":
        raise AdmissionError("handoff activation must run from the active Mac authority")
    root = config.authority_record.parent
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    from .authority import GatewayLease
    from .maintenance import verify_archive
    from .store import OperationStore

    # This transaction linearizes against operation creation. Any request that
    # passed admission but has not created its ledger row will be rejected by
    # create_operation; any existing row makes the handoff fail closed.
    store = OperationStore(config.state_dir / "gateway.sqlite3")
    try:
        current_operation = store.set_mutation_fence(True)
    finally:
        store.close()
    if current_operation is not None:
        raise AdmissionError("authority handoff refused because a model operation is active")
    local_record = json.loads(config.authority_record.read_text(encoding="utf-8"))
    if local_record.get("phase") == "ACTIVE" and local_record.get("active_authority_id") == "mac-dgx-gateway":
        resuming = False
        current = local_record
        disabled_existing = None
        operation_id = f"authority-handoff-{int(current['epoch']) + 1}"
    elif local_record.get("phase") == "DISABLED" and local_record.get("disabled_for") == "agentos-dgx-gateway":
        resuming = True
        disabled_existing = local_record
        current = {
            key: value for key, value in local_record.items()
            if key not in {"disabled_for", "handoff_lease_generation"}
        }
        current["epoch"] = int(local_record["epoch"]) - 1
        current["phase"] = "ACTIVE"
        operation_id = f"authority-handoff-{int(local_record['epoch'])}"
    elif local_record.get("phase") == "ACTIVE" and local_record.get("active_authority_id") == "agentos-dgx-gateway":
        resuming = True
        disabled_path = root / "disabled-authority.json"
        if not disabled_path.is_file():
            raise AdmissionError("handoff resume is missing the disabled Mac record")
        disabled_existing = json.loads(disabled_path.read_text(encoding="utf-8"))
        current = {
            key: value for key, value in disabled_existing.items()
            if key not in {"disabled_for", "handoff_lease_generation"}
        }
        current["epoch"] = int(disabled_existing["epoch"]) - 1
        current["phase"] = "ACTIVE"
        operation_id = f"authority-handoff-{int(disabled_existing['epoch'])}"
    else:
        raise AdmissionError("local authority state cannot start or resume an AgentOS handoff")
    handoff_state_path = config.state_dir / "authority-handoff-operations" / f"{operation_id}.json"

    def persist_handoff(phase: str, **fields) -> None:
        handoff_state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        previous = {}
        if handoff_state_path.exists():
            previous = json.loads(handoff_state_path.read_text(encoding="utf-8"))
        _atomic_json(handoff_state_path, {
            **previous,
            "schema": "dgx-gateway-authority-handoff-operation-v1",
            "operation_id": operation_id,
            "phase": phase,
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            **fields,
        })

    if resuming:
        persist_handoff(
            "RESUME_PREFLIGHT", outcome="RECOVERY_REQUIRED", mac_fenced=True,
        )
    else:
        persist_handoff("PREFLIGHT", outcome=None, mac_fenced=False)
    lease_operation_id = f"{operation_id}-attempt-{secrets.token_hex(6)}"
    lease = lease_factory(lease_operation_id) if lease_factory else GatewayLease(
        lock_cli=config.lock_cli,
        lock_root=config.lock_root,
        token_dir=config.token_dir,
        operation_id=lease_operation_id,
        purpose="exclusive Mac to AgentOS authority handoff",
    )
    acquired = None
    body_succeeded = False
    mac_fenced = resuming
    try:
        try:
            acquired = lease.acquire()
        except BaseException as acquire_error:
            if resuming:
                persist_handoff(
                    "RECOVERY_REQUIRED", outcome="RECOVERY_REQUIRED",
                    mac_fenced=True, attempt_phase="ACQUIRE_FAILED",
                    acquire_error_type=type(acquire_error).__name__,
                )
            else:
                persist_handoff(
                    "ACQUIRE_FAILED", outcome="FAILED", mac_fenced=False,
                    acquire_error_type=type(acquire_error).__name__,
                )
            raise
        persist_handoff(
            "LEASE_ACQUIRED",
            lease_generation=acquired["lease_generation"],
            lease_operation_id=lease_operation_id,
            token_file=str(lease.token_path),
        )
        lease.start_heartbeat()
        mutex_path = root / ".handoff.lock"
        with mutex_path.open("a+", encoding="utf-8") as mutex:
            os.chmod(mutex_path, 0o600)
            fcntl.flock(mutex.fileno(), fcntl.LOCK_EX)
            if disabled_existing is None:
                MutationAdmission(config).assert_allowed()
            verify_archive(export_archive)
            completion = json.loads(release_completion.read_text(encoding="utf-8"))
            if completion.get("terminal_state") != "UNLOCKED" or completion.get("cluster_id") != "dgx-head+dgx-worker":
                raise AdmissionError("release completion is not a terminal receipt for this cluster")
            new_credential = MutationAdmission._read_secret(agentos_credential)
            lease.check_supervision()
            disabled = disabled_existing or {
                **current,
                "epoch": int(current["epoch"]) + 1,
                "phase": "DISABLED",
                "disabled_for": "agentos-dgx-gateway",
                "handoff_lease_generation": acquired["lease_generation"],
            }
            remote_current = {
                "epoch": int(current["epoch"]),
                "phase": "ACTIVE",
                "active_authority_id": "mac-dgx-gateway",
            }
            remote_disabled = {
                "schema": "dgx-gateway-remote-authority-v1",
                "cluster_id": "dgx-head+dgx-worker",
                "epoch": disabled["epoch"],
                "phase": "DISABLED",
                "active_authority_id": "mac-dgx-gateway",
                "credential_sha256": current["credential_sha256"],
                "handoff_lease_generation": acquired["lease_generation"],
            }
            remote_observed = (
                MutationAdmission(config)._remote_record()
                if config.authority_remote_fence_enabled else remote_current
            )
            remote_tuple = (
                remote_observed.get("epoch"), remote_observed.get("active_authority_id"),
                remote_observed.get("phase"),
            )
            current_tuple = (
                remote_current["epoch"], remote_current["active_authority_id"],
                remote_current["phase"],
            )
            disabled_tuple = (
                remote_disabled["epoch"], remote_disabled["active_authority_id"],
                remote_disabled["phase"],
            )
            target_tuple = (
                disabled["epoch"] + 1, "agentos-dgx-gateway", "ACTIVE",
            )
            if remote_tuple == current_tuple:
                # This remote CAS is the global fencing point, including copied roots.
                _remote_cas(config, remote_current, remote_disabled)
                mac_fenced = True
                persist_handoff("MAC_FENCED", outcome="RECOVERY_REQUIRED")
                remote_observed = remote_disabled
            elif remote_tuple == disabled_tuple:
                mac_fenced = True
                persist_handoff("MAC_FENCED", outcome="RECOVERY_REQUIRED")
            elif remote_tuple == target_tuple:
                mac_fenced = True
                persist_handoff("AGENTOS_REMOTE_ACTIVE", outcome="RECOVERY_REQUIRED")
            else:
                raise AdmissionError("remote authority state cannot resume this handoff")
            if local_record.get("active_authority_id") != "agentos-dgx-gateway":
                _atomic_json(config.authority_record, disabled)
            disabled_path = root / "disabled-authority.json"
            _atomic_json(disabled_path, disabled)
            completion_path = root / "prior-release-completion.json"
            export_path = root / "state-export.tar.gz"
            _atomic_copy(release_completion, completion_path)
            _atomic_copy(export_archive, export_path)
            handoff = {
                "schema": MutationAdmission.HANDOFF_SCHEMA,
                "cluster_id": "dgx-head+dgx-worker",
                "from_authority_id": "mac-dgx-gateway",
                "to_authority_id": "agentos-dgx-gateway",
                "disabled_authority_record_sha256": MutationAdmission._safe_digest(disabled_path),
                "prior_release_completion_sha256": MutationAdmission._safe_digest(completion_path),
                "export_archive_sha256": MutationAdmission._safe_digest(export_path),
                "authority_epoch": disabled["epoch"] + 1,
            }
            canonical = json.dumps(handoff, sort_keys=True, separators=(",", ":")).encode()
            handoff["chain_sha256"] = hashlib.sha256(canonical).hexdigest()
            _atomic_json(root / "handoff.json", handoff)
            active = {
                "schema": MutationAdmission.SCHEMA,
                "cluster_id": "dgx-head+dgx-worker",
                "epoch": handoff["authority_epoch"],
                "phase": "ACTIVE",
                "active_authority_id": "agentos-dgx-gateway",
                "credential_sha256": hashlib.sha256(new_credential.encode()).hexdigest(),
                "handoff_chain_sha256": handoff["chain_sha256"],
                "handoff_lease_generation": acquired["lease_generation"],
            }
            remote_active = {
                "schema": "dgx-gateway-remote-authority-v1",
                "cluster_id": "dgx-head+dgx-worker",
                "epoch": active["epoch"],
                "phase": "ACTIVE",
                "active_authority_id": "agentos-dgx-gateway",
                "credential_sha256": active["credential_sha256"],
                "handoff_lease_generation": acquired["lease_generation"],
            }
            lease.check_supervision()
            remote_observed = (
                MutationAdmission(config)._remote_record()
                if config.authority_remote_fence_enabled else remote_disabled
            )
            if (
                remote_observed.get("epoch"), remote_observed.get("active_authority_id"),
                remote_observed.get("phase"),
            ) == disabled_tuple:
                _remote_cas(config, remote_disabled, remote_active)
            elif (
                remote_observed.get("epoch"), remote_observed.get("active_authority_id"),
                remote_observed.get("phase"),
            ) != target_tuple:
                raise AdmissionError("remote authority changed during handoff completion")
            _atomic_json(config.authority_record, active)
            reconciled_remote = (
                MutationAdmission(config)._remote_record()
                if config.authority_remote_fence_enabled else remote_active
            )
            reconciled_local = json.loads(config.authority_record.read_text(encoding="utf-8"))
            if not (
                reconciled_remote.get("epoch") == reconciled_local.get("epoch") == active["epoch"]
                and reconciled_remote.get("active_authority_id")
                == reconciled_local.get("active_authority_id")
                == "agentos-dgx-gateway"
                and reconciled_remote.get("credential_sha256")
                == reconciled_local.get("credential_sha256")
                == active["credential_sha256"]
            ):
                raise AdmissionError("AgentOS authority reconciliation failed")
            body_succeeded = True
        persist_handoff(
            "AUTHORITY_COMMITTED",
            outcome="COMMITTED",
            authority_epoch=active["epoch"],
            handoff_chain_sha256=handoff["chain_sha256"],
        )
        return {"active_authority_id": "agentos-dgx-gateway", "epoch": active["epoch"], "chain_sha256": handoff["chain_sha256"]}
    finally:
        if acquired is not None:
            try:
                release = lease.release()
            except BaseException as release_error:
                lease.stop_heartbeat()
                persist_handoff(
                    "RECOVERY_REQUIRED",
                    outcome="COMMITTED" if body_succeeded else "RECOVERY_REQUIRED",
                    error="lease_release_failed",
                    release_error_type=type(release_error).__name__,
                )
                raise
            else:
                release_public = {
                        key: release[key]
                        for key in (
                            "state", "release_operation_id", "release_receipt",
                            "release_receipt_sha256",
                        )
                        if key in release
                    }
                if body_succeeded:
                    persist_handoff("RELEASED", outcome="COMMITTED", release=release_public)
                elif mac_fenced:
                    persist_handoff(
                        "RECOVERY_REQUIRED", outcome="RECOVERY_REQUIRED",
                        error="partial_authority_handoff", release=release_public,
                    )
                else:
                    persist_handoff("RELEASED", outcome="FAILED", release=release_public)


def initialize(config) -> dict:
    credential_path = config.authority_credential
    record_path = config.authority_record
    credential_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not credential_path.exists():
        descriptor = os.open(credential_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(secrets.token_hex(32) + "\n")
    os.chmod(credential_path, 0o600)
    credential = MutationAdmission._read_secret(credential_path)
    if not record_path.exists():
        if (config.state_dir / "authority-epoch.json").exists():
            raise AdmissionError("refusing to re-bootstrap a previously admitted authority")
        record = {
            "schema": MutationAdmission.SCHEMA,
            "cluster_id": "dgx-head+dgx-worker",
            "epoch": 1,
            "phase": "ACTIVE",
            "active_authority_id": config.authority_id,
            "credential_sha256": hashlib.sha256(credential.encode()).hexdigest(),
        }
        record_path.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.chmod(record_path, 0o600)
    return MutationAdmission(config).assert_allowed()


def main() -> None:
    from .config import load_config

    parser = argparse.ArgumentParser(description="DGX Gateway mutation admission")
    parser.add_argument("command", choices=("init", "check", "bootstrap-remote", "activate-agentos"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--release-completion", type=Path)
    parser.add_argument("--export-archive", type=Path)
    parser.add_argument("--agentos-credential", type=Path)
    parser.add_argument("--lock-token", type=Path)
    args = parser.parse_args()
    config = load_config(args.config)
    if args.command == "init":
        result = initialize(config)
    elif args.command == "check":
        result = MutationAdmission(config).assert_allowed()
    elif args.command == "bootstrap-remote":
        if not args.lock_token:
            parser.error("bootstrap-remote requires --lock-token")
        result = bootstrap_remote_authority(config, args.lock_token)
    else:
        if not all((args.release_completion, args.export_archive, args.agentos_credential)):
            parser.error("activate-agentos requires release completion, export archive and AgentOS credential")
        result = activate_agentos_handoff(
            config, args.release_completion, args.export_archive, args.agentos_credential
        )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
