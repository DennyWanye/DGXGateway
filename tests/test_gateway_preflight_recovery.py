from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from DGXGateway.dgx_gateway.store import OperationStore
from DGXGateway.dgx_gateway.server import GatewayApplication
from DGXGateway.dgx_gateway.recovery_preflight import recover_preflight_stop


class StoreConcurrencyTests(unittest.TestCase):
    def test_readers_and_lifecycle_writes_share_connection_safely(self):
        with tempfile.TemporaryDirectory() as root:
            store = OperationStore(Path(root) / 'store.sqlite3')
            store.create_operation('operation', None, 'personaplex', 'test-key')
            def write():
                for n in range(100):
                    store.set_phase('operation', 'PREFLIGHT' if n % 2 else 'REQUESTED')
                    store.append_event('operation', 'probe', {'n': n})
            def read():
                for _ in range(100):
                    self.assertEqual(store.get_operation('operation').operation_id, 'operation')
                    self.assertEqual(store.active_operation().operation_id, 'operation')
                    self.assertEqual(store.list_operations()[0].operation_id, 'operation')
                    self.assertTrue(store.events('operation'))
            with ThreadPoolExecutor(max_workers=9) as pool:
                tasks = [pool.submit(write)] + [pool.submit(read) for _ in range(8)]
                for task in tasks:
                    task.result(15)
            store.close()

    def test_uncaught_preflight_failure_becomes_recoverable(self):
        with tempfile.TemporaryDirectory() as root:
            store = OperationStore(Path(root) / 'store.sqlite3')
            store.create_operation('operation', None, 'personaplex', 'test-key')
            app = SimpleNamespace(store=store, operations=SimpleNamespace(execute=Mock(side_effect=KeyError('preflight'))))
            result = GatewayApplication._execute_operation(app, 'operation')
            self.assertEqual(result.phase, 'RECOVERY_REQUIRED')
            self.assertEqual(result.outcome, 'RECOVERY_REQUIRED')
            store.close()


class PreflightRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = SimpleNamespace(state_dir=self.root, lock_cli=Path('/lockcli'), lock_root=self.root/'lock', token_dir=self.root/'tokens')
        self.store = OperationStore(self.root/'gateway.sqlite3')
        with patch('DGXGateway.dgx_gateway.store.utc_now', return_value=(datetime.now(timezone.utc)-timedelta(seconds=60)).isoformat()):
            self.store.create_operation('orphan-op', None, 'personaplex', 'orphan-key')
        self.store.close()
        self.lease = Mock(token_path=self.root/'tokens/orphan-op.token', lease_generation='generation')
        self.lease.release.return_value = {'state': 'UNLOCKED'}
        self.adapters = []
        self.patches = [patch('DGXGateway.dgx_gateway.recovery_preflight.load_config', return_value=self.config), patch('DGXGateway.dgx_gateway.recovery_preflight.MutationAdmission', return_value=SimpleNamespace(assert_allowed=lambda: None)), patch('DGXGateway.dgx_gateway.recovery_preflight.GatewayLease', return_value=self.lease)]
        catalog = {}
        for model in ('mia', 'longcat', 'h3', 'music', 'persona', 'ring'):
            adapter = Mock()
            adapter.stopped_proof.return_value = {'stopped': True}
            catalog[model] = adapter
            self.adapters.append(adapter)
        self.patches.append(patch('DGXGateway.dgx_gateway.recovery_preflight.build_adapters', return_value=catalog))
        for p in self.patches:p.start()

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def test_recovery_only_proves_never_mutates_model(self):
        result=recover_preflight_stop(self.root/'config','orphan-op')
        self.assertEqual(result['phase'],'RELEASED');self.assertEqual(result['outcome'],'FAILED_SAFE_STOPPED')
        self.lease.acquire.assert_called_once();self.lease.release.assert_called_once()
        for a in self.adapters:
            a.stopped_proof.assert_called_once();a.start.assert_not_called();a.stop.assert_not_called();a.retire.assert_not_called()

    def test_existing_cluster_lease_refuses_without_probes(self):
        self.lease.acquire.side_effect=RuntimeError('cluster locked')
        with self.assertRaises(RuntimeError):recover_preflight_stop(self.root/'config','orphan-op')
        for a in self.adapters:a.stopped_proof.assert_not_called()

    def test_failed_stopped_proof_preserves_recovery_lease(self):
        self.adapters[0].stopped_proof.side_effect=RuntimeError('model active')
        with self.assertRaises(RuntimeError):recover_preflight_stop(self.root/'config','orphan-op')
        self.lease.release.assert_not_called()
        store=OperationStore(self.root/'gateway.sqlite3');self.assertEqual(store.get_operation('orphan-op').phase,'RECOVERY_REQUIRED');store.close()

    def rejected_acquire(self, acquired=False):
        store=OperationStore(self.root/'gateway.sqlite3')
        with patch('DGXGateway.dgx_gateway.store.utc_now', return_value=(datetime.now(timezone.utc)-timedelta(seconds=60)).isoformat()):
            store.set_phase('orphan-op','PREFLIGHT')
            if acquired: store.set_phase('orphan-op','LEASE_ACQUIRED')
            store.set_phase('orphan-op','RECOVERY_REQUIRED',error="deployment-lock: ACTIVE lock already owned by 'another'")
            store.freeze_outcome('orphan-op','RECOVERY_REQUIRED')
        store.close()

    def test_rejected_acquire_can_be_proven_stopped_preserving_original_outcome(self):
        self.rejected_acquire()
        result=recover_preflight_stop(self.root/'config','orphan-op')
        self.assertEqual(result['phase'],'RELEASED')
        self.assertEqual(result['outcome'],'RECOVERY_REQUIRED')
        for a in self.adapters:a.start.assert_not_called();a.stop.assert_not_called()

    def test_post_acquire_recovery_never_uses_preflight_shortcut(self):
        self.rejected_acquire(acquired=True)
        with self.assertRaises(ValueError):recover_preflight_stop(self.root/'config','orphan-op')
        self.lease.acquire.assert_not_called()

    def test_young_request_refused(self):
        store=OperationStore(self.root/'gateway.sqlite3');store.set_phase('orphan-op','PREFLIGHT');store.close()
        with self.assertRaisesRegex(ValueError,'30 seconds'):recover_preflight_stop(self.root/'config','orphan-op')
        self.lease.acquire.assert_not_called()


if __name__ == '__main__':unittest.main()
