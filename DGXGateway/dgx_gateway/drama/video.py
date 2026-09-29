"""Durable versioned H3 batches. Gateway retains exclusive model authority."""
from __future__ import annotations

import fcntl
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
from ..audio_assets import _validate_wav
from .contracts import DramaError, fields, identifier, integer, revision, text
from .gateway_adapter import GatewayRejected, GatewayBusy
from .references import decode_image
from .restore import restore_guard
from .store import encode, new_id
from ..video_contracts import normalize_generation, gateway_generation, STABLE_PROFILE, TEXT_PROFILE, generation_for_references
from ..video_reference_assets import MAX_VIDEO_REFERENCE_BYTES

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED", "INTERRUPTED"}
MAX_AUDIO = 256 * 1024
MAX_RESERVED = 128
OUTPUT_UNAVAILABLE_ERROR = "原片暂不可读取，已保留来源身份；可再次核查"
OFFICIAL_PROMPT_LABELS = (
    "subject_definitions:", "summary:", "retention_analysis:", "detailed_description:",
    "overall_soundscape:", "non_diegetic_music:",
)
NODE_MEDIA = {"image", "video", "audio"}


def saved_official_prompt(snapshot):
    prompt = snapshot.get("prompt") if isinstance(snapshot, dict) else ""
    if not isinstance(prompt, str) or not prompt.strip():
        return ""
    text_value = prompt.strip()
    if all(label in text_value for label in OFFICIAL_PROMPT_LABELS):
        return text_value
    return ""


def reference_nodes(snapshot):
    if not isinstance(snapshot, dict):
        return []
    nodes = snapshot.get("referenceNodes")
    if not isinstance(nodes, list) and isinstance(snapshot.get("content"), dict):
        nodes = snapshot["content"].get("referenceNodes")
    if not isinstance(nodes, list):
        return []
    return [node for node in nodes if isinstance(node, dict)]


def node_asset_ids(snapshot, media):
    return tuple(
        node.get("assetId") for node in reference_nodes(snapshot)
        if node.get("media") == media and isinstance(node.get("assetId"), str) and node.get("assetId")
    )


def h3_png_or_jpeg(data):
    decoded = decode_image(data)
    if decoded["extension"] in {"png", "jpg"}:
        return data, hashlib.sha256(data).hexdigest()
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
    png = buffer.getvalue()
    if decode_image(png)["extension"] != "png":
        raise GatewayRejected("WebP参考图未能转成H3可接受的PNG")
    return png, hashlib.sha256(png).hexdigest()


