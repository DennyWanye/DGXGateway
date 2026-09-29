import base64
import io
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from DGXGateway.dgx_gateway.audio_assets import AudioAssets
from DGXGateway.dgx_gateway.image_assets import ImageAssets
from DGXGateway.dgx_gateway.video_jobs import VideoJobs
from DGXGateway.dgx_gateway.video_reference_assets import VideoReferenceAssets
from DGXGateway.dgx_gateway.job_cancel import JobCanceled


def wav_bytes(frames=96000):
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(b"\0\0" * frames)
    return output.getvalue()


class AudioAssetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.assets = AudioAssets(Path(self.tmp.name))

    def test_register_and_verify_exact_contract(self):
        data = wav_bytes()
        result = self.assets.register_base64("silence.wav", base64.b64encode(data).decode())
        self.assertEqual(result["duration_seconds"], 4.0)
        self.assertEqual(result["sample_rate"], 24000)
        self.assertEqual(Path(result["path"]).read_bytes(), data)

    def test_wrong_duration_rejected(self):
        with self.assertRaisesRegex(ValueError, "exactly four-second"):
            self.assets.register_base64("short.wav", base64.b64encode(wav_bytes(24000)).decode())


class VideoReferenceAssetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.assets = VideoReferenceAssets(self.root / "refs")

    def make_mp4(self, seconds):
        path = self.root / f"{seconds}.mp4"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                        f"color=c=black:s=64x64:r=24:d={seconds}", "-an", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-y", str(path)], check=True)
        return path.read_bytes()

    def test_registers_content_addressed_two_to_fifteen_second_mp4(self):
        data = self.make_mp4(2)
        saved = self.assets.register_bytes("motion.mp4", data)
        self.assertEqual(saved["asset_id"], saved["sha256"])
        self.assertGreaterEqual(saved["duration_seconds"], 2)
        self.assertEqual(Path(saved["path"]).read_bytes(), data)

    def test_rejects_video_shorter_than_two_seconds(self):
        with self.assertRaisesRegex(ValueError, "2–15 second"):
            self.assets.register_bytes("short.mp4", self.make_mp4(1))


class VideoJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        database = sqlite3.connect(root / "gateway.sqlite3")
        database.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        database.commit()
        database.close()
        images = ImageAssets(root / "creative-assets")
        self.image = images.register_base64("x.png", base64.b64encode(b"\x89PNG\r\n\x1a\nbody").decode())
        self.app = SimpleNamespace(
            config=SimpleNamespace(
                state_dir=root,
                repo_root=root,
                nodes=[SimpleNamespace(address="127.0.0.1")],
                bind="127.0.0.1",
                port=18082,
            ),
            _submit_lock=threading.Lock(),
            creative_jobs=SimpleNamespace(active=None, assets=images),
            store=SimpleNamespace(active_operation=lambda: None),
        )
        self.jobs = VideoJobs(self.app)
        self.app.video_jobs = self.jobs
        self.audio = self.jobs.audio.register_base64("x.wav", base64.b64encode(wav_bytes()).decode())
        self.shot = {
            "name": "WH-01",
            "prompt": "Subtle breathing in a stable forest shot.",
            "image_asset_id": self.image["asset_id"],
            "audio_asset_id": self.audio["asset_id"],
            "seed": 42,
        }

    def test_diagnostic_admission_idempotency_and_budget_cancel(self):
        diagnostic = {"trace_step": 3, "max_steps": 6, "max_seconds": 3000}
        with patch("threading.Thread"):
            first = self.jobs.submit([self.shot], "diagnostic-key-one", diagnostic=diagnostic)
            same = self.jobs.submit([self.shot], "diagnostic-key-one", diagnostic=diagnostic)
            self.assertEqual(first["job_id"], same["job_id"])
            with self.assertRaisesRegex(ValueError, "different diagnostic"):
                self.jobs.submit([self.shot], "diagnostic-key-one", diagnostic={**diagnostic, "trace_step": -1})
        row = self.jobs.get(first["job_id"])
        row["diagnostic_started_at"] = 1
        transport = Mock()
        transport.diagnostic_progress.return_value = {}
        self.jobs._stop_ring4_after_cancel = Mock()
        with self.assertRaises(JobCanceled):
            self.jobs._monitor_ring4(row, row["shots"][0], transport, "remote-id")
        self.assertEqual(row["diagnostic_stop_reason"], "time_budget")
        self.assertTrue(self.jobs.cancel_requested(row["job_id"]))
        self.jobs._stop_ring4_after_cancel.assert_called_once()
        transport.submit.assert_not_called()
        transport.download.assert_not_called()

    def test_submit_is_persistent_idempotent_and_declares_serial(self):
        with patch("threading.Thread") as worker:
            first = self.jobs.submit([self.shot], "video-test-key")
            second = self.jobs.submit([self.shot], "video-test-key")
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(first["execution_mode"], "ring4_four_node")
        worker.assert_called_once()

    def test_batch_loads_once_and_releases_once(self):
        row = {
            "job_id": "a" * 32,
            "idempotency_key": "video-test-key",
            "profile": "heretic-ref2va-v1",
            "state": "QUEUED",
            "memory_released": False,
            "shots": [{**self.shot, "state": "QUEUED", "index": 0}, {**self.shot, "name": "WH-02", "state": "QUEUED", "index": 1}],
        }
        self.jobs.active = row["job_id"]
        self.jobs.switch = Mock()
        self.jobs._generate = Mock(side_effect=[{"path": "a.mp4"}, {"path": "b.mp4"}])
        self.jobs.run(row)
        self.assertEqual(row["state"], "SUCCEEDED")
        self.assertTrue(row["memory_released"])
        self.assertEqual([call.args[1] for call in self.jobs.switch.call_args_list], ["h3-ring4"])
        self.assertTrue(all(shot["state"] == "SUCCEEDED" for shot in row["shots"]))

    def test_failed_clip_attempts_release_and_does_not_continue(self):
        row = {
            "job_id": "b" * 32,
            "idempotency_key": "video-test-key",
            "profile": "heretic-ref2va-v1",
            "state": "QUEUED",
            "memory_released": False,
            "shots": [{**self.shot, "state": "QUEUED", "index": 0}, {**self.shot, "name": "WH-02", "state": "QUEUED", "index": 1}],
        }
        self.jobs.active = row["job_id"]
        self.jobs.switch = Mock()
        self.jobs._generate = Mock(side_effect=RuntimeError("failed"))
        self.jobs.run(row)
        self.assertEqual(row["state"], "FAILED")
        self.assertTrue(row["memory_released"])
        self.assertEqual(self.jobs._generate.call_count, 1)
        self.assertEqual(row["shots"][0]["state"], "FAILED")
        self.assertEqual(row["shots"][1]["state"], "QUEUED")
        self.assertEqual([call.args[1] for call in self.jobs.switch.call_args_list], ["h3-ring4"])

    def test_cancel_during_clip_preserves_it_and_skips_next(self):
        with patch("threading.Thread"):
            submitted = self.jobs.submit([self.shot, {**self.shot, "name": "WH-02"}], "cancel-video-key")
        row = self.jobs.get(submitted["job_id"])
        self.jobs.switch = Mock()
        def generate(row, shot):
            self.jobs.cancel(row["job_id"])
            return {"path": "completed.mp4"}
        self.jobs._generate = Mock(side_effect=generate)
        self.jobs.run(row)
        self.assertEqual(self.jobs._generate.call_count, 1)
        self.assertEqual(row["state"], "CANCELED")
        self.assertTrue(row["memory_released"])
        self.assertEqual(row["shots"][0]["state"], "SUCCEEDED")
        self.assertEqual(row["shots"][1]["state"], "CANCELED")
        self.assertEqual(row["shots"][0]["artifact"]["path"], "completed.mp4")
        self.assertIsNone(self.jobs.active)

    def test_invalid_or_changed_batch_rejected(self):
        with self.assertRaises(ValueError):
            self.jobs.submit([{**self.shot, "extra": True}], "video-test-key")
        with self.assertRaises(ValueError):
            self.jobs.submit([{**self.shot, "name": "../escape"}], "video-unsafe-name")
        with self.assertRaises(ValueError):
            self.jobs.submit([self.shot, self.shot], "video-duplicate-name")
        with patch("threading.Thread"):
            self.jobs.submit([self.shot], "video-test-key")
            with self.assertRaises(ValueError):
                self.jobs.submit([{**self.shot, "seed": 43}], "video-test-key")

    def test_runtime_target_maps_new_workbench_ids_to_ring4(self):
        from DGXGateway.dgx_gateway.video_contracts import runtime_target, STABLE_PROFILE, TEXT_PROFILE, LONG_PROFILE, RING4_MODEL
        self.assertEqual(runtime_target(STABLE_PROFILE), (RING4_MODEL, "ref2va-official", "ref2va"))
        self.assertEqual(runtime_target(TEXT_PROFILE), (RING4_MODEL, "fl2va-official", "t2va"))
        self.assertEqual(runtime_target(LONG_PROFILE), (RING4_MODEL, "ref2va-heretic", "ref2va"))

    def test_ring4_reconcile_queries_original_id_and_never_resubmits(self):
        from DGXGateway.dgx_gateway.video_contracts import LONG_PROFILE
        row = {
            "job_id": "c" * 32,
            "idempotency_key": "ring4-original-key",
            "profile": LONG_PROFILE,
            "state": "SUBMISSION_UNKNOWN",
            "memory_released": False,
            "cleanup_required": False,
            "shots": [{**self.shot, "state": "SUBMISSION_UNKNOWN", "index": 0,
                       "request_id": "d" * 32, "submission_phase": "UNKNOWN",
                       "ring_job_id": "ring-job-original", "request_duration_seconds": 10,
                       "contract_version": "h3-ref2va-v1"}],
        }
        self.jobs.save(row)
        self.jobs.active = row["job_id"]
        self.jobs.ring_transport = SimpleNamespace(
            poll=Mock(return_value={"status": "completed"}),
            download=Mock(return_value=b"fixture-mp4"),
            submit=Mock(),
        )
        self.jobs._publish_result = Mock(return_value={"path": "original.mp4"})
        result = self.jobs.reconcile(row["job_id"])
        self.assertEqual(result["state"], "SUCCEEDED")
        self.jobs.ring_transport.poll.assert_called_once_with("ring-job-original")
        self.jobs.ring_transport.download.assert_called_once_with("ring-job-original")
        self.jobs.ring_transport.submit.assert_not_called()

    def test_ring4_reconcile_keeps_running_original_without_resubmit(self):
        from DGXGateway.dgx_gateway.video_contracts import LONG_PROFILE
        row = {
            "job_id": "e" * 32,
            "idempotency_key": "ring4-running-key",
            "profile": LONG_PROFILE,
            "state": "SUBMISSION_UNKNOWN",
            "memory_released": False,
            "cleanup_required": False,
            "shots": [{**self.shot, "state": "SUBMISSION_UNKNOWN", "index": 0,
                       "request_id": "f" * 32, "submission_phase": "UNKNOWN",
                       "ring_job_id": "ring-job-running", "request_duration_seconds": 15,
                       "contract_version": "h3-ref2va-v1"}],
        }
        self.jobs.save(row)
        self.jobs.active = row["job_id"]
        self.jobs.ring_transport = SimpleNamespace(
            poll=Mock(return_value={"status": "running"}), submit=Mock()
        )
        with patch("threading.Thread") as worker:
            result = self.jobs.reconcile(row["job_id"])
            self.assertEqual(result["state"], "RUNNING")
            self.assertEqual(result["shots"][0]["submission_phase"], "RECEIVED")
            worker.return_value.start.assert_called_once()
        with self.assertRaises(BlockingIOError):
            self.jobs.reconcile(row["job_id"])
        self.jobs.ring_transport.poll.side_effect = [RuntimeError("temporary query failure"), {"status": "completed"}]
        self.jobs.ring_transport.download = Mock(return_value=b"fixture-mp4")
        self.jobs._publish_result = Mock(return_value={"path": "original.mp4"})
        with patch("DGXGateway.dgx_gateway.video_jobs.time.sleep"):
            self.jobs._resume_ring4_monitor(self.jobs.get(row["job_id"]))
        result = self.jobs.get(row["job_id"])
        self.assertEqual(result["state"], "SUCCEEDED")
        self.assertIsNone(self.jobs.active)
        self.assertEqual(result["shots"][0]["ring_poll_failures"], 0)
        self.assertTrue(all(c.args == ("ring-job-running",) for c in self.jobs.ring_transport.poll.call_args_list))
        self.jobs.ring_transport.submit.assert_not_called()

    def test_restart_preserves_received_ring_identity_without_submission(self):
        from DGXGateway.dgx_gateway.video_contracts import LONG_PROFILE
        row = {"job_id": "d" * 32, "profile": LONG_PROFILE, "state": "RUNNING",
               "shots": [{"state": "RUNNING", "submission_phase": "RECEIVED",
                          "ring_job_id": "existing-ring-id"}]}
        self.jobs.save(row)
        with patch("threading.Thread") as worker:
            restarted = VideoJobs(self.app)
            worker.assert_not_called()
        saved = restarted.get(row["job_id"])
        self.assertEqual(saved["state"], "SUBMISSION_UNKNOWN")
        self.assertEqual(saved["shots"][0]["submission_phase"], "UNKNOWN")
        self.assertEqual(saved["shots"][0]["ring_job_id"], "existing-ring-id")

    def test_ring4_recovery_repeated_query_failure_preserves_original(self):
        from DGXGateway.dgx_gateway.video_contracts import LONG_PROFILE
        row = {"job_id": "e" * 32, "profile": LONG_PROFILE, "state": "RUNNING", "shots": [{
            "index": 0, "state": "RUNNING", "submission_phase": "RECEIVED",
            "ring_job_id": "original-job-123", "request_duration_seconds": 8}]}
        self.jobs.save(row)
        self.jobs.active = row["job_id"]
        self.jobs._running.add(row["job_id"])
        self.jobs.ring_transport = SimpleNamespace(poll=Mock(side_effect=RuntimeError("offline")), submit=Mock())
        with patch("DGXGateway.dgx_gateway.video_jobs.time.sleep"):
            self.jobs._resume_ring4_monitor(row)
        result = self.jobs.get(row["job_id"])
        self.assertEqual(result["state"], "SUBMISSION_UNKNOWN")
        self.assertEqual(result["shots"][0]["submission_phase"], "UNKNOWN")
        self.assertEqual(result["shots"][0]["ring_job_id"], "original-job-123")
        self.assertEqual(self.jobs.ring_transport.poll.call_count, 5)
        self.assertEqual(self.jobs.active, row["job_id"])
        self.assertNotIn(row["job_id"], self.jobs._running)
        self.jobs.ring_transport.submit.assert_not_called()

    def test_ring4_cancel_deletes_remote_job_and_stops_owned_runtime(self):
        from DGXGateway.dgx_gateway.video_contracts import TEXT_PROFILE

        row = {
            "job_id": "1" * 32,
            "idempotency_key": "ring4-cancel-key",
            "profile": TEXT_PROFILE,
            "state": "RUNNING",
            "memory_released": False,
            "cleanup_required": False,
            "shots": [{"name": "TXT-01", "prompt": "A stable shot", "seed": 7,
                        "state": "RUNNING", "index": 0,
                        "request_duration_seconds": 4,
                        "submission_phase": "NOT_SUBMITTED"}],
        }
        self.jobs.save(row)
        transport = SimpleNamespace(
            stage=Mock(),
            payload=Mock(return_value={}),
            submit=Mock(return_value="ring-job-cancel"),
            poll=Mock(side_effect=lambda _job: (self.jobs.root / (row["job_id"] + ".cancel")).touch() or {"status": "running"}),
            cancel=Mock(return_value={"id": "ring-job-cancel", "status": "deleted"}),
        )
        self.jobs.ring_transport = transport
        self.jobs.switch = Mock()

        with self.assertRaises(JobCanceled):
            self.jobs._generate_ring4(row, row["shots"][0])

        transport.cancel.assert_called_once_with("ring-job-cancel")
        self.jobs.switch.assert_called_once()
        stopped_row, target, suffix = self.jobs.switch.call_args.args
        self.assertEqual((stopped_row["job_id"], target, suffix), (row["job_id"], None, "cancel_stop"))
        self.assertTrue(row["ring4_cancel_stopped"])

    def test_ring4_cancel_recovers_unknown_batch_without_worker(self):
        from DGXGateway.dgx_gateway.video_contracts import LONG_PROFILE

        row = {
            "job_id": "2" * 32,
            "idempotency_key": "ring4-unknown-cancel-key",
            "profile": LONG_PROFILE,
            "state": "SUBMISSION_UNKNOWN",
            "memory_released": False,
            "cleanup_required": False,
            "shots": [{"name": "LONG-01", "prompt": "A stable shot", "seed": 8,
                        "state": "SUBMISSION_UNKNOWN", "index": 0,
                        "request_duration_seconds": 15,
                        "submission_phase": "UNKNOWN",
                        "ring_job_id": "ring-job-unknown"}],
        }
        self.jobs.save(row)
        self.jobs.active = row["job_id"]
        self.jobs.ring_transport = SimpleNamespace(
            cancel=Mock(return_value={"id": "ring-job-unknown", "status": "deleted"}),
        )
        self.jobs.switch = Mock()

        result = self.jobs.cancel(row["job_id"])

        self.jobs.ring_transport.cancel.assert_called_once_with("ring-job-unknown")
        self.assertEqual(self.jobs.switch.call_count, 1)
        self.assertEqual(self.jobs.switch.call_args.args[0]["job_id"], row["job_id"])
        self.assertEqual(self.jobs.switch.call_args.args[1:], (None, "cancel_stop"))
        self.assertEqual(result["state"], "CANCELED")
        self.assertTrue(result["memory_released"])
        self.assertTrue(result["resource_settled"])

    def test_ring4_cancel_settles_unknown_batch_when_listener_is_down(self):
        from DGXGateway.dgx_gateway.video_contracts import LONG_PROFILE

        row = {
            "job_id": "3" * 32,
            "idempotency_key": "ring4-refused-cancel-key",
            "profile": LONG_PROFILE,
            "state": "SUBMISSION_UNKNOWN",
            "memory_released": False,
            "cleanup_required": False,
            "shots": [{"name": "LONG-01", "prompt": "A stable shot", "seed": 8,
                        "state": "SUBMISSION_UNKNOWN", "index": 0,
                        "request_duration_seconds": 15,
                        "submission_phase": "UNKNOWN",
                        "ring_job_id": "ring-job-refused"}],
        }
        self.jobs.save(row)
        self.jobs.active = row["job_id"]
        self.jobs.ring_transport = SimpleNamespace(
            cancel=Mock(return_value={"status": "not_found", "reason": "connection_refused"}),
        )
        self.jobs.switch = Mock()

        result = self.jobs.cancel(row["job_id"])

        self.jobs.ring_transport.cancel.assert_called_once_with("ring-job-refused")
        self.assertEqual(self.jobs.switch.call_args.args[1:], (None, "cancel_stop"))
        self.assertEqual(result["state"], "CANCELED")
        self.assertTrue(result["memory_released"])
        self.assertTrue(result["resource_settled"])
        self.assertFalse(result["cleanup_required"])

    def test_ring4_cancel_skips_stop_when_all_models_already_stopped(self):
        from DGXGateway.dgx_gateway.domain import ObservedModel
        from DGXGateway.dgx_gateway.video_contracts import LONG_PROFILE

        row = {
            "job_id": "4" * 32,
            "idempotency_key": "ring4-already-stopped-cancel-key",
            "profile": LONG_PROFILE,
            "state": "SUBMISSION_UNKNOWN",
            "memory_released": False,
            "cleanup_required": False,
            "shots": [{"name": "LONG-01", "prompt": "A stable shot", "seed": 8,
                        "state": "SUBMISSION_UNKNOWN", "index": 0,
                        "request_duration_seconds": 15,
                        "submission_phase": "UNKNOWN",
                        "ring_job_id": "ring-job-stopped"}],
        }
        self.jobs.save(row)
        self.jobs.active = row["job_id"]
        self.app.operations = SimpleNamespace(adapters={
            "h3-ring4": SimpleNamespace(observe=lambda: ObservedModel("h3-ring4", "STOPPED")),
            "minimax-h3": SimpleNamespace(observe=lambda: ObservedModel("minimax-h3", "STOPPED")),
        })
        self.jobs.ring_transport = SimpleNamespace(
            cancel=Mock(return_value={"status": "not_found", "reason": "connection_refused"}),
        )
        self.jobs.switch = Mock(side_effect=BlockingIOError("Video batch did not start the pre-existing H3 engine"))

        result = self.jobs.cancel(row["job_id"])

        self.jobs.switch.assert_not_called()
        self.assertEqual(result["state"], "CANCELED")
        self.assertTrue(result["memory_released"])
        self.assertTrue(result["resource_settled"])
        self.assertFalse(result["cleanup_required"])

    def test_ring4_cancel_finishes_cleanup_after_interrupted_unknown(self):
        from DGXGateway.dgx_gateway.video_contracts import LONG_PROFILE

        row = {
            "job_id": "5" * 32,
            "idempotency_key": "ring4-interrupted-cleanup-key",
            "profile": LONG_PROFILE,
            "state": "INTERRUPTED",
            "memory_released": False,
            "cleanup_required": True,
            "resource_settled": False,
            "resource_status": "cancel_stop_failed",
            "shots": [{"name": "LONG-01", "prompt": "A stable shot", "seed": 8,
                        "state": "CANCELED", "index": 0,
                        "request_duration_seconds": 15,
                        "submission_phase": "CANCELED",
                        "ring_job_id": "ring-job-interrupted",
                        "ring_cancel_receipt": {"status": "not_found", "reason": "connection_refused"}}],
        }
        self.jobs.save(row)
        self.jobs.active = row["job_id"]
        (self.jobs.root / (row["job_id"] + ".cancel")).write_text("cancel\n")
        self.jobs.ring_transport = SimpleNamespace(cancel=Mock())
        self.jobs.switch = Mock()

        result = self.jobs.cancel(row["job_id"])

        self.jobs.ring_transport.cancel.assert_not_called()
        # A completed DELETE is not proof that the four-rank denoiser stopped.
        self.jobs.switch.assert_called_once()
        stopped_row, target, suffix = self.jobs.switch.call_args.args
        self.assertEqual((stopped_row["job_id"], target, suffix), (row["job_id"], None, "cancel_stop"))
        self.assertEqual(result["state"], "CANCELED")
        self.assertTrue(result["memory_released"])
        self.assertTrue(result["resource_settled"])
        self.assertFalse(result["cleanup_required"])

    def _adapters(self, *models):
        from DGXGateway.dgx_gateway.domain import ObservedModel
        self.app.residency = SimpleNamespace(row={"state": "NONE"})
        self.app.operations = SimpleNamespace(adapters={
            model_id: SimpleNamespace(observe=lambda model_id=model_id, state=state, profile=profile: ObservedModel(model_id, state, profile))
            for model_id, state, profile in models
        })

    def test_non_preemptive_ring4_healthy_is_admitted(self):
        self._adapters(
            ("h3-ring4", "HEALTHY", "ref2va-official"),
            ("minimax-h3", "STOPPED", None),
        )
        with patch("threading.Thread"):
            row = self.jobs.submit([self.shot], "ring4-nonpreempt-key", non_preemptive=True)
        self.assertEqual(row["state"], "QUEUED")
        self.assertTrue(row["non_preemptive"])

    def test_running_ring_profile_overrides_requested_heretic(self):
        from DGXGateway.dgx_gateway.video_contracts import LONG_PROFILE
        self._adapters(("h3-ring4", "HEALTHY", "ref2va-official"), ("minimax-h3", "STOPPED", None))
        with patch("threading.Thread"):
            row = self.jobs.submit([self.shot], "use-running-official", LONG_PROFILE, non_preemptive=True)
        self.assertEqual(row["runtime_model"], "h3-ring4")
        self.assertEqual(row["runtime_profile"], "ref2va-official")
        self.assertEqual(row["runtime_task"], "ref2va")

    def test_stopped_ring_defaults_to_official(self):
        from DGXGateway.dgx_gateway.video_contracts import LONG_PROFILE
        self._adapters(("h3-ring4", "STOPPED", None))
        with patch("threading.Thread"):
            row = self.jobs.submit([self.shot], "default-official", LONG_PROFILE)
        self.assertEqual(row["runtime_profile"], "ref2va-official")
        self.assertEqual(row["runtime_task"], "ref2va")

    def test_non_preemptive_other_healthy_model_is_busy(self):
        self._adapters(
            ("dsv41-ring-tp4", "HEALTHY", "smoke32k"),
            ("h3-ring4", "STOPPED", None),
        )
        with self.assertRaisesRegex(BlockingIOError, "another model is active"):
            self.jobs.submit([self.shot], "ring4-busy-key", non_preemptive=True)


if __name__ == "__main__":
    unittest.main()
