"""Atomic project organization; never dispatches, cancels or clones generation."""
from __future__ import annotations

import json
import time

from .contracts import DramaError, fields, identifier, revision
from .store import encode, new_id


def _project_refs(value, source, target):
    """Rehome typed ownership metadata, never authored text or model inputs."""
    if isinstance(value, list):
        return [_project_refs(item, source, target) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in {"projectId", "project_id"} and item == source:
            result[key] = target
        elif key in {"prompt", "content", "description", "parameters", "rawText", "raw_text"}:
            result[key] = item
        else:
            result[key] = _project_refs(item, source, target)
    return result


class ProjectMergeMixin:
    def merge_project(self, target, body):
        from .restore import restore_guard
        restore_guard(self, "write")
        required = {"sourceProjectId", "sourceExpectedRevision", "expectedRevision", "episodeIds", "requestKey"}
        fields(body, required, required)
        target, source = identifier(target), identifier(body["sourceProjectId"])
        if target == source:
            raise DramaError("same_project", "来源和目标项目不能相同")
        episodes = body["episodeIds"]
        if (not isinstance(episodes, list) or not episodes or len(episodes) > 2000
                or any(not isinstance(eid, str) for eid in episodes) or len(set(episodes)) != len(episodes)):
            raise DramaError("invalid_episodes", "须列出来源项目全部分集，且不能重复")
        for eid in episodes:
            identifier(eid)

        def save(db):
            src = self.store.one(db, "dr_projects", source)
            dst = self.store.one(db, "dr_projects", target)
            revision(src["revision"], body["sourceExpectedRevision"])
            revision(dst["revision"], body["expectedRevision"])
            if any(src[key] != dst[key] for key in ("width", "height", "fps", "kind")):
                raise DramaError("project_format_mismatch", "归并项目的类型、画幅和帧率必须相同", 409)
            actual = {r[0] for r in db.execute("SELECT id FROM dr_episodes WHERE project_id=?", (source,))}
            if actual != set(episodes):
                raise DramaError("episode_set_changed", "来源分集清单已变化，须重新核对全部分集", 409)
            if self._production_state(db, source)["paused"] != self._production_state(db, target)["paused"]:
                raise DramaError("pause_mismatch", "来源与目标的暂停状态不同，不能隐式改变生成许可", 409)
            # Text/export workers do not share the image/video ownership locks.
            if db.execute("SELECT 1 FROM dr_text_jobs WHERE episode_id IN (SELECT id FROM dr_episodes WHERE project_id=?) AND state NOT IN ('SUCCEEDED','FAILED','CANCELED','INTERRUPTED') LIMIT 1", (source,)).fetchone():
                raise DramaError("project_busy", "来源还有文本任务，请结束后再归并", 409)
            if db.execute("SELECT 1 FROM dr_exports WHERE project_id=? AND state IN ('QUEUED','RUNNING') LIMIT 1", (source,)).fetchone():
                raise DramaError("project_busy", "来源还有导出任务，请结束后再归并", 409)
            if db.execute("SELECT 1 FROM dr_transcriptions WHERE timeline_id IN (SELECT t.id FROM dr_timeline_revisions t JOIN dr_episodes e ON e.id=t.episode_id WHERE e.project_id=?) AND state IN ('QUEUED','RUNNING') LIMIT 1", (source,)).fetchone():
                raise DramaError("project_busy", "来源还有转写任务，请结束后再归并", 409)

            # Enumerate ownership before moving. IDs, versions, approvals, takes,
            # frozen prompts, Gateway IDs and dispatch keys all remain unchanged.
            owned = {source, *actual}
            queries = [
                "SELECT id FROM dr_entities WHERE project_id=?",
                "SELECT s.id FROM dr_shots s JOIN dr_episodes e ON e.id=s.episode_id WHERE e.project_id=?",
                "SELECT t.id FROM dr_takes t JOIN dr_shots s ON s.id=t.shot_id JOIN dr_episodes e ON e.id=s.episode_id WHERE e.project_id=?",
                "SELECT id FROM dr_timeline_revisions WHERE episode_id IN (SELECT id FROM dr_episodes WHERE project_id=?)",
                "SELECT id FROM dr_story_revisions WHERE episode_id IN (SELECT id FROM dr_episodes WHERE project_id=?)",
                "SELECT id FROM dr_exports WHERE project_id=?",
                "SELECT id FROM dr_subtitle_revisions WHERE episode_id IN (SELECT id FROM dr_episodes WHERE project_id=?)",
                "SELECT id FROM dr_text_jobs WHERE episode_id IN (SELECT id FROM dr_episodes WHERE project_id=?)",
                "SELECT id FROM dr_transcriptions WHERE timeline_id IN (SELECT t.id FROM dr_timeline_revisions t JOIN dr_episodes e ON e.id=t.episode_id WHERE e.project_id=?)",
            ]
            for query in queries:
                owned.update(r[0] for r in db.execute(query, (source,)))
            requests = []
            for row in db.execute("SELECT * FROM dr_requests"):
                parts = row["scope"].split(":")
                if len(parts) != 2 or parts[1] not in owned:
                    continue
                scope = parts[0] + ":" + target if parts[1] == source else row["scope"]
                if scope != row["scope"] and db.execute("SELECT 1 FROM dr_requests WHERE scope=? AND request_key=?", (scope, row["request_key"])).fetchone():
                    raise DramaError("merge_key_conflict", "目标存在同名历史请求，不能覆盖归并", 409)
                requests.append((row, scope))
            db.execute("INSERT OR IGNORE INTO dr_asset_bindings SELECT ?,asset_id FROM dr_asset_bindings WHERE project_id=?", (target, source))
            counts = {}
            for table in ("dr_episodes", "dr_entities", "dr_exports", "dr_reference_jobs", "dr_reference_candidates", "dr_reference_candidate_reviews", "dr_video_jobs"):
                counts[table] = db.execute(f"UPDATE {table} SET project_id=? WHERE project_id=?", (target, source)).rowcount
            for row in db.execute("SELECT id,input_snapshot FROM dr_reference_jobs WHERE project_id=?", (target,)).fetchall():
                old = json.loads(row["input_snapshot"])
                updated = _project_refs(old, source, target)
                if updated != old:
                    db.execute("UPDATE dr_reference_jobs SET input_snapshot=? WHERE id=?", (encode(updated), row["id"]))
            for row, scope in requests:
                response = encode(_project_refs(json.loads(row["response"]), source, target))
                db.execute("UPDATE dr_requests SET scope=?,response=? WHERE scope=? AND request_key=?", (scope, response, row["scope"], row["request_key"]))
            for row in db.execute("SELECT * FROM dr_events").fetchall():
                parts = row["scope"].split(":")
                if len(parts) == 2 and parts[1] in owned:
                    scope = parts[0] + ":" + target if parts[1] == source else row["scope"]
                    payload = encode(_project_refs(json.loads(row["payload"]), source, target))
                    db.execute("UPDATE dr_events SET scope=?,payload=? WHERE id=?", (scope, payload, row["id"]))
            db.execute("UPDATE dr_projects SET revision=revision+1 WHERE id IN (?,?)", (source, target))
            result = {"projectId": target, "recordedSourceProjectId": source, "episodeIds": sorted(actual),
                      "revision": dst["revision"] + 1, "counts": counts, "sourceRetained": True}
            from .business_mcp import CALL_SOURCE
            db.execute("INSERT INTO dr_events VALUES (?,?,?,?,?)", (new_id(), f"project:{target}", "project_merged", encode({**result, "actor": CALL_SOURCE.get()}), time.time()))
            return result

        # Same ordering as dispatch; polling holds the corresponding inner lock.
        # No collector can bind an output to a cached source project mid-move.
        with self._generation_lock, self._video_lock, self._reference_lock:
            return self.store.mutate(f"project-merge:{target}", body["requestKey"], body, save)
