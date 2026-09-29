#!/usr/bin/env python3
"""Unit tests for the mandatory Mac-side DeepSeek deployment authority."""

from __future__ import annotations

import importlib.util
import contextlib
import hashlib
import io
import json
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "control-deepseek-v4-flash-dspark.py"
BLACKBOX_AUTHORITY = ROOT / "testcase" / "deepseek-v4-flash-dspark" / "blackbox-authority-lock.json"
LOCK_MATRIX = ROOT / "testcase" / "deepseek-v4-flash-dspark" / "lock-mutation-matrix.json"
SPEC = importlib.util.spec_from_file_location("deepseek_controller", MODULE_PATH)
controller = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(controller)


class FakeRunner:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append((tuple(map(str, argv)), kwargs))
        if self.results:
            return self.results.pop(0)
        return controller.Completed(0, "{}\n", "")

    def run_supervised(self, argv, **kwargs):
        self.calls.append((tuple(map(str, argv)), kwargs))
        check = kwargs.get("lease_check")
        if check:
            check()
        if self.results:
            return self.results.pop(0)
        return controller.Completed(0, "{}\n", "")


def completed(value):
    return controller.Completed(0, json.dumps(value) + "\n", "")


def locked_matrix():
    authority = json.loads(BLACKBOX_AUTHORITY.read_text(encoding="utf-8"))
    relative = str(LOCK_MATRIX.relative_to(ROOT))
    rows = [row for row in authority["oracle_assets"] if row["path"] == relative]
    if len(rows) != 1:
        raise AssertionError("lock mutation matrix is not uniquely authority-locked")
    raw = LOCK_MATRIX.read_bytes()
    if hashlib.sha256(raw).hexdigest() != rows[0]["sha256"]:
        raise AssertionError("lock mutation matrix drifted from black-box authority")
    return json.loads(raw)


