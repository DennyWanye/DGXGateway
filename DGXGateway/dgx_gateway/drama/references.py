"""Persistent image references and candidates, sharing the existing Gateway authority."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time

from PIL import Image

from ..image_assets import _read_regular
from .contracts import DramaError, fields, identifier, integer, text
from .gateway_adapter import GatewayRejected, GatewayBusy, IMAGE_TOOLS, MAX_IMAGE_BYTES, input_asset_ids, validate_parameters
from .production import ROLES
from .restore import restore_guard
from .store import encode, new_id


TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED", "INTERRUPTED"}
MAX_IMAGE_PIXELS = 32 * 1024 * 1024


def decode_image(data: bytes) -> dict:
    """Verify the container and decode every pixel before accepting an image."""
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMAGE_BYTES:
        raise DramaError("invalid_image", "图片必须为不超过8MiB的PNG、JPEG或WebP")
    formats = {"PNG": ("png", "image/png"), "JPEG": ("jpg", "image/jpeg"), "WEBP": ("webp", "image/webp")}
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in formats or getattr(image, "n_frames", 1) != 1:
                raise ValueError("unsupported image format")
            width, height = image.size
            if not 0 < width <= 8192 or not 0 < height <= 8192 or width * height > MAX_IMAGE_PIXELS:
                raise ValueError("image exceeds pixel limit")
            extension, content_type = formats[image.format]
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise DramaError("invalid_image", "图片不能完整解码，或超过格式、尺寸限制") from exc
    return {"kind": "image", "contentType": content_type, "extension": extension,
            "width": width, "height": height}


class ReferenceMixin:
    def init_references(self, adapter):
        self.reference_adapter = adapter
        self._reference_adapter = adapter
        self._reference_lock = threading.RLock()
        self._reference_stop = threading.Event()
        self._reference_schedule_lock = threading.Lock()
        self._reference_scheduled = set()
        self._reference_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="drama-image-submit")
        if not getattr(self, "_restore_mode", None):
            with self.store.connect(write=True) as db:
                db.execute("UPDATE dr_reference_jobs SET state='SUBMISSION_UNKNOWN',error=?,updated_at=? "
                           "WHERE state='DISPATCHING' OR (state='PREPARED' AND (gateway_job_id IS NOT NULL OR COALESCE(json_extract(input_snapshot, '$.queueVersion'),0)!=1 OR json_extract(input_snapshot, '$.dispatchStarted')=1))",
                           ("服务重启，先按原提交编号核查；不会自动重提", time.time()))
        self._reference_poller = threading.Thread(target=self._poll_references, daemon=True,
                                                  name="drama-image-query")
        self._reference_poller.start()

    def close_references(self):
        self._reference_stop.set()
        self._reference_poller.join()
        self._reference_executor.shutdown(wait=True)

    def _schedule_reference(self, jid):
        with self._reference_schedule_lock:
            if jid in self._reference_scheduled:
                return
            self._reference_scheduled.add(jid)
        def run():
            try:
                self._dispatch_reference(jid)
            finally:
                with self._reference_schedule_lock:
                    self._reference_scheduled.discard(jid)
        self._reference_executor.submit(run)

    def _poll_references(self):
        while not self._reference_stop.wait(2):
            if getattr(self, "_restore_mode", None):
                continue
            if self._reference_adapter is None:
                continue
            with self.store.connect() as db:
                ids = [row[0] for row in db.execute(
                    "SELECT id FROM dr_reference_jobs WHERE state NOT IN ('SUCCEEDED','FAILED','CANCELED','INTERRUPTED') OR json_extract(input_snapshot, '$.gatewayStatus.memoryReleased')=0 OR json_extract(input_snapshot, '$.gatewayStatus.cleanupRequired')=1")]
            for jid in ids:
                if self._reference_stop.is_set():
                    break
                try:
                    job = self.refresh_reference_job(jid)["job"]
                    if job["state"] == "PREPARED":
                        with self.store.connect() as db:
                            first = self.generation_queue(db)
                        if first and first[0][1:] == ("image", jid):
                            self._schedule_reference(jid)
                except Exception:
                    # The job retains its last durable state. A later query is
                    # safe; this loop never retries a submission or cancellation.
                    continue

    def _archive_image(self, data: bytes) -> tuple[str, dict]:
        metadata = decode_image(data)
        descriptor, name = tempfile.mkstemp(prefix="reference-", dir=self.root / "temp")
        path = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            return self._publish(path, suffix="." + metadata["extension"]), metadata
        finally:
            path.unlink(missing_ok=True)

    def upload_image(self, pid, stream, length, filename, key):
        integer(length, "图片字节数", 1, MAX_IMAGE_BYTES)
        filename = text(filename, "图片名", 255)
        text(key, "requestKey", 128)
        with self.store.connect() as db:
            self.store.one(db, "dr_projects", identifier(pid))
        data = bytearray()
        while len(data) < length:
            part = stream.read(min(1024 * 1024, length - len(data)))
            if not part or len(part) > length - len(data):
                raise DramaError("upload_truncated", "图片上传不完整")
            data.extend(part)
        aid, metadata = self._archive_image(bytes(data))
        def save(db):
            self._bind_asset(db, pid, aid, filename, metadata, length)
            return {"asset": self._asset_view(self.store.one(db, "dr_assets", aid))}
        return self.store.mutate(f"image-upload:{pid}", key, {"sha256": aid, "filename": filename}, save)

    def _image_library_paths(self):
        root = self.repo_root / "creative-projects"
        result = {}
        if not root.is_dir() or root.is_symlink():
            return result
        # Fixed project media folders only; never recurse into arbitrary state.
        for folder in ("images", "references", "candidates"):
            for path in sorted(root.glob(f"*/{folder}/*")):
                if len(result) >= 500:
                    return result
                if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                    continue
                if any(part.is_symlink() for part in (path, path.parent, path.parent.parent)):
                    continue
                if path.is_file() and root.resolve() in path.resolve().parents:
                    result[hashlib.sha256(str(path).encode()).hexdigest()] = path
        return result

    def image_library(self):
        return {"sources": [{"sourceId": sid, "title": path.name} for sid, path in self._image_library_paths().items()]}

    def import_image_source(self, pid, body):
        fields(body, {"sourceId", "requestKey"}, {"sourceId", "requestKey"})
        path = self._image_library_paths().get(identifier(body["sourceId"]))
        if path is None:
            raise DramaError("source_not_found", "图片不在已配置素材库中", 404)
        try:
            data = _read_regular(path, MAX_IMAGE_BYTES)
        except (OSError, ValueError) as exc:
            raise DramaError("invalid_image", "素材库图片不可读取或超过8MiB") from exc
        return self.upload_image(pid, io.BytesIO(data), len(data), path.name, body["requestKey"])

    def _reference_row(self, db, jid):
        row = db.execute("SELECT * FROM dr_reference_jobs WHERE id=?", (identifier(jid),)).fetchone()
        if row is None:
            raise DramaError("not_found", "找不到参考图片任务", 404)
        return dict(row)

    def _reference_view(self, db, row):
        context = self.get_revision_context(row["target_type"], row["target_revision_id"], db=db)
        stale = context["currentRevisionId"] != row["target_revision_id"]
        snapshot = json.loads(row["input_snapshot"])
        gateway = snapshot.get("gatewayStatus", {})
        batch = gateway.get("imageBatch")
        if batch:
            batch = {**batch, 'modelName': gateway.get('modelName')}
        identities = {c.get("sourceKey"): c for c in (batch or {}).get("candidates", []) if c.get("sourceKey")}
        candidates = []
        for candidate in db.execute("SELECT * FROM dr_reference_candidates WHERE job_id=? ORDER BY created_at,id", (row["id"],)):
            candidates.append({"id": candidate["id"], "assetId": candidate["asset_id"],
                               "sourceKey": candidate["source_key"],
                               "index": identities.get(candidate["source_key"], {}).get("index"),
                               "seed": identities.get(candidate["source_key"], {}).get("seed"),
                               "seedText": identities.get(candidate["source_key"], {}).get("seedText"),
                               "targetType": candidate["target_type"], "targetId": candidate["target_id"],
                               "targetRevisionId": candidate["target_revision_id"], "jobId": row["id"], "stale": stale,
                               "asset": self._asset_view(self._bound_asset(db, row["project_id"], candidate["asset_id"]))})
        return {"id": row["id"], "projectId": row["project_id"], "targetType": row["target_type"],
                "targetId": row["target_id"], "targetRevisionId": row["target_revision_id"],
                "tool": row["tool"], "parameters": json.loads(row["parameters"]), "state": row["state"],
                "error": row["error"], "gatewayJobId": row["gateway_job_id"], "candidates": candidates,
                "imageBatch": batch, "retryOf": snapshot.get("retryOf"),
                "referenceRole": snapshot.get("referenceRole") or context["snapshot"].get("referenceRole"),
                "resourceSettled": gateway.get("resourceSettled", False), "resourceStatus": gateway.get("resourceStatus", "unsettled"),
                "memoryReleased": gateway.get("memoryReleased"),
                "cleanupRequired": gateway.get("cleanupRequired", False),
                "stale": stale, "createdAt": row["created_at"], "updatedAt": row["updated_at"]}

    def reference_job(self, jid):
        with self.store.connect() as db:
            return {"job": self._reference_view(db, self._reference_row(db, jid))}

    def reference_jobs(self, pid):
        with self.store.connect() as db:
            self.store.one(db, "dr_projects", identifier(pid))
            return {"jobs": [self._reference_view(db, row) for row in db.execute(
                "SELECT * FROM dr_reference_jobs WHERE project_id=? ORDER BY created_at DESC", (pid,))]}

    @staticmethod
    def _review_notes(value):
        if value is None:
            return ""
        if not isinstance(value, str) or len(value) > 2000 or "\x00" in value:
            raise DramaError("invalid_input", "审核意见必须是不超过2000个字符的文本")
        return value.strip()

    def image_review_queue(self, pid, episode_id=None, scope="episode"):
        if scope not in {"episode", "project"}:
            raise DramaError("invalid_scope", "审图范围必须为episode或project")
        with self.store.connect() as db:
            self.store.one(db, "dr_projects", identifier(pid))
            if scope == "episode":
                if not episode_id:
                    raise DramaError("invalid_input", "按分集审图必须提供episodeId")
                episode = self.store.one(db, "dr_episodes", identifier(episode_id))
                if episode["project_id"] != pid:
                    raise DramaError("wrong_project", "分集不属于当前项目", 404)
            elif episode_id:
                episode = self.store.one(db, "dr_episodes", identifier(episode_id))
                if episode["project_id"] != pid:
                    raise DramaError("wrong_project", "分集不属于当前项目", 404)

            reviews = {row["candidate_id"]: dict(row) for row in db.execute(
                "SELECT * FROM dr_reference_candidate_reviews WHERE project_id=?", (pid,))}
            items = []
            query = """SELECT c.*,j.tool,j.created_at AS job_created_at,j.input_snapshot,j.parameters
                         FROM dr_reference_candidates c JOIN dr_reference_jobs j ON j.id=c.job_id
                         WHERE c.project_id=? ORDER BY j.created_at DESC,c.created_at,c.id"""
            for candidate in db.execute(query, (pid,)):
                candidate = dict(candidate)
                if candidate["target_type"] == "shot":
                    shot = self.store.one(db, "dr_shots", candidate["target_id"])
                    episode_ids = [shot["episode_id"]]
                    snapshot = json.loads(self.store.one(db, "dr_shot_revisions", candidate["target_revision_id"])["snapshot"])
                    target_name = snapshot.get("title", "未命名镜头")
                    role = snapshot.get("referenceRole")
                    current_snapshot = json.loads(self.store.one(db, "dr_shot_revisions", shot["current_content_revision_id"])["snapshot"])
                    selected = current_snapshot.get("approvedReferenceAssetId") == candidate["asset_id"]
                    selected_approved = selected
                    stale = shot["current_content_revision_id"] != candidate["target_revision_id"]
                    scope_kind = "episode"
                    current_revision, current_revision_id, target_kind = shot["revision"], shot["current_content_revision_id"], "shot"
                else:
                    entity = self.store.one(db, "dr_entities", candidate["target_id"])
                    snapshot = json.loads(self.store.one(db, "dr_entity_revisions", candidate["target_revision_id"])["snapshot"])
                    target_name = snapshot.get("name", "未命名资料")
                    role = json.loads(candidate["input_snapshot"]).get("referenceRole") or snapshot.get("referenceRole")
                    episode_ids = sorted({row[0] for row in db.execute(
                        """SELECT s.episode_id FROM dr_dependencies d
                           JOIN dr_shots s ON s.current_content_revision_id=d.shot_revision_id
                           JOIN dr_entity_revisions r ON r.id=d.entity_revision_id
                           WHERE r.entity_id=?""", (candidate["target_id"],))})
                    current = json.loads(self.store.one(db, "dr_entity_revisions", entity["current_revision_id"])["snapshot"])
                    selected = any(ref.get("assetId") == candidate["asset_id"] and ref.get("role") == role for ref in current.get("references", []))
                    selected_approved = selected and entity["approved_revision_id"] == entity["current_revision_id"]
                    stale = entity["current_revision_id"] != candidate["target_revision_id"]
                    scope_kind = "unlinked" if not episode_ids else ("episode" if len(episode_ids) == 1 else "shared")
                    current_revision, current_revision_id, target_kind = entity["revision"], entity["current_revision_id"], entity["kind"]
                if scope == "episode" and episode_id not in episode_ids and episode_ids:
                    continue
                episode_titles = [self.store.one(db, "dr_episodes", value)["title"] for value in episode_ids]
                gateway = json.loads(candidate["input_snapshot"]).get("gatewayStatus", {})
                batch = gateway.get("imageBatch") or {}
                identity = next((row for row in batch.get("candidates", []) if row.get("sourceKey") == candidate["source_key"]), {})
                review = reviews.get(candidate["id"])
                parameters = json.loads(candidate["parameters"] or "{}")
                prompt = parameters.get("prompt") if isinstance(parameters.get("prompt"), str) else ""
                excerpt = " ".join(prompt.split())[:80]
                items.append({"id": candidate["id"], "candidateId": candidate["id"], "assetId": candidate["asset_id"],
                              "index": identity.get("index"), "seed": identity.get("seed"), "jobId": candidate["job_id"],
                              "targetType": candidate["target_type"], "targetId": candidate["target_id"],
                              "targetRevisionId": candidate["target_revision_id"], "targetName": target_name,
                              "targetKind": target_kind, "role": role, "model": gateway.get("modelName") or candidate["tool"],
                              "createdAt": candidate["created_at"], "jobCreatedAt": candidate["job_created_at"],
                              "promptExcerpt": excerpt,
                              "currentRevision": current_revision, "currentRevisionId": current_revision_id,
                              "episodeIds": episode_ids, "episodeTitles": episode_titles, "scopeKind": scope_kind,
                              "reviewStatus": review["decision"] if review else "pending",
                              "notes": review["notes"] if review else "", "selected": selected,
                              "selectedApproved": selected_approved, "stale": stale})
            return {"items": items, "scope": scope, "episodeId": episode_id if scope == "episode" else None}

    def review_image_candidates(self, pid, body):
        restore_guard(self, "write")
        fields(body, {"candidateIds", "decision", "notes", "requestKey"}, {"candidateIds", "decision", "requestKey"})
        candidate_ids = body["candidateIds"]
        if (not isinstance(candidate_ids, list) or not candidate_ids or len(candidate_ids) > 2000
                or any(not isinstance(value, str) for value in candidate_ids) or len(set(candidate_ids)) != len(candidate_ids)):
            raise DramaError("invalid_candidates", "候选必须是1至2000个不重复的候选编号")
        candidate_ids = [identifier(value) for value in candidate_ids]
        if not isinstance(body["decision"], str) or body["decision"] not in {"approved", "rejected"}:
            raise DramaError("invalid_review", "图片审核结论须为approved或rejected")
        notes = self._review_notes(body.get("notes"))
        def save(db):
            self.store.one(db, "dr_projects", identifier(pid))
            marks = ",".join("?" for _ in candidate_ids)
            rows = [dict(row) for row in db.execute(
                f"SELECT id,project_id FROM dr_reference_candidates WHERE id IN ({marks})", candidate_ids)]
            if len(rows) != len(candidate_ids) or any(row["project_id"] != pid for row in rows):
                raise DramaError("wrong_project", "候选不属于当前项目", 404)
            now = time.time()
            for candidate_id in candidate_ids:
                db.execute("""INSERT INTO dr_reference_candidate_reviews(candidate_id,project_id,decision,notes,updated_at)
                              VALUES(?,?,?,?,?) ON CONFLICT(candidate_id) DO UPDATE SET
                              decision=excluded.decision,notes=excluded.notes,updated_at=excluded.updated_at""",
                           (candidate_id, pid, body["decision"], notes, now))
            return {"reviews": [{"candidateId": candidate_id, "decision": body["decision"], "notes": notes, "updatedAt": now}
                                for candidate_id in candidate_ids]}
        return self.store.mutate(f"image-reviews:{pid}", body["requestKey"], body, save)

    def _reference_inputs(self, db, pid, tool, parameters):
        ids = input_asset_ids(tool, parameters)
        for aid in ids:
            metadata = json.loads(self._bound_asset(db, pid, aid)["metadata"])
            if metadata.get("kind") != "image":
                raise DramaError("invalid_reference", "参考输入必须是本项目图片")
        return ids

    @staticmethod
    def _require_identity_inputs(context, tool, ids):
        if tool not in {"edit_image", "compose_image_layers"}:
            return
        required = set(context.get("requiredReferenceAssetIds", []))
        approved = context.get("approvedReferenceAssetId")
        if approved:
            required.add(approved)
        if not required.issubset(ids):
            raise DramaError("approved_reference_required", "派生编辑或合成必须实际包含全部已批准身份参考图")

    def recover_reference_artifacts(self, jid):
        restore_guard(self, "write")
        with self._generation_lock, self._reference_lock:
            job = self.reference_job(jid)["job"]
            if (job["memoryReleased"] is not True or job["cleanupRequired"] or not job["gatewayJobId"] or
                    not any(c.get("error") == "Saved image download requires recovery" for c in (job.get("imageBatch") or {}).get("candidates", []))):
                raise DramaError("recovery_not_available", "仅可恢复已保存但下载失败的图片；其他未知状态请核查原任务", 409)
            if self._reference_adapter is None or not hasattr(self._reference_adapter, "recover"):
                raise DramaError("generation_unavailable", "Gateway归档恢复适配器未配置", 503)
            record = self._reference_adapter.recover(job["gatewayJobId"])
            self._accept_reference_record(jid, record)
            return self.reference_job(jid)

    def retry_reference_job(self, jid, body):
        fields(body, {"candidateIndices", "requestKey"}, {"candidateIndices", "requestKey"})
        indices = body["candidateIndices"]
        if (not isinstance(indices, list) or not 1 <= len(indices) <= 8 or
                any(type(i) is not int or not 0 <= i < 8 for i in indices) or len(set(indices)) != len(indices)):
            raise DramaError("invalid_retry", "请选择需要重新制作的失败候选")
        with self.store.connect() as db:
            parent = self._reference_row(db, jid)
        parameters = json.loads(parent["parameters"])
        frozen = json.loads(parent["input_snapshot"])
        parameters.update(batch_count=len(indices), candidate_indices=sorted(indices))
        return self.submit_reference_job(parent["target_type"], parent["target_revision_id"], {
            "tool": parent["tool"], "parameters": parameters, "requestKey": body["requestKey"],
            **({"referenceRole": frozen["referenceRole"]} if frozen.get("referenceRole") else {})},
            _retry={"retryOf": jid, "retryIndices": sorted(indices)})

    def submit_reference_job(self, kind, rid, body, *, _retry=None):
        restore_guard(self, "write")
        fields(body, {"tool", "parameters", "requestKey", "referenceRole"}, {"tool", "parameters", "requestKey"})
        if self._reference_adapter is None:
            raise DramaError("generation_unavailable", "图片Gateway适配器未配置", 503)
        try:
            parameters = validate_parameters(body["tool"], body["parameters"])
        except (ValueError, TypeError) as exc:
            raise DramaError("invalid_parameters", "图片工具参数不符合已验证能力") from exc
        context = self.get_revision_context(kind, rid)
        pid = context["projectId"]
        def save(db):
            current = self.validate_reference_inputs(kind, rid, db=db, reference_role=body.get("referenceRole"))
            ids = self._reference_inputs(db, pid, body["tool"], parameters)
            self._require_identity_inputs(current, body["tool"], ids)
            if db.execute("SELECT COUNT(*) FROM dr_reference_jobs WHERE state IN ('PREPARED','DISPATCHING')").fetchone()[0] >= 32:
                raise DramaError("image_queue_full", "图片提交队列已满", 429)
            requested_role = body.get("referenceRole")
            if requested_role is not None:
                if kind != "entity":
                    raise DramaError("invalid_reference_role", "镜头任务不能指定资料参考用途")
                entity = self.store.one(db, "dr_entities", current["targetId"])
                if requested_role not in ROLES[entity["kind"]]:
                    raise DramaError("invalid_reference_role", "参考用途与实体类型不符")
            retry_snapshot = {}
            if requested_role:
                retry_snapshot["referenceRole"] = requested_role
                retry_snapshot["identityReferencePolicy"] = "approved_target_slot_v1"
            if _retry:
                parent = self._reference_row(db, _retry["retryOf"])
                frozen = json.loads(parent["input_snapshot"])
                status = frozen.get("gatewayStatus", {})
                states = {c["index"]: c["state"] for c in (status.get("imageBatch") or {}).get("candidates", [])}
                if (parent["state"] not in TERMINAL or status.get("memoryReleased") is not True or
                        status.get("cleanupRequired") or any(states.get(i) not in {"FAILED", "CANCELED"} for i in _retry["retryIndices"])):
                    raise DramaError("retry_not_safe", "仅可重拍已释放资源任务中的确定失败候选；成功或状态未知请核查原任务", 409)
                for child in db.execute("SELECT id,input_snapshot FROM dr_reference_jobs WHERE project_id=?", (pid,)):
                    other = json.loads(child["input_snapshot"])
                    if other.get("retryOf") == parent["id"] and set(other.get("retryIndices", [])) & set(_retry["retryIndices"]):
                        raise DramaError("retry_already_exists", "这些候选已有重拍任务，请查看原重拍记录：" + child["id"], 409)
                retry_snapshot = dict(_retry)
                if frozen.get("referenceRole"):
                    retry_snapshot["referenceRole"] = frozen["referenceRole"]
                    retry_snapshot["identityReferencePolicy"] = "approved_target_slot_v1"
            jid, now = new_id(), time.time()
            db.execute("INSERT INTO dr_reference_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (jid, pid, kind, current["targetId"], rid, body["tool"], encode(parameters),
                        encode({"context": current, "inputAssetIds": ids, "queueVersion": 1, "dispatchStarted": False, **retry_snapshot}), "drama-reference-" + jid,
                        None, "PREPARED", None, now, now))
            return {"jobId": jid}
        result = self.store.mutate(f"reference-submit:{pid}", body["requestKey"],
                                   {"kind": kind, "revisionId": rid, **body, **({"retry": _retry} if _retry else {})}, save)
        jid = result["jobId"]
        # _dispatch claims PREPARED atomically. Duplicate scheduling cannot POST twice.
        self._schedule_reference(jid)
        return self.reference_job(jid)

    def _dispatch_reference(self, jid):
        restore_guard(self, "write")
        with self._generation_lock, self._reference_lock:
            with self.store.connect() as db:
                row = self._reference_row(db, jid)
                if row["state"] != "PREPARED" or not self.generation_turn(db, "image", jid):
                    return
            dispatch_started = False
            try:
                parameters = json.loads(row["parameters"])
                images = []
                with self.store.connect() as db:
                    for aid in input_asset_ids(row["tool"], parameters):
                        asset = self._bound_asset(db, row["project_id"], aid)
                        metadata = json.loads(asset["metadata"])
                        data = _read_regular(self.root / "assets" / (aid + "." + metadata["extension"]), MAX_IMAGE_BYTES)
                        decode_image(data)
                        if hashlib.sha256(data).hexdigest() != aid:
                            raise GatewayRejected("Source image changed")
                        images.append({"assetId": aid, "filename": f"reference-{aid}.{metadata['extension']}", "data": data})

                def gate():
                    nonlocal dispatch_started
                    with self.store.connect(write=True) as db:
                        live = self._reference_row(db, jid)
                        if live["state"] != "PREPARED":
                            raise GatewayRejected("Task is no longer prepared")
                        frozen = json.loads(row["input_snapshot"])
                        role = frozen.get("referenceRole") if frozen.get("identityReferencePolicy") == "approved_target_slot_v1" else None
                        current = self.validate_reference_inputs(row["target_type"], row["target_revision_id"], db=db, reference_role=role)
                        prior = frozen["context"]
                        ids = self._reference_inputs(db, row["project_id"], row["tool"], parameters)
                        if (ids != frozen["inputAssetIds"]
                                or current.get("approvedReferenceAssetId") != prior.get("approvedReferenceAssetId")
                                or set(current.get("requiredReferenceAssetIds", [])) != set(prior.get("requiredReferenceAssetIds", []))):
                            raise DramaError("reference_changed", "排队期间批准参考已变化，请刷新并创建新请求", 409)
                        self._require_identity_inputs(current, row["tool"], ids)
                        frozen["dispatchStarted"] = True
                        db.execute("UPDATE dr_reference_jobs SET state='DISPATCHING',input_snapshot=?,updated_at=? WHERE id=?", (encode(frozen), time.time(), jid))
                    dispatch_started = True

                if hasattr(self._reference_adapter, "submit_with_gate"):
                    record = self._reference_adapter.submit_with_gate(row["tool"], parameters, row["dispatch_key"], images, gate)
                else:
                    gate()
                    record = self._reference_adapter.submit(row["tool"], parameters, row["dispatch_key"], images)
                self._accept_reference_record(jid, record)
            except GatewayBusy:
                with self.store.connect(write=True) as db:
                    db.execute("UPDATE dr_reference_jobs SET state='PREPARED',input_snapshot=json_set(input_snapshot,'$.dispatchStarted',json('false')),error=?,updated_at=? WHERE id=? AND gateway_job_id IS NULL AND state!='CANCELED'",
                               ("DGX资源繁忙，已保存并等待执行", time.time(), jid))
            except Exception as exc:
                state = "FAILED" if not dispatch_started or isinstance(exc, (GatewayRejected, DramaError)) else "SUBMISSION_UNKNOWN"
                message = (exc.message if isinstance(exc, DramaError) else "图片输入或提交被拒绝") if state == "FAILED" else "提交未确认，请核查原任务；不会自动重提"
                with self.store.connect(write=True) as db:
                    db.execute("UPDATE dr_reference_jobs SET state=?,error=?,updated_at=? WHERE id=? AND state!='CANCELED'",
                               (state, message, time.time(), jid))

    def _accept_reference_record(self, jid, record):
        gid = identifier(record.get("job_id"))
        with self.store.connect(write=True) as db:
            row = self._reference_row(db, jid)
            if row["gateway_job_id"] and row["gateway_job_id"] != gid:
                raise DramaError("gateway_identity_conflict", "Gateway返回了不同任务身份", 409)
            other = db.execute("SELECT id FROM dr_reference_jobs WHERE gateway_job_id=? AND id!=?",
                               (gid, jid)).fetchone()
            if other:
                raise DramaError("gateway_identity_conflict", "Gateway任务已绑定另一个制作目标", 409)
            db.execute("UPDATE dr_reference_jobs SET gateway_job_id=?,updated_at=? WHERE id=?", (gid, time.time(), jid))
        outputs = self._reference_adapter.outputs(row["tool"], record)
        if not isinstance(outputs, list) or len(outputs) > 8:
            raise DramaError("invalid_outputs", "图片任务返回了超出范围的产物")
        invalid = False
        for item in outputs:
            try:
                source_key = text(item["sourceKey"], "产物编号", 255)
                data = item["data"]
                if not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMAGE_BYTES:
                    raise DramaError("invalid_image", "图片产物大小或数据不正确")
                digest = hashlib.sha256(data).hexdigest()
                with self.store.connect() as db:
                    old = db.execute("SELECT asset_id FROM dr_reference_candidates WHERE job_id=? AND source_key=?", (jid, source_key)).fetchone()
                if old:
                    if old[0] != digest:
                        raise DramaError("artifact_changed", "同一产物身份返回了不同图片", 409)
                    # Content-addressed candidates were already fully decoded.
                    # Polling model release need not decode and write them again.
                    with self.store.connect() as db:
                        asset = self._bound_asset(db, row["project_id"], digest)
                    extension = json.loads(asset["metadata"])["extension"]
                    try:
                        archived = _read_regular(self.root / "assets" / (digest + "." + extension), MAX_IMAGE_BYTES)
                    except FileNotFoundError:
                        archived = None
                    if archived is not None:
                        if hashlib.sha256(archived).hexdigest() != digest:
                            raise DramaError("asset_corrupt", "归档图片哈希不匹配，原文件未覆盖", 409)
                        continue
                    # Restore a missing immutable file from the verified source.
                aid, metadata = self._archive_image(item["data"])
                filename = text(item["filename"], "图片名", 255)
                with self.store.connect(write=True) as db:
                    old = db.execute("SELECT asset_id FROM dr_reference_candidates WHERE job_id=? AND source_key=?", (jid, source_key)).fetchone()
                    if old and old[0] != aid:
                        raise DramaError("artifact_changed", "同一产物身份返回了不同图片", 409)
                    self._bind_asset(db, row["project_id"], aid, filename, metadata, len(item["data"]))
                    db.execute("INSERT OR IGNORE INTO dr_reference_candidates VALUES (?,?,?,?,?,?,?,?,?)",
                               (new_id(), row["project_id"], row["target_type"], row["target_id"], row["target_revision_id"], aid, jid, source_key, time.time()))
            except (DramaError, OSError, ValueError, KeyError, TypeError):
                invalid = True
        upstream = record.get("state")
        if upstream not in {"QUEUED", "LOADING", "UPLOADING", "SUBMITTING", "RUNNING", "RELEASING", "RECONCILING", "CANCELING", *TERMINAL}:
            upstream = "SUBMITTED"
        error = None
        imported = json.loads(row["input_snapshot"]).get("imported", False)
        expected = None if imported else json.loads(row["parameters"]).get("batch_count", 1)
        with self.store.connect() as db:
            collected = db.execute("SELECT COUNT(*) FROM dr_reference_candidates WHERE job_id=?", (jid,)).fetchone()[0]
        if invalid or (upstream == "SUCCEEDED" and (not collected or (expected is not None and collected != expected))):
            upstream, error = "FAILED", "部分图片缺失或不能完整验证；已验证候选仍保留"
        elif upstream in {"FAILED", "INTERRUPTED"}:
            error = "Gateway图片任务失败或中断，请核查已有任务；不会自动重提"
        elif row["state"] == "CANCEL_REQUESTED" and upstream not in TERMINAL:
            upstream = "CANCEL_REQUESTED"
        with self.store.connect(write=True) as db:
            snapshot = json.loads(self._reference_row(db, jid)["input_snapshot"])
            snapshot["gatewayStatus"] = {"resourceSettled": record.get("resource_settled", False), "resourceStatus": record.get("resource_status", "unsettled"), "memoryReleased": record.get("memory_released"), "cleanupRequired": record.get("cleanup_required", False),
                                         "modelName": (record.get('runtime') or {}).get('model_name'),
                                         "imageBatch": record.get("image_batch", snapshot.get("gatewayStatus", {}).get("imageBatch"))}
            db.execute("UPDATE dr_reference_jobs SET state=?,error=?,input_snapshot=?,updated_at=? WHERE id=?", (upstream, error, encode(snapshot), time.time(), jid))

    def refresh_reference_job(self, jid):
        restore_guard(self, "query")
        if self._reference_adapter is None:
            return self.reference_job(jid)
        with self._reference_lock:
            with self.store.connect() as db:
                row = self._reference_row(db, jid)
            if row["state"] in {"PREPARED", "DISPATCHING"} and not getattr(self, "_restore_mode", None):
                return self.reference_job(jid)
            try:
                record = (self._reference_adapter.get(row["tool"], row["gateway_job_id"]) if row["gateway_job_id"]
                          else self._reference_adapter.find(row["tool"], row["dispatch_key"]))
                if record:
                    self._accept_reference_record(jid, record)
            except Exception:
                with self.store.connect(write=True) as db:
                    db.execute("UPDATE dr_reference_jobs SET error=?,updated_at=? WHERE id=?",
                               ("暂时无法查询或归档Gateway产物；保留原任务，请稍后核查", time.time(), jid))
        return self.reference_job(jid)

    def cancel_reference_job(self, jid):
        restore_guard(self, "write")
        with self._reference_lock:
            with self.store.connect(write=True) as db:
                row = self._reference_row(db, jid)
                if row["state"] in TERMINAL:
                    return {"job": self._reference_view(db, row)}
                if row["state"] == "CANCEL_REQUESTED":
                    return {"job": self._reference_view(db, row)}
                if row["state"] == "PREPARED":
                    db.execute("UPDATE dr_reference_jobs SET state='CANCELED',updated_at=? WHERE id=?", (time.time(), jid))
                    return {"job": self._reference_view(db, self._reference_row(db, jid))}
                if row["tool"] == "compose_image_layers":
                    raise DramaError("cancel_unsupported", "现有本地图层合成任务不支持中途取消", 409)
                if not row["gateway_job_id"] or self._reference_adapter is None:
                    raise DramaError("submission_unknown", "尚未确认Gateway任务编号，请先核查", 409)
                db.execute("UPDATE dr_reference_jobs SET state='CANCEL_REQUESTED',updated_at=? WHERE id=?", (time.time(), jid))
            try:
                record = self._reference_adapter.cancel(row["tool"], row["gateway_job_id"])
                self._accept_reference_record(jid, record)
            except Exception:
                with self.store.connect(write=True) as db:
                    db.execute("UPDATE dr_reference_jobs SET error=? WHERE id=?", ("取消回执未确认；只查询原任务，不自动重复取消", jid))
        return self.reference_job(jid)

    def import_reference_job(self, pid, body):
        restore_guard(self, "write")
        required = {"targetType", "targetRevisionId", "gatewayJobId", "tool", "requestKey"}
        fields(body, required, required)
        if body["tool"] not in IMAGE_TOOLS:
            raise DramaError("invalid_tool", "不支持此图片工具")
        gid = identifier(body["gatewayJobId"])
        if len(gid) != 32:
            raise DramaError("invalid_gateway_job", "Gateway任务编号必须为32位")
        def save(db):
            self.store.one(db, "dr_projects", identifier(pid))
            context = self.validate_reference_inputs(body["targetType"], body["targetRevisionId"], db=db)
            if context["projectId"] != pid:
                raise DramaError("wrong_project", "制作目标不属于本项目", 404)
            existing = db.execute("SELECT * FROM dr_reference_jobs WHERE gateway_job_id=?", (gid,)).fetchone()
            if existing:
                if existing["tool"] != body["tool"] or existing["project_id"] != pid or existing["target_revision_id"] != body["targetRevisionId"] or existing["target_type"] != body["targetType"]:
                    raise DramaError("wrong_target", "该Gateway任务已经绑定其他项目或制作目标", 409)
                return {"jobId": existing["id"]}
            jid, now = new_id(), time.time()
            db.execute("INSERT INTO dr_reference_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (jid, pid, body["targetType"], context["targetId"], context["revisionId"], body["tool"],
                        encode({}), encode({"context": context, "imported": True}), "drama-import-" + jid,
                        gid, "SUBMITTED", None, now, now))
            return {"jobId": jid}
        response = self.store.mutate(f"reference-import:{pid}", body["requestKey"], body, save)
        return self.refresh_reference_job(response["jobId"])
