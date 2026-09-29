import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


def config_text(root: Path) -> str:
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
id = "test-vision"
record = "{root / 'authority.json'}"
credential = "{root / 'authority.credential'}"
mutation_enabled = false
remote_fence_enabled = false
[models.deepseek]
journal = "{root / 'deepseek.jsonl'}"
gateway_secret = "{root / 'deepseek.token'}"
drain_timeout_seconds = 10
[models.deepseek_vision]
journal = "{root / 'vision.jsonl'}"
gateway_secret = "{root / 'vision.token'}"
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


class FakeLease:
    def __init__(self, events, operation_id):
        self.events = events
        self.operation_id = operation_id
        self.token_path = Path("/tmp/fake-vision.token")
        self.lease_generation = "a" * 32

    def acquire(self):
        self.events.append("lease.acquire")
        return {"lease_generation": self.lease_generation}

    def start_heartbeat(self):
        self.events.append("lease.heartbeat")

    def check_supervision(self):
        self.events.append("lease.guard")

    def release(self):
        self.events.append("lease.release")
        return {"state": "UNLOCKED", "release_operation_id": self.operation_id}


class FakeAdapter:
    def __init__(self, model_id, events, state="STOPPED"):
        self.model_id = model_id
        self.events = events
        self.state = state

    def observe(self):
        from DGXGateway.dgx_gateway.domain import ObservedModel

        return ObservedModel(self.model_id, self.state)

    def drain(self, _context):
        self.events.append(f"{self.model_id}.drain")

    def stop(self, _context):
        self.events.append(f"{self.model_id}.stop")
        self.state = "STOPPED"

    def retire(self, _context):
        self.events.append(f"{self.model_id}.retire")
        self.state = "STOPPED"

    def stopped_proof(self, context):
        self.events.append(f"{self.model_id}.stopped-proof")
        if self.state != "STOPPED":
            raise RuntimeError("not stopped")
        return {
            "model_id": self.model_id,
            "stopped": True,
            "lease_generation": context.lease_generation,
            "sha256": "b" * 64,
        }

    def start(self, _context, _profile=None):
        self.events.append(f"{self.model_id}.start")
        self.state = "HEALTHY"

    def health(self, _context):
        self.events.append(f"{self.model_id}.health")
        return {"healthy": self.state == "HEALTHY"}


class FailingVisionAdapter(FakeAdapter):
    def __init__(self, events):
        super().__init__("deepseek-v4-flash-vision-exp", events)
        self.cleanup_available = False

    def health(self, _context):
        self.events.append(f"{self.model_id}.health")
        return {"healthy": False}

    def retire(self, _context):
        self.events.append(f"{self.model_id}.retire")
        if not self.cleanup_available:
            raise RuntimeError("worker unreachable")
        self.state = "STOPPED"

    def fail_close_head(self, context):
        self.events.append(f"{self.model_id}.fail-close-head")
        return {
            "model_id": self.model_id,
            "head_fail_closed": True,
            "maintenance_active": True,
            "recovery_id": context.operation_id,
            "sha256": "c" * 64,
        }


