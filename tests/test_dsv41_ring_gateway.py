import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from DGXGateway.dgx_gateway.adapters.dsv41_ring import (
    CANARY_TIMEOUT_SECONDS,
    Dsv41RingAdapter,
    OWNER_LABEL,
    PROFILES,
    RANKS,
    SERVED_MODEL,
    parse_request_metrics,
)
from DGXGateway.dgx_gateway.connection_view import active_connection_view
from DGXGateway.dgx_gateway.domain import ActiveModelClassification
from DGXGateway.dgx_gateway.operations import OperationContext
from DGXGateway.dgx_gateway.ssh import CommandResult


def ok(stdout: str = "") -> CommandResult:
    return CommandResult(0, stdout, "")


def fail(stderr: str = "No such container", code: int = 1) -> CommandResult:
    return CommandResult(code, "", stderr)


class ParseMetricsTests(unittest.TestCase):
    def test_reads_vllm_running_and_waiting(self):
        text = (
            "# HELP vllm:num_requests_running Running requests.\n"
            "vllm:num_requests_running{model=\"deepseek-v4.1-flash-ring\"} 0.0\n"
            "vllm:num_requests_waiting{model=\"deepseek-v4.1-flash-ring\"} 2.0\n"
        )
        self.assertEqual(parse_request_metrics(text), (0.0, 2.0))

    def test_rejects_incomplete_metrics(self):
        with self.assertRaises(RuntimeError):
            parse_request_metrics("vllm:num_requests_running 1\n")


