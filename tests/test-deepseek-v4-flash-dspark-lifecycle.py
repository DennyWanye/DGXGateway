#!/usr/bin/env python3
"""Lifecycle ordering and fail-closed tests for the DeepSeek controller."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "control-deepseek-v4-flash-dspark.py"
SPEC = importlib.util.spec_from_file_location("deepseek_controller_lifecycle", MODULE_PATH)
controller = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(controller)


class AuthorityStub:
    def __init__(self):
        self.events = []
        self.guards = 0

    def status(self):
        return {"state": "UNLOCKED"}

    def acquire(self, task_id, purpose, lease_seconds=1800):
        self.events.append(("acquire", task_id, purpose))
        return {"state": "ACTIVE", "owner": controller.OWNER}

    def guard(self):
        self.guards += 1
        return {"state": "ACTIVE", "owner": controller.OWNER, "lease_generation": "a" * 32}

    def heartbeat(self):
        return {"state": "ACTIVE"}

    def release(self):
        self.events.append(("release",))
        return {
            "state": "UNLOCKED",
            "release_operation_id": "d" * 32,
            "release_receipt": "/tmp/release-completion.json",
            "release_receipt_sha256": "e" * 64,
        }


class LifecycleRunner:
    def __init__(self, lose_lease=False):
        self.calls = []
        self.lose_lease = lose_lease

    def run(self, argv, **kwargs):
        argv = tuple(map(str, argv))
        self.calls.append((argv, kwargs))
        text = " ".join(argv)
        if "hostname" in text:
            role = "worker" if "192.0.2.31" in text else "head"
            host = controller.HOSTS[role]
            return controller.Completed(0, f"{host.hostname}\noperator\n", "")
        if "--inspect-state" in text:
            return controller.Completed(
                0,
                json.dumps({"prior_state_sha256": "b" * 64, "state": "stopped"}) + "\n",
                "",
            )
        return controller.Completed(0, "{}\n", "")

    def run_supervised(self, argv, **kwargs):
        self.calls.append((tuple(map(str, argv)), kwargs))
        if self.lose_lease:
            raise controller.LeaseLost("simulated")
        kwargs["lease_check"]()
        return controller.Completed(
            0,
            json.dumps({"terminal": "succeeded", "evidence_sha256": "c" * 64}) + "\n",
            "",
        )


class LifecycleTests(unittest.TestCase):
    def make_controller(self, runner=None):
        tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(tmp.cleanup)
        authority = AuthorityStub()
        instance = controller.ClusterController(
            authority=authority,
            runner=runner or LifecycleRunner(),
            journal=controller.Journal(Path(tmp.name) / "journal.jsonl"),
            task_id="task-1",
        )
        instance._verify_fabric_consumer_receipts = mock.Mock(
            return_value={"source_lease_generation": "0" * 32}
        )
        return instance, authority

    def test_start_is_worker_first_and_stop_is_head_first(self):
        runner = LifecycleRunner()
        instance, authority = self.make_controller(runner)
        instance.start()
        start_receipts = [
            json.loads(call[1]["input_text"])
            for call in runner.calls
            if call[1].get("input_text") and "--verb" in " ".join(call[0])
        ]
        self.assertEqual([r["role"] for r in start_receipts], ["worker", "head"])
        self.assertTrue(all(r["verb"] == "start" for r in start_receipts))
        self.assertGreaterEqual(authority.guards, 4)
        self.assertEqual(authority.events[-1], ("release",))
        events = [json.loads(line) for line in instance.journal.path.read_text().splitlines()]
        self.assertEqual(
            [event["role"] for event in events if event["event"] == "READY"],
            ["worker", "head"],
        )

        runner.calls.clear()
        instance.stop()
        stop_receipts = [
            json.loads(call[1]["input_text"])
            for call in runner.calls
            if call[1].get("input_text") and "--verb" in " ".join(call[0])
        ]
        self.assertEqual([r["role"] for r in stop_receipts], ["head", "worker"])
        self.assertTrue(all(r["verb"] == "stop" for r in stop_receipts))

    def test_borrowed_start_stop_never_acquire_or_release_outer_lease(self):
        runner = LifecycleRunner()
        instance, authority = self.make_controller(runner)
        instance.start(transaction_id="1" * 32, _under_active=True)
        instance.stop(transaction_id="2" * 32, _under_active=True)
        self.assertEqual(authority.events, [])
        self.assertGreater(authority.guards, 0)

    def test_production_gateway_verbs_are_exact_and_borrowed(self):
        runner = LifecycleRunner()
        instance, authority = self.make_controller(runner)
        transaction = "3" * 32
        activated = instance.production_activate_gateway(transaction, _under_active=True)
        deactivated = instance.production_deactivate_gateway(transaction, _under_active=True)
        self.assertEqual(authority.events, [])
        self.assertEqual(activated["transaction_id"], transaction)
        self.assertEqual(deactivated["transaction_id"], transaction)
        receipts = [
            json.loads(call[1]["input_text"])
            for call in runner.calls
            if call[1].get("input_text") and "--verb" in " ".join(call[0])
        ]
        self.assertEqual(
            [receipt["verb"] for receipt in receipts],
            ["production-activate-gateway", "production-deactivate-gateway"],
        )
        self.assertEqual(
            controller.action_argv("head", "production-activate-gateway", transaction)[-3:],
            ["gateway-start", "--transaction-id", transaction],
        )
        self.assertEqual(
            controller.action_argv("head", "production-deactivate-gateway", transaction)[-3:],
            ["gateway-stop", "--transaction-id", transaction],
        )
        helper = (ROOT / "scripts/run-deepseek-v4-flash-dspark.sh").read_text()
        self.assertIn(
            "cutover|production-activate-gateway|production-deactivate-gateway|stage-gateway|stop-staging-gateway)",
            helper,
        )
        self.assertIn(
            "cutover|production-activate-gateway|production-deactivate-gateway|rollback|arm-fallback",
            helper,
        )

    def test_hostname_and_user_are_verified_before_first_mutation(self):
        runner = LifecycleRunner()
        instance, _ = self.make_controller(runner)
        instance.start()
        texts = [" ".join(call[0]) for call in runner.calls]
        first_mutation = next(i for i, call in enumerate(runner.calls) if call[1].get("input_text"))
        self.assertTrue(any("hostname" in text and "192.0.2.31" in text for text in texts[:first_mutation]))

    def test_wrong_hostname_stops_before_mutation(self):
        runner = LifecycleRunner()
        original = runner.run

        def wrong(argv, **kwargs):
            if "hostname" in " ".join(map(str, argv)):
                return controller.Completed(0, "wrong-host\noperator\n", "")
            return original(argv, **kwargs)

        runner.run = wrong
        instance, _ = self.make_controller(runner)
        with self.assertRaises(controller.TargetMismatch):
            instance.start()
        self.assertFalse(any(call[1].get("input_text") for call in runner.calls))

    def test_lease_loss_aborts_exact_supervised_operation(self):
        instance, authority = self.make_controller(LifecycleRunner(lose_lease=True))
        with self.assertRaises(controller.LeaseLost):
            instance.start()
        self.assertEqual(authority.events[-1], ("release",))

    def test_failed_worker_start_attempts_only_exact_head_then_worker_cleanup(self):
        class FailingFirstMutation(LifecycleRunner):
            def __init__(self):
                super().__init__()
                self.mutation_count = 0

            def run_supervised(self, argv, **kwargs):
                self.calls.append((tuple(map(str, argv)), kwargs))
                kwargs["lease_check"]()
                self.mutation_count += 1
                if self.mutation_count == 1:
                    return controller.Completed(75, '{"terminal":"failed"}\n', "failed")
                return controller.Completed(0, '{"terminal":"succeeded"}\n', "")

        runner = FailingFirstMutation()
        instance, _ = self.make_controller(runner)
        with self.assertRaises(controller.ControllerError):
            instance.start()
        receipts = [
            json.loads(call[1]["input_text"])
            for call in runner.calls
            if call[1].get("input_text") and "--verb" in " ".join(call[0])
        ]
        self.assertEqual(
            [(receipt["role"], receipt["verb"]) for receipt in receipts],
            [("worker", "start"), ("head", "stop"), ("worker", "stop")],
        )

    def test_cutover_requires_external_fallback_armed_state(self):
        instance, _ = self.make_controller()
        with self.assertRaises(controller.PlanGateError):
            instance.cutover(None)

    def test_systemd_receipts_bind_the_exact_unit_resource(self):
        self.assertEqual(
            controller.action_resource("head", "cutover"),
            "deepseek-v4-flash-gateway.service",
        )
        self.assertEqual(
            controller.action_resource("head", "rollback"),
            "deepseek-v4-flash-fallback.service",
        )

    def test_fallback_actions_bind_transaction_and_dedicated_mutex(self):
        transaction = "d" * 32
        fallback = {
            "schema_version": 1,
            "state": "FALLBACK_ARMED",
            "transaction_id": transaction,
            "head_hostname": "dgx-head",
            "old_model": "qwen38-q4-256k:latest",
            "old_ollama_version": "0.32.15",
            "legacy_units": {
                "ollama.service": {"enabled": True, "active": True},
                "dgx-codex-production.service": {"enabled": True, "active": True},
            },
        }
        arm = controller.action_argv("head", "arm-fallback", transaction, fallback)
        refresh = controller.action_argv("head", "refresh-fallback", transaction)
        disarm = controller.action_argv("head", "disarm-fallback", transaction)
        for argv in (arm, refresh, disarm):
            self.assertIn("/run/lock/deepseek-v4-flash-fallback.lifecycle.lock", argv)
            self.assertNotIn(
                "/home/operator/.local/state/deepseek-v4-flash-dspark/lifecycle.lock",
                argv,
            )
        self.assertIn(json.dumps(fallback, sort_keys=True, separators=(",", ":")), arm)

    def test_cutover_states_are_exact_order_and_transaction_bound(self):
        tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(tmp.cleanup)
        journal = controller.Journal(Path(tmp.name) / "journal.jsonl")
        transaction = "e" * 32
        machine = controller.CutoverStateMachine(journal, transaction)
        for state in controller.CUTOVER_STATES:
            machine.advance(state)
        entries = [json.loads(line) for line in journal.path.read_text().splitlines()]
        self.assertEqual([entry["state"] for entry in entries], list(controller.CUTOVER_STATES))
        self.assertTrue(all(entry["transaction_id"] == transaction for entry in entries))
        with self.assertRaises(controller.PlanGateError):
            machine.advance("VERIFIED")

    def test_cutover_state_machine_rejects_skip_and_bad_transaction(self):
        tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(tmp.cleanup)
        journal = controller.Journal(Path(tmp.name) / "journal.jsonl")
        with self.assertRaises(controller.PlanGateError):
            controller.CutoverStateMachine(journal, "short")
        machine = controller.CutoverStateMachine(journal, "f" * 32)
        with self.assertRaises(controller.PlanGateError):
            machine.advance("FALLBACK_ARMED")

    def test_gateway_cutover_uses_one_shot_capability_and_active_state_gate(self):
        helper = (ROOT / "scripts" / "run-deepseek-v4-flash-dspark.sh").read_text()
        self.assertIn("gateway-authorized.pending", helper)
        self.assertIn("ActiveState", helper)
        self.assertIn("condition-skipped gateway start", helper)

    def test_gateway_cutover_checks_root_only_capability_as_root(self):
        helper = (ROOT / "scripts" / "run-deepseek-v4-flash-dspark.sh").read_text()
        self.assertIn('sudo -n test ! -e "$GATEWAY_RUN/gateway-authorized.pending"', helper)
        self.assertIn('sudo -n test -f "$GATEWAY_RUN/gateway-authorized.running"', helper)

    def test_rollback_terminal_requires_matching_recovery_success(self):
        helper = (ROOT / "scripts" / "run-deepseek-v4-flash-dspark.sh").read_text()
        self.assertIn("recovery-succeeded.json", helper)
        self.assertIn("rollback recovery transaction mismatch", helper)
        self.assertNotIn("no armed fallback is success", helper)

    def test_staging_gateway_is_loopback_only_manual_and_never_restarts(self):
        conf = (ROOT / "gateway/deepseek-v4-flash-nginx-staging.conf").read_text()
        unit = (ROOT / "systemd/deepseek-v4-flash-gateway-staging.service").read_text()
        self.assertIn("listen 127.0.0.1:11435", conf)
        self.assertNotIn("192.0.2.30:11435", conf)
        self.assertIn("deepseek-v4-flash-gateway-staging", unit)
        self.assertIn("Restart=no", unit)
        self.assertNotIn("WantedBy=", unit)

    def test_artifact_mutations_are_exact_receipt_scoped_and_fd3_only(self):
        self.assertEqual(
            set(controller.ARTIFACT_VERBS),
            {
                "finalize-model-lock",
                "copy-model",
                "acquire-images",
                "save-runtime-archive",
                "copy-runtime-archive",
                "load-runtime-archive",
                "apply-runtime-patch",
                "install-integration-artifacts",
                "finalize-deployment-lock",
                "copy-patched-source",
                "record-license-acceptance",
            },
        )
        for verb, (role, timeout) in controller.ARTIFACT_VERBS.items():
            argv = controller.action_argv(role, verb, "a" * 32)
            self.assertEqual(argv[0], "/usr/local/libexec/prepare-deepseek-v4-flash-dspark.sh")
            self.assertEqual(argv[1], verb)
            self.assertEqual(argv[-2:], ["--receipt-file", "/proc/self/fd/3"])
            self.assertEqual(controller.action_resource(role, verb), f"deepseek-v4-flash-dspark:{verb}")
            self.assertIn(timeout, {600, 1800, 3600, 7200})
        for role in ("worker", "head"):
            argv = controller.action_argv(role, "record-node-identity", "a" * 32)
            self.assertEqual(argv[1], "record-node-identity")
            self.assertEqual(argv[-2:], ["--receipt-file", "/proc/self/fd/3"])
            journal = argv[argv.index("--image-journal") + 1]
            self.assertEqual(
                journal,
                (
                    "/home/operator/dgx-models/deepseek-v4-flash-dspark/artifacts/image-acquisition.journal.json"
                    if role == "worker"
                    else "/home/operator/.local/state/deepseek-v4-flash-dspark/artifacts/image-acquisition.journal.json"
                ),
            )
        helper = (ROOT / "scripts" / "run-deepseek-v4-flash-dspark.sh").read_text()
        self.assertIn('3<"$running_path"', helper)
        self.assertIn('>"$action_stdout" 2>"$action_stderr"', helper)
        self.assertIn('"action_output_sha256": action_output', helper)
        self.assertIn('echo "$verb failed stderr (redacted, first 80 lines):"', helper)
        self.assertIn('--project-directory "$SOURCE_ROOT"', helper)
        self.assertIn('label=com.docker.compose.project=$PROJECT', helper)
        self.assertIn('com.docker.compose.config-hash', helper)
        self.assertNotIn('ps --format json', helper)
        self.assertIn("sed -E 's/[A-Za-z0-9_-]{43,}/[REDACTED]/g'", helper)
        self.assertIn("if [[ $mode != trust-action ]]; then", helper)
        self.assertNotIn("/Users/operator/.codex/locks", helper)
        self.assertIn('f"192.0.2.36 {key_type} {key_blob}\\n"', helper)
        controller_source = (ROOT / "scripts" / "control-deepseek-v4-flash-dspark.py").read_text()
        self.assertIn('(\"192.0.2.31\", \"192.0.2.34\", \"192.0.2.36\")', controller_source)

    def test_task2_live_fabric_report_checks_two_rails_and_strict_worker_ssh(self):
        report = {
            "hostname": "dgx-head",
            "user": "operator",
            "boot_id": "0" * 36,
            "kernel": "6.17.0-1031-nvidia",
            "dgx_ota": "7.5.0",
            "driver": "580.173.02",
            "netplan_sha256": controller.FABRIC_EXPECTED["head"]["netplan_sha256"],
            "links": {
                "enp1s0f0np0": {
                    "mtu": 9000,
                    "operstate": "UP",
                    "speed": "200000",
                    "cidrs": ["192.0.2.33/30"],
                    "mac": "02:00:00:00:00:01",
                    "bus_info": "0000:01:00.0",
                    "firmware": "28.45.4028",
                },
                "enP2p1s0f0np0": {
                    "mtu": 9000,
                    "operstate": "UP",
                    "speed": "200000",
                    "cidrs": ["192.0.2.35/30"],
                    "mac": "02:00:00:00:00:01",
                    "bus_info": "0000:02:00.0",
                    "firmware": "28.45.4028",
                },
            },
            "default_routes": [{"dev": "wlP9s9"}],
            "ibdev2netdev": "rocep1s0f0 port 1 ==> enp1s0f0np0 (Up)\nroceP2p1s0f0 port 1 ==> enP2p1s0f0np0 (Up)",
            "show_gids": (
                "DEV\tPORT\tINDEX\tGID\tIPv4\tVER\tDEV\n"
                "rocep1s0f0\t1\t3\tabc\t192.0.2.33\tv2\tenp1s0f0np0\n"
                "roceP2p1s0f0\t1\t3\tdef\t192.0.2.35\tv2\tenP2p1s0f0np0"
            ),
            "hca_firmware": {"rocep1s0f0": "28.45.4028", "roceP2p1s0f0": "28.45.4028"},
            "pings": {"192.0.2.34": 0, "192.0.2.36": 0},
            "routes": {
                "192.0.2.34": {"dev": "enp1s0f0np0", "prefsrc": "192.0.2.33"},
                "192.0.2.36": {"dev": "enP2p1s0f0np0", "prefsrc": "192.0.2.35"},
            },
            "listeners": ["LISTEN 0 4096 192.0.2.30:11434 0.0.0.0:*"],
            "head_to_worker": {"returncode": 0, "stdout": ["dgx-worker", "operator"]},
        }
        self.assertIs(controller.validate_live_fabric("head", report), report)
        report["links"]["enP2p1s0f0np0"]["mtu"] = 1500
        with self.assertRaises(controller.PlanGateError):
            controller.validate_live_fabric("head", report)

    def test_task2_probe_is_read_only_and_pins_fabric_host_key_files(self):
        probe = controller.FABRIC_PROBE
        self.assertIn("deepseek-v4-flash-worker-known_hosts", probe)
        self.assertIn("StrictHostKeyChecking=yes", probe)
        self.assertIn('"-M", "do", "-s", "8972"', probe)
        self.assertIn('release_value("/etc/dgx-release", "DGX_OTA_VERSION")', probe)
        self.assertNotIn('"dgx-ota"', probe)
        self.assertIn('["sudo", "-n", "sha256sum", "--", path]', probe)
        self.assertNotIn("pathlib.Path(path).read_bytes()", probe)
        for forbidden in ("netplan apply", "ufw", "nft", "ip addr add", "ip link set"):
            self.assertNotIn(forbidden, probe)

    def test_service_assets_install_is_worker_first_and_secret_is_fd4_only(self):
        head_argv = controller.action_argv("head", "install-service-assets", "a" * 32)
        worker_argv = controller.action_argv("worker", "install-service-assets", "a" * 32)
        self.assertEqual(head_argv[-2:], ["--secret-fd", "/proc/self/fd/4"])
        self.assertNotIn("--secret-fd", worker_argv)
        self.assertEqual(
            controller.action_resource("head", "install-service-assets"),
            "deepseek-v4-flash-dspark:install-service-assets:head",
        )
        runner = LifecycleRunner()
        instance, _ = self.make_controller(runner)
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            secret = Path(temporary) / "gateway.token"
            token = "A" * 64
            secret.write_text(token + "\n")
            secret.chmod(0o600)
            instance.install_service_assets(secret)
        mutations = [
            call for call in runner.calls if call[1].get("input_text") and "--verb" in " ".join(call[0])
        ]
        receipts = []
        for call in mutations:
            raw = call[1]["input_text"]
            if "--receipt-secret-stdin" in " ".join(call[0]):
                length_text, framed = raw.split("\n", 1)
                receipt_text = framed[: int(length_text)]
                self.assertEqual(framed[int(length_text) :], token)
                self.assertNotIn(token, receipt_text)
            else:
                receipt_text = raw
                self.assertNotIn(token, receipt_text)
            receipts.append(json.loads(receipt_text))
        self.assertEqual([receipt["role"] for receipt in receipts], ["worker", "head"])
        self.assertTrue(all(token not in json.dumps(receipt) for receipt in receipts))
        helper = (ROOT / "scripts/run-deepseek-v4-flash-dspark.sh").read_text()
        self.assertIn('3<"$running_path" 4<"$secret_tmp"', helper)

    def test_fabric_consumer_qualification_and_trust_are_exact_receipt_scoped(self):
        transaction = "a" * 32
        public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest deepseek-v4-flash-dspark"
        host_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHost dgx-worker"
        fingerprint = "SHA256:" + "f" * 43
        key_argv = controller.action_argv(
            "head", "ensure-head-worker-key", transaction
        )
        worker_argv = controller.action_argv(
            "worker",
            "install-worker-authorized-key",
            transaction,
            {"public_key": public_key, "fingerprint": fingerprint},
        )
        known_argv = controller.action_argv(
            "head",
            "install-head-worker-known-hosts",
            transaction,
            {"host_public_key": host_key, "fingerprint": fingerprint},
        )
        for role, verb, argv in (
            ("head", "ensure-head-worker-key", key_argv),
            ("worker", "install-worker-authorized-key", worker_argv),
            ("head", "install-head-worker-known-hosts", known_argv),
        ):
            self.assertEqual(argv[0], controller.REMOTE_HELPER)
            self.assertEqual(argv[-2:], ["--receipt-file", "/proc/self/fd/3"])
            self.assertEqual(
                controller.action_resource(role, verb),
                f"deepseek-v4-flash-dspark:trust:{verb}",
            )
        self.assertNotIn("PRIVATE", " ".join(key_argv + worker_argv + known_argv))

        qualify = controller.action_argv(
            "head", controller.QUALIFY_VERB, transaction
        )
        self.assertEqual(
            qualify,
            [
                controller.REMOTE_PREPARE,
                controller.QUALIFY_VERB,
                "--role",
                "head",
                "--fabric-lock",
                "/opt/deepseek-v4-flash-dspark/manifests/dgx-spark-fabric-qualification.lock.json",
                "--provenance-addendum",
                "/opt/deepseek-v4-flash-dspark/plans/deepseek-v4-flash-dspark-2node/verification/20260829T1951/h3-lock-provenance-addendum-v1.json",
                "--deployment-lock",
                "/home/operator/.local/state/deepseek-v4-flash-dspark/artifacts/deployment.lock.json",
                "--output",
                "/home/operator/.local/state/deepseek-v4-flash-dspark/artifacts/fabric-consumer.receipt.json",
                "--receipt-file",
                "/proc/self/fd/3",
            ],
        )
        self.assertEqual(
            controller.action_resource("head", controller.QUALIFY_VERB),
            "deepseek-v4-flash-dspark:qualify-fabric-consumer",
        )

    def test_start_refuses_without_valid_released_two_node_qualification(self):
        runner = LifecycleRunner()
        instance, _ = self.make_controller(runner)
        with mock.patch.object(
            instance,
            "_verify_fabric_consumer_receipts",
            side_effect=controller.PlanGateError("qualification absent"),
        ):
            with self.assertRaisesRegex(controller.PlanGateError, "qualification absent"):
                instance.start()
        self.assertFalse(any(call[1].get("input_text") for call in runner.calls))

    def test_lease_correlates_every_pending_qualification_only_after_release(self):
        instance, _ = self.make_controller()
        with instance._lease("multiple qualification correlation"):
            instance._pending_qualifications.extend(
                [
                    {
                        "transaction_id": "1" * 32,
                        "source_lease_generation": "a" * 32,
                        "qualification_receipt_sha256": "b" * 64,
                    },
                    {
                        "transaction_id": "2" * 32,
                        "source_lease_generation": "a" * 32,
                        "qualification_receipt_sha256": "c" * 64,
                    },
                ]
            )
        released = [
            row
            for row in instance.journal.entries()
            if row.get("event") == "FABRIC_QUALIFICATION_RELEASED"
        ]
        self.assertEqual([row["transaction_id"] for row in released], ["1" * 32, "2" * 32])
        self.assertTrue(all(row["release_operation_id"] == "d" * 32 for row in released))

    def test_failed_operation_or_release_never_correlates_qualification(self):
        instance, _ = self.make_controller()
        with self.assertRaisesRegex(RuntimeError, "operation failed"):
            with instance._lease("failed qualification operation"):
                instance._pending_qualifications.append(
                    {
                        "transaction_id": "1" * 32,
                        "source_lease_generation": "a" * 32,
                        "qualification_receipt_sha256": "b" * 64,
                    }
                )
                raise RuntimeError("operation failed")
        self.assertFalse(
            any(row.get("event") == "FABRIC_QUALIFICATION_RELEASED" for row in instance.journal.entries())
        )

        class ReleaseFailureAuthority(AuthorityStub):
            def release(self):
                self.events.append(("release",))
                raise controller.LockUnavailable("release failed")

        tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(tmp.cleanup)
        failed = controller.ClusterController(
            authority=ReleaseFailureAuthority(),
            runner=LifecycleRunner(),
            journal=controller.Journal(Path(tmp.name) / "journal.jsonl"),
            task_id="task-1",
        )
        with self.assertRaisesRegex(controller.LockUnavailable, "release failed"):
            with failed._lease("failed release"):
                failed._pending_qualifications.append(
                    {
                        "transaction_id": "2" * 32,
                        "source_lease_generation": "a" * 32,
                        "qualification_receipt_sha256": "c" * 64,
                    }
                )
        self.assertFalse(
            any(row.get("event") == "FABRIC_QUALIFICATION_RELEASED" for row in failed.journal.entries())
        )

    def test_bootstrap_executor_is_fixed_stdin_hash_scoped_and_wrong_host_has_no_mutation(self):
        self.assertEqual(
            set(controller.BOOTSTRAP_SOURCES),
            {
                "/usr/local/libexec/run-deepseek-v4-flash-dspark.sh",
                "/usr/local/libexec/prepare-deepseek-v4-flash-dspark.sh",
            },
        )
        self.assertNotIn("scp", controller.BOOTSTRAP_REMOTE)
        self.assertNotIn("rsync", controller.BOOTSTRAP_REMOTE)
        self.assertIn("bootstrap source digest mismatch", controller.BOOTSTRAP_REMOTE)
        self.assertIn("restore(); raise", controller.BOOTSTRAP_REMOTE)

        class WrongHostRunner(LifecycleRunner):
            def run(self, argv, **kwargs):
                if "hostname" in " ".join(map(str, argv)):
                    self.calls.append((tuple(map(str, argv)), kwargs))
                    return controller.Completed(0, "wrong-host\noperator\n", "")
                return super().run(argv, **kwargs)

        runner = WrongHostRunner()
        instance, _ = self.make_controller(runner)
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            helper = Path(temporary) / "run"
            prepare = Path(temporary) / "prepare"
            helper.write_text("helper")
            prepare.write_text("prepare")
            with mock.patch.object(
                controller,
                "BOOTSTRAP_SOURCES",
                {
                    controller.REMOTE_HELPER: helper,
                    controller.REMOTE_PREPARE: prepare,
                },
            ):
                with self.assertRaises(controller.TargetMismatch):
                    instance.bootstrap_executor()
        self.assertFalse(any(call[1].get("input_text") for call in runner.calls))

    def test_manifest_input_bootstrap_is_role_scoped_hash_closed_and_worker_first(self):
        raw_manifest = controller.DEFAULT_BOOTSTRAP_INPUT_LOCK.read_bytes()
        self.assertEqual(
            __import__("hashlib").sha256(raw_manifest).hexdigest(),
            controller.BOOTSTRAP_INPUT_RAW_SHA256,
        )
        manifest = json.loads(raw_manifest)
        self.assertEqual(
            controller.validate_bootstrap_input_manifest(manifest),
            controller.BOOTSTRAP_INPUT_CANONICAL_SHA256,
        )
        runner = LifecycleRunner()
        instance, _ = self.make_controller(runner)
        result = instance.bootstrap_input_bundle()
        payloads = [json.loads(call[1]["input_text"]) for call in runner.calls if call[1].get("input_text")]
        self.assertEqual([payload["role"] for payload in payloads], ["worker", "head"])
        self.assertTrue(all(len(payload["manifest"]["files"]) == 19 for payload in payloads))
        self.assertEqual(result["worker"]["terminal"], "succeeded")
        self.assertIn("different input bootstrap preimage rejected", controller.INPUT_BUNDLE_REMOTE)
        self.assertNotIn("tarfile", controller.INPUT_BUNDLE_REMOTE)
        self.assertNotIn("extractall", controller.INPUT_BUNDLE_REMOTE)

    def test_bootstrap_input_lock_is_canonical_clean_and_rejects_row_drift(self):
        parser = controller.build_parser()
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            parser.parse_args(["bootstrap-input-bundle", "--lock", "/tmp/forged.json"])

        class DirtyCanonicalRunner(LifecycleRunner):
            def run(self, argv, **kwargs):
                text = " ".join(map(str, argv))
                if "git" in text and "diff --quiet" in text:
                    return controller.Completed(1, "", "dirty")
                return super().run(argv, **kwargs)

        canonical = json.loads(controller.DEFAULT_BOOTSTRAP_INPUT_LOCK.read_text())
        single = json.loads(json.dumps(canonical))
        single["files"] = single["files"][:1]
        for mutated in (
            single,
            {**canonical, "file_count": 18},
            {**canonical, "total_bytes": canonical["total_bytes"] - 1},
            {**canonical, "root_sha256": "0" * 64},
        ):
            with self.assertRaises(controller.PlanGateError):
                controller.validate_bootstrap_input_manifest(mutated)
        changed_row = json.loads(json.dumps(canonical))
        changed_row["files"][0]["sha256"] = "0" * 64
        with self.assertRaises(controller.PlanGateError):
            controller.validate_bootstrap_input_manifest(changed_row)

        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            lock = temporary_root / "manifests/deepseek-v4-flash-bootstrap-input.lock.json"
            lock.parent.mkdir(parents=True)
            lock.write_bytes(controller.DEFAULT_BOOTSTRAP_INPUT_LOCK.read_bytes())
            instance, _ = self.make_controller(DirtyCanonicalRunner())
            with mock.patch.object(controller, "DEFAULT_BOOTSTRAP_INPUT_LOCK", lock), mock.patch.object(controller, "REPO_ROOT", temporary_root):
                with self.assertRaisesRegex(controller.PlanGateError, "committed canonical authority"):
                    instance.bootstrap_input_bundle()

        self.assertIn("input bootstrap exact role tree contains a symlink", controller.INPUT_BUNDLE_REMOTE)
        self.assertIn("source frozen count/bytes/root closure mismatch", controller.SOURCE_INSTALL_REMOTE)
        self.assertIn("existing source content/owner/mode drift", controller.SOURCE_INSTALL_REMOTE)
        self.assertIn("renameat2", controller.SOURCE_INSTALL_REMOTE)
        self.assertNotIn("os.replace(staging, target)", controller.SOURCE_INSTALL_REMOTE)

    def test_pinned_source_snapshot_is_exact_current_tree_not_mutable_clone(self):
        instance, _ = self.make_controller(controller.SubprocessRunner())
        payload, digest = instance._pinned_source_snapshot()
        value = json.loads(payload)
        self.assertEqual(value["commit"], controller.PINNED_SOURCE_COMMIT)
        self.assertEqual(value["tree"], controller.PINNED_SOURCE_TREE)
        self.assertEqual(value["file_count"], 171)
        self.assertEqual(value["total_bytes"], 4293967)
        self.assertEqual(value["root_sha256"], "83bea7e3d33dfac0e778ea1bd2bb885429a6e51d9cab34e6130342f7a7e8d2c8")
        self.assertEqual(value["snapshot_sha256"], digest)
        self.assertNotIn("git clone", controller.SOURCE_INSTALL_REMOTE)

    def test_source_install_failure_injection_preserves_race_preimage_and_removes_failed_postimage(self):
        snapshot_instance, _ = self.make_controller(controller.SubprocessRunner())
        payload, digest = snapshot_instance._pinned_source_snapshot()

        def execute(destination: Path, mode: str):
            script = controller.SOURCE_INSTALL_REMOTE.replace(
                'target = pathlib.Path("/home/operator/dgx-models/deepseek-v4-flash-dspark/source")',
                f"target = pathlib.Path({str(destination)!r})",
            )
            parent_start = script.index("def ensure_source_parent():")
            parent_end = script.index("identity_data=", parent_start)
            script = (
                script[:parent_start]
                + "def ensure_source_parent():\n    target.parent.mkdir(parents=True, exist_ok=True)\n"
                + script[parent_end:]
            )
            rename_start = script.index("def rename_noreplace(source, destination):")
            rename_end = script.index("ensure_source_parent()", rename_start)
            if mode == "race":
                replacement = """def rename_noreplace(source, destination):
    destination.mkdir(mode=0o755)
    (destination / "competitor-marker").write_text("keep")
    raise FileExistsError("injected no-replace race")
