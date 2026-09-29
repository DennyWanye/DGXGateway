import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from DGXGateway.dgx_gateway.adapters.h3_ring import (
    H3RingAdapter,
    MODEL_ID,
    PROFILES,
    RANKS,
    docker_run_detached,
)
from DGXGateway.dgx_gateway.operations import OperationContext
from DGXGateway.dgx_gateway.ssh import CommandResult


def ok(stdout: str = "") -> CommandResult:
    return CommandResult(0, stdout, "")


def fail(stderr: str = "No such container", code: int = 1) -> CommandResult:
    return CommandResult(code, "", stderr)


class H3RingAdapterTests(unittest.TestCase):
    def setUp(self):
        self.ssh = Mock()
        self.adapter = H3RingAdapter(SimpleNamespace(), ssh=self.ssh)
        self.context = OperationContext("operation", Path("/tmp/token"), "generation", lambda: None)

    def _inspect_map(self, states: dict[int, str], *, api_up: bool = True) -> None:
        def run(node, argv, timeout=10):
            if argv[0] == "docker" and argv[1] == "inspect":
                rank = next(spec.rank for spec in RANKS if spec.node.address == node.address)
                state = states[rank]
                if state == "absent":
                    return fail()
                if state == "ssh":
                    return fail("ssh failed", 255)
                label = "fl2va-official p0-operation"
                return ok(f"true {label}\n" if state == "running" else f"false {label}\n")
            if argv[:2] == ["sh", "-c"] and argv[-1].startswith("hostname"):
                spec = next(item for item in RANKS if item.node.address == node.address)
                return ok(f"{spec.node.hostname}\n{spec.node.user}\n")
            if argv[0] == "ss" or (argv[0] == "sh" and "30021" in argv[-1]):
                return ok("LISTEN 127.0.0.1:30021\n" if api_up else "")
            if argv[0] == "python3" and "profile_node.py" in argv[1] and argv[2] == "render":
                rank = next(spec.rank for spec in RANKS if spec.node.address == node.address)
                return ok(
                    f"docker run --rm --name h3-ring-serve-r{rank} local/h3-ring:e970453b433e sglang serve\n"
                )
            if argv[0] == "docker" and argv[1] == "run":
                return ok("cid\n")
            if argv[0] == "docker" and argv[1] in {"rm", "stop"}:
                return ok()
            if argv[0] == "docker" and argv[1] == "logs":
                return ok("Model path: /models/h3/FL2VA/FL2VA\nFileNotFoundError: FL2VA/FL2VA\n")
            raise AssertionError(argv)

        self.ssh.run.side_effect = run

    def test_model_id_is_new_four_node_identity(self):
        self.assertEqual(MODEL_ID, "h3-ring4")
        self.assertNotEqual(MODEL_ID, "minimax-h3")
        self.assertEqual(self.adapter.profiles, PROFILES)
        self.assertEqual(PROFILES[0], "fl2va-official")

    def test_all_absent_is_stopped(self):
        self._inspect_map({0: "absent", 1: "absent", 2: "absent", 3: "absent"})
        self.assertEqual(self.adapter.observe().state, "STOPPED")

    def test_four_running_with_api_is_healthy(self):
        self._inspect_map({0: "running", 1: "running", 2: "running", 3: "running"})
        observed = self.adapter.observe()
        self.assertEqual(observed.state, "HEALTHY")
        self.assertEqual(observed.profile, "fl2va-official")

    def test_mixed_ranks_are_partial(self):
        self._inspect_map({0: "running", 1: "absent", 2: "running", 3: "absent"})
        self.assertEqual(self.adapter.observe().state, "PARTIAL")

    def test_ssh_failure_is_unknown_not_stopped(self):
        self._inspect_map({0: "ssh", 1: "absent", 2: "absent", 3: "absent"})
        self.assertEqual(self.adapter.observe().state, "UNKNOWN")

    def test_start_renders_then_detaches_workers_first(self):
        self._inspect_map({0: "absent", 1: "absent", 2: "absent", 3: "absent"})
        self.adapter.start(self.context, "fl2va-official")
        renders = [
            call
            for call in self.ssh.run.call_args_list
            if call.args[1] and call.args[1][0] == "python3" and "render" in call.args[1]
        ]
        runs = [
            call
            for call in self.ssh.run.call_args_list
            if call.args[1][:2] == ["docker", "run"]
        ]
        self.assertEqual(len(renders), 4)
        self.assertEqual(len(runs), 4)
        self.assertEqual(
            [call.args[0].role for call in renders],
            ["rank3", "rank2", "rank1", "rank0"],
        )
        self.assertEqual(
            [call.args[0].role for call in runs],
            ["rank3", "rank2", "rank1", "rank0"],
        )
        for call in renders:
            argv = call.args[1]
            self.assertIn("fl2va-official", argv)
            self.assertIn("--deployment-id", argv)
            self.assertIn("p0-operation", argv)
            self.assertTrue(argv[1].endswith("/h3-ring-kit-v2/scripts/profile_node.py"))
        for call in runs:
            argv = call.args[1]
            self.assertEqual(argv[1], "run")
            self.assertIn("-d", argv)
            self.assertNotIn("start_profile.sh", " ".join(argv))

    def test_unknown_profile_is_rejected_before_ssh(self):
        with self.assertRaises(ValueError):
            self.adapter.start(self.context, "10eros-fl2va-v1")
        self.ssh.run.assert_not_called()

    def test_two_node_h3_profile_is_rejected(self):
        with self.assertRaises(ValueError):
            self.adapter.start(self.context, "heretic-ref2va-v1")
        self.ssh.run.assert_not_called()

    def test_docker_run_detached_inserts_dash_d(self):
        argv = docker_run_detached("docker run --rm --name h3-ring-serve-r0 img sglang serve")
        self.assertEqual(argv[:5], ["docker", "run", "-d", "--name", "h3-ring-serve-r0"])
        self.assertNotIn("--rm", argv)

    def test_health_fails_fast_when_ranks_absent(self):
        self._inspect_map({0: "absent", 1: "absent", 2: "absent", 3: "absent"})
        proof = self.adapter.health(self.context)
        self.assertFalse(proof["healthy"])
        self.assertIn("absent", proof["reason"])

    def test_health_fails_fast_when_ranks_exited(self):
        self._inspect_map({0: "exited", 1: "exited", 2: "exited", 3: "exited"})
        proof = self.adapter.health(self.context)
        self.assertFalse(proof["healthy"])
        self.assertIn("exited", proof["reason"])
        self.assertEqual(proof["rank_states"], {"0": "exited", "1": "exited", "2": "exited", "3": "exited"})
        self.assertIn("FL2VA/FL2VA", proof["rank_logs"]["0"])

    def test_health_inspect_timeout_does_not_abort_warmup(self):
        self.ssh.run.side_effect = subprocess.TimeoutExpired("ssh", 30)
        with patch("DGXGateway.dgx_gateway.adapters.h3_ring.HEALTH_TIMEOUT_SECONDS", 0.05):
            with patch("DGXGateway.dgx_gateway.adapters.h3_ring.time.sleep", lambda *_: None):
                proof = self.adapter.health(self.context)
        self.assertFalse(proof["healthy"])
        self.assertIn("inspect deferred", proof["reason"])
        self.assertNotIn("absent", proof.get("reason", ""))
        self.assertNotIn("rank_states", proof)

    def test_stop_continues_after_rank_ssh_timeout(self):
        def run(node, argv, timeout=10):
            if node.role == "rank1" and argv[:2] == ["docker", "stop"]:
                raise subprocess.TimeoutExpired("ssh", 90)
            return ok()

        self.ssh.run.side_effect = run
        with self.assertRaises(RuntimeError) as raised:
            self.adapter.stop(self.context)
        self.assertIn("rank1 stop", str(raised.exception))
        removed = [
            call
            for call in self.ssh.run.call_args_list
            if call.args[1][:3] == ["docker", "rm", "-f"]
        ]
        self.assertEqual(len(removed), 4)

    def test_stop_removes_containers(self):
        self._inspect_map({0: "exited", 1: "exited", 2: "exited", 3: "exited"})
        self.adapter.stop(self.context)
        removed = [
            call
            for call in self.ssh.run.call_args_list
            if call.args[1][:3] == ["docker", "rm", "-f"]
        ]
        self.assertEqual(len(removed), 4)
        self.assertEqual(
            [call.args[0].role for call in removed],
            ["rank0", "rank1", "rank2", "rank3"],
        )


if __name__ == "__main__":
    unittest.main()
