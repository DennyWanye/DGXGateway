import hashlib
import base64
import json
import os
import pwd
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch


class FakeHandoffLease:
    def __init__(self, operation_id):
        self.operation_id = operation_id
        self.token_path = Path("/tmp/fake-handoff.token")

    def acquire(self):
        return {"lease_generation": "f" * 32}

    def start_heartbeat(self):
        return None

    def check_supervision(self):
        return None

    def release(self):
        return {"state": "UNLOCKED"}

    def stop_heartbeat(self):
        return None


class FailStartHandoffLease(FakeHandoffLease):
    def start_heartbeat(self):
        raise RuntimeError("injected heartbeat start failure")


class FailAcquireHandoffLease(FakeHandoffLease):
    def acquire(self):
        raise RuntimeError("injected acquire failure")


class FailReleaseHandoffLease(FakeHandoffLease):
    def release(self):
        raise RuntimeError("injected release failure")


def config_text(root: Path, *, mutation_enabled: bool = True) -> str:
    return f'''[gateway]
bind = "127.0.0.1"
port = 18082
allowed_origins = ["http://127.0.0.1:18082"]
state_dir = "{root / 'state'}"
admin_token_file = "{root / 'admin.token'}"
[cluster]
repo_root = "/Users/operator/projects/DGXSpark"
lock_root = "{root / 'lock'}"
lock_cli = "/Users/operator/projects/DGXSpark/scripts/dgx-spark-deployment-lock.py"
token_dir = "{root / 'tokens'}"
[authority]
id = "test-authority"
record = "{root / 'authority.json'}"
credential = "{root / 'authority.credential'}"
mutation_enabled = {str(mutation_enabled).lower()}
remote_fence_enabled = false
remote_fence_path = "/home/operator/.local/state/dgx-cluster-gateway/authority.json"
[models.deepseek]
journal = "{root / 'journal.jsonl'}"
gateway_secret = "{root / 'deepseek.token'}"
drain_timeout_seconds = 10
[models.deepseek_vision]
journal = "{root / 'vision-journal.jsonl'}"
gateway_secret = "{root / 'deepseek-vision.token'}"
drain_timeout_seconds = 10
internal_port = 18889
[models.h3]
queue_url = "http://192.0.2.41:8188/queue"
[models.music3]
root = "/home/operator/ai/ComfyUI"
python = "/home/operator/ai/comfyui-env/bin/python"
port = 8188
[models.personaplex]
root = "/home/operator/dgx-models/personaplex"
[nodes.head]
hostname = "dgx-head"
address = "192.0.2.30"
user = "operator"
[nodes.worker]
hostname = "dgx-worker"
address = "192.0.2.31"
user = "operator"
'''


