"""Frozen admission oracle: graceful close must never escalate on uncertainty."""
import importlib.util
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("text_fence_recovery", ROOT / "scripts/recover-deepseek-v4-flash.py")
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
CID = "c" * 64


class FakeRunner:
    def __init__(self, fault=None):
        self.fault = fault
        self.calls = []
        self.polls = 0

    def run(self, *argv, **kw):
        self.calls.append(argv)
        if argv[:2] == ("systemctl", "show"):
            return "always" if self.fault == "restart" else "no"
        if argv[:2] == ("docker", "inspect"):
            return ("d" * 64 if self.fault == "identity" else CID) + "|/deepseek-v4-flash-gateway|true|no"
        if argv[:2] == ("docker", "exec"):
            return ""
        if argv[:2] == ("docker", "ps"):
            self.polls += 1
            if self.fault == "docker":
                raise RuntimeError("docker unavailable")
            if self.fault == "replaced":
                return "d" * 64
            return CID if self.fault == "timeout" or self.polls == 1 else ""
        if argv[0] == "ss":
            return "LISTEN" if self.fault == "listener" else ""
        if argv[:2] == ("docker", "events"):
            return json.dumps({"Actor": {"ID": CID, "Attributes": {"exitCode": "137" if self.fault == "crash" else "0"}}})
        raise AssertionError(argv)


class TextFenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.receipts = Path(self.tmp.name)
        for name, value in (("GATEWAY_CAPABILITY_DIR", self.receipts), ("ROOT_UID", os.getuid())):
            item = patch.object(recovery, name, value)
            item.start()
            self.addCleanup(item.stop)

    def call(self, runner):
        ticks = iter(range(100))
        with patch.object(recovery, "_require_root"):
            return recovery.gateway_fence("a" * 32, CID, runner, hostname="dgx-head",
                                          timeout=2, clock=lambda: next(ticks), sleep=lambda _: None)

    def test_graceful_exit_receipt_and_no_forced_stop(self):
        runner = FakeRunner()
        result = self.call(runner)
        self.assertTrue(result["admission_closed"])
        self.assertIn(("docker", "exec", CID, "nginx", "-s", "quit"), runner.calls)
        self.assertFalse(any("stop" in c or "kill" in c or "rm" in c for c in runner.calls))

    def test_all_ambiguous_boundaries_retain_model(self):
        for fault in ("restart", "identity", "docker", "replaced", "timeout", "listener", "crash"):
            with self.subTest(fault=fault):
                for receipt in self.receipts.glob("*.json"): receipt.unlink()
                runner = FakeRunner(fault)
                with self.assertRaises(RuntimeError):
                    self.call(runner)
                self.assertFalse(any("stop" in c or "kill" in c or "rm" in c for c in runner.calls))

    def test_lost_closed_receipt_reply_is_idempotently_recovered(self):
        runner = FakeRunner()
        self.call(runner)
        runner.calls.clear()
        result = self.call(runner)
        self.assertTrue(result["graceful_exit"])
        self.assertFalse(any(c[:2] in (("docker", "exec"), ("docker", "events")) for c in runner.calls))

    def test_closed_after_intent_and_observation_failure_is_recoverable(self):
        runner = FakeRunner("listener")
        with self.assertRaisesRegex(RuntimeError, "listener"):
            self.call(runner)
        self.assertEqual(json.loads(next(self.receipts.glob("*.json")).read_text())["phase"], "INTENT")
        runner.fault = None
        self.assertTrue(self.call(runner)["admission_closed"])

    def test_absence_without_intent_is_not_a_graceful_proof(self):
        runner = FakeRunner(); runner.polls = 5
        with self.assertRaisesRegex(RuntimeError, "durable fence intent"):
            self.call(runner)
        self.assertFalse(any(c[:2] == ("docker", "exec") for c in runner.calls))

    def test_malformed_closed_receipts_fail_closed(self):
        record = {"schema_version": 1, "container_id": CID, "transaction_id": "a" * 32,
                  "since": "1700000000", "phase": "CLOSED", "exit_code": 0}
        path = self.receipts / ("fence-" + CID + ".json")
        for changes in ({"exit_code": 137}, {"exit_code": None}, {"exit_code": False},
                        {"transaction_id": "bad"}, {"since": "nan"}, {"since": "9999999999999"}):
            with self.subTest(changes=changes):
                path.write_text(json.dumps({**record, **changes})); path.chmod(0o600)
                with self.assertRaisesRegex(RuntimeError, "identity is invalid"):
                    self.call(FakeRunner())

    def test_same_container_restart_is_a_different_model_instance(self):
        from DGXGateway.dgx_gateway.adapters.deepseek import DeepSeekAdapter
        config = SimpleNamespace(nodes=[SimpleNamespace(role="head"), SimpleNamespace(role="worker")])
        adapter = DeepSeekAdapter(config)
        def inventory(node, argv, **kw):
            value = {"hostname": "dgx-head" if node.role == "head" else "dgx-worker", "user": "operator",
                     "bootId": "sameboot", "containers": [{"id": CID, "startedAt": "2026-09-08T00:00:00.000Z",
                     "restartCount": inventory.restart, "pid": 123 + inventory.restart, "running": True}],
                     "gateway": [CID] if node.role == "head" else [],
                     "upstream": "LISTEN 0 128 127.0.0.1:18888 0.0.0.0:*" if node.role == "head" else "",
                     "lan": "LISTEN" if node.role == "head" else ""}
            return SimpleNamespace(returncode=0, stdout=json.dumps(value))
        inventory.restart = 0
        adapter.ssh = SimpleNamespace(run=inventory)
        expected = adapter._text_runtime_identity(admission_open=True)
        inventory.restart = 1
        actual = adapter._text_runtime_identity(admission_open=True)
        self.assertNotEqual(expected["nodes"], actual["nodes"])
        adapter.text_fence_qualified = True
        adapter._controller = Mock()
        with self.assertRaisesRegex(RuntimeError, "runtime changed"):
            adapter.fence_text_requests(SimpleNamespace(guard=lambda: None), expected)
        adapter._controller.assert_not_called()

    def test_wrong_host_or_container_cannot_signal(self):
        for host, cid in (("dgx-worker", CID), ("dgx-head", "name")):
            runner = FakeRunner()
            with patch.object(recovery, "_require_root"), self.assertRaises((ValueError, RuntimeError)):
                recovery.gateway_fence("a" * 32, cid, runner, hostname=host)
            self.assertEqual(runner.calls, [])

    def test_adapter_requires_qualification_and_exact_model_identity(self):
        from DGXGateway.dgx_gateway.adapters.deepseek import DeepSeekAdapter
        adapter = DeepSeekAdapter(SimpleNamespace(deepseek_drain_timeout_seconds=0))
        ctx = SimpleNamespace(guard=Mock(), operation_id="a" * 32, lease_generation="b" * 32)
        expected = {"nodes": {"head": "h", "worker": "w"}, "gateway_container_id": CID}
        adapter._controller = Mock()
        with self.assertRaisesRegex(RuntimeError, "not qualified"):
            adapter.fence_text_requests(ctx, expected)
        adapter._controller.assert_not_called()
        adapter.text_fence_qualified = True
        adapter._text_runtime_identity = Mock(return_value={"nodes": expected["nodes"], "gateway_container_id": None})
        adapter._request_metrics = Mock(return_value=(0, 0))
        adapter._controller.return_value.production_fence_gateway.return_value = {"receipt": "proof"}
        result = adapter.fence_text_requests(ctx, expected)
        self.assertTrue(result["inflight_zero"])
        self.assertEqual(result["runtime_identity"], expected)
        adapter._text_runtime_identity.return_value = {"nodes": {"head": "replaced"}, "gateway_container_id": None}
        with self.assertRaisesRegex(RuntimeError, "runtime changed"):
            adapter.fence_text_requests(ctx, expected)

    def test_prefixed_parent_uses_stable_lease_bound_hex_transaction(self):
        from DGXGateway.dgx_gateway.adapters.deepseek import DeepSeekAdapter
        adapter = DeepSeekAdapter(SimpleNamespace(deepseek_drain_timeout_seconds=0))
        adapter.text_fence_qualified = True
        ctx = SimpleNamespace(guard=Mock(), operation_id="text-" + "a" * 32, lease_generation="b" * 32)
        expected = {"nodes": {"head": "h"}, "gateway_container_id": CID}
        adapter._text_runtime_identity = Mock(return_value={"nodes": expected["nodes"], "gateway_container_id": None})
        adapter._request_metrics = Mock(return_value=(0, 0))
        adapter._controller = Mock()
        adapter._controller.return_value.production_fence_gateway.return_value = {"receipt": "proof"}
        adapter.fence_text_requests(ctx, expected)
        first = adapter._controller.return_value.production_fence_gateway.call_args.args[0]
        self.assertRegex(first, r"^[a-f0-9]{32}$")
        adapter._controller.assert_called_with(ctx)
        adapter.fence_text_requests(ctx, expected)
        self.assertEqual(first, adapter._controller.return_value.production_fence_gateway.call_args.args[0])
        ctx.lease_generation = "c" * 32
        adapter.fence_text_requests(ctx, expected)
        self.assertNotEqual(first, adapter._controller.return_value.production_fence_gateway.call_args.args[0])

    def test_closed_admission_with_active_request_does_not_stop(self):
        from DGXGateway.dgx_gateway.adapters.deepseek import DeepSeekAdapter
        adapter = DeepSeekAdapter(SimpleNamespace(deepseek_drain_timeout_seconds=0))
        adapter.text_fence_qualified = True
        ctx = SimpleNamespace(guard=lambda: None, operation_id="a" * 32, lease_generation="b" * 32)
        expected = {"nodes": {"head": "h"}, "gateway_container_id": CID}
        adapter._text_runtime_identity = Mock(return_value={"nodes": expected["nodes"], "gateway_container_id": None})
        adapter._controller = Mock()
        adapter._request_metrics = Mock(return_value=(1, 0))
        adapter.stop = Mock()
        with self.assertRaises(TimeoutError):
            adapter.fence_text_requests(ctx, expected)
        adapter.stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
