"""Explicit drama routes, including binary upload and seekable media."""
from __future__ import annotations

import json
import re
import sqlite3
from urllib.parse import unquote, urlsplit, parse_qs
import uuid

from .contracts import DramaError
from .service import MAX_UPLOAD


PREFIX = "/api/v1/drama"


def media_response(handler, path):
    # Open first so Content-Length and streamed bytes refer to the same file.
    import os
    import stat
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise DramaError("media_missing", "素材不可读取", 404)
        size = info.st_size
        start, end, response = 0, size - 1, 200
        requested = handler.headers.get("Range")
        if requested:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
            try:
                if not match or not any(match.groups()):
                    raise ValueError()
                first, last = match.groups()
                if first:
                    start = int(first)
                    end = min(int(last), size - 1) if last else size - 1
                else:
                    amount = int(last)
                    if amount <= 0:
                        raise ValueError()
                    start = max(0, size - amount)
                if start < 0 or start >= size or end < start:
                    raise ValueError()
                response = 206
            except (ValueError, OverflowError):
                handler.send_response(416)
                handler.send_header("Content-Range", f"bytes */{size}")
                handler.send_header("Content-Length", "0")
                handler.end_headers()
                return
        count = max(0, end - start + 1)
        handler.send_response(response)
        content_type = {".mp4": "video/mp4", ".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp", ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".flac": "audio/flac", ".srt": "text/plain; charset=utf-8", ".ass": "text/plain; charset=utf-8", ".json": "application/json", ".zip": "application/zip"}[path.suffix]
        for key, value in {"Content-Type": content_type, "Content-Length": str(count),
                           "Accept-Ranges": "bytes", "Cache-Control": "private, no-cache",
                           "X-Content-Type-Options": "nosniff"}.items():
            handler.send_header(key, value)
        if path.suffix == ".zip":
            handler.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        if response == 206:
            handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        handler.end_headers()
        if handler.command == "HEAD":
            return
        stream.seek(start)
        while count:
            data = stream.read(min(count, 1024 * 1024))
            if not data:
                handler.close_connection = True
                return
            handler.wfile.write(data)
            count -= len(data)


