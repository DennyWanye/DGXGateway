"""Delete one shot without erasing the generation audit or other projects."""
from __future__ import annotations

import json
import time

from .contracts import DramaError, fields, identifier, revision
from .restore import restore_guard
from .store import encode, new_id
from .video import TERMINAL

TEXT_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED"}
UNSAFE_ITEM = {"QUEUED", "BLOCKED", "DISPATCHING", "RUNNING", "PREPARED", "COLLECTING",
               "OUTPUT_UNAVAILABLE", "SUBMISSION_UNKNOWN", "RECOVERY_REQUIRED"}
IDENTITY_KEYS = {"shotId", "shotRevisionId", "takeId", "shot_id", "source_revision_id",
                 "targetId", "targetRevisionId", "jobId"}
LIST_KEYS = {"shotIds", "shotRevisionIds", "affectedShotIds", "takeIds", "jobIds"}
RECORDED = {
    "shotId": "recordedShotId", "shotRevisionId": "recordedShotRevisionId", "takeId": "recordedTakeId",
    "shot_id": "recordedShotId", "source_revision_id": "recordedShotRevisionId",
    "targetId": "recordedTargetId", "targetRevisionId": "recordedTargetRevisionId", "jobId": "recordedJobId",
}
SKIP_CHILDREN = {"content", "snapshot", "rawText", "raw_text", "inputSnapshot", "parameters"}


def _mentions(value, gone, key=""):
    if key in SKIP_CHILDREN:
        return False
    if key in IDENTITY_KEYS and isinstance(value, str) and value in gone:
        return True
    if key in LIST_KEYS and isinstance(value, list) and any(isinstance(item, str) and item in gone for item in value):
        return True
    if isinstance(value, dict):
        if isinstance(value.get("id"), str) and value["id"] in gone:
            return True
        return any(_mentions(child, gone, name) for name, child in value.items())
    if isinstance(value, list):
        return any(_mentions(child, gone, key) for child in value)
    return False


def _rename_gone(value, gone):
    if isinstance(value, list):
        return [_rename_gone(item, gone) for item in value]
    if not isinstance(value, dict):
        return value
    renamed = {}
    for key, child in value.items():
        recorded = _rename_gone(child, gone)
        if key in RECORDED and isinstance(child, str) and child in gone:
            renamed[RECORDED[key]] = recorded
        else:
            renamed[key] = recorded
    return renamed


def _filter_lists(value, gone):
    if isinstance(value, list):
        return [_filter_lists(item, gone) for item in value]
    if not isinstance(value, dict):
        return value
    return {key: [item for item in child if item not in gone] if key in LIST_KEYS and isinstance(child, list)
            else _filter_lists(child, gone) for key, child in value.items()}


