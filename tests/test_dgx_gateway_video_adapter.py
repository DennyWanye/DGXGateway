"""P3 adapter only exercises local fixtures and mocked JSON-RPC dispatch."""
import base64
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from DGXGateway.dgx_gateway import mcp
from DGXGateway.dgx_gateway.drama.gateway_adapter import GatewayRejected
from DGXGateway.dgx_gateway.drama.gateway_video_adapter import GatewayBusy, GatewayVideoAdapter
from tests.test_dgx_video_safety import pcm


class VideoGatewayAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.adapter = GatewayVideoAdapter(state_dir=self.root)
        data = io.BytesIO()
        Image.new("RGB", (32, 24), "red").save(data, format="PNG")
        self.images = [{"assetId": hashlib.sha256(data.getvalue()).hexdigest(), "filename": "reference.png", "data": data.getvalue()}]
        audio = pcm()
        self.audios = [{"assetId": hashlib.sha256(audio).hexdigest(), "filename": "reference.wav", "data": audio}]
        self.shots = [{"name": "first", "prompt": "a stable shot", "seed": 42,
                       "image_asset_id": self.images[0]["assetId"], "audio_asset_id": self.audios[0]["assetId"]}]

    def test_registration_gate_and_non_preemptive_submission_order(self):
        calls = []
        def rpc(name, arguments):
            calls.append((name, arguments))
            if name in {"comfy_register_image", "video_register_audio"}:
                field = "image_base64" if name == "comfy_register_image" else "audio_base64"
                return {"asset_id": hashlib.sha256(base64.b64decode(arguments[field])).hexdigest()}
            return {"job_id": "a" * 32, "state": "QUEUED", "non_preemptive": arguments["non_preemptive"]}
        with patch.object(self.adapter, "_call", side_effect=rpc):
            result = self.adapter.submit(self.shots, "video-key", self.images, self.audios,
                                         lambda: calls.append(("business-gate", {})))
        self.assertEqual([name for name, _ in calls], ["comfy_register_image", "video_register_audio", "business-gate", "video_batch_submit"])
        self.assertTrue(calls[-1][1]["non_preemptive"])
        self.assertTrue(result["non_preemptive"])

    def test_pre_submit_gate_failure_does_not_generate(self):
        def rpc(name, _args):
            return {"asset_id": self.images[0]["assetId"] if name == "comfy_register_image" else self.audios[0]["assetId"]}
        with patch.object(self.adapter, "_call", side_effect=rpc) as call:
            with self.assertRaises(ValueError):
                self.adapter.submit(self.shots, "video-key", self.images, self.audios, lambda: (_ for _ in ()).throw(ValueError("stale")))
        self.assertEqual([item.args[0] for item in call.call_args_list], ["comfy_register_image", "video_register_audio"])

    def test_registration_timeout_is_known_not_submitted(self):
        def rpc(name, _arguments):
            if name == "comfy_register_image":
                return {"asset_id": self.images[0]["assetId"]}
            raise TimeoutError("lost audio registration response")
        gate = Mock()
        with patch.object(self.adapter, "_call", side_effect=rpc) as call, self.assertRaises(GatewayRejected) as error:
            self.adapter.submit(self.shots, "video-key", self.images, self.audios, gate)
        self.assertIn("no video request", str(error.exception))
        self.assertIn("lost audio registration response", str(error.exception))
        self.assertEqual([item.args[0] for item in call.call_args_list], ["comfy_register_image", "video_register_audio"])
        gate.assert_not_called()

    def test_invalid_or_truncated_audio_rejected_before_registration(self):
        data = pcm()[:-2]
        bad = [{"assetId": hashlib.sha256(data).hexdigest(), "filename": "audio.wav", "data": data}]
        shots = [{**self.shots[0], "audio_asset_id": bad[0]["assetId"]}]
        with patch.object(self.adapter, "_call") as call, self.assertRaises(GatewayRejected):
            self.adapter.submit(shots, "video-key", self.images, bad, lambda: None)
        call.assert_not_called()

    def test_find_calls_only_video_job_find(self):
        record = {"job_id": "a" * 32, "state": "RUNNING"}
        with patch.object(self.adapter, "_call", return_value={"job": record}) as call:
            self.assertEqual(self.adapter.find("original-key"), record)
        call.assert_called_once_with("video_job_find", {"idempotency_key": "original-key"})

    def test_typed_busy_and_unknown_response_are_distinct(self):
        app = SimpleNamespace(config=SimpleNamespace(state_dir=self.root))
        adapter = GatewayVideoAdapter(app=app)
        reply = {"result": {"isError": True, "structuredContent": {"error": {"code": "DGX_BUSY"}}}}
        with patch("DGXGateway.dgx_gateway.drama.gateway_video_adapter.mcp.dispatch", return_value=reply), self.assertRaises(GatewayBusy):
            adapter._call("video_batch_submit", {})
        with patch("DGXGateway.dgx_gateway.drama.gateway_video_adapter.mcp.dispatch", return_value={"result": {"isError": True}}), self.assertRaises(RuntimeError):
            adapter._call("video_batch_submit", {})

    def test_outputs_preserve_partial_batch_and_ignore_arbitrary_artifact_paths(self):
        jid = "a" * 32
        directory = self.root / "creative-artifacts" / jid
        directory.mkdir(parents=True)
        (directory / "000-first.mp4").write_bytes(b"owned-video-bytes")
        row = {"job_id": jid, "state": "FAILED", "shots": [
            {"index": 0, "name": "first", "submission_phase": "RECEIVED", "artifact": {"path": "/private/secret"}},
            {"index": 1, "name": "missing", "submission_phase": "UNKNOWN"},
            {"index": 2, "name": "queued", "submission_phase": "NOT_SUBMITTED"}]}
        output = list(self.adapter.outputs(row))
        self.assertEqual(len(output), 2)
        self.assertEqual(output[0]["data"], b"owned-video-bytes")
        self.assertEqual(output[1]["error"], "artifact_unavailable")
        (directory / "001-missing.mp4").symlink_to(directory / "000-first.mp4")
        self.assertEqual(list(self.adapter.outputs(row))[1]["error"], "artifact_unavailable")

    def test_inflight_without_artifact_does_not_read_or_report_missing_output(self):
        record = {"job_id": "a" * 32, "state": "RUNNING", "shots": [
            {"index": 0, "name": "first", "state": "RUNNING", "submission_phase": "REQUEST_STARTED"}]}
        with patch("DGXGateway.dgx_gateway.drama.gateway_video_adapter._read_regular", side_effect=FileNotFoundError) as read:
            self.assertEqual(list(self.adapter.outputs(record)), [])
        read.assert_not_called()

    def test_completed_or_ambiguous_output_still_recovers_fixed_published_file(self):
        jid = "a" * 32
        directory = self.root / "creative-artifacts" / jid
        directory.mkdir(parents=True)
        (directory / "000-first.mp4").write_bytes(b"already-published-video")
        cases = [
            ("RUNNING", "RUNNING", "REQUEST_STARTED", {"sha256": "published"}),
            ("RUNNING", "RUNNING", "RECEIVED", None),
            ("SUBMISSION_UNKNOWN", "SUBMISSION_UNKNOWN", "UNKNOWN", None),
            ("INTERRUPTED", "RUNNING", "REQUEST_STARTED", None),
            ("SUCCEEDED", "SUCCEEDED", "RECEIVED", None),
        ]
        for batch_state, shot_state, phase, artifact in cases:
            with self.subTest(batch_state=batch_state, phase=phase, artifact=artifact):
                record = {"job_id": jid, "state": batch_state, "shots": [{"index": 0, "name": "first",
                    "state": shot_state, "submission_phase": phase, "artifact": artifact}]}
                output = list(self.adapter.outputs(record))
                self.assertEqual(len(output), 1)
                self.assertEqual(output[0]["data"], b"already-published-video")
                self.assertEqual(output[0]["sourceKey"], jid + "/000-first.mp4")

    def test_output_path_traversal_is_rejected(self):
        with self.assertRaises(ValueError):
            list(self.adapter.outputs({"job_id": "a" * 32, "shots": [{"index": 0, "name": "../secret"}]}))

    def test_mcp_find_is_read_only_and_submission_busy_has_typed_error(self):
        jobs = SimpleNamespace(find=Mock(return_value=None), submit=Mock(side_effect=BlockingIOError("busy")))
        app = SimpleNamespace(video_jobs=jobs)
        find = mcp.dispatch(app, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "video_job_find", "arguments": {"idempotency_key": "original-key"}}})
        self.assertEqual(find["result"]["structuredContent"], {"job": None})
        jobs.submit.assert_not_called()
        busy = mcp.dispatch(app, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "video_batch_submit", "arguments": {"shots": self.shots, "idempotency_key": "original-key", "non_preemptive": True}}})
        self.assertEqual(busy["result"]["structuredContent"]["error"]["code"], "DGX_BUSY")
        self.assertTrue(jobs.submit.call_args.kwargs["non_preemptive"])


if __name__ == "__main__":
    unittest.main()
