"""Persistent H3 batches with Ring4 as the production execution backend.

Historical dual-pair rows remain readable and are never rewritten; new rows
freeze the Ring4 runtime selected by ``runtime_target``.
"""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import stat
import threading
import tempfile
import time
import urllib.error
import urllib.request
import uuid

from .audio_assets import AudioAssets
from .job_cancel import CancelableJobs, JobCanceled
from .maintenance import creative_admission
from .image_assets import _read_regular
from .video_contracts import (gateway_generation, runtime_target, RUNTIME_MODEL, RING4_MODEL,
                              TWO_NODE_MODEL, STABLE_PROFILE, LONG_PROFILE, TEXT_PROFILE, PROFILES)
from .video_transport import LongVideoTransport
from .video_reference_assets import VideoReferenceAssets

TERMINAL = {"SUCCEEDED", "FAILED", "INTERRUPTED", "CANCELED", "BLOCKED"}
PROFILE = STABLE_PROFILE
MAX_VIDEO_BYTES = 256 * 1024 * 1024
_SHOT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")


class NoVideoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "H3 redirects are not permitted", headers, fp)


class SubmissionUnknown(Exception):
    """The sync request may still be running; neither replay nor cleanup is safe."""


def _start_owner(row, start):
    if start is None:
        return None, None
    runtime_model, runtime_profile, _task = _row_runtime(row)
    if start.target_model == runtime_model and start.target_profile == runtime_profile:
        return runtime_model, runtime_profile
    if start.target_model == "minimax-h3" and start.target_profile == row["profile"]:
        return "minimax-h3", row["profile"]
    return None, None


def _row_runtime(row):
    """Return the frozen backend, preserving old Ring4/two-node rows."""
    model = row.get("runtime_model")
    profile = row.get("runtime_profile")
    task = row.get("runtime_task")
    if model:
        return model, profile, task
    if any(shot.get("ring_job_id") for shot in row.get("shots", [])):
        _new_model, mapped_profile, mapped_task = runtime_target(row["profile"])
        return RING4_MODEL, mapped_profile, mapped_task
    if "created_at" not in row:
        _new_model, mapped_profile, mapped_task = runtime_target(row["profile"])
        return RING4_MODEL, mapped_profile, mapped_task
    # Rows created before runtime freezing used the original exact-two service.
    return TWO_NODE_MODEL, row.get("profile"), "t2va" if row.get("profile") == TEXT_PROFILE else "ref2va"


def assert_video_stop_ownership(store, row, observed, *, current_operation_id=None):
    """Prove ownership from durable start and uninterrupted lifecycle history."""
    job_id = row["job_id"]
    operations = store.list_operations(limit=10000)
    if current_operation_id is not None:
        if not operations or operations[0].operation_id != current_operation_id:
            raise BlockingIOError("Video stop is no longer the latest lifecycle operation")
        operations = [op for op in operations if op.operation_id != current_operation_id]
    start = next((op for op in operations if op.idempotency_key == job_id + "-start"), None)
    owner_model, owner_profile = _start_owner(row, start)
    if (start is None or start.operation_id != row.get("start_operation_id", start.operation_id)
            or owner_model is None
            or start.phase != "RELEASED" or start.outcome != "COMMITTED"):
        raise BlockingIOError("Video recovery has no proven committed start ownership")
    # A no-op start is ownership only with a durable, uninterrupted Gateway handoff.
    chain = start
    history = {op.operation_id: i for i, op in enumerate(operations)}
    for _ in range(len(operations) + 1):
        events = store.events(chain.operation_id)
        if not any(e["kind"] == "already-at-target" for e in events):
            break
        proofs = [e["payload"] for e in events if e["kind"] == "resident-handoff"]
        if len(proofs) != 1:
            # Ring4 is a standing service. A committed task-origin no-op start
            # still owns its accepted distributed request, whose cancellation
            # requires stopping that service. Keep the uninterrupted-history
            # and exact live-model checks below; never extend this to two-node
            # jobs or to a batch that has no remote receipt/cancel intent.
            standing_ring_cancel = (
                owner_model == RING4_MODEL and row.get("cancel_requested") is True
                and any(e["kind"] == "request-origin"
                        and e["payload"].get("kind") == "task"
                        and e["payload"].get("job_id") == job_id for e in events)
                and any(re.fullmatch(r"[A-Za-z0-9._:-]{8,80}", str(s.get("ring_job_id", "")))
                        and not s.get("artifact") for s in row.get("shots", []))
            )
            if standing_ring_cancel:
                break
            raise BlockingIOError("Video batch did not start the pre-existing H3 engine")
        proof = proofs[0]
        index = history.get(chain.operation_id, -1)
        previous = operations[index + 1] if 0 <= index < len(operations)-1 else None
        if (previous is None or previous.operation_id != proof.get("previous_operation_id")
                or previous.idempotency_key != str(proof.get("previous_job_id")) + "-start"
                or previous.target_model != owner_model or previous.target_profile != owner_profile
                or previous.phase != "RELEASED" or previous.outcome != "COMMITTED"):
            raise BlockingIOError("Video model reuse has no uninterrupted ownership chain")
        chain = previous
    else:
        raise BlockingIOError("Video ownership chain is invalid")
    latest = operations[0] if operations else None
    allowed_keys = {job_id + "-" + suffix for suffix in ("start", "stop", "cleanup", "reconcile_stop", "cancel_stop")}
    all_stopped = bool(observed) and all(item.state == "STOPPED" for item in observed)
    recovered_stop = False
    if (latest is not None and latest.phase == "RELEASED" and latest.outcome == "RECOVERY_REQUIRED"
            and latest.idempotency_key in allowed_keys - {job_id + "-start"}
            and latest.target_model is None and latest.source_model == owner_model
            and latest.source_profile == owner_profile and all_stopped
            and all(item.profile == owner_profile for item in observed if item.model_id == owner_model)):
        # Explicit safe-stop preserves the failed outcome. Only its complete
        # durable proof chain permits bookkeeping after all resources are gone.
        events = store.events(latest.operation_id)
        expected_models = {item.model_id for item in observed} | {owner_model}
        for index, event in enumerate(events):
            if event["kind"] != "recovery-safe-stop-complete" or event["payload"].get("stopped") is not True:
                continue
            proofs = [prior for prior in events[:index] if prior["payload"].get("stopped") is True
                      and re.fullmatch(r"[a-f0-9]{64}", str(prior["payload"].get("sha256", "")))]
            h3_proven = any(proof["kind"] == "recovery-stopped-proof"
                            and proof["payload"].get("model_id") == owner_model for proof in proofs)
            all_proven = {proof["payload"].get("model_id") for proof in proofs
                          if proof["kind"] == "recovery-all-stopped-proof"}
            recovered_stop = h3_proven and expected_models <= all_proven
            if recovered_stop:
                break
    if (latest is None or latest.idempotency_key not in allowed_keys or latest.phase != "RELEASED"
            or (latest.outcome != "COMMITTED" and not recovered_stop)):
        raise BlockingIOError("Video recovery is blocked by a later or unfinished lifecycle operation")
    # Checking every later operation prevents an intervening foreign owner
    # from being hidden by a subsequent same-job operation.
    for operation in operations:
        if operation.operation_id == start.operation_id:
            break
        if operation.idempotency_key not in allowed_keys:
            raise BlockingIOError("Video recovery observed an intervening external owner")
    if not observed:
        raise BlockingIOError("Video recovery cannot establish the current model state")
    owned_h3 = all((item.model_id == owner_model and item.state in {"HEALTHY", "PARTIAL"} and item.profile == owner_profile)
                   or (item.model_id != owner_model and item.state == "STOPPED") for item in observed)
    if not all_stopped and not owned_h3:
        raise BlockingIOError("Video recovery cannot stop a foreign or uncertain model")
    if owned_h3 and latest.target_model != owner_model:
        raise BlockingIOError("H3 appeared after this batch's completed stop")
    return "STOPPED" if all_stopped else "OWNED_H3"