class DeepSeekVisionGatewayTest(unittest.TestCase):
    def make_config(self, directory: str):
        from DGXGateway.dgx_gateway.config import load_config

        path = Path(directory) / "gateway.toml"
        path.write_text(config_text(Path(directory)), encoding="utf-8")
        return path, load_config(path)

    def test_legacy_deepseek_stopped_probe_ignores_foreign_shared_gateway_port(self):
        from DGXGateway.dgx_gateway.adapters.deepseek import DeepSeekAdapter

        probe = {
            "head": {"ok": True, "stdout": "dgx-head\noperator\nLISTEN 0 511 192.0.2.30:11434\n"},
            "worker": {"ok": True, "stdout": "dgx-worker\noperator\n"},
        }
        self.assertTrue(DeepSeekAdapter._probe_is_stopped(probe))
        probe["head"]["stdout"] += (
            "LEGACY_GATEWAY deepseek-v4-flash-gateway Up 1 minute\n"
        )
        self.assertFalse(DeepSeekAdapter._probe_is_stopped(probe))

    def test_config_requires_distinct_vision_paths_and_internal_port(self):
        from DGXGateway.dgx_gateway.config import load_config

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "gateway.toml"
            text = config_text(root).replace(
                f'journal = "{root / "vision.jsonl"}"',
                f'journal = "{root / "deepseek.jsonl"}"',
            )
            path.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "journals must be distinct"):
                load_config(path)
            path.write_text(
                config_text(root).replace("internal_port = 18889", "internal_port = 18888"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "conflicts"):
                load_config(path)

            path.write_text(
                config_text(root).replace("[models.deepseek_vision]", "[models.unused]"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "deepseek_vision is required"):
                load_config(path)

    def test_exact_probe_requires_role_scoped_labels_and_one_rank_each(self):
        from DGXGateway.dgx_gateway.adapters.paired_probe import (
            PairedComposeProbeSpec,
            paired_probe_state,
            probe_paired_compose_target,
        )
        from DGXGateway.dgx_gateway.ssh import CommandResult

        class SSH:
            def run(self, node, argv, timeout):
                script = argv[-1]
                self.assertions(node, script)
                container = (
                    'CONTAINER\t{"ID":"' + node.role + '","Status":"Up 1 minute"}\n'
                )
                listener = "LISTENER\tLISTEN 0 4096 127.0.0.1:18889 0.0.0.0:*\n" if node.role == "head" else ""
                unit = "UNIT\tloaded\nUNIT\tactive\n" if node.role == "head" else ""
                return CommandResult(
                    0,
                    f"HOST\t{node.hostname}\nUSER\t{node.user}\n{container}{listener}{unit}",
                    "",
                )

            def assertions(self, node, script):
                for exact in (
                    "label=com.docker.compose.project=deepseek-v4-flash-vision-exp",
                    "label=com.docker.compose.service=vllm-vision",
                    "label=com.dgx-spark.model-id=deepseek-v4-flash-vision-exp",
                    f"label=com.dgx-spark.role={node.role}",
                ):
                    if exact not in script:
                        raise AssertionError(exact)

        with tempfile.TemporaryDirectory() as directory:
            _, config = self.make_config(directory)
            probe = probe_paired_compose_target(
                SSH(),
                config.nodes,
                PairedComposeProbeSpec(
                    project="deepseek-v4-flash-vision-exp",
                    service="vllm-vision",
                    model_id="deepseek-v4-flash-vision-exp",
                    unit="deepseek-v4-flash-vision-exp.service",
                    internal_port=18889,
                ),
            )
            self.assertEqual(paired_probe_state(probe), "RUNNING")
            probe["worker"]["containers"] = []
            self.assertEqual(paired_probe_state(probe), "PARTIAL")

    def test_health_requires_native_visual_canary_and_both_ranks(self):
        from DGXGateway.dgx_gateway.adapters.deepseek_vision import DeepSeekVisionAdapter
        from DGXGateway.dgx_gateway.domain import ObservedModel

        class Controller:
            def acceptance_health(self, operation_id, _under_active):
                return {
                    "healthy": True,
                    "model_id": "deepseek-v4-flash-vision-exp",
                    "native_vision": True,
                    "canary_passed": True,
                    "head_rank": True,
                    "worker_rank": True,
                }

        with tempfile.TemporaryDirectory() as directory:
            _, config = self.make_config(directory)
            adapter = DeepSeekVisionAdapter(config)
            adapter.observe = lambda: ObservedModel(adapter.model_id, "HEALTHY")
            adapter._controller = lambda _context: Controller()
            context = SimpleNamespace(
                operation_id="op-health",
                guard=lambda: None,
            )
            self.assertTrue(adapter.health(context)["healthy"])
            Controller.acceptance_health = lambda *_args, **_kwargs: {"healthy": True}
            self.assertFalse(adapter.health(context)["healthy"])

    def test_worker_cleanup_failure_records_single_durable_obligation(self):
        from DGXGateway.dgx_gateway.operations import OperationController
        from DGXGateway.dgx_gateway.store import OperationStore

        events = []
        source = FakeAdapter("minimax-h3", events, state="HEALTHY")
        target = FailingVisionAdapter(events)
        with tempfile.TemporaryDirectory() as directory:
            store = OperationStore(Path(directory) / "gateway.sqlite3")
            operation = store.create_operation(
                "op-worker-loss", source.model_id, target.model_id, "worker-loss-key"
            )
            controller = OperationController(
                store=store,
                adapters={source.model_id: source, target.model_id: target},
                lease_factory=lambda operation_id: FakeLease(events, operation_id),
            )
            deferred = controller.execute(operation.operation_id)
            self.assertEqual((deferred.phase, deferred.outcome), ("RECOVERY_REQUIRED", "RECOVERY_REQUIRED"))
            self.assertIn(f"{target.model_id}.fail-close-head", events)
            self.assertNotIn(f"{source.model_id}.start", events)
            self.assertNotIn("lease.release", events)
            obligations = [
                event for event in store.events(operation.operation_id)
                if event["kind"] == "degraded-recovery-obligation"
            ]
            self.assertEqual(len(obligations), 1)
            self.assertEqual(obligations[0]["payload"]["source_model"], source.model_id)

            target.cleanup_available = True
            class ResumeLease(FakeLease):
                def acquire(self):
                    raise AssertionError("durable recovery must reuse the live lease")

                def start_heartbeat(self):
                    self.events.append(f"resume.heartbeat:{self.lease_generation}")

                def check_supervision(self):
                    if self.lease_generation != "a" * 32:
                        raise AssertionError("stored lease generation was not restored")
                    self.events.append("resume.guard")

            resumed = controller.resume_degraded_recovery(
                operation.operation_id, ResumeLease(events, operation.operation_id)
            )
            self.assertEqual((resumed.phase, resumed.outcome), ("RELEASED", "RECOVERY_REQUIRED"))
            self.assertEqual(events.count("lease.acquire"), 1)
            self.assertIn(f"resume.heartbeat:{'a' * 32}", events)
            self.assertEqual(
                len([
                    event for event in store.events(operation.operation_id)
                    if event["kind"] == "degraded-recovery-resolved"
                ]),
                1,
            )
            self.assertIn(f"{source.model_id}.start", events)
            self.assertIn("lease.release", events)

            # If the process dies after releasing the lock but before writing the
            # final resolution event, the same durable operation can backfill it.
            store._db.execute(
                "DELETE FROM events WHERE operation_id=? AND kind=?",
                (operation.operation_id, "degraded-recovery-resolved"),
            )
            backfilled = controller.resume_degraded_recovery(
                operation.operation_id, FakeLease(events, operation.operation_id)
            )
            self.assertEqual(backfilled.phase, "RELEASED")
            self.assertEqual(
                len([
                    event for event in store.events(operation.operation_id)
                    if event["kind"] == "degraded-recovery-resolved"
                ]),
                1,
            )

    def test_generic_failed_switch_recovery_restores_source_under_live_lease(self):
        from DGXGateway.dgx_gateway.operations import OperationController
        from DGXGateway.dgx_gateway.store import OperationStore

        events = []
        source = FakeAdapter("deepseek-v4-flash-vision-exp", events, state="STOPPED")
        target = FakeAdapter("minimax-h3", events, state="STOPPED")

        class ResumeLease(FakeLease):
            lease_generation = None

            def guard(self):
                return {"state": "ACTIVE", "lease_generation": "e" * 32}

            def start_heartbeat(self):
                events.append("generic-resume.heartbeat")

            def check_supervision(self):
                if self.lease_generation != "e" * 32:
                    raise AssertionError("live lease generation was not bound")
                events.append("generic-resume.guard")

        with tempfile.TemporaryDirectory() as directory:
            store = OperationStore(Path(directory) / "gateway.sqlite3")
            operation = store.create_operation(
                "op-generic-recovery", source.model_id, target.model_id, "generic-key"
            )
            store.set_phase(operation.operation_id, "SOURCE_STOPPED_PROVEN")
            store.set_phase(operation.operation_id, "TARGET_STARTING")
            store.set_phase(operation.operation_id, "RECOVERY_REQUIRED", error="ssh reset")
            store.freeze_outcome(operation.operation_id, "RECOVERY_REQUIRED")
            controller = OperationController(
                store=store,
                adapters={source.model_id: source, target.model_id: target},
                lease_factory=lambda operation_id: ResumeLease(events, operation_id),
            )
            resumed = controller.resume_degraded_recovery(
                operation.operation_id, ResumeLease(events, operation.operation_id)
            )
            self.assertEqual((resumed.phase, resumed.outcome), ("RELEASED", "RECOVERY_REQUIRED"))
            self.assertIn(f"{target.model_id}.retire", events)
            self.assertIn(f"{target.model_id}.stopped-proof", events)
            self.assertIn(f"{source.model_id}.start", events)
            self.assertIn(f"{source.model_id}.health", events)
            self.assertIn("lease.release", events)

    def test_application_registers_vision_as_mutable_operation_adapter(self):
        from DGXGateway.dgx_gateway.__main__ import build_application

        with tempfile.TemporaryDirectory() as directory:
            config_path, _ = self.make_config(directory)
            app = build_application(config_path)
            try:
                self.assertEqual(
                    set(app.operations.adapters),
                    {
                        "minimax-h3",
                        "comfyui",
                        "personaplex",
                        "mia-dsv41-ablit",
                        "longcat-video-avatar-1.5",
                        "dsv41-ring-tp4",
                        "h3-ring4",
                        "h3-dual-pair",
                    },
                )
                self.assertIn("dsv41-ring-tp4", app.snapshot.adapters)
                self.assertNotIn("deepseek-v4-flash-vision-exp", app.snapshot.adapters)
            finally:
                app.store.close()


class FourNodePairedProbeTests(unittest.TestCase):
    def four_nodes(self):
        from DGXGateway.dgx_gateway.config import NodeConfig

        return (
            NodeConfig("head", "dgx-head", "192.0.2.41", "operator"),
            NodeConfig("worker", "dgx-worker", "192.0.2.42", "operator"),
            NodeConfig("rank2", "dgx-rank2", "192.0.2.39", "operator"),
            NodeConfig("rank3", "dgx-rank3", "192.0.2.40", "operator"),
        )

    def paired_ssh(self):
        from DGXGateway.dgx_gateway.ssh import CommandResult

        ssh = Mock()

        def run(node, argv, timeout=12):
            self.assertIn(node.role, {"head", "worker"})
            return CommandResult(0, f"HOST\t{node.hostname}\nUSER\t{node.user}\n", "")

        ssh.run.side_effect = run
        return ssh

    def test_probe_ignores_rank2_and_rank3(self):
        from DGXGateway.dgx_gateway.adapters.paired_probe import (
            PairedComposeProbeSpec,
            paired_probe_state,
            probe_paired_compose_target,
        )

        ssh = self.paired_ssh()
        probe = probe_paired_compose_target(
            ssh,
            self.four_nodes(),
            PairedComposeProbeSpec("vision", "api", "deepseek-v4-flash-vision-exp", "vision.service", 18889),
        )
        self.assertEqual(set(probe), {"head", "worker"})
        self.assertEqual(paired_probe_state(probe), "STOPPED")
        self.assertEqual(ssh.run.call_count, 2)

    def test_mia_probe_ignores_rank2_and_rank3(self):
        from DGXGateway.dgx_gateway.adapters.mia_dsv41 import MiaDsv41Adapter

        adapter = object.__new__(MiaDsv41Adapter)
        adapter.config = SimpleNamespace(nodes=self.four_nodes())
        adapter.ssh = self.paired_ssh()
        probe = adapter._probe()
        self.assertEqual(set(probe), {"head", "worker"})
        self.assertEqual(adapter.ssh.run.call_count, 2)

    def test_h3_stopped_probe_ignores_rank2_and_rank3(self):
        from DGXGateway.dgx_gateway.adapters.h3 import H3Adapter

        adapter = object.__new__(H3Adapter)
        adapter.config = SimpleNamespace(nodes=self.four_nodes())
        adapter.ssh = self.paired_ssh()
        evidence = adapter._probe_stopped()
        self.assertEqual(set(evidence), {"head", "worker"})
        roles = [call.args[0].role for call in adapter.ssh.run.call_args_list]
        self.assertTrue(set(roles) <= {"head", "worker"})
        assert_cmds = [
            call.args[1] for call in adapter.ssh.run.call_args_list
            if call.args[1][:1] == ["sudo"]
        ]
        self.assertEqual(len(assert_cmds), 2)

    def test_retired_dual_node_control_is_stopped_not_unknown(self):
        from DGXGateway.dgx_gateway.adapters.h3 import H3Adapter
        from DGXGateway.dgx_gateway.ssh import CommandResult

        adapter = object.__new__(H3Adapter)
        adapter.config = SimpleNamespace(nodes=self.four_nodes())
        adapter.last_profile = "heretic-ref2va-v1"
        adapter.ssh = Mock()
        adapter.ssh.run.return_value = CommandResult(1, "", "")
        adapter._authority = lambda: (_ for _ in ()).throw(
            AssertionError("retired dual-node control must not be queried")
        )
        observed = adapter.observe()
        self.assertEqual(observed.model_id, "minimax-h3")
        self.assertEqual(observed.state, "STOPPED")
        self.assertIsNone(observed.profile)
        adapter.ssh.run.assert_called_once()
        node, argv = adapter.ssh.run.call_args.args[:2]
        self.assertEqual(node.role, "head")
        self.assertEqual(argv, ["test", "-x", "/usr/local/sbin/minimax-h3-control"])

    def test_missing_h3_control_ssh_failure_stays_unknown(self):
        from DGXGateway.dgx_gateway.adapters.h3 import H3Adapter
        from DGXGateway.dgx_gateway.ssh import CommandResult

        adapter = object.__new__(H3Adapter)
        adapter.config = SimpleNamespace(nodes=self.four_nodes())
        adapter.last_profile = "heretic-ref2va-v1"
        adapter.ssh = Mock()
        adapter.ssh.run.return_value = CommandResult(255, "", "ssh failed")
        adapter._authority = lambda: SimpleNamespace(
            status=Mock(side_effect=RuntimeError("head role verification failed"))
        )
        observed = adapter.observe()
        self.assertEqual(observed.state, "UNKNOWN")
        self.assertEqual(observed.profile, "heretic-ref2va-v1")

    def test_retired_dual_node_stopped_proof_does_not_need_old_scripts(self):
        from DGXGateway.dgx_gateway.adapters.h3 import H3Adapter
        from DGXGateway.dgx_gateway.ssh import CommandResult

        adapter = object.__new__(H3Adapter)
        adapter.config = SimpleNamespace(nodes=self.four_nodes())
        adapter.ssh = Mock()
        adapter.ssh.run.return_value = CommandResult(1, "", "")
        context = SimpleNamespace(guard=lambda: None, lease_generation=7)
        proof = adapter.stopped_proof(context)
        self.assertTrue(proof["stopped"])
        self.assertEqual(proof["model_id"], "minimax-h3")
        adapter.ssh.run.assert_called_once()
        self.assertEqual(
            adapter.ssh.run.call_args.args[1],
            ["test", "-x", "/usr/local/sbin/minimax-h3-control"],
        )

    def test_deepseek_probe_ignores_rank2_and_rank3(self):
        from DGXGateway.dgx_gateway.adapters.deepseek import DeepSeekAdapter

        adapter = object.__new__(DeepSeekAdapter)
        adapter.config = SimpleNamespace(nodes=self.four_nodes())
        adapter.ssh = self.paired_ssh()
        probe = adapter._probe()
        self.assertEqual(set(probe), {"head", "worker"})
        self.assertEqual(adapter.ssh.run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
