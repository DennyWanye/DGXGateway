import tempfile
import unittest
from pathlib import Path


class FakeLease:
    def __init__(self, events, operation_id, fail_release=False):
        self.events = events
        self.operation_id = operation_id
        self.token_path = Path("/tmp/fake-gateway.token")
        self.lease_generation = "a" * 32
        self.released = False
        self.fail_release = fail_release

    def acquire(self):
        self.events.append("lease.acquire")
        return {"lease_generation": self.lease_generation}

    def start_heartbeat(self):
        self.events.append("lease.heartbeat")

    def check_supervision(self):
        self.events.append("lease.guard")

    def release(self):
        self.events.append("lease.release")
        if self.fail_release:
            raise RuntimeError("injected release failure")
        self.released = True
        return {"state": "UNLOCKED", "release_operation_id": self.operation_id}


class FakeAdapter:
    def __init__(self, model_id, events, state="STOPPED", fail_health=False, ambiguous_stop=False):
        self.model_id = model_id
        self.events = events
        self.state = state
        self.fail_health = fail_health
        self.ambiguous_stop = ambiguous_stop
        self.started_profiles = []

    def observe(self):
        from DGXGateway.dgx_gateway.domain import ObservedModel
        return ObservedModel(self.model_id, self.state)

    def drain(self, _context):
        self.events.append(f"{self.model_id}.drain")

    def stop(self, _context):
        self.events.append(f"{self.model_id}.stop")
        self.state = "UNKNOWN" if self.ambiguous_stop else "STOPPED"

    def retire(self, _context):
        self.events.append(f"{self.model_id}.retire")
        self.state = "STOPPED"

    def stopped_proof(self, _context):
        self.events.append(f"{self.model_id}.stopped-proof")
        if self.state != "STOPPED":
            raise RuntimeError("stopped proof unavailable")
        return {"model_id": self.model_id, "stopped": True, "sha256": "b" * 64}

    def start(self, _context, profile=None):
        self.events.append(f"{self.model_id}.start")
        self.started_profiles.append(profile)
        self.state = "HEALTHY"

    def health(self, _context):
        self.events.append(f"{self.model_id}.health")
        if self.fail_health:
            raise RuntimeError("injected health failure")
        return {"healthy": self.state == "HEALTHY"}