class VideoJobs(CancelableJobs):
    cancel_boundary = "Waiting for the current H3 clip to finish; no subsequent clip will start; then releasing its pair slot"
    ring4_cancel_boundary = "已请求立即终止当前 Ring4 片段；Gateway 会撤销远端任务、停止四节点运行时并释放资源"
    def __init__(self, app):
        self.app = app
        self.root = app.config.state_dir / "video-jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.audio = AudioAssets(app.config.state_dir / "creative-audio-assets")
        self.images = app.creative_jobs.assets
        self.video_refs = VideoReferenceAssets(app.config.state_dir / "creative-video-reference-assets")
        self.active = None
        self.active_jobs = set()
        self._running = set()
        self._reconciling = set()
        self._pending_recovery = set()
        self.endpoint = f"http://{app.config.nodes[0].address}:18081/v1/videos/sync"
        self.secret_path = app.config.repo_root / "ComfyUI/secrets/h3_gateway.token"
        self.long_transport = LongVideoTransport()
        self._slot_condition = threading.Condition(threading.RLock())
        self._slots = {"pair-a": None, "pair-b": None}
        self._waiting = []
        self._runtime_condition = threading.Condition(threading.RLock())
        self._runtime_starter = None
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoVideoRedirect())
        for path in sorted(self.root.glob("*.json")):
            if not re.fullmatch(r"[a-f0-9]{32}\.json", path.name):
                continue
            try:
                row = json.loads(_read_regular(path, 2 * 1024 * 1024))
                if row.get("state") not in TERMINAL or row.get("cleanup_required"):
                    shots = row.get("shots", [])
                    resumable_pair = (
                        _row_runtime(row)[0] == RUNTIME_MODEL
                        and any(
                            shot.get("pair_job_id")
                            and shot.get("pair_id") in self._slots
                            and shot.get("submission_phase") in {"REQUEST_STARTED", "UNKNOWN", "RECEIVED"}
                            and not shot.get("artifact")
                            for shot in shots
                        )
                    )
                    resumable_ring = (
                        row.get("profile") in PROFILES
                        and any(
                            shot.get("ring_job_id")
                            and shot.get("submission_phase") in {"REQUEST_STARTED", "UNKNOWN", "RECEIVED"}
                            and not shot.get("artifact")
                            for shot in shots
                        )
                    )
                    if resumable_pair:
                        pair_ids = {
                            shot["pair_id"] for shot in shots
                            if shot.get("pair_job_id") and shot.get("pair_id") in self._slots
                            and not shot.get("artifact")
                        }
                        if len(pair_ids) != 1:
                            raise ValueError("saved dual-pair batch has inconsistent executor slots")
                        pair_id = pair_ids.pop()
                        if self._slots[pair_id] not in {None, row["job_id"]}:
                            raise ValueError("saved dual-pair executor slot has multiple owners")
                        self._slots[pair_id] = row["job_id"]
                        row["executor_slot"] = pair_id
                        for shot in shots:
                            if shot.get("submission_phase") == "REQUEST_STARTED":
                                shot.update(submission_phase="UNKNOWN", state="SUBMISSION_UNKNOWN")
                        row.update(
                            state="SUBMISSION_UNKNOWN",
                            error="Gateway restarted; original pair job id is retained for read-only reconciliation",
                            cleanup_required=False,
                            memory_released=False,
                            resource_status="remote_job_unknown",
                        )
                        self._pending_recovery.add(row["job_id"])
                        self.active = self.active or row["job_id"]
                        self.active_jobs.add(row["job_id"])
                        self.save(row)
                        continue
                    if resumable_ring:
                        for shot in shots:
                            if (not shot.get("artifact")
                                    and shot.get("submission_phase") in {"REQUEST_STARTED", "RECEIVED"}):
                                shot.update(submission_phase="UNKNOWN", state="SUBMISSION_UNKNOWN")
                        row.update(
                            state="SUBMISSION_UNKNOWN",
                            error="Gateway restarted; original Ring4 job id is retained for read-only reconciliation",
                            cleanup_required=False,
                            memory_released=False,
                        )
                        self._pending_recovery.add(row["job_id"])
                        self.active = self.active or row["job_id"]
                        self.active_jobs.add(row["job_id"])
                        self.save(row)
                        continue
                    queued = (row.get("state") == "QUEUED" and not row.get("start_operation_id") and bool(shots)
                              and all(s.get("state") == "QUEUED" and s.get("submission_phase") in {None, "NOT_SUBMITTED"}
                                      for s in shots))
                    no_start = not any(op.idempotency_key == row["job_id"] + "-start"
                                       for op in self.app.store.list_operations(limit=-1)) if queued else False
                    unstarted = queued and no_start
                    for shot in shots:
                        shot.setdefault("submission_phase", "NOT_SUBMITTED" if unstarted else "UNKNOWN")
                        if shot["submission_phase"] == "REQUEST_STARTED":
                            shot.update(submission_phase="UNKNOWN", state="SUBMISSION_UNKNOWN")
                    row.update(state="INTERRUPTED", error="Gateway restarted; query original requests before recovery",
                               cleanup_required=not unstarted, memory_released=unstarted)
                    if unstarted:
                        row["release_reason"] = "This queued batch never started a model"
                    else:
                        self._pending_recovery.add(row["job_id"])
                        self.active = self.active or row["job_id"]
                        self.active_jobs.add(row["job_id"])
                    self.save(row)
            except (OSError, ValueError):
                pass

    def save(self, row):
        row["updated_at"] = time.time()
        path = self.root / f"{row['job_id']}.json"
        descriptor, name = tempfile.mkstemp(prefix=".video-job-", dir=self.root)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(row, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            descriptor = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)

    def get(self, job_id):
        if not isinstance(job_id, str) or len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
            raise ValueError("invalid job_id")
        row = json.loads(_read_regular(self.root / f"{job_id}.json", 2 * 1024 * 1024))
        for shot in row.get("shots", []):
            shot.setdefault("submission_phase", "UNKNOWN")
            if not shot.get("artifact") and shot["submission_phase"] in {"REQUEST_STARTED", "UNKNOWN", "RECEIVED"}:
                artifact = self._recover_artifact(row, shot)
                if artifact:
                    shot.update(artifact=artifact, state="SUCCEEDED", submission_phase="RECEIVED", artifact_recovered=True)
        try:
            row["cooperative_cancel"] = json.loads(_read_regular(self.root / (job_id + ".cooperative-cancel.json"), 8192))
        except (OSError, ValueError):
            pass
        from .runtime_view import job_runtime_view
        row['runtime'] = job_runtime_view(row, kind='video')
        return self.cancel_view(row)

    def cancel(self, job_id):
        row = self.get(job_id)
        if row["state"] == "BLOCKED":
            return row  # No worker or acquired resource remains to cancel.
        result = super().cancel(job_id)
        # A Gateway restart can leave a Ring4 row in SUBMISSION_UNKNOWN with
        # no worker thread left to observe the durable marker.  In that case
        # complete the same remote-delete + Gateway-stop path synchronously;
        # otherwise the marker alone would recreate the bug this MCP is meant
        # to fix.  A live worker owns the fast polling path below.
        # INTERRUPTED+cleanup_required is also unfinished: a prior cancel_stop
        # ownership failure must be retryable without rewriting the journal.
        latest = self.get(job_id)
        if (_row_runtime(latest)[0] == RING4_MODEL
                and job_id not in self._running
                and (latest["state"] not in TERMINAL or latest.get("cleanup_required"))):
            try:
                self._cancel_ring4_unresolved(latest)
            except Exception as exc:
                latest.update(state="INTERRUPTED", cleanup_required=True,
                              memory_released=False, resource_settled=False,
                              resource_status="cancel_stop_failed",
                              error=f"Ring4 cancellation requires recovery: {exc}")
                self.save(latest)
            return self.get(job_id)
        return result

    def cancel_view(self, row):
        result = super().cancel_view(row)
        if result["state"] == "BLOCKED":
            result.pop("cancel_status", None)
        if (_row_runtime(row)[0] == RING4_MODEL and result.get("cancel_requested")
                and result["state"] not in TERMINAL):
            result["cancel_status"] = self.ring4_cancel_boundary
            result["cancel_action"] = "ring4_remote_delete_then_gateway_stop"
        elif row.get("profile") == LONG_PROFILE and result.get("cancel_requested") and result["state"] not in TERMINAL:
            result["cancel_status"] = "已记录取消；H3 当前片段完成后不再提交后续片段，并释放所占执行槽"
        return result

    def find(self, key):
        if not isinstance(key, str) or not 8 <= len(key) <= 128:
            raise ValueError("invalid video idempotency key")
        found = []
        for path in self.root.glob("*.json"):
            if not re.fullmatch(r"[a-f0-9]{32}\.json", path.name):
                continue
            row = json.loads(_read_regular(path, 2 * 1024 * 1024))
            if row.get("idempotency_key") == key:
                found.append(row["job_id"])
        if len(found) > 1:
            raise ValueError("multiple video batches match the original key")
        return self.get(found[0]) if found else None

    def _token(self):
        if self.secret_path.is_symlink():
            raise RuntimeError("H3 credential is unavailable")
        info = self.secret_path.stat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeError("H3 credential is unavailable")
        value = self.secret_path.read_bytes().rstrip(b"\r\n")
        if len(value) < 16 or b"\n" in value or b"\r" in value:
            raise RuntimeError("H3 credential is unavailable")
        return value.decode("ascii")

    def _arrange_ring_runtime(self, profile):
        """Use the H3 partition that is already up. Start official only when none is running."""
        model, mapped_profile, task = runtime_target(profile)
        if model != RING4_MODEL:
            return model, mapped_profile, task
        adapters = getattr(getattr(self.app, "operations", None), "adapters", None) or {}
        ring = adapters.get(RING4_MODEL)
        if ring is None or not hasattr(ring, "observe"):
            return model, mapped_profile, task
        from .adapters.h3_ring import PROFILES as RING_PROFILES
        observed = ring.observe()
        if observed.state in {"HEALTHY", "STARTING"} and observed.profile in RING_PROFILES:
            supported = {"t2va", "fl2va"} if observed.profile == "fl2va-official" else {"ref2va"}
            if task not in supported:
                raise BlockingIOError(f"H3 partition {observed.profile} does not serve {task}; switch through Gateway first")
            return RING4_MODEL, observed.profile, task
        if observed.state == "STOPPED":
            default = "fl2va-official" if task == "t2va" else "ref2va-official"
            return RING4_MODEL, default, task
        raise BlockingIOError("DGX busy: H3 state is uncertain")

    def _non_preemptive_clear(self, profile, runtime_model=None):
        residency = getattr(self.app, "residency", None)
        if residency and residency.row["state"] == "IDLE":
            return
        runtime_model = runtime_model or runtime_target(profile)[0]
        adapters = getattr(getattr(self.app, "operations", None), "adapters", {})
        observed = [adapter.observe() for adapter in adapters.values()]
        if not observed:
            raise BlockingIOError("DGX busy: another model is active or uncertain")
        for item in observed:
            if item.state == "STOPPED":
                continue
            if item.model_id == runtime_model and item.state in {"STARTING", "HEALTHY"}:
                continue
            raise BlockingIOError("DGX busy: another model is active or uncertain")

    def submit(self, shots, key, profile=PROFILE, non_preemptive=False, diagnostic=None):
        from .h3_diagnostics import validate_diagnostic
        diagnostic = validate_diagnostic(diagnostic)
        if diagnostic and (len(shots) != 1 or profile == TEXT_PROFILE):
            raise ValueError("Diagnostics require one Ref2VA shot")
        if type(non_preemptive) is not bool or not isinstance(key, str) or not 8 <= len(key) <= 128:
            raise ValueError("invalid video admission options")
        if profile not in PROFILES:
            raise ValueError("unsupported H3 video profile")
        if not isinstance(shots, list) or not 1 <= len(shots) <= 32:
            raise ValueError("shots must contain 1–32 items")
        normalized = []
        names = set()
        for index, shot in enumerate(shots):
            if (not isinstance(shot, dict) or not {"name", "seed"} <= set(shot)
                    or set(shot) - {"name", "prompt", "image_asset_id", "action_image_asset_id", "video_asset_id",
                                    "audio_asset_id", "seed", "request_duration_seconds", "contract_version", "reference_image_ids"}):
                raise ValueError(f"invalid shot {index}")
            shot = {"prompt": "", "image_asset_id": "", "audio_asset_id": "", "action_image_asset_id": "", "video_asset_id": "", **shot}
            if not isinstance(shot["name"], str) or not _SHOT_NAME.fullmatch(shot["name"]) or shot["name"] in names:
                raise ValueError(f"invalid shot {index} name")
            names.add(shot["name"])
            if not isinstance(shot["prompt"], str) or "\x00" in shot["prompt"] or len(shot["prompt"].encode()) > 16384:
                raise ValueError(f"invalid shot {index} prompt")
            if profile == TEXT_PROFILE and not shot["prompt"].strip():
                raise ValueError("Legacy T2VA requires a non-empty prompt")
            if isinstance(shot["seed"], bool) or not isinstance(shot["seed"], int) or not 0 <= shot["seed"] <= 2147483647:
                raise ValueError(f"invalid shot {index} seed")
            reference_ids = [shot["image_asset_id"], shot["action_image_asset_id"],
                             shot["video_asset_id"], shot["audio_asset_id"]]
            node_images = shot.get("reference_image_ids", [])
            if not isinstance(node_images, list) or len(node_images) > 9 or any(not isinstance(aid, str) or not re.fullmatch(r"[a-f0-9]{64}", aid) for aid in node_images):
                raise ValueError("invalid reference_image_ids")
            has_reference = any(reference_ids) or bool(node_images)
            if profile == TEXT_PROFILE and has_reference:
                raise ValueError("T2VA requires empty reference assets")
            for field in ("image_asset_id", "action_image_asset_id"):
                if shot[field]:
                    image = self.images.get(shot[field])
                    if image["content_type"] not in {"image/png", "image/jpeg"}:
                        raise ValueError(f"invalid shot {index} image type; H3 requires PNG or JPEG")
            for aid in node_images:
                if self.images.get(aid)["content_type"] not in {"image/png", "image/jpeg"}:
                    raise ValueError(f"invalid shot {index} reference image type")
            if shot["video_asset_id"]:
                self.video_refs.get(shot["video_asset_id"])
            if shot["audio_asset_id"]:
                self.audio.get(shot["audio_asset_id"])
            generation = gateway_generation(profile, shot)
            normalized.append({**shot, "request_duration_seconds": generation["requestDurationSeconds"],
                               "contract_version": generation["contractVersion"], "state": "QUEUED", "index": index,
                               "request_id": uuid.uuid4().hex, "submission_phase": "NOT_SUBMITTED"})
        with self.app._submit_lock:
            for path in self.root.glob("*.json"):
                if not re.fullmatch(r"[a-f0-9]{32}\.json", path.name):
                    continue
                old = json.loads(path.read_text())
                if old.get("idempotency_key") == key:
                    identity_fields = ("name", "prompt", "image_asset_id", "action_image_asset_id", "video_asset_id", "audio_asset_id", "seed")
                    expected = [{k: s.get(k, "") for k in identity_fields} for s in old["shots"]]
                    supplied = [{k: s.get(k, "") for k in identity_fields} for s in normalized]
                    references_match = [s.get("reference_image_ids") or [] for s in old["shots"]] == [s.get("reference_image_ids") or [] for s in normalized]
                    contracts_match = all(gateway_generation(old.get("profile", PROFILE), a) == gateway_generation(profile, b)
                                          for a, b in zip(old["shots"], normalized))
                    if old.get("diagnostic") != diagnostic:
                        raise ValueError("idempotency key reused with different diagnostic options")
                    if old.get("profile") != profile or expected != supplied or not references_match or not contracts_match or old.get("non_preemptive", False) != non_preemptive:
                        raise ValueError("idempotency key reused with different video batch")
                    return self.get(old["job_id"])
            text_jobs = getattr(self.app, 'text_jobs', None)
            if text_jobs: text_jobs.assert_available()
            runtime_model, runtime_profile, runtime_task = self._arrange_ring_runtime(profile)
            if diagnostic and (runtime_model != RING4_MODEL or runtime_profile != "ref2va-official"):
                raise ValueError("Diagnostics require h3-ring4 / ref2va-official")
            adapters = getattr(getattr(self.app, "operations", None), "adapters", {})
            if runtime_model == RUNTIME_MODEL and runtime_model not in adapters:
                runtime_model = RING4_MODEL
            if self.app.creative_jobs.active or getattr(getattr(self.app, 'avatar_jobs', None), 'active', None):
                raise BlockingIOError("DGX busy")
            operation = self.app.store.active_operation()
            queueing_behind_dual_start = bool(
                operation and runtime_model == RUNTIME_MODEL
                and operation.target_model == RUNTIME_MODEL
                and operation.idempotency_key.endswith("-start")
            )
            if operation and not queueing_behind_dual_start:
                raise BlockingIOError("DGX busy")
            if self.active_jobs:
                if runtime_model != RUNTIME_MODEL:
                    raise BlockingIOError("DGX busy")
                active_rows = [self.get(job_id) for job_id in self.active_jobs]
                if any(_row_runtime(item)[0] != RUNTIME_MODEL for item in active_rows):
                    raise BlockingIOError("DGX busy")
                if any(_row_runtime(item)[1] != runtime_profile for item in active_rows):
                    raise BlockingIOError("H3 pair queue already owns a different profile")
                if len(active_rows) >= 32:
                    raise BlockingIOError("H3 pair queue is full")
            if non_preemptive:
                self._non_preemptive_clear(profile, runtime_model)
            with creative_admission(self.app.config):
                job_id = uuid.uuid4().hex
                row = {"job_id": job_id, "idempotency_key": key, "profile": profile, "non_preemptive": non_preemptive,
                       "state": "QUEUED", "created_at": time.time(), "memory_released": False,
                       "runtime_model": runtime_model, "runtime_profile": runtime_profile,
                       "runtime_task": runtime_task,
                       "diagnostic": diagnostic,
                       "execution_mode": "parallel_two_pair_queue" if runtime_model == RUNTIME_MODEL else "ring4_four_node",
                       "shots": normalized}
                self.save(row)
                self.active_jobs.add(job_id)
                self.active = self.active or job_id
            threading.Thread(target=self.run, args=(row,), daemon=True, name=f"video-batch-{job_id}").start()
            return self.get(job_id)

    def switch(self, row, target, suffix):
        profile = None
        if target:
            runtime_model, runtime_profile, _task = _row_runtime(row)
            profile = runtime_profile if target == runtime_model else row["profile"]
        options = {"job_owner": row["job_id"]}
        if target is not None and row.get("non_preemptive"):
            options["non_preemptive"] = True
        operation, _ = self.app.submit_switch(target, profile, f"{row['job_id']}-{suffix}", **options)
        row[f"{suffix}_operation_id"] = operation.operation_id
        self.save(row)
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            operation = self.app.store.get_operation(operation.operation_id)
            if operation.phase in {"RELEASED", "RECOVERY_REQUIRED", "CANCELED"}:
                if operation.outcome == "NO_RESOURCES_ACQUIRED":
                    if target is None:
                        row.update(memory_released=False, cleanup_required=True)
                        self.save(row)
                        raise BlockingIOError("Video stop ownership changed; cleanup remains unresolved")
                    row.update(resource_not_acquired=True, memory_released=True, cleanup_required=False,
                               release_reason="Gateway rejected admission before acquiring model resources")
                    self.save(row)
                    raise BlockingIOError("DGX busy; this batch never acquired model resources")
                if target is not None and operation.phase == "RELEASED" and operation.outcome in {"FAILED_SAFE_STOPPED", "ROLLED_BACK", "CANCELED"}:
                    row.update(memory_released=True, cleanup_required=False,
                               release_reason="Gateway completed safe cleanup of the failed start")
                    self.save(row)
                if operation.outcome != "COMMITTED":
                    raise RuntimeError("model lifecycle did not commit")
                return
            time.sleep(1)
        raise TimeoutError("model lifecycle timeout")

    @staticmethod
    def _multipart(fields, files):
        boundary = f"dgx-video-{uuid.uuid4().hex}"
        chunks = []
        for name, value in fields.items():
            chunks.extend([f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode(), str(value).encode(), b"\r\n"])
        for name, filename, content_type, data in files:
            chunks.extend([f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n".encode(), data, b"\r\n"])
        chunks.append(f"--{boundary}--\r\n".encode())
        return b"".join(chunks), f"multipart/form-data; boundary={boundary}"

    def _ring_transport(self):
        if getattr(self, "ring_transport", None) is None:
            from .video_ring_transport import Ring4VideoTransport
            ssh = None
            adapters = getattr(getattr(self.app, "operations", None), "adapters", None) or {}
            ring = adapters.get("h3-ring4")
            if ring is not None:
                ssh = getattr(ring, "ssh", None)
            self.ring_transport = Ring4VideoTransport(ssh)
        return self.ring_transport

    def _pair_transport(self, slot):
        transports = getattr(self, "pair_transports", None)
        if transports is None:
            transports = self.pair_transports = {}
        if slot not in transports:
            from .video_pair_transport import PairVideoTransport
            ssh = None
            adapters = getattr(getattr(self.app, "operations", None), "adapters", None) or {}
            pair = adapters.get(RUNTIME_MODEL)
            if pair is not None:
                ssh = getattr(pair, "ssh", None)
            transports[slot] = PairVideoTransport(slot, ssh)
        return transports[slot]

    def _release_ring4_claim(self, row):
        row.update(memory_released=True, cleanup_required=False, resource_settled=True,
                   resource_status="released",
                   release_reason="h3-ring4 standing four-node service; clip finished without stop")

    def _observed_all_stopped(self):
        adapters = getattr(getattr(self.app, "operations", None), "adapters", None) or {}
        if not adapters:
            return False
        try:
            observed = [adapter.observe() for adapter in adapters.values()]
        except Exception:
            return False
        return bool(observed) and all(item.state == "STOPPED" for item in observed)

    def _stop_ring4_after_cancel(self, row):
        """Stop the owned Ring4 runtime after a confirmed cancel request.

        Ring4 is a standing four-node service.  Its public DELETE endpoint
        removes the remote job record but the upstream runtime does not yet
        interrupt the distributed denoise loop.  Therefore cancellation must
        also go through the Gateway lifecycle controller; this is the only
        path that can prove all four ranks stopped and release ownership.
        When every adapter is already STOPPED, skip that switch: a no-op
        already-at-target start has no resident-handoff, and ownership would
        refuse the stop even though there is nothing left to drain.
        """
        if row.get("ring4_cancel_stopped"):
            return
        if self._observed_all_stopped():
            row["ring4_cancel_stopped"] = True
            row["ring4_cancel_stop_at"] = time.time()
            self.save(row)
            return
        self.switch(row, None, "cancel_stop")
        row["ring4_cancel_stopped"] = True
        row["ring4_cancel_stop_at"] = time.time()
        self.save(row)

    def _cancel_ring4_unresolved(self, row):
        """Cancel remote Ring4 IDs for a post-restart/unknown batch."""
        transport = self._ring_transport()
        # An earlier DELETE can succeed before lifecycle stop fails. A retry
        # must still stop the service even if every remote shot says CANCELED.
        found = any(s.get("ring_job_id") and not s.get("artifact") for s in row.get("shots", []))
        for shot in row.get("shots", []):
            remote_id = shot.get("ring_job_id")
            if not remote_id or shot.get("artifact") or shot.get("state") in {"SUCCEEDED", "CANCELED"}:
                continue
            found = True
            try:
                shot["ring_cancel_receipt"] = transport.cancel(remote_id)
            except Exception as exc:
                shot["ring_cancel_error"] = str(exc)[:300]
                self.save(row)
                raise
            shot.update(submission_phase="CANCELED", state="CANCELED",
                        ring_cancel_requested_at=time.time())
            self.save(row)
        if found:
            self._stop_ring4_after_cancel(row)
        for shot in row.get("shots", []):
            if shot.get("state") == "QUEUED":
                shot["state"] = "CANCELED"
        self._release_ring4_claim(row)
        row.update(state="CANCELED", cancel_completed_at=time.time(),
                   release_reason="h3-ring4 runtime stopped after explicit cancellation")
        self._clear_active(row["job_id"])
        self.save(row)

    def _release_pair_claim(self, row):
        row.update(memory_released=True, cleanup_required=False, resource_settled=True,
                   resource_status="released",
                   release_reason="dual-pair standing service; batch released its pair execution slot")

    def _generate(self, row, shot):
        runtime_model, _profile, _task = _row_runtime(row)
        if runtime_model == RUNTIME_MODEL:
            return self._generate_pair(row, shot)
        if runtime_model == RING4_MODEL:
            return self._generate_ring4(row, shot)
        return self._generate_two_node(row, shot)

    def _generate_pair(self, row, shot):
        from .video_pair_transport import PairRequestUnknown
        if shot.get("submission_phase", "NOT_SUBMITTED") != "NOT_SUBMITTED":
            raise SubmissionUnknown("Never replay a clip whose request has already started")
        if self._artifact_path(row, shot).exists() or self._artifact_path(row, shot).is_symlink():
            raise ValueError("An existing video result requires read-only recovery, not another request")
        slot = row.get("executor_slot")
        if slot not in self._slots:
            raise RuntimeError("H3 pair slot was not assigned")
        shot.setdefault("request_id", uuid.uuid4().hex)
        _model, _profile, task = _row_runtime(row)
        transport = self._pair_transport(slot)
        conditions = []
        if task != "t2va":
            for field, suffix in (("image_asset_id", "primary"), ("action_image_asset_id", "action")):
                if not shot.get(field):
                    continue
                image = self.images.get(shot[field])
                ext = ".png" if image["content_type"] == "image/png" else ".jpg"
                rel = f"workbench/{row['job_id']}/{shot['request_id']}-{suffix}{ext}"
                transport.stage(rel, image["path"])
                conditions.append({"type": "image", "uri": transport.uri(rel), "role": "reference"})
            if shot.get("video_asset_id"):
                video = self.video_refs.get(shot["video_asset_id"])
                rel = f"workbench/{row['job_id']}/{shot['request_id']}-video.mp4"
                transport.stage(rel, video["path"])
                conditions.append({"type": "video", "uri": transport.uri(rel), "role": "reference"})
            if shot.get("audio_asset_id"):
                audio = self.audio.get(shot["audio_asset_id"])
                rel = f"workbench/{row['job_id']}/{shot['request_id']}-audio.wav"
                transport.stage(rel, audio["path"])
                conditions.append({"type": "audio", "uri": transport.uri(rel), "role": "reference"})
        payload = transport.payload(task, shot["prompt"], shot["seed"], conditions,
                                    shot["request_duration_seconds"])
        self.check_cancel(row)
        def mark_started():
            shot["submission_phase"] = "REQUEST_STARTED"
            self.save(row)
        try:
            remote_id = transport.submit(payload, before_post=mark_started)
            shot.update(pair_job_id=remote_id, pair_id=slot)
            self.save(row)
            while True:
                status = transport.poll(remote_id).get("status")
                if shot.get("pair_status") != status:
                    shot.update(pair_status=status, pair_status_updated_at=time.time())
                    self.save(row)
                if status == "completed":
                    return self._publish_result(row, shot, "video/mp4", transport.download(remote_id))
                if status in {"failed", "cancelled", "canceled", "expired"}:
                    raise ValueError("H3 pair rejected the video request")
                if status not in {"queued", "pending", "running", "in_progress", "processing"}:
                    raise SubmissionUnknown("H3 pair job status is unknown; original request is retained")
                time.sleep(2)
        except JobCanceled:
            shot.update(submission_phase="CANCELED", state="CANCELED")
            self.save(row); raise
        except ValueError:
            shot["submission_phase"] = "REJECTED"
            self.save(row); raise
        except (SubmissionUnknown, PairRequestUnknown) as exc:
            shot["submission_phase"] = "UNKNOWN"
            self.save(row)
            raise SubmissionUnknown("H3 pair request outcome is unknown; original request is retained") from exc
        except RuntimeError:
            raise
        except Exception as exc:
            shot["submission_phase"] = "UNKNOWN"
            self.save(row)
            raise SubmissionUnknown("H3 pair request outcome is unknown; original request is retained") from exc

    def _generate_ring4(self, row, shot):
        from .video_ring_transport import Ring4Unknown
        if shot.get("submission_phase", "NOT_SUBMITTED") != "NOT_SUBMITTED":
            raise SubmissionUnknown("Never replay a clip whose request has already started")
        if self._artifact_path(row, shot).exists() or self._artifact_path(row, shot).is_symlink():
            raise ValueError("An existing video result requires read-only recovery, not another request")
        shot.setdefault("request_id", uuid.uuid4().hex)
        _model, _profile, task = _row_runtime(row)
        transport = self._ring_transport()
        conditions = []
        if task != "t2va":
            image_ids = [item for item in (shot.get("reference_image_ids") or [shot.get("image_asset_id"), shot.get("action_image_asset_id")]) if item]
            for index, asset_id in enumerate(image_ids, 1):
                image = self.images.get(asset_id)
                ext = ".png" if image["content_type"] == "image/png" else ".jpg"
                rel = f"workbench/{row['job_id']}/{shot['request_id']}-picture-{index}{ext}"
                transport.stage(rel, image["path"])
                conditions.append({"type": "image", "uri": transport.uri(rel), "role": "reference"})
            if shot.get("video_asset_id"):
                video = self.video_refs.get(shot["video_asset_id"])
                rel = f"workbench/{row['job_id']}/{shot['request_id']}-video.mp4"
                transport.stage(rel, video["path"])
                conditions.append({"type": "video", "uri": transport.uri(rel), "role": "reference"})
            if shot.get("audio_asset_id"):
                audio = self.audio.get(shot["audio_asset_id"])
                rel = f"workbench/{row['job_id']}/{shot['request_id']}-audio.wav"
                transport.stage(rel, audio["path"])
                conditions.append({"type": "audio", "uri": transport.uri(rel), "role": "reference"})
        payload = transport.payload(task, shot["prompt"], shot["seed"], conditions,
                                    duration_seconds=int(shot.get("request_duration_seconds") or 4))
        if row.get("diagnostic"):
            payload["performance_diagnostic"] = {"id": row["job_id"], "trace_step": row["diagnostic"]["trace_step"]}
            if row["diagnostic"].get("attention_probe"):
                payload["performance_diagnostic"]["attention_probe"] = True
            row["diagnostic_started_at"] = time.time()
            self.save(row)
        self.check_cancel(row)
        def mark_started():
            shot["submission_phase"] = "REQUEST_STARTED"
            self.save(row)
        try:
            remote_id = transport.submit(payload, before_post=mark_started)
            shot["ring_job_id"] = remote_id
            shot["submission_phase"] = "RECEIVED"
            self.save(row)
            return self._monitor_ring4(row, shot, transport, remote_id)
        except JobCanceled:
            shot.update(submission_phase="CANCELED", state="CANCELED")
            self.save(row)
            raise
        except ValueError:
            shot["submission_phase"] = "REJECTED"
            self.save(row)
            raise
        except RuntimeError:
            raise
        except (SubmissionUnknown, Ring4Unknown) as exc:
            shot["submission_phase"] = "UNKNOWN"
            self.save(row)
            raise SubmissionUnknown("H3 request outcome is unknown; original request is retained") from exc
        except Exception as exc:
            shot["submission_phase"] = "UNKNOWN"
            self.save(row)
            raise SubmissionUnknown("H3 request outcome is unknown; original request is retained") from exc

    def _monitor_ring4(self, row, shot, transport, remote_id):
        """Track an already submitted job; never stage assets or POST inference."""
        # Long Ring4 clips are asynchronous and have no client-side total
        # deadline. A fixed 3700s deadline previously abandoned a healthy
        # 15s job while all four ranks were still computing.
        deadline = None if int(shot.get("request_duration_seconds") or 4) > 4 else time.monotonic() + 3700
        poll_failures = 0
        diagnostic_poll_at = 0.0
        while True:
            if row.get("diagnostic"):
                from .h3_diagnostics import should_stop
                if time.monotonic() >= diagnostic_poll_at:
                    row["diagnostic_progress"] = transport.diagnostic_progress(row["job_id"])
                    diagnostic_poll_at = time.monotonic() + 10
                    self.save(row)
                reason = should_stop(row, time.time())
                if reason:
                    row["diagnostic_stop_reason"] = reason
                    self.save(row)
                    self.cancel(row["job_id"])
                    self._stop_ring4_after_cancel(row)
                    raise JobCanceled("Diagnostic budget completed; Ring4 stopped through Gateway")
            if deadline is not None and time.monotonic() >= deadline:
                raise SubmissionUnknown("Ring4 client deadline reached; original job is retained")
            if self.cancel_requested(row["job_id"]):
                try:
                    shot["ring_cancel_receipt"] = transport.cancel(remote_id)
                    shot["ring_cancel_requested_at"] = time.time()
                    self.save(row)
                except Exception as exc:
                    shot["ring_cancel_error"] = str(exc)[:300]
                    self.save(row)
                    # The user explicitly requested termination.  Continue
                    # to the Gateway-owned stop even when the remote DELETE
                    # receipt is unavailable; the runtime stop is the
                    # authoritative compute termination proof.
                self._stop_ring4_after_cancel(row)
                raise JobCanceled("Ring4 generation canceled and runtime stopped")
            try:
                remote = transport.poll(remote_id)
            except (RuntimeError, OSError, TimeoutError, ValueError) as exc:
                poll_failures += 1
                shot.update(ring_poll_failures=poll_failures,
                            ring_poll_error=type(exc).__name__,
                            ring_poll_error_at=time.time())
                self.save(row)
                if poll_failures >= 5:
                    if row.get("diagnostic"):
                        row["diagnostic_stop_reason"] = "status_queries_failed"
                        self.save(row)
                        self.cancel(row["job_id"])
                        self._stop_ring4_after_cancel(row)
                        raise JobCanceled("Diagnostic status lost; Ring4 stopped through Gateway") from exc
                    raise SubmissionUnknown("Ring4 status queries failed repeatedly; original job is retained") from exc
                # GET is safe to retry; POST and the original job id are untouched.
                time.sleep(min(5 * poll_failures, 20))
                continue
            poll_failures = 0
            status = remote.get("status")
            shot.update(ring_status=status, ring_status_updated_at=time.time(),
                        ring_poll_failures=0)
            shot.pop("ring_poll_error", None)
            shot.pop("ring_poll_error_at", None)
            self.save(row)
            if status == "completed":
                if row.get("diagnostic"):
                    self.cancel(row["job_id"])
                    self._stop_ring4_after_cancel(row)
                    raise JobCanceled("Diagnostic run ended; no creative artifact published")
                return self._publish_result(row, shot, "video/mp4", transport.download(remote_id))
            if status in {"failed", "cancelled", "canceled", "expired"}:
                raise ValueError("H3 rejected the video request")
            if status not in {"queued", "pending", "running", "in_progress", "processing"}:
                raise SubmissionUnknown("Ring4 job status is unknown; original job is retained")
            time.sleep(2)

    def _generate_two_node(self, row, shot):
        if shot.get("submission_phase", "NOT_SUBMITTED") != "NOT_SUBMITTED":
            raise SubmissionUnknown("Never replay a clip whose request has already started")
        if self._artifact_path(row, shot).exists() or self._artifact_path(row, shot).is_symlink():
            raise ValueError("An existing video result requires read-only recovery, not another request")
        shot.setdefault("request_id", uuid.uuid4().hex)
        text_only = row["profile"] == TEXT_PROFILE
        files = []
        if not text_only:
            image = self.images.get(shot["image_asset_id"])
            audio = self.audio.get(shot["audio_asset_id"])
            files = [("reference_image", image["filename"], image["content_type"], Path(image["path"]).read_bytes()),
                     ("reference_audio", audio["filename"], "audio/wav", Path(audio["path"]).read_bytes())]
        generation = gateway_generation(row["profile"], shot)
        fields = {"prompt": shot["prompt"], "width": 1344, "height": 768, "fps": 24, "num_inference_steps": 8 if text_only else 20, "flow_shift": 12.0, "seed": shot["seed"], "extra_params": json.dumps({"task": "t2va" if text_only else "ref2va", "duration": float(generation["requestDurationSeconds"]), "audio_flow_shift": 3.0, "required_profile": row["profile"]}, separators=(",", ":"))}
        body, content_type = self._multipart(fields, files)
        request = urllib.request.Request(self.endpoint, data=body, headers={"Authorization": f"Bearer {self._token()}",
            "Content-Type": content_type, "X-H3-Request-ID": shot["request_id"]})
        self.check_cancel(row)
        shot["submission_phase"] = "REQUEST_STARTED"
        self.save(row)  # fsync both file and directory before the first request byte.
        try:
            if row["profile"] == LONG_PROFILE:
                def receipt(status):
                    self._cancel_receipt(row["job_id"], shot["request_id"], status)
                media_type, data = self.long_transport.read(self.endpoint, request, MAX_VIDEO_BYTES,
                    lambda: self.cancel_requested(row["job_id"]), receipt)
            else:
                with self.http.open(request, timeout=30) as response:
                    media_type = response.headers.get_content_type()
                    data = response.read(MAX_VIDEO_BYTES + 1)
        except JobCanceled:
            shot.update(submission_phase="CANCELED", state="CANCELED")
            self.save(row)
            raise
        except urllib.error.HTTPError as exc:
            if exc.code in {400, 401, 403, 404, 409, 422, 429}:
                shot["submission_phase"] = "REJECTED"
                self.save(row)
                raise ValueError("H3 rejected the video request") from exc
            recovered = self._wait_original_result(row, shot)
            if recovered is None:
                shot["submission_phase"] = "UNKNOWN"
                self.save(row)
                raise SubmissionUnknown("H3 request outcome is unknown; original request is retained") from exc
            media_type, data = recovered
        except Exception as exc:
            recovered = self._wait_original_result(row, shot)
            if recovered is None:
                shot["submission_phase"] = "UNKNOWN"
                self.save(row)
                raise SubmissionUnknown("H3 request outcome is unknown; original request is retained") from exc
            media_type, data = recovered
        return self._publish_result(row, shot, media_type, data)

    def _publish_result(self, row, shot, media_type, data):
        shot["submission_phase"] = "RECEIVED"
        self.save(row)
        if media_type != "video/mp4" or not data or len(data) > MAX_VIDEO_BYTES or not data[4:12].startswith(b"ftyp"):
            raise RuntimeError("H3 returned an invalid or oversized MP4")
        target = self._artifact_path(row, shot)
        root = target.parent
        root.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".video-artifact-", suffix=".mp4", dir=root)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            from .drama.media import probe_media
            probe_media(temporary)
            os.link(temporary, target)  # Never overwrite an existing request result.
            directory = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        partial = self.root / (shot["request_id"] + ".result.part")
        if partial.is_file() and not partial.is_symlink(): partial.unlink()
        return self._artifact_view(row, shot, data)

    def _wait_original_result(self, row, shot, *, wait_seconds=3700):
        """Recover one request with authenticated GET/Range; never submit inference again."""
        import http.client
        endpoint = self.endpoint.removesuffix("/sync") + "/result"
        rid = shot.get("request_id", "")
        if not re.fullmatch(r"[a-f0-9]{32}", rid): return None
        partial = self.root / (rid + ".result.part")
        if partial.is_symlink(): raise ValueError("Result download cannot be a symlink")
        deadline, failures = time.monotonic() + wait_seconds, 0
        while True:
            offset = partial.stat().st_size if partial.exists() else 0
            try: token = self._token()
            except OSError: return None
            headers = {"Authorization": f"Bearer {token}", "X-H3-Request-ID": rid}
            if offset: headers["Range"] = f"bytes={offset}-"
            request = urllib.request.Request(endpoint, headers=headers)
            try:
                with self.http.open(request, timeout=15) as response:
                    if response.status in {200, 206}:
                        digest = response.headers.get("X-H3-Result-SHA256", "")
                        if not re.fullmatch(r"[a-f0-9]{64}", digest): raise ValueError("Missing original result checksum")
                        if response.status == 206:
                            match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", response.headers.get("Content-Range", ""))
                            if not match or int(match[1]) != offset: raise ValueError("Invalid resumed result range")
                            total = int(match[3])
                        else:
                            offset = 0
                            total = int(response.headers.get("Content-Length", "0"))
                        if not 0 < total <= MAX_VIDEO_BYTES: raise ValueError("Original result size exceeds limit")
                        with partial.open("ab" if offset else "wb") as out:
                            while True:
                                chunk = response.read(65536)
                                if not chunk: break
                                offset += len(chunk)
                                if offset > total: raise ValueError("Original result exceeds declared length")
                                out.write(chunk); out.flush()
                            os.fsync(out.fileno())
                        data = _read_regular(partial, MAX_VIDEO_BYTES)
                        if len(data) != total: raise OSError("Incomplete original result download")
                        if hashlib.sha256(data).hexdigest() != digest: raise ValueError("Original video result checksum mismatch")
                        shot["resultRecovered"] = True
                        return response.headers.get_content_type(), data
                    if response.status != 202: return None
                    failures = 0
                    shot["resultStatus"] = "RUNNING_ORIGINAL"
                    self.save(row)
            except urllib.error.HTTPError as error:
                if error.code in {400, 401, 403, 404, 409, 416, 422}: return None
                failures += 1
            except (OSError, TimeoutError, http.client.HTTPException):
                failures += 1
            if failures >= 3 or time.monotonic() >= deadline: return None
            time.sleep(min(5, max(0, deadline - time.monotonic())))

    def _cancel_receipt(self, job_id, request_id, status):
        # Separate atomic receipt prevents cancel watcher writes racing with the
        # generation worker's durable job snapshot.
        fd, name = tempfile.mkstemp(prefix=".video-cancel-", dir=self.root)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({"request_id": request_id, "status": status, "acknowledged": status == 200,
                           "acknowledgement_only": True, "updated_at": time.time()}, stream)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, self.root / (job_id + ".cooperative-cancel.json"))
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def _artifact_path(self, row, shot):
        if (not re.fullmatch(r"[a-f0-9]{32}", row.get("job_id", ""))
                or type(shot.get("index")) is not int or not 0 <= shot["index"] < 32
                or not _SHOT_NAME.fullmatch(shot.get("name", ""))):
            raise ValueError("invalid saved video artifact identity")
        directory = self.app.config.state_dir / "creative-artifacts" / row["job_id"]
        if directory.is_symlink() or directory.parent.is_symlink():
            raise ValueError("video artifact directory cannot be a symlink")
        return directory / f"{shot['index']:03d}-{shot['name']}.mp4"

    def _artifact_view(self, row, shot, data):
        target = self._artifact_path(row, shot)
        return {"kind": "video", "path": str(target),
                "url": f"http://{self.app.config.bind}:{self.app.config.port}/api/v1/artifacts/{row['job_id']}/{target.name}",
                "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def _recover_artifact(self, row, shot):
        """Read-only repair of the view after file publication but before JSON save."""
        try:
            target = self._artifact_path(row, shot)
            data = _read_regular(target, MAX_VIDEO_BYTES)
            from .drama.media import probe_media
            probe_media(target)
            return self._artifact_view(row, shot, data)
        except (OSError, ValueError):
            return None

    def _clear_active(self, job_id):
        self._pending_recovery.discard(job_id)
        self.active_jobs.discard(job_id)
        if self.active == job_id:
            candidates = self.active_jobs | self._pending_recovery
            self.active = next(iter(sorted(candidates)), None)

    def _acquire_slot(self, row):
        job_id = row["job_id"]
        with self._slot_condition:
            if job_id not in self._waiting:
                self._waiting.append(job_id)
            while True:
                self.check_cancel(row)
                free = next((slot for slot, owner in self._slots.items() if owner is None), None)
                if free is not None and self._waiting and self._waiting[0] == job_id:
                    self._waiting.pop(0)
                    self._slots[free] = job_id
                    row["executor_slot"] = free
                    row["slot_acquired_at"] = time.time()
                    self.save(row)
                    return free
                self._slot_condition.wait(timeout=1)

    def _release_slot(self, row):
        job_id = row["job_id"]
        with self._slot_condition:
            self._waiting = [item for item in self._waiting if item != job_id]
            for slot, owner in self._slots.items():
                if owner == job_id:
                    self._slots[slot] = None
            row["slot_released_at"] = time.time()
            self._slot_condition.notify_all()

    def _ensure_dual_runtime(self, row):
        expected = row.get("runtime_profile")
        adapter = self.app.operations.adapters[RUNTIME_MODEL]
        observed = adapter.observe()
        if observed.state == "HEALTHY" and observed.profile == expected:
            return
        starter = False
        with self._runtime_condition:
            while self._runtime_starter not in {None, row["job_id"]}:
                self._runtime_condition.wait(timeout=2)
                observed = adapter.observe()
                if observed.state == "HEALTHY" and observed.profile == expected:
                    return
            if self._runtime_starter is None:
                self._runtime_starter = row["job_id"]
                starter = True
        if not starter:
            return
        try:
            self.switch(row, RUNTIME_MODEL, "start")
            observed = adapter.observe()
            if observed.state != "HEALTHY" or observed.profile != expected:
                raise RuntimeError("dual-pair H3 did not become healthy at the requested profile")
        finally:
            with self._runtime_condition:
                self._runtime_starter = None
                self._runtime_condition.notify_all()

    def assert_stop_ownership(self, job_id, observed):
        """Called again inside submit_switch's lock immediately before a stop."""
        row = self.get(job_id)
        return assert_video_stop_ownership(self.app.store, row, observed)

    def run(self, row):
        with self.app._submit_lock:
            path = self.root / (row["job_id"] + ".json")
            current = self.get(row["job_id"]) if path.exists() else row
            if row["job_id"] in self._running or current.get("state") != "QUEUED":
                return
            self._running.add(row["job_id"])
            row.update(current)
            self.save(row)
        owns_h3 = False
        slot_acquired = False
        runtime_model, _runtime_profile, _task = _row_runtime(row)
        try:
            self.check_cancel(row)
            if runtime_model == RUNTIME_MODEL:
                self._acquire_slot(row)
                slot_acquired = True
            row["state"] = "LOADING"
            self.save(row)
            residency = getattr(self.app, 'residency', None)
            use_residency = residency if runtime_model not in {RUNTIME_MODEL, RING4_MODEL} else None
            if use_residency: use_residency.before_start('video_jobs', row)
            if runtime_model == RUNTIME_MODEL:
                self._ensure_dual_runtime(row)
            else:
                self.switch(row, runtime_model, "start")
            if use_residency: use_residency.claimed('video_jobs', row)
            owns_h3 = True
            self.check_cancel(row)
            row["state"] = "RUNNING"
            self.save(row)
            for shot in row["shots"]:
                self.check_cancel(row)
                shot["state"] = "RUNNING"
                shot["started_at"] = time.time()
                self.save(row)
                shot["artifact"] = self._generate(row, shot)
                shot["state"] = "SUCCEEDED"
                shot["completed_at"] = time.time()
                self.save(row)
            row["state"] = "RELEASING"
            self.save(row)
            if runtime_model == RUNTIME_MODEL:
                self._release_pair_claim(row)
            elif runtime_model == RING4_MODEL:
                self._release_ring4_claim(row)
            elif not (residency and residency.retain('video_jobs', row)):
                self.switch(row, None, "stop")
                row["memory_released"] = True
            row["state"] = "CANCELED" if self.cancel_requested(row["job_id"]) else "SUCCEEDED"
        except Exception as exc:
            unknown = isinstance(exc, SubmissionUnknown) or any(
                shot.get("submission_phase") in {"REQUEST_STARTED", "UNKNOWN"} for shot in row["shots"])
            blocked = isinstance(exc, BlockingIOError) and (not row.get("start_operation_id") or row.get("resource_not_acquired"))
            row["state"] = "SUBMISSION_UNKNOWN" if unknown else "BLOCKED" if blocked else "CANCELING" if isinstance(exc, JobCanceled) else "FAILED"
            row["error"] = str(exc) if isinstance(exc, (ValueError, BlockingIOError, SubmissionUnknown, JobCanceled)) else "video batch failed; inspect Gateway logs"
            for shot in row["shots"]:
                if shot["state"] == "RUNNING":
                    shot["state"] = "SUBMISSION_UNKNOWN" if unknown else "FAILED"
                    shot["error"] = row["error"]
            if runtime_model == RUNTIME_MODEL:
                if unknown:
                    # The pair backend is standing. Preserve the remote job
                    # identity; never submit the same clip to the other pair.
                    row.update(cleanup_required=False, memory_released=False,
                               resource_settled=False, resource_status="remote_job_unknown")
                else:
                    self._release_pair_claim(row)
                    if not row.get("start_operation_id"):
                        row["resource_not_acquired"] = True
                        row["release_reason"] = "No model was loaded by this job"
            elif runtime_model == RING4_MODEL:
                if unknown:
                    row.update(cleanup_required=False, memory_released=False,
                               resource_settled=False, resource_status="remote_job_unknown")
                else:
                    if isinstance(exc, JobCanceled) and owns_h3:
                        try:
                            self._stop_ring4_after_cancel(row)
                        except Exception as stop_exc:
                            row.update(cleanup_required=True, memory_released=False,
                                       resource_settled=False,
                                       resource_status="cancel_stop_failed",
                                       error=f"Ring4 cancellation stop failed: {stop_exc}")
                        else:
                            self._release_ring4_claim(row)
                            row["release_reason"] = "h3-ring4 runtime stopped after explicit cancellation"
                    else:
                        self._release_ring4_claim(row)
                    if not row.get("start_operation_id"):
                        row["resource_not_acquired"] = True
                        row["release_reason"] = "No model was loaded by this job"
            else:
                row["cleanup_required"] = owns_h3 or bool(row.get("start_operation_id")) and not row.get("memory_released")
                if not row["cleanup_required"]:
                    row["memory_released"] = True
                    if not row.get("start_operation_id"):
                        row["resource_not_acquired"] = True
                        row["release_reason"] = "No model was loaded by this job"
                if owns_h3 and not unknown:
                    try:
                        self.switch(row, None, "cleanup")
                        row["memory_released"] = True
                        row["cleanup_required"] = False
                    except Exception:
                        pass
        finally:
            if self.cancel_requested(row["job_id"]):
                for shot in row["shots"]:
                    if shot["state"] == "QUEUED": shot["state"] = "CANCELED"
                if row.get("memory_released"): row["state"] = "CANCELED"
                elif row.get("cleanup_required") and row["state"] != "SUBMISSION_UNKNOWN": row["state"] = "INTERRUPTED"
            residency = getattr(self.app, 'residency', None)
            if residency and runtime_model not in {RUNTIME_MODEL, RING4_MODEL}: residency.job_finished('video_jobs', row)
            if runtime_model == RUNTIME_MODEL:
                self._release_slot(row)
            self.save(row)
            with self.app._submit_lock:
                self._running.discard(row["job_id"])
                if not row.get("cleanup_required") and row.get("state") != "SUBMISSION_UNKNOWN":
                    self._clear_active(row["job_id"])

    def reconcile(self, job_id):
        row = self.get(job_id)
        if _row_runtime(row)[0] == RUNTIME_MODEL and any(
                shot.get("pair_job_id") and not shot.get("artifact") for shot in row.get("shots", [])):
            return self._reconcile_pair(job_id)
        if _row_runtime(row)[0] == RING4_MODEL and any(
                shot.get("ring_job_id") and not shot.get("artifact") for shot in row.get("shots", [])):
            return self._reconcile_ring4(job_id)
        with self.app._submit_lock:
            row = self.get(job_id)
            if not row.get("cleanup_required"):
                if row.get("memory_released"):
                    return row
                raise BlockingIOError("video batch is not awaiting recovery")
            text_jobs = getattr(self.app, 'text_jobs', None)
            if text_jobs: text_jobs.assert_available()
            if self.active != job_id or job_id in self._running or job_id in self._reconciling or row["state"] == "RECONCILING":
                raise BlockingIOError("video batch does not own idle recovery")
            if self.app.store.active_operation():
                raise BlockingIOError("a lifecycle operation is still active")
            observed = [adapter.observe() for adapter in self.app.operations.adapters.values()]
            # Recover results before release when the original H3 API is reachable.
            # Failed/stopped runtimes continue through the existing drain/evidence gates.
            if any(o.model_id == "minimax-h3" and o.state == "HEALTHY" for o in observed):
                for shot in row["shots"]:
                    if shot.get("submission_phase") in {"UNKNOWN", "REQUEST_STARTED"} and not shot.get("artifact"):
                        recovered = self._wait_original_result(row, shot, wait_seconds=0)
                        if recovered is None:
                            if shot.get("resultStatus") == "RUNNING_ORIGINAL": raise BlockingIOError("原任务仍在生成，请稍后核查；不会重发或停止模型")
                            continue  # Legacy unavailable receipt: retain UNKNOWN; existing drain gates still apply.
                        shot["artifact"] = self._publish_result(row, shot, *recovered)
                        shot["state"] = "SUCCEEDED"
                        self.save(row)
            ownership = self.assert_stop_ownership(job_id, observed)
            self._reconciling.add(job_id)
            row["state"] = "RECONCILING"
            self.save(row)
        try:
            if ownership != "STOPPED":
                self.switch(row, None, "reconcile_stop")
            row["memory_released"] = True
            row["cleanup_required"] = False
            row["state"] = "SUCCEEDED" if all(shot["state"] == "SUCCEEDED" for shot in row["shots"]) else "INTERRUPTED"
        except Exception:
            row["state"] = "INTERRUPTED"
            row["error"] = "video reconciliation failed; inspect Gateway lifecycle status"
            raise
        finally:
            if self.cancel_requested(row["job_id"]):
                for shot in row["shots"]:
                    if shot["state"] == "QUEUED": shot["state"] = "CANCELED"
                if row.get("memory_released"): row["state"] = "CANCELED"
                elif row.get("cleanup_required"): row["state"] = "INTERRUPTED"
            residency = getattr(self.app, 'residency', None)
            if residency: residency.job_finished('video_jobs', row)
            self.save(row)
            with self.app._submit_lock:
                self._reconciling.discard(job_id)
                if not row.get("cleanup_required"):
                    self._clear_active(job_id)
        return self.get(job_id)

    def _reconcile_pair(self, job_id):
        """Query/download the original pair job without ever submitting it again."""
        from .video_pair_transport import PairRequestUnknown
        with self.app._submit_lock:
            row = self.get(job_id)
            if job_id in self._running or job_id in self._reconciling:
                raise BlockingIOError("video batch does not own idle dual-pair reconciliation")
            pair_id = row.get("executor_slot")
            if pair_id not in self._slots:
                pair_ids = {shot.get("pair_id") for shot in row.get("shots", []) if shot.get("pair_job_id")}
                if len(pair_ids) != 1 or next(iter(pair_ids)) not in self._slots:
                    raise BlockingIOError("dual-pair recovery has no unambiguous original executor")
                pair_id = next(iter(pair_ids))
                row["executor_slot"] = pair_id
            owner = self._slots[pair_id]
            if owner not in {None, job_id}:
                raise BlockingIOError("original dual-pair executor is owned by another batch")
            self._slots[pair_id] = job_id
            self.active = self.active or job_id
            self.active_jobs.add(job_id)
            self._pending_recovery.add(job_id)
            self._reconciling.add(job_id)
            row["state"] = "RECONCILING"
            self.save(row)
        active_remote = False
        definite_failure = False
        try:
            transport = self._pair_transport(pair_id)
            for shot in row["shots"]:
                if shot.get("artifact") or shot.get("state") == "SUCCEEDED":
                    continue
                remote_id = shot.get("pair_job_id")
                if not remote_id or shot.get("submission_phase") not in {"REQUEST_STARTED", "UNKNOWN", "RECEIVED"}:
                    continue
                status = transport.poll(remote_id).get("status")
                shot.update(pair_status=status, pair_status_updated_at=time.time())
                if status == "completed":
                    shot["artifact"] = self._publish_result(
                        row, shot, "video/mp4", transport.download(remote_id)
                    )
                    shot.update(state="SUCCEEDED", completed_at=time.time())
                elif status in {"queued", "pending", "running", "in_progress", "processing"}:
                    active_remote = True
                    shot["state"] = "SUBMISSION_UNKNOWN"
                elif status in {"failed", "cancelled", "canceled", "expired"}:
                    definite_failure = True
                    shot.update(submission_phase="REJECTED", state="FAILED",
                                error="H3 original pair job ended without a video")
                else:
                    raise PairRequestUnknown("H3 pair job status is unknown; original request is retained")
            if active_remote:
                row.update(
                    state="SUBMISSION_UNKNOWN",
                    error="双节点原任务仍在排队或生成；已保留原 pair_job_id，不会重发或换组",
                    cleanup_required=False,
                    memory_released=False,
                    resource_status="remote_job_unknown",
                )
            else:
                self._release_pair_claim(row)
                row["state"] = (
                    "SUCCEEDED" if all(shot.get("state") == "SUCCEEDED" for shot in row["shots"])
                    else "FAILED" if definite_failure else "INTERRUPTED"
                )
                row["error"] = None if row["state"] == "SUCCEEDED" else row.get("error")
        except (PairRequestUnknown, RuntimeError, OSError, TimeoutError) as exc:
            row.update(
                state="SUBMISSION_UNKNOWN",
                error="双节点原任务暂时无法查询；已保留原 pair_job_id，不会重发或换组",
                cleanup_required=False,
                memory_released=False,
                resource_status="remote_job_unknown",
            )
            raise BlockingIOError(row["error"]) from exc
        finally:
            self.save(row)
            with self.app._submit_lock:
                self._reconciling.discard(job_id)
                if row.get("state") != "SUBMISSION_UNKNOWN":
                    self._release_slot(row)
                    self._clear_active(job_id)
        return self.get(job_id)

    def _reconcile_ring4(self, job_id):
        """Query/download an existing Ring4 job. This method never submits inference."""
        from .video_ring_transport import Ring4Unknown
        with self.app._submit_lock:
            row = self.get(job_id)
            if self.active not in {None, job_id} or job_id in self._running or job_id in self._reconciling:
                raise BlockingIOError("video batch does not own idle Ring4 reconciliation")
            self.active = job_id
            self._pending_recovery.add(job_id)
            self._reconciling.add(job_id)
            row["state"] = "RECONCILING"
            self.save(row)
        active_remote = False
        definite_failure = False
        try:
            transport = self._ring_transport()
            for shot in row["shots"]:
                if shot.get("artifact") or shot.get("state") == "SUCCEEDED":
                    continue
                remote_id = shot.get("ring_job_id")
                if not remote_id or shot.get("submission_phase") not in {"REQUEST_STARTED", "UNKNOWN", "RECEIVED"}:
                    continue
                status = transport.poll(remote_id).get("status")
                shot.update(ring_status=status, ring_status_updated_at=time.time())
                if status == "completed":
                    shot["artifact"] = self._publish_result(
                        row, shot, "video/mp4", transport.download(remote_id)
                    )
                    shot.update(state="SUCCEEDED", completed_at=time.time())
                elif status in {"queued", "pending", "running", "in_progress", "processing"}:
                    active_remote = True
                    shot["state"] = "SUBMISSION_UNKNOWN"
                elif status in {"failed", "cancelled", "canceled", "expired"}:
                    definite_failure = True
                    shot.update(submission_phase="REJECTED", state="FAILED", error="Ring4 original job ended without a video")
                else:
                    raise Ring4Unknown("Ring4 job status is unknown; original job is retained")

            if active_remote:
                row.update(
                    state="RUNNING",
                    error=None,
                    cleanup_required=False,
                    memory_released=False,
                )
            else:
                self._release_ring4_claim(row)
                row["state"] = (
                    "SUCCEEDED" if all(shot.get("state") == "SUCCEEDED" for shot in row["shots"])
                    else "FAILED" if definite_failure else "INTERRUPTED"
                )
                row["error"] = None if row["state"] == "SUCCEEDED" else row.get("error")
        except (Ring4Unknown, RuntimeError, OSError, TimeoutError) as exc:
            row.update(
                state="SUBMISSION_UNKNOWN",
                error="Ring4 原任务暂时无法查询；已保留原 ring_job_id，不会重发",
                cleanup_required=False,
                memory_released=False,
            )
            raise BlockingIOError(row["error"]) from exc
        finally:
            self.save(row)
            with self.app._submit_lock:
                self._reconciling.discard(job_id)
                if row.get("state") not in {"SUBMISSION_UNKNOWN", "RUNNING"}:
                    self._clear_active(job_id)
                elif active_remote and row.get("state") == "RUNNING":
                    self._running.add(job_id)
                    for shot in row["shots"]:
                        if shot.get("ring_status") in {"queued", "pending", "running", "in_progress", "processing"}:
                            shot.update(state="RUNNING", submission_phase="RECEIVED", error=None)
                    self.save(row)
                    threading.Thread(target=self._resume_ring4_monitor,
                                     args=(row,), daemon=True).start()
        return self.get(job_id)

    def _resume_ring4_monitor(self, row):
        """Finish recovery by observing the original ids, without starting a model."""
        job_id = row["job_id"]
        try:
            transport = self._ring_transport()
            for shot in row["shots"]:
                if shot.get("artifact") or shot.get("state") == "FAILED":
                    continue
                remote_id = shot.get("ring_job_id")
                if not remote_id:
                    raise SubmissionUnknown("Unsubmitted clips require explicit continuation")
                shot["artifact"] = self._monitor_ring4(row, shot, transport, remote_id)
                shot.update(state="SUCCEEDED", completed_at=time.time(), error=None)
                self.save(row)
            self._release_ring4_claim(row)
            row.update(state="SUCCEEDED" if all(s.get("artifact") for s in row["shots"]) else "FAILED", error=None)
        except JobCanceled:
            for shot in row["shots"]:
                if not shot.get("artifact"):
                    shot.update(state="CANCELED", submission_phase="CANCELED")
            row.update(state="CANCELED", error=None)
        except Exception as exc:
            for shot in row["shots"]:
                if shot.get("state") == "RUNNING":
                    shot.update(state="SUBMISSION_UNKNOWN", submission_phase="UNKNOWN")
            row.update(state="SUBMISSION_UNKNOWN", memory_released=False,
                       error=str(exc) if isinstance(exc, (SubmissionUnknown, ValueError))
                       else "Ring4 recovery query failed; original job is retained")
        finally:
            self.save(row)
            with self.app._submit_lock:
                self._running.discard(job_id)
                if row["state"] != "SUBMISSION_UNKNOWN":
                    self._clear_active(job_id)
