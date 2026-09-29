"""Context changes must restart both ranks and retain the old profile for rollback."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from DGXGateway.dgx_gateway.adapters.deepseek_vision import DeepSeekVisionAdapter
from DGXGateway.dgx_gateway.domain import ObservedModel
from DGXGateway.dgx_gateway.operations import OperationController
from DGXGateway.dgx_gateway.server import GatewayApplication
from DGXGateway.dgx_gateway.store import OperationStore
from test_dgx_gateway_operations import FakeAdapter, FakeLease

MODEL = DeepSeekVisionAdapter.model_id


class ContextTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = OperationStore(Path(self.temp.name) / 'state.sqlite3')
        self.addCleanup(self.store._db.close)

    def application(self, profile='32K'):
        adapter = Mock(profiles=DeepSeekVisionAdapter.profiles, default_profile='256K')
        adapter.observe.return_value = ObservedModel(MODEL, 'HEALTHY', profile)
        return GatewayApplication(config=SimpleNamespace(), store=self.store,
            operations=SimpleNamespace(adapters={MODEL: adapter}), snapshot=None,
            static_dir=Path(self.temp.name), admission=SimpleNamespace(assert_allowed=Mock()))

    def test_new_context_starts_operation_and_captures_actual_source(self):
        app = self.application()
        with patch('DGXGateway.dgx_gateway.server.threading.Thread') as thread:
            op, created = app.submit_switch(MODEL, '256K', 'context-test')
        self.assertTrue(created)
        thread.return_value.start.assert_called_once()
        self.assertEqual((op.source_profile, op.target_profile), ('32K', '256K'))
        self.assertIsNone(op.outcome)

    def test_same_context_is_noop_and_persisted(self):
        app = self.application('256K')
        with patch('DGXGateway.dgx_gateway.server.threading.Thread') as thread:
            op, _ = app.submit_switch(MODEL, None, 'context-default')
        thread.assert_not_called()
        self.assertEqual(op.outcome, 'COMMITTED')
        self.assertEqual(self.store.last_committed_profile(MODEL), '256K')

    def test_invalid_context_rejected_before_operation(self):
        app = self.application()
        for value in ('1M', '256K;reboot', 262144, True, []):
            with self.assertRaises(ValueError):
                app.submit_switch(MODEL, value, 'invalid-context')
        self.assertEqual(self.store.list_operations(), [])

    def test_failed_larger_context_rolls_back_same_model(self):
        events = []
        class Adapter(FakeAdapter):
            def start(self, context, profile=None):
                super().start(context, profile)
                if profile == '512K':
                    raise RuntimeError('insufficient KV cache')
        adapter = Adapter(MODEL, events, state='HEALTHY')
        op = self.store.create_operation('context-rollback', MODEL, MODEL, 'rollback-key',
                                         '512K', source_profile='256K')
        controller = OperationController(store=self.store, adapters={MODEL: adapter},
            lease_factory=lambda op_id: FakeLease(events, op_id))
        controller.execute(op.operation_id)
        final = self.store.get_operation(op.operation_id)
        self.assertEqual(final.outcome, 'ROLLED_BACK')
        self.assertEqual(final.phase, 'RELEASED')
        self.assertEqual(adapter.started_profiles, ['512K', '256K'])
        self.assertIsNone(self.store.last_committed_profile(MODEL))

    def test_actual_api_limit_drives_observed_profile(self):
        adapter = object.__new__(DeepSeekVisionAdapter)
        for tokens, expected in ((32768, '32K'), (262144, '256K'), (524288, '512K'), (123, None)):
            with patch.object(adapter, '_probe'), patch('DGXGateway.dgx_gateway.adapters.deepseek_vision.paired_probe_state', return_value='RUNNING'), patch.object(adapter, '_api_model', return_value={'max_model_len': tokens}):
                value = adapter.observe()
            self.assertEqual(value.profile, expected)
            self.assertEqual(value.state, 'HEALTHY' if expected else 'PARTIAL')


if __name__ == '__main__':
    unittest.main()