class OperationControllerTest(unittest.TestCase):
    def make(self, source, target, events, *, fail_release=False):
        from DGXGateway.dgx_gateway.operations import OperationController
        from DGXGateway.dgx_gateway.store import OperationStore

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = OperationStore(Path(temp.name) / "state.sqlite3")
        operation = store.create_operation("op-1", source.model_id, target.model_id, "key-1")
        controller = OperationController(
            store=store,
            adapters={source.model_id: source, target.model_id: target},
            lease_factory=lambda operation_id: FakeLease(
                events, operation_id, fail_release=fail_release
            ),
        )
        return controller, store, operation

    def test_success_proves_source_stopped_before_target_start(self):
        events = []
        source = FakeAdapter("h3", events, state="HEALTHY")
        target = FakeAdapter("deepseek", events)
        controller, store, operation = self.make(source, target, events)

        controller.execute(operation.operation_id)

        final = store.get_operation(operation.operation_id)
        self.assertEqual(final.phase, "RELEASED")
        self.assertEqual(final.outcome, "COMMITTED")
        self.assertLess(events.index("h3.stopped-proof"), events.index("deepseek.start"))
        self.assertEqual(events[-1], "lease.release")

    def test_failed_target_is_retired_before_source_rollback(self):
        events = []
        source = FakeAdapter("h3", events, state="HEALTHY")
        target = FakeAdapter("deepseek", events, fail_health=True)
        controller, store, operation = self.make(source, target, events)

        controller.execute(operation.operation_id)

        final = store.get_operation(operation.operation_id)
        self.assertEqual(final.phase, "RELEASED")
        self.assertEqual(final.outcome, "ROLLED_BACK")
        self.assertLess(events.index("deepseek.stopped-proof"), events.index("h3.start"))
        self.assertLess(events.index("h3.health"), events.index("lease.release"))

    def test_same_model_profile_rollback_restores_captured_source_profile(self):
        from DGXGateway.dgx_gateway.operations import OperationController
        from DGXGateway.dgx_gateway.store import OperationStore

        events = []
        adapter = FakeAdapter("h3", events, state="HEALTHY")
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = OperationStore(Path(temp.name) / "state.sqlite3")
        operation = store.create_operation(
            "op-profile", "h3", "h3", "key-profile",
            target_profile="ref2va", source_profile="fl2va",
        )
        controller = OperationController(
            store=store,
            adapters={"h3": adapter},
            lease_factory=lambda operation_id: FakeLease(events, operation_id),
            acceptance_failure_model="h3",
        )

        controller.execute(operation.operation_id)

        self.assertEqual(store.get_operation(operation.operation_id).outcome, "ROLLED_BACK")
        self.assertEqual(adapter.started_profiles, ["ref2va", "fl2va"])

    def test_unknown_source_stop_enters_recovery_required_without_target_start_or_release(self):
        events = []
        source = FakeAdapter("h3", events, state="HEALTHY", ambiguous_stop=True)
        target = FakeAdapter("deepseek", events)
        controller, store, operation = self.make(source, target, events)

        controller.execute(operation.operation_id)

        final = store.get_operation(operation.operation_id)
        self.assertEqual(final.phase, "RECOVERY_REQUIRED")
        self.assertEqual(final.outcome, "RECOVERY_REQUIRED")
        self.assertNotIn("deepseek.start", events)
        self.assertNotIn("lease.release", events)

    def test_release_failure_preserves_committed_outcome_and_retains_lease(self):
        events = []
        source = FakeAdapter("h3", events, state="HEALTHY")
        target = FakeAdapter("deepseek", events)
        controller, store, operation = self.make(
            source, target, events, fail_release=True
        )

        controller.execute(operation.operation_id)

        final = store.get_operation(operation.operation_id)
        self.assertEqual(final.phase, "RECOVERY_REQUIRED")
        self.assertEqual(final.outcome, "COMMITTED")
        self.assertIn(operation.operation_id, controller.recovery_leases)

    def test_cancel_after_source_stop_restores_source_and_releases(self):
        events = []
        source = FakeAdapter("h3", events, state="HEALTHY")
        target = FakeAdapter("deepseek", events)
        controller, store, operation = self.make(source, target, events)
        original_proof = source.stopped_proof

        def stopped_proof_and_cancel(context):
            result = original_proof(context)
            store.request_cancel(operation.operation_id)
            return result

        source.stopped_proof = stopped_proof_and_cancel
        controller.execute(operation.operation_id)

        final = store.get_operation(operation.operation_id)
        self.assertEqual(final.phase, "RELEASED")
        self.assertEqual(final.outcome, "CANCELED")
        self.assertNotIn("deepseek.start", events)
        self.assertLess(events.index("h3.start"), events.index("lease.release"))

    def test_success_proves_third_model_stopped_before_commit(self):
        from DGXGateway.dgx_gateway.operations import OperationController
        from DGXGateway.dgx_gateway.store import OperationStore

        events = []
        source = FakeAdapter("h3", events, state="HEALTHY")
        target = FakeAdapter("deepseek", events)
        third = FakeAdapter("music3", events)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = OperationStore(Path(temp.name) / "state.sqlite3")
        operation = store.create_operation("op-third", "h3", "deepseek", "key-third")
        controller = OperationController(
            store=store,
            adapters={"h3": source, "deepseek": target, "music3": third},
            lease_factory=lambda operation_id: FakeLease(events, operation_id),
        )

        controller.execute(operation.operation_id)

        final = store.get_operation(operation.operation_id)
        self.assertEqual(final.outcome, "COMMITTED")
        self.assertIn("music3.stopped-proof", events)
        self.assertLess(events.index("music3.stopped-proof"), events.index("lease.release"))

    def test_cancel_after_target_health_retires_target_and_restores_source(self):
        events = []
        source = FakeAdapter("h3", events, state="HEALTHY")
        target = FakeAdapter("deepseek", events)
        controller, store, operation = self.make(source, target, events)
        original_health = target.health

        def health_and_cancel(context):
            result = original_health(context)
            store.request_cancel(operation.operation_id)
            return result

        target.health = health_and_cancel
        controller.execute(operation.operation_id)

        final = store.get_operation(operation.operation_id)
        self.assertEqual(final.phase, "RELEASED")
        self.assertEqual(final.outcome, "ROLLED_BACK")
        self.assertLess(events.index("deepseek.retire"), events.index("h3.start"))


if __name__ == "__main__":
    unittest.main()
