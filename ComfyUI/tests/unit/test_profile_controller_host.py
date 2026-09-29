import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).parents[2] / "scripts/h3-profile-controller.py"
SPEC = importlib.util.spec_from_file_location("h3_profile_controller_host", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class ProfileControllerHostTests(unittest.TestCase):
    def test_run_can_stream_an_atomic_attestation_payload(self):
        result = MODULE.Authority.run(
            [sys.executable, "-c", "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())"],
            input_bytes=b"attestation",
        )
        self.assertEqual(result.stdout, b"attestation")

    def test_switch_from_stopped_is_an_explicit_quiesced_start(self):
        self.assertEqual(
            MODULE.Authority.lifecycle_command("switch", "10eros-fl2va-v1", "STOPPED"),
            "sudo -n /usr/local/sbin/minimax-h3-control start --profile 10eros-fl2va-v1 --llm-already-quiesced",
        )

    def test_gateway_borrowed_commands_preserve_llm_quiesce(self):
        self.assertEqual(
            MODULE.Authority.borrowed_lifecycle_command(
                "switch", "10eros-fl2va-v1", "STOPPED"
            ),
            "sudo -n /usr/local/sbin/minimax-h3-control start --profile 10eros-fl2va-v1 --llm-already-quiesced --preserve-llm-quiesce-on-failure",
        )
        self.assertEqual(
            MODULE.Authority.borrowed_lifecycle_command(
                "stop", "10eros-fl2va-v1", "READY"
            ),
            "sudo -n /usr/local/sbin/minimax-h3-control stop --preserve-llm-quiesce",
        )

    def test_borrowed_mutation_guards_outer_lease_without_acquire_or_release(self):
        class FakeAuthority(MODULE.Authority):
            def __init__(self):
                super().__init__(
                    pathlib.Path("/repo"), pathlib.Path("/lock"), owner="dgx-cluster-gateway"
                )
                self.events = []

            def queue_idle(self):
                return True

            def _borrowed_lock_snapshot(self, _token_path):
                self.events.append("guard")
                return {
                    "schema_version": 1,
                    "cluster_id": "dgx-head+dgx-worker",
                    "owner": "dgx-cluster-gateway",
                    "task_id": "op-1",
                    "purpose": "switch",
                    "token_sha256": "0" * 64,
                    "lease_generation": "1" * 32,
                    "expires_at": "2099-01-01T00:00:00Z",
                }

            def _sync_attestation(self, _value):
                self.events.append("sync")

            def _clear_attestation(self, _token_sha256):
                self.events.append("clear")

            def status(self):
                return {"state": "STOPPED", "profile": "10eros-fl2va-v1", "healthy": False}

            def ssh(self, target, command, **_kwargs):
                if command == "hostname; whoami":
                    hostname = "dgx-head" if target == MODULE.HEAD else "dgx-worker"
                    return subprocess.CompletedProcess([], 0, f"{hostname}\noperator\n".encode(), b"")
                self.events.append(command)
                return subprocess.CompletedProcess([], 0, b"READY\n", b"")

        with tempfile.TemporaryDirectory() as directory:
            token = pathlib.Path(directory) / "outer.token"
            token.write_text("capability\n")
            authority = FakeAuthority()
            result = authority.mutate_borrowed("switch", "10eros-fl2va-v1", token)
        self.assertEqual(result["state"], "STOPPED")
        self.assertNotIn("acquire", authority.events)
        self.assertNotIn("release", authority.events)
        self.assertIn("guard", authority.events)
        self.assertIn("clear", authority.events)

    def test_profile_operation_lease_covers_a_full_cold_start(self):
        self.assertEqual(MODULE.PROFILE_OPERATION_LEASE_SECONDS, 1800)

    def test_final_attestation_confirmation_retries_a_transient_ssh_failure(self):
        class FakeAuthority(MODULE.Authority):
            def __init__(self):
                super().__init__(pathlib.Path("/repo"), pathlib.Path("/lock"))
                self.calls = 0

            def _borrowed_lock_snapshot(self, _token_path):
                return {"token_sha256": "0" * 64}

            def _sync_attestation(self, _value):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired("ssh", 30)

        authority = FakeAuthority()
        authority._confirm_borrowed_attestation(
            pathlib.Path("/tmp/token"), attempts=2, delay_seconds=0
        )
        self.assertEqual(authority.calls, 2)

    def test_queue_idle_allows_the_current_control_job_but_no_pending_jobs(self):
        authority = MODULE.Authority(
            pathlib.Path("/repo"), pathlib.Path("/lock"), "http://192.0.2.37:8188/queue"
        )
        commands = []

        def run(argv, **_kwargs):
            commands.append(argv)
            return subprocess.CompletedProcess(
                [], 0, json.dumps({"queue_running": [[1, "control"]], "queue_pending": []}).encode(), b""
            )

        authority.run = run
        self.assertTrue(authority.queue_idle())
        self.assertEqual(commands, [["curl", "-fsS", "http://192.0.2.37:8188/queue"]])

    def test_acquired_lock_is_released_when_attestation_sync_fails(self):
        class FakeAuthority(MODULE.Authority):
            def __init__(self):
                super().__init__(pathlib.Path("/repo"), pathlib.Path("/lock"))
                self.lock_calls = []

            def queue_idle(self):
                return True

            def ssh(self, target, command, **_kwargs):
                hostname = "dgx-head" if target == MODULE.HEAD else "dgx-worker"
                return subprocess.CompletedProcess([], 0, f"{hostname}\noperator\n".encode(), b"")

            def _lock(self, command, token_path, *extra):
                self.lock_calls.append(command)
                return {
                    "schema_version": 1,
                    "cluster_id": "dgx-head+dgx-worker",
                    "owner": MODULE.OWNER,
                    "task_id": "test",
                    "purpose": "test",
                    "token_sha256": "0" * 64,
                    "expires_at": "2099-01-01T00:00:00Z",
                }

            def _sync_attestation(self, _value):
                raise RuntimeError("sync failed")

            def _clear_attestation(self, token_sha256):
                assert token_sha256 == "0" * 64
                self.lock_calls.append("clear")

        authority = FakeAuthority()
        with self.assertRaisesRegex(RuntimeError, "sync failed"):
            authority.mutate("switch", "heretic-ref2va-v1")
        self.assertEqual(authority.lock_calls, ["acquire", "clear", "release"])

    def test_lock_is_not_released_when_remote_attestation_cleanup_fails(self):
        class FakeAuthority(MODULE.Authority):
            def __init__(self):
                super().__init__(pathlib.Path("/repo"), pathlib.Path("/lock"))
                self.lock_calls = []

            def queue_idle(self):
                return True

            def ssh(self, target, command, **_kwargs):
                hostname = "dgx-head" if target == MODULE.HEAD else "dgx-worker"
                return subprocess.CompletedProcess([], 0, f"{hostname}\noperator\n".encode(), b"")

            def _lock(self, command, token_path, *extra):
                self.lock_calls.append(command)
                return {
                    "schema_version": 1, "cluster_id": "dgx-head+dgx-worker",
                    "owner": MODULE.OWNER, "task_id": "test", "purpose": "test",
                    "token_sha256": "1" * 64, "expires_at": "2099-01-01T00:00:00Z",
                }

            def _sync_attestation(self, _value):
                raise RuntimeError("sync failed")

            def _clear_attestation(self, _token_sha256):
                self.lock_calls.append("clear")
                raise RuntimeError("clear failed")

        authority = FakeAuthority()
        with self.assertRaisesRegex(RuntimeError, "clear failed"):
            authority.mutate("switch", "heretic-ref2va-v1")
        self.assertEqual(authority.lock_calls, ["acquire", "clear"])


if __name__ == "__main__":
    unittest.main()
