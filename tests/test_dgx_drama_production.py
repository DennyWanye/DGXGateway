"""P2-A/B/C/E: immutable revisions, exact dependency decisions and canvas CAS."""
import io
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from PIL import Image

from DGXGateway.dgx_gateway.drama.contracts import DramaError
from DGXGateway.dgx_gateway.drama.service import DramaService


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = DramaService(self.root, self.root)
        self.pid = self.service.create_project({"title": "修订测试", "requestKey": "p"})["project"]["id"]
        self.eid = self.service.create_episode(self.pid, {"title": "第一集", "requestKey": "e"})["episode"]["id"]
        self.serial = 0
        buffer = io.BytesIO()
        Image.new("RGB", (64, 64), "steelblue").save(buffer, format="PNG")
        self.image = self.service.upload_image(self.pid, io.BytesIO(buffer.getvalue()), len(buffer.getvalue()), "face.png", "image")["asset"]["id"]

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def key(self):
        self.serial += 1
        return f"test-request-{self.serial}"

    def entity(self, kind="character", approved=True):
        entity = self.service.create_entity(self.pid, {"kind": kind, "name": kind, "referenceAssetId": self.image, "requestKey": self.key()})["entity"]
        if approved:
            entity = self.service.approve_entity(entity["id"], {"revisionId": entity["currentRevisionId"], "expectedRevision": entity["revision"], "requestKey": self.key()})["entity"]
        return entity

    def revise_entity(self, entity):
        return self.service.revise_entity(entity["id"], {"expectedRevision": entity["revision"], "name": entity["current"]["name"] + "新", "referenceAssetId": self.image, "requestKey": self.key()})["entity"]

    def shot(self, **values):
        sid = self.service.create_shot(self.eid, {"title": "镜头", "requestKey": self.key()})["shot"]["id"]
        if values:
            current = self.service.shot_production(sid)
            self.service.revise_shot(sid, {"expectedRevision": current["revision"], "baseRevisionId": current["currentRevisionId"],
                                         "snapshot": {**current["current"]["snapshot"], **values}, "requestKey": self.key()})
        return sid

    def candidate(self, target_type, target_id, target_revision_id, asset_id):
        """Create an already-archived candidate without invoking an image model."""
        from DGXGateway.dgx_gateway.drama.store import new_id
        with self.service.store.connect(write=True) as db:
            jid, cid = new_id(), new_id()
            now = 1.0
            db.execute("INSERT INTO dr_reference_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (jid, self.pid, target_type, target_id, target_revision_id, "generate_image", "{}", "{}",
                        "test-" + jid, None, "SUCCEEDED", None, now, now))
            db.execute("INSERT INTO dr_reference_candidates VALUES (?,?,?,?,?,?,?,?,?)",
                       (cid, self.pid, target_type, target_id, target_revision_id, asset_id, jid, "test-source", now))
        return cid

    def approve(self, entity):
        return self.service.approve_entity(entity["id"], {"revisionId": entity["currentRevisionId"], "expectedRevision": entity["revision"], "requestKey": self.key()})

    def test_all_entity_kinds_keep_immutable_references_and_restart(self):
        for kind in ("character", "location", "costume", "prop"):
            first = self.entity(kind)
            updated = self.revise_entity(first)
            self.assertEqual(updated["approvedRevisionId"], first["currentRevisionId"])
            self.assertEqual(updated["revisions"][1], first["current"])
            self.assertNotEqual(updated["currentRevisionId"], first["currentRevisionId"])
        self.service.close()
        self.service = DramaService(self.root, self.root)
        self.assertEqual(len(self.service.entities(self.pid)["entities"]), 4)
        self.assertTrue(self.service.media_path(self.pid, self.image).exists())

    def test_draft_does_not_invalidate_and_approval_only_affects_exact_dependencies(self):
        face = self.entity()
        sid = self.shot(characterRevisionIds=[face["currentRevisionId"]])
        unrelated = self.shot()
        updated = self.revise_entity(face)
        self.assertEqual(self.service.shot_production(sid)["invalidations"], [])
        result = self.approve(updated)
        self.assertEqual(result["affectedShotIds"], [sid])
        self.assertEqual(self.service.shot_production(unrelated)["invalidations"], [])
        production = self.service.shot_production(sid)
        self.assertEqual(production["current"]["snapshot"]["characterRevisionIds"], [face["currentRevisionId"]])
        with self.assertRaises(DramaError) as error:
            self.service.validate_reference_inputs("shot", production["currentRevisionId"])
        self.assertEqual(error.exception.code, "dependencies_stale")

    def test_review_one_reason_does_not_clear_another(self):
        face, prop = self.entity(), self.entity("prop")
        sid = self.shot(characterRevisionIds=[face["currentRevisionId"]], propRevisionIds=[prop["currentRevisionId"]])
        self.approve(self.revise_entity(face)); self.approve(self.revise_entity(prop))
        shot = self.service.shot_production(sid)
        self.assertEqual(len(shot["invalidations"]), 2)
        result = self.service.review_shot(sid, {"expectedRevision": shot["revision"], "invalidationIds": [shot["invalidations"][0]["id"]], "reason": "本镜头明确继续旧人物设定", "requestKey": self.key()})
        self.assertEqual(sum(not r["resolved"] for r in result["invalidations"]), 1)
        with self.assertRaises(DramaError): self.service.validate_reference_inputs("shot", shot["currentRevisionId"])

    def test_new_revision_with_new_dependency_preserves_history(self):
        face = self.entity()
        sid = self.shot(characterRevisionIds=[face["currentRevisionId"]])
        old = self.service.shot_production(sid)
        newface = self.approve(self.revise_entity(face))["entity"]
        result = self.service.revise_shot(sid, {"expectedRevision": old["revision"], "baseRevisionId": old["currentRevisionId"],
                  "snapshot": {**old["current"]["snapshot"], "characterRevisionIds": [newface["approvedRevisionId"]]}, "requestKey": self.key()})
        self.service.validate_reference_inputs("shot", result["currentRevisionId"])
        history = self.service.shot_production(sid)["history"]
        self.assertEqual(history[1]["snapshot"], old["current"]["snapshot"])

    def test_repeated_approval_cycle_requires_a_new_review(self):
        face = self.entity()
        sid = self.shot(characterRevisionIds=[face["currentRevisionId"]])
        v2 = self.approve(self.revise_entity(face))["entity"]
        old = self.service.shot_production(sid)
        self.service.review_shot(sid, {"expectedRevision": old["revision"], "invalidationIds": [old["invalidations"][0]["id"]], "reason": "本次继续旧版", "requestKey": self.key()})
        reverted = self.service.approve_entity(face["id"], {"revisionId": face["currentRevisionId"], "expectedRevision": v2["revision"], "requestKey": self.key()})["entity"]
        self.service.approve_entity(face["id"], {"revisionId": v2["currentRevisionId"], "expectedRevision": reverted["revision"], "requestKey": self.key()})
        now = self.service.shot_production(sid)
        self.assertEqual(len(now["invalidations"]), 2)
        self.assertTrue(now["invalidations"][0]["resolved"])
        self.assertFalse(now["invalidations"][1]["resolved"])
        with self.assertRaises(DramaError): self.service.validate_reference_inputs("shot", now["currentRevisionId"])

    def test_side_reference_cannot_replace_approved_identity_input(self):
        with self.assertRaises(DramaError):
            self.service.create_entity(self.pid, {"kind": "character", "name": "face", "referenceAssetId": self.image,
                "referenceRole": "identity_3q", "references": [{"role": "identity_front", "assetId": self.image}], "requestKey": self.key()})
        entity = self.entity()
        self.assertEqual(self.service.get_revision_context("entity", entity["currentRevisionId"])["approvedReferenceAssetId"], self.image)

    def test_turnaround_sheet_is_its_own_slot(self):
        entity = self.service.create_entity(self.pid, {"kind": "character", "name": "天舞",
            "references": [{"role": "identity_sheet", "assetId": self.image}], "requestKey": self.key()})["entity"]
        self.assertEqual(entity["current"]["references"], [{"role": "identity_sheet", "assetId": self.image}])
        self.assertEqual(entity["current"]["referenceRole"], "identity_sheet")
        self.assertIsNone(entity["approvedRevisionId"])
        approved = self.approve(entity)["entity"]
        self.assertEqual(approved["approvedRevisionId"], entity["currentRevisionId"])
        context = self.service.get_revision_context("entity", approved["approvedRevisionId"])
        self.assertEqual(context["approvedReferenceAssetId"], self.image)

    def test_only_real_shot_approval_can_replace_entity_reference_requirements(self):
        face = self.entity()
        buffer = io.BytesIO()
        Image.new("RGB", (64, 64), "tomato").save(buffer, format="PNG")
        other = self.service.upload_image(self.pid, io.BytesIO(buffer.getvalue()), len(buffer.getvalue()), "layout.png", self.key())["asset"]["id"]
        sid = self.shot(characterRevisionIds=[face["currentRevisionId"]], approvedReferenceAssetId=other)
        current = self.service.shot_production(sid)
        context = self.service.get_revision_context("shot", current["currentRevisionId"])
        self.assertIsNone(context["approvedReferenceAssetId"])
        self.assertEqual(context["requiredReferenceAssetIds"], [self.image])
        approved = self.service.select_reference(sid, {"expectedRevision": current["revision"], "assetId": other, "requestKey": self.key()})
        context = self.service.get_revision_context("shot", approved["currentRevisionId"])
        self.assertEqual(context["requiredReferenceAssetIds"], [other])
        self.assertEqual(context["approvedReferenceAssetId"], other)

    def test_reference_approval_is_new_content_version_and_other_edits_are_drafts(self):
        sid = self.shot()
        original = self.service.shot_production(sid)
        approved = self.service.select_reference(sid, {"expectedRevision": original["revision"], "assetId": self.image, "requestKey": self.key()})
        self.assertTrue(approved["current"]["approved"])
        self.assertNotEqual(approved["currentRevisionId"], original["currentRevisionId"])
        draft = self.service.revise_shot(sid, {"expectedRevision": approved["revision"], "baseRevisionId": approved["currentRevisionId"], "snapshot": approved["current"]["snapshot"], "requestKey": self.key()})
        self.assertFalse(draft["current"]["approved"])
        self.assertTrue(self.service.shot_production(sid)["history"][1]["approved"])

    def test_candidate_selection_is_idempotent_and_preserves_other_entity_slots(self):
        entity = self.entity()
        candidate = self.candidate("entity", entity["id"], entity["currentRevisionId"], self.image)
        # The user later fills a second slot. Selecting the old candidate must
        # replace only its original source slot, not discard this newer work.
        entity = self.service.revise_entity(entity["id"], {
            "expectedRevision": entity["revision"], "name": entity["current"]["name"],
            "description": entity["current"]["description"], "referenceRole": "identity_front",
            "referenceAssetId": self.image,
            "references": [*entity["current"]["references"], {"role": "identity_3q", "assetId": self.image}],
            "requestKey": self.key()})["entity"]
        chosen = self.service.revise_entity(entity["id"], {
            "expectedRevision": entity["revision"], "name": "ignored by candidate selection",
            "referenceAssetId": self.image, "candidateId": candidate, "acceptStaleCandidate": True,
            "requestKey": self.key()})["entity"]
        self.assertEqual({ref["role"] for ref in chosen["current"]["references"]}, {"identity_front", "identity_3q"})
        repeated = self.service.revise_entity(chosen["id"], {
            "expectedRevision": chosen["revision"], "name": "also ignored", "referenceAssetId": self.image,
            "candidateId": candidate, "acceptStaleCandidate": True, "requestKey": self.key()})["entity"]
        self.assertEqual(repeated["revision"], chosen["revision"])
        self.assertEqual(len(repeated["revisions"]), len(chosen["revisions"]))

    def test_repeated_shot_candidate_selection_does_not_append_a_revision(self):
        sid = self.shot()
        original = self.service.shot_production(sid)
        candidate = self.candidate("shot", sid, original["currentRevisionId"], self.image)
        selected = self.service.select_reference(sid, {"expectedRevision": original["revision"], "assetId": self.image,
            "candidateId": candidate, "requestKey": self.key()})
        repeated = self.service.select_reference(sid, {"expectedRevision": selected["revision"], "assetId": self.image,
            "candidateId": candidate, "acceptStaleCandidate": True, "requestKey": self.key()})
        self.assertEqual(repeated["revision"], selected["revision"])
        self.assertEqual(repeated["currentRevisionId"], selected["currentRevisionId"])

    def test_action_reference_is_a_separate_idempotent_slot_and_preserves_primary(self):
        sid = self.shot()
        original = self.service.shot_production(sid)
        primary = self.service.select_reference(sid, {"expectedRevision": original["revision"], "assetId": self.image,
            "requestKey": self.key()})
        candidate = self.candidate("shot", sid, primary["currentRevisionId"], self.image)
        action = self.service.select_action_reference(sid, {"expectedRevision": primary["revision"], "assetId": self.image,
            "candidateId": candidate, "requestKey": self.key()})
        self.assertEqual(action["current"]["snapshot"]["approvedReferenceAssetId"], self.image)
        self.assertEqual(action["current"]["snapshot"]["actionReferenceAssetId"], self.image)
        self.assertTrue(action["current"]["approved"])
        repeated = self.service.select_action_reference(sid, {"expectedRevision": action["revision"], "assetId": self.image,
            "candidateId": candidate, "acceptStaleCandidate": True, "requestKey": self.key()})
        self.assertEqual(repeated["revision"], action["revision"])
        self.assertEqual(repeated["currentRevisionId"], action["currentRevisionId"])

    def test_planning_slots_are_separate_idempotent_and_preserve_primary(self):
        sid = self.shot()
        original = self.service.shot_production(sid)
        current = self.service.select_reference(sid, {"expectedRevision": original["revision"], "assetId": self.image,
            "requestKey": self.key()})
        self.assertIsNone(current["current"]["snapshot"]["sceneReferenceAssetId"])
        self.assertEqual(current["current"]["snapshot"]["sceneReferencePrompt"], "")
        self.assertIsNone(current["current"]["snapshot"]["blockingReferenceAssetId"])
        self.assertIsNone(current["current"]["snapshot"]["keyframeGridAssetId"])
        for slot, key in (("scene", "sceneReferenceAssetId"), ("blocking", "blockingReferenceAssetId"),
                          ("keyframeGrid", "keyframeGridAssetId")):
            candidate = self.candidate("shot", sid, current["currentRevisionId"], self.image)
            selected = self.service.select_slot_reference(sid, {"expectedRevision": current["revision"], "assetId": self.image,
                "candidateId": candidate, "requestKey": self.key()}, slot=slot)
            self.assertEqual(selected["current"]["snapshot"]["approvedReferenceAssetId"], self.image)
            self.assertEqual(selected["current"]["snapshot"][key], self.image)
            self.assertTrue(selected["current"]["approved"])
            repeated = self.service.select_slot_reference(sid, {"expectedRevision": selected["revision"], "assetId": self.image,
                "candidateId": candidate, "acceptStaleCandidate": True, "requestKey": self.key()}, slot=slot)
            self.assertEqual(repeated["revision"], selected["revision"])
            self.assertEqual(repeated["currentRevisionId"], selected["currentRevisionId"])
            current = selected

    def test_revise_shot_accepts_planning_slot_fields(self):
        sid = self.shot()
        current = self.service.shot_production(sid)
        saved = self.service.revise_shot(sid, {"expectedRevision": current["revision"],
            "baseRevisionId": current["currentRevisionId"],
            "snapshot": {**current["current"]["snapshot"], "sceneReferenceAssetId": self.image,
                         "sceneReferencePrompt": "教室空镜", "blockingReferencePrompt": "课桌边沿",
                         "keyframeGridPrompt": "九宫格"}, "requestKey": self.key()})
        snap = saved["current"]["snapshot"]
        self.assertEqual(snap["sceneReferenceAssetId"], self.image)
        self.assertEqual(snap["sceneReferencePrompt"], "教室空镜")
        self.assertEqual(snap["blockingReferencePrompt"], "课桌边沿")
        self.assertEqual(snap["keyframeGridPrompt"], "九宫格")
        self.assertIsNone(snap["actionReferenceAssetId"])

    def test_canvas_round_trip_idempotent_then_stale_conflict(self):
        sid = self.shot()
        projection = self.service.canvas_projection(sid)
        body = {**projection, "snapshot": {**projection["snapshot"], "action": "转身看向门口"}, "requestKey": self.key()}
        saved = self.service.revise_shot(sid, body, canvas=True)
        self.assertEqual(saved, self.service.revise_shot(sid, body, canvas=True))
        self.assertEqual(saved["current"]["snapshot"]["action"], "转身看向门口")
        with self.assertRaises(DramaError) as error:
            self.service.revise_shot(sid, {**body, "requestKey": self.key()}, canvas=True)
        self.assertEqual(error.exception.status, 409)
        fresh = self.service.canvas_projection(sid)
        for bad in ({"shotId": "f" * 32}, {"templateVersion": "unknown"}, {"snapshot": {**fresh["snapshot"], "approved": True}}):
            with self.assertRaises(DramaError):
                self.service.revise_shot(sid, {**fresh, **bad, "requestKey": self.key()}, canvas=True)

    def test_wrong_project_kind_unapproved_and_duplicate_dependency_rejected(self):
        face, location, draft = self.entity(), self.entity("location"), self.entity(approved=False)
        other = self.service.create_project({"title": "other", "requestKey": self.key()})["project"]["id"]
        foreign = self.service.create_entity(other, {"kind": "character", "name": "foreign", "requestKey": self.key()})["entity"]
        for refs in ([location["currentRevisionId"]], [draft["currentRevisionId"]], [foreign["currentRevisionId"]], [face["currentRevisionId"]] * 2):
            with self.assertRaises(DramaError): self.shot(characterRevisionIds=refs)
        with self.assertRaises(DramaError):
            self.service.create_entity(other, {"name": "wrong", "kind": "character", "referenceAssetId": self.image, "requestKey": self.key()})
        with self.assertRaises(DramaError):
            self.service.create_entity(self.pid, {"name": "wrong role", "kind": "location", "referenceAssetId": self.image, "referenceRole": "identity_front", "requestKey": self.key()})


