"""P1 project/shot editing and CPU exports. Never starts or stops DGX models."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import time

from .contracts import DramaError, fields, identifier, integer, revision, text
from .store import DramaStore, encode, new_id
from .production import ProductionMixin
from .references import ReferenceMixin
from .video import VideoMixin
from .shot_deletion import ShotDeletionMixin
from .project_merge import ProjectMergeMixin
from .postproduction import PostproductionMixin
from .scripts import ScriptMixin
from .restore import BackupMixin
from .tasks import TaskMixin
from ..video_contracts import video_capabilities


MAX_UPLOAD = 512 * 1024 * 1024


def regular(path: Path) -> Path:
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise DramaError("invalid_media", "素材必须是普通文件")
    return path


def digest_file(path: Path) -> str:
    with regular(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class DramaService(ProjectMergeMixin, TaskMixin, BackupMixin, ProductionMixin, ReferenceMixin, VideoMixin, ShotDeletionMixin, PostproductionMixin, ScriptMixin):
    def __init__(self, state_dir: Path, repo_root: Path, reference_adapter=None, video_adapter=None, text_adapter=None):
        self.root = state_dir / "drama"
        self.repo_root = repo_root.resolve()
        self.state_dir = state_dir.resolve()
        for name in ("assets", "temp", "exports"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.init_restore_guard()
        self.claim_drama_state()
        try:
            self.store = DramaStore(self.root / "drama.sqlite3")
        except BaseException:
            self._video_owner.close()
            raise
        self._generation_lock = threading.RLock()
        self.init_backups()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="drama-export")
        self._schedule_lock = threading.Lock()
        self._scheduled: set[str] = set()
        with self.store.connect(write=True) as db:
            interrupted = [r["id"] for r in db.execute("SELECT id FROM dr_exports WHERE state IN ('QUEUED','RUNNING')") if not self.restored_original_task("dr_exports", r["id"])]
            for xid in interrupted:
                db.execute("UPDATE dr_exports SET state='INTERRUPTED',error=?,updated_at=? WHERE id=?",
                           ("服务重启，原时间线已保留；可重新导出", time.time(), xid))
        self.init_video(video_adapter)
        self.init_references(reference_adapter)
        self.init_postproduction()
        self.init_scripts(text_adapter)
        self._text_stop = threading.Event()
        self._text_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="drama-text")
        self._text_scheduled = set()

    def close(self):
        self.close_backups()
        self._text_stop.set()
        self._text_executor.shutdown(wait=True)
        self._video_stop.set()
        self.close_references()
        self.close_video()
        self._executor.shutdown(wait=True)

    def start_text_job(self, eid, body):
        result = self.create_text_job(eid, body)
        jid = result["job"]["id"]
        with self._schedule_lock:
            if jid not in self._text_scheduled and result["job"]["state"] == "QUEUED":
                self._text_scheduled.add(jid)
                self._text_executor.submit(self._run_text_job, jid)
        return result

    def _run_text_job(self, jid):
        try:
            if self._text_stop.is_set():
                return
            result = self.dispatch_text_job(jid)
            while result["job"]["state"] not in {"SUCCEEDED", "FAILED", "CANCELED", "SUBMISSION_UNKNOWN", "RECOVERY_REQUIRED"}:
                if self._text_stop.wait(3):
                    return
                try:
                    if result['job']['state'] == 'QUEUED' and not result['job']['currentAttemptId']:
                        # A known original can outlive this service restart.
                        # Query its outcome before taking the next queue slot;
                        # never redispatch an unknown original request.
                        with self.store.connect() as db:
                            prior = [r[0] for r in db.execute("SELECT id FROM dr_text_jobs WHERE id<>? AND current_attempt_id IS NOT NULL AND state NOT IN ('SUCCEEDED','FAILED','CANCELED')", (jid,))]
                        for original in prior:
                            self.reconcile_text_job(original)
                        result = self.dispatch_text_job(jid)
                    else:
                        result = self.reconcile_text_job(jid)
                except Exception:
                    # Lost GET is not permission to repeat a model POST.
                    continue
        finally:
            with self._schedule_lock:
                self._text_scheduled.discard(jid)

    def capabilities(self):
        return {"enabled": True, "stage": "P3", "backupEnabled": True, "restoreMode": self._restore_mode, "generationEnabled": self.video_adapter is not None and not self._restore_mode,
                "videoCapabilities": video_capabilities(), **self.post_capabilities(), **self.script_capabilities(),
                "videoBatchEnabled": self.video_adapter is not None and not self._restore_mode, "maxReferenceAudioBytes": 256 * 1024,
                "referenceGenerationEnabled": self.reference_adapter is not None and not self._restore_mode,
                "entityKinds": ["character", "location", "costume", "prop"], "maxImageBytes": 8 * 1024 * 1024,
                "mediaReady": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
                "presets": [{"width": 1344, "height": 768, "fps": 24}, {"width": 1080, "height": 1920, "fps": 24}],
                "fitModes": ["contain", "cover"], "maxUploadBytes": MAX_UPLOAD}

    def projects(self):
        with self.store.connect() as db:
            return {"projects": [dict(r) for r in db.execute("SELECT * FROM dr_projects ORDER BY created_at DESC")]}

    def create_project(self, body):
        fields(body, {"title", "width", "height", "fps", "requestKey", "kind"}, {"title", "requestKey"})
        title = text(body["title"], "项目名")
        width = integer(body.get("width", 1344), "width", 64, 3840)
        height = integer(body.get("height", 768), "height", 64, 3840)
        fps = integer(body.get("fps", 24), "fps", 24, 24)
        kind = body.get("kind", "drama")
        if kind not in ("drama", "mv"):
            raise DramaError("invalid_kind", "项目类型必须是短剧或MV")
        if width % 2 or height % 2 or width * height > 3840 * 2160:
            raise DramaError("invalid_canvas", "画布必须为偶数尺寸且不超过4K像素数")
        def save(db):
            pid = new_id()
            db.execute("INSERT INTO dr_projects(id,title,width,height,fps,revision,created_at,kind) VALUES (?,?,?,?,?,?,?,?)",
                       (pid, title, width, height, fps, 1, time.time(), kind))
            return {"project": self.store.one(db, "dr_projects", pid)}
        return self.store.mutate("projects", body["requestKey"], body, save)

    def _asset_view(self, row):
        return {"id": row["id"], "filename": row["filename"], "sizeBytes": row["size_bytes"], **json.loads(row["metadata"])}

    def update_project_canvas(self, pid, body):
        from .restore import restore_guard
        restore_guard(self, "write")
        fields(body, {"width", "height", "expectedRevision", "requestKey"},
               {"width", "height", "expectedRevision", "requestKey"})
        width = integer(body["width"], "width", 64, 3840)
        height = integer(body["height"], "height", 64, 3840)
        if width % 2 or height % 2 or width * height > 3840 * 2160:
            raise DramaError("invalid_canvas", "画布必须为偶数尺寸且不超过4K像素数")
        def save(db):
            project = self.store.one(db, "dr_projects", identifier(pid))
            revision(project["revision"], body["expectedRevision"])
            # Existing edit revisions contain placement/crop choices for their canvas.
            # Changing these requires a separate, explicit timeline migration.
            if (width, height) != (project["width"], project["height"]) and db.execute(
                    "SELECT 1 FROM dr_timeline_revisions t JOIN dr_episodes e ON e.id=t.episode_id WHERE e.project_id=? LIMIT 1", (pid,)).fetchone():
                raise DramaError("canvas_has_timeline", "项目已有剪辑时间线，需先迁移裁切与字幕布局，当前不能直接更改画幅", 409)
            db.execute("UPDATE dr_projects SET width=?,height=?,revision=revision+1 WHERE id=?", (width, height, pid))
            return {"project": self.store.one(db, "dr_projects", pid)}
        return self.store.mutate(f"project-canvas:{pid}", body["requestKey"], body, save)

    def project(self, pid):
        with self.store.connect() as db:
            project = self.store.one(db, "dr_projects", identifier(pid))
            episodes = [dict(r) for r in db.execute("SELECT * FROM dr_episodes WHERE project_id=? ORDER BY created_at", (pid,))]
            assets = [self._asset_view(r) for r in db.execute("SELECT a.* FROM dr_assets a JOIN dr_asset_bindings b ON b.asset_id=a.id WHERE b.project_id=? ORDER BY a.created_at", (pid,))]
        return {"project": project, "episodes": episodes, "assets": assets}

    def create_episode(self, pid, body):
        fields(body, {"title", "requestKey"}, {"title", "requestKey"})
        title = text(body["title"], "分集名")
        def save(db):
            self.store.one(db, "dr_projects", identifier(pid))
            eid = new_id()
            db.execute("INSERT INTO dr_episodes(id,project_id,title,revision,timeline_revision,created_at) VALUES (?,?,?,?,?,?)",
                       (eid, pid, title, 1, 0, time.time()))
            return {"episode": self.store.one(db, "dr_episodes", eid)}
        return self.store.mutate(f"episodes:{pid}", body["requestKey"], body, save)

    def _confined_import_path(self, value, label):
        if not isinstance(value, str) or not value:
            raise DramaError("invalid_input", "导入路径必须是绝对路径")
        candidate = Path(value)
        if not candidate.is_absolute():
            raise DramaError("invalid_input", "导入路径必须是绝对路径")
        resolved = candidate.resolve()
        roots = (self.repo_root, self.state_dir, self.root.resolve())
        if not any(resolved == root or root in resolved.parents for root in roots):
            raise DramaError("path_not_allowed", f"{label}路径必须位于仓库、状态目录或项目素材目录内")
        return regular(resolved)

    def _confined_audio_path(self, value):
        return self._confined_import_path(value, "音频")

    def import_audio(self, pid, body):
        fields(body, {"path", "assetId", "filename", "requestKey"}, {"requestKey"})
        identifier(pid)
        if ("path" in body) == ("assetId" in body):
            raise DramaError("invalid_fields", "导入音频须提供本机路径或已有资产哈希之一")
        if "assetId" in body:
            aid = identifier(body["assetId"])
            if len(aid) != 64:
                raise DramaError("not_found", "找不到指定对象", 404)
            def save(db):
                self.store.one(db, "dr_projects", pid)
                asset = self.store.one(db, "dr_assets", aid)
                if json.loads(asset["metadata"]).get("kind") != "audio":
                    raise DramaError("audio_required", "只能导入已归档的音频素材")
                db.execute("INSERT OR IGNORE INTO dr_asset_bindings VALUES (?,?)", (pid, aid))
                return {"asset": self._asset_view(self.store.one(db, "dr_assets", aid))}
            return self.store.mutate(f"audio-import:{pid}", body["requestKey"], body, save)
        try:
            path = self._confined_audio_path(body["path"])
        except (OSError, DramaError) as exc:
            if isinstance(exc, DramaError) and exc.code != "invalid_media":
                raise
            raise DramaError("invalid_media", "素材必须是普通文件") from exc
        filename = text(body["filename"], "文件名", 255) if "filename" in body else text(path.name, "文件名", 255)
        with path.open("rb") as stream:
            return self.upload_audio(pid, stream, path.stat().st_size, filename, body["requestKey"])

    def import_video(self, pid, body):
        fields(body, {"path", "assetId", "filename", "requestKey"}, {"requestKey"})
        identifier(pid)
        if ("path" in body) == ("assetId" in body):
            raise DramaError("invalid_fields", "导入视频须提供本机路径或已有资产哈希之一")
        if "assetId" in body:
            aid = identifier(body["assetId"])
            if len(aid) != 64:
                raise DramaError("not_found", "找不到指定对象", 404)
            def save(db):
                self.store.one(db, "dr_projects", pid)
                asset = self.store.one(db, "dr_assets", aid)
                metadata = json.loads(asset["metadata"])
                self._require_action_video(metadata)
                db.execute("INSERT OR IGNORE INTO dr_asset_bindings VALUES (?,?)", (pid, aid))
                return {"asset": self._asset_view(self.store.one(db, "dr_assets", aid))}
            return self.store.mutate(f"video-import:{pid}", body["requestKey"], body, save)
        try:
            path = self._confined_import_path(body["path"], "视频")
        except (OSError, DramaError) as exc:
            if isinstance(exc, DramaError) and exc.code != "invalid_media":
                raise
            raise DramaError("invalid_media", "素材必须是普通文件") from exc
        from .media import probe_media
        try:
            metadata = probe_media(path)
        except (ValueError, OSError, RuntimeError) as exc:
            raise DramaError("invalid_media", "视频无法完整解码，或媒体工具未就绪") from exc
        self._require_action_video(metadata)
        filename = text(body["filename"], "文件名", 255) if "filename" in body else text(path.name, "文件名", 255)
        with path.open("rb") as stream:
            return self.upload(pid, stream, path.stat().st_size, filename, body["requestKey"])

    def import_image(self, pid, body):
        fields(body, {"path", "assetId", "filename", "requestKey"}, {"requestKey"})
        identifier(pid)
        if ("path" in body) == ("assetId" in body):
            raise DramaError("invalid_fields", "导入图片须提供本机路径或已有资产哈希之一")
        if "assetId" in body:
            aid = identifier(body["assetId"])
            if len(aid) != 64:
                raise DramaError("not_found", "找不到指定对象", 404)
            def save(db):
                self.store.one(db, "dr_projects", pid)
                asset = self.store.one(db, "dr_assets", aid)
                if json.loads(asset["metadata"]).get("kind") != "image":
                    raise DramaError("image_required", "只能导入已归档的图片素材")
                db.execute("INSERT OR IGNORE INTO dr_asset_bindings VALUES (?,?)", (pid, aid))
                return {"asset": self._asset_view(self.store.one(db, "dr_assets", aid))}
            return self.store.mutate(f"image-import:{pid}", body["requestKey"], body, save)
        try:
            path = self._confined_import_path(body["path"], "图片")
        except (OSError, DramaError) as exc:
            if isinstance(exc, DramaError) and exc.code != "invalid_media":
                raise
            raise DramaError("invalid_media", "素材必须是普通文件") from exc
        filename = text(body["filename"], "文件名", 255) if "filename" in body else text(path.name, "文件名", 255)
        with path.open("rb") as stream:
            return self.upload_image(pid, stream, path.stat().st_size, filename, body["requestKey"])

    @staticmethod
    def _require_action_video(metadata):
        duration = metadata.get("duration")
        if metadata.get("kind") != "video" or not isinstance(duration, (int, float)) or not 2 <= duration <= 15:
            raise DramaError("invalid_video_reference", "动作参考视频须为可完整解码的 2–15 秒 MP4")

    def bind_song(self, eid, body):
        fields(body, {"songAssetId", "expectedRevision", "requestKey"}, {"songAssetId", "expectedRevision", "requestKey"})
        aid = identifier(body["songAssetId"])
        if len(aid) != 64:
            raise DramaError("not_found", "找不到指定对象", 404)
        def save(db):
            ep = self.store.one(db, "dr_episodes", identifier(eid))
            revision(ep["revision"], body["expectedRevision"])
            project = self.store.one(db, "dr_projects", ep["project_id"])
            if project["kind"] != "mv":
                raise DramaError("not_mv", "只有MV项目可以绑定本集歌曲")
            asset = self._bound_asset(db, ep["project_id"], aid)
            if json.loads(asset["metadata"]).get("kind") != "audio":
                raise DramaError("audio_required", "歌曲必须是已绑定的音频素材")
            db.execute("UPDATE dr_episodes SET song_asset_id=?,revision=revision+1 WHERE id=?", (aid, eid))
            return {"episode": self.store.one(db, "dr_episodes", eid)}
        return self.store.mutate(f"song:{eid}", body["requestKey"], body, save)

    def _song_clock(self, db, ep, project):
        aid = ep["song_asset_id"]
        if not aid:
            raise DramaError("song_required", "MV须先绑定本集歌曲")
        asset = self._bound_asset(db, ep["project_id"], aid)
        info = json.loads(asset["metadata"])
        if info.get("kind") != "audio":
            raise DramaError("audio_required", "歌曲必须是已绑定的音频素材")
        frames = math.floor(info["duration"] * project["fps"])
        if frames < 1:
            raise DramaError("invalid_audio", "歌曲时长不足以形成成片时钟")
        return aid, frames

    @staticmethod
    def _ensure_song_bgm(clips, song_id, song_frames):
        matches = [c for c in clips if isinstance(c, dict) and c.get("assetId") == song_id and c.get("track") == "bgm"]
        if not matches:
            return [{"audioClipId": "song-bgm", "assetId": song_id, "track": "bgm",
                     "startFrame": 0, "sourceInFrame": 0, "frames": song_frames}, *clips]
        for clip in matches:
            if clip.get("startFrame", 0) != 0 or clip.get("sourceInFrame", 0) != 0 or clip.get("frames") != song_frames:
                raise DramaError("song_clock_required", "MV主歌曲BGM必须从0帧铺满整首歌")
        return clips

    def _library_paths(self):
        sources = {}
        for root, pattern in ((self.repo_root / "creative-projects", "*/video/*.mp4"),
                              (self.state_dir / "creative-artifacts", "*/*.mp4")):
            if not root.is_dir() or root.is_symlink():
                continue
            for path in sorted(root.glob(pattern)):
                if len(sources) >= 500:
                    break
                if any(part.is_symlink() for part in (path, path.parent)) or not path.is_file():
                    continue
                if root.resolve() not in path.resolve().parents:
                    continue
                sid = hashlib.sha256(str(path).encode()).hexdigest()
                sources[sid] = path
        return sources

    def library(self):
        return {"sources": [{"sourceId": key, "title": path.name} for key, path in self._library_paths().items()]}

    def import_source(self, pid, body):
        fields(body, {"sourceId", "requestKey"}, {"sourceId", "requestKey"})
        path = self._library_paths().get(identifier(body["sourceId"]))
        if path is None:
            raise DramaError("source_not_found", "原素材已移走或不在素材库中", 404)
        with regular(path).open("rb") as stream:
            return self.upload(pid, stream, path.stat().st_size, path.name, body["requestKey"])

    def upload(self, pid, stream, length, filename, key):
        from .media import probe_media
        integer(length, "文件字节数", 1, MAX_UPLOAD)
        filename = text(filename, "文件名", 255)
        text(key, "requestKey", 128)
        with self.store.connect() as db:
            self.store.one(db, "dr_projects", identifier(pid))
        if shutil.disk_usage(self.root).free < length + 128 * 1024 * 1024:
            raise DramaError("disk_full", "磁盘空间不足，请先释放空间", 507)
        fd, name = tempfile.mkstemp(prefix="upload-", suffix=".mp4", dir=self.root / "temp")
        temporary = Path(name)
        try:
            remaining = length
            with os.fdopen(fd, "wb") as target:
                while remaining:
                    data = stream.read(min(1024 * 1024, remaining))
                    if not data:
                        raise DramaError("upload_truncated", "上传未完成，请使用原请求编号重试")
                    target.write(data)
                    remaining -= len(data)
                target.flush()
                os.fsync(target.fileno())
            try:
                metadata = probe_media(temporary)
            except (ValueError, OSError, RuntimeError) as exc:
                raise DramaError("invalid_media", "视频无法完整解码，或媒体工具未就绪") from exc
            aid = self._publish(temporary)
            def save(db):
                self._bind_asset(db, pid, aid, filename, metadata, length)
                return {"asset": self._asset_view(self.store.one(db, "dr_assets", aid))}
            return self.store.mutate(f"upload:{pid}", key, {"sha256": aid, "filename": filename}, save)
        finally:
            temporary.unlink(missing_ok=True)

    def _publish(self, source, suffix=".mp4"):
        if suffix not in {".mp4", ".png", ".jpg", ".webp", ".wav", ".mp3", ".m4a", ".flac", ".srt", ".ass", ".json"}:
            raise DramaError("invalid_media", "不支持的归档类型")
        aid = digest_file(source)
        target = self.root / "assets" / (aid + suffix)
        try:
            os.link(source, target)
        except FileExistsError:
            if digest_file(target) != aid:
                raise DramaError("asset_corrupt", "归档文件校验失败，原文件未覆盖", 409)
        directory_fd = os.open(self.root / "assets", os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return aid

    @staticmethod
    def _bind_asset(db, pid, aid, filename, metadata, size):
        db.execute("INSERT OR IGNORE INTO dr_assets VALUES (?,?,?,?,?)", (aid, filename, encode(metadata), size, time.time()))
        db.execute("INSERT OR IGNORE INTO dr_asset_bindings VALUES (?,?)", (pid, aid))

    def _bound_asset(self, db, pid, aid):
        asset = self.store.one(db, "dr_assets", identifier(aid))
        if not db.execute("SELECT 1 FROM dr_asset_bindings WHERE project_id=? AND asset_id=?", (pid, aid)).fetchone():
            raise DramaError("not_found", "项目中没有此素材", 404)
        return asset

    def media_path(self, pid, aid):
        with self.store.connect() as db:
            asset = self._bound_asset(db, identifier(pid), aid)
        extension = json.loads(asset["metadata"]).get("extension", "mp4")
        if extension not in {"mp4", "png", "jpg", "webp", "wav", "mp3", "m4a", "flac", "srt", "ass", "json"}:
            raise DramaError("invalid_media", "不支持的归档类型")
        path = self.root / "assets" / (aid + "." + extension)
        try:
            return regular(path)
        except (OSError, DramaError) as exc:
            raise DramaError("media_missing", "素材文件缺失或不可读取，请从备份恢复", 404) from exc

    def _shot_view(self, db, row):
        return {"id": row["id"], "title": row["title"], "revision": row["revision"],
                "currentRevisionId": row["current_content_revision_id"],
                "needsReview": any(not item["resolved"] for item in self._invalidations(db, row["current_content_revision_id"])),
                "ordinal": row["ordinal"], "selectedTakeId": row["selected_take_id"],
                "takes": [{"id": t["id"], "assetId": t["asset_id"], "source": t["source"], "sourceRevisionId": t["source_revision_id"],
                           "reviewStatus": (db.execute("SELECT decision FROM dr_take_reviews WHERE take_id=?", (t["id"],)).fetchone() or ["pending"])[0],
                           "stale": t["source_revision_id"] != row["current_content_revision_id"]}
                          for t in db.execute("SELECT * FROM dr_takes WHERE shot_id=? ORDER BY created_at", (row["id"],))]}

    def episode(self, eid):
        with self.store.connect() as db:
            episode = self.store.one(db, "dr_episodes", identifier(eid))
            shots = [self._shot_view(db, r) for r in db.execute("SELECT * FROM dr_shots WHERE episode_id=? ORDER BY ordinal", (eid,))]
            timeline = db.execute("SELECT snapshot FROM dr_timeline_revisions WHERE episode_id=? ORDER BY revision DESC LIMIT 1", (eid,)).fetchone()
            exports = [self._export_view(r) for r in db.execute("SELECT * FROM dr_exports WHERE episode_id=? ORDER BY created_at DESC", (eid,))]
        exports = [self.export(item["id"])["export"] for item in exports]
        return {"episode": episode, "shots": shots, "timeline": json.loads(timeline[0]) if timeline else None, "exports": exports}

    def create_shot(self, eid, body):
        fields(body, {"title", "assetId", "requestKey"}, {"title", "requestKey"})
        title = text(body["title"], "镜头名")
        def save(db):
            episode = self.store.one(db, "dr_episodes", identifier(eid))
            if body.get("assetId"):
                asset = self._bound_asset(db, episode["project_id"], body["assetId"])
                if json.loads(asset["metadata"]).get("kind") != "video":
                    raise DramaError("video_required", "已有视频候选必须是视频素材")
            sid, tid = new_id(), new_id()
            ordinal = db.execute("SELECT COALESCE(MAX(ordinal),0)+1 FROM dr_shots WHERE episode_id=?", (eid,)).fetchone()[0]
            db.execute("INSERT INTO dr_shots(id,episode_id,title,ordinal,revision,selected_take_id,created_at) VALUES (?,?,?,?,?,?,?)", (sid, eid, title, ordinal, 1, None, time.time()))
            if body.get("assetId"):
                db.execute("INSERT INTO dr_takes(id,shot_id,asset_id,source,created_at) VALUES (?,?,?,?,?)", (tid, sid, body["assetId"], "imported", time.time()))
            db.execute("INSERT INTO dr_shot_revisions(shot_id,revision,snapshot,created_at) VALUES (?,?,?,?)", (sid, 1, encode({"title": title, "assetId": body.get("assetId")}), time.time()))
            self._initialize_shot_content(db, sid)
            db.execute("UPDATE dr_episodes SET revision=revision+1 WHERE id=?", (eid,))
            return {"shot": self._shot_view(db, self.store.one(db, "dr_shots", sid))}
        return self.store.mutate(f"shots:{eid}", body["requestKey"], body, save)

    def select_take(self, sid, body):
        fields(body, {"takeId", "expectedRevision", "acceptStale", "requestKey"}, {"takeId", "expectedRevision", "requestKey"})
        def save(db):
            shot = self.store.one(db, "dr_shots", identifier(sid))
            revision(shot["revision"], body["expectedRevision"])
            take = self.store.one(db, "dr_takes", identifier(body["takeId"]))
            if take["shot_id"] != sid:
                raise DramaError("wrong_shot", "候选不属于这个镜头")
            if take["source"] == "h3_video":
                review = db.execute("SELECT decision FROM dr_take_reviews WHERE take_id=?", (take["id"],)).fetchone()
                if review is None or review[0] != "approved":
                    raise DramaError("review_required", "生成候选须先通过人工审片才能采用", 409)
            if "acceptStale" in body and type(body["acceptStale"]) is not bool:
                raise DramaError("invalid_confirmation", "旧候选确认必须为布尔值")
            if take["source_revision_id"] != shot["current_content_revision_id"] and body.get("acceptStale") is not True:
                raise DramaError("stale_candidate", "候选来自旧镜头修订，请明确确认采用", 409)
            db.execute("UPDATE dr_shots SET selected_take_id=?,revision=revision+1 WHERE id=?", (take["id"], sid))
            return {"shot": self._shot_view(db, self.store.one(db, "dr_shots", sid))}
        return self.store.mutate(f"selection:{sid}", body["requestKey"], body, save)

    def reorder(self, eid, body):
        fields(body, {"shotIds", "expectedRevision", "requestKey"}, {"shotIds", "expectedRevision", "requestKey"})
        ids = body["shotIds"]
        if not isinstance(ids, list) or not all(isinstance(s, str) for s in ids) or len(set(ids)) != len(ids):
            raise DramaError("invalid_order", "镜头顺序必须包含每个镜头且不能重复")
        def save(db):
            ep = self.store.one(db, "dr_episodes", identifier(eid))
            revision(ep["revision"], body["expectedRevision"])
            existing = {r[0] for r in db.execute("SELECT id FROM dr_shots WHERE episode_id=?", (eid,))}
            if set(ids) != existing:
                raise DramaError("invalid_order", "镜头列表已变化，请刷新")
            db.execute("UPDATE dr_shots SET ordinal=-ordinal WHERE episode_id=?", (eid,))
            for index, sid in enumerate(ids, 1):
                db.execute("UPDATE dr_shots SET ordinal=? WHERE id=?", (index, sid))
            db.execute("UPDATE dr_episodes SET revision=revision+1 WHERE id=?", (eid,))
            return {"episode": self.store.one(db, "dr_episodes", eid)}
        return self.store.mutate(f"order:{eid}", body["requestKey"], body, save)

    def save_timeline(self, eid, body):
        fields(body, {"clips", "audioClips", "expectedRevision", "requestKey"}, {"clips", "expectedRevision", "requestKey"})
        clips = body["clips"]
        if not isinstance(clips, list) or not 1 <= len(clips) <= 64:
            raise DramaError("invalid_timeline", "时间线需要1–64个片段")
        def save(db):
            ep = self.store.one(db, "dr_episodes", identifier(eid))
            revision(ep["timeline_revision"], body["expectedRevision"])
            latest = db.execute("SELECT snapshot FROM dr_timeline_revisions WHERE episode_id=? ORDER BY revision DESC LIMIT 1", (eid,)).fetchone()
            if latest and json.loads(latest[0]).get("schemaVersion") == "drama-timeline-v2" and "audioClips" not in body:
                raise DramaError("timeline_version_required", "此时间线含新版后期字段，请使用新版剪辑台完整保存音轨", 409)
            project = self.store.one(db, "dr_projects", ep["project_id"])
            normalized, seen = [], set()
            for clip in clips:
                fields(clip, {"clipId", "takeId", "inFrame", "frames", "keepSourceAudio", "fit", "crop"}, {"clipId", "takeId", "inFrame", "frames"})
                cid = text(clip["clipId"], "片段ID", 128)
                if cid in seen:
                    raise DramaError("duplicate_clip", "片段ID不能重复")
                seen.add(cid)
                take = self.store.one(db, "dr_takes", identifier(clip["takeId"]))
                shot = self.store.one(db, "dr_shots", take["shot_id"])
                if shot["episode_id"] != eid or shot["selected_take_id"] != take["id"]:
                    raise DramaError("take_not_selected", "片段必须使用本集已采用候选")
                asset = self._bound_asset(db, ep["project_id"], take["asset_id"])
                info = json.loads(asset["metadata"])
                start = integer(clip["inFrame"], "入点帧", 0, 86400)
                length = integer(clip["frames"], "片段帧数", 1, 86400)
                available = math.floor(info["frames"] * project["fps"] / info["fps"] + 1e-6)
                if start + length > available:
                    raise DramaError("clip_out_of_bounds", f"片段超过素材可用范围（{available}帧）")
                keep = clip.get("keepSourceAudio", project["kind"] != "mv")
                if type(keep) is not bool:
                    raise DramaError("invalid_clip", "原声开关必须是布尔值")
                geometry = self.normalize_crop(clip)
                normalized.append({"clipId": cid, "takeId": take["id"], "assetId": take["asset_id"], "inFrame": start,
                                   "frames": length, "keepSourceAudio": keep, **geometry})
            video_total = sum(c["frames"] for c in normalized)
            if video_total > 86400:
                raise DramaError("timeline_too_long", "时间线不能超过一小时")
            is_mv = project["kind"] == "mv"
            if is_mv:
                song_id, song_length = self._song_clock(db, ep, project)
                if video_total != song_length:
                    raise DramaError("timeline_length_mismatch", f"画面总帧必须等于歌曲长度（{song_length}帧）；请补黑场或定格，不会自动补帧")
                clock = song_length
                audio_clips = self._ensure_song_bgm(list(body.get("audioClips", [])), song_id, song_length)
            else:
                clock = video_total
                audio_clips = body.get("audioClips", [])
            tid, rev = new_id(), ep["timeline_revision"] + 1
            snapshot = {"id": tid, "revision": rev, "width": project["width"], "height": project["height"], "fps": project["fps"], "clips": normalized}
            if is_mv or "audioClips" in body or any(c["fit"] == "cover" for c in normalized):
                snapshot.update(schemaVersion="drama-timeline-v2", audioClips=self.normalize_audio_clips(db, ep["project_id"], audio_clips, clock))
            db.execute("INSERT INTO dr_timeline_revisions VALUES (?,?,?,?,?)", (tid, eid, rev, encode(snapshot), time.time()))
            db.execute("UPDATE dr_episodes SET timeline_revision=? WHERE id=?", (rev, eid))
            return {"timeline": snapshot}
        return self.store.mutate(f"timeline:{eid}", body["requestKey"], body, save)

    @staticmethod
    def _export_view(row):
        return {"id": row["id"], "projectId": row["project_id"], "episodeId": row["episode_id"],
                "timelineId": row["timeline_id"], "state": row["state"], "assetId": row["asset_id"],
                "error": row["error"], "createdAt": row["created_at"]}

    def export(self, xid):
        with self.store.connect() as db:
            view = self._export_view(self.store.one(db, "dr_exports", identifier(xid)))
            recipe = db.execute("SELECT * FROM dr_export_recipes WHERE export_id=?", (xid,)).fetchone()
            if recipe:
                view.update(recipe=self.public_recipe(json.loads(recipe["recipe"])), outputs=json.loads(recipe["outputs"]), review=json.loads(recipe["review"]) if recipe["review"] else None)
            return {"export": view}

    def jobs(self, pid):
        with self.store.connect() as db:
            self.store.one(db, "dr_projects", identifier(pid))
            return {"jobs": [self._export_view(r) for r in db.execute("SELECT * FROM dr_exports WHERE project_id=? ORDER BY created_at DESC", (pid,))]}

    def create_export(self, tid, body):
        fields(body, {"requestKey", "outputs", "subtitleRevisionId", "presetVersion"}, {"requestKey"})
        def save(db):
            tl = self.store.one(db, "dr_timeline_revisions", identifier(tid))
            extended = json.loads(tl["snapshot"]).get("schemaVersion") == "drama-timeline-v2" or bool(set(body).intersection({"outputs", "subtitleRevisionId", "presetVersion"}))
            if not self.capabilities()["postMediaReady" if extended else "mediaReady"]:
                raise DramaError("media_tools_missing", "Gateway运行环境缺少所需的ffmpeg或ffprobe", 503)
            ep = self.store.one(db, "dr_episodes", tl["episode_id"])
            if self.active_export_count(db) >= 32:
                raise DramaError("export_queue_full", "导出队列已满，请稍后提交", 429)
            xid, now = new_id(), time.time()
            db.execute("INSERT INTO dr_exports VALUES (?,?,?,?,?,?,?,?,?)", (xid, ep["project_id"], ep["id"], tid, "QUEUED", None, None, now, now))
            self.freeze_export_recipe(db, xid, tl, body)
            return {"export": self._export_view(self.store.one(db, "dr_exports", xid))}
        response = self.store.mutate(f"exports:{tid}", body["requestKey"], body, save)
        xid = response["export"]["id"]
        with self._schedule_lock:
            current = self.export(xid)
            if not self.restored_original_task("dr_exports", xid) and xid not in self._scheduled and current["export"]["state"] == "QUEUED":
                self._scheduled.add(xid)
                self._executor.submit(self._render, xid)
        return self.export(xid)

    def _render(self, xid):
        if self.restored_original_task("dr_exports", xid):
            return
        with self.store.connect() as db:
            extended = db.execute("SELECT 1 FROM dr_export_recipes WHERE export_id=?", (xid,)).fetchone()
        if extended:
            return self.render_post_export(xid)
        from .media import render_timeline
        try:
            with self.store.connect(write=True) as db:
                row = self.store.one(db, "dr_exports", xid)
                if row["state"] != "QUEUED":
                    return
                db.execute("UPDATE dr_exports SET state='RUNNING',updated_at=? WHERE id=?", (time.time(), xid))
                tl = json.loads(self.store.one(db, "dr_timeline_revisions", row["timeline_id"])["snapshot"])
            clips = []
            for clip in tl["clips"]:
                path = self.media_path(row["project_id"], clip["assetId"])
                if digest_file(path) != clip["assetId"]:
                    raise DramaError("asset_corrupt", "素材哈希不匹配，请恢复原素材")
                clips.append({**clip, "path": path})
            output = self.root / "exports" / (xid + ".mp4")
            metadata = render_timeline(clips, output, width=tl["width"], height=tl["height"], fps=tl["fps"])
            aid = self._publish(output)
            with self.store.connect(write=True) as db:
                self._bind_asset(db, row["project_id"], aid, f"成片-{xid[:8]}.mp4", metadata, output.stat().st_size)
                db.execute("UPDATE dr_exports SET state='SUCCEEDED',asset_id=?,updated_at=? WHERE id=?", (aid, time.time(), xid))
        except Exception as exc:
            message = exc.message if isinstance(exc, DramaError) else "导出失败，请检查源素材、磁盘空间和媒体工具；原时间线已保留"
            with self.store.connect(write=True) as db:
                db.execute("UPDATE dr_exports SET state='FAILED',error=?,updated_at=? WHERE id=?", (message, time.time(), xid))
        finally:
            with self._schedule_lock:
                self._scheduled.discard(xid)
