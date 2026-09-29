"""Immutable entity/shot content and explicit production decisions."""
from __future__ import annotations

from contextlib import nullcontext
import json
import time

from .contracts import DramaError, fields, identifier, integer, revision, text
from .store import encode, new_id
from ..video_contracts import normalize_generation


ROLES = {"character": ("identity_front", "identity_3q", "identity_back", "costume_fullbody", "identity_sheet"),
         "location": ("location_wide", "location_detail", "layout"),
         "costume": ("costume_fullbody",), "prop": ("prop_detail",)}


def confirming_reference(kind, references):
    """A saved reference is enough to confirm. Front identity stays preferred when it exists."""
    refs = list(references or [])
    for role in (ROLES[kind][0], "identity_sheet"):
        found = next((item for item in refs if item.get("role") == role), None)
        if found:
            return found
    return refs[0] if refs else None
DEPENDENCIES = {"characterRevisionIds": "character", "locationRevisionId": "location",
                "costumeRevisionIds": "costume", "propRevisionIds": "prop"}
TEMPLATE = "dgx-drama-shot-v1"
SHOT_FIELDS = {"title", "action", "framing", "camera", "prompt", "characterRevisionIds", "locationRevisionId",
               "costumeRevisionIds", "propRevisionIds", "approvedReferenceAssetId", "actionReferenceAssetId",
               "actionReferencePrompt", "sceneReferenceAssetId", "sceneReferencePrompt",
               "blockingReferenceAssetId", "blockingReferencePrompt", "keyframeGridAssetId", "keyframeGridPrompt",
               "videoReferenceAssetId", "audioReferenceAssetId", "audioDescription",
               "firstFrameAssetId", "lastFrameAssetId",
               "referenceRole", "targetEditFrames", "generation", "referenceNodes", "customReferenceRoles"}
SHOT_IMAGE_SLOTS = {
    "action": ("actionReferenceAssetId", "shot-action-reference", "动作"),
    "scene": ("sceneReferenceAssetId", "shot-scene-reference", "场景"),
    "blocking": ("blockingReferenceAssetId", "shot-blocking-reference", "站位"),
    "keyframeGrid": ("keyframeGridAssetId", "shot-keyframe-grid-reference", "九宫格"),
}
SHOT_IMAGE_ASSET_KEYS = tuple(asset for asset, _mutate, _label in SHOT_IMAGE_SLOTS.values())