class MigrationTests(unittest.TestCase):
    def test_upgrade_real_v1_shape_preserves_ids_and_selection_without_fabricating_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "drama").mkdir()
            dbpath = root / "drama/drama.sqlite3"
            ids = [str(i) * 32 for i in range(1, 5)]
            pid, eid, sid, tid = ids
            aid = "a" * 64
            with closing(sqlite3.connect(dbpath)) as db, db:
                db.executescript((Path(__file__).parents[1] / "DGXGateway/dgx_gateway/drama/migrations/001_initial.sql").read_text())
                db.execute("INSERT INTO dr_projects VALUES(?,?,?,?,?,?,?)", (pid, "p", 1344, 768, 24, 1, 1))
                db.execute("INSERT INTO dr_episodes VALUES(?,?,?,?,?,?)", (eid, pid, "e", 1, 0, 1))
                db.execute("INSERT INTO dr_assets VALUES(?,?,?,?,?)", (aid, "old.mp4", json.dumps({"kind": "video"}), 1, 1))
                db.execute("INSERT INTO dr_asset_bindings VALUES(?,?)", (pid, aid))
                db.execute("INSERT INTO dr_shots VALUES(?,?,?,?,?,?,?)", (sid, eid, "old", 1, 1, None, 1))
                db.execute("INSERT INTO dr_takes VALUES(?,?,?,?,?)", (tid, sid, aid, "imported", 1))
                db.execute("UPDATE dr_shots SET revision=2,selected_take_id=? WHERE id=?", (tid, sid))
                db.execute("INSERT INTO dr_shot_revisions VALUES(?,?,?,?)", (sid, 1, '{"title":"old"}', 1))
            service = DramaService(root, root)
            try:
                prod = service.shot_production(sid)
                shot = service.episode(eid)["shots"][0]
                self.assertEqual((prod["revision"], len(prod["history"]), prod["current"]["revision"]), (2, 1, 1))
                self.assertEqual(shot["selectedTakeId"], tid)
                self.assertEqual(shot["takes"][0]["sourceRevisionId"], prod["currentRevisionId"])
                self.assertFalse(shot["takes"][0]["stale"])
                self.assertTrue(dbpath.with_name("drama.sqlite3.before-v2.bak").exists())
                saved = prod
            finally: service.close()
            service = DramaService(root, root)
            try: self.assertEqual(service.shot_production(sid), saved)
            finally: service.close()


if __name__ == "__main__": unittest.main()