"""
            else:
                replacement = """def rename_noreplace(source, destination):
    if destination.exists(): raise FileExistsError("destination exists")
    os.rename(source, destination)
"""
            script = script[:rename_start] + replacement + script[rename_end:]
            if mode == "postreadback":
                script = script.replace(
                    "rename_noreplace(staging,target); created_target=True",
                    "rename_noreplace(staging,target); created_target=True\n"
                    "    victim=next(path for path in target.rglob('*') if path.is_file() and path.name!='.deepseek-source-identity.json'); victim.write_bytes(b'injected-drift')",
                )
            with (
                mock.patch.dict(os.environ, {"SUDO_USER": "operator"}),
                mock.patch.object(os, "geteuid", return_value=0),
                mock.patch.object(controller.sys, "argv", ["source-install", socket.gethostname(), digest]),
                mock.patch.object(controller.sys, "stdin", io.StringIO(payload)),
                mock.patch.object(controller.sys, "stdout", io.StringIO()),
            ):
                exec(compile(script, "<source-install-failure-injection>", "exec"), {})

        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            race_target = Path(temporary) / "race" / "source"
            with self.assertRaisesRegex(FileExistsError, "injected no-replace race"):
                execute(race_target, "race")
            self.assertEqual((race_target / "competitor-marker").read_text(), "keep")

            failed_target = Path(temporary) / "postreadback" / "source"
            with self.assertRaisesRegex(SystemExit, "content/owner/mode drift"):
                execute(failed_target, "postreadback")
            self.assertFalse(failed_target.exists())

    def test_prepare_artifacts_uses_one_lease_and_trust_precedes_fabric_copy(self):
        instance, authority = self.make_controller()
        events = []
        instance.preflight_fabric = mock.Mock(return_value={"terminal": "PASS"})
        instance.bootstrap_executor = mock.Mock(
            side_effect=lambda **kwargs: events.append("bootstrap-executor") or {}
        )
        instance.bootstrap_input_bundle = mock.Mock(
            side_effect=lambda **kwargs: events.append("bootstrap-inputs") or {}
        )
        instance.install_pinned_source = mock.Mock(
            side_effect=lambda **kwargs: events.append("install-source") or {}
        )
        instance._install_trust_under_active_lease = mock.Mock(
            side_effect=lambda tx: events.append("trust") or {}
        )
        instance._probe_live_fabric = mock.Mock(
            side_effect=lambda **kwargs: events.append("fabric-probe") or {}
        )

        def mutate(role, verb, transaction, *args, **kwargs):
            events.append(verb)
            return {"terminal": "succeeded"}

        instance._mutate = mock.Mock(side_effect=mutate)

        def qualify(transaction):
            events.append("qualify")
            instance._pending_qualifications.append(
                {
                    "transaction_id": transaction,
                    "source_lease_generation": "a" * 32,
                    "qualification_receipt_sha256": "f" * 64,
                }
            )
            return {"terminal": "PASS"}

        instance._qualify_under_active_lease = mock.Mock(side_effect=qualify)
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            secret = Path(temporary) / "gateway.token"
            secret.write_text("A" * 64 + "\n")
            secret.chmod(0o600)
            instance.prepare_artifacts(
                gateway_secret=secret, operator="operator", accepted=True
            )
        self.assertEqual([event[0] for event in authority.events], ["acquire", "release"])
        self.assertLess(events.index("trust"), events.index("copy-model"))
        self.assertLess(events.index("fabric-probe"), events.index("copy-model"))
        self.assertEqual(
            len(
                [
                    row
                    for row in instance.journal.entries()
                    if row.get("event") == "FABRIC_QUALIFICATION_RELEASED"
                ]
            ),
            1,
        )

    def test_prepare_artifacts_failure_releases_and_does_not_continue(self):
        instance, authority = self.make_controller()
        events = []
        instance.preflight_fabric = mock.Mock(return_value={})
        instance.bootstrap_executor = mock.Mock(return_value={})
        instance.bootstrap_input_bundle = mock.Mock(return_value={})
        instance.install_pinned_source = mock.Mock(return_value={})
        instance._install_trust_under_active_lease = mock.Mock(return_value={})
        instance._probe_live_fabric = mock.Mock(return_value={})

        def mutate(role, verb, transaction, *args, **kwargs):
            events.append(verb)
            if verb == "copy-model":
                raise controller.ControllerError("copy failed")
            return {}

        instance._mutate = mock.Mock(side_effect=mutate)
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            secret = Path(temporary) / "gateway.token"
            secret.write_text("A" * 64 + "\n")
            secret.chmod(0o600)
            with self.assertRaisesRegex(controller.ControllerError, "copy failed"):
                instance.prepare_artifacts(
                    gateway_secret=secret, operator="operator", accepted=True
                )
        self.assertEqual([event[0] for event in authority.events], ["acquire", "release"])
        self.assertNotIn("acquire-images", events)

    def test_reboot_quiesces_both_ranks_then_requalifies_before_restart(self):
        instance, _ = self.make_controller()
        events = []
        instance._boot_id = mock.Mock(return_value="old")
        instance._wait_reboot = mock.Mock(return_value="new")
        instance._mutate = mock.Mock(side_effect=lambda role, verb, tx, *a, **k: events.append((role, verb)) or {})
        instance._qualify_under_active_lease = mock.Mock(side_effect=lambda tx: events.append(("cluster", "qualify")) or {})
        refresher = mock.Mock()
        instance._reboot_and_recover("worker", "a" * 32, refresher)
        self.assertLess(events.index(("head", "stop")), events.index(("cluster", "qualify")))
        self.assertLess(events.index(("worker", "stop")), events.index(("cluster", "qualify")))
        self.assertLess(events.index(("cluster", "qualify")), events.index(("worker", "start")))
        self.assertLess(events.index(("worker", "start")), events.index(("head", "start")))

    def test_rollback_remains_available_without_worker(self):
        runner = LifecycleRunner()
        instance, _ = self.make_controller(runner)
        instance.rollback("d" * 32)
        receipt_roles = [
            json.loads(call[1]["input_text"])["role"]
            for call in runner.calls
            if call[1].get("input_text") and "--verb" in " ".join(call[0])
        ]
        self.assertEqual(receipt_roles[0], "head")

    def test_systemd_unit_is_manual_and_never_restarts(self):
        unit = (ROOT / "systemd" / "deepseek-v4-flash-dspark.service").read_text()
        self.assertIn("Restart=no", unit)
        self.assertIn("ConditionPathExists=/run/deepseek-v4-flash-dspark/start-receipt.json", unit)
        self.assertNotIn("WantedBy=", unit)
        self.assertNotIn("Restart=always", unit)
        self.assertNotIn("WatchdogSec=", unit)
        self.assertNotIn("[Install]", unit)

    def test_private_helper_has_exact_cleanup_and_receipt_replay_guards(self):
        helper = (ROOT / "scripts" / "run-deepseek-v4-flash-dspark.sh").read_text()
        self.assertIn('down", "--remove-orphans"', helper)
        self.assertIn("receipt replay rejected", helper)
        self.assertIn('.pending.json', helper)
        self.assertIn("os.replace(pending, running)", helper)
        self.assertIn("os.replace(temporary, running)", helper)
        self.assertIn("receipt expired", helper)
        self.assertIn("prior-state binding mismatch", helper)
        self.assertNotIn("docker prune", helper)
        self.assertNotIn("pkill", helper)
        self.assertNotIn("--volumes", helper)
        self.assertIn('has_option("--node-rank", rank)', helper)
        self.assertIn('shell_script = args[1]', helper)
        self.assertIn('re.search(', helper)
        self.assertGreaterEqual(helper.count('deadline=$((SECONDS + 30))'), 2)
        self.assertIn('has_option("--headless")', helper)
        self.assertIn('has_option("--host", "127.0.0.1")', helper)
        self.assertIn('has_option("--port", "18888")', helper)

    def test_environment_pins_parallelism_offline_mode_and_measured_interfaces(self):
        env = (ROOT / "configs" / "deepseek-v4-flash-dspark.env.example").read_text()
        for expected in (
            "DSPARK_TENSOR_PARALLEL_SIZE=2",
            "DSPARK_PIPELINE_PARALLEL_SIZE=1",
            "MTP_NUM_TOKENS=5",
            "MAX_MODEL_LEN=1048576",
            "DSPARK_RESTART_POLICY=no",
            "HF_HUB_OFFLINE=1",
            "NCCL_SOCKET_IFNAME==enp1s0f0np0",
            "NCCL_IB_HCA==rocep1s0f0:1,roceP2p1s0f0:1",
            "NCCL_IB_GID_INDEX=3",
            "NCCL_CROSS_NIC=0",
        ):
            self.assertIn(expected, env)


if __name__ == "__main__":
    unittest.main()
