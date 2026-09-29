from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from DGXGateway.dgx_gateway.adapters.personaplex import PersonaPlexAdapter
from DGXGateway.dgx_gateway.operations import OperationContext


class PersonaPlexAdapterTests(unittest.TestCase):
    def setUp(self):
        node = SimpleNamespace(role='head', hostname='dgx-head', address='192.0.2.30')
        self.adapter = PersonaPlexAdapter(SimpleNamespace(nodes=[node], personaplex_root=Path('/tmp/personaplex')), ssh=Mock())
        self.context = OperationContext('operation', Path('/tmp/token'), 'generation', lambda: None)
        self.stopped = {'unit_state': 'inactive', 'main_pid': '0', 'cgroup': '', 'listener': False, 'process': False}

    def test_draining_remains_classifiable_for_gateway_stop(self):
        self.adapter._runtime_probe = Mock(return_value={**self.stopped, 'unit_state': 'active', 'main_pid': '123', 'listener': True, 'process': True})
        self.adapter._request = Mock(return_value={'model': 'personaplex', 'ready': True, 'draining': True, 'active_sessions': 0})
        self.assertEqual(self.adapter.observe().state, 'HEALTHY')

    def test_process_or_cgroup_prevents_stop_proof(self):
        for field, value in [('process', True), ('listener', True), ('main_pid', '12'), ('cgroup', '/user.slice/residual')]:
            self.adapter._runtime_probe = Mock(return_value={**self.stopped, field: value})
            with self.assertRaises(RuntimeError):
                self.adapter.stopped_proof(self.context)

    def test_stop_receipt_bound_to_operation(self):
        self.adapter._runtime_probe = Mock(return_value=self.stopped)
        proof = self.adapter.stopped_proof(self.context)
        self.assertEqual(proof['operation_id'], 'operation')
        self.assertEqual(proof['lease_generation'], 'generation')
        self.assertEqual(proof['host'], 'dgx-head')
        self.assertEqual(len(proof['sha256']), 64)

    def test_drain_preserves_active_session_then_finishes(self):
        self.adapter._runtime_probe = Mock(return_value={**self.stopped, 'unit_state': 'active', 'main_pid': '123'})
        self.adapter._request = Mock(side_effect=[{'draining': True}, {'draining': True, 'active_sessions': 0}])
        self.adapter.drain(self.context)
        self.adapter._request.assert_any_call('/drain', 'POST')

    def test_unknown_probe_is_not_stopped(self):
        self.adapter._runtime_probe = Mock(side_effect=RuntimeError('ssh failed'))
        self.assertEqual(self.adapter.observe().state, 'UNKNOWN')
        with self.assertRaises(RuntimeError):
            self.adapter.stopped_proof(self.context)


if __name__ == '__main__':
    unittest.main()