class LockAuthorityTests(unittest.TestCase):
    def test_canonical_lock_root_is_not_configurable(self):
        self.assertEqual(
            controller.LOCK_ROOT,
            Path("/Users/operator/.codex/locks/dgx-spark-two-node"),
        )
        parser = controller.build_parser()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["--root", "/tmp/not-canonical", "status"])

    def test_repo_contained_token_and_journal_are_rejected(self):
        with self.assertRaises(controller.ControllerError):
            controller.assert_private_external_path(ROOT / "secret.token", "token")
        with self.assertRaises(controller.ControllerError):
            controller.assert_private_external_path(ROOT / "journal.jsonl", "journal")

    def test_lock_cli_never_passes_root_override(self):
        runner = FakeRunner([completed({"state": "UNLOCKED"})])
        authority = controller.LockAuthority(
            runner=runner,
            token_file=Path("/Users/operator/.codex/locks/test-deepseek.token"),
        )
        self.assertEqual(authority.status()["state"], "UNLOCKED")
        argv = runner.calls[0][0]
        self.assertNotIn("--root", argv)
        self.assertEqual(argv[-1], "status")

    def test_recording_runner_is_side_effect_free(self):
        runner = controller.RecordingRunner([completed({"state": "UNLOCKED"})])
        authority = controller.LockAuthority(
            runner=runner,
            token_file=Path("/Users/operator/.codex/locks/record-only.token"),
        )
        self.assertEqual(authority.status()["state"], "UNLOCKED")
        self.assertEqual(len(runner.calls), 1)
        self.assertFalse(Path("/Users/operator/.codex/locks/record-only.token").exists())

    def test_active_other_owner_stale_and_unknown_fail_closed(self):
        for state in ("ACTIVE", "STALE", "UNKNOWN"):
            runner = FakeRunner([completed({"state": state, "owner": "minimax-h3-two-node"})])
            authority = controller.LockAuthority(
                runner=runner,
                token_file=Path("/Users/operator/.codex/locks/test-deepseek.token"),
            )
            with self.subTest(state=state), self.assertRaises(controller.LockUnavailable):
                authority.acquire("task", "test")

    def test_acquire_uses_fixed_owner_and_external_mode_0600_token(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            token = Path(tmp) / "authority.token"
            class AcquireRunner(FakeRunner):
                def run(self, argv, **kwargs):
                    result = super().run(argv, **kwargs)
                    if "acquire" in argv:
                        token.write_text("test-token\n", encoding="utf-8")
                        token.chmod(0o600)
                    return result

            runner = AcquireRunner(
                [
                    completed({"state": "UNLOCKED"}),
                    completed({"state": "ACTIVE", "owner": controller.OWNER}),
                ]
            )
            authority = controller.LockAuthority(runner=runner, token_file=token)
            authority.acquire("task-1", "bounded test")
            argv = runner.calls[1][0]
            self.assertIn(controller.OWNER, argv)
            self.assertIn(str(token.resolve()), argv)

    def test_release_performs_fresh_guard_first(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            original_history = controller.LOCK_HISTORY
            self.addCleanup(setattr, controller, "LOCK_HISTORY", original_history)
            controller.LOCK_HISTORY = Path(tmp)
            receipt = Path(tmp) / "release-receipt.json"
            receipt.write_text('{"state":"UNLOCKED"}\n', encoding="utf-8")
            digest = hashlib.sha256(receipt.read_bytes()).hexdigest()
            runner = FakeRunner(
                [
                    completed({"state": "ACTIVE", "owner": controller.OWNER}),
                    completed({
                        "state": "UNLOCKED",
                        "released_owner": controller.OWNER,
                        "release_operation_id": "1" * 32,
                        "release_receipt": str(receipt),
                        "release_receipt_sha256": digest,
                    }),
                ]
            )
            authority = controller.LockAuthority(
                runner=runner,
                token_file=Path(tmp) / "deleted.token",
            )
            authority.release()
            self.assertEqual(runner.calls[0][0][2], "guard")
            self.assertEqual(runner.calls[1][0][2], "release")

    def test_supervisor_kills_only_its_process_group_when_lease_is_lost(self):
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])

        def stop_unrelated():
            if unrelated.poll() is None:
                unrelated.terminate()
                unrelated.wait(timeout=2)

        self.addCleanup(stop_unrelated)
        started = time.monotonic()
        with self.assertRaises(controller.LeaseLost):
            controller.SubprocessRunner().run_supervised(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                input_text=None,
                timeout=20,
                poll_seconds=0.01,
                lease_check=lambda: (_ for _ in ()).throw(controller.LeaseLost("lost")),
            )
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsNone(unrelated.poll())


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)

    def test_receipt_has_exact_v1_binding(self):
        receipt = controller.make_receipt(
            role="worker",
            verb="start",
            resource=controller.PROJECT,
            argv=["fixed", "argv"],
            prior_state_sha256="a" * 64,
            transaction_id="b" * 32,
            lease_generation="c" * 32,
            now=self.now,
        )
        self.assertEqual(receipt["schema"], "deepseek-dspark-receipt-v1")
        self.assertEqual(receipt["host"], controller.HOSTS["worker"].hostname)
        self.assertEqual(receipt["role"], "worker")
        self.assertEqual(receipt["verb"], "start")
        self.assertEqual(receipt["resource"], controller.PROJECT)
        self.assertEqual(receipt["argv"], ["fixed", "argv"])
        self.assertEqual(receipt["prior_state_sha256"], "a" * 64)
        self.assertEqual(receipt["lease_generation"], "c" * 32)
        self.assertNotEqual(receipt["nonce"], "")
        self.assertGreater(receipt["not_after"], receipt["issued_at"])

    def test_journal_is_append_only_and_mode_0600(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            journal = controller.Journal(Path(tmp) / "events.jsonl")
            journal.append({"event": "ONE"})
            journal.append({"event": "TWO"})
            rows = [json.loads(line) for line in journal.path.read_text().splitlines()]
            self.assertEqual([row["event"] for row in rows], ["ONE", "TWO"])
            self.assertEqual(stat.S_IMODE(journal.path.stat().st_mode), 0o600)


class FenceDenied(controller.LeaseLost):
    pass


class RemoteEffectRecorder(FakeRunner):
    """Permit local/read-only setup while counting only delegated mutations."""

    def __init__(self, authority=None):
        super().__init__()
        self.authority = authority
        self.remote_effect_count = 0

    def run_supervised(self, argv, **kwargs):
        self.calls.append((tuple(map(str, argv)), kwargs))
        if self.authority is not None and self.authority.state == "LEASE_LOST_DURING":
            self.authority.lost = True
        check = kwargs.get("lease_check")
        if check:
            check()
        self.remote_effect_count += 1
        return controller.Completed(0, '{"terminal":"succeeded"}\n', "")

    def run(self, argv, **kwargs):
        self.calls.append((tuple(map(str, argv)), kwargs))
        command = tuple(map(str, argv))
        if command and command[0] == "ssh":
            remote = command[-1]
            if "hostname; whoami" in remote:
                hostname = "dgx-worker" if "192.0.2.31" in command else "dgx-head"
                return controller.Completed(0, f"{hostname}\noperator\n", "")
            if "--inspect-state" in remote:
                return completed({"prior_state_sha256": "a" * 64})
        return controller.Completed(0, "{}\n", "")


class NegativeStateAuthority:
    ACQUIRE_DENIED = {"NO_LOCK", "ACTIVE_OTHER_OWNER", "STALE", "UNKNOWN"}
    GUARD_DENIED = {
        "WRONG_OWNER",
        "WRONG_TOKEN",
        "WRONG_GENERATION",
        "LEASE_LOST_BEFORE",
    }

    def __init__(self, state):
        self.state = state
        self.lost = False
        self.acquires = 0

    def acquire(self, *_args, **_kwargs):
        self.acquires += 1
        if self.state in self.ACQUIRE_DENIED:
            raise controller.LockUnavailable(f"isolated lock rejected {self.state}")
        return {
            "state": "ACTIVE",
            "owner": controller.OWNER,
            "lease_generation": "b" * 32,
        }

    def guard(self):
        if self.state in self.GUARD_DENIED or self.lost:
            raise controller.LeaseLost(f"isolated guard rejected {self.state}")
        return {
            "state": "ACTIVE",
            "owner": controller.OWNER,
            "lease_generation": "b" * 32,
        }

    heartbeat = guard

    def release(self):
        return {"state": "UNLOCKED", "released_owner": controller.OWNER}


class MutationMatrixFenceTests(unittest.TestCase):
    PUBLIC_PATHS = {
        "prepare-artifacts",
        "bootstrap-executor",
        "bootstrap-input-bundle",
        "install-pinned-source",
        *(f"artifact {verb}" for verb in controller.ARTIFACT_VERBS if verb != "record-license-acceptance"),
        "record-license-acceptance",
        "record-node-identities",
        "install-service-assets",
        "qualify-fabric-consumer",
        "start",
        "stop",
        "cutover",
        "rollback",
    }

    def _cluster(self, tmp, runner, authority=None):
        return controller.ClusterController(
            authority=authority or mock.Mock(),
            runner=runner,
            journal=controller.Journal(Path(tmp) / "journal.jsonl"),
            task_id="isolated-lock-matrix",
        )

    @staticmethod
    def _invoke(cluster, command_path, secret):
        if command_path == "prepare-artifacts":
            return cluster.prepare_artifacts(
                gateway_secret=secret, operator="operator", accepted=True
            )
        if command_path == "bootstrap-executor":
            return cluster.bootstrap_executor()
        if command_path == "bootstrap-input-bundle":
            return cluster.bootstrap_input_bundle()
        if command_path == "install-pinned-source":
            return cluster.install_pinned_source()
        if command_path.startswith("artifact "):
            return cluster.artifact(command_path.split(" ", 1)[1])
        if command_path == "record-license-acceptance":
            return cluster.record_license_acceptance("operator", True)
        if command_path == "record-node-identities":
            return cluster.record_node_identities()
        if command_path == "install-service-assets":
            return cluster.install_service_assets(secret)
        if command_path == "qualify-fabric-consumer":
            return cluster.qualify_fabric_consumer()
        if command_path == "start":
            return cluster.start()
        if command_path == "stop":
            return cluster.stop()
        if command_path == "cutover":
            return cluster.cutover(None, secret)
        if command_path == "rollback":
            return cluster.rollback("c" * 32)
        raise AssertionError(f"unmapped public mutation surface: {command_path}")

    def test_hash_locked_public_surface_map_all_enters_one_fence_before_effect(self):
        matrix = locked_matrix()
        required = set(matrix["required_public_command_paths"])
        self.assertEqual(required, self.PUBLIC_PATHS)
        self.assertEqual(set(matrix["read_only_exclusions"]), {"preflight-fabric"})
        mutation_rows = {
            row["command_path"]: row
            for row in matrix["mutations"]
            if "command_path" in row
        }
        internal_rows = {
            row["internal_mutation"]: row
            for row in matrix["mutations"]
            if "internal_mutation" in row
        }
        self.assertEqual(set(mutation_rows), required)
        self.assertEqual(set(internal_rows), set(matrix["required_internal_mutations"]))
        negative_states = set(matrix["negative_states"])
        for row in (*mutation_rows.values(), *internal_rows.values()):
            self.assertEqual(set(row["negative_states"]), negative_states)
            self.assertEqual(row["remote_effect_count"], 0)

        for command_path in sorted(required):
            with self.subTest(command_path=command_path), tempfile.TemporaryDirectory(dir="/tmp") as tmp:
                runner = RemoteEffectRecorder()
                cluster = self._cluster(tmp, runner)
                fence_calls = []

                @contextlib.contextmanager
                def denied_fence(*args, **kwargs):
                    fence_calls.append((args, kwargs))
                    raise FenceDenied(command_path)
                    yield  # pragma: no cover

                secret = Path(tmp) / "gateway.secret"
                with (
                    mock.patch.object(cluster, "_lease", denied_fence),
                    mock.patch.object(cluster, "preflight_fabric", return_value={"lock": "UNLOCKED"}),
                    mock.patch.object(cluster, "_ensure_gateway_secret", return_value=secret),
                    mock.patch.object(cluster, "_pinned_source_snapshot", return_value=("{}", "a" * 64)),
                ):
                    with self.assertRaises(FenceDenied):
                        self._invoke(cluster, command_path, secret)
                self.assertEqual(len(fence_calls), 1)
                self.assertEqual(runner.remote_effect_count, 0)
                self.assertEqual(mutation_rows[command_path]["remote_effect_count"], 0)

    def test_locally_rejected_artifact_alias_never_enters_fence_or_has_effect(self):
        matrix = locked_matrix()
        self.assertEqual(len(matrix["locally_rejected_public_aliases"]), 1)
        rejected = matrix["locally_rejected_public_aliases"][0]
        self.assertEqual(rejected["command_path"], "artifact record-license-acceptance")
        self.assertEqual(rejected["rejected_before"], "lease-acquisition")
        self.assertEqual(rejected["remote_effect_count"], 0)
        self.assertIs(rejected["all_negative_states_required"], True)
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            runner = RemoteEffectRecorder()
            cluster = self._cluster(tmp, runner)
            with mock.patch.object(cluster, "_lease") as fence:
                with self.assertRaises(controller.PlanGateError):
                    cluster.artifact("record-license-acceptance")
            fence.assert_not_called()
            self.assertEqual(runner.remote_effect_count, 0)

    def test_internal_mutations_use_guarded_dispatch_or_guarded_release(self):
        matrix = locked_matrix()
        internal = set(matrix["required_internal_mutations"])
        delegated = {
            "fault-worker-loss": ("worker", "fault-worker-loss"),
            "reboot-worker": ("worker", "reboot"),
            "reboot-head": ("head", "reboot"),
            "refresh-fallback": ("head", "refresh-fallback"),
        }
        self.assertEqual(internal, set(delegated) | {"release"})
        for name, (role, verb) in delegated.items():
            with self.subTest(internal_mutation=name), tempfile.TemporaryDirectory(dir="/tmp") as tmp:
                runner = RemoteEffectRecorder()
                cluster = self._cluster(tmp, runner)
                with mock.patch.object(
                    cluster, "_guard", side_effect=controller.LeaseLost(name)
                ):
                    with self.assertRaises(controller.LeaseLost):
                        cluster._mutate(role, verb, "d" * 32)
                self.assertEqual(runner.remote_effect_count, 0)

    def test_all_nine_negative_states_fail_before_a_remote_effect(self):
        matrix = locked_matrix()
        states = set(matrix["negative_states"])
        self.assertEqual(
            states,
            {
                "NO_LOCK",
                "WRONG_OWNER",
                "WRONG_TOKEN",
                "WRONG_GENERATION",
                "ACTIVE_OTHER_OWNER",
                "STALE",
                "UNKNOWN",
                "LEASE_LOST_BEFORE",
                "LEASE_LOST_DURING",
            },
        )
        for state in sorted(states):
            with self.subTest(state=state), tempfile.TemporaryDirectory(dir="/tmp") as tmp:
                authority = NegativeStateAuthority(state)
                runner = RemoteEffectRecorder(authority)
                cluster = self._cluster(tmp, runner, authority)
                with self.assertRaises(controller.ControllerError):
                    cluster.stop()
                self.assertEqual(authority.acquires, 1)
                self.assertEqual(runner.remote_effect_count, 0)


if __name__ == "__main__":
    unittest.main()
