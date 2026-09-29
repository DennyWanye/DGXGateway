"""P2 image contracts using real decoded images and a provider that counts POSTs."""
import io
import json
from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image

from DGXGateway.dgx_gateway.drama.contracts import DramaError
from DGXGateway.dgx_gateway.drama.gateway_adapter import GatewayReferenceAdapter, validate_parameters
from DGXGateway.dgx_gateway.drama.references import decode_image
from DGXGateway.dgx_gateway.drama.service import DramaService
from DGXGateway.dgx_gateway.drama.store import new_id


def image_bytes(color="red", format="PNG", size=(32, 24)):
    data = io.BytesIO()
    Image.new("RGB", size, color).save(data, format=format)
    return data.getvalue()


class FakeGateway:
    def __init__(self):
        self.submissions = []
        self.records = {}
        self.keys = {}
        self.fail_after_accept = False
        self.fail_before_accept = False
        self.initial_state = "SUCCEEDED"
        self.output_bytes = [image_bytes()]
        self.cancel_count = 0

    def submit(self, tool, parameters, dispatch_key, images):
        self.submissions.append((tool, parameters, dispatch_key, images))
        if self.fail_before_accept:
            raise ConnectionError("no reply")
        jid = new_id()
        record = {"job_id": jid, "state": self.initial_state,
                  "outputs": [{"sourceKey": str(index), "filename": f"candidate-{index}.png", "data": data}
                              for index, data in enumerate(self.output_bytes)]}
        self.records[jid] = record
        self.keys[dispatch_key] = jid
        if self.fail_after_accept:
            raise ConnectionError("reply lost after durable acceptance")
        return record

    def get(self, tool, jid):
        return self.records[jid]

    def find(self, tool, key):
        return self.records.get(self.keys.get(key))

    def outputs(self, tool, record):
        return record.get("outputs", [])

    def cancel(self, tool, jid):
        self.cancel_count += 1
        return {**self.records[jid], "state": "RUNNING"}


class DramaReferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.gateway = FakeGateway()
        self.service = DramaService(self.root / "state", self.root / "repo", reference_adapter=self.gateway)
        self.addCleanup(lambda: self.service.close())
        self.pid = self.service.create_project({"title": "短剧", "requestKey": "project"})["project"]["id"]
        self.entity = self.service.create_entity(self.pid, {"kind": "character", "name": "角色", "requestKey": "entity"})["entity"]

    def upload(self, data=None, pid=None, key=None):
        data = image_bytes() if data is None else data
        return self.service.upload_image(pid or self.pid, io.BytesIO(data), len(data), "图.png", key or new_id())["asset"]

    def submit(self, tool="generate_image", parameters=None, key="submission", rid=None):
        return self.service.submit_reference_job("entity", rid or self.entity["currentRevisionId"], {
            "tool": tool, "parameters": parameters or {"prompt": "一个人物"}, "requestKey": key})["job"]

    def drain(self):
        # A barrier on the single submission executor, not a timing-based sleep.
        self.service._reference_executor.submit(lambda: None).result(timeout=5)

    @contextmanager
    def hold_submission_executor(self):
        entered, release = threading.Event(), threading.Event()
        def barrier():
            entered.set()
            if not release.wait(5):
                raise TimeoutError("submission barrier was not released")
        future = self.service._reference_executor.submit(barrier)
        self.assertTrue(entered.wait(5))
        try:
            yield
        finally:
            release.set()
            future.result(timeout=5)

    def test_upload_checks_real_content_and_persists_actual_extension(self):
        for format, extension in (("PNG", "png"), ("JPEG", "jpg"), ("WEBP", "webp")):
            asset = self.upload(image_bytes(format=format))
            self.assertEqual(asset["extension"], extension)
            self.assertEqual((asset["width"], asset["height"]), (32, 24))
            self.assertTrue((self.service.root / "assets" / (asset["id"] + "." + extension)).is_file())
        for data in (b"not an image", image_bytes()[:30]):
            with self.assertRaises(DramaError):
                self.upload(data)
        self.assertEqual(len(self.service.project(self.pid)["assets"]), 3)

    def test_three_tools_ordered_inputs_and_multiple_candidates(self):
        first = self.upload(image_bytes("red"))
        second = self.upload(image_bytes("blue"))
        cases = [
            ("generate_image", {"prompt": "生成场景", "width": 512, "height": 512}, []),
            ("edit_image", {"prompt": "改变背景", "asset_ids": [second["id"], first["id"]], "batch_count": 2}, [second["id"], first["id"]]),
            ("compose_image_layers", {"background_asset_id": first["id"], "layers": [{"name": "人", "asset_id": second["id"], "x": 0, "y": 0, "width": 12, "height": 12, "opacity": 100}]}, [first["id"], second["id"]]),
        ]
        for index, (tool, parameters, ordered) in enumerate(cases):
            with self.subTest(tool=tool):
                self.gateway.output_bytes = [image_bytes("red"), image_bytes("blue")] if index == 1 else [image_bytes()]
                job = self.submit(tool, parameters, key=str(index))
                self.drain()
                result = self.service.reference_job(job["id"])["job"]
                self.assertEqual(result["state"], "SUCCEEDED")
                self.assertEqual(len(result["candidates"]), 2 if index == 1 else 1)
                self.assertEqual([item["assetId"] for item in self.gateway.submissions[-1][3]], ordered)
                self.assertTrue(all(candidate["targetRevisionId"] == self.entity["currentRevisionId"] for candidate in result["candidates"]))

    def test_entity_reference_job_can_target_non_primary_slot(self):
        front = self.upload(image_bytes("green"))
        side = self.upload(image_bytes("blue"))
        entity = self.service.revise_entity(self.entity["id"], {
            "expectedRevision": self.entity["revision"], "name": "角色",
            "referenceAssetId": front["id"], "referenceRole": "identity_front", "requestKey": "front-ref"})["entity"]
        job = self.service.submit_reference_job("entity", entity["currentRevisionId"], {
            "tool": "edit_image", "parameters": {"prompt": "四分之三侧面", "asset_ids": [side["id"]]},
            "requestKey": "slot-3q", "referenceRole": "identity_3q"})["job"]
        self.assertEqual(job["referenceRole"], "identity_3q")
        self.drain()
        result = self.service.reference_job(job["id"])["job"]
        self.assertEqual(result["state"], "SUCCEEDED")
        self.assertEqual(result["referenceRole"], "identity_3q")
        candidate = result["candidates"][0]
        item = next(row for row in self.service.image_review_queue(self.pid, None, "project")["items"]
                    if row["candidateId"] == candidate["id"])
        self.assertEqual(item["role"], "identity_3q")
        chosen = self.service.revise_entity(entity["id"], {
            "expectedRevision": entity["revision"], "name": entity["current"]["name"],
            "referenceAssetId": candidate["assetId"], "candidateId": candidate["id"], "requestKey": "pick-3q"})["entity"]
        refs = {ref["role"]: ref["assetId"] for ref in chosen["current"]["references"]}
        self.assertEqual(refs["identity_front"], front["id"])
        self.assertEqual(refs["identity_3q"], candidate["assetId"])
        self.assertEqual(chosen["current"]["referenceRole"], "identity_front")

    def test_entity_reference_job_can_target_back_view_slot(self):
        image = self.upload(image_bytes("teal"))
        job = self.service.submit_reference_job("entity", self.entity["currentRevisionId"], {
            "tool": "edit_image", "parameters": {"prompt": "背视图", "asset_ids": [image["id"]]},
            "requestKey": "slot-back", "referenceRole": "identity_back"})["job"]
        self.assertEqual(job["referenceRole"], "identity_back")
        self.drain()
        result = self.service.reference_job(job["id"])["job"]
        self.assertEqual(result["state"], "SUCCEEDED")
        self.assertEqual(result["referenceRole"], "identity_back")
        item = next(row for row in self.service.image_review_queue(self.pid, None, "project")["items"]
                    if row["candidateId"] == result["candidates"][0]["id"])
        self.assertEqual(item["role"], "identity_back")

    def test_edit_approved_view_requires_that_original_without_front_pose(self):
        front = self.upload(image_bytes("green"))["id"]
        side = self.upload(image_bytes("blue"))["id"]
        entity = self.service.revise_entity(self.entity["id"], {
            "expectedRevision": self.entity["revision"], "name": "角色", "requestKey": "approved-views",
            "references": [{"assetId": front, "role": "identity_front"}, {"assetId": side, "role": "identity_3q"}]})["entity"]
        self.service.approve_entity(entity["id"], {"revisionId": entity["currentRevisionId"],
            "expectedRevision": entity["revision"], "requestKey": "approve-views"})
        body = {"tool": "edit_image", "parameters": {"prompt": "只改光影", "asset_ids": [side]},
                "requestKey": "side-light", "referenceRole": "identity_3q"}
        with self.assertRaises(DramaError) as wrong:
            self.service.submit_reference_job("entity", entity["currentRevisionId"], {
                **body, "parameters": {"prompt": "只改光影", "asset_ids": [front]}, "requestKey": "wrong-view"})
        self.assertEqual(wrong.exception.code, "approved_reference_required")
        job = self.service.submit_reference_job("entity", entity["currentRevisionId"], body)["job"]
        self.drain()
        self.assertEqual(self.service.reference_job(job["id"])["job"]["state"], "SUCCEEDED")
        self.assertEqual([item["assetId"] for item in self.gateway.submissions[-1][3]], [side])
        # A view without an approved original must still include the primary.
        with self.assertRaises(DramaError) as missing:
            self.service.submit_reference_job("entity", entity["currentRevisionId"], {
                **body, "referenceRole": "identity_back", "requestKey": "new-back"})
        self.assertEqual(missing.exception.code, "approved_reference_required")

    def test_queued_view_edit_rechecks_approved_slot_before_dispatch(self):
        front = self.upload(image_bytes("green"))["id"]
        side = self.upload(image_bytes("blue"))["id"]
        replacement = self.upload(image_bytes("yellow"))["id"]
        refs = [{"assetId": front, "role": "identity_front"}, {"assetId": side, "role": "identity_3q"}]
        entity = self.service.revise_entity(self.entity["id"], {"expectedRevision": 1, "name": "角色",
            "references": refs, "requestKey": "views-queued"})["entity"]
        self.service.approve_entity(entity["id"], {"revisionId": entity["currentRevisionId"],
            "expectedRevision": entity["revision"], "requestKey": "approve-queued"})
        live = self.service.entity(entity["id"])["entity"]
        draft = self.service.revise_entity(entity["id"], {"expectedRevision": live["revision"], "name": "角色",
            "references": [refs[0], {"assetId": replacement, "role": "identity_3q"}], "requestKey": "replacement-view"})["entity"]
        with self.hold_submission_executor():
            job = self.service.submit_reference_job("entity", draft["currentRevisionId"], {
                "tool": "edit_image", "parameters": {"prompt": "只改光影", "asset_ids": [side]},
                "requestKey": "queued-side", "referenceRole": "identity_3q"})["job"]
            self.service.approve_entity(entity["id"], {"revisionId": draft["currentRevisionId"],
                "expectedRevision": draft["revision"], "requestKey": "change-approved-slot"})
        self.drain()
        result = self.service.reference_job(job["id"])["job"]
        self.assertNotEqual(result["state"], "SUCCEEDED")
        self.assertEqual(self.gateway.submissions, [])

    def test_reference_role_is_rejected_for_shots_and_unknown_slots(self):
        with self.assertRaises(DramaError) as error:
            self.service.submit_reference_job("entity", self.entity["currentRevisionId"], {
                "tool": "generate_image", "parameters": {"prompt": "x"},
                "requestKey": "bad-role", "referenceRole": "location_wide"})
        self.assertEqual(error.exception.code, "invalid_reference_role")
        episode = self.service.create_episode(self.pid, {"title": "集", "requestKey": "ep-role"})["episode"]
        shot = self.service.create_shot(episode["id"], {"title": "镜", "requestKey": "shot-role"})["shot"]
        with self.assertRaises(DramaError) as error:
            self.service.submit_reference_job("shot", shot["currentRevisionId"], {
                "tool": "generate_image", "parameters": {"prompt": "x"},
                "requestKey": "shot-role-job", "referenceRole": "identity_3q"})
        self.assertEqual(error.exception.code, "invalid_reference_role")

    def test_same_request_is_not_submitted_twice_and_changed_input_conflicts(self):
        first = self.submit()
        self.drain()
        second = self.submit()
        self.drain()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.gateway.submissions), 1)
        with self.assertRaises(DramaError) as conflict:
            self.submit(parameters={"prompt": "完全不同"})
        self.assertEqual(conflict.exception.status, 409)

    def test_project_image_review_queue_persists_quality_without_adopting_candidate(self):
        episode = self.service.create_episode(self.pid, {"title": "第一集", "requestKey": "episode"})["episode"]
        job = self.submit(key="queue")
        self.drain()
        candidate = self.service.reference_job(job["id"])["job"]["candidates"][0]
        queued = self.service.image_review_queue(self.pid, episode["id"], "episode")
        item = next(item for item in queued["items"] if item["candidateId"] == candidate["id"])
        self.assertEqual((item["reviewStatus"], item["scopeKind"], item["role"]), ("pending", "unlinked", "identity_front"))
        self.assertEqual(item["promptExcerpt"], "一个人物")
        self.assertFalse(item["selected"])
        body = {"candidateIds": [candidate["id"]], "decision": "approved", "requestKey": "review"}
        first = self.service.review_image_candidates(self.pid, body)
        second = self.service.review_image_candidates(self.pid, body)
        self.assertEqual(first, second)
        queued = self.service.image_review_queue(self.pid, episode["id"], "episode")
        item = next(item for item in queued["items"] if item["candidateId"] == candidate["id"])
        self.assertEqual((item["reviewStatus"], item["notes"], item["selected"]), ("approved", "", False))
        self.assertEqual(self.service.reference_job(job["id"])["job"]["candidates"][0]["assetId"], candidate["assetId"])

    def test_image_review_rejects_foreign_or_duplicate_candidates_atomically(self):
        job = self.submit(key="atomic")
        self.drain()
        candidate = self.service.reference_job(job["id"])["job"]["candidates"][0]
        with self.assertRaises(DramaError):
            self.service.review_image_candidates(self.pid, {"candidateIds": [candidate["id"], candidate["id"]], "decision": "approved", "requestKey": "duplicate"})
        other = self.service.create_project({"title": "其他", "requestKey": "other"})["project"]["id"]
        with self.assertRaises(DramaError):
            self.service.review_image_candidates(other, {"candidateIds": [candidate["id"]], "decision": "approved", "requestKey": "foreign"})
        with self.assertRaises(DramaError):
            self.service.review_image_candidates(self.pid, {"candidateIds": [candidate["id"]], "decision": ["approved"], "requestKey": "type"})
        self.assertEqual(self.service.image_review_queue(self.pid, None, "project")["items"][0]["reviewStatus"], "pending")

    def test_old_entity_candidate_is_not_unlinked_or_leaked_to_other_episode(self):
        first = self.service.create_episode(self.pid, {"title": "第一集", "requestKey": "first"})["episode"]
        second = self.service.create_episode(self.pid, {"title": "第二集", "requestKey": "second"})["episode"]
        job = self.submit(key="old-candidate")
        self.drain()
        candidate = self.service.reference_job(job["id"])["job"]["candidates"][0]
        self.service.revise_entity(self.entity["id"], {"expectedRevision": self.entity["revision"], "name": "新版角色", "requestKey": "new-entity"})
        current_entity = self.service.entity(self.entity["id"])["entity"]
        shot = self.service.create_shot(second["id"], {"title": "第二集镜头", "requestKey": "second-shot"})["shot"]
        with self.service.store.connect(write=True) as db:
            db.execute("INSERT INTO dr_dependencies VALUES (?,?,?)", (shot["currentRevisionId"], current_entity["currentRevisionId"], "character"))
        project_item = next(item for item in self.service.image_review_queue(self.pid, None, "project")["items"] if item["candidateId"] == candidate["id"])
        self.assertEqual((project_item["scopeKind"], project_item["episodeIds"], project_item["episodeTitles"]), ("episode", [second["id"]], ["第二集"]))
        self.assertFalse(any(item["candidateId"] == candidate["id"] for item in self.service.image_review_queue(self.pid, first["id"], "episode")["items"]))

    def test_lost_reply_recovers_by_original_key_without_another_post(self):
        self.gateway.fail_after_accept = True
        job = self.submit()
        self.drain()
        self.assertEqual(self.service.reference_job(job["id"])["job"]["state"], "SUBMISSION_UNKNOWN")
        self.service.close()
        self.service = DramaService(self.root / "state", self.root / "repo", reference_adapter=self.gateway)
        recovered = self.service.refresh_reference_job(job["id"])["job"]
        self.assertEqual(recovered["state"], "SUCCEEDED")
        self.assertEqual(len(self.gateway.submissions), 1)
        repeated = self.service.refresh_reference_job(job["id"])["job"]
        self.assertEqual([item["id"] for item in recovered["candidates"]], [item["id"] for item in repeated["candidates"]])
        with patch.object(self.service, "_archive_image", wraps=self.service._archive_image) as archive:
            self.service.refresh_reference_job(job["id"])
            archive.assert_not_called()

    def test_unknown_without_known_provider_task_never_retries(self):
        self.gateway.fail_before_accept = True
        job = self.submit()
        self.drain()
        for _ in range(3):
            self.assertEqual(self.service.refresh_reference_job(job["id"])["job"]["state"], "SUBMISSION_UNKNOWN")
        self.assertEqual(len(self.gateway.submissions), 1)

    def test_wrong_project_source_and_stale_target_are_rejected_before_post(self):
        other = self.service.create_project({"title": "另一项目", "requestKey": "other"})["project"]["id"]
        foreign = self.upload(pid=other)
        with self.assertRaises(DramaError):
            self.submit("edit_image", {"prompt": "编辑", "asset_ids": [foreign["id"]]})
        self.service.revise_entity(self.entity["id"], {"expectedRevision": self.entity["revision"], "name": "新版", "requestKey": "new"})
        with self.assertRaises(DramaError) as error:
            self.submit()
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.gateway.submissions, [])

    def test_finished_old_target_candidates_are_stale_and_never_auto_approved(self):
        self.gateway.initial_state = "RUNNING"
        self.gateway.output_bytes = []
        job = self.submit()
        self.drain()
        self.service.revise_entity(self.entity["id"], {"expectedRevision": self.entity["revision"], "name": "新版", "requestKey": "new"})
        gid = self.service.reference_job(job["id"])["job"]["gatewayJobId"]
        self.gateway.records[gid].update(state="SUCCEEDED", outputs=[{"sourceKey": "0", "filename": "result.png", "data": image_bytes()}])
        result = self.service.refresh_reference_job(job["id"])["job"]
        self.assertTrue(result["candidates"][0]["stale"])
        self.assertIsNone(self.service.entity(self.entity["id"])["entity"]["approvedRevisionId"])

    def test_bad_output_preserves_good_candidate_and_fails_job(self):
        source = self.upload()
        self.gateway.output_bytes = [image_bytes(), b"corrupt"]
        job = self.submit("edit_image", {"prompt": "编辑", "asset_ids": [source["id"]], "batch_count": 2})
        self.drain()
        result = self.service.reference_job(job["id"])["job"]
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(len(result["candidates"]), 1)

    def test_import_same_source_is_idempotent_and_cannot_change_project_or_target(self):
        job = self.submit()
        self.drain()
        result = self.service.reference_job(job["id"])["job"]
        body = {"targetType": "entity", "targetRevisionId": self.entity["currentRevisionId"],
                "gatewayJobId": result["gatewayJobId"], "tool": "generate_image", "requestKey": "import"}
        self.assertEqual(self.service.import_reference_job(self.pid, body)["job"]["id"], job["id"])
        other = self.service.create_project({"title": "另一项目", "requestKey": "other"})["project"]["id"]
        with self.assertRaises(DramaError):
            self.service.import_reference_job(other, body)
        entity2 = self.service.create_entity(self.pid, {"kind": "prop", "name": "道具", "requestKey": "prop"})["entity"]
        with self.assertRaises(DramaError):
            self.service.import_reference_job(self.pid, {**body, "targetRevisionId": entity2["currentRevisionId"], "requestKey": "import2"})

    def test_approved_identity_must_be_an_actual_ordered_input(self):
        reference = self.upload()
        other = self.upload(image_bytes("blue"))
        entity = self.service.revise_entity(self.entity["id"], {"name": "定稿", "referenceAssetId": reference["id"], "expectedRevision": 1, "requestKey": "rev"})["entity"]
        self.service.approve_entity(entity["id"], {"revisionId": entity["currentRevisionId"], "expectedRevision": entity["revision"], "requestKey": "approve"})
        with self.assertRaises(DramaError):
            self.submit("edit_image", {"prompt": "换服装", "asset_ids": [other["id"]]}, rid=entity["currentRevisionId"])
        self.assertEqual(self.gateway.submissions, [])

    def test_fixed_library_excludes_symlinks_and_unknown_folders(self):
        allowed = self.root / "repo/creative-projects/test/images"
        allowed.mkdir(parents=True)
        (allowed / "good.png").write_bytes(image_bytes())
        (allowed / "link.png").symlink_to(allowed / "good.png")
        secrets = allowed.parent / "private"
        secrets.mkdir()
        (secrets / "secret.png").write_bytes(image_bytes())
        listing = self.service.image_library()["sources"]
        self.assertEqual([row["title"] for row in listing], ["good.png"])
        asset = self.service.import_image_source(self.pid, {"sourceId": listing[0]["sourceId"], "requestKey": "library"})["asset"]
        self.assertEqual(asset["kind"], "image")

    def test_unconfigured_adapter_allows_upload_but_rejects_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            service = DramaService(Path(directory), self.root / "repo")
            try:
                project = service.create_project({"title": "离线", "requestKey": "p"})["project"]
                data = image_bytes()
                self.assertEqual(service.upload_image(project["id"], io.BytesIO(data), len(data), "x.png", "image")["asset"]["kind"], "image")
                with self.assertRaises(DramaError) as error:
                    service.submit_reference_job("entity", self.entity["currentRevisionId"], {"tool": "generate_image", "parameters": {"prompt": "x"}, "requestKey": "no"})
                self.assertEqual(error.exception.status, 503)
            finally:
                service.close()

    def test_cancellation_intent_is_not_posted_twice(self):
        self.gateway.initial_state = "RUNNING"
        self.gateway.output_bytes = []
        job = self.submit()
        self.drain()
        first = self.service.cancel_reference_job(job["id"])["job"]
        second = self.service.cancel_reference_job(job["id"])["job"]
        self.assertEqual(first["state"], "CANCEL_REQUESTED")
        self.assertEqual(second["state"], "CANCEL_REQUESTED")
        self.assertEqual(self.gateway.cancel_count, 1)

    def test_oversize_pixels_bytes_and_animated_images_are_rejected(self):
        for data in (b"x" * (8 * 1024 * 1024 + 1), image_bytes(size=(8193, 1))):
            with self.assertRaises(DramaError):
                decode_image(data)
        data = io.BytesIO()
        Image.new("RGB", (16, 16), "red").save(data, format="WEBP", save_all=True,
                                                append_images=[Image.new("RGB", (16, 16), "blue")])
        with self.assertRaises(DramaError):
            decode_image(data.getvalue())

    def test_restarting_prepared_intent_only_queries_and_does_not_submit(self):
        self.gateway.fail_before_accept = True
        job = self.submit()
        self.drain()
        with self.service.store.connect(write=True) as db:
            db.execute("UPDATE dr_reference_jobs SET state='PREPARED' WHERE id=?", (job["id"],))
        self.service.close()
        self.service = DramaService(self.root / "state", self.root / "repo", reference_adapter=self.gateway)
        self.drain()
        result = self.service.refresh_reference_job(job["id"])["job"]
        self.assertEqual(result["state"], "SUBMISSION_UNKNOWN")
        self.assertEqual(len(self.gateway.submissions), 1)

    def test_prepared_target_edited_before_dispatch_fails_without_provider_post(self):
        with self.hold_submission_executor():
            job = self.submit()
            self.assertEqual(job["state"], "PREPARED")
            self.service.revise_entity(self.entity["id"], {"expectedRevision": self.entity["revision"], "name": "排队时已修改", "requestKey": "edit-queued"})
        self.drain()
        result = self.service.reference_job(job["id"])["job"]
        self.assertEqual(result["state"], "FAILED")
        self.assertIn("更新", result["error"])
        self.assertEqual(self.gateway.submissions, [])

    def test_approval_changed_with_same_current_revision_blocks_queued_dispatch(self):
        first, second = self.upload(), self.upload(image_bytes("blue"))
        rev1 = self.service.revise_entity(self.entity["id"], {"expectedRevision": 1, "name": "参考A", "referenceAssetId": first["id"], "requestKey": "ref-a"})["entity"]
        rev2 = self.service.revise_entity(self.entity["id"], {"expectedRevision": rev1["revision"], "name": "参考B", "referenceAssetId": second["id"], "requestKey": "ref-b"})["entity"]
        approved = self.service.approve_entity(self.entity["id"], {"expectedRevision": rev2["revision"], "revisionId": rev1["currentRevisionId"], "requestKey": "approve-a"})["entity"]
        with self.hold_submission_executor():
            job = self.submit(rid=rev2["currentRevisionId"])
            after = self.service.approve_entity(self.entity["id"], {"expectedRevision": approved["revision"], "revisionId": rev2["currentRevisionId"], "requestKey": "approve-b"})["entity"]
            self.assertEqual(after["currentRevisionId"], rev2["currentRevisionId"])
        self.drain()
        result = self.service.reference_job(job["id"])["job"]
        self.assertEqual(result["state"], "FAILED")
        self.assertIn("批准参考", result["error"])
        self.assertEqual(self.gateway.submissions, [])

    def test_real_adapter_partial_artifact_failure_can_be_recollected_without_post(self):
        gateway_root = self.root / "real-gateway-state"
        adapter = GatewayReferenceAdapter(state_dir=gateway_root)
        self.service._reference_adapter = adapter
        for broken_kind in ("missing", "oversize"):
            with self.subTest(broken_kind=broken_kind):
                gid = new_id()
                archive = gateway_root / "creative-artifacts" / gid
                archive.mkdir(parents=True)
                (archive / "good.png").write_bytes(image_bytes())
                if broken_kind == "oversize":
                    (archive / "later.png").write_bytes(b"x" * (8 * 1024 * 1024 + 1))
                record = {"job_id": gid, "state": "SUCCEEDED", "artifacts": [
                    {"url": f"http://192.0.2.37:18082/api/v1/artifacts/{gid}/{name}"}
                    for name in ("good.png", "later.png")]}
                with patch.object(adapter, "_call", return_value=record) as rpc:
                    imported = self.service.import_reference_job(self.pid, {
                        "targetType": "entity", "targetRevisionId": self.entity["currentRevisionId"],
                        "gatewayJobId": gid, "tool": "edit_image", "requestKey": broken_kind})["job"]
                    self.assertEqual(imported["state"], "FAILED")
                    self.assertEqual(len(imported["candidates"]), 1)
                    original = imported["candidates"][0]["id"]
                    (archive / "later.png").write_bytes(image_bytes("blue"))
                    completed = self.service.refresh_reference_job(imported["id"])["job"]
                    self.assertEqual(completed["state"], "SUCCEEDED")
                    self.assertEqual(len(completed["candidates"]), 2)
                    self.assertIn(original, [item["id"] for item in completed["candidates"]])
                    self.assertTrue(all(call.args[0] == "comfy_job" for call in rpc.call_args_list))
                self.assertEqual(self.gateway.submissions, [])

    def test_required_identity_inputs_apply_to_both_edit_and_composition(self):
        aid = "a" * 64
        context = {"requiredReferenceAssetIds": [aid], "approvedReferenceAssetId": None}
        for tool in ("edit_image", "compose_image_layers"):
            with self.subTest(tool=tool), self.assertRaises(DramaError):
                self.service._require_identity_inputs(context, tool, ["b" * 64])
            self.service._require_identity_inputs(context, tool, [aid])
        self.service._require_identity_inputs(context, "generate_image", [])

    def test_target_edit_during_real_adapter_registration_blocks_generation_post(self):
        asset = self.upload()
        adapter = GatewayReferenceAdapter(state_dir=self.root / "gateway-state")
        self.service._reference_adapter = adapter
        calls = []
        def rpc(name, arguments):
            calls.append(name)
            if name != "comfy_register_image":
                raise AssertionError("Stale target reached a generation POST")
            self.service.revise_entity(self.entity["id"], {
                "expectedRevision": 1, "name": "上传期间修改", "requestKey": "during-registration"})
            return {"asset_id": asset["id"]}
        with patch.object(adapter, "_call", side_effect=rpc):
            job = self.submit("edit_image", {"prompt": "编辑", "asset_ids": [asset["id"]]})
            self.drain()
        self.assertEqual(self.service.reference_job(job["id"])["job"]["state"], "FAILED")
        self.assertEqual(calls, ["comfy_register_image"])


