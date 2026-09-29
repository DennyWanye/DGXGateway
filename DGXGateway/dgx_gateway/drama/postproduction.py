"""Frame based postproduction. GPU generation is never invoked here."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time

from .contracts import DramaError, fields, identifier, integer, revision, text
from .restore import restore_guard
from .store import encode, new_id

MAX_AUDIO = 256 * 1024 * 1024


def number(value, label, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise DramaError("invalid_input", f"{label}必须在{low}–{high}之间")
    return value


def mapping_hash(timeline):
    value = {"fps": timeline["fps"], "clips": [{k: c[k] for k in
             ("clipId", "assetId", "inFrame", "frames", "keepSourceAudio")} for c in timeline["clips"]],
             "audioClips": timeline.get("audioClips", [])}
    return hashlib.sha256(encode(value).encode()).hexdigest()


class PostproductionMixin:
    def init_postproduction(self):
        # Administrator controlled paths only; never accepted from HTTP input.
        self.post_ffmpeg = os.environ.get("DGX_DRAMA_FFMPEG", "ffmpeg")
        self.post_ffprobe = os.environ.get("DGX_DRAMA_FFPROBE", "ffprobe")
        self.asr_worker = os.environ.get("DGX_DRAMA_ASR_WORKER", "")
        self.asr_model = os.environ.get("DGX_DRAMA_ASR_MODEL", "")
        self.post_font_file = os.environ.get("DGX_DRAMA_FONT_FILE", "")
        self.post_font_family = os.environ.get("DGX_DRAMA_FONT_FAMILY", "Heiti SC")
        self.post_font_probe = os.environ.get("DGX_DRAMA_FONT_PROBE", "fc-query")
        self._dialogue_lock = threading.Lock()
        if not getattr(self, "_restore_mode", None):
            with self.store.connect(write=True) as db:
                db.execute("UPDATE dr_transcriptions SET state='INTERRUPTED',error=?,updated_at=? WHERE state IN ('QUEUED','RUNNING')",
                           ("服务重启，转写未自动重发；请核查原任务", time.time()))

    def post_capabilities(self):
        return {"postproductionEnabled": True, "maxAudioBytes": MAX_AUDIO,
                "postMediaReady": bool(shutil.which(self.post_ffmpeg) and shutil.which(self.post_ffprobe)),
                "audioTracks": ["voiceover", "ambience", "bgm"], "timelineSchema": "drama-timeline-v2",
                "asrReady": not getattr(self, "_restore_mode", None) and bool(self.asr_worker and shutil.which(self.asr_worker) and self.asr_model and Path(self.asr_model).is_file())}

    def media_tools(self):
        return {"ffmpeg_bin": self.post_ffmpeg, "ffprobe_bin": self.post_ffprobe}

    @staticmethod
    def normalize_crop(clip):
        fit = clip.get("fit", "contain")
        if fit not in ("contain", "cover"):
            raise DramaError("invalid_crop", "画面适配必须为完整画面或明确裁切")
        if fit == "contain":
            if "crop" in clip:
                raise DramaError("invalid_crop", "完整画面不接受隐藏裁切参数")
            return {"fit": fit}
        crop = clip.get("crop")
        fields(crop, {"x", "y"}, {"x", "y"})
        return {"fit": fit, "crop": {k: number(crop[k], "裁切位置", 0, 1) for k in ("x", "y")}}

    def normalize_audio_clips(self, db, pid, clips, total):
        if not isinstance(clips, list) or len(clips) > 32:
            raise DramaError("invalid_audio", "独立音轨最多32个片段")
        seen, result = set(), []
        allowed = {"audioClipId", "assetId", "track", "startFrame", "sourceInFrame", "frames", "gainDb", "fadeInFrames", "fadeOutFrames"}
        for clip in clips:
            fields(clip, allowed, {"audioClipId", "assetId", "track", "startFrame", "sourceInFrame", "frames"})
            cid = text(clip["audioClipId"], "音频片段ID", 128)
            if cid in seen:
                raise DramaError("duplicate_clip", "音频片段ID不能重复")
            seen.add(cid)
            info = json.loads(self._bound_asset(db, pid, clip["assetId"])["metadata"])
            if info.get("kind") != "audio":
                raise DramaError("audio_required", "独立音轨必须使用音频素材")
            track = clip["track"]
            if track not in ("voiceover", "ambience", "bgm"):
                raise DramaError("invalid_audio", "不支持此音轨类型")
            start = integer(clip["startFrame"], "开始帧", 0, total)
            source = integer(clip["sourceInFrame"], "音频源入点", 0, 86400)
            length = integer(clip["frames"], "音频长度", 1, 86400)
            available = math.floor(info.get("decodedSamples48k", info["duration"] * 48000) / 2000)
            if start + length > total or source + length > available:
                raise DramaError("audio_out_of_bounds", "音频超出源文件或成片范围；请缩短音轨或延长画面")
            gain = number(clip.get("gainDb", 0), "音量", -60, 12)
            fade_in = integer(clip.get("fadeInFrames", 0), "淡入帧数", 0, length)
            fade_out = integer(clip.get("fadeOutFrames", 0), "淡出帧数", 0, length)
            if fade_in + fade_out > length:
                raise DramaError("invalid_audio", "淡入和淡出合计不能超过音频片段")
            result.append(dict(audioClipId=cid, assetId=clip["assetId"], track=track, startFrame=start,
                               sourceInFrame=source, frames=length, gainDb=gain, fadeInFrames=fade_in, fadeOutFrames=fade_out))
        return result

    def upload_audio(self, pid, stream, length, filename, key):
        from .post_media import probe_audio
        integer(length, "文件字节数", 1, MAX_AUDIO)
        filename = text(filename, "文件名", 255)
        text(key, "requestKey", 128)
        with self.store.connect() as db:
            self.store.one(db, "dr_projects", identifier(pid))
        if shutil.disk_usage(self.root).free < length + 128 * 1024 * 1024:
            raise DramaError("disk_full", "磁盘空间不足", 507)
        fd, name = tempfile.mkstemp(prefix="audio-", dir=self.root / "temp")
        path = Path(name)
        try:
            with os.fdopen(fd, "wb") as output:
                remaining = length
                while remaining:
                    data = stream.read(min(remaining, 1024 * 1024))
                    if not data:
                        raise DramaError("upload_truncated", "音频上传不完整")
                    output.write(data)
                    remaining -= len(data)
                output.flush()
                os.fsync(output.fileno())
            try:
                info = probe_audio(path, **self.media_tools())
            except (ValueError, OSError, RuntimeError) as exc:
                raise DramaError("invalid_audio", "音频无法完整解码；支持最长1小时的WAV、MP3、M4A、FLAC") from exc
            aid = self._publish(path, "." + info["extension"])
            def save(db):
                self._bind_asset(db, pid, aid, filename, info, length)
                return {"asset": self._asset_view(self.store.one(db, "dr_assets", aid))}
            return self.store.mutate(f"audio-upload:{pid}", key, {"sha256": aid, "filename": filename}, save)
        finally:
            path.unlink(missing_ok=True)

    def post_inputs(self, tid):
        from .service import digest_file
        with self.store.connect() as db:
            row = self.store.one(db, "dr_timeline_revisions", identifier(tid))
            ep = self.store.one(db, "dr_episodes", row["episode_id"])
        tl = json.loads(row["snapshot"])
        def resolve(c):
            path = self.media_path(ep["project_id"], c["assetId"])
            if digest_file(path) != c["assetId"]:
                raise DramaError("asset_corrupt", "素材哈希校验失败")
            return {**c, "path": path}
        return ep, tl, [resolve(c) for c in tl["clips"]], [resolve(c) for c in tl.get("audioClips", [])]

    def dialogue_asset(self, tid):
        # A text-only subtitle revision reuses the exact archived dialogue WAV.
        # Serializing the cache fill prevents duplicate CPU renders on lost receipts.
        with self._dialogue_lock:
            return self._dialogue_asset_locked(tid)

    def _dialogue_asset_locked(self, tid):
        from .post_media import render_dialogue_audio
        from .service import digest_file
        ep, tl, clips, audio = self.post_inputs(tid)
        mapping = mapping_hash(tl)
        with self.store.connect() as db:
            candidates = db.execute("SELECT a.id,a.metadata FROM dr_assets a JOIN dr_asset_bindings b ON b.asset_id=a.id WHERE b.project_id=?", (ep["project_id"],)).fetchall()
        for row in candidates:
            info = json.loads(row["metadata"])
            if info.get("dialogueContract") == "drama-dialogue-v1" and info.get("timeMappingHash") == mapping:
                path = self.media_path(ep["project_id"], row["id"])
                if digest_file(path) != row["id"]:
                    raise DramaError("asset_corrupt", "已归档对白音轨哈希不匹配")
                return row["id"]
        with tempfile.TemporaryDirectory(dir=self.root / "temp", prefix="dialogue-") as directory:
            output = Path(directory) / "dialogue.wav"
            info = render_dialogue_audio(clips, output, fps=tl["fps"], audio_clips=audio, **self.media_tools())
            aid = self._publish(output, ".wav")
            with self.store.connect(write=True) as db:
                self._bind_asset(db, ep["project_id"], aid, "对白检查.wav", {**info, "timeMappingHash": mapping, "dialogueContract": "drama-dialogue-v1"}, output.stat().st_size)
        return aid

    @staticmethod
    def normalize_segments(segments, total):
        if not isinstance(segments, list) or len(segments) > 2000:
            raise DramaError("invalid_subtitles", "字幕最多2000条")
        result, previous = [], 0
        for segment in segments:
            fields(segment, {"startFrame", "endFrame", "text"}, {"startFrame", "endFrame", "text"})
            start = integer(segment["startFrame"], "字幕开始帧", 0, total - 1)
            end = integer(segment["endFrame"], "字幕结束帧", start + 1, total)
            if start < previous:
                raise DramaError("invalid_subtitles", "单轨字幕必须按时间排序且不能重叠")
            result.append({"startFrame": start, "endFrame": end, "text": text(segment["text"], "字幕", 1000)})
            previous = end
        return result

    def subtitles(self, tid):
        with self.store.connect() as db:
            row = self.store.one(db, "dr_timeline_revisions", identifier(tid))
            mapping = mapping_hash(json.loads(row["snapshot"]))
            rows = db.execute("SELECT r.*,a.approved_at FROM dr_subtitle_revisions r LEFT JOIN dr_subtitle_approvals a ON a.subtitle_id=r.id WHERE episode_id=? ORDER BY revision DESC", (row["episode_id"],)).fetchall()
        return {"subtitles": [{**json.loads(r["snapshot"]), "approved": r["approved_at"] is not None,
                              "needsReview": json.loads(r["snapshot"])["timeMappingHash"] != mapping} for r in rows]}

    def save_subtitles(self, tid, body, *, source="manual", audio_asset_id=None):
        fields(body, {"segments", "expectedRevision", "requestKey"}, {"segments", "expectedRevision", "requestKey"})
        ep, tl, _, _ = self.post_inputs(tid)
        segments = self.normalize_segments(body["segments"], sum(c["frames"] for c in tl["clips"]))
        # Resolve idempotent replay before expensive media rendering.
        with self.store.connect() as db:
            old = db.execute("SELECT input_hash,response FROM dr_requests WHERE scope=? AND request_key=?", (f"subtitles:{ep['id']}", text(body["requestKey"], "requestKey", 128))).fetchone()
            if old:
                if old["input_hash"] != hashlib.sha256(encode({**body, "timelineId": tid}).encode()).hexdigest():
                    raise DramaError("request_conflict", "原请求键已经用于不同字幕", 409)
                return json.loads(old["response"])
            current = db.execute("SELECT COALESCE(MAX(revision),0) FROM dr_subtitle_revisions WHERE episode_id=?", (ep["id"],)).fetchone()[0]
            revision(current, body["expectedRevision"])
        aid = audio_asset_id or self.dialogue_asset(tid)
        def save(db):
            current = db.execute("SELECT COALESCE(MAX(revision),0) FROM dr_subtitle_revisions WHERE episode_id=?", (ep["id"],)).fetchone()[0]
            revision(current, body["expectedRevision"])
            sid = new_id()
            snapshot = dict(id=sid, revision=current + 1, sourceTimelineRevisionId=tid, audioAssetHash=aid,
                            timeMappingHash=mapping_hash(tl), segments=segments, source=source, style={"fontFamily": self.post_font_family})
            db.execute("INSERT INTO dr_subtitle_revisions VALUES (?,?,?,?,?,?)", (sid, ep["id"], tid, current + 1, encode(snapshot), time.time()))
            return {"subtitle": {**snapshot, "approved": False, "needsReview": False}}
        return self.store.mutate(f"subtitles:{ep['id']}", body["requestKey"], {**body, "timelineId": tid}, save)

    def approve_subtitles(self, sid, body):
        fields(body, {"timelineId", "requestKey"}, {"timelineId", "requestKey"})
        def save(db):
            sub = self.store.one(db, "dr_subtitle_revisions", identifier(sid))
            tl = self.store.one(db, "dr_timeline_revisions", identifier(body["timelineId"]))
            ep = self.store.one(db, "dr_episodes", sub["episode_id"])
            if tl["episode_id"] != sub["episode_id"] or tl["revision"] != ep["timeline_revision"] or mapping_hash(json.loads(tl["snapshot"])) != json.loads(sub["snapshot"])["timeMappingHash"]:
                raise DramaError("subtitle_stale", "字幕与当前时间线不匹配，请在当前时间线上校对另存", 409)
            latest = db.execute("SELECT MAX(revision) FROM dr_subtitle_revisions WHERE episode_id=?", (sub["episode_id"],)).fetchone()[0]
            if sub["revision"] != latest:
                raise DramaError("subtitle_stale", "只能批准最新字幕修订", 409)
            db.execute("INSERT OR IGNORE INTO dr_subtitle_approvals VALUES (?,?)", (sid, time.time()))
            return {"subtitleId": sid, "approved": True}
        return self.store.mutate(f"subtitle-approve:{sid}", body["requestKey"], body, save)

    def freeze_export_recipe(self, db, xid, row, body):
        tl = json.loads(row["snapshot"])
        if tl.get("schemaVersion") != "drama-timeline-v2" and not set(body).intersection({"outputs", "subtitleRevisionId", "presetVersion"}):
            return  # Preserve the qualified legacy renderer and its exact recipe.
        outputs = body.get("outputs", ["master"])
        allowed = {"master", "proxy", "burned", "srt", "ass", "report"}
        if not isinstance(outputs, list) or not outputs or any(type(v) is not str or v not in allowed for v in outputs) or len(set(outputs)) != len(outputs):
            raise DramaError("invalid_export", "请选择有效且不重复的导出种类")
        preset = body.get("presetVersion", "drama-export-v2")
        if preset != "drama-export-v2":
            raise DramaError("invalid_export", "不支持此导出预设")
        sub_id = body.get("subtitleRevisionId")
        if set(outputs).intersection({"burned", "srt", "ass"}) and not sub_id:
            raise DramaError("subtitle_required", "字幕导出需要选择已批准字幕")
        if sub_id:
            sub = self.store.one(db, "dr_subtitle_revisions", identifier(sub_id))
            if sub["episode_id"] != row["episode_id"] or json.loads(sub["snapshot"])["timeMappingHash"] != mapping_hash(tl):
                raise DramaError("subtitle_stale", "字幕时间映射不匹配，请先校对", 409)
            if not db.execute("SELECT 1 FROM dr_subtitle_approvals WHERE subtitle_id=?", (sub_id,)).fetchone():
                raise DramaError("subtitle_unapproved", "字幕尚未批准", 409)
        recipe = dict(timelineRevisionId=row["id"], subtitleRevisionId=sub_id, timeMappingHash=mapping_hash(tl),
                      presetVersion=preset, outputs=outputs)
        recipe["_runtime"] = self.freeze_post_runtime("burned" in outputs)
        db.execute("INSERT INTO dr_export_recipes(export_id,recipe) VALUES (?,?)", (xid, encode(recipe)))

    def freeze_post_runtime(self, burned):
        from .service import digest_file
        result = {}
        for key, configured in (("ffmpeg_bin", self.post_ffmpeg), ("ffprobe_bin", self.post_ffprobe)):
            resolved = shutil.which(configured)
            if not resolved:
                raise DramaError("media_tools_missing", "已配置的后期媒体程序不可用", 503)
            path = Path(resolved).resolve()
            result[key] = {"path": str(path), "sha256": digest_file(path)}
        if burned:
            if not self.post_font_file:
                raise DramaError("font_missing", "请先配置已验证的中文字幕字体", 503)
            path = Path(self.post_font_file).resolve()
            try:
                result["font_file"] = {"path": str(path), "sha256": digest_file(path)}
                probe = Path(shutil.which(self.post_font_probe) or "").resolve()
                result["font_probe_bin"] = {"path": str(probe), "sha256": digest_file(probe)}
            except (OSError, DramaError) as exc:
                raise DramaError("font_missing", "字幕字体或资格检查程序不可用", 503) from exc
        return result

    def render_post_export(self, xid):
        from .post_media import render_post_timeline, encode_subtitles, burn_subtitles
        published = []
        role = "prepare"
        try:
            with self.store.connect(write=True) as db:
                row = self.store.one(db, "dr_exports", xid)
                if row["state"] != "QUEUED":
                    return
                recipe = json.loads(db.execute("SELECT recipe FROM dr_export_recipes WHERE export_id=?", (xid,)).fetchone()[0])
                db.execute("UPDATE dr_exports SET state='RUNNING',updated_at=? WHERE id=?", (time.time(), xid))
                sub = json.loads(self.store.one(db, "dr_subtitle_revisions", recipe["subtitleRevisionId"])["snapshot"]) if recipe["subtitleRevisionId"] else None
            ep, tl, clips, audio = self.post_inputs(row["timeline_id"])
            from .service import digest_file
            runtime = recipe["_runtime"]
            for value in runtime.values():
                if digest_file(Path(value["path"])) != value["sha256"]:
                    raise DramaError("render_runtime_changed", "原导出使用的媒体程序或字体身份已变化，请恢复原依赖或明确新建导出")
            tools = {key: runtime[key]["path"] for key in ("ffmpeg_bin", "ffprobe_bin")}
            with tempfile.TemporaryDirectory(dir=self.root / "exports", prefix=xid + "-") as directory:
                directory = Path(directory)
                frozen_font = None
                if "font_file" in runtime:
                    frozen_font = directory / ("font" + Path(runtime["font_file"]["path"]).suffix)
                    shutil.copyfile(runtime["font_file"]["path"], frozen_font)
                    if digest_file(frozen_font) != runtime["font_file"]["sha256"]:
                        raise DramaError("render_runtime_changed", "原导出字体身份已变化，请恢复原依赖或明确新建导出")
                master = directory / "master.mp4"
                generated = {}
                wanted = recipe["outputs"]
                if "master" in wanted or "burned" in wanted:
                    role = "master"
                    meta = render_post_timeline(clips, master, width=tl["width"], height=tl["height"], fps=tl["fps"], audio_clips=audio, **tools)
                    if "master" in wanted:
                        generated["master"] = (master, meta)
                if "proxy" in wanted:
                    role = "proxy"
                    ratio = min(1, 640 / max(tl["width"], tl["height"]))
                    # Equal aspect ratio to within one output pixel; exact time/crop mapping is shared.
                    width, height = max(2, round(tl["width"] * ratio / 2) * 2), max(2, round(tl["height"] * ratio / 2) * 2)
                    proxy = directory / "proxy.mp4"
                    generated["proxy"] = (proxy, render_post_timeline(clips, proxy, width=width, height=height, fps=tl["fps"], audio_clips=audio, **tools))
                if sub and set(wanted).intersection({"burned", "srt", "ass"}):
                    role = "subtitles"
                    subtitles = encode_subtitles(sub["segments"], fps=tl["fps"], total_frames=sum(c["frames"] for c in tl["clips"]), width=tl["width"], height=tl["height"], style=sub["style"])
                    for kind in ("srt", "ass"):
                        path = directory / ("captions." + kind)
                        path.write_text(subtitles[kind], encoding="utf-8")
                        if kind in wanted:
                            generated[kind] = (path, {"kind": "subtitle", "extension": kind})
                    if "burned" in wanted:
                        role = "burned"
                        path = directory / "burned.mp4"
                        generated["burned"] = (path, burn_subtitles(master, directory / "captions.ass", path,
                            font_file=str(frozen_font), font_family=sub["style"]["fontFamily"],
                            font_probe_bin=runtime["font_probe_bin"]["path"], **tools))
                for role, (path, metadata) in generated.items():
                    aid = self._publish(path, path.suffix)
                    metadata = {**metadata, "outputRole": role, "sourceTimelineId": tl["id"], "timeMappingHash": recipe["timeMappingHash"]}
                    with self.store.connect(write=True) as db:
                        self._bind_asset(db, ep["project_id"], aid, f"{role}-{xid[:8]}{path.suffix}", metadata, path.stat().st_size)
                    published.append({"role": role, "assetId": aid, "metadata": metadata})
                if "report" in wanted:
                    role = "report"
                    report = directory / "report.json"
                    report.write_text(encode({"exportId": xid, "recipe": self.public_recipe(recipe), "outputs": published, "technicalStatus": "passed", "humanReview": "pending"}), encoding="utf-8")
                    aid = self._publish(report, ".json")
                    with self.store.connect(write=True) as db:
                        self._bind_asset(db, ep["project_id"], aid, f"report-{xid[:8]}.json", {"kind": "report", "extension": "json"}, report.stat().st_size)
                    published.append({"role": "report", "assetId": aid})
            with self.store.connect(write=True) as db:
                db.execute("UPDATE dr_export_recipes SET outputs=? WHERE export_id=?", (encode(published), xid))
                master_id = next((v["assetId"] for v in published if v["role"] == "master"), None)
                db.execute("UPDATE dr_exports SET state='SUCCEEDED',asset_id=?,updated_at=? WHERE id=?", (master_id, time.time(), xid))
        except Exception as exc:
            message = exc.message if isinstance(exc, DramaError) else "请检查媒体工具、字幕滤镜、字体和源素材"
            with self.store.connect(write=True) as db:
                db.execute("UPDATE dr_export_recipes SET outputs=? WHERE export_id=?", (encode(published), xid))
                db.execute("UPDATE dr_exports SET state='FAILED',error=?,updated_at=? WHERE id=?", (f"{role}导出失败：{message}；输入配方已保留", time.time(), xid))
        finally:
            with self._schedule_lock:
                self._scheduled.discard(xid)

    def retry_export(self, xid, body):
        fields(body, {"requestKey"}, {"requestKey"})
        old = self.export(xid)["export"]
        if old["state"] not in ("FAILED", "INTERRUPTED"):
            raise DramaError("export_not_retryable", "只能重试失败或中断的导出", 409)
        with self.store.connect() as db:
            recipe = db.execute("SELECT recipe FROM dr_export_recipes WHERE export_id=?", (xid,)).fetchone()
        if not recipe:
            return self.create_export(old["timelineId"], body)
        def save(db):
            if self.active_export_count(db) >= 32:
                raise DramaError("export_queue_full", "导出队列已满", 429)
            new, now = new_id(), time.time()
            db.execute("INSERT INTO dr_exports VALUES (?,?,?,?,?,?,?,?,?)", (new, old["projectId"], old["episodeId"], old["timelineId"], "QUEUED", None, None, now, now))
            db.execute("INSERT INTO dr_export_recipes(export_id,recipe) VALUES (?,?)", (new, recipe["recipe"]))
            return {"exportId": new}
        result = self.store.mutate(f"export-retry:{xid}", body["requestKey"], body, save)
        new = result["exportId"]
        with self._schedule_lock:
            if not self.restored_original_task("dr_exports", new) and new not in self._scheduled and self.export(new)["export"]["state"] == "QUEUED":
                self._scheduled.add(new)
                self._executor.submit(self._render, new)
        return self.export(new)

    @staticmethod
    def public_recipe(recipe):
        return {**{k: v for k, v in recipe.items() if not k.startswith("_")},
                "runtimeHashes": {k: v["sha256"] for k, v in recipe.get("_runtime", {}).items()}}

    def approve_export(self, xid, body):
        fields(body, {"requestKey", "note"}, {"requestKey", "note"})
        note = text(body["note"], "审片备注", 2000)
        def save(db):
            row = self.store.one(db, "dr_exports", identifier(xid))
            if row["state"] != "SUCCEEDED":
                raise DramaError("export_not_ready", "请等待完整技术导出通过后审片", 409)
            review = {"status": "approved", "note": note, "approvedAt": time.time()}
            recipe = db.execute("SELECT 1 FROM dr_export_recipes WHERE export_id=?", (xid,)).fetchone()
            if not recipe:
                tl = self.store.one(db, "dr_timeline_revisions", row["timeline_id"])
                db.execute("INSERT INTO dr_export_recipes(export_id,recipe,outputs) VALUES (?,?,?)", (xid, encode({"timelineRevisionId": row["timeline_id"], "outputs": ["master"], "presetVersion": "legacy-v1", "subtitleRevisionId": None}), encode([{"role": "master", "assetId": row["asset_id"]}])))
            db.execute("UPDATE dr_export_recipes SET review=? WHERE export_id=?", (encode(review), xid))
            return {"exportId": xid, "review": review}
        return self.store.mutate(f"export-approve:{xid}", body["requestKey"], body, save)

    def transcription(self, jid):
        with self.store.connect() as db:
            view = self.store.one(db, "dr_transcriptions", identifier(jid))
            detail = db.execute("SELECT * FROM dr_transcription_details WHERE job_id=?", (jid,)).fetchone()
            if detail:
                view.update(baseRevision=detail["base_revision"], result=json.loads(detail["result"]) if detail["result"] else None)
            return {"transcription": view}

    def transcriptions(self, tid):
        with self.store.connect() as db:
            self.store.one(db, "dr_timeline_revisions", identifier(tid))
            ids = [r[0] for r in db.execute("SELECT id FROM dr_transcriptions WHERE timeline_id=? ORDER BY created_at DESC", (tid,))]
        return {"transcriptions": [self.transcription(jid)["transcription"] for jid in ids]}

    def create_transcription(self, tid, body):
        restore_guard(self, "write")
        fields(body, {"requestKey"}, {"requestKey"})
        if not self.asr_worker or not shutil.which(self.asr_worker):
            raise DramaError("ASR_WORKER_MISSING", "请配置本地whisper.cpp转写程序；仍可手工编辑字幕", 503)
        if not self.asr_model or not Path(self.asr_model).is_file() or Path(self.asr_model).is_symlink():
            raise DramaError("ASR_MODEL_MISSING", "请配置已下载的本地ASR模型；不会自动下载，仍可手工编辑字幕", 503)
        def save(db):
            timeline = self.store.one(db, "dr_timeline_revisions", identifier(tid))
            if db.execute("SELECT COUNT(*) FROM dr_transcriptions WHERE state IN ('QUEUED','RUNNING')").fetchone()[0] >= 8:
                raise DramaError("ASR_QUEUE_FULL", "转写队列已满", 429)
            jid, now = new_id(), time.time()
            db.execute("INSERT INTO dr_transcriptions VALUES (?,?,?,?,?,?,?,?)", (jid, tid, "QUEUED", None, None, None, now, now))
            base_revision = db.execute("SELECT COALESCE(MAX(revision),0) FROM dr_subtitle_revisions WHERE episode_id=?", (timeline["episode_id"],)).fetchone()[0]
            db.execute("INSERT INTO dr_transcription_details VALUES (?,?,NULL)", (jid, base_revision))
            return {"transcription": self.store.one(db, "dr_transcriptions", jid)}
        result = self.store.mutate(f"transcriptions:{tid}", body["requestKey"], body, save)
        jid = result["transcription"]["id"]
        with self._schedule_lock:
            current = self.transcription(jid)
            if jid not in self._scheduled and current["transcription"]["state"] == "QUEUED":
                self._scheduled.add(jid)
                self._executor.submit(self.run_transcription, jid)
        return self.transcription(jid)

    def run_transcription(self, jid):
        restore_guard(self, "write")
        from .asr import transcribe
        try:
            with self.store.connect(write=True) as db:
                job = self.store.one(db, "dr_transcriptions", jid)
                if job["state"] != "QUEUED":
                    return
                db.execute("UPDATE dr_transcriptions SET state='RUNNING',updated_at=? WHERE id=?", (time.time(), jid))
                tl_row = self.store.one(db, "dr_timeline_revisions", job["timeline_id"])
                expected = db.execute("SELECT base_revision FROM dr_transcription_details WHERE job_id=?", (jid,)).fetchone()[0]
            ep, tl, _, _ = self.post_inputs(job["timeline_id"])
            aid = self.dialogue_asset(job["timeline_id"])
            with self.store.connect(write=True) as db:
                db.execute("UPDATE dr_transcriptions SET audio_asset_id=? WHERE id=?", (aid, jid))
            segments, provenance = transcribe(self.media_path(ep["project_id"], aid), worker=self.asr_worker,
                                             model=self.asr_model, total_frames=sum(c["frames"] for c in tl["clips"]), fps=tl["fps"])
            with self.store.connect(write=True) as db:
                db.execute("UPDATE dr_transcription_details SET result=? WHERE job_id=?", (encode({"segments": segments, "provenance": provenance}), jid))
            try:
                result = self.save_subtitles(tl["id"], {"segments": segments, "expectedRevision": expected, "requestKey": "asr-result-" + jid}, source="asr", audio_asset_id=aid)
            except DramaError as exc:
                if exc.code != "revision_conflict":
                    raise
                with self.store.connect(write=True) as db:
                    db.execute("UPDATE dr_transcriptions SET state='NEEDS_REVIEW',error=?,updated_at=? WHERE id=?", ("字幕已由人工更新；识别结果已保留，请显式载入并校对另存", time.time(), jid))
                return
            # Provenance is append-only alongside the immutable subtitle, not a change to its text.
            with self.store.connect(write=True) as db:
                db.execute("INSERT INTO dr_events VALUES (?,?,?,?,?)", (new_id(), "transcription:" + jid, "asr_provenance", encode(provenance), time.time()))
                db.execute("UPDATE dr_transcriptions SET state='SUCCEEDED',subtitle_id=?,updated_at=? WHERE id=?", (result["subtitle"]["id"], time.time(), jid))
        except Exception as exc:
            message = f"{exc.code}: {exc.message}" if isinstance(exc, DramaError) else "转写失败，原音轨已保留；可继续手工字幕"
            with self.store.connect(write=True) as db:
                raw = getattr(exc, "asr_result", None)
                if raw is not None:
                    try:
                        preserved = encode(raw)
                    except (ValueError, TypeError, OverflowError):
                        preserved = encode({"rawText": str(raw.get("rawText", ""))[:4 * 1024 * 1024],
                                            "segments": [], "requiresCorrection": True, "validationError": "识别结果结构无效"})
                    db.execute("UPDATE dr_transcription_details SET result=? WHERE job_id=?", (preserved, jid))
                db.execute("UPDATE dr_transcriptions SET state=?,error=?,updated_at=? WHERE id=?", ("NEEDS_REVIEW" if raw is not None else "FAILED", message, time.time(), jid))
        finally:
            with self._schedule_lock:
                self._scheduled.discard(jid)
