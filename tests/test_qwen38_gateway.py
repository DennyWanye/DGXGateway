import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('qwen_action_test', ROOT/'scripts/qwen38-flash-next/runtime-action.py')
action = importlib.util.module_from_spec(spec)
spec.loader.exec_module(action)

class ReceiptTests(unittest.TestCase):
    def receipt(self, role='head', verb='start'):
        return dict(schema='qwen38-receipt-v1', project='qwen38-flash-next', role=role, verb=verb,
                    operation_id='test-operation', lease_generation='generation-001', nonce='nonce-001',
                    not_after=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat())
    def test_valid_head_and_worker(self):
        for role in ('head','worker'):
            for verb in ('start','stop'):
                self.assertEqual(action.validate(self.receipt(role,verb),role,verb)['role'],role)
    def test_wrong_binding_rejected(self):
        for field,value in [('schema','other'),('project','deepseek-v4-flash'),('role','worker'),('verb','stop'),('nonce','../unsafe')]:
            row=self.receipt();row[field]=value
            with self.assertRaises(ValueError): action.validate(row,'head','start')
    def test_expiry_and_far_future(self):
        for minutes in (-1,8):
            row=self.receipt();row['not_after']=(datetime.now(timezone.utc)+timedelta(minutes=minutes)).isoformat()
            with self.assertRaises(ValueError): action.validate(row,'head','start')
    def test_secret_never_worker_or_start(self):
        for role,verb in [('worker','start'),('head','start'),('head','drain')]:
            row=self.receipt(role,verb);row['gateway_token']='x'*43
            with self.assertRaises(ValueError): action.validate(row,role,verb)
    def test_gateway_secret_validation(self):
        row=self.receipt('head','gateway-production');row['gateway_token']='x'*43
        action.validate(row,'head','gateway-production')
        row['gateway_token']='bad"; return 200;'
        with self.assertRaises(ValueError): action.validate(row,'head','gateway-production')
    def test_profiles_are_bound_to_start(self):
        for role in ('head','worker'):
            for profile in ('32K','256K'):
                row=self.receipt(role);row['profile']=profile
                action.validate(row,role,'start')
            for profile in ('512K','256k',262144,None):
                row=self.receipt(role);row['profile']=profile
                with self.assertRaises(ValueError): action.validate(row,role,'start')
        row=self.receipt('head','stop');row['profile']='256K'
        with self.assertRaises(ValueError): action.validate(row,'head','stop')

class StartupLogTests(unittest.TestCase):
    def test_successful_real_runtime_logs_are_not_fatal(self):
        for name in ('functional-head.log','model-smoke-kv8-head.log'):
            log=(ROOT/'plans/qwen38-flash-next/evidence/deploy'/name).read_text()
            self.assertIsNone(action.fatal_startup(log))
    def test_actual_warning_and_socket_fallback_are_fatal(self):
        for line in ('host:123 [0] NCCL WARN transport failure', 'host NCCL ERROR failed',
                     'NCCL INFO Using network Socket', 'Traceback (most recent call last):', 'CUDA error: out of memory'):
            self.assertIsNotNone(action.fatal_startup(line))

class DrainTests(unittest.TestCase):
    def test_close_admission_before_waiting_for_inflight(self):
        from DGXGateway.dgx_gateway.adapters.qwen38 import Qwen38Adapter
        obj=object.__new__(Qwen38Adapter)
        obj.observe=Mock(return_value=Mock(state='HEALTHY'))
        controller=Mock();obj._controller=Mock(return_value=controller)
        samples=iter([(1.,0.),(0.,0.),(0.,0.),(0.,0.)])
        def metrics():
            controller.drain_admission.assert_called_once_with('op-test')
            return next(samples)
        obj._request_metrics=Mock(side_effect=metrics)
        context=Mock(operation_id='op-test')
        with patch('DGXGateway.dgx_gateway.adapters.qwen38.time.sleep'):
            obj.drain(context)
        self.assertEqual(obj._request_metrics.call_count,4)
        self.assertEqual(obj.observe.call_count,1)
    def test_refuse_unqualified_profile_before_start(self):
        from DGXGateway.dgx_gateway.adapters.qwen38 import Qwen38Adapter
        obj=object.__new__(Qwen38Adapter);obj._controller=Mock()
        with self.assertRaises(ValueError): obj.start(Mock(),'64K')
        obj._controller.assert_not_called()
    def test_start_checks_actual_requested_profile(self):
        from DGXGateway.dgx_gateway.adapters.qwen38 import Qwen38Adapter
        obj=object.__new__(Qwen38Adapter);controller=Mock();obj._controller=Mock(return_value=controller)
        obj.observe=Mock(return_value=Mock(state='HEALTHY',profile='32K'))
        with self.assertRaises(RuntimeError): obj.start(Mock(operation_id='op-test'),'256K')
        controller.start.assert_called_once_with('op-test',profile='256K')

if __name__=='__main__': unittest.main()