def handle(handler) -> bool:
    parsed = urlsplit(handler.path)
    if parsed.path != PREFIX and not parsed.path.startswith(PREFIX + "/"):
        return False
    request_id = uuid.uuid4().hex
    app = handler.server.application
    service = getattr(app, "drama", None)
    relative = parsed.path[len(PREFIX):].rstrip("/")
    try:
        if relative == "/capabilities" and handler.command == "GET":
            handler._json(200, service.capabilities() if service else {"enabled": False, "stage": "disabled"})
            return True
        if service is None:
            raise DramaError("disabled", "短剧工作台尚未启用", 404)
        if not app.authorized(handler.headers.get("Cookie")):
            raise DramaError("forbidden", "当前连接没有访问权限", 403)
        depth = 0
        while restored := re.fullmatch(r"/restored/([a-f0-9]{32})(/.*)", relative):
            depth += 1
            if depth > 4:
                raise DramaError("invalid_workspace", "恢复工作区层级超过上限", 422)
            service = service.restored_service(restored[1])
            relative = restored[2].rstrip("/")
        if handler.command == "POST" and not handler._mutation_allowed():
            raise DramaError("forbidden", "请求来源不受信任", 403)
        if handler.command in {"GET", "HEAD"}:
            if relative == "/capabilities" and handler.command == "GET":
                handler._json(200, service.capabilities()); return True
            download = re.fullmatch(r"/backups/([a-f0-9]{32})/download", relative)
            if download:
                media_response(handler, service.backup_download(download[1])); return True
            match = re.fullmatch(r"/projects/([a-f0-9]{32})/assets/([a-f0-9]{64})/media", relative)
            if match:
                media_response(handler, service.media_path(*match.groups()))
                return True
            if handler.command == "HEAD":
                raise DramaError("not_found", "接口不存在", 404)
            if match := re.fullmatch(r"/projects/([a-f0-9]{32})/backups", relative):
                query = parse_qs(parsed.query,keep_blank_values=True)
                if set(query)-{'before'} or any(len(v)!=1 for v in query.values()):
                    raise DramaError("invalid_query", "归档分页参数无效")
                result = service.project_backups(match[1],query.get('before',[None])[0])
            elif match := re.fullmatch(r"/(?:backups|restores)/([a-f0-9]{32})", relative):
                result = service.backup_operation(match[1])
            elif relative == "/tasks":
                query = parse_qs(parsed.query, keep_blank_values=True)
                if set(query) - {"projectId"} or any(len(v) != 1 for v in query.values()):
                    raise DramaError("invalid_query", "任务查询参数无效")
                result = service.task_index(query.get("projectId", [None])[0])
            elif relative == "/projects":
                result = service.projects()
            elif relative == "/library":
                result = service.library()
            elif match := re.fullmatch(r"/episodes/([a-f0-9]{32})/story", relative):
                result = service.story_state(match[1])
            elif match := re.fullmatch(r"/episodes/([a-f0-9]{32})/text-jobs", relative):
                query = parse_qs(parsed.query)
                if set(query) != {"requestKey"} or len(query["requestKey"]) != 1:
                    raise DramaError("invalid_input", "请提供原任务requestKey")
                result = service.find_text_job(match[1], query["requestKey"][0])
            elif match := re.fullmatch(r"/text-jobs/([a-f0-9]{32})", relative):
                result = service.text_job(match[1])
            elif match := re.fullmatch(r"/shots/([a-f0-9]{32})/script-review", relative):
                result = service.script_invalidations(match[1])
            elif match := re.fullmatch(r"/projects/([a-f0-9]{32})/production-state", relative):
                result = service.production_state(match[1])
            elif match := re.fullmatch(r"/projects/([a-f0-9]{32})/video-jobs", relative):
                result = service.video_jobs(match[1])
            elif match := re.fullmatch(r"/video-jobs/([a-f0-9]{32})", relative):
                result = service.video_job(match[1])
            elif match := re.fullmatch(r"/projects/([a-f0-9]{32})/entities", relative):
                result = service.entities(match[1])
            elif match := re.fullmatch(r"/entities/([a-f0-9]{32})", relative):
                result = service.entity(match[1])
            elif match := re.fullmatch(r"/shots/([a-f0-9]{32})/production", relative):
                result = service.shot_production(match[1])
            elif match := re.fullmatch(r"/shots/([a-f0-9]{32})/canvas", relative):
                result = service.canvas_projection(match[1])
            elif match := re.fullmatch(r"/projects/([a-f0-9]{32})/image-library", relative):
                service.project(match[1])
                result = service.image_library()
            elif match := re.fullmatch(r"/projects/([a-f0-9]{32})/reference-jobs", relative):
                result = service.reference_jobs(match[1])
            elif match := re.fullmatch(r"/projects/([a-f0-9]{32})/image-review-queue", relative):
                query = parse_qs(parsed.query, keep_blank_values=True)
                if set(query) - {"scope", "episodeId"} or any(len(value) != 1 for value in query.values()):
                    raise DramaError("invalid_query", "审图队列参数无效")
                result = service.image_review_queue(match[1], query.get("episodeId", [None])[0], query.get("scope", ["episode"])[0])
            elif match := re.fullmatch(r"/reference-jobs/([a-f0-9]{32})", relative):
                result = service.reference_job(match[1])
            elif match := re.fullmatch(r"/projects/([a-f0-9]{32})", relative):
                result = service.project(match[1])
            elif match := re.fullmatch(r"/projects/([a-f0-9]{32})/jobs", relative):
                result = service.jobs(match[1])
            elif match := re.fullmatch(r"/episodes/([a-f0-9]{32})", relative):
                result = service.episode(match[1])
            elif match := re.fullmatch(r"/timeline-revisions/([a-f0-9]{32})/subtitles", relative):
                result = service.subtitles(match[1])
            elif match := re.fullmatch(r"/transcriptions/([a-f0-9]{32})", relative):
                result = service.transcription(match[1])
            elif match := re.fullmatch(r"/timeline-revisions/([a-f0-9]{32})/transcriptions", relative):
                result = service.transcriptions(match[1])
            elif match := re.fullmatch(r"/exports/([a-f0-9]{32})", relative):
                result = service.export(match[1])
            else:
                raise DramaError("not_found", "接口不存在", 404)
            handler._json(200, result)
            return True
        if handler.command != "POST":
            raise DramaError("method_not_allowed", "不支持此请求方法", 405)
        if relative == "/restore-uploads":
            if handler.headers.get_content_type() != "application/octet-stream" or handler.headers.get("Transfer-Encoding"):
                raise DramaError("invalid_upload", "项目包上传需要明确长度的二进制请求")
            try:
                length = int(handler.headers.get("Content-Length", "-1"))
            except ValueError:
                length = -1
            handler.close_connection = True
            old_timeout = handler.connection.gettimeout()
            handler.connection.settimeout(60)
            try:
                result = service.upload_restore(handler.rfile, length, handler.headers.get("X-Request-Key"), handler.headers.get("X-Content-SHA256"))
            finally:
                handler.connection.settimeout(old_timeout)
            handler._json(201, result, {"Connection":"close"}); return True
        match = re.fullmatch(r"/projects/([a-f0-9]{32})/(assets|images|reference-audio|audio-assets)", relative)
        if match:
            if handler.headers.get_content_type() != "application/octet-stream" or handler.headers.get("Transfer-Encoding"):
                raise DramaError("invalid_upload", "视频上传需要明确长度的二进制请求")
            try:
                length = int(handler.headers.get("Content-Length", "-1"))
            except ValueError:
                length = -1
            limit = {"images": 8 * 1024 * 1024, "reference-audio": 256 * 1024, "assets": MAX_UPLOAD, "audio-assets": 256 * 1024 * 1024}[match[2]]
            if not 1 <= length <= limit:
                raise DramaError("upload_size", "图片须在8MiB以内，视频须在512MiB以内，参考音频须在256KiB以内，且长度明确", 413)
            old_timeout = handler.connection.gettimeout()
            handler.connection.settimeout(60)
            try:
                upload = {"images": service.upload_image, "assets": service.upload, "reference-audio": service.upload_reference_audio, "audio-assets": service.upload_audio}[match[2]]
                result = upload(match[1], handler.rfile, length,
                                        unquote(handler.headers.get("X-Filename", "video.mp4")),
                                        handler.headers.get("X-Request-Key"))
            finally:
                handler.connection.settimeout(old_timeout)
            handler._json(201, result)
            return True
        try:
            story_body = bool(re.fullmatch(r"/episodes/[a-f0-9]{32}/(?:source|entities|beats|script|storyboard)-(?:revisions|drafts)", relative))
            if (story_body or relative == "/mcp" or relative.endswith("/text-jobs")
                    or relative.startswith("/story-revisions/") or relative.startswith("/text-jobs/")
                    or relative.endswith("/script-review")):
                maximum = (16 if story_body else 1) * 1024 * 1024
                length = int(handler.headers.get("Content-Length", "-1"))
                if handler.headers.get_content_type() != "application/json" or not 2 <= length <= maximum:
                    raise ValueError("body length/type")
                def pairs(items):
                    value = {}
                    for key, item in items:
                        if key in value: raise ValueError("duplicate JSON field")
                        value[key] = item
                    return value
                raw = handler.rfile.read(length)
                if len(raw) != length: raise ValueError("incomplete body")
                body = json.loads(raw, object_pairs_hook=pairs,
                                  parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
                from .script_contracts import check_tree
                check_tree(body)
                if not isinstance(body, dict): raise ValueError("body root")
            else:
                body = handler._read_json(maximum=1024 * 1024)
        except (ValueError, OSError, RecursionError) as exc:
            raise DramaError("invalid_json", "请求必须是大小限制内的JSON对象") from exc
        if relative == "/mcp":
            from .business_mcp import rpc
            handler._json(200, rpc(service, body))
            return True
        routes = [
            (r"/projects/([a-f0-9]{32})/backups", lambda oid: service.create_backup(oid, body), 202),
            (r"/restores", lambda: service.create_restore(body), 202),
            (r"/restores/([a-f0-9]{32})/query-mode", lambda oid: service.restore_query_mode(oid, body), 200),
            (r"/episodes/([a-f0-9]{32})/source-revisions", lambda oid: service.save_source(oid, body), 201),
            (r"/episodes/([a-f0-9]{32})/(entities|beats|script|storyboard)-revisions", lambda oid, kind: service.save_story_revision(oid, kind, body), 201),
            (r"/episodes/([a-f0-9]{32})/(entities|beats|script|storyboard)-drafts", lambda oid, kind: service.save_story_draft(oid, kind, body), 201),
            (r"/episodes/([a-f0-9]{32})/text-jobs", lambda oid: service.start_text_job(oid, body), 202),
            (r"/story-revisions/([a-f0-9]{32})/approve", lambda oid: service.approve_story_revision(oid, body), 200),
            (r"/story-revisions/([a-f0-9]{32})/confirm-entity", lambda oid: service.confirm_entity_candidate(oid, body), 201),
            (r"/story-revisions/([a-f0-9]{32})/import", lambda oid: service.import_storyboard(oid, body), 201),
            (r"/shots/([a-f0-9]{32})/script-review", lambda oid: service.review_script_invalidations(oid, body), 200),
            (r"/projects/([a-f0-9]{32})/reference-audio/silence", lambda pid: service.default_reference_audio(pid, body), 200),
            (r"/text-jobs/([a-f0-9]{32})/cancel", lambda oid: service.cancel_text_job(oid), 200),
            (r"/text-jobs/([a-f0-9]{32})/reconcile", lambda oid: service.reconcile_text_job(oid), 200),
            (r"/timeline-revisions/([a-f0-9]{32})/subtitle-revisions", lambda oid: service.save_subtitles(oid, body), 201),
            (r"/subtitle-revisions/([a-f0-9]{32})/approve", lambda oid: service.approve_subtitles(oid, body), 200),
            (r"/timeline-revisions/([a-f0-9]{32})/transcriptions", lambda oid: service.create_transcription(oid, body), 202),
            (r"/exports/([a-f0-9]{32})/retry", lambda oid: service.retry_export(oid, body), 202),
            (r"/exports/([a-f0-9]{32})/approve", lambda oid: service.approve_export(oid, body), 200),
            (r"/projects/([a-f0-9]{32})/production-state", lambda oid: service.set_production_state(oid, body), 200),
            (r"/episodes/([a-f0-9]{32})/video-batches", lambda oid: service.submit_video_batch(oid, body), 202),
            (r"/video-jobs/([a-f0-9]{32})/refresh", lambda oid: service.refresh_video_job(oid), 200),
            (r"/video-jobs/([a-f0-9]{32})/cancel", lambda oid: service.cancel_video_job(oid), 200),
            (r"/video-jobs/([a-f0-9]{32})/release", lambda oid: service.release_video_job(oid), 200),
            (r"/video-jobs/([a-f0-9]{32})/continue", lambda oid: service.continue_video_job(oid, body), 202),
            (r"/takes/([a-f0-9]{32})/review", lambda oid: service.review_take(oid, body), 200),
            (r"/projects/([a-f0-9]{32})/entities", lambda oid: service.create_entity(oid, body), 201),
            (r"/entities/([a-f0-9]{32})/revisions", lambda oid: service.revise_entity(oid, body), 201),
            (r"/entities/([a-f0-9]{32})/approval", lambda oid: service.approve_entity(oid, body), 200),
            (r"/shots/([a-f0-9]{32})/revisions", lambda oid: service.revise_shot(oid, body), 201),
            (r"/shots/([a-f0-9]{32})/canvas-revisions", lambda oid: service.revise_shot(oid, body, canvas=True), 201),
            (r"/shots/([a-f0-9]{32})/reference-selection", lambda oid: service.select_reference(oid, body), 201),
            (r"/shots/([a-f0-9]{32})/action-reference-selection", lambda oid: service.select_slot_reference(oid, body, slot="action"), 201),
            (r"/shots/([a-f0-9]{32})/scene-reference-selection", lambda oid: service.select_slot_reference(oid, body, slot="scene"), 201),
            (r"/shots/([a-f0-9]{32})/blocking-reference-selection", lambda oid: service.select_slot_reference(oid, body, slot="blocking"), 201),
            (r"/shots/([a-f0-9]{32})/keyframe-grid-selection", lambda oid: service.select_slot_reference(oid, body, slot="keyframeGrid"), 201),
            (r"/shots/([a-f0-9]{32})/review", lambda oid: service.review_shot(oid, body), 200),
            (r"/projects/([a-f0-9]{32})/image-imports", lambda oid: service.import_image_source(oid, body), 201),
            (r"/entity-revisions/([a-f0-9]{32})/reference-jobs", lambda oid: service.submit_reference_job("entity", oid, body), 202),
            (r"/shot-revisions/([a-f0-9]{32})/reference-jobs", lambda oid: service.submit_reference_job("shot", oid, body), 202),
            (r"/reference-jobs/([a-f0-9]{32})/recover", lambda oid: service.recover_reference_artifacts(oid), 202),
            (r"/reference-jobs/([a-f0-9]{32})/retry", lambda oid: service.retry_reference_job(oid, body), 202),
            (r"/reference-jobs/([a-f0-9]{32})/refresh", lambda oid: service.refresh_reference_job(oid), 200),
            (r"/reference-jobs/([a-f0-9]{32})/cancel", lambda oid: service.cancel_reference_job(oid), 200),
            (r"/projects/([a-f0-9]{32})/reference-imports", lambda oid: service.import_reference_job(oid, body), 202),
            (r"/projects/([a-f0-9]{32})/image-reviews", lambda oid: service.review_image_candidates(oid, body), 200),
            (r"/projects", lambda: service.create_project(body), 201),
            (r"/projects/([a-f0-9]{32})/canvas", lambda oid: service.update_project_canvas(oid, body), 200),
            (r"/projects/([a-f0-9]{32})/episodes", lambda oid: service.create_episode(oid, body), 201),
            (r"/projects/([a-f0-9]{32})/imports", lambda oid: service.import_source(oid, body), 201),
            (r"/episodes/([a-f0-9]{32})/shots", lambda oid: service.create_shot(oid, body), 201),
            (r"/shots/([a-f0-9]{32})/selection", lambda oid: service.select_take(oid, body), 200),
            (r"/shots/([a-f0-9]{32})/deletion", lambda oid: service.delete_shot(oid, body), 200),
            (r"/episodes/([a-f0-9]{32})/shot-order", lambda oid: service.reorder(oid, body), 200),
            (r"/episodes/([a-f0-9]{32})/timeline-revisions", lambda oid: service.save_timeline(oid, body), 201),
            (r"/timeline-revisions/([a-f0-9]{32})/exports", lambda oid: service.create_export(oid, body), 202),
            (r"/projects/([a-f0-9]{32})/audio-imports", lambda oid: service.import_audio(oid, body), 201),
            (r"/projects/([a-f0-9]{32})/video-imports", lambda oid: service.import_video(oid, body), 201),
            (r"/episodes/([a-f0-9]{32})/song", lambda oid: service.bind_song(oid, body), 200),
        ]
        for pattern, operation, response_status in routes:
            match = re.fullmatch(pattern, relative)
            if match:
                handler._json(response_status, operation(*match.groups()))
                return True
        raise DramaError("not_found", "接口不存在", 404)
    except DramaError as exc:
        handler.close_connection = True
        error = {"code": exc.code, "message": exc.message}
        if hasattr(exc, "errors"):
            error["errors"] = exc.errors
        handler._json(exc.status, {"error": error, "requestId": request_id}, {"Connection": "close"})
    except (BrokenPipeError, ConnectionResetError):
        handler.close_connection = True
    except Exception as exc:
        handler.close_connection = True
        # No raw exception, file path, uploaded filename or credential in responses.
        print(f"drama-error request_id={request_id} kind={type(exc).__name__}")
        handler._json(503, {"error": {"code": "service_unavailable", "message": "服务暂不可用；请查询原任务后再操作"}, "requestId": request_id}, {"Connection": "close"})
    return True