class ProductionMixin:
    def _image_asset(self, db, pid, aid):
        asset = self._bound_asset(db, pid, aid)
        if json.loads(asset["metadata"]).get("kind") != "image":
            raise DramaError("image_required", "参考素材必须为经过完整检查的图片")
        return asset

    def _entity_snapshot(self, db, pid, kind, body):
        name = text(body.get("name"), "名称", 160)
        description = body.get("description", "")
        if not isinstance(description, str) or len(description) > 8000:
            raise DramaError("invalid_description", "描述须为8000字以内文本")
        references = body.get("references", [])
        if not isinstance(references, list) or len(references) > len(ROLES[kind]):
            raise DramaError("invalid_references", "参考图片用途或数量不正确")
        refs = []
        seen = set()
        for ref in references:
            fields(ref, {"assetId", "role"}, {"assetId", "role"})
            role = ref["role"]
            if not isinstance(role, str) or role not in ROLES[kind] or role in seen:
                raise DramaError("invalid_reference_role", "参考用途与实体类型不符，或用途重复")
            self._image_asset(db, pid, ref["assetId"])
            refs.append({"assetId": ref["assetId"], "role": role})
            seen.add(role)
        primary = body.get("referenceAssetId")
        role = body.get("referenceRole", ROLES[kind][0])
        if role not in ROLES[kind]:
            raise DramaError("invalid_reference_role", "参考用途与实体类型不符")
        if primary:
            if role != ROLES[kind][0]:
                raise DramaError("invalid_primary_role", "主参考必须使用此实体的主用途；其他视角放入参考列表")
            self._image_asset(db, pid, primary)
            old = next((r for r in refs if r["role"] == role), None)
            if old and old["assetId"] != primary:
                raise DramaError("reference_conflict", "主参考与对应用途图片不一致")
            if not old:
                refs.append({"assetId": primary, "role": role})
        elif refs:
            main = next((r for r in refs if r["role"] == ROLES[kind][0]), refs[0])
            primary, role = main["assetId"], main["role"]
        return {"name": name, "description": description, "referenceAssetId": primary or None,
                "referenceRole": role, "references": refs}

    def _entity_view(self, db, entity):
        revisions = []
        for r in db.execute("SELECT * FROM dr_entity_revisions WHERE entity_id=? ORDER BY revision DESC", (entity["id"],)):
            revisions.append({"id": r["id"], "revision": r["revision"], **json.loads(r["snapshot"]),
                              "approved": bool(db.execute("SELECT 1 FROM dr_production_approvals WHERE revision_id=? AND target_type='entity'", (r["id"],)).fetchone())})
        return {"id": entity["id"], "projectId": entity["project_id"], "kind": entity["kind"], "revision": entity["revision"],
                "currentRevisionId": entity["current_revision_id"], "approvedRevisionId": entity["approved_revision_id"],
                "current": next((r for r in revisions if r["id"] == entity["current_revision_id"]), None), "revisions": revisions}

    def entities(self, pid):
        with self.store.connect() as db:
            self.store.one(db, "dr_projects", identifier(pid))
            return {"entities": [self._entity_view(db, e) for e in db.execute("SELECT * FROM dr_entities WHERE project_id=? ORDER BY created_at", (pid,))]}

    def entity(self, eid):
        with self.store.connect() as db:
            return {"entity": self._entity_view(db, self.store.one(db, "dr_entities", identifier(eid)))}

    def create_entity(self, pid, body):
        fields(body, {"kind", "name", "description", "referenceAssetId", "referenceRole", "references", "requestKey"}, {"kind", "name", "requestKey"})
        kind = body["kind"]
        if not isinstance(kind, str) or kind not in ROLES:
            raise DramaError("invalid_kind", "实体类型须为人物、场景、服装或道具")
        def save(db):
            self.store.one(db, "dr_projects", identifier(pid))
            snapshot = self._entity_snapshot(db, pid, kind, body)
            eid = new_id()
            db.execute("INSERT INTO dr_entities(id,project_id,kind,created_at) VALUES(?,?,?,?)", (eid, pid, kind, time.time()))
            self._append_entity(db, eid, snapshot)
            return {"entity": self._entity_view(db, self.store.one(db, "dr_entities", eid))}
        return self.store.mutate(f"entities:{pid}", body["requestKey"], body, save)

    def _append_entity(self, db, eid, snapshot):
        rid = new_id()
        version = db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM dr_entity_revisions WHERE entity_id=?", (eid,)).fetchone()[0]
        db.execute("INSERT INTO dr_entity_revisions VALUES(?,?,?,?,?)", (rid, eid, version, encode(snapshot), time.time()))
        for ref in snapshot["references"]:
            db.execute("INSERT INTO dr_entity_reference_bindings VALUES(?,?,?)", (rid, ref["assetId"], ref["role"]))
        db.execute("UPDATE dr_entities SET current_revision_id=? WHERE id=?", (rid, eid))
        return rid

    def revise_entity(self, eid, body):
        fields(body, {"expectedRevision", "name", "description", "referenceAssetId", "referenceRole", "references", "candidateId", "acceptStaleCandidate", "requestKey"}, {"expectedRevision", "name", "requestKey"})
        def save(db):
            entity = self.store.one(db, "dr_entities", identifier(eid))
            revision(entity["revision"], body["expectedRevision"])
            if "acceptStaleCandidate" in body and type(body["acceptStaleCandidate"]) is not bool:
                raise DramaError("invalid_confirmation", "旧候选确认必须为布尔值")
            candidate_role = None
            if body.get("candidateId"):
                candidate = self.store.one(db, "dr_reference_candidates", identifier(body["candidateId"]))
                if candidate["target_type"] != "entity" or candidate["target_id"] != eid or candidate["asset_id"] != body.get("referenceAssetId"):
                    raise DramaError("wrong_candidate", "图片候选不属于此实体")
                if candidate["target_revision_id"] != entity["current_revision_id"] and body.get("acceptStaleCandidate") is not True:
                    raise DramaError("stale_candidate", "候选来自旧实体版本，需要明确确认", 409)
                # The generated candidate belongs to an immutable source revision.
                # Its slot is authoritative: a later edit must not redirect an old
                # candidate into whatever role happens to be selected now.
                source = self.store.one(db, "dr_entity_revisions", candidate["target_revision_id"])
                source_snapshot = json.loads(source["snapshot"])
                job = db.execute("SELECT input_snapshot FROM dr_reference_jobs WHERE id=?", (candidate["job_id"],)).fetchone()
                job_role = json.loads(job["input_snapshot"]).get("referenceRole") if job else None
                candidate_role = job_role or source_snapshot.get("referenceRole", ROLES[entity["kind"]][0])
                if candidate_role not in ROLES[entity["kind"]]:
                    raise DramaError("invalid_reference_role", "候选来源版本的参考用途不合法")
                self._image_asset(db, entity["project_id"], candidate["asset_id"])
                current_snapshot = json.loads(self.store.one(db, "dr_entity_revisions", entity["current_revision_id"])["snapshot"])
                current_refs = current_snapshot.get("references", [])
                if any(ref.get("role") == candidate_role and ref.get("assetId") == candidate["asset_id"] for ref in current_refs):
                    # A fresh request key must not manufacture another identical
                    # revision. Candidate, ownership, staleness and image checks
                    # above still run before this idempotent return.
                    return {"entity": self._entity_view(db, entity)}
                refs = [ref for ref in current_refs if ref.get("role") != candidate_role]
                refs.append({"role": candidate_role, "assetId": candidate["asset_id"]})
                primary_role = ROLES[entity["kind"]][0]
                primary = next((ref["assetId"] for ref in refs if ref["role"] == primary_role), None)
                # Selection is intentionally a narrow slot replacement. Preserve
                # all other current references and do not let stale form fields
                # overwrite them.
                payload = {"name": current_snapshot.get("name", ""),
                           "description": current_snapshot.get("description", ""),
                           "references": refs, "referenceRole": primary_role,
                           "referenceAssetId": primary}
            else:
                payload = body
            snapshot = self._entity_snapshot(db, entity["project_id"], entity["kind"], payload)
            self._append_entity(db, eid, snapshot)
            db.execute("UPDATE dr_entities SET revision=revision+1 WHERE id=?", (eid,))
            return {"entity": self._entity_view(db, self.store.one(db, "dr_entities", eid))}
        return self.store.mutate(f"entity-revisions:{eid}", body["requestKey"], body, save)

    def approve_entity(self, eid, body):
        fields(body, {"revisionId", "expectedRevision", "requestKey"}, {"revisionId", "expectedRevision", "requestKey"})
        def save(db):
            entity = self.store.one(db, "dr_entities", identifier(eid))
            revision(entity["revision"], body["expectedRevision"])
            row = self.store.one(db, "dr_entity_revisions", identifier(body["revisionId"]))
            if row["entity_id"] != eid:
                raise DramaError("wrong_entity", "修订不属于此实体")
            snapshot = json.loads(row["snapshot"])
            primary = confirming_reference(entity["kind"], snapshot["references"])
            if not primary:
                raise DramaError("reference_required", "请先放入至少一张参考图再确认")
            self._image_asset(db, entity["project_id"], primary["assetId"])
            self.media_path(entity["project_id"], primary["assetId"])
            db.execute("UPDATE dr_entities SET approved_revision_id=?,revision=revision+1 WHERE id=?", (row["id"], eid))
            approval_id = new_id()
            db.execute("INSERT INTO dr_production_approvals VALUES(?,?,?,?,?,?)", (approval_id, "entity", eid, row["id"], primary["assetId"], time.time()))
            affected = []
            for dep in db.execute("SELECT d.shot_revision_id,d.entity_revision_id,s.id AS shot_id FROM dr_dependencies d JOIN dr_entity_revisions e ON e.id=d.entity_revision_id JOIN dr_shots s ON s.current_content_revision_id=d.shot_revision_id WHERE e.entity_id=? AND d.entity_revision_id<>?", (eid, row["id"])):
                if entity["approved_revision_id"] == row["id"]:
                    continue
                db.execute("INSERT INTO dr_invalidations VALUES(?,?,?,?,?,?,NULL,NULL,?,?)", (new_id(), dep["shot_revision_id"], eid, dep["entity_revision_id"], row["id"], "实体批准版本已变更，请复核镜头参考", time.time(), approval_id))
                affected.append(dep["shot_id"])
            return {"entity": self._entity_view(db, self.store.one(db, "dr_entities", eid)), "affectedShotIds": sorted(set(affected))}
        return self.store.mutate(f"entity-approval:{eid}", body["requestKey"], body, save)

    @staticmethod
    def _default_snapshot(shot, snapshot):
        return {"title": snapshot.get("title", shot["title"]), "action": snapshot.get("action", ""),
                "framing": snapshot.get("framing", "medium"), "camera": snapshot.get("camera", "locked"),
                "prompt": snapshot.get("prompt", ""), "characterRevisionIds": snapshot.get("characterRevisionIds", []),
                "locationRevisionId": snapshot.get("locationRevisionId"), "costumeRevisionIds": snapshot.get("costumeRevisionIds", []),
                "propRevisionIds": snapshot.get("propRevisionIds", []), "approvedReferenceAssetId": snapshot.get("approvedReferenceAssetId"),
                "actionReferenceAssetId": snapshot.get("actionReferenceAssetId"),
                "actionReferencePrompt": snapshot.get("actionReferencePrompt", ""),
                "sceneReferenceAssetId": snapshot.get("sceneReferenceAssetId"),
                "sceneReferencePrompt": snapshot.get("sceneReferencePrompt", ""),
                "blockingReferenceAssetId": snapshot.get("blockingReferenceAssetId"),
                "blockingReferencePrompt": snapshot.get("blockingReferencePrompt", ""),
                "keyframeGridAssetId": snapshot.get("keyframeGridAssetId"),
                "keyframeGridPrompt": snapshot.get("keyframeGridPrompt", ""),
                "videoReferenceAssetId": snapshot.get("videoReferenceAssetId"),
                "audioReferenceAssetId": snapshot.get("audioReferenceAssetId"),
                "audioDescription": snapshot.get("audioDescription", ""),
                "firstFrameAssetId": snapshot.get("firstFrameAssetId"),
                "lastFrameAssetId": snapshot.get("lastFrameAssetId"),
                "referenceRole": snapshot.get("referenceRole", "reference_image"), "targetEditFrames": snapshot.get("targetEditFrames", 96),
                "referenceNodes": snapshot.get("referenceNodes"), "customReferenceRoles": snapshot.get("customReferenceRoles") or [],
                "generation": snapshot.get("generation", normalize_generation())}

    def _initialize_shot_content(self, db, sid):
        rid = new_id()
        db.execute("UPDATE dr_shot_revisions SET id=? WHERE shot_id=? AND revision=1", (rid, sid))
        db.execute("UPDATE dr_shots SET current_content_revision_id=? WHERE id=?", (rid, sid))
        db.execute("UPDATE dr_takes SET source_revision_id=? WHERE shot_id=?", (rid, sid))

    def _content_view(self, db, row):
        shot = self.store.one(db, "dr_shots", row["shot_id"])
        return {"id": row["id"], "revision": row["revision"], "snapshot": self._default_snapshot(shot, json.loads(row["snapshot"])),
                "approved": bool(db.execute("SELECT 1 FROM dr_production_approvals WHERE target_type='shot' AND revision_id=?", (row["id"],)).fetchone())}

    def _invalidations(self, db, rid):
        return [{"id": r["id"], "entityId": r["entity_id"], "oldRevisionId": r["old_revision_id"], "newRevisionId": r["new_revision_id"],
                 "reason": r["reason"], "resolved": r["resolved_at"] is not None, "resolution": r["resolution"]}
                for r in db.execute("SELECT * FROM dr_invalidations WHERE shot_revision_id=? ORDER BY created_at", (rid,))] + self.script_dependency_state(db, rid)

    def shot_production(self, sid):
        with self.store.connect() as db:
            shot = self.store.one(db, "dr_shots", identifier(sid))
            history = [self._content_view(db, r) for r in db.execute("SELECT * FROM dr_shot_revisions WHERE shot_id=? ORDER BY revision DESC", (sid,))]
            return {"shotId": sid, "revision": shot["revision"], "currentRevisionId": shot["current_content_revision_id"],
                    "current": next(r for r in history if r["id"] == shot["current_content_revision_id"]),
                    "history": history, "invalidations": self._invalidations(db, shot["current_content_revision_id"])}

    def _normalize_shot(self, db, pid, shot, snapshot):
        fields(snapshot, SHOT_FIELDS, {"title"})
        value = self._default_snapshot(shot, snapshot)
        value["title"] = text(value["title"], "镜头名", 160)
        for key, limit in (("action", 4000), ("actionReferencePrompt", 4000), ("sceneReferencePrompt", 4000),
                           ("blockingReferencePrompt", 4000), ("keyframeGridPrompt", 4000),
                           ("audioDescription", 2000), ("framing", 120), ("camera", 120), ("prompt", 16384)):
            if not isinstance(value[key], str) or len(value[key]) > limit:
                raise DramaError("invalid_snapshot", f"{key}字段格式或长度不合法")
        integer(value["targetEditFrames"], "剪辑帧数", 1, 86400)
        try:
            value["generation"] = normalize_generation(value["generation"])
        except ValueError as exc:
            raise DramaError("invalid_generation", str(exc)) from exc
        if value["referenceRole"] not in {"reference_image", "first_frame"}:
            raise DramaError("invalid_reference_role", "参考画面角色不合法")
        nodes = value.get("referenceNodes")
        if nodes is not None and not isinstance(nodes, list):
            raise DramaError("invalid_reference_nodes", "参考节点必须是列表")
        for node in nodes or []:
            if not isinstance(node, dict) or node.get("media") not in {"image", "video", "audio"} or node.get("role") not in {"character", "costume", "scene", "action", "first", "last", "custom"}:
                raise DramaError("invalid_reference_nodes", "参考节点缺少文件类型或用途")
            if node.get("assetId") and not isinstance(node.get("assetId"), str):
                raise DramaError("invalid_reference_nodes", "参考节点素材编号无效")
        dependencies = []
        for key, kind in DEPENDENCIES.items():
            refs = [value[key]] if key == "locationRevisionId" and value[key] else [] if key == "locationRevisionId" else value[key]
            if not isinstance(refs, list) or len(refs) > 16 or any(not isinstance(r, str) for r in refs) or len(refs) != len(set(refs)):
                raise DramaError("invalid_dependencies", "实体修订列表无效")
            for rid in refs:
                row = self.store.one(db, "dr_entity_revisions", identifier(rid))
                entity = self.store.one(db, "dr_entities", row["entity_id"])
                if entity["project_id"] != pid or entity["kind"] != kind:
                    raise DramaError("wrong_dependency", "实体类型或项目归属不匹配")
                if entity["approved_revision_id"] != rid:
                    raise DramaError("dependency_not_approved", "请选择实体当前已批准的版本", 409)
                dependencies.append((rid, kind))
        if value["approvedReferenceAssetId"]:
            self._image_asset(db, pid, value["approvedReferenceAssetId"])
        for key in (*SHOT_IMAGE_ASSET_KEYS, "firstFrameAssetId", "lastFrameAssetId"):
            if value[key]:
                self._image_asset(db, pid, value[key])
        if value["videoReferenceAssetId"]:
            asset = self._bound_asset(db, pid, value["videoReferenceAssetId"])
            metadata = json.loads(asset["metadata"])
            duration = metadata.get("duration")
            if metadata.get("kind") != "video" or not isinstance(duration, (int, float)) or not 2 <= duration <= 15:
                raise DramaError("invalid_video_reference", "动作参考视频须为可完整解码的 2–15 秒 MP4")
        if value["audioReferenceAssetId"]:
            asset = self._bound_asset(db, pid, value["audioReferenceAssetId"])
            metadata = json.loads(asset["metadata"])
            if metadata.get("kind") != "audio" or metadata.get("role") != "h3_reference":
                raise DramaError("invalid_audio_reference", "H3 参考音频须为已验证的 4 秒 WAV")
        return value, dependencies

    def _append_shot(self, db, sid, value, dependencies, approved=False):
        rid = new_id()
        version = db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM dr_shot_revisions WHERE shot_id=?", (sid,)).fetchone()[0]
        db.execute("INSERT INTO dr_shot_revisions(shot_id,revision,snapshot,created_at,id) VALUES(?,?,?,?,?)", (sid, version, encode(value), time.time(), rid))
        for entity_rid, role in dependencies:
            db.execute("INSERT INTO dr_dependencies VALUES(?,?,?)", (rid, entity_rid, role))
        self.carry_script_dependency(db, sid, rid)
        db.execute("UPDATE dr_shots SET title=?,current_content_revision_id=?,revision=revision+1 WHERE id=?", (value["title"], rid, sid))
        if approved:
            db.execute("INSERT INTO dr_production_approvals VALUES(?,?,?,?,?,?)", (new_id(), "shot", sid, rid, value["approvedReferenceAssetId"], time.time()))
        return rid

    def revise_shot(self, sid, body, *, canvas=False):
        allowed = {"expectedRevision", "baseRevisionId", "snapshot", "requestKey"}
        if canvas:
            allowed |= {"shotId", "templateVersion"}
        fields(body, allowed, allowed)
        if canvas and (body["shotId"] != sid or body["templateVersion"] != TEMPLATE):
            raise DramaError("invalid_projection", "画布模板或镜头身份不一致")
        def save(db):
            shot = self.store.one(db, "dr_shots", identifier(sid))
            revision(shot["revision"], body["expectedRevision"])
            if shot["current_content_revision_id"] != body["baseRevisionId"]:
                raise DramaError("revision_conflict", "镜头内容已变化，请重新打开画布或刷新", 409)
            ep = self.store.one(db, "dr_episodes", shot["episode_id"])
            value, dependencies = self._normalize_shot(db, ep["project_id"], shot, body["snapshot"])
            # Editing content keeps the reference as a draft; approval is explicit.
            rid = self._append_shot(db, sid, value, dependencies)
            return {"shotId": sid, "revision": shot["revision"] + 1, "currentRevisionId": rid,
                    "current": self._content_view(db, self.store.one(db, "dr_shot_revisions", rid))}
        return self.store.mutate(f"shot-content:{sid}", body["requestKey"], body, save)

    def canvas_projection(self, sid):
        result = self.shot_production(sid)
        return {"shotId": sid, "expectedRevision": result["revision"], "baseRevisionId": result["currentRevisionId"],
                "templateVersion": TEMPLATE, "snapshot": result["current"]["snapshot"]}

    def select_reference(self, sid, body):
        fields(body, {"expectedRevision", "assetId", "candidateId", "acceptStaleCandidate", "requestKey"}, {"expectedRevision", "assetId", "requestKey"})
        def save(db):
            shot = self.store.one(db, "dr_shots", identifier(sid))
            revision(shot["revision"], body["expectedRevision"])
            context = self.validate_reference_inputs("shot", shot["current_content_revision_id"], db=db)
            if "acceptStaleCandidate" in body and type(body["acceptStaleCandidate"]) is not bool:
                raise DramaError("invalid_confirmation", "旧候选确认必须为布尔值")
            if body.get("candidateId"):
                candidate = self.store.one(db, "dr_reference_candidates", identifier(body["candidateId"]))
                if candidate["target_type"] != "shot" or candidate["target_id"] != sid or candidate["asset_id"] != body["assetId"]:
                    raise DramaError("wrong_candidate", "图片候选不属于此镜头")
                if candidate["target_revision_id"] != shot["current_content_revision_id"] and body.get("acceptStaleCandidate") is not True:
                    raise DramaError("stale_candidate", "候选来自旧镜头版本，需要明确确认", 409)
                source = self.store.one(db, "dr_shot_revisions", candidate["target_revision_id"])
                source_role = self._default_snapshot(shot, json.loads(source["snapshot"]))["referenceRole"]
            else:
                source_role = context["snapshot"]["referenceRole"]
            self._image_asset(db, context["projectId"], body["assetId"])
            self.media_path(context["projectId"], body["assetId"])
            if (context["snapshot"].get("approvedReferenceAssetId") == body["assetId"] and
                    db.execute("SELECT 1 FROM dr_production_approvals WHERE target_type='shot' AND revision_id=? AND asset_id=?",
                               (shot["current_content_revision_id"], body["assetId"])).fetchone()):
                # As above, preserve validation while making a repeated selection
                # idempotent even when it arrives with a fresh request key.
                return self.shot_production(sid)
            value = {**context["snapshot"], "referenceRole": source_role, "approvedReferenceAssetId": body["assetId"]}
            deps = [(r[0], r[1]) for r in db.execute("SELECT entity_revision_id,role FROM dr_dependencies WHERE shot_revision_id=?", (context["revisionId"],))]
            rid = self._append_shot(db, sid, value, deps, approved=True)
            return {"shotId": sid, "revision": shot["revision"] + 1, "currentRevisionId": rid,
                    "current": self._content_view(db, self.store.one(db, "dr_shot_revisions", rid))}
        return self.store.mutate(f"shot-reference:{sid}", body["requestKey"], body, save)

    def select_slot_reference(self, sid, body, *, slot):
        """Attach a workbench planning/action image without replacing composition."""
        if slot not in SHOT_IMAGE_SLOTS:
            raise DramaError("invalid_slot", "镜头图片槽位不合法")
        asset_key, mutate_key, label = SHOT_IMAGE_SLOTS[slot]
        fields(body, {"expectedRevision", "assetId", "candidateId", "acceptStaleCandidate", "requestKey"},
               {"expectedRevision", "assetId", "requestKey"})
        def save(db):
            shot = self.store.one(db, "dr_shots", identifier(sid))
            revision(shot["revision"], body["expectedRevision"])
            context = self.validate_reference_inputs("shot", shot["current_content_revision_id"], db=db)
            if "acceptStaleCandidate" in body and type(body["acceptStaleCandidate"]) is not bool:
                raise DramaError("invalid_confirmation", "旧候选确认必须为布尔值")
            if body.get("candidateId"):
                candidate = self.store.one(db, "dr_reference_candidates", identifier(body["candidateId"]))
                if candidate["target_type"] != "shot" or candidate["target_id"] != sid or candidate["asset_id"] != body["assetId"]:
                    raise DramaError("wrong_candidate", f"{label}图片候选不属于此镜头")
                if candidate["target_revision_id"] != shot["current_content_revision_id"] and body.get("acceptStaleCandidate") is not True:
                    raise DramaError("stale_candidate", f"{label}候选来自旧镜头版本，需要明确确认", 409)
            self._image_asset(db, context["projectId"], body["assetId"])
            self.media_path(context["projectId"], body["assetId"])
            if context["snapshot"].get(asset_key) == body["assetId"]:
                return self.shot_production(sid)
            value = {**context["snapshot"], asset_key: body["assetId"]}
            deps = [(r[0], r[1]) for r in db.execute(
                "SELECT entity_revision_id,role FROM dr_dependencies WHERE shot_revision_id=?", (context["revisionId"],))]
            # This operation changes only the named slot; preserve an already
            # approved composition reference on the new immutable revision.
            rid = self._append_shot(db, sid, value, deps, approved=bool(context["approvedReferenceAssetId"]))
            return {"shotId": sid, "revision": shot["revision"] + 1, "currentRevisionId": rid,
                    "current": self._content_view(db, self.store.one(db, "dr_shot_revisions", rid))}
        return self.store.mutate(f"{mutate_key}:{sid}", body["requestKey"], body, save)

    def select_action_reference(self, sid, body):
        """Attach an explicitly chosen pose/action image without replacing composition."""
        return self.select_slot_reference(sid, body, slot="action")

    def review_shot(self, sid, body):
        fields(body, {"expectedRevision", "invalidationIds", "reason", "requestKey"}, {"expectedRevision", "invalidationIds", "reason", "requestKey"})
        reason = body["reason"]
        if not isinstance(reason, str) or len(reason) > 2000:
            raise DramaError("invalid_review", "复核意见须为2000字以内文本")
        ids = body["invalidationIds"]
        if not isinstance(ids, list) or not ids or len(ids) > 64 or any(not isinstance(i, str) for i in ids) or len(ids) != len(set(ids)):
            raise DramaError("invalid_review", "请选择需要处理的失效记录")
        def save(db):
            shot = self.store.one(db, "dr_shots", identifier(sid))
            revision(shot["revision"], body["expectedRevision"])
            for iid in ids:
                row = self.store.one(db, "dr_invalidations", identifier(iid))
                if row["shot_revision_id"] != shot["current_content_revision_id"] or row["resolved_at"] is not None:
                    raise DramaError("wrong_invalidation", "失效记录不属于当前镜头版本，或已处理", 409)
                db.execute("UPDATE dr_invalidations SET resolved_at=?,resolution=? WHERE id=?", (time.time(), reason, iid))
            db.execute("UPDATE dr_shots SET revision=revision+1 WHERE id=?", (sid,))
            return {"shotId": sid, "revision": shot["revision"] + 1, "invalidations": self._invalidations(db, shot["current_content_revision_id"])}
        return self.store.mutate(f"shot-review:{sid}", body["requestKey"], body, save)

    def get_revision_context(self, kind, rid, db=None, *, reference_role=None):
        with self.store.connect() if db is None else nullcontext(db) as conn:
            if kind == "entity":
                row = self.store.one(conn, "dr_entity_revisions", identifier(rid))
                target = self.store.one(conn, "dr_entities", row["entity_id"])
                approved = self.store.one(conn, "dr_entity_revisions", target["approved_revision_id"]) if target["approved_revision_id"] else None
                approved_refs = json.loads(approved["snapshot"]).get("references", []) if approved else []
                # Editing an approved view can use that exact original alone.
                # New/unapproved views still require the approved primary identity.
                found = next((ref for ref in approved_refs if ref.get("role") == reference_role), None) if reference_role else None
                found = found or confirming_reference(target["kind"], approved_refs)
                approved_primary = found["assetId"] if found else None
                return {"projectId": target["project_id"], "targetId": target["id"], "revisionId": rid,
                        "currentRevisionId": target["current_revision_id"], "revision": target["revision"], "snapshot": json.loads(row["snapshot"]),
                        "approvedReferenceAssetId": approved_primary,
                        "requiredReferenceAssetIds": [approved_primary] if approved_primary else []}
            if kind != "shot":
                raise DramaError("invalid_target", "图片任务目标类型不正确")
            row = self.store.one(conn, "dr_shot_revisions", identifier(rid))
            target = self.store.one(conn, "dr_shots", row["shot_id"])
            ep = self.store.one(conn, "dr_episodes", target["episode_id"])
            snapshot = self._default_snapshot(target, json.loads(row["snapshot"]))
            approved_reference = snapshot["approvedReferenceAssetId"] if conn.execute("SELECT 1 FROM dr_production_approvals WHERE target_type='shot' AND revision_id=? AND asset_id=?", (rid, snapshot["approvedReferenceAssetId"])).fetchone() else None
            required = [approved_reference] if approved_reference else []
            if not required:
                dependencies = conn.execute("SELECT er.snapshot,e.kind FROM dr_dependencies d JOIN dr_entity_revisions er ON er.id=d.entity_revision_id JOIN dr_entities e ON e.id=er.entity_id WHERE d.shot_revision_id=? ORDER BY e.kind,e.id", (rid,)).fetchall()
                for dependency in dependencies:
                    found = confirming_reference(dependency["kind"], json.loads(dependency["snapshot"]).get("references", []))
                    primary = found["assetId"] if found else None
                    if primary and primary not in required:
                        required.append(primary)
            return {"projectId": ep["project_id"], "targetId": target["id"], "revisionId": rid,
                    "currentRevisionId": target["current_content_revision_id"], "revision": target["revision"], "snapshot": snapshot,
                    "approvedReferenceAssetId": approved_reference, "requiredReferenceAssetIds": required}

    def validate_reference_inputs(self, kind, rid, db=None, *, reference_role=None):
        with self.store.connect() if db is None else nullcontext(db) as conn:
            context = self.get_revision_context(kind, rid, db=conn, reference_role=reference_role)
            if context["currentRevisionId"] != rid:
                raise DramaError("revision_conflict", "目标内容已经更新，请刷新后再操作", 409)
            if kind == "shot" and any(not r["resolved"] for r in self._invalidations(conn, rid)):
                raise DramaError("dependencies_stale", "镜头依赖已更新，请先复核或更换引用", 409)
            return context
