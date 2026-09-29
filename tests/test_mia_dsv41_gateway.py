import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'mia_action_test', ROOT / 'scripts/mia-dsv41-ablit/runtime-action.py')
action = importlib.util.module_from_spec(spec)
spec.loader.exec_module(action)


class ReceiptTests(unittest.TestCase):
    def receipt(self, role='head', verb='start'):
        return dict(
            schema='mia-dsv41-receipt-v1', project='mia-dsv41-ablit', role=role, verb=verb,
            operation_id='test-operation', lease_generation='generation-001', nonce='nonce-001',
            not_after=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())

    def test_valid_head_and_worker(self):
        for role in ('head', 'worker'):
            start = self.receipt(role, 'start')
            start['profile'] = '256K'
            self.assertEqual(action.validate(start, role, 'start')['role'], role)
            self.assertEqual(action.validate(self.receipt(role, 'stop'), role, 'stop')['role'], role)

    def test_wrong_binding_rejected(self):
        for field, value in [
            ('schema', 'qwen38-receipt-v1'), ('project', 'qwen38-flash-next'),
            ('role', 'worker'), ('verb', 'stop'), ('nonce', '../unsafe'),
        ]:
            row = self.receipt()
            row[field] = value
            with self.assertRaises(ValueError):
                action.validate(row, 'head', 'start')

    def test_expiry_and_far_future(self):
        for minutes in (-1, 8):
            row = self.receipt()
            row['not_after'] = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
            with self.assertRaises(ValueError):
                action.validate(row, 'head', 'start')

    def test_secret_never_worker_or_start(self):
        for role, verb in [('worker', 'start'), ('head', 'start'), ('head', 'drain')]:
            row = self.receipt(role, verb)
            if verb == 'start':
                row['profile'] = '256K'
            row['gateway_token'] = 'x' * 43
            with self.assertRaises(ValueError):
                action.validate(row, role, verb)

    def test_gateway_secret_validation(self):
        row = self.receipt('head', 'gateway-production')
        row['gateway_token'] = 'x' * 43
        action.validate(row, 'head', 'gateway-production')
        row['gateway_token'] = 'bad"; return 200;'
        with self.assertRaises(ValueError):
            action.validate(row, 'head', 'gateway-production')

    def test_profiles_are_bound_to_start(self):
        for role in ('head', 'worker'):
            for profile in ('256K', '512K'):
                row = self.receipt(role)
                row['profile'] = profile
                action.validate(row, role, 'start')
            for profile in ('128K', '32K', '256k', 262144, None):
                row = self.receipt(role)
                row['profile'] = profile
                with self.assertRaises(ValueError):
                    action.validate(row, role, 'start')
        row = self.receipt('head', 'stop')
        row['profile'] = '256K'
        with self.assertRaises(ValueError):
            action.validate(row, 'head', 'stop')


class ProfileEnvTests(unittest.TestCase):
    def test_appends_and_replaces_gateway_profile_block(self):
        original = 'MAX_MODEL_LEN=131072\nKV_CACHE_MEMORY_BYTES=1\n'
        first = action.apply_profile_env(original, '256K')
        self.assertIn('MAX_MODEL_LEN=262144', first)
        self.assertIn('MAX_NUM_SEQS=2', first)
        self.assertIn('KV_CACHE_MEMORY_BYTES=2684354560', first)
        self.assertIn('SERVED_MODEL_NAME=mia-dsv41-ablit', first)
        self.assertIn('DSV41_BOOT_MARGIN_GIB=8', first)
        self.assertEqual(first.count('BEGIN-GATEWAY-PROFILE'), 1)
        second = action.apply_profile_env(first, '512K')
        self.assertEqual(second.count('BEGIN-GATEWAY-PROFILE'), 1)
        self.assertIn('MAX_MODEL_LEN=524288', second)
        self.assertIn('MAX_NUM_SEQS=1', second)
        self.assertIn('KV_CACHE_MEMORY_BYTES=5368709120', second)
        self.assertNotIn('MAX_MODEL_LEN=262144', second.split('BEGIN-GATEWAY-PROFILE', 1)[1])

    def test_unknown_profile_rejected(self):
        with self.assertRaises(ValueError):
            action.apply_profile_env('', '128K')


class StartupLogTests(unittest.TestCase):
    def test_traceback_and_socket_fallback_are_fatal(self):
        for line in (
            'Traceback (most recent call last):',
            'NCCL ERROR failed',
            'NCCL INFO Using network Socket',
            'CUDA error: out of memory',
            'Segmentation fault',
        ):
            self.assertIsNotNone(action.fatal_startup(line))

    def test_nccl_warn_alone_is_not_fatal(self):
        self.assertIsNone(action.fatal_startup('dgx-head:123 [0] NCCL WARN plugin missing'))


class GatewayConfTests(unittest.TestCase):
    def test_lan_gateway_injects_upstream_key_and_keeps_client_auth(self):
        conf = (ROOT / 'containers/mia-dsv41-ablit/gateway.conf').read_text()
        self.assertIn('listen 192.0.2.30:11434', conf)
        self.assertIn('proxy_pass http://127.0.0.1:8888', conf)
        self.assertIn('upstream-auth.conf', conf)
        self.assertNotIn('proxy_set_header Authorization "";', conf)
        self.assertIn('mia-dsv41-auth.conf', conf)


class DrainTests(unittest.TestCase):
    def test_close_admission_before_waiting_for_inflight(self):
        from DGXGateway.dgx_gateway.adapters.mia_dsv41 import MiaDsv41Adapter
        obj = object.__new__(MiaDsv41Adapter)
        obj.observe = Mock(return_value=Mock(state='HEALTHY'))
        controller = Mock()
        obj._controller = Mock(return_value=controller)
        samples = iter([(1., 0.), (0., 0.), (0., 0.), (0., 0.)])

        def metrics():
            controller.drain_admission.assert_called_once_with('op-test')
            return next(samples)

        obj._request_metrics = Mock(side_effect=metrics)
        context = Mock(operation_id='op-test')
        with patch('DGXGateway.dgx_gateway.adapters.mia_dsv41.time.sleep'):
            obj.drain(context)
        self.assertEqual(obj._request_metrics.call_count, 4)
        self.assertEqual(obj.observe.call_count, 1)

    def test_refuse_unqualified_profile_before_start(self):
        from DGXGateway.dgx_gateway.adapters.mia_dsv41 import MiaDsv41Adapter
        obj = object.__new__(MiaDsv41Adapter)
        obj._controller = Mock()
        with self.assertRaises(ValueError):
            obj.start(Mock(), '128K')
        obj._controller.assert_not_called()

    def test_start_checks_actual_requested_profile(self):
        from DGXGateway.dgx_gateway.adapters.mia_dsv41 import MiaDsv41Adapter
        obj = object.__new__(MiaDsv41Adapter)
        controller = Mock()
        obj._controller = Mock(return_value=controller)
        obj.observe = Mock(return_value=Mock(state='HEALTHY', profile='256K'))
        with self.assertRaises(RuntimeError):
            obj.start(Mock(operation_id='op-test'), '512K')
        controller.start.assert_called_once_with('op-test', profile='512K')


if __name__ == '__main__':
    unittest.main()