class VideoMixin:
    def claim_drama_state(self):
        # Lifetime process lock prevents a second scheduler from converting a
        # live dispatch into UNKNOWN or claiming the same local database.
        self._video_owner = (self.root / "scheduler.lock").open("a+b")
        try:
            fcntl.flock(self._video_owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._video_owner.close()
            raise RuntimeError("Drama state directory already has a scheduler")
    def init_video(self, adapter):
        self.video_adapter = adapter
        self._video_lock = threading.RLock()
        self._video_dispatch_lock = threading.RLock()
        self._video_stop = threading.Event()
        self._video_poll_times = {}
        if not getattr(self, "_restore_mode", None):
            with self.store.connect(write=True) as db:
                db.execute("UPDATE dr_video_items SET submission_phase='UNKNOWN',state='SUBMISSION_UNKNOWN' WHERE job_id IN (SELECT id FROM dr_video_jobs WHERE state='DISPATCHING') AND take_id IS NULL")
                db.execute("UPDATE dr_video_jobs SET state='SUBMISSION_UNKNOWN',error=?,updated_at=? WHERE state='DISPATCHING'",
                           ("服务重启，正在按原派发编号核查；不会重拍", time.time()))
        self._video_thread = threading.Thread(target=self._poll_video, daemon=True, name="drama-video-scheduler")
        self._video_thread.start()

    def close_video(self):
        self._video_stop.set()
        self._video_thread.join()
        fcntl.flock(self._video_owner, fcntl.LOCK_UN)
        self._video_owner.close()

    def default_reference_audio(self, pid, body):
        restore_guard(self, "write")
        fields(body, {"requestKey"}, {"requestKey"})
        import io
        import wave
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(bytes(4 * 24000 * 2))
        data = output.getvalue()
        return self.upload_reference_audio(pid, io.BytesIO(data), len(data),
                                           "默认静音参考-4秒.wav", body["requestKey"])

    def upload_reference_audio(self, pid, stream, length, filename, key):
        integer(length, "音频字节数", 1, MAX_AUDIO)
        filename = text(filename, "文件名", 255)
        with self.store.connect() as db:
            self.store.one(db, "dr_projects", identifier(pid))
        data = stream.read(length)
        if len(data) != length:
            raise DramaError("upload_truncated", "音频上传未完成")
        try:
            _validate_wav(data)
        except (ValueError, OSError, EOFError) as exc:
            raise DramaError("invalid_audio", "参考音频须为完整4秒、24kHz、单声道PCM16 WAV，最大256KiB") from exc
        fd, name = tempfile.mkstemp(prefix="audio-", suffix=".wav", dir=self.root / "temp")
        path = Path(name)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            aid = self._publish(path, ".wav")
            metadata = {"kind": "audio", "extension": "wav", "contentType": "audio/wav", "duration": 4,
                        "sampleRate": 24000, "channels": 1, "samples": 96000, "role": "h3_reference"}
            def save(db):
                self._bind_asset(db, pid, aid, filename, metadata, length)
                return {"asset": self._asset_view(self.store.one(db, "dr_assets", aid))}
            return self.store.mutate(f"audio:{pid}", key, {"sha256": aid, "filename": filename}, save)
        finally:
            path.unlink(missing_ok=True)

    def _production_state(self, db, pid):
        self.store.one(db, "dr_projects", identifier(pid))
        row = db.execute("SELECT * FROM dr_production_state WHERE project_id=?", (pid,)).fetchone()
        count = db.execute("SELECT COALESCE(SUM(reserved_shots),0) FROM dr_video_jobs WHERE project_id=?", (pid,)).fetchone()[0]
        seconds = 0
        for job in db.execute("SELECT id,state FROM dr_video_jobs WHERE project_id=? AND reserved_shots>0", (pid,)):
            for item in db.execute("SELECT input_snapshot,submission_phase FROM dr_video_items WHERE job_id=?", (job["id"],)):
                if job["state"] in TERMINAL | {"BLOCKED"} and item["submission_phase"] not in {"UNKNOWN", "REQUEST_STARTED"}:
                    continue
                seconds += normalize_generation(json.loads(item["input_snapshot"]).get("generation"))["requestDurationSeconds"]
        return {"revision": row["revision"] if row else 1, "paused": bool(row["paused"]) if row else False,
                "reservedShots": count, "reservedSeconds": seconds, "maxReservedShots": MAX_RESERVED}

    def production_state(self, pid):
        with self.store.connect() as db:
            return self._production_state(db, pid)

    def set_production_state(self, pid, body):
        fields(body, {"paused", "expectedRevision", "requestKey"}, {"paused", "expectedRevision", "requestKey"})
        if type(body["paused"]) is not bool:
            raise DramaError("invalid_input", "paused必须为布尔值")
        def save(db):
            current = self._production_state(db, pid)
            revision(current["revision"], body["expectedRevision"])
            db.execute("INSERT INTO dr_production_state VALUES (?,?,?) ON CONFLICT(project_id) DO UPDATE SET paused=excluded.paused,revision=excluded.revision",
                       (pid, int(body["paused"]), current["revision"] + 1))
            return self._production_state(db, pid)
        # The final pre-POST gate holds this lock through Gateway acceptance.
        # A successful pause response therefore precedes no new batch POST.
        with self._generation_lock, self._video_dispatch_lock:
            return self.store.mutate(f"production-state:{pid}", body["requestKey"], body, save)

    def _video_snapshot(self, db, eid, item):
        fields(item, {"shotId", "shotRevisionId", "audioAssetId", "actionImageAssetId", "videoAssetId", "audioDescription",
                      "prompt", "seed", "generation", "candidateCount", "promptJobId"},
               {"shotId", "shotRevisionId", "seed"})
        item = {"prompt": "", "audioAssetId": "", **item}
        shot = self.store.one(db, "dr_shots", identifier(item["shotId"]))
        if shot["episode_id"] != eid:
            raise DramaError("wrong_shot", "镜头不属于当前分集")
        context = self.validate_reference_inputs("shot", identifier(item["shotRevisionId"]), db=db)
        if context["targetId"] != shot["id"]:
            raise DramaError("wrong_revision", "修订不属于此镜头")
        image = context["approvedReferenceAssetId"] or ""
        if context["snapshot"].get("approvedReferenceAssetId") and not image:
            raise DramaError("reference_required", "已选择的参考画面尚未批准，请先确认或移除参考画面")
        if image:
            image_meta = json.loads(self._bound_asset(db, context["projectId"], image)["metadata"])
            if image_meta.get("extension") not in {"png", "jpg"}:
                raise DramaError("reference_format", "H3镜头参考目前需要PNG或JPEG")
        action_image = context["snapshot"].get("actionReferenceAssetId") or item.get("actionImageAssetId") or ""
        if action_image:
            action_meta = json.loads(self._bound_asset(db, context["projectId"], identifier(action_image))["metadata"])
            if action_meta.get("extension") not in {"png", "jpg"}:
                raise DramaError("reference_format", "H3动作参考图需要PNG或JPEG")
        video = context["snapshot"].get("videoReferenceAssetId") or item.get("videoAssetId") or ""
        if video:
            video_meta = json.loads(self._bound_asset(db, context["projectId"], identifier(video))["metadata"])
            if video_meta.get("kind") != "video" or not 2 <= float(video_meta.get("duration", 0)) <= 15:
                raise DramaError("reference_format", "H3动作参考视频须为2–15秒MP4")
        saved_audio = context["snapshot"].get("audioReferenceAssetId") or ""
        if saved_audio and item["audioAssetId"] and saved_audio != item["audioAssetId"]:
            raise DramaError("audio_source_changed", "镜头已保存的参考音频与提交输入不一致，请重新读取镜头")
        audio_id = saved_audio or item["audioAssetId"]
        if audio_id:
            audio = self._bound_asset(db, context["projectId"], identifier(audio_id))
            if json.loads(audio["metadata"]).get("role") != "h3_reference":
                raise DramaError("audio_required", "请选择已验证的H3参考WAV")
        audio_description = item.get("audioDescription", context["snapshot"].get("audioDescription", ""))
        if not isinstance(audio_description, str) or len(audio_description) > 2000:
            raise DramaError("invalid_audio_description", "音频文字描述须为2000字以内文本")
        prompt = item["prompt"]
        if not isinstance(prompt, str) or "\x00" in prompt:
            raise DramaError("invalid_input", "视频提示词须为文字，可留空")
        if len(prompt.encode()) > 16384:
            raise DramaError("prompt_limit", "提示词不能超过16384 UTF-8字节")
        if item.get("promptJobId"):
            from .prompts import prepare_video_prompt
            writer = self._text_job_row(db, identifier(item["promptJobId"]))
            payload = json.loads(writer["payload"])
            frozen_prompt_refs = tuple(payload.get(field, "") for field in ("imageAssetId", "actionImageAssetId", "videoAssetId")) + (
                payload.get("audioAssetId", "") if payload.get("soundMode") == "reference" else audio_id,
            )
            current_prompt_refs = (image, action_image, video, audio_id)
            if writer["episode_id"] != eid or writer["kind"] != "draft_video_prompt" or writer["output_status"] != "valid_draft" or payload.get("shotRevisionId") != item["shotRevisionId"] or frozen_prompt_refs != current_prompt_refs:
                raise DramaError("prompt_source_changed", "提示词来源与当前镜头参考不一致，请重新核对")
            attempt = db.execute("SELECT * FROM dr_text_attempts WHERE id=?", (writer["current_attempt_id"],)).fetchone()
            try: compiled, _ = prepare_video_prompt(json.loads(attempt["raw_text"]), payload)
            except (ValueError, TypeError): raise DramaError("prompt_source_invalid", "原提示词结构无效")
            if compiled != prompt: raise DramaError("prompt_source_changed", "提示词已手动修改，请作为人工提示词保存")
        saved_prompt = saved_official_prompt(context["snapshot"])
        if saved_prompt and not item.get("promptJobId"):
            prompt = text(saved_prompt, "视频提示词", 16384)
            if len(prompt.encode()) > 16384:
                raise DramaError("prompt_limit", "提示词不能超过16384 UTF-8字节")
        nodes = reference_nodes(context["snapshot"])
        has_nodes = any(node.get("assetId") and node.get("media") in NODE_MEDIA for node in nodes)
        try:
            generation = generation_for_references(context["snapshot"].get("generation"), bool(image or action_image or video or audio_id or has_nodes))
            if "generation" in item and generation_for_references(item["generation"], True) != generation:
                raise DramaError("generation_mismatch", "生成设置与固定镜头修订不一致", 409)
        except ValueError as exc:
            raise DramaError("invalid_generation", str(exc)) from exc
        count = integer(item.get("candidateCount", 1), "candidateCount", 1, 4)
        return {**item, "candidateCount": count, "imageAssetId": image, "actionImageAssetId": action_image,
                "videoAssetId": video, "audioAssetId": audio_id, "audioDescription": audio_description,
                "prompt": prompt, "generation": generation, "referenceNodes": nodes,
                "seed": integer(item["seed"], "seed", 0, 2147483647), "content": context["snapshot"]}

    def submit_video_batch(self, eid, body, *, continuation_of=None):
        restore_guard(self, "write")
        fields(body, {"shots", "requestKey"}, {"shots", "requestKey"})
        if self.video_adapter is None:
            raise DramaError("video_disabled", "视频Gateway未配置", 503)
        shots = body["shots"]
        if not isinstance(shots, list) or not 1 <= len(shots) <= 32:
            raise DramaError("batch_size", "每批次须包含1至32个镜头")
        def save(db):
            if continuation_of:
                existing = db.execute("SELECT continuation_job_id FROM dr_video_continuations WHERE source_job_id=?", (continuation_of,)).fetchone()
                if existing:
                    return {"job": self._video_view(db, self.store.one(db, "dr_video_jobs", existing[0]))}
                origin = self._video_view(db, self.store.one(db, "dr_video_jobs", continuation_of))
                if not origin["canContinue"]:
                    raise DramaError("cannot_continue", "没有可确认未发送的镜头", 409)
            episode = self.store.one(db, "dr_episodes", identifier(eid))
            pid = episode["project_id"]
            status = self._production_state(db, pid)
            if status["paused"]:
                raise DramaError("production_paused", "项目已暂停，请先恢复制作", 409)
            requested = sum(integer(item.get("candidateCount", 1), "candidateCount", 1, 4) for item in shots if isinstance(item, dict))
            if requested > 32:
                raise DramaError("batch_size", "镜头数乘以候选数量不能超过32个视频")
            if status["reservedShots"] + requested > MAX_RESERVED:
                raise DramaError("queue_limit", "待处理镜头超过128个，请先处理已有任务", 409)
            snapshots = [self._video_snapshot(db, eid, item) for item in shots]
            if not continuation_of and len({item["shotId"] for item in snapshots}) != len(snapshots):
                raise DramaError("duplicate_shot", "同一批次不能重复提交同一镜头")
            snapshots = [{**item, "candidateIndex": candidate + 1,
                          "seed": (item["seed"] + candidate) % 2147483648}
                         for item in snapshots for candidate in range(item["candidateCount"])]
            jid = new_id()
            now = time.time()
            db.execute("INSERT INTO dr_video_jobs(id,project_id,episode_id,dispatch_key,state,reserved_shots,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                       (jid, pid, eid, "drama-video-" + jid, "QUEUED", len(snapshots), now, now))
            actor = self._call_actor()
            for index, snapshot in enumerate(snapshots):
                iid = new_id()
                snapshot["name"] = "shot-" + iid
                db.execute("INSERT INTO dr_video_items(id,job_id,ordinal,shot_id,shot_revision_id,input_snapshot,state) VALUES (?,?,?,?,?,?,?)",
                           (iid, jid, index, snapshot["shotId"], snapshot["shotRevisionId"], encode(snapshot), "QUEUED"))
                self._record_video_generation(db, pid, eid, jid, iid, snapshot, actor=actor, backfilled=False, request_key=body["requestKey"])
            if continuation_of:
                db.execute("INSERT INTO dr_video_continuations VALUES (?,?)", (continuation_of, jid))
            return {"job": self._video_view(db, self.store.one(db, "dr_video_jobs", jid))}
        return self.store.mutate(f"video-batches:{eid}", body["requestKey"], body, save)

    def _call_actor(self):
        from .business_mcp import CALL_SOURCE
        actor = CALL_SOURCE.get()
        if actor not in {"ui_api", "business_mcp"}:
            raise DramaError("audit_incomplete", "生成调用来源未纳入审计合同")
        return actor

    def _record_video_generation(self, db, pid, eid, job_id, item_id, snapshot, *, actor, backfilled, request_key):
        # recorded* keys are not archive contract identities. shotId/takeId would
        # fail backup after the shot row is gone; the prompt and bound assets must remain.
        prompt = snapshot.get("prompt")
        if not isinstance(prompt, str):
            raise DramaError("audit_incomplete", "生成提示词缺失，不能留下无法审计的记录")
        if actor not in {"ui_api", "business_mcp"}:
            raise DramaError("audit_incomplete", "生成调用来源未纳入审计合同")
        generation = snapshot.get("generation") if isinstance(snapshot.get("generation"), dict) else {}
        payload = {
            "actor": actor,
            "backfilled": backfilled,
            "episodeId": eid,
            "jobId": job_id,
            "recordedItemId": item_id,
            "recordedShotId": snapshot["shotId"],
            "recordedShotRevisionId": snapshot["shotRevisionId"],
            "prompt": prompt,
            "imageAssetId": snapshot.get("imageAssetId") or "",
            "actionImageAssetId": snapshot.get("actionImageAssetId") or "",
            "videoAssetId": snapshot.get("videoAssetId") or "",
            "audioAssetId": snapshot.get("audioAssetId") or "",
        }
        if isinstance(snapshot.get("audioDescription"), str) and snapshot["audioDescription"]:
            payload["audioDescription"] = snapshot["audioDescription"]
        if type(snapshot.get("seed")) is int:
            payload["seed"] = snapshot["seed"]
        if type(snapshot.get("candidateIndex")) is int:
            payload["candidateIndex"] = snapshot["candidateIndex"]
        duration = generation.get("requestDurationSeconds")
        if type(duration) is int:
            payload["requestDurationSeconds"] = duration
        profile = generation.get("profile")
        if isinstance(profile, str) and profile:
            payload["profile"] = profile
        if request_key and not backfilled:
            payload["requestKey"] = request_key
        db.execute("INSERT INTO dr_events VALUES (?,?,?,?,?)",
                   (new_id(), f"project:{pid}", "video_generation", encode(payload), time.time()))

    def _video_view(self, db, row):
        items = []
        for item in db.execute("SELECT * FROM dr_video_items WHERE job_id=? ORDER BY ordinal", (row["id"],)):
            snapshot = json.loads(item["input_snapshot"])
            review = db.execute("SELECT decision FROM dr_take_reviews WHERE take_id=?", (item["take_id"],)).fetchone()
            items.append({"id": item["id"], "index": item["ordinal"], "shotId": item["shot_id"],
                          "shotRevisionId": item["shot_revision_id"], "prompt": snapshot["prompt"], "seed": snapshot["seed"],
                          "imageAssetId": snapshot["imageAssetId"], "actionImageAssetId": snapshot.get("actionImageAssetId", ""),
                          "videoAssetId": snapshot.get("videoAssetId", ""), "audioAssetId": snapshot["audioAssetId"],
                          "audioDescription": snapshot.get("audioDescription", ""),
                          "generation": normalize_generation(snapshot.get("generation")),
                          "promptJobId": snapshot.get("promptJobId"),
                          "candidateIndex": snapshot.get("candidateIndex", 1), "candidateCount": snapshot.get("candidateCount", 1),
                          "state": item["state"], "submissionPhase": item["submission_phase"], "error": item["error"],
                          "takeId": item["take_id"], "assetId": item["output_hash"],
                          "reviewStatus": review[0] if review else "pending"})
        can_continue = row["state"] in TERMINAL | {"BLOCKED"} and any(i["submissionPhase"] == "NOT_SUBMITTED" for i in items) and not db.execute("SELECT 1 FROM dr_video_continuations WHERE source_job_id=?", (row["id"],)).fetchone()
        return {"id": row["id"], "projectId": row["project_id"], "episodeId": row["episode_id"], "state": row["state"],
                "error": row["error"], "gatewayJobId": row["gateway_job_id"], "dispatchKey": row["dispatch_key"],
                "resourceSettled": bool(row["resource_settled"]), "resourceStatus": row["resource_status"],
                "memoryReleased": bool(row["memory_released"]), "cleanupRequired": bool(row["cleanup_required"]),
                "cancelRequested": bool(row["cancel_requested"]), "canContinue": bool(can_continue), "items": items,
                "createdAt": row["created_at"], "updatedAt": row["updated_at"]}

    def video_job(self, jid):
        with self.store.connect() as db:
            return {"job": self._video_view(db, self.store.one(db, "dr_video_jobs", identifier(jid)))}

    def video_jobs(self, pid):
        with self.store.connect() as db:
            self.store.one(db, "dr_projects", identifier(pid))
            return {"jobs": [self._video_view(db, r) for r in db.execute("SELECT * FROM dr_video_jobs WHERE project_id=? ORDER BY created_at DESC", (pid,))]}

    def _poll_video(self):
        while not self._video_stop.wait(2):
            if getattr(self, "_restore_mode", None):
                continue
            if self.video_adapter is None:
                continue
            try:
                with self.store.connect() as db:
                    ids = [r[0] for r in db.execute("SELECT id FROM dr_video_jobs WHERE state NOT IN ('QUEUED','BLOCKED') AND (state NOT IN ('SUCCEEDED','FAILED','CANCELED','INTERRUPTED') OR cleanup_required=1 OR (gateway_job_id IS NOT NULL AND memory_released=0) OR EXISTS(SELECT 1 FROM dr_video_items i WHERE i.job_id=dr_video_jobs.id AND i.state IN ('COLLECTING','OUTPUT_UNAVAILABLE')))")]
                for jid in ids:
                    if self._video_stop.is_set():
                        break
                    now = time.monotonic()
                    next_at, delay, previous = self._video_poll_times.get(jid, (0, 2, None))
                    if now < next_at:
                        continue
                    try:
                        job = self.refresh_video_job(jid)["job"]
                        fingerprint = encode({key: job[key] for key in ("state", "gatewayJobId", "memoryReleased", "cleanupRequired", "items")})
                        delay = min(delay * 2, 30) if previous == fingerprint else 2
                        self._video_poll_times[jid] = (time.monotonic() + delay, delay, fingerprint)
                    except Exception:
                        # Read failures cannot authorize another generation.
                        delay = min(delay * 2, 30)
                        self._video_poll_times[jid] = (time.monotonic() + delay, delay, previous)
                if not self._video_stop.is_set():
                    self.dispatch_video_once()
            except Exception:
                # Durable state, not the survival of this loop, is authoritative.
                continue

    def dispatch_video_once(self):
        restore_guard(self, "write")
        if self.video_adapter is None:
            return
        with self._generation_lock, self._video_lock:
            with self.store.connect(write=True) as db:
                active = db.execute("SELECT 1 FROM dr_video_jobs WHERE state NOT IN ('QUEUED','BLOCKED','SUCCEEDED','FAILED','CANCELED','INTERRUPTED') OR (gateway_job_id IS NOT NULL AND memory_released=0 AND resource_settled=0) LIMIT 1").fetchone()
                if active:
                    return
                row = db.execute("SELECT j.* FROM dr_video_jobs j LEFT JOIN dr_production_state p ON p.project_id=j.project_id WHERE j.state='QUEUED' AND COALESCE(p.paused,0)=0 ORDER BY j.created_at,j.id LIMIT 1").fetchone()
                if row is None:
                    return
                row = dict(row)
                if not self.generation_turn(db, "video", row["id"]):
                    return
                db.execute("UPDATE dr_video_jobs SET state='DISPATCHING',updated_at=? WHERE id=? AND state='QUEUED'", (time.time(), row["id"]))
            self._dispatch_video(row)

    def _stage_h3_image(self, project_id, aid, images, staged):
        if not aid:
            return ""
        if aid in staged:
            return staged[aid]
        path = self.media_path(project_id, aid)
        try:
            data = _read_regular(path, 8 * 1024 * 1024)
        except (OSError, ValueError) as exc:
            raise GatewayRejected("参考素材无法安全读取，未发送任务") from exc
        if hashlib.sha256(data).hexdigest() != aid:
            raise GatewayRejected("参考素材哈希已变化，未发送任务")
        try:
            sent, sent_id = h3_png_or_jpeg(data)
        except (OSError, DramaError, ValueError) as exc:
            raise GatewayRejected("参考图无法转成H3可接受的PNG或JPEG，未发送任务") from exc
        filename = path.name if sent_id == aid else f"{path.stem or 'reference'}.png"
        if any(c in filename for c in "/\\") or any(ord(c) < 32 for c in filename):
            filename = "reference.png"
        images[sent_id] = {"assetId": sent_id, "filename": filename, "data": sent}
        staged[aid] = sent_id
        return sent_id

    def _dispatch_video(self, row):
        restore_guard(self, "write")
        jid, gate_held = row["id"], False
        def gate():
            nonlocal gate_held
            self._video_dispatch_lock.acquire()
            gate_held = True
            with self.store.connect() as db:
                current = self.store.one(db, "dr_video_jobs", jid)
                if current["cancel_requested"] or self._production_state(db, row["project_id"])["paused"]:
                    raise GatewayRejected("项目暂停或任务已取消，未提交新批次")
                for item in db.execute("SELECT * FROM dr_video_items WHERE job_id=?", (jid,)):
                    context = self.validate_reference_inputs("shot", item["shot_revision_id"], db=db)
                    frozen = json.loads(item["input_snapshot"])
                    current_refs = (context["approvedReferenceAssetId"] or "", context["snapshot"].get("actionReferenceAssetId") or "",
                                    context["snapshot"].get("videoReferenceAssetId") or "", context["snapshot"].get("audioReferenceAssetId") or frozen.get("audioAssetId", ""))
                    frozen_refs = tuple(frozen.get(field, "") for field in ("imageAssetId", "actionImageAssetId", "videoAssetId", "audioAssetId"))
                    current_nodes = tuple(node_asset_ids(context["snapshot"], media) for media in ("image", "video", "audio"))
                    frozen_nodes = tuple(node_asset_ids(frozen, media) for media in ("image", "video", "audio"))
                    if current_refs != frozen_refs or current_nodes != frozen_nodes:
                        raise GatewayRejected("镜头参考素材已变化，请重新审查")
        try:
            with self.store.connect() as db:
                snapshots = [json.loads(r[0]) for r in db.execute("SELECT input_snapshot FROM dr_video_items WHERE job_id=? ORDER BY ordinal", (jid,))]
            shots, images, videos, audios = [], {}, {}, {}
            staged_images = {}
            for snapshot in snapshots:
                node_images = list(node_asset_ids(snapshot, "image"))
                for key, bucket, maximum in (("videoAssetId", videos, MAX_VIDEO_REFERENCE_BYTES),
                                             ("audioAssetId", audios, MAX_AUDIO)):
                    aid = snapshot.get(key)
                    if key == "videoAssetId":
                        aid = next(iter(node_asset_ids(snapshot, "video")), "") or aid
                    if not aid or aid in bucket:
                        continue
                    path = self.media_path(row["project_id"], aid)
                    try:
                        data = _read_regular(path, maximum)
                    except (OSError, ValueError) as exc:
                        raise GatewayRejected("参考素材无法安全读取，未发送任务") from exc
                    if hashlib.sha256(data).hexdigest() != aid:
                        raise GatewayRejected("参考素材哈希已变化，未发送任务")
                    bucket[aid] = {"assetId": aid, "filename": path.name, "data": data}
                sent_nodes = [self._stage_h3_image(row["project_id"], aid, images, staged_images) for aid in node_images]
                image_id = self._stage_h3_image(row["project_id"], snapshot.get("imageAssetId") or "", images, staged_images)
                action_id = self._stage_h3_image(row["project_id"], snapshot.get("actionImageAssetId") or "", images, staged_images)
                shots.append({"name": snapshot["name"], "prompt": snapshot["prompt"], "seed": snapshot["seed"],
                              "image_asset_id": image_id,
                              "action_image_asset_id": action_id,
                              "reference_image_ids": sent_nodes,
                              "video_asset_id": next(iter(node_asset_ids(snapshot, "video")), "") or snapshot.get("videoAssetId", ""),
                              "audio_asset_id": snapshot["audioAssetId"],
                              "request_duration_seconds": normalize_generation(snapshot.get("generation"))["requestDurationSeconds"],
                              "contract_version": normalize_generation(snapshot.get("generation"))["contractVersion"]})
            profiles = {normalize_generation(s.get("generation"))["profile"] for s in snapshots}
            if len(profiles) != 1:
                raise GatewayRejected("混合生成模式的持久批次不受支持")
            profile = profiles.pop()
            options = {} if profile == STABLE_PROFILE else {"profile": profile}
            if videos:
                options["videos"] = list(videos.values())
            record = self.video_adapter.submit(shots, row["dispatch_key"], list(images.values()), list(audios.values()), gate, **options)
            self._bind_video_record(jid, record)
        except GatewayBusy:
            with self.store.connect(write=True) as db:
                db.execute("UPDATE dr_video_jobs SET state='QUEUED',error=?,updated_at=? WHERE id=? AND gateway_job_id IS NULL AND cancel_requested=0",
                           ("DGX资源繁忙，已保存并等待执行", time.time(), jid))
        except (GatewayRejected, DramaError, FileNotFoundError) as exc:
            # Business gate and local inputs are definite pre-POST failures.
            with self.store.connect(write=True) as db:
                current = self.store.one(db, "dr_video_jobs", jid)
                db.execute("UPDATE dr_video_jobs SET state=?,error=?,reserved_shots=0,updated_at=? WHERE id=? AND gateway_job_id IS NULL",
                           ("CANCELED" if current["cancel_requested"] else "BLOCKED", str(exc), time.time(), jid))
        except Exception:
            with self.store.connect(write=True) as db:
                db.execute("UPDATE dr_video_items SET submission_phase='UNKNOWN',state='SUBMISSION_UNKNOWN' WHERE job_id=? AND take_id IS NULL", (jid,))
                db.execute("UPDATE dr_video_jobs SET state='SUBMISSION_UNKNOWN',error=?,updated_at=? WHERE id=?",
                           ("未收到确定提交回执，正在只读核查原编号；不会重拍", time.time(), jid))
        finally:
            if gate_held:
                self._video_dispatch_lock.release()

    def _bind_video_record(self, jid, record):
        try:
            gateway_id = identifier(record.get("job_id"))
        except DramaError as exc:
            raise ValueError("Invalid video receipt") from exc
        if len(gateway_id) != 32:
            raise ValueError("Invalid video job identity")
        with self.store.connect(write=True) as db:
            row = self.store.one(db, "dr_video_jobs", jid)
            if record.get("idempotency_key") != row["dispatch_key"]:
                raise ValueError("Gateway batch key differs from saved dispatch")
            expected = [json.loads(i[0]) for i in db.execute("SELECT input_snapshot FROM dr_video_items WHERE job_id=? ORDER BY ordinal", (jid,))]
            actual = record.get("shots")
            if not isinstance(actual, list) or len(actual) != len(expected):
                raise ValueError("Gateway batch differs from saved inputs")
            for a, e in zip(actual, expected):
                if gateway_generation(record.get("profile", STABLE_PROFILE), a) != normalize_generation(e.get("generation")):
                    raise ValueError("Gateway generation contract differs from saved inputs")
                pairs = (("name", "name"), ("prompt", "prompt"), ("seed", "seed"),
                         ("image_asset_id", "imageAssetId"), ("action_image_asset_id", "actionImageAssetId"),
                         ("video_asset_id", "videoAssetId"), ("audio_asset_id", "audioAssetId"))
                if any((a.get(k, "") != e.get(v, "")) for k, v in pairs):
                    raise ValueError("Gateway shot differs from saved inputs")
            if row["gateway_job_id"] not in (None, gateway_id):
                raise ValueError("Gateway task identity changed")
            db.execute("UPDATE dr_video_jobs SET gateway_job_id=?,state='SUBMITTED',error=NULL,updated_at=? WHERE id=?", (gateway_id, time.time(), jid))

    def refresh_video_job(self, jid):
        restore_guard(self, "query")
        identifier(jid)
        if self.video_adapter is None:
            return self.video_job(jid)
        with self._video_lock:
            with self.store.connect() as db:
                row = self.store.one(db, "dr_video_jobs", jid)
            if (row["state"] in {"QUEUED", "BLOCKED"} and not row["gateway_job_id"]) or (not row["gateway_job_id"] and row["state"] in TERMINAL):
                return self.video_job(jid)
            record = self.video_adapter.get(row["gateway_job_id"]) if row["gateway_job_id"] else self.video_adapter.find(row["dispatch_key"])
            if record is None:
                return self.video_job(jid)
            self._bind_video_record(jid, record)
            if not getattr(self, "_restore_mode", None) and row["cancel_requested"] and not record.get("cancel_requested") and record.get("state") not in TERMINAL | {"BLOCKED"}:
                record = self.video_adapter.cancel(record["job_id"])
                self._bind_video_record(jid, record)
            outputs = self.video_adapter.outputs(record)
            for output in outputs:
                self._archive_video_output(jid, output)
            with self.store.connect(write=True) as db:
                for index, shot in enumerate(record["shots"]):
                    item = db.execute("SELECT * FROM dr_video_items WHERE job_id=? AND ordinal=?", (jid, index)).fetchone()
                    phase = shot.get("submission_phase", "UNKNOWN")
                    error = item["error"]
                    if (not item["take_id"] and item["state"] == "OUTPUT_UNAVAILABLE"
                            and error == OUTPUT_UNAVAILABLE_ERROR and not shot.get("artifact")
                            and phase in {"REQUEST_STARTED", "RECEIVED"} and shot.get("state") == "RUNNING"
                            and record.get("state") in {"RUNNING", "CANCELING"}):
                        # Older collectors mistook a running request for a
                        # missing completed file. Clear only that transient error.
                        error = None
                    # An upstream success only becomes locally successful after validation.
                    state = "SUCCEEDED" if item["take_id"] else item["state"] if error else shot.get("state", "UNKNOWN")
                    if state == "SUCCEEDED" and not item["take_id"]:
                        state = "COLLECTING"
                    db.execute("UPDATE dr_video_items SET state=?,submission_phase=?,error=? WHERE id=?", (state, phase, error, item["id"]))
                current = self.store.one(db, "dr_video_jobs", jid)
                items = db.execute("SELECT * FROM dr_video_items WHERE job_id=?", (jid,)).fetchall()
                remote_state = record.get("state", "UNKNOWN")
                state = remote_state if remote_state in TERMINAL | {"BLOCKED", "SUBMISSION_UNKNOWN"} else "CANCEL_REQUESTED" if current["cancel_requested"] else remote_state if remote_state in {"LOADING", "UPLOADING", "RELEASING", "RECONCILING", "CANCELING"} else "RUNNING"
                if remote_state == "SUCCEEDED" and not all(i["take_id"] for i in items):
                    state = "FAILED"
                released = record.get("memory_released") is True
                cleanup = record.get("cleanup_required") is True
                settled = released or (record.get("resource_settled") is True and remote_state in TERMINAL and not cleanup)
                resource_status = "released" if released else "resident" if settled else "unsettled"
                count = sum(i["submission_phase"] in {"UNKNOWN", "REQUEST_STARTED"} for i in items) if state in TERMINAL | {"BLOCKED"} else len(items)
                error = None if state == "SUCCEEDED" else ("部分镜头尚未形成有效候选，请核查逐镜头结果" if state == "FAILED" else None)
                db.execute("UPDATE dr_video_jobs SET state=?,memory_released=?,resource_settled=?,resource_status=?,cleanup_required=?,reserved_shots=?,error=?,updated_at=? WHERE id=?",
                           (state, int(released), int(settled), resource_status, int(cleanup), count, error, time.time(), jid))
            return self.video_job(jid)

    def _archive_video_output(self, jid, output):
        from .media import probe_media
        index = integer(output.get("index"), "镜头序号", 0, 31)
        with self.store.connect() as db:
            row = self.store.one(db, "dr_video_jobs", jid)
            item = db.execute("SELECT * FROM dr_video_items WHERE job_id=? AND ordinal=?", (jid, index)).fetchone()
            if item is None:
                raise ValueError("Output index not in batch")
            item = dict(item)
        if item["take_id"]:
            return
        if output.get("error"):
            if item["state"] == "INVALID":
                return  # A later read failure cannot erase a proven invalid result.
            error = OUTPUT_UNAVAILABLE_ERROR
            error_state = "OUTPUT_UNAVAILABLE"
        else:
            error = None
            fd, name = tempfile.mkstemp(prefix="video-result-", suffix=".mp4", dir=self.root / "temp")
            path = Path(name)
            try:
                data = output["data"]
                if not isinstance(data, bytes) or not 0 < len(data) <= 256 * 1024 * 1024:
                    raise ValueError("Invalid video payload")
                with os.fdopen(fd, "wb") as stream:
                    fd = None
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                metadata = probe_media(path)
                generation = normalize_generation(json.loads(item["input_snapshot"]).get("generation"))
                frames = metadata.get("frames", 0)
                valid_frames = frames == 107 if generation["profile"] == STABLE_PROFILE else generation["requestDurationSeconds"] * 24 <= frames <= (generation["requestDurationSeconds"] + 1) * 24
                if (metadata.get("width"), metadata.get("height"), metadata.get("fps")) != (1344, 768, 24) or not valid_frames or not metadata.get("hasAudio"):
                    raise ValueError("Video does not satisfy frozen H3 contract")
                aid = self._publish(path)
                with self.store.connect(write=True) as db:
                    current = self.store.one(db, "dr_video_items", item["id"])
                    if not current["take_id"]:
                        self._bind_asset(db, row["project_id"], aid, output["filename"], {**metadata, "extension": "mp4"}, len(data))
                        tid = new_id()
                        db.execute("INSERT INTO dr_takes(id,shot_id,asset_id,source,created_at,source_revision_id) VALUES (?,?,?,?,?,?)",
                                   (tid, item["shot_id"], aid, "h3_video", time.time(), item["shot_revision_id"]))
                        db.execute("UPDATE dr_video_items SET take_id=?,output_hash=?,state='SUCCEEDED',error=NULL WHERE id=?", (tid, aid, item["id"]))
            except (ValueError, RuntimeError, OSError, DramaError):
                error_state = "INVALID"
                error = "原片未通过完整解码/1344×768/24fps/冻结时长合同/音轨检查，未建立可采用候选"
            finally:
                if fd is not None:
                    os.close(fd)
                path.unlink(missing_ok=True)
        if error:
            with self.store.connect(write=True) as db:
                db.execute("UPDATE dr_video_items SET state=?,error=? WHERE id=? AND take_id IS NULL", (error_state, error, item["id"]))

    def cancel_video_job(self, jid):
        restore_guard(self, "write")
        # Persist intent before any network I/O. In-flight clips may finish.
        with self._video_dispatch_lock:
            with self.store.connect(write=True) as db:
                row = self.store.one(db, "dr_video_jobs", identifier(jid))
                if row["state"] in {"QUEUED", "BLOCKED", "INTERRUPTED"} or (
                        row["state"] == "SUBMISSION_UNKNOWN" and not row["gateway_job_id"]):
                    # Leftover INTERRUPTED rows can still hold a gateway_job_id with
                    # unsettled memory, which blocks FIFO even after CANCELED.
                    settle = row["state"] == "INTERRUPTED" and row["gateway_job_id"]
                    db.execute(
                        "UPDATE dr_video_jobs SET cancel_requested=1,state='CANCELED',reserved_shots=0,"
                        "memory_released=?,resource_settled=?,cleanup_required=0,resource_status=?,"
                        "updated_at=? WHERE id=?",
                        (1 if settle or row["memory_released"] else 0,
                         1 if settle or row["resource_settled"] else 0,
                         "released" if settle or row["memory_released"] or row["resource_settled"] else row["resource_status"],
                         time.time(), jid),
                    )
                elif row["state"] not in TERMINAL:
                    db.execute("UPDATE dr_video_jobs SET cancel_requested=1,state='CANCEL_REQUESTED',updated_at=? WHERE id=?", (time.time(), jid))
        if row["gateway_job_id"] and row["state"] not in TERMINAL:
            self.video_adapter.cancel(row["gateway_job_id"])
        return self.video_job(jid)

    def release_video_job(self, jid):
        restore_guard(self, "write")
        with self._video_lock:
            with self.store.connect() as db:
                row = self.store.one(db, "dr_video_jobs", identifier(jid))
            if not row["gateway_job_id"] or not row["cleanup_required"]:
                raise DramaError("release_unavailable", "此任务没有已确认的待释放资源", 409)
            self.video_adapter.reconcile(row["gateway_job_id"])
            return self.refresh_video_job(jid)

    def continue_video_job(self, jid, body):
        restore_guard(self, "write")
        fields(body, {"requestKey"}, {"requestKey"})
        with self._video_lock:
            with self.store.connect() as db:
                existing = db.execute("SELECT continuation_job_id FROM dr_video_continuations WHERE source_job_id=?", (identifier(jid),)).fetchone()
            if existing:
                return self.video_job(existing[0])
            job = self.video_job(jid)["job"]
            if not job["canContinue"]:
                raise DramaError("cannot_continue", "没有可确认未发送的镜头；结果不明时禁止续拍", 409)
            shots = [{key: item[key] for key in ("shotId", "shotRevisionId", "audioAssetId", "prompt", "seed", "generation")}
                     | {key: item.get(key, "") for key in ("actionImageAssetId", "videoAssetId", "audioDescription")}
                     for item in job["items"] if item["submissionPhase"] == "NOT_SUBMITTED"]
            return self.submit_video_batch(job["episodeId"], {"shots": shots, "requestKey": "continue-" + text(body["requestKey"], "requestKey", 110)}, continuation_of=jid)

    def review_take(self, tid, body):
        fields(body, {"decision", "notes", "requestKey"}, {"decision", "requestKey"})
        if body["decision"] not in {"approved", "rejected"}:
            raise DramaError("invalid_review", "审片结论须为approved或rejected")
        notes = body.get("notes", "")
        if not isinstance(notes, str) or len(notes) > 2000 or "\x00" in notes:
            raise DramaError("invalid_input", "审片意见应为不超过 2000 个字符的文本")
        notes = notes.strip()
        def save(db):
            self.store.one(db, "dr_takes", identifier(tid))
            db.execute("INSERT INTO dr_take_reviews VALUES (?,?,?,?) ON CONFLICT(take_id) DO UPDATE SET decision=excluded.decision,notes=excluded.notes,updated_at=excluded.updated_at",
                       (tid, body["decision"], notes, time.time()))
            return {"takeId": tid, "reviewStatus": body["decision"]}
        return self.store.mutate(f"take-review:{tid}", body["requestKey"], body, save)
