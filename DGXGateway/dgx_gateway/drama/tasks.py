"""Read-only task catalogue and durable FIFO admission for image/video work.

The business lock only orders saved intents. Gateway remains the sole resource
authority; only an explicit pre-allocation busy rejection permits another POST.
"""
from __future__ import annotations

import json

from .contracts import identifier


TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED", "INTERRUPTED", "BLOCKED"}


class TaskMixin:
    def generation_queue(self, db):
        return sorted(
            [(r["created_at"], "image", r["id"]) for r in db.execute(
                "SELECT j.* FROM dr_reference_jobs j LEFT JOIN dr_production_state p ON p.project_id=j.project_id "
                "WHERE j.state='PREPARED' AND COALESCE(p.paused,0)=0")]
            + [(r["created_at"], "video", r["id"]) for r in db.execute(
                "SELECT j.* FROM dr_video_jobs j LEFT JOIN dr_production_state p ON p.project_id=j.project_id "
                "WHERE j.state='QUEUED' AND COALESCE(p.paused,0)=0")])

    def generation_turn(self, db, kind, jid):
        for r in db.execute("SELECT * FROM dr_reference_jobs WHERE state!='PREPARED'"):
            status = json.loads(r["input_snapshot"]).get("gatewayStatus", {})
            if (r["state"] not in TERMINAL or (status.get("memoryReleased") is False and status.get("resourceSettled") is not True)
                    or status.get("cleanupRequired") is True):
                return False
        if db.execute("SELECT 1 FROM dr_video_jobs WHERE state NOT IN "
                      "('QUEUED','BLOCKED','SUCCEEDED','FAILED','CANCELED','INTERRUPTED') "
                      "OR (gateway_job_id IS NOT NULL AND memory_released=0 AND resource_settled=0) OR cleanup_required=1 LIMIT 1").fetchone():
            return False
        if db.execute("SELECT 1 FROM dr_text_jobs WHERE resource_status NOT IN ('not_acquired','released') LIMIT 1").fetchone():
            return False
        queue = self.generation_queue(db)
        return bool(queue and queue[0][1:] == (kind, jid))

    def task_index(self, pid=None):
        """No reconciling, cancellation or model calls are performed by this GET."""
        with self.store.connect() as db:
            if pid is not None:
                self.store.one(db, "dr_projects", identifier(pid))
            projects = {r["id"]: r["title"] for r in db.execute("SELECT id,title FROM dr_projects")}
            episodes = {r["id"]: dict(r) for r in db.execute("SELECT id,project_id,title FROM dr_episodes")}
            queue = {(kind, jid): i + 1 for i, (_, kind, jid) in enumerate(self.generation_queue(db))}
            paused = {r[0] for r in db.execute("SELECT project_id FROM dr_production_state WHERE paused=1")}
            tasks = []

            def add(row, kind, project, episode=None, **details):
                if pid is not None and project != pid:
                    return
                state = row["state"]
                stage = "WAITING" if (kind, row["id"]) in queue else "PAUSED" if project in paused and state in {"PREPARED", "QUEUED"} else state
                tasks.append(dict(id=row["id"], kind=kind, projectId=project,
                                  projectTitle=projects[project], episodeId=episode,
                                  state=state, stage=stage, error=row["error"],
                                  createdAt=row["created_at"], updatedAt=row["updated_at"],
                                  queuePosition=queue.get((kind, row["id"])), **details))

            for r in db.execute("SELECT * FROM dr_reference_jobs"):
                status = json.loads(r["input_snapshot"]).get("gatewayStatus", {})
                table = "dr_shots" if r["target_type"] == "shot" else "dr_entities"
                target = self.store.one(db, table, r["target_id"])
                cancel = r["state"] not in TERMINAL | {"CANCEL_REQUESTED"} and (r["state"] == "PREPARED" or bool(r["gateway_job_id"]) and r["tool"] != "compose_image_layers")
                add(r, "image", r["project_id"], target["episode_id"] if r["target_type"] == "shot" else None,
                    targetId=r["target_id"], targetType=r["target_type"], targetRevisionId=r["target_revision_id"],
                    targetName=target["title"] if r["target_type"] == "shot" else json.loads(self.store.one(db, "dr_entity_revisions", r["target_revision_id"])["snapshot"]).get("name", "参考资料"), gatewayJobId=r["gateway_job_id"],
                    resourceSettled=status.get("resourceSettled", False), resourceStatus=status.get("resourceStatus", "unsettled"), memoryReleased=status.get("memoryReleased"), cleanupRequired=status.get("cleanupRequired", False),
                    resultCount=db.execute("SELECT COUNT(*) FROM dr_reference_candidates WHERE job_id=?", (r["id"],)).fetchone()[0],
                    totalCount=json.loads(r['parameters']).get('batch_count', 1),
                    cancelPath=f'/reference-jobs/{r["id"]}/cancel' if cancel else None,
                    refreshPath=f'/reference-jobs/{r["id"]}/refresh', detailPath=f'/reference-jobs/{r["id"]}')
            for r in db.execute("SELECT * FROM dr_video_jobs"):
                first = db.execute("SELECT shot_id FROM dr_video_items WHERE job_id=? ORDER BY ordinal LIMIT 1", (r["id"],)).fetchone()
                add(r, "video", r["project_id"], r["episode_id"], targetType="shot", targetId=first[0] if first else None,
                    targetName=episodes[r["episode_id"]]["title"], gatewayJobId=r["gateway_job_id"],
                    resourceSettled=bool(r["resource_settled"]), resourceStatus=r["resource_status"], memoryReleased=bool(r["memory_released"]), cleanupRequired=bool(r["cleanup_required"]),
                    resultCount=db.execute("SELECT COUNT(*) FROM dr_video_items WHERE job_id=? AND take_id IS NOT NULL", (r["id"],)).fetchone()[0],
                    totalCount=db.execute("SELECT COUNT(*) FROM dr_video_items WHERE job_id=?", (r["id"],)).fetchone()[0],
                    cancelPath=f'/video-jobs/{r["id"]}/cancel' if r["state"] not in TERMINAL | {"CANCEL_REQUESTED"} else None,
                    refreshPath=f'/video-jobs/{r["id"]}/refresh', detailPath=f'/video-jobs/{r["id"]}')
            for r in db.execute("SELECT * FROM dr_text_jobs"):
                ep = episodes[r["episode_id"]]
                payload = json.loads(r["payload"])
                revision = db.execute("SELECT r.shot_id,s.title,s.ordinal FROM dr_shot_revisions r JOIN dr_shots s ON s.id=r.shot_id WHERE r.id=?", (payload.get("shotRevisionId"),)).fetchone() if r["kind"] == "draft_video_prompt" else None
                add(r, "text", ep["project_id"], r["episode_id"], taskKind=r["kind"], targetId=revision[0] if revision else None, targetType="shot" if revision else None, targetName=(f"视频提示词 · S{revision[2]:02d} · {revision[1]}" if revision else ep["title"]),
                    gatewayJobId=r["gateway_id"], resourceStatus=r["resource_status"], memoryReleased=r["resource_status"] == "released",
                    resultCount=int(r["output_status"] == "valid_draft"),
                    cancelPath=f'/text-jobs/{r["id"]}/cancel' if r["state"] not in TERMINAL | {"SUBMISSION_UNKNOWN", "RECOVERY_REQUIRED"} else None,
                    refreshPath=f'/text-jobs/{r["id"]}/reconcile', detailPath=f'/text-jobs/{r["id"]}')
            for r in db.execute("SELECT * FROM dr_exports"):
                add(r, "export", r["project_id"], r["episode_id"], targetName=episodes[r["episode_id"]]["title"],
                    resultCount=int(r["asset_id"] is not None), cancelPath=None, detailPath=f'/exports/{r["id"]}')
            for r in db.execute("SELECT x.*, t.episode_id FROM dr_transcriptions x JOIN dr_timeline_revisions t ON t.id=x.timeline_id"):
                ep = episodes[r["episode_id"]]
                add(r, "subtitle", ep["project_id"], r["episode_id"], targetName=ep["title"],
                    resultCount=int(r["subtitle_id"] is not None), cancelPath=None, detailPath=f'/transcriptions/{r["id"]}')
            for task in tasks:
                if task.get("queuePosition"):
                    task["waitReason"] = "任务已保存，等待前序制作完成并交接资源；不会抢占现有任务。"
            if getattr(self, "_restore_mode", None):
                for task in tasks:
                    task["cancelPath"] = None
                    # Restore queries must not silently acquire original ownership.
                    if self._restore_mode != "query_only":
                        task["refreshPath"] = None
            return {"tasks": sorted(tasks, key=lambda t: (t["createdAt"], t["id"]), reverse=True)}
