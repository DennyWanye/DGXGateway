import importlib.util
from importlib.machinery import SourceFileLoader
import json
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROL_PATH = ROOT / "scripts/minimax-h3-two-node/minimax-h3-control"


def load_control():
    loader = SourceFileLoader("minimax_h3_control", str(CONTROL_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[loader.name] = module
    spec.loader.exec_module(module)
    return module


class FakeDriver:
    def __init__(self, fail_at=None, worker_reachable=True, current_memory=None):
        self.fail_at = fail_at
        self.worker_reachable = worker_reachable
        self.actions = []
        self.current_memory = current_memory or {"dgx-head": 1000, "dgx-worker": 900}

    def run(self, action):
        self.actions.append(action)
        if action == self.fail_at:
            raise RuntimeError(f"injected failure: {action}")
        if action == "check-worker":
            return self.worker_reachable
        if action == "measure-concurrent":
            return self.current_memory
        return True


class StateMachineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.control = load_control()

    def make_controller(self, driver, mode="exclusive", budget=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state_path = Path(tmp.name) / "state.json"
        controller = self.control.LifecycleController(
            driver=driver,
            state_path=state_path,
            mode=mode,
            budget=budget,
        )
        return controller, state_path

    def test_ready_state_attests_selected_profile_contract(self):
        driver = FakeDriver()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        contract = {
            "name": "heretic-ref2va-v1",
            "task_type": "ref2va",
            "runtime_image": "minimax-h3-two-node:ref2va-candidate",
            "expected_image_id": "sha256:" + "a" * 64,
            "model_path": "/models/h3-ref2va",
            "profile_tree_sha256": "b" * 64,
            "contract_sha256": "c" * 64,
        }
        controller = self.control.LifecycleController(
            driver=driver,
            state_path=Path(tmp.name) / "state.json",
            profile_contract=contract,
        )

        self.assertEqual("READY", controller.start())
        state = json.loads((Path(tmp.name) / "state.json").read_text())
        self.assertEqual("heretic-ref2va-v1", state["profile"])
        self.assertEqual("ref2va", state["task_type"])
        self.assertEqual(contract["expected_image_id"], state["runtime_image_id"])
        self.assertEqual(contract["profile_tree_sha256"], state["model_root_sha256"])
        self.assertEqual(contract["contract_sha256"], state["profile_contract_sha256"])

    def test_exclusive_start_and_stop_are_idempotent(self):
        driver = FakeDriver()
        controller, state_path = self.make_controller(driver)

        self.assertEqual(controller.start(), "READY")
        first_actions = list(driver.actions)
        self.assertEqual(controller.start(), "READY")
        self.assertEqual(driver.actions, first_actions)

        self.assertEqual(controller.stop(), "STOPPED")
        stopped_actions = list(driver.actions)
        self.assertEqual(controller.stop(), "STOPPED")
        self.assertEqual(driver.actions, stopped_actions)
        self.assertLess(
            stopped_actions.index("restore-llm"),
            stopped_actions.index("cleanup-worker"),
        )
        final_document = json.loads(state_path.read_text())
        self.assertEqual(final_document["state"], "STOPPED")
        history_states = [entry["state"] for entry in final_document["history"]]
        self.assertLess(history_states.index("DRAINING"), history_states.index("CLEANING_LOCAL"))
        self.assertLess(history_states.index("CLEANING_LOCAL"), history_states.index("RESTORING_LLM"))
        self.assertLess(history_states.index("RESTORING_LLM"), history_states.index("CLEANING_WORKER"))
        self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o644)

    def test_worker_is_stable_before_rank1_and_rank0(self):
        driver = FakeDriver()
        controller, _ = self.make_controller(driver)

        controller.start()

        self.assertLess(driver.actions.index("wait-worker-stable"), driver.actions.index("start-rank1"))
        self.assertLess(driver.actions.index("start-rank1"), driver.actions.index("wait-rank1-stable"))
        self.assertLess(driver.actions.index("wait-rank1-stable"), driver.actions.index("start-rank0"))

    def test_explicitly_quiesced_start_rechecks_that_llm_is_absent(self):
        driver = FakeDriver()
        controller, _ = self.make_controller(driver)

        self.assertEqual(controller.start(llm_already_quiesced=True), "READY")

        self.assertIn("assert-llm-quiesced", driver.actions)
        self.assertNotIn("quiesce-llm", driver.actions)
        self.assertLess(driver.actions.index("assert-llm-quiesced"), driver.actions.index("start-ray-head"))

    def test_every_start_stage_failure_restores_llm_before_worker_cleanup(self):
        stages = [
            "preflight",
            "quiesce-llm",
            "start-ray-head",
            "start-ray-worker",
            "wait-worker-stable",
            "start-rank1",
            "wait-rank1-stable",
            "start-rank0",
            "wait-ranks-ready",
            "start-api",
            "start-gateway",
            "wait-ready",
        ]
        for stage in stages:
            with self.subTest(stage=stage):
                driver = FakeDriver(fail_at=stage)
                controller, state_path = self.make_controller(driver)
                with self.assertRaises(self.control.LifecycleFailure):
                    controller.start()
                self.assertEqual(json.loads(state_path.read_text())["state"], "FAILED")
                self.assertIn("restore-llm", driver.actions)
                self.assertIn("cleanup-worker", driver.actions)
                self.assertLess(driver.actions.index("restore-llm"), driver.actions.index("cleanup-worker"))

    def test_gateway_borrowed_start_failure_keeps_llm_quiesced(self):
        driver = FakeDriver(fail_at="start-rank0")
        controller, state_path = self.make_controller(driver)

        with self.assertRaises(self.control.LifecycleFailure):
            controller.start(
                llm_already_quiesced=True,
                preserve_llm_quiesce_on_failure=True,
            )

        self.assertNotIn("restore-llm", driver.actions)
        self.assertIn("cleanup-worker", driver.actions)
        self.assertEqual(json.loads(state_path.read_text())["state"], "FAILED")

    def test_worker_cleanup_failure_does_not_prevent_local_llm_restore(self):
        driver = FakeDriver(fail_at="cleanup-worker")
        controller, state_path = self.make_controller(driver)
        controller._write_state("READY")

        with self.assertRaises(self.control.LifecycleFailure):
            controller.stop()

        self.assertLess(driver.actions.index("restore-llm"), driver.actions.index("cleanup-worker"))
        self.assertEqual(json.loads(state_path.read_text())["state"], "FAILED")

    def test_worker_loss_reports_unhealthy_without_single_node_fallback(self):
        driver = FakeDriver(worker_reachable=False)
        controller, _ = self.make_controller(driver)
        controller._write_state("READY")

        status = controller.status()

        self.assertFalse(status["healthy"])
        self.assertEqual(status["reason"], "worker_unreachable")
        self.assertNotIn("start-rank0", driver.actions)
        self.assertNotIn("single-node", " ".join(driver.actions))

    def test_dead_api_is_unhealthy_even_with_ready_state_and_reachable_worker(self):
        driver = FakeDriver(fail_at="check-runtime")
        controller, _ = self.make_controller(driver)
        controller._write_state("READY")
        status = controller.status()
        self.assertFalse(status["healthy"])
        self.assertFalse(status["runtime_ready"])
        self.assertEqual(status["reason"], "runtime_unhealthy")
        self.assertEqual(driver.actions, ["check-worker", "check-runtime"])

    def test_switch_from_failed_state_requires_llm_to_be_quiesced(self):
        driver = FakeDriver()
        controller, state_path = self.make_controller(driver)
        controller._write_state("FAILED")

        self.assertEqual(controller.switch(), "READY")

        self.assertIn("assert-llm-quiesced", driver.actions)
        self.assertNotIn("quiesce-llm", driver.actions)
        self.assertLess(driver.actions.index("restore-llm"), driver.actions.index("assert-llm-quiesced"))
        self.assertEqual(json.loads(state_path.read_text())["state"], "READY")

    def test_cross_profile_switch_retires_source_and_starts_target_then_rolls_back_source(self):
        source_driver = FakeDriver()
        target_driver = FakeDriver(fail_at="start-rank0")
        source, state_path = self.make_controller(source_driver)
        source._write_state("READY")
        target = self.control.LifecycleController(
            driver=target_driver,
            state_path=state_path,
            profile_contract={
                "name": "10eros-fl2va-v1",
                "task_type": "fl2va",
                "expected_image_id": "sha256:" + "a" * 64,
                "profile_tree_sha256": "b" * 64,
                "contract_sha256": "c" * 64,
            },
        )

        with self.assertRaises(self.control.LifecycleFailure):
            source.switch_to(target)

        self.assertIn("stop-rank0", source_driver.actions)
        self.assertIn("start-rank0", target_driver.actions)
        self.assertIn("start-rank0", source_driver.actions)
        self.assertEqual(json.loads(state_path.read_text())["state"], "READY")

    def test_double_failure_is_safe_only_after_both_nodes_and_llm_are_verified_stopped(self):
        source_driver = FakeDriver(fail_at="start-rank0")
        target_driver = FakeDriver(fail_at="start-rank0")
        source, state_path = self.make_controller(source_driver)
        source._write_state("READY")
        target = self.control.LifecycleController(
            driver=target_driver,
            state_path=state_path,
            profile_contract={"name": "target-profile"},
        )

        with self.assertRaisesRegex(self.control.LifecycleFailure, "rollback failed safely"):
            source.switch_to(target)

        self.assertEqual(json.loads(state_path.read_text())["state"], "FAILED_SAFE_STOPPED")
        self.assertEqual(
            source_driver.actions[-3:],
            ["assert-h3-stopped", "assert-worker-stopped", "assert-llm-quiesced"],
        )

    def test_double_failure_with_unverified_stop_is_unknown_not_safe(self):
        class VerificationFailDriver(FakeDriver):
            def run(self, action):
                self.actions.append(action)
                if action in {"start-rank0", "assert-worker-stopped"}:
                    raise RuntimeError(f"injected failure: {action}")
                if action == "check-worker":
                    return True
                return True

        source_driver = VerificationFailDriver()
        target_driver = FakeDriver(fail_at="start-rank0")
        source, state_path = self.make_controller(source_driver)
        source._write_state("READY")
        target = self.control.LifecycleController(
            driver=target_driver,
            state_path=state_path,
            profile_contract={"name": "target-profile"},
        )

        with self.assertRaisesRegex(self.control.LifecycleFailure, "stop state is unknown"):
            source.switch_to(target)

        self.assertEqual(json.loads(state_path.read_text())["state"], "FAILED_UNSAFE_UNKNOWN")


class ConcurrentBudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.control = load_control()

    def valid_document(self):
        now = datetime.now(timezone.utc)
        return {
            "schema_version": 1,
            "status": "calibrated",
            "created_at": (now - timedelta(minutes=1)).isoformat(),
            "expires_at": (now + timedelta(hours=1)).isoformat(),
            "combined_smoke_passed": True,
            "bindings": {
                "head_hostname": "dgx-head",
                "worker_hostname": "dgx-worker",
                "kernel": "kernel-hash",
                "driver": "driver-hash",
                "llm_model": "llm-hash",
                "runtime": "runtime-hash",
                "model": "model-hash",
                "profile": "profile-hash",
                "request_policy": "policy-hash",
            },
            "nodes": {
                "dgx-head": {
                    "a_idle_bytes": 1200,
                    "d_h3_bytes": 300,
                    "reserve_bytes": 200,
                    "llm_resident": True,
                    "d_llm_bytes": 250,
                },
                "dgx-worker": {
                    "a_idle_bytes": 1100,
                    "d_h3_bytes": 400,
                    "reserve_bytes": 200,
                    "llm_resident": False,
                    "d_llm_bytes": 0,
                },
            },
            "safety": {
                "oom_events_delta": 0,
                "swap_pressure_ok": True,
                "cpu_fallback": False,
                "psi_ok": True,
            },
        }

    def expected_bindings(self):
        return self.valid_document()["bindings"]

    def current_memory(self):
        return {"dgx-head": 1000, "dgx-worker": 900}

    def test_missing_budget_fails_closed(self):
        with self.assertRaises(self.control.AdmissionDenied):
            self.control.ConcurrentBudget(None).admit(self.expected_bindings(), self.current_memory())

    def test_expired_budget_fails_closed(self):
        document = self.valid_document()
        document["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with self.assertRaises(self.control.AdmissionDenied):
            self.control.ConcurrentBudget(document).admit(self.expected_bindings(), self.current_memory())

    def test_binding_mismatch_fails_closed(self):
        document = self.valid_document()
        expected = self.expected_bindings()
        expected["runtime"] = "different-runtime"
        with self.assertRaises(self.control.AdmissionDenied):
            self.control.ConcurrentBudget(document).admit(expected, self.current_memory())

    def test_insufficient_memory_fails_closed(self):
        document = self.valid_document()
        with self.assertRaises(self.control.AdmissionDenied):
            self.control.ConcurrentBudget(document).admit(
                self.expected_bindings(), {"dgx-head": 499, "dgx-worker": 900}
            )

    def test_unsafe_telemetry_fails_closed(self):
        document = self.valid_document()
        document["safety"]["oom_events_delta"] = 1
        with self.assertRaises(self.control.AdmissionDenied):
            self.control.ConcurrentBudget(document).admit(self.expected_bindings(), self.current_memory())

    def test_missing_live_memory_measurement_fails_closed(self):
        with self.assertRaises(self.control.AdmissionDenied):
            self.control.ConcurrentBudget(self.valid_document()).admit(self.expected_bindings(), None)

    def test_valid_budget_admits_without_quiescing_llm(self):
        document = self.valid_document()
        budget = self.control.ConcurrentBudget(document)
        driver = FakeDriver(current_memory=self.current_memory())
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        controller = self.control.LifecycleController(
            driver=driver,
            state_path=Path(tmp.name) / "state.json",
            mode="concurrent",
            budget=budget,
            expected_bindings=self.expected_bindings(),
        )

        self.assertEqual(controller.start(), "READY")
        self.assertNotIn("quiesce-llm", driver.actions)
        self.assertIn("admit-concurrent", driver.actions)


class RuntimePolicyTests(unittest.TestCase):
    def test_ray_memory_monitor_is_disabled_on_both_nodes(self):
        head = (ROOT / "scripts/minimax-h3-two-node/minimax-h3-runtime-action").read_text()
        worker = (ROOT / "scripts/minimax-h3-two-node/minimax-h3-worker-action").read_text()
        self.assertIn("RAY_memory_monitor_refresh_ms=0", head)
        self.assertIn("RAY_memory_monitor_refresh_ms=0", worker)


if __name__ == "__main__":
    unittest.main()