class Dsv41RingAdapterTests(unittest.TestCase):
    def setUp(self):
        self.ssh = Mock()
        self.adapter = Dsv41RingAdapter(SimpleNamespace(), ssh=self.ssh)
        self.context = OperationContext("operation", Path("/tmp/token"), "generation", lambda: None)

    def _inspect_map(self, states: dict[int, str]) -> None:
        def run(node, argv, timeout=10):
            if argv[0] == "docker":
                rank = next(spec.rank for spec in RANKS if spec.node.address == node.address)
                state = states[rank]
                if state == "absent":
                    return fail()
                if state == "ssh":
                    return fail("ssh failed", 255)
                running = "true" if state == "running" else "false"
                stage = states.get(f"{rank}-stage", "smoke32k")
                return ok(f"{running} {OWNER_LABEL} {stage}\n")
            if argv[0] == "sh" and "v1/models" in argv[-1]:
                return ok('{"data":[{"id":"deepseek-v4.1-flash-ring"}]}')
            if argv[0] == "curl":
                return ok("vllm:num_requests_running 0\nvllm:num_requests_waiting 0\n")
            if argv[:2] == ["sh", "-c"] and argv[-1].startswith("hostname"):
                spec = next(item for item in RANKS if item.node.address == node.address)
                return ok(f"{spec.node.hostname}\n{spec.node.user}\n")
            if argv[0] == "python3":
                return ok("Launched\n")
            raise AssertionError(argv)

        self.ssh.run.side_effect = run

    def test_model_id_is_new_and_not_mia(self):
        self.assertEqual(self.adapter.model_id, "dsv41-ring-tp4")
        self.assertNotEqual(self.adapter.model_id, "mia-dsv41-ablit")
        self.assertEqual(SERVED_MODEL, "deepseek-v4.1-flash-ring")
        self.assertEqual(self.adapter.profiles, ("smoke32k", "text256k", "text512k"))
        self.assertEqual(PROFILES, self.adapter.profiles)

    def test_all_absent_is_stopped(self):
        self._inspect_map({0: "absent", 1: "absent", 2: "absent", 3: "absent"})
        self.assertEqual(self.adapter.observe().state, "STOPPED")

    def test_four_running_and_served_model_is_healthy(self):
        self._inspect_map({0: "running", 1: "running", 2: "running", 3: "running"})
        observed = self.adapter.observe()
        self.assertEqual(observed.state, "HEALTHY")
        self.assertEqual(observed.profile, "smoke32k")

    def test_mixed_ranks_are_partial(self):
        self._inspect_map({0: "running", 1: "absent", 2: "running", 3: "absent"})
        self.assertEqual(self.adapter.observe().state, "PARTIAL")

    def test_ssh_failure_is_unknown_not_stopped(self):
        self._inspect_map({0: "ssh", 1: "absent", 2: "absent", 3: "absent"})
        self.assertEqual(self.adapter.observe().state, "UNKNOWN")
        with self.assertRaises(RuntimeError):
            self.adapter.stopped_proof(self.context)

    def test_stopped_proof_requires_all_absent(self):
        self._inspect_map({0: "absent", 1: "absent", 2: "absent", 3: "absent"})
        proof = self.adapter.stopped_proof(self.context)
        self.assertTrue(proof["stopped"])
        self.assertEqual(proof["model_id"], "dsv41-ring-tp4")
        self.assertEqual(proof["lease_generation"], "generation")
        self.assertEqual(len(proof["sha256"]), 64)

    def test_start_checks_all_ranks_then_launches_workers_first(self):
        self._inspect_map({0: "absent", 1: "absent", 2: "absent", 3: "absent"})
        self.adapter.start(self.context)
        python = [call.args[1] for call in self.ssh.run.call_args_list if call.args[1][0] == "python3"]
        verifies = [
            call.args[0].role
            for call in self.ssh.run.call_args_list
            if call.args[1][:2] == ["sh", "-c"] and call.args[1][-1].startswith("hostname")
        ]
        self.assertEqual(verifies, ["rank0", "rank1", "rank2", "rank3"])
        starts = [argv for argv in python if argv[4] == "start"]
        self.assertEqual([argv[6] for argv in starts], ["3", "2", "1", "0"])
        self.assertTrue(all("--maintenance-approved" in argv for argv in starts))
        self.assertEqual(
            [call.args[0].role for call in self.ssh.run.call_args_list if call.args[1][0] == "python3" and call.args[1][4] == "start"],
            ["rank3", "rank2", "rank1", "rank0"],
        )

    def test_stop_is_head_first(self):
        self._inspect_map({0: "running", 1: "running", 2: "running", 3: "running"})
        self.adapter.stop(self.context)
        python = [call.args[1] for call in self.ssh.run.call_args_list if call.args[1][0] == "python3"]
        self.assertEqual([argv[4] for argv in python], ["stop", "stop", "stop", "stop"])
        self.assertEqual([argv[6] for argv in python], ["0", "1", "2", "3"])

    def test_unknown_profile_is_rejected_before_ssh(self):
        with self.assertRaises(ValueError):
            self.adapter.start(self.context, "256K")
        self.ssh.run.assert_not_called()

    def test_text256k_start_passes_stage_to_all_ranks(self):
        self._inspect_map({0: "absent", 1: "absent", 2: "absent", 3: "absent"})
        self.adapter.start(self.context, "text256k")
        starts = [
            call.args[1]
            for call in self.ssh.run.call_args_list
            if call.args[1][0] == "python3" and call.args[1][4] == "start"
        ]
        self.assertEqual(len(starts), 4)
        self.assertTrue(all(argv[-2:] == ["--stage", "text256k"] for argv in starts))
        self.assertEqual(self.adapter.last_profile, "text256k")

    def test_drain_waits_for_three_idle_samples(self):
        samples = iter([(1.0, 0.0), (0.0, 0.0), (0.0, 0.0), (0.0, 0.0)])
        self.adapter.observe = Mock(return_value=SimpleNamespace(state="HEALTHY"))
        self.adapter._metrics = Mock(side_effect=lambda: next(samples))
        with patch("DGXGateway.dgx_gateway.adapters.dsv41_ring.time.sleep"):
            self.adapter.drain(self.context)
        self.assertEqual(self.adapter._metrics.call_count, 4)

    def test_canary_timeout_is_longer_for_text256k_than_smoke(self):
        self.assertGreater(CANARY_TIMEOUT_SECONDS["text256k"], CANARY_TIMEOUT_SECONDS["smoke32k"])
        self.assertGreater(CANARY_TIMEOUT_SECONDS["text512k"], CANARY_TIMEOUT_SECONDS["text256k"])
        self.assertGreaterEqual(CANARY_TIMEOUT_SECONDS["text256k"], 600)

    def test_canary_uses_profile_timeout_in_request_and_ssh(self):
        self.adapter.last_profile = "text256k"
        self.ssh.run.return_value = ok("323\n")
        self.assertEqual(self.adapter._canary(), "323")
        argv = self.ssh.run.call_args.args[1]
        self.assertEqual(argv[:2], ["sh", "-c"])
        self.assertIn("timeout=600", argv[-1])
        self.assertIn("'max_tokens':256", argv[-1])
        self.assertEqual(self.ssh.run.call_args.kwargs["timeout"], 630)

    def test_health_retries_failed_canary_until_it_passes(self):
        self.adapter.last_profile = "text256k"
        self.adapter.observe = Mock(return_value=SimpleNamespace(state="HEALTHY", profile="text256k"))
        self.adapter._canary = Mock(side_effect=[RuntimeError("ring canary request failed"), "323"])
        with patch("DGXGateway.dgx_gateway.adapters.dsv41_ring.time.sleep"), patch(
            "DGXGateway.dgx_gateway.adapters.dsv41_ring.time.monotonic",
            side_effect=[0, 1, 2, 3],
        ):
            result = self.adapter.health(self.context)
        self.assertTrue(result["healthy"])
        self.assertTrue(result["canary_passed"])
        self.assertEqual(self.adapter._canary.call_count, 2)

    def test_health_keeps_retrying_wrong_canary_answer(self):
        self.adapter.last_profile = "text256k"
        self.adapter.observe = Mock(return_value=SimpleNamespace(state="HEALTHY", profile="text256k"))
        self.adapter._canary = Mock(side_effect=["not-yet", "323"])
        with patch("DGXGateway.dgx_gateway.adapters.dsv41_ring.time.sleep"), patch(
            "DGXGateway.dgx_gateway.adapters.dsv41_ring.time.monotonic",
            side_effect=[0, 1, 2, 3],
        ):
            result = self.adapter.health(self.context)
        self.assertTrue(result["healthy"])
        self.assertEqual(self.adapter._canary.call_count, 2)