class ExtendedGatewayTest(unittest.TestCase):
    def make_config(self, directory: str):
        from DGXGateway.dgx_gateway.config import load_config

        root = Path(directory)
        path = root / "gateway.toml"
        path.write_text(config_text(root), encoding="utf-8")
        return path, load_config(path)

    def test_authority_admission_rejects_disabled_and_epoch_rollback(self):
        from DGXGateway.dgx_gateway.admission import AdmissionError, MutationAdmission, initialize

        with tempfile.TemporaryDirectory() as directory:
            _, config = self.make_config(directory)
            self.assertTrue(initialize(config)["allowed"])
            record = json.loads(config.authority_record.read_text())
            record["epoch"] = 2
            config.authority_record.write_text(json.dumps(record))
            MutationAdmission(config).assert_allowed()
            record["epoch"] = 1
            config.authority_record.write_text(json.dumps(record))
            with self.assertRaises(AdmissionError):
                MutationAdmission(config).assert_allowed()

    def test_export_verify_import_roundtrip_and_no_secret_payload(self):
        from DGXGateway.dgx_gateway.admission import initialize
        from DGXGateway.dgx_gateway.maintenance import export_state, import_state, verify_archive
        from DGXGateway.dgx_gateway.store import OperationStore

        with tempfile.TemporaryDirectory() as directory:
            config_path, config = self.make_config(directory)
            initialize(config)
            store = OperationStore(config.state_dir / "gateway.sqlite3")
            store.create_operation("op-1", "minimax-h3", "deepseek-v4-flash", "idempotency-1")
            store.close()
            archive = Path(directory) / "export.tar.gz"
            export_state(config_path, archive)
            self.assertTrue(verify_archive(archive)["valid"])
            payload = archive.read_bytes()
            self.assertNotIn(config.authority_credential.read_bytes().strip(), payload)
            os.remove(config.state_dir / "gateway.sqlite3")
            result = import_state(config_path, archive)
            self.assertIn("gateway.sqlite3", result["imported"])

    def test_provider_contract_is_network_free_and_unconfigured_fails_closed(self):
        from DGXGateway.dgx_gateway.providers import FakeProvider, default_providers

        fake = FakeProvider()
        job = fake.submit({"task": "text-to-video", "prompt": "test"})
        self.assertEqual(fake.status(job)["state"], "QUEUED")
        fake.cancel(job)
        self.assertEqual(fake.status(job)["state"], "CANCELED")
        for provider in default_providers():
            self.assertEqual(provider.capability().state, "UNCONFIGURED")
            with self.assertRaises(RuntimeError):
                provider.submit({"task": "text-to-video"})

    def test_h3_remote_timeout_exceeds_runtime_init_timeout(self):
        text = Path("ComfyUI/scripts/h3-profile-controller.py").read_text(encoding="utf-8")
        self.assertIn("LIFECYCLE_SSH_TIMEOUT_SECONDS = 3000", text)

    def test_h3_controller_targets_live_rank0_management_ip(self):
        text = Path("ComfyUI/scripts/h3-profile-controller.py").read_text(encoding="utf-8")
        self.assertIn('HEAD = "operator@192.0.2.41"', text)
        self.assertNotIn('HEAD = "operator@192.0.2.30"', text)

    def test_already_at_target_is_a_committed_noop_without_lease(self):
        from DGXGateway.dgx_gateway.domain import ObservedModel
        from DGXGateway.dgx_gateway.server import GatewayApplication
        from DGXGateway.dgx_gateway.store import OperationStore

        class Adapter:
            model_id = "minimax-h3"
            profiles = ("10eros-fl2va-v1",)
            def observe(self):
                return ObservedModel(self.model_id, "HEALTHY", "10eros-fl2va-v1")

        class Admission:
            def assert_allowed(self):
                return {"allowed": True}

        class Operations:
            adapters = {"minimax-h3": Adapter()}
            def execute(self, _operation_id):
                raise AssertionError("noop must not execute a lifecycle operation")

        with tempfile.TemporaryDirectory() as directory:
            store = OperationStore(Path(directory) / "state.sqlite3")
            app = GatewayApplication(
                config=object(), store=store, operations=Operations(), snapshot=object(),
                static_dir=Path(directory), admission=Admission(),
            )
            operation, created = app.submit_switch(
                "minimax-h3", "10eros-fl2va-v1", "same-target-key"
            )
            self.assertTrue(created)
            self.assertEqual(operation.phase, "RELEASED")
            self.assertEqual(operation.outcome, "COMMITTED")
            self.assertIn("already-at-target", [event["kind"] for event in store.events(operation.operation_id)])

    def test_mutation_fence_is_atomic_with_operation_creation(self):
        from DGXGateway.dgx_gateway.maintenance import set_mutation_fence
        from DGXGateway.dgx_gateway.store import OperationStore

        with tempfile.TemporaryDirectory() as directory:
            config_path, config = self.make_config(directory)
            store = OperationStore(config.state_dir / "gateway.sqlite3")
            store.create_operation("active-op", "minimax-h3", "deepseek-v4-flash", "active-key")
            store.close()
            with self.assertRaises(RuntimeError):
                set_mutation_fence(config_path, True)
            with self.assertRaises(RuntimeError):
                store = OperationStore(config.state_dir / "gateway.sqlite3")
                try:
                    store.create_operation("blocked-op", None, "minimax-h3", "blocked-key")
                finally:
                    store.close()

    def test_agentos_handoff_chain_is_mechanically_verified(self):
        from DGXGateway.dgx_gateway.admission import AdmissionError, MutationAdmission
        from DGXGateway.dgx_gateway.config import load_config

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "gateway.toml"
            text = config_text(root)
            config_path.write_text(text, encoding="utf-8")
            config = replace(load_config(config_path), authority_id="agentos-dgx-gateway")
            credential = "c" * 64
            config.authority_credential.write_text(credential + "\n", encoding="utf-8")
            os.chmod(config.authority_credential, 0o600)
            record = {
                "schema": MutationAdmission.SCHEMA,
                "cluster_id": "dgx-head+dgx-worker", "epoch": 2, "phase": "ACTIVE",
                "active_authority_id": "agentos-dgx-gateway",
                "credential_sha256": hashlib.sha256(credential.encode()).hexdigest(),
            }
            disabled = {
                "schema": MutationAdmission.SCHEMA,
                "cluster_id": "dgx-head+dgx-worker", "epoch": 1, "phase": "DISABLED",
                "active_authority_id": "mac-dgx-gateway",
            }
            disabled_path = root / "disabled-authority.json"
            disabled_path.write_text(json.dumps(disabled), encoding="utf-8")
            completion_path = root / "prior-release-completion.json"
            completion_path.write_text(json.dumps({"terminal_state": "UNLOCKED"}), encoding="utf-8")
            export_path = root / "state-export.tar.gz"
            export_path.write_bytes(b"verified export fixture")
            for artifact in (disabled_path, completion_path, export_path):
                os.chmod(artifact, 0o600)
            handoff = {
                "schema": MutationAdmission.HANDOFF_SCHEMA,
                "cluster_id": "dgx-head+dgx-worker",
                "from_authority_id": "mac-dgx-gateway",
                "to_authority_id": "agentos-dgx-gateway",
                "disabled_authority_record_sha256": hashlib.sha256(disabled_path.read_bytes()).hexdigest(),
                "prior_release_completion_sha256": hashlib.sha256(completion_path.read_bytes()).hexdigest(),
                "export_archive_sha256": hashlib.sha256(export_path.read_bytes()).hexdigest(),
                "authority_epoch": 2,
            }
            canonical = json.dumps(handoff, sort_keys=True, separators=(",", ":")).encode()
            handoff["chain_sha256"] = hashlib.sha256(canonical).hexdigest()
            record["handoff_chain_sha256"] = handoff["chain_sha256"]
            config.authority_record.write_text(json.dumps(record), encoding="utf-8")
            os.chmod(config.authority_record, 0o600)
            handoff_path = config.authority_record.parent / "handoff.json"
            handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
            os.chmod(handoff_path, 0o600)
            self.assertTrue(MutationAdmission(config).assert_allowed()["allowed"])
            mac_path = root / "mac-gateway.toml"
            mac_path.write_text(text, encoding="utf-8")
            with self.assertRaises(AdmissionError):
                MutationAdmission(replace(load_config(mac_path), authority_id="mac-dgx-gateway")).assert_allowed()
            export_path.write_bytes(b"tampered export fixture")
            with self.assertRaises(AdmissionError):
                MutationAdmission(config).assert_allowed()

    def test_agentos_activation_fences_mac_before_new_authority_passes(self):
        from DGXGateway.dgx_gateway.admission import (
            AdmissionError, MutationAdmission, activate_agentos_handoff, initialize,
        )
        from DGXGateway.dgx_gateway.config import load_config

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mac_path = root / "mac.toml"
            mac_text = config_text(root)
            mac_path.write_text(mac_text, encoding="utf-8")
            mac_config = replace(load_config(mac_path), authority_id="mac-dgx-gateway")
            initialize(mac_config)
            completion = root / "release.json"
            completion.write_text(json.dumps({
                "cluster_id": "dgx-head+dgx-worker", "terminal_state": "UNLOCKED"
            }), encoding="utf-8")
            export = root / "export.tar.gz"
            export.write_bytes(b"verified export")
            agent_credential = root / "agentos.credential"
            agent_credential.write_text("a" * 64 + "\n", encoding="utf-8")
            for path in (completion, export, agent_credential):
                os.chmod(path, 0o600)
            unlocked = SimpleNamespace(returncode=0, stdout=json.dumps({"state": "UNLOCKED"}))
            with patch("DGXGateway.dgx_gateway.admission.subprocess.run", return_value=unlocked), patch(
                "DGXGateway.dgx_gateway.maintenance.verify_archive", return_value={"valid": True}
            ):
                with self.assertRaises(RuntimeError):
                    activate_agentos_handoff(
                        mac_config, completion, export, agent_credential,
                        lease_factory=FailStartHandoffLease,
                    )
                failed_state = json.loads(next(
                    (mac_config.state_dir / "authority-handoff-operations").glob("*.json")
                ).read_text())
                self.assertEqual((failed_state["phase"], failed_state["outcome"]), ("RELEASED", "FAILED"))
                from DGXGateway.dgx_gateway.store import OperationStore
                store = OperationStore(mac_config.state_dir / "gateway.sqlite3")
                store.set_mutation_fence(False)
                store.close()
                result = activate_agentos_handoff(
                    mac_config, completion, export, agent_credential,
                    lease_factory=FakeHandoffLease,
                )
            self.assertEqual(result["active_authority_id"], "agentos-dgx-gateway")
            with self.assertRaises(AdmissionError):
                MutationAdmission(mac_config).assert_allowed()
            agent_path = root / "agentos.toml"
            agent_text = mac_text.replace(
                f'credential = "{root / "authority.credential"}"',
                f'credential = "{agent_credential}"',
            )
            agent_path.write_text(agent_text, encoding="utf-8")
            agent_config = replace(load_config(agent_path), authority_id="agentos-dgx-gateway")
            self.assertTrue(MutationAdmission(agent_config).assert_allowed()["allowed"])

    def test_copied_authority_root_cannot_leave_original_mac_writer_active(self):
        from DGXGateway.dgx_gateway.admission import (
            AdmissionError, MutationAdmission, activate_agentos_handoff,
        )
        from DGXGateway.dgx_gateway.config import load_config

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            original = base / "original"
            copied = base / "copied"
            original.mkdir()
            copied.mkdir()
            original_path = original / "gateway.toml"
            copied_path = copied / "gateway.toml"
            original_path.write_text(config_text(original), encoding="utf-8")
            copied_path.write_text(config_text(copied), encoding="utf-8")
            original_config = replace(
                load_config(original_path), authority_id="mac-dgx-gateway",
                authority_remote_fence_enabled=True,
            )
            copied_config = replace(
                load_config(copied_path), authority_id="mac-dgx-gateway",
                authority_remote_fence_enabled=True,
            )
            old_credential = "m" * 64
            old_record = {
                "schema": MutationAdmission.SCHEMA,
                "cluster_id": "dgx-head+dgx-worker", "epoch": 1, "phase": "ACTIVE",
                "active_authority_id": "mac-dgx-gateway",
                "credential_sha256": hashlib.sha256(old_credential.encode()).hexdigest(),
            }
            for config in (original_config, copied_config):
                config.authority_credential.write_text(old_credential + "\n", encoding="utf-8")
                config.authority_record.write_text(json.dumps(old_record), encoding="utf-8")
                os.chmod(config.authority_credential, 0o600)
                os.chmod(config.authority_record, 0o600)
            completion = copied / "release.json"
            completion.write_text(json.dumps({
                "cluster_id": "dgx-head+dgx-worker", "terminal_state": "UNLOCKED"
            }), encoding="utf-8")
            export = copied / "export.tar.gz"
            export.write_bytes(b"verified copied export")
            agent_credential = copied / "agentos.credential"
            agent_credential.write_text("n" * 64 + "\n", encoding="utf-8")
            for path in (completion, export, agent_credential):
                os.chmod(path, 0o600)
            remote = {
                "schema": "dgx-gateway-remote-authority-v1",
                "cluster_id": "dgx-head+dgx-worker", "epoch": 1, "phase": "ACTIVE",
                "active_authority_id": "mac-dgx-gateway",
                "credential_sha256": old_record["credential_sha256"],
            }

            def remote_record(_self):
                return dict(remote)

            def remote_cas(_config, expected, payload):
                self.assertEqual(
                    (remote["epoch"], remote["active_authority_id"], remote["phase"]),
                    (expected["epoch"], expected["active_authority_id"], expected["phase"]),
                )
                remote.clear()
                remote.update(payload)

            unlocked = SimpleNamespace(returncode=0, stdout=json.dumps({"state": "UNLOCKED"}))
            with patch.object(MutationAdmission, "_remote_record", remote_record), patch(
                "DGXGateway.dgx_gateway.admission._remote_cas", side_effect=remote_cas
            ), patch(
                "DGXGateway.dgx_gateway.admission.subprocess.run", return_value=unlocked
            ), patch("DGXGateway.dgx_gateway.maintenance.verify_archive", return_value={"valid": True}):
                with patch(
                    "DGXGateway.dgx_gateway.admission._atomic_copy",
                    side_effect=RuntimeError("injected post-fence artifact failure"),
                ), self.assertRaises(RuntimeError):
                    activate_agentos_handoff(
                        copied_config, completion, export, agent_credential,
                        lease_factory=FakeHandoffLease,
                    )
                partial_state = json.loads(next(
                    (copied_config.state_dir / "authority-handoff-operations").glob("*.json")
                ).read_text())
                self.assertEqual(
                    (partial_state["phase"], partial_state["outcome"]),
                    ("RECOVERY_REQUIRED", "RECOVERY_REQUIRED"),
                )
                with self.assertRaises(RuntimeError):
                    activate_agentos_handoff(
                        copied_config, completion, export, agent_credential,
                        lease_factory=FailAcquireHandoffLease,
                    )
                acquire_failed_state = json.loads(next(
                    (copied_config.state_dir / "authority-handoff-operations").glob("*.json")
                ).read_text())
                self.assertEqual(
                    (
                        acquire_failed_state["phase"], acquire_failed_state["outcome"],
                        acquire_failed_state["attempt_phase"], acquire_failed_state["mac_fenced"],
                    ),
                    ("RECOVERY_REQUIRED", "RECOVERY_REQUIRED", "ACQUIRE_FAILED", True),
                )
                with self.assertRaises(RuntimeError):
                    activate_agentos_handoff(
                        copied_config, completion, export, agent_credential,
                        lease_factory=FailReleaseHandoffLease,
                    )
                handoff_state = json.loads(next(
                    (copied_config.state_dir / "authority-handoff-operations").glob("*.json")
                ).read_text())
                self.assertEqual(
                    (handoff_state["phase"], handoff_state["outcome"]),
                    ("RECOVERY_REQUIRED", "COMMITTED"),
                )
                with self.assertRaises(AdmissionError):
                    MutationAdmission(original_config).assert_allowed()
                agent_config = replace(
                    copied_config,
                    authority_id="agentos-dgx-gateway",
                    authority_credential=agent_credential,
                )
                self.assertTrue(MutationAdmission(agent_config).assert_allowed()["allowed"])

    def test_remote_authority_cas_is_linearizable_under_concurrency(self):
        from DGXGateway.dgx_gateway.admission import REMOTE_AUTHORITY_CAS

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "authority.json"
            hostname = socket.gethostname()
            user = pwd.getpwuid(os.getuid()).pw_name

            def argv(expected_epoch, expected_id, expected_phase, payload):
                encoded = base64.b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
                return [
                    "python3", "-c", REMOTE_AUTHORITY_CAS, hostname, user, str(path),
                    str(expected_epoch), expected_id, expected_phase, encoded,
                ]

            initial = {
                "epoch": 1, "active_authority_id": "mac-dgx-gateway", "phase": "ACTIVE"
            }
            self.assertEqual(subprocess.run(argv(0, "", "", initial), capture_output=True).returncode, 0)
            candidate_a = {"epoch": 2, "active_authority_id": "agent-a", "phase": "ACTIVE"}
            candidate_b = {"epoch": 2, "active_authority_id": "agent-b", "phase": "ACTIVE"}
            processes = [
                subprocess.Popen(argv(1, "mac-dgx-gateway", "ACTIVE", candidate), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for candidate in (candidate_a, candidate_b)
            ]
            returncodes = []
            for process in processes:
                process.communicate(timeout=5)
                returncodes.append(process.returncode)
            self.assertEqual(sorted(returncodes), [0, 77])
            final = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn(final["active_authority_id"], {"agent-a", "agent-b"})


if __name__ == "__main__":
    unittest.main()
