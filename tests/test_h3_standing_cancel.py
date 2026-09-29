import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock
from types import SimpleNamespace as NS
from DGXGateway.dgx_gateway.video_jobs import assert_video_stop_ownership


class StandingRingCancelTests(unittest.TestCase):
    def setUp(self):
        self.job = "a" * 32
        self.start = NS(operation_id="start-op", idempotency_key=self.job+"-start",
                        target_model="h3-ring4", target_profile="ref2va-official",
                        phase="RELEASED", outcome="COMMITTED")
        self.events = [{"kind": "already-at-target", "payload": {}},
                       {"kind": "request-origin", "payload": {"kind": "task", "job_id": self.job}}]
        self.history = [self.start]
        self.store = NS(list_operations=lambda **kw: self.history, events=lambda op: self.events)
        self.row = {"job_id": self.job, "runtime_model": "h3-ring4", "runtime_profile": "ref2va-official",
                    "profile": "heretic-ref2va-hmnsfw-v1", "start_operation_id": "start-op",
                    "cancel_requested": True, "shots": [{"ring_job_id": "original-ring-job"}]}
        self.observed = [NS(model_id="h3-ring4", profile="ref2va-official", state="HEALTHY")]

    def test_owned_standing_request_can_cancel(self):
        self.assertEqual(assert_video_stop_ownership(self.store, self.row, self.observed), "OWNED_H3")

    def test_missing_cancel_receipt_or_durable_origin_rejected(self):
        for field, value in (("cancel_requested", False), ("shots", [])):
            with self.assertRaises(BlockingIOError):
                assert_video_stop_ownership(self.store, {**self.row, field:value}, self.observed)
        self.events.pop()
        with self.assertRaises(BlockingIOError):
            assert_video_stop_ownership(self.store, self.row, self.observed)

    def test_foreign_intervening_operation_and_profile_rejected(self):
        self.history.insert(0, NS(operation_id="foreign", idempotency_key="external-switch", phase="RELEASED", outcome="COMMITTED"))
        with self.assertRaises(BlockingIOError):
            assert_video_stop_ownership(self.store, self.row, self.observed)
        self.history.pop(0)
        self.observed[0].profile = "ref2va-heretic"
        with self.assertRaises(BlockingIOError):
            assert_video_stop_ownership(self.store, self.row, self.observed)

    def test_async_cancel_stop_rechecks_original_history(self):
        self.history.insert(0, NS(operation_id="stop-op", idempotency_key=self.job+"-cancel_stop"))
        self.assertEqual(assert_video_stop_ownership(self.store, self.row, self.observed,
                                                     current_operation_id="stop-op"), "OWNED_H3")

    def test_controller_reads_durable_cancel_intent_and_rejects_foreign_history(self):
        from DGXGateway.dgx_gateway.operations import OperationController
        with tempfile.TemporaryDirectory() as directory:
            self.store.path = Path(directory) / "operations.sqlite"
            root = Path(directory) / "video-jobs"
            root.mkdir()
            (root / (self.job + ".json")).write_text(json.dumps({**self.row, "cancel_requested": False}))
            marker = root / (self.job + ".cancel")
            marker.write_text("cancel\n")
            stop = NS(operation_id="stop-op", idempotency_key=self.job+"-cancel_stop",
                      source_model="h3-ring4", source_profile="ref2va-official", target_model=None)
            self.history.insert(0, stop)
            controller = OperationController(store=self.store, lease_factory=None,
                adapters={"h3-ring4": NS(observe=lambda: self.observed[0])})
            context = NS(guard=Mock())
            controller._guard_video_stop(stop, context)
            context.guard.assert_called_once()
            marker.unlink()
            with self.assertRaises(BlockingIOError):
                controller._guard_video_stop(stop, context)