class RingConnectionViewTests(unittest.TestCase):
    def test_publishes_loopback_tunnel_not_old_two_node_gateway(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "dsv41-ring.token"
            secret.write_text("fixture-" + "b" * 56 + "\n", encoding="utf-8")
            os.chmod(secret, 0o600)
            config = SimpleNamespace(
                deepseek_gateway_secret=Path(directory) / "unused.token",
                dsv41_ring_secret=secret,
                bind="192.0.2.37",
                music3_port=8188,
                nodes=(SimpleNamespace(role="head", address="192.0.2.30"),),
            )
            view = active_connection_view(
                config,
                ActiveModelClassification("ACTIVE", "dsv41-ring-tp4"),
            )
            self.assertTrue(view["available"])
            self.assertEqual(view["base_url"], "http://127.0.0.1:8891/v1")
            self.assertEqual(view["model_name"], "deepseek-v4.1-flash-ring")
            self.assertEqual(view["request_path"], "/chat/completions")
            self.assertNotIn("11434", view["base_url"])
            self.assertNotEqual(view["model_name"], "mia-dsv41-ablit")
            self.assertIn("192.0.2.41", view["credential_source"])
            self.assertEqual(view["api_key"], "fixture-" + "b" * 56)


if __name__ == "__main__":
    unittest.main()