class GatewayAdapterTests(unittest.TestCase):
    def test_schema_rejects_unknown_fields_nested_paths_and_invalid_batches(self):
        for parameters in ({"prompt": "x", "url": "http://example.test"}, {"prompt": "x", "idempotency_key": "override"},
                           {"prompt": "x", "width": True}, {"prompt": "x", "seed": -1}):
            with self.assertRaises(ValueError):
                validate_parameters("generate_image", parameters)
        with self.assertRaises(ValueError):
            validate_parameters("edit_image", {"prompt": "x", "asset_ids": ["a" * 64], "batch_count": 0})
        # The fixed eight-candidate cap was removed in the 2026-09-10 contract.
        self.assertEqual(validate_parameters("edit_image", {
            "prompt": "x", "asset_ids": ["a" * 64], "batch_count": 9})["batch_count"], 9)

    def test_artifact_origin_and_path_are_fixed(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = GatewayReferenceAdapter(state_dir=Path(directory))
            jid = "a" * 32
            for url in (f"http://example.test/api/v1/artifacts/{jid}/out.png", f"http://127.0.0.1:18082/api/v1/artifacts/{jid}/%2e%2e%2fsecret.png"):
                with self.assertRaises(ValueError):
                    adapter.outputs("generate_image", {"job_id": jid, "artifacts": [{"url": url}]})

    def test_adapter_registers_actual_ordered_bytes_then_calls_existing_tool(self):
        import base64
        import hashlib
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as directory:
            calls = []
            images = [{"assetId": hashlib.sha256(data).hexdigest(), "filename": f"reference-{index}.png", "data": data}
                      for index, data in enumerate((image_bytes("blue"), image_bytes("red")))]
            app = SimpleNamespace(config=SimpleNamespace(state_dir=Path(directory)))
            adapter = GatewayReferenceAdapter(app=app)

            def dispatch(_app, request):
                params = request["params"]
                calls.append(params)
                if params["name"] == "comfy_register_image":
                    aid = hashlib.sha256(base64.b64decode(params["arguments"]["image_base64"])).hexdigest()
                    result = {"asset_id": aid}
                else:
                    result = {"job_id": "b" * 32, "state": "QUEUED"}
                return {"result": {"isError": False, "structuredContent": result}}

            with patch("DGXGateway.dgx_gateway.drama.gateway_adapter.mcp.dispatch", side_effect=dispatch):
                adapter.submit("edit_image", {"prompt": "编辑", "asset_ids": [image["assetId"] for image in images]}, "drama-request", images)
            self.assertEqual([call["name"] for call in calls], ["comfy_register_image", "comfy_register_image", "edit_image"])
            self.assertEqual(calls[-1]["arguments"]["asset_ids"], [image["assetId"] for image in images])
            self.assertEqual(calls[-1]["arguments"]["idempotency_key"], "drama-request")

    def test_adapter_find_is_read_only_and_outputs_read_only_known_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = GatewayReferenceAdapter(state_dir=root)
            jid = "a" * 32
            (root / "creative-jobs").mkdir()
            (root / "creative-jobs" / (jid + ".json")).write_text(json.dumps({"job_id": jid, "idempotency_key": "saved"}))
            with patch.object(adapter, "_call", return_value={"job_id": jid, "state": "RUNNING"}) as call:
                result = adapter.find("generate_image", "saved")
                self.assertEqual(result["job_id"], jid)
                call.assert_called_once_with("comfy_job", {"job_id": jid})
            archive = root / "creative-artifacts" / jid
            archive.mkdir(parents=True)
            data = image_bytes()
            (archive / "image.png").write_bytes(data)
            record = {"job_id": jid, "artifacts": [{"path": "/untrusted/secret", "url": f"http://192.0.2.37:18082/api/v1/artifacts/{jid}/image.png"}]}
            self.assertEqual(adapter.outputs("generate_image", record)[0]["data"], data)


if __name__ == "__main__":
    unittest.main()