class ShotDeletionMixin:
    def delete_shot(self, sid, body):
        restore_guard(self, "write")
        fields(body, {"expectedRevision", "requestKey"}, {"expectedRevision", "requestKey"})
        # shot-deletion:{shot} is omitted once the shot is gone. Do not register
        # that prefix. The durable receipt is the project-scoped shot_deleted event.
        def save(db):
            shot = self.store.one(db, "dr_shots", identifier(sid))
            revision(shot["revision"], body["expectedRevision"])
            return self._delete_shot_rows(db, shot, body["requestKey"])
        return self.store.mutate(f"shot-deletion:{identifier(sid)}", body["requestKey"], body, save)

    def list_shot_audit(self, pid):
        with self.store.connect() as db:
            self.store.one(db, "dr_projects", identifier(pid))
            events = []
            for row in db.execute(
                    "SELECT id, kind, payload, created_at FROM dr_events WHERE scope=? AND kind IN ('video_generation','shot_deleted') ORDER BY created_at",
                    (f"project:{pid}",)):
                events.append({"id": row["id"], "kind": row["kind"], "payload": json.loads(row["payload"]), "createdAt": row["created_at"]})
            return {"events": events}

    def _delete_shot_rows(self, db, shot, request_key):
        sid = shot["id"]
        episode = self.store.one(db, "dr_episodes", shot["episode_id"])
        pid, eid = episode["project_id"], episode["id"]
        revisions = [row["id"] for row in db.execute("SELECT id FROM dr_shot_revisions WHERE shot_id=?", (sid,))]
        takes = [row["id"] for row in db.execute("SELECT id FROM dr_takes WHERE shot_id=?", (sid,))]
        items = [dict(row) for row in db.execute("SELECT * FROM dr_video_items WHERE shot_id=?", (sid,))]
        candidates = [row["id"] for row in db.execute(
            "SELECT id FROM dr_reference_candidates WHERE target_type='shot' AND target_id=?", (sid,))]
        reference_jobs = [dict(row) for row in db.execute(
            "SELECT * FROM dr_reference_jobs WHERE target_type='shot' AND target_id=?", (sid,))]
        gone = set(revisions) | set(takes) | {sid} | {row["id"] for row in items} | set(candidates) | {row["id"] for row in reference_jobs}
        self._refuse_busy_shot(db, eid, sid, revisions, items, reference_jobs)
        self._refuse_timeline_reference(db, eid, gone)
        prompt = self._current_prompt(db, sid)
        audited = self._backfill_generation_audit(db, pid, eid, items)
        self._scrub_surviving_references(db, pid, eid, sid, gone)
        self._remove_shot_rows(db, sid, revisions, takes, candidates, reference_jobs)
        self._write_shot_deleted(db, pid, shot, revisions, takes, audited, request_key, prompt)
        db.execute("UPDATE dr_episodes SET revision=revision+1 WHERE id=?", (eid,))
        episode_revision = db.execute("SELECT revision FROM dr_episodes WHERE id=?", (eid,)).fetchone()[0]
        self._refuse_remaining_mentions(db, pid, eid, gone)
        return {"deleted": True, "episodeId": eid, "episodeRevision": episode_revision,
                "recordedShotId": sid, "auditedGenerations": audited}

    def _refuse_busy_shot(self, db, eid, sid, revisions, items, reference_jobs):
        mentioned = {sid, *revisions}
        for item in items:
            job = self.store.one(db, "dr_video_jobs", item["job_id"])
            if (job["state"] not in TERMINAL or job["cleanup_required"] or not job["resource_settled"]
                    or item["state"] not in TERMINAL or item["state"] in UNSAFE_ITEM
                    or item["submission_phase"] == "UNKNOWN"):
                raise DramaError("shot_busy", "这个镜头还有未结束的生成任务，请等任务结束并释放后再删除", 409)
        for job in reference_jobs:
            if not self._reference_settled(job):
                raise DramaError("shot_busy", "这个镜头还有未结束的图片任务，请等任务结束并释放后再删除", 409)
        for row in db.execute("SELECT state, payload FROM dr_text_jobs WHERE episode_id=?", (eid,)):
            if row["state"] in TEXT_TERMINAL:
                continue
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                raise DramaError("shot_busy", "文本任务无法核对，请先结束该任务再删除", 409) from None
            if _mentions(payload, mentioned):
                raise DramaError("shot_busy", "这个镜头还有未结束的文本任务，请等任务结束后再删除", 409)

    @staticmethod
    def _reference_settled(row):
        if row["state"] not in TERMINAL:
            return False
        if not row["gateway_job_id"]:
            return True
        try:
            status = json.loads(row["input_snapshot"]).get("gatewayStatus") or {}
        except (TypeError, ValueError):
            return False
        return status.get("memoryReleased") is True and not status.get("cleanupRequired") and status.get("resourceSettled") is True

    def _refuse_timeline_reference(self, db, eid, gone):
        for row in db.execute("SELECT snapshot FROM dr_timeline_revisions WHERE episode_id=?", (eid,)):
            try:
                snapshot = json.loads(row["snapshot"])
            except (TypeError, ValueError):
                raise DramaError("timeline_ref", "时间线无法核对，请先从时间线移除这个镜头后再删除", 409) from None
            if _mentions(snapshot, gone):
                raise DramaError("timeline_ref", "时间线仍引用这个镜头或候选，请先从时间线移除后再删除", 409)

    def _backfill_generation_audit(self, db, pid, eid, items):
        recorded = set()
        for row in db.execute("SELECT payload FROM dr_events WHERE scope=? AND kind='video_generation'", (f"project:{pid}",)):
            payload = json.loads(row["payload"])
            if isinstance(payload.get("recordedItemId"), str):
                recorded.add(payload["recordedItemId"])
        count = 0
        for item in items:
            count += 1
            if item["id"] in recorded:
                continue
            try:
                snapshot = json.loads(item["input_snapshot"])
            except (TypeError, ValueError):
                raise DramaError("audit_incomplete", "生成记录无法核对，不能删除", 409) from None
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("shotId"), str):
                raise DramaError("audit_incomplete", "生成记录缺少镜头身份，不能删除", 409)
            actor = self._generation_actor(db, eid, item["job_id"])
            if actor is None:
                raise DramaError("audit_incomplete", "找不到这次生成的调用来源，不能删除", 409)
            self._record_video_generation(db, pid, eid, item["job_id"], item["id"], snapshot,
                                          actor=actor, backfilled=True, request_key=None)
        return count

    def _generation_actor(self, db, eid, job_id):
        for row in db.execute("SELECT request_key, response FROM dr_requests WHERE scope=?", (f"video-batches:{eid}",)):
            try:
                response = json.loads(row["response"])
            except (TypeError, ValueError):
                continue
            job = response.get("job") if isinstance(response, dict) else None
            if not isinstance(job, dict) or job.get("id") != job_id:
                continue
            for event in db.execute("SELECT payload FROM dr_events WHERE scope=? AND kind='mutation'", (f"video-batches:{eid}",)):
                payload = json.loads(event["payload"])
                if payload.get("requestKey") == row["request_key"] and payload.get("actor") in {"ui_api", "business_mcp"}:
                    return payload["actor"]
            return None
        return None

    def _scrub_surviving_references(self, db, pid, eid, sid, gone):
        owned = {pid, eid}
        for row in db.execute("SELECT scope, request_key, response FROM dr_requests"):
            parts = row["scope"].split(":")
            if len(parts) != 2 or parts[1] in gone or parts[1] == sid:
                continue
            if parts[1] not in owned and not self._scope_stays(db, parts[0], parts[1], pid, eid):
                continue
            try:
                response = json.loads(row["response"])
            except (TypeError, ValueError):
                raise DramaError("shot_in_use", "删除后仍有无法核对的归档引用，已取消删除", 409) from None
            scrubbed = self._scrub_response(parts[0], response, sid, gone)
            if _mentions(scrubbed, gone):
                raise DramaError("shot_in_use", "删除后仍有归档引用这个镜头，已取消删除", 409)
            if scrubbed != response:
                db.execute("UPDATE dr_requests SET response=? WHERE scope=? AND request_key=?",
                           (encode(scrubbed), row["scope"], row["request_key"]))
        for row in db.execute("SELECT id, scope, payload FROM dr_events WHERE scope=?", (f"project:{pid}",)):
            payload = json.loads(row["payload"])
            scrubbed = _rename_gone(_filter_lists(payload, gone), gone)
            if _mentions(scrubbed, gone):
                raise DramaError("shot_in_use", "项目审计仍引用这个镜头，已取消删除", 409)
            if scrubbed != payload:
                db.execute("UPDATE dr_events SET payload=? WHERE id=?", (encode(scrubbed), row["id"]))
        for row in db.execute("SELECT id, payload FROM dr_text_jobs WHERE episode_id=?", (eid,)):
            payload = json.loads(row["payload"])
            scrubbed = _rename_gone(payload, gone)
            if scrubbed != payload:
                db.execute("UPDATE dr_text_jobs SET payload=? WHERE id=?", (encode(scrubbed), row["id"]))

    @staticmethod
    def _scope_stays(db, prefix, oid, pid, eid):
        if prefix in {"reference-submit", "reference-import", "image-reviews", "review-suggestion", "entities", "production-state"}:
            return oid == pid
        if prefix in {"shots", "order", "video-batches", "text-jobs", "timeline", "song", "subtitles"}:
            return oid == eid
        if prefix in {"story-approve", "story-confirm", "story-import"}:
            row = db.execute("SELECT episode_id FROM dr_story_revisions WHERE id=?", (oid,)).fetchone()
            return row is not None and row["episode_id"] == eid
        if prefix == "entity-approval":
            row = db.execute("SELECT project_id FROM dr_entities WHERE id=?", (oid,)).fetchone()
            return row is not None and row["project_id"] == pid
        return False

    @staticmethod
    def _scrub_response(prefix, response, sid, gone):
        if not isinstance(response, dict):
            return response
        if prefix == "shots":
            shot = response.get("shot")
            if isinstance(shot, dict) and (shot.get("id") == sid or shot.get("id") in gone):
                return {"shot": {"deleted": True}}
            return response
        if prefix == "video-batches":
            job = response.get("job")
            if not isinstance(job, dict) or not isinstance(job.get("items"), list):
                return response
            kept = [item for item in job["items"]
                    if not (isinstance(item, dict) and (item.get("id") in gone or item.get("shotId") == sid))]
            return {**response, "job": {**job, "items": kept}}
        if prefix == "text-jobs":
            job = response.get("job")
            if not isinstance(job, dict):
                return response
            nested = dict(job)
            for field in ("inputRevisions", "promptInput"):
                if field in nested:
                    nested[field] = _rename_gone(nested[field], gone)
            return {**response, "job": nested}
        if prefix == "image-reviews" and isinstance(response.get("reviews"), list):
            kept = [item for item in response["reviews"] if not (isinstance(item, dict) and item.get("id") in gone)]
            return {**response, "reviews": kept}
        if prefix == "story-import":
            scrubbed = dict(response)
            for field in ("shots", "mappings"):
                if isinstance(scrubbed.get(field), list):
                    scrubbed[field] = [item for item in scrubbed[field]
                                       if not (isinstance(item, dict) and (item.get("id") in gone or item.get("shotId") in gone or item.get("id") == sid))]
            return scrubbed
        if prefix == "review-suggestion":
            return _rename_gone(response, gone)
        if prefix in {"reference-submit", "reference-import"} and response.get("jobId") in gone:
            return {"jobId": None}
        return _filter_lists(response, gone)

    def _remove_shot_rows(self, db, sid, revisions, takes, candidates, reference_jobs):
        # Request rows stay. Scrubbing already nulls a deleted reference jobId so
        # the same requestKey cannot resubmit, and a scope LIKE delete would cross projects.
        db.execute("DELETE FROM dr_video_items WHERE shot_id=?", (sid,))
        if takes:
            db.executemany("DELETE FROM dr_take_reviews WHERE take_id=?", [(tid,) for tid in takes])
        if candidates:
            db.executemany("DELETE FROM dr_reference_candidate_reviews WHERE candidate_id=?", [(cid,) for cid in candidates])
            db.execute("DELETE FROM dr_reference_candidates WHERE target_type='shot' AND target_id=?", (sid,))
        if reference_jobs:
            db.execute("DELETE FROM dr_reference_jobs WHERE target_type='shot' AND target_id=?", (sid,))
        if revisions:
            db.execute("DELETE FROM dr_dependencies WHERE shot_revision_id IN (SELECT id FROM dr_shot_revisions WHERE shot_id=?)", (sid,))
            db.execute("DELETE FROM dr_invalidations WHERE shot_revision_id IN (SELECT id FROM dr_shot_revisions WHERE shot_id=?)", (sid,))
            db.execute("DELETE FROM dr_script_dependencies WHERE shot_revision_id IN (SELECT id FROM dr_shot_revisions WHERE shot_id=?)", (sid,))
            db.execute("DELETE FROM dr_script_invalidations WHERE shot_revision_id IN (SELECT id FROM dr_shot_revisions WHERE shot_id=?)", (sid,))
        db.execute("DELETE FROM dr_storyboard_mappings WHERE shot_id=?", (sid,))
        db.execute("DELETE FROM dr_production_approvals WHERE target_type='shot' AND target_id=?", (sid,))
        db.execute("UPDATE dr_shots SET selected_take_id=NULL, current_content_revision_id=NULL WHERE id=?", (sid,))
        db.execute("DELETE FROM dr_takes WHERE shot_id=?", (sid,))
        db.execute("DELETE FROM dr_shot_revisions WHERE shot_id=?", (sid,))
        db.execute("DELETE FROM dr_shots WHERE id=?", (sid,))

    def _current_prompt(self, db, sid):
        current = db.execute(
            "SELECT snapshot FROM dr_shot_revisions WHERE shot_id=? ORDER BY revision DESC LIMIT 1", (sid,)).fetchone()
        if not current:
            return ""
        try:
            raw = json.loads(current["snapshot"]).get("prompt")
        except (TypeError, ValueError):
            return ""
        return raw if isinstance(raw, str) else ""

    def _write_shot_deleted(self, db, pid, shot, revisions, takes, audited, request_key, prompt):
        payload = {
            "actor": self._call_actor(),
            "requestKey": request_key,
            "recordedShotId": shot["id"],
            "title": shot["title"],
            "ordinal": shot["ordinal"],
            "episodeId": shot["episode_id"],
            "recordedRevisionIds": revisions,
            "recordedTakeIds": takes,
            "generationCount": audited,
        }
        if prompt:
            payload["prompt"] = prompt
        db.execute("INSERT INTO dr_events VALUES (?,?,?,?,?)",
                   (new_id(), f"project:{pid}", "shot_deleted", encode(payload), time.time()))

    def _refuse_remaining_mentions(self, db, pid, eid, gone):
        scopes = []
        for row in db.execute("SELECT scope, response FROM dr_requests"):
            parts = row["scope"].split(":")
            if len(parts) == 2 and parts[1] not in gone and (parts[1] in {pid, eid} or self._scope_stays(db, parts[0], parts[1], pid, eid)):
                scopes.append(json.loads(row["response"]))
        for row in db.execute("SELECT payload FROM dr_events WHERE scope=?", (f"project:{pid}",)):
            scopes.append(json.loads(row["payload"]))
        for row in db.execute("SELECT payload FROM dr_text_jobs WHERE episode_id=?", (eid,)):
            scopes.append(json.loads(row["payload"]))
        for row in db.execute("SELECT snapshot FROM dr_timeline_revisions WHERE episode_id=?", (eid,)):
            scopes.append(json.loads(row["snapshot"]))
        for value in scopes:
            if _mentions(value, gone):
                raise DramaError("shot_in_use", "删除后仍有归档引用这个镜头，已取消删除", 409)
