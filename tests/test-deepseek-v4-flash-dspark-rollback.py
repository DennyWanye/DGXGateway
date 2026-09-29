#!/usr/bin/env python3
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_recovery():
    path = ROOT / "scripts/recover-deepseek-v4-flash.py"
    spec = importlib.util.spec_from_file_location("deepseek_recovery", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeRunner:
    def __init__(self):
        self.calls = []

    def run(self, *argv, check=True):
        self.calls.append(tuple(argv))
        return ""


class FallbackContractTests(unittest.TestCase):
    def setUp(self):
        self.service = (ROOT / "systemd/deepseek-v4-flash-fallback.service").read_text()
        self.timer = (ROOT / "systemd/deepseek-v4-flash-fallback.timer").read_text()

    def test_timer_is_persistent_absolute_and_normally_disabled(self):
        self.assertRegex(self.timer, r"OnCalendar=\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC")
        self.assertIn("Persistent=true", self.timer)
        self.assertIn("AccuracySec=1s", self.timer)
        self.assertIn("WantedBy=timers.target", self.timer)

    def test_service_is_bounded_and_uses_shared_lifecycle_flock(self):
        self.assertIn("TimeoutStartSec=10min", self.service)
        self.assertIn(
            "/usr/bin/flock -x /run/lock/deepseek-v4-flash-fallback.lifecycle.lock",
            self.service,
        )
        self.assertIn("/usr/local/libexec/recover-deepseek-v4-flash", self.service)
        self.assertIn("Restart=no", self.service)
        self.assertNotIn("WantedBy=", self.service)

    def test_every_capability_mutation_uses_only_the_dedicated_fallback_mutex(self):
        source = (ROOT / "scripts/recover-deepseek-v4-flash.py").read_text()
        self.assertIn(
            'FALLBACK_LIFECYCLE_LOCK = pathlib.Path("/run/lock/deepseek-v4-flash-fallback.lifecycle.lock")',
            source,
        )
        self.assertNotIn(
            "/home/operator/.local/state/deepseek-v4-flash-dspark/lifecycle.lock",
            source,
        )
        helper = (ROOT / "scripts/run-deepseek-v4-flash-dspark.sh").read_text()
        for verb in ("arm-fallback", "refresh-fallback", "disarm-fallback", "cancel-fallback"):
            self.assertIn(verb, helper)
        self.assertIn("/run/lock/deepseek-v4-flash-fallback.lifecycle.lock", helper)

    def test_recovery_capability_is_atomic_and_transaction_bound(self):
        source = (ROOT / "scripts/recover-deepseek-v4-flash.py").read_text()
        self.assertIn("os.replace(ARMED, RUNNING)", source)
        self.assertIn('config.get("state") != "FALLBACK_ARMED"', source)
        self.assertIn('len(transaction_id) != 32', source)
        self.assertIn('committed.get("transaction_id") == config["transaction_id"]', source)

    def test_recovery_is_local_ordered_and_restores_both_preimages(self):
        recovery = load_recovery()
        runner = FakeRunner()
        oracles = []

        config = {
            "schema_version": 1,
            "state": "FALLBACK_ARMED",
            "transaction_id": "a" * 32,
            "head_hostname": "dgx-head",
            "old_model": "qwen38-q4-256k:latest",
            "old_ollama_version": "0.32.15",
            "legacy_units": {
                "ollama.service": {"enabled": True, "active": True},
                "dgx-codex-production.service": {"enabled": True, "active": True},
            },
        }

        recovery.recover(
            config,
            runner,
            lambda model, version: oracles.append((model, version)),
            hostname="dgx-head",
        )

        calls = runner.calls
        self.assertEqual(calls[0], ("user-systemctl", "stop", "dgx-codex-production.service"))
        self.assertEqual(calls[1], ("user-systemctl", "stop", "ollama.service"))
        self.assertEqual(calls[2], ("systemctl", "stop", "deepseek-v4-flash-gateway.service"))
        self.assertEqual(
            calls[3],
            ("docker", "stop", "--time", "30", "deepseek-v4-flash-gateway"),
        )
        self.assertEqual(
            calls[4],
            (
                "docker",
                "compose",
                "--env-file",
                "/etc/deepseek-v4-flash-dspark.env",
                "--file",
                "/opt/deepseek-v4-flash-dspark/docker-compose.dspark.yml",
                "--project-name",
                "deepseek-v4-flash-dspark",
                "stop",
                "--timeout",
                "30",
                "vllm-dspark",
            ),
        )
        self.assertEqual(calls[5][-2:], ("-q", "vllm-dspark"))
        self.assertIn("label=com.docker.compose.project=deepseek-v4-flash-dspark", calls[6])
        ollama_restart = calls.index(("user-systemctl", "restart", "ollama.service"))
        socket_oracle = calls.index(("assert-ollama-socket",))
        second_start = calls.index(("user-systemctl", "start", "--no-block", "dgx-codex-production.service"))
        self.assertLess(ollama_restart, socket_oracle)
        self.assertLess(socket_oracle, second_start)
        self.assertEqual(calls[-1], ("assert-ollama-socket",))
        self.assertEqual(oracles, [("qwen38-q4-256k:latest", "0.32.15")])

        flattened = " ".join(" ".join(call) for call in calls).lower()
        for forbidden in ("ssh", "192.168.100", "192.168.101", "minimax", "h3", "netplan", "ufw"):
            self.assertNotIn(forbidden, flattened)

    def test_compose_stop_failure_never_starts_ollama(self):
        recovery = load_recovery()
        class StopFailure(FakeRunner):
            def run(self, *argv, check=True):
                self.calls.append(tuple(argv))
                if "compose" in argv and "stop" in argv:
                    raise RuntimeError("simulated compose stop failure")
                return ""
        runner = StopFailure()
        config = {
            "schema_version": 1, "state": "FALLBACK_ARMED", "transaction_id": "a" * 32,
            "head_hostname": "dgx-head", "old_model": "qwen38-q4-256k:latest",
            "old_ollama_version": "0.32.15", "legacy_units": {
                "ollama.service": {"enabled": True, "active": True},
                "dgx-codex-production.service": {"enabled": True, "active": True},
            },
        }
        with self.assertRaisesRegex(RuntimeError, "compose stop failure"):
            recovery.recover(config, runner, lambda *_: None, hostname="dgx-head")
        self.assertNotIn(("user-systemctl", "restart", "ollama.service"), runner.calls)

    def test_wrong_host_or_unarmed_config_fails_closed(self):
        recovery = load_recovery()
        base = {
            "schema_version": 1,
            "state": "FALLBACK_ARMED",
            "transaction_id": "a" * 32,
            "head_hostname": "dgx-head",
            "old_model": "qwen38-q4-256k:latest",
            "old_ollama_version": "0.32.15",
            "legacy_units": {
                "ollama.service": {"enabled": True, "active": True},
                "dgx-codex-production.service": {"enabled": True, "active": True},
            },
        }
        with self.assertRaises(ValueError):
            recovery.recover(base, FakeRunner(), lambda *_: None, hostname="dgx-worker")
        missing_transaction = dict(base)
        missing_transaction["transaction_id"] = ""
        with self.assertRaises(ValueError):
            recovery.recover(missing_transaction, FakeRunner(), lambda *_: None, hostname="dgx-head")

    def test_arm_is_exact_atomic_root_mode_and_refreshes_absolute_deadline(self):
        recovery = load_recovery()
        class ArmedRunner(FakeRunner):
            def run(self, *argv, check=True):
                self.calls.append(tuple(argv))
                if argv[:3] == ("systemctl", "is-enabled", recovery.FALLBACK_TIMER):
                    return "enabled\n"
                if argv[:3] == ("systemctl", "is-active", recovery.FALLBACK_TIMER):
                    return "active\n"
                return ""

        runner = ArmedRunner()
        config = {
            "schema_version": 1,
            "state": "FALLBACK_ARMED",
            "transaction_id": "a" * 32,
            "head_hostname": "dgx-head",
            "old_model": "qwen38-q4-256k:latest",
            "old_ollama_version": "0.32.15",
            "legacy_units": {
                "ollama.service": {"enabled": True, "active": True},
                "dgx-codex-production.service": {"enabled": True, "active": True},
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            state = root / "state"
            dropin = root / "timer.d" / "deadline.conf"
            state.mkdir()
            with mock.patch.object(recovery, "STATE_DIR", state), mock.patch.object(
                recovery, "ARMED", state / "armed.json"
            ), mock.patch.object(recovery, "RUNNING", state / "running.json"), mock.patch.object(
                recovery, "COMMITTED", state / "committed.json"
            ), mock.patch.object(recovery, "TIMER_DROPIN", dropin), mock.patch.object(
                recovery, "ROOT_UID", os.getuid()
            ):
                recovery.arm(config, runner, hostname="dgx-head", deadline_epoch=1_800_000_000)
                armed = state / "armed.json"
                self.assertEqual(json.loads(armed.read_text()), config)
                self.assertEqual(armed.stat().st_mode & 0o777, 0o600)
                self.assertIn("OnCalendar=2027-01-15 08:00:00 UTC", dropin.read_text())
                recovery.refresh("a" * 32, runner, hostname="dgx-head", deadline_epoch=1_800_000_120)
                self.assertIn("OnCalendar=2027-01-15 08:02:00 UTC", dropin.read_text())

    def test_disarm_proves_disabled_inactive_and_dropin_absent(self):
        recovery = load_recovery()

        class StateRunner(FakeRunner):
            def run(self, *argv, check=True):
                self.calls.append(tuple(argv))
                if argv[:3] == ("systemctl", "is-enabled", recovery.FALLBACK_TIMER):
                    return "disabled\n"
                if argv[:3] == ("systemctl", "is-active", recovery.FALLBACK_TIMER):
                    return "inactive\n"
                return ""

        runner = StateRunner()
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            state = root / "state"
            dropin = root / "timer.d" / "deadline.conf"
            state.mkdir()
            dropin.parent.mkdir()
            dropin.write_text("stale")
            config = {
                "schema_version": 1,
                "state": "FALLBACK_ARMED",
                "transaction_id": "b" * 32,
                "head_hostname": "dgx-head",
                "old_model": "qwen38-q4-256k:latest",
                "old_ollama_version": "0.32.15",
                "legacy_units": {
                    "ollama.service": {"enabled": True, "active": True},
                    "dgx-codex-production.service": {"enabled": True, "active": True},
                },
            }
            (state / "armed.json").write_text(json.dumps(config))
            os.chmod(state / "armed.json", 0o600)
            with mock.patch.object(recovery, "STATE_DIR", state), mock.patch.object(
                recovery, "ARMED", state / "armed.json"
            ), mock.patch.object(recovery, "RUNNING", state / "running.json"), mock.patch.object(
                recovery, "COMMITTED", state / "committed.json"
            ), mock.patch.object(recovery, "TIMER_DROPIN", dropin), mock.patch.object(
                recovery, "ROOT_UID", os.getuid()
            ):
                recovery.disarm("b" * 32, runner, hostname="dgx-head")
                self.assertFalse((state / "armed.json").exists())
                self.assertFalse(dropin.exists())
                committed = json.loads((state / "committed.json").read_text())
                self.assertEqual(committed["transaction_id"], "b" * 32)

    def test_recovery_without_capability_is_failure_not_successful_noop(self):
        recovery = load_recovery()
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            with mock.patch.object(recovery, "STATE_DIR", root), mock.patch.object(
                recovery, "ARMED", root / "armed.json"
            ), mock.patch.object(recovery, "RUNNING", root / "running.json"):
                with self.assertRaises(RuntimeError):
                    recovery.recover_main()


if __name__ == "__main__":
    unittest.main()
