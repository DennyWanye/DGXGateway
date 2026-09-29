"""P3 safety oracle: ownership, durable request identity and non-replay recovery."""
import base64
import contextlib
import fcntl
import io
import json
import shutil
import subprocess
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import wave
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from DGXGateway.dgx_gateway.audio_assets import AudioAssets
from DGXGateway.dgx_gateway.adapters.h3 import H3Adapter
from DGXGateway.dgx_gateway.domain import ObservedModel
from DGXGateway.dgx_gateway.image_assets import ImageAssets
from DGXGateway.dgx_gateway.server import GatewayApplication
from DGXGateway.dgx_gateway.store import OperationStore
from DGXGateway.dgx_gateway.video_jobs import VideoJobs
from DGXGateway.dgx_gateway.operations import OperationController
from tests.test_dgx_gateway_operations import FakeAdapter, FakeLease


def pcm():
    data = io.BytesIO()
    with wave.open(data, "wb") as stream:
        stream.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\0\0" * 96000)
    return data.getvalue()


class VideoSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = OperationStore(self.root / "gateway.sqlite3")
        self.addCleanup(self.store.close)
        self.h3 = SimpleNamespace(profiles=("heretic-ref2va-v1",), default_profile="heretic-ref2va-v1",
                                  observe=Mock(return_value=ObservedModel("minimax-h3", "STOPPED")))
        self.ring4 = SimpleNamespace(profiles=("ref2va-official", "fl2va-official"), default_profile="fl2va-official",
                                     observe=Mock(return_value=ObservedModel("h3-ring4", "STOPPED")))
        self.vision = SimpleNamespace(profiles=(), observe=Mock(return_value=ObservedModel("vision", "STOPPED")))
        self.app = GatewayApplication(config=SimpleNamespace(state_dir=self.root, repo_root=self.root,
            bind="127.0.0.1", port=18082, nodes=[SimpleNamespace(address="127.0.0.1")]),
            store=self.store, operations=SimpleNamespace(adapters={
                "minimax-h3": self.h3, "h3-ring4": self.ring4, "vision": self.vision}),
            snapshot=None, static_dir=self.root, admission=SimpleNamespace(assert_allowed=lambda: None))
        assets = ImageAssets(self.root / "creative-assets")
        data = io.BytesIO()
        Image.new("RGB", (32, 24), "red").save(data, format="PNG")
        image = assets.register_base64("image.png", base64.b64encode(data.getvalue()).decode())
        self.app.creative_jobs = SimpleNamespace(assets=assets, active=None)
        self.jobs = VideoJobs(self.app)
        self.app.video_jobs = self.jobs
        audio = self.jobs.audio.register_base64("audio.wav", base64.b64encode(pcm()).decode())
        self.shot = {"name": "shot-1", "prompt": "a stable shot", "image_asset_id": image["asset_id"],
                     "audio_asset_id": audio["asset_id"], "seed": 42}

    def submit(self, count=1):
        with patch("DGXGateway.dgx_gateway.video_jobs.threading.Thread"):
            return self.jobs.submit([{**self.shot, "name": f"shot-{i}"} for i in range(count)],
                                    "safe-video-key", non_preemptive=True)

    def own_start(self, row):
        op = self.store.create_operation("c" * 32, None, "minimax-h3", row["job_id"] + "-start", "heretic-ref2va-v1")
        self.store.freeze_outcome(op.operation_id, "COMMITTED")
        self.store.set_phase(op.operation_id, "RELEASED")
        row["start_operation_id"] = op.operation_id
        self.h3.observe.return_value = ObservedModel("minimax-h3", "HEALTHY", "heretic-ref2va-v1")
        return op

    def test_non_preemptive_guard_rejects_running_or_uncertain_models_inside_switch(self):
        for model in (ObservedModel("vision", "HEALTHY"), ObservedModel("vision", "UNKNOWN")):
            self.vision.observe.return_value = model
            with self.assertRaises(BlockingIOError), patch("DGXGateway.dgx_gateway.server.threading.Thread") as thread:
                self.app.submit_switch("minimax-h3", "heretic-ref2va-v1", "guard", non_preemptive=True)
            thread.assert_not_called()
            self.assertEqual(self.store.list_operations(), [])

    def test_non_preemptive_switch_admits_healthy_ring4_target(self):
        self.ring4.observe.return_value = ObservedModel("h3-ring4", "HEALTHY", "ref2va-official")
        with patch("DGXGateway.dgx_gateway.server.threading.Thread") as thread:
            operation, created = self.app.submit_switch(
                "h3-ring4", "ref2va-official", "ring4-guard-key", non_preemptive=True)
        thread.assert_not_called()
        self.assertTrue(created)
        self.assertEqual(operation.phase, "RELEASED")
        self.assertEqual(operation.outcome, "COMMITTED")

    def test_ring4_healthy_already_at_target_runs_without_blocking(self):
        self.ring4.observe.return_value = ObservedModel("h3-ring4", "HEALTHY", "ref2va-official")
        row = self.submit()
        self.jobs._generate = Mock(return_value={"path": "clip.mp4"})
        with patch("DGXGateway.dgx_gateway.server.threading.Thread") as thread:
            thread.side_effect = AssertionError("already-at-target must not start a mutation thread")
            self.jobs.run(row)
        self.assertEqual(row["state"], "SUCCEEDED")
        self.assertTrue(row["memory_released"])
        self.assertFalse(row.get("resource_not_acquired", False))
        self.jobs._generate.assert_called_once()

    def test_initial_busy_rejection_does_not_create_video_job(self):
        self.vision.observe.return_value = ObservedModel("vision", "HEALTHY")
        with self.assertRaises(BlockingIOError):
            self.submit()
        self.assertEqual(list(self.jobs.root.glob("*.json")), [])

    def test_model_becomes_active_after_acceptance_before_worker_start(self):
        row = self.submit()
        self.vision.observe.return_value = ObservedModel("vision", "HEALTHY")
        self.jobs.run(row)
        result = self.jobs.get(row["job_id"])
        self.assertEqual(result["state"], "BLOCKED")
        self.assertEqual(result["shots"][0]["submission_phase"], "NOT_SUBMITTED")
        self.assertTrue(result["resource_not_acquired"])
        self.assertTrue(result["memory_released"])
        self.assertEqual(self.store.list_operations(), [])
        self.assertIsNone(self.jobs.active)
        canceled = self.jobs.cancel(row["job_id"])
        self.assertEqual(canceled["state"], "BLOCKED")
        self.assertFalse(canceled["cancel_requested"])
        self.assertNotIn("cancel_status", canceled)

    def test_acceptance_is_fsynced_before_worker_start_and_has_independent_request_ids(self):
        ordering = []
        original_fsync = __import__("os").fsync
        def fsync(fd):
            original_fsync(fd)
            ordering.append("fsync")
        with patch("DGXGateway.dgx_gateway.video_jobs.os.fsync", side_effect=fsync), \
                patch("DGXGateway.dgx_gateway.video_jobs.threading.Thread") as thread:
            thread.return_value.start.side_effect = lambda: ordering.append("start")
            row = self.jobs.submit([self.shot, {**self.shot, "name": "shot-2"}], "durable-key", non_preemptive=True)
        self.assertGreaterEqual(ordering.index("start"), 2)
        self.assertTrue(row["non_preemptive"])
        self.assertEqual(len({shot["request_id"] for shot in row["shots"]}), 2)
        self.assertTrue(all(shot["submission_phase"] == "NOT_SUBMITTED" for shot in row["shots"]))

    def test_post_identity_is_durable_and_disconnect_never_cleans_or_reposts(self):
        row = self.submit(count=2)
        # This oracle exercises the historical synchronous two-node transport.
        # New batches use h3-dual-pair, so freeze the legacy backend explicitly.
        row.update(runtime_model="minimax-h3", runtime_profile="heretic-ref2va-v1", runtime_task="ref2va")
        self.jobs.save(row)
        self.jobs.switch = Mock()
        self.jobs._token = Mock(return_value="test-token-unused-on-network")
        def failed_post(request, **_kwargs):
            persisted = json.loads((self.jobs.root / (row["job_id"] + ".json")).read_text())
            self.assertEqual(persisted["shots"][0]["submission_phase"], "REQUEST_STARTED")
            self.assertEqual(request.get_header("X-h3-request-id"), persisted["shots"][0]["request_id"])
            raise urllib.error.URLError("simulated lost response")
        self.jobs.http.open = Mock(side_effect=failed_post)
        self.jobs.run(row)
        self.assertEqual(row["state"], "SUBMISSION_UNKNOWN")
        self.assertEqual(row["shots"][0]["submission_phase"], "UNKNOWN")
        self.assertEqual(row["shots"][1]["submission_phase"], "NOT_SUBMITTED")
        self.assertTrue(row["cleanup_required"])
        self.assertFalse(row["memory_released"])
        self.assertEqual(self.jobs.switch.call_count, 1)
        self.jobs.run(row)
        self.assertEqual(sum(c.args[0].get_method() == "POST" for c in self.jobs.http.open.call_args_list), 1)
        self.assertTrue(all(c.args[0].get_method() == "GET" for c in self.jobs.http.open.call_args_list[1:]))

    def test_restart_of_unstarted_queue_never_claims_external_model(self):
        row = self.submit()
        self.vision.observe.return_value = ObservedModel("vision", "HEALTHY")
        restarted = VideoJobs(self.app)
        result = restarted.get(row["job_id"])
        self.assertFalse(result["cleanup_required"])
        self.assertTrue(result["memory_released"])
        self.assertIsNone(restarted.active)
        with patch.object(self.app, "submit_switch") as switch:
            restarted.reconcile(row["job_id"])
        switch.assert_not_called()

    def test_reconcile_refuses_foreign_operation_even_if_h3_looks_healthy(self):
        row = self.submit()
        self.own_start(row)
        row.update(state="INTERRUPTED", cleanup_required=True)
        self.jobs.save(row)
        foreign = self.store.create_operation("d" * 32, "minimax-h3", "minimax-h3", "external-owner", "heretic-ref2va-v1")
        self.store.freeze_outcome(foreign.operation_id, "COMMITTED")
        self.store.set_phase(foreign.operation_id, "RELEASED")
        with patch.object(self.app, "submit_switch") as switch, self.assertRaises(BlockingIOError):
            self.jobs.reconcile(row["job_id"])
        switch.assert_not_called()

    def test_reconcile_requires_own_committed_start_operation(self):
        row = self.submit()
        row.update(state="INTERRUPTED", cleanup_required=True)
        self.jobs.save(row)
        self.h3.observe.return_value = ObservedModel("minimax-h3", "HEALTHY", "heretic-ref2va-v1")
        with patch.object(self.app, "submit_switch") as switch, self.assertRaises(BlockingIOError):
            self.jobs.reconcile(row["job_id"])
        switch.assert_not_called()

    def test_read_only_find_does_not_submit(self):
        row = self.submit()
        with patch.object(self.jobs, "submit") as submit:
            self.assertEqual(self.jobs.find("safe-video-key")["job_id"], row["job_id"])
            self.assertIsNone(self.jobs.find("not-present"))
        submit.assert_not_called()

    def test_lease_time_busy_guard_never_stops_or_rolls_back_external_model(self):
        row = self.submit()
        events = []
        target = FakeAdapter("minimax-h3", events)
        external = FakeAdapter("vision", events)
        class RacingLease(FakeLease):
            def acquire(lease_self):
                result = super().acquire()
                external.state = "HEALTHY"  # Becomes busy after preflight accepted.
                return result
        operation = self.store.create_operation("e" * 32, None, "minimax-h3", row["job_id"] + "-start", "heretic-ref2va-v1")
        controller = OperationController(store=self.store, adapters={"minimax-h3": target, "vision": external},
                                        lease_factory=lambda oid: RacingLease(events, oid))
        result = controller.execute(operation.operation_id)
        self.assertEqual(result.outcome, "NO_RESOURCES_ACQUIRED")
        self.assertEqual(result.phase, "RELEASED")
        self.assertEqual(external.state, "HEALTHY")
        self.assertFalse(any(event.endswith((".start", ".stop", ".drain", ".retire")) for event in events))
        self.assertIn("lease.release", events)

    def test_ring4_video_start_intent_passes_post_lease_admission(self):
        row = self.submit()
        events = []
        target = FakeAdapter("h3-ring4", events)
        operation = self.store.create_operation(
            "9" * 32, None, "h3-ring4", row["job_id"] + "-start", "ref2va-official")
        controller = OperationController(
            store=self.store,
            adapters={"h3-ring4": target},
            lease_factory=lambda oid: FakeLease(events, oid),
        )

        result = controller.execute(operation.operation_id)

        self.assertEqual((result.phase, result.outcome), ("RELEASED", "COMMITTED"))
        self.assertIn("h3-ring4.start", events)
        self.assertEqual(target.started_profiles, ["ref2va-official"])
        self.assertIn("lease.release", events)

    def test_stop_rechecks_ownership_after_lease_without_touching_external_h3(self):
        for suffix in ("stop", "cleanup", "reconcile_stop"):
            with self.subTest(suffix=suffix):
                # Each scenario has isolated operation history.
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    store = OperationStore(root / "gateway.sqlite3")
                    self.addCleanup(store.close)
                    jid = "a" * 32
                    start = store.create_operation("b" * 32, None, "minimax-h3", jid + "-start", "heretic-ref2va-v1")
                    store.freeze_outcome(start.operation_id, "COMMITTED")
                    store.set_phase(start.operation_id, "RELEASED")
                    (root / "video-jobs").mkdir()
                    (root / "video-jobs" / (jid + ".json")).write_text(json.dumps({
                        "job_id": jid, "profile": "heretic-ref2va-v1", "start_operation_id": start.operation_id,
                        "non_preemptive": True, "shots": []}))
                    events = []
                    target = FakeAdapter("minimax-h3", events, state="HEALTHY")
                    target.observe = Mock(return_value=ObservedModel("minimax-h3", "HEALTHY", "heretic-ref2va-v1"))
                    class RacingLease(FakeLease):
                        def acquire(lease_self):
                            result = super().acquire()
                            target.observe.return_value = ObservedModel("minimax-h3", "HEALTHY", "external-new-profile")
                            return result
                    operation = store.create_operation("d" * 32, "minimax-h3", None, jid + "-" + suffix,
                                                       source_profile="heretic-ref2va-v1")
                    controller = OperationController(store=store, adapters={"minimax-h3": target},
                                                    lease_factory=lambda oid: RacingLease(events, oid))
                    result = controller.execute(operation.operation_id)
                    self.assertEqual(result.outcome, "NO_RESOURCES_ACQUIRED")
                    self.assertEqual(result.phase, "RELEASED")
                    self.assertFalse(any(event.endswith((".start", ".stop", ".drain", ".retire")) for event in events))
                    self.assertIn("lease.release", events)

    def test_owned_stop_passes_execution_guard_and_releases_model(self):
        row = self.submit()
        self.own_start(row)
        self.jobs.save(row)
        events = []
        target = FakeAdapter("minimax-h3", events, state="HEALTHY")
        target.observe = lambda: ObservedModel("minimax-h3", target.state, row["profile"])
        operation = self.store.create_operation("e" * 32, "minimax-h3", None, row["job_id"] + "-stop",
                                               source_profile=row["profile"])
        controller = OperationController(store=self.store, adapters={"minimax-h3": target},
                                        lease_factory=lambda oid: FakeLease(events, oid))
        result = controller.execute(operation.operation_id)
        self.assertEqual(result.outcome, "COMMITTED")
        self.assertEqual(result.phase, "RELEASED")
        self.assertEqual(events.count("minimax-h3.stop"), 1)
        self.assertEqual(target.state, "STOPPED")

    def partial_recovery_fixture(self):
        """Real adapter/controller with only remote transport and lease simulated."""
        row = self.submit()
        self.own_start(row)
        row.update(state="SUBMISSION_UNKNOWN", cleanup_required=True, memory_released=False)
        row["shots"][0].update(state="SUBMISSION_UNKNOWN", submission_phase="UNKNOWN")
        self.jobs.save(row)
        events = []
        state = {"state": "FAILED", "profile": row["profile"], "healthy": False}
        authority = Mock()
        authority.status.side_effect = lambda: dict(state)
        authority.run.return_value = SimpleNamespace(returncode=0, stdout=json.dumps({
            "queue_running": [], "queue_pending": []}))
        def mutate(*_args):
            events.append("h3.stop")
            state.update(state="STOPPED", healthy=False)
        authority.mutate_borrowed.side_effect = mutate
        ssh = Mock()
        def remote(_node, command, **_kwargs):
            if command[-1] == "status-local":
                return SimpleNamespace(returncode=0, stdout='{"api":"STOPPED"}')
            self.assertIn(command[-1], {"assert-h3-stopped", "assert-stopped"})
            events.append(command[-1])
            return SimpleNamespace(returncode=0 if state["state"] == "STOPPED" else 1, stdout="stopped")
        ssh.run.side_effect = remote
        adapter = H3Adapter(SimpleNamespace(repo_root=self.root, lock_root=self.root / "locks",
            h3_queue_url="http://unused.invalid/queue", nodes=[SimpleNamespace(role="head"), SimpleNamespace(role="worker")]), ssh)
        adapter._authority = lambda: authority
        class RecoveryLease(FakeLease):
            guard = FakeLease.check_supervision
        controller = OperationController(store=self.store,
            adapters={"minimax-h3": adapter, "vision": FakeAdapter("vision", events)},
            lease_factory=lambda oid: RecoveryLease(events, oid))
        self.app.operations = controller
        operation = self.store.create_operation("e" * 32, "minimax-h3", None, row["job_id"] + "-reconcile_stop",
                                               source_profile=row["profile"])
        return row, operation, controller, adapter, authority, ssh, state, events

    def test_owned_partial_uses_real_drain_and_archives_without_replaying_unknown(self):
        row, operation, controller, _adapter, authority, ssh, _state, events = self.partial_recovery_fixture()
        with patch.object(self.jobs, "_generate") as generate:
            result = controller.execute(operation.operation_id)
            self.assertEqual((result.phase, result.outcome), ("RELEASED", "COMMITTED"))
            result = self.jobs.reconcile(row["job_id"])
            again = self.jobs.reconcile(row["job_id"])
        generate.assert_not_called()
        self.assertEqual(events.count("h3.stop"), 1)
        self.assertIn("assert-h3-stopped", events)
        self.assertIn("assert-stopped", events)
        self.assertTrue(any(call.args[1][-1] == "status-local" for call in ssh.run.call_args_list))
        authority.run.assert_called_once()
        self.assertTrue(result["memory_released"])
        self.assertFalse(result["cleanup_required"])
        self.assertEqual(result["shots"][0]["submission_phase"], "UNKNOWN")
        self.assertEqual(result["state"], "INTERRUPTED")
        self.assertEqual(again, result)
        self.assertIsNone(self.jobs.active)

    def test_partial_recovery_repeats_drain_then_reconciles_original_failed_outcome(self):
        row, operation, controller, _adapter, authority, _ssh, _state, events = self.partial_recovery_fixture()
        authority.run.return_value.stdout = '{"queue_running":[1],"queue_pending":[]}'
        result = controller.execute(operation.operation_id)
        self.assertEqual((result.phase, result.outcome), ("RECOVERY_REQUIRED", "RECOVERY_REQUIRED"))
        with self.assertRaises(RuntimeError):
            self.app.recover_stop(operation.operation_id)
        self.assertNotIn("h3.stop", events)
        self.assertTrue(self.jobs.get(row["job_id"])["cleanup_required"])
        authority.run.return_value.stdout = '{"queue_running":[],"queue_pending":[]}'
        result = self.app.recover_stop(operation.operation_id)
        self.assertEqual((result.phase, result.outcome), ("RELEASED", "RECOVERY_REQUIRED"))
        with patch.object(VideoJobs, "_generate") as generate, patch.object(self.app, "submit_switch") as switch:
            self.jobs = VideoJobs(self.app)
            self.app.video_jobs = self.jobs
            result = self.jobs.reconcile(row["job_id"])
        generate.assert_not_called()
        switch.assert_not_called()
        self.assertEqual(events.count("h3.stop"), 1)
        self.assertEqual(result["shots"][0]["submission_phase"], "UNKNOWN")
        self.assertTrue(result["memory_released"])
        self.assertFalse(result["cleanup_required"])

    def test_partial_drain_refuses_live_api_or_unproved_queue_on_both_entry_points(self):
        _row, operation, controller, _adapter, authority, ssh, _state, events = self.partial_recovery_fixture()
        # First reach actual SOURCE_DRAINING and keep its original failed outcome.
        authority.run.return_value.stdout = '{"queue_running":[1],"queue_pending":[]}'
        self.assertEqual(controller.execute(operation.operation_id).outcome, "RECOVERY_REQUIRED")
        remote = ssh.run.side_effect
        cases = [
            ("live-api-even-if-unhealthy", 0, '{"api":"RUNNING","api_healthy":false}', 0, '{"queue_running":[],"queue_pending":[]}'),
            ("status-failure", 1, '{"api":"STOPPED"}', 0, '{"queue_running":[],"queue_pending":[]}'),
            ("status-malformed", 0, 'invalid', 0, '{"queue_running":[],"queue_pending":[]}'),
            ("running", 0, '{"api":"STOPPED"}', 0, '{"queue_running":[1],"queue_pending":[]}'),
            ("pending", 0, '{"api":"STOPPED"}', 0, '{"queue_running":[],"queue_pending":[1]}'),
            ("missing-key", 0, '{"api":"STOPPED"}', 0, '{"queue_pending":[]}'),
            ("queue-malformed", 0, '{"api":"STOPPED"}', 0, 'invalid'),
            ("queue-error", 0, '{"api":"STOPPED"}', 1, '{"queue_running":[],"queue_pending":[]}'),
        ]
        for name, status_code, status_body, queue_code, queue_body in cases:
            with self.subTest(name=name):
                ssh.run.side_effect = lambda node, command, **kwargs: (
                    SimpleNamespace(returncode=status_code, stdout=status_body)
                    if command[-1] == "status-local" else remote(node, command, **kwargs))
                authority.run.return_value = SimpleNamespace(returncode=queue_code, stdout=queue_body)
                with self.assertRaises((RuntimeError, ValueError)):
                    self.app.recover_stop(operation.operation_id)
                self.assertNotIn("h3.stop", events)
                self.assertEqual(self.store.get_operation(operation.operation_id).phase, "RECOVERY_REQUIRED")

    def test_partial_explicit_recovery_rechecks_current_ownership(self):
        _row, operation, controller, _adapter, authority, _ssh, state, events = self.partial_recovery_fixture()
        authority.run.return_value.stdout = '{"queue_running":[1],"queue_pending":[]}'
        self.assertEqual(controller.execute(operation.operation_id).outcome, "RECOVERY_REQUIRED")
        authority.run.return_value.stdout = '{"queue_running":[],"queue_pending":[]}'
        state["profile"] = "external-profile"
        with self.assertRaises(BlockingIOError):
            self.app.recover_stop(operation.operation_id)
        self.assertNotIn("h3.stop", events)
        state["profile"] = "heretic-ref2va-v1"
        with patch.object(authority, "status", side_effect=RuntimeError("status unavailable")):
            with self.assertRaises(BlockingIOError):
                self.app.recover_stop(operation.operation_id)
        controller.adapters["vision"].state = "HEALTHY"
        with self.assertRaises(BlockingIOError):
            self.app.recover_stop(operation.operation_id)
        controller.adapters["vision"].state = "STOPPED"
        foreign = self.store.create_operation("f" * 32, None, None, "external-owner")
        self.store.freeze_outcome(foreign.operation_id, "COMMITTED")
        self.store.set_phase(foreign.operation_id, "RELEASED")
        with self.assertRaises(BlockingIOError):
            self.app.recover_stop(operation.operation_id)
        self.assertNotIn("h3.stop", events)

    def test_partial_stop_rechecks_profile_after_actual_lease_acquisition(self):
        _row, operation, controller, _adapter, _authority, _ssh, state, events = self.partial_recovery_fixture()
        class RacingLease(FakeLease):
            def acquire(lease_self):
                result = super().acquire()
                state["profile"] = "external-profile"
                return result
        controller.lease_factory = lambda oid: RacingLease(events, oid)
        result = controller.execute(operation.operation_id)
        self.assertEqual((result.phase, result.outcome), ("RELEASED", "NO_RESOURCES_ACQUIRED"))
        self.assertNotIn("h3.stop", events)
        self.assertIn("lease.release", events)

    def test_remote_stop_refuses_held_generation_lock_before_constructing_controller(self):
        from tests.test_minimax_h3_state_machine import load_control
        control = load_control()
        lock_path = self.root / "generation.lock"
        with lock_path.open("w") as held, patch.object(control, "_production_controller") as lifecycle, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            fcntl.flock(held.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            result = control.main(["stop", "--preserve-llm-quiesce", "--lock-path", str(lock_path),
                                   "--state-path", str(self.root / "unused-state.json")])
        lifecycle.assert_not_called()
        self.assertEqual(result, 75)
        self.assertEqual(output.getvalue().strip(), "BUSY")

    def test_recovered_stop_requires_complete_evidence_and_all_models_stopped(self):
        row, operation, controller, _adapter, authority, _ssh, state, events = self.partial_recovery_fixture()
        authority.run.return_value.stdout = '{"queue_running":[1],"queue_pending":[]}'
        self.assertEqual(controller.execute(operation.operation_id).outcome, "RECOVERY_REQUIRED")
        authority.run.return_value.stdout = '{"queue_running":[],"queue_pending":[]}'
        self.app.recover_stop(operation.operation_id)
        original_events = self.store.events
        for omitted in ("recovery-safe-stop-complete", "recovery-stopped-proof", "recovery-all-stopped-proof"):
            with self.subTest(omitted=omitted), patch.object(self.store, "events", side_effect=lambda oid: [
                    event for event in original_events(oid) if oid != operation.operation_id or event["kind"] != omitted]):
                with self.assertRaises(BlockingIOError):
                    self.jobs.reconcile(row["job_id"])
        with patch.object(self.store, "events", side_effect=lambda oid: [event for event in original_events(oid)
                if oid != operation.operation_id or not (event["kind"] == "recovery-all-stopped-proof"
                    and event["payload"].get("model_id") == "vision")]):
            with self.assertRaises(BlockingIOError):
                self.jobs.reconcile(row["job_id"])
        for model_state in ("FAILED", "READY"):
            state.update(state=model_state, healthy=model_state == "READY")
            with self.assertRaises(BlockingIOError):
                self.jobs.reconcile(row["job_id"])
        state["state"] = "STOPPED"
        state["profile"] = "external-profile"
        with self.assertRaises(BlockingIOError):
            self.jobs.reconcile(row["job_id"])
        state["profile"] = row["profile"]
        with patch.object(authority, "status", side_effect=RuntimeError("status unavailable")):
            with self.assertRaises(BlockingIOError):
                self.jobs.reconcile(row["job_id"])
        foreign = self.store.create_operation("f" * 32, None, None, "external-owner")
        self.store.freeze_outcome(foreign.operation_id, "COMMITTED")
        self.store.set_phase(foreign.operation_id, "RELEASED")
        with self.assertRaises(BlockingIOError):
            self.jobs.reconcile(row["job_id"])
        self.assertTrue(self.jobs.get(row["job_id"])["cleanup_required"])
        self.assertEqual(events.count("h3.stop"), 1)

    def test_stop_admission_rejection_preserves_cleanup_obligation(self):
        row = self.submit()
        self.own_start(row)
        row.update(state="INTERRUPTED", cleanup_required=True, memory_released=False)
        self.jobs.save(row)
        operation = self.store.create_operation("e" * 32, "minimax-h3", None, row["job_id"] + "-reconcile_stop",
                                               source_profile=row["profile"])
        self.store.freeze_outcome(operation.operation_id, "NO_RESOURCES_ACQUIRED")
        self.store.set_phase(operation.operation_id, "RELEASED")
        with patch.object(self.app, "submit_switch", return_value=(operation, True)):
            with self.assertRaises(BlockingIOError):
                self.jobs.switch(row, None, "reconcile_stop")
        self.assertTrue(row["cleanup_required"])
        self.assertFalse(row["memory_released"])
        self.assertFalse(row.get("resource_not_acquired", False))

    def test_canceled_stop_does_not_claim_memory_was_released(self):
        row = self.submit()
        self.own_start(row)
        row.update(state="INTERRUPTED", cleanup_required=True, memory_released=False)
        self.jobs.save(row)
        operation = self.store.create_operation("e" * 32, "minimax-h3", None, row["job_id"] + "-stop",
                                               source_profile=row["profile"])
        self.store.freeze_outcome(operation.operation_id, "CANCELED")
        self.store.set_phase(operation.operation_id, "RELEASED")
        with patch.object(self.app, "submit_switch", return_value=(operation, True)):
            with self.assertRaises(RuntimeError):
                self.jobs.switch(row, None, "stop")
        self.assertTrue(row["cleanup_required"])
        self.assertFalse(row["memory_released"])

    def test_legacy_queued_restart_does_not_claim_resources_or_replay(self):
        row = self.submit()
        row.pop("non_preemptive")
        for shot in row["shots"]:
            shot.pop("submission_phase")
            shot.pop("request_id")
        self.jobs.save(row)
        restarted = VideoJobs(self.app)
        result = restarted.get(row["job_id"])
        self.assertEqual(result["state"], "INTERRUPTED")
        self.assertTrue(result["memory_released"])
        self.assertFalse(result["cleanup_required"])
        self.assertEqual(result["shots"][0]["submission_phase"], "NOT_SUBMITTED")
        self.assertIsNone(restarted.active)
        # A queued-looking legacy file with a recorded lifecycle start is uncertain.
        row["state"] = "QUEUED"
        self.own_start(row)
        row.pop("start_operation_id")
        self.jobs.save(row)
        restarted = VideoJobs(self.app)
        self.assertTrue(restarted.get(row["job_id"])["cleanup_required"])
        self.assertEqual(restarted.active, row["job_id"])

    def test_missing_durable_start_policy_is_not_treated_as_preemption_authorization(self):
        events = []
        target, external = FakeAdapter("minimax-h3", events), FakeAdapter("vision", events, state="HEALTHY")
        operation = self.store.create_operation("e" * 32, None, "minimax-h3", "f" * 32 + "-start", "heretic-ref2va-v1")
        controller = OperationController(store=self.store, adapters={"minimax-h3": target, "vision": external},
                                        lease_factory=lambda oid: FakeLease(events, oid))
        result = controller.execute(operation.operation_id)
        self.assertEqual(result.outcome, "NO_RESOURCES_ACQUIRED")
        self.assertEqual(external.state, "HEALTHY")
        self.assertFalse(any(event.endswith((".start", ".stop", ".drain", ".retire")) for event in events))

    def test_no_resources_acquired_outcome_releases_business_ownership_without_stop(self):
        row = self.submit()
        operation = self.store.create_operation("e" * 32, None, "minimax-h3", row["job_id"] + "-start", "heretic-ref2va-v1")
        self.store.freeze_outcome(operation.operation_id, "NO_RESOURCES_ACQUIRED")
        self.store.set_phase(operation.operation_id, "RELEASED")
        with patch.object(self.app, "submit_switch", return_value=(operation, True)) as switch:
            self.jobs.run(row)
        self.assertEqual(row["state"], "BLOCKED")
        self.assertTrue(row["resource_not_acquired"])
        self.assertTrue(row["memory_released"])
        self.assertFalse(row["cleanup_required"])
        self.assertEqual(switch.call_count, 1)
        self.assertIsNone(self.jobs.active)

    def test_reconcile_concurrent_claim_calls_stop_only_once(self):
        row = self.submit()
        self.own_start(row)
        row.update(state="INTERRUPTED", cleanup_required=True)
        self.jobs.save(row)
        entered, release = threading.Event(), threading.Event()
        def switch(*_args):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("reconcile barrier")
        self.jobs.switch = Mock(side_effect=switch)
        errors = []
        def recover():
            try:
                self.jobs.reconcile(row["job_id"])
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=recover)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaises(BlockingIOError):
                self.jobs.reconcile(row["job_id"])
        finally:
            release.set()
            thread.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(self.jobs.switch.call_count, 1)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_published_video_is_recovered_without_writing_or_resubmitting(self):
        row = self.submit()
        row.update(state="SUBMISSION_UNKNOWN", cleanup_required=True)
        row["shots"][0].update(state="SUBMISSION_UNKNOWN", submission_phase="REQUEST_STARTED")
        self.jobs.save(row)
        path = self.jobs._artifact_path(row, row["shots"][0])
        path.parent.mkdir(parents=True)
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                        "color=c=red:size=64x64:rate=24:duration=4", "-c:v", "libx264", "-threads", "1", str(path)],
                       check=True, capture_output=True, timeout=30)
        saved = (self.jobs.root / (row["job_id"] + ".json")).read_bytes()
        with patch.object(self.jobs, "save") as save, patch.object(self.jobs, "submit") as submit:
            result = self.jobs.get(row["job_id"])
        save.assert_not_called()
        submit.assert_not_called()
        self.assertTrue(result["shots"][0]["artifact_recovered"])
        self.assertEqual(result["shots"][0]["state"], "SUCCEEDED")
        self.assertEqual(result["shots"][0]["submission_phase"], "RECEIVED")
        self.assertEqual((self.jobs.root / (row["job_id"] + ".json")).read_bytes(), saved)


class StrictAudioTests(unittest.TestCase):
    def test_declared_four_seconds_with_truncated_samples_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            assets = AudioAssets(Path(directory))
            for data in (pcm()[:-2], pcm()[:100], pcm() + b"unclaimed trailing bytes"):
                with self.subTest(size=len(data)), self.assertRaises(ValueError):
                    assets.register_base64("bad.wav", base64.b64encode(data).decode())


if __name__ == "__main__":
    unittest.main()
