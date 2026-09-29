#!/usr/bin/env python3
"""Mia Abliterated adapter over the existing receipt/lease transport baseline."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import uuid

_spec = importlib.util.spec_from_file_location(
    'mia_receipt_transport', Path(__file__).with_name('control-deepseek-v4-flash-vision-exp.py'))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
SubprocessRunner, LockAuthority, Journal = _base.SubprocessRunner, _base.LockAuthority, _base.Journal
PROJECT = 'mia-dsv41-ablit'


class ClusterController(_base.ClusterController):
    def __init__(self, *, gateway_secret, **kwargs):
        super().__init__(**kwargs)
        self.gateway_secret = gateway_secret

    def _read_secret(self):
        from DGXGateway.dgx_gateway.connection_view import _read_shared_api_key
        return _read_shared_api_key(self.gateway_secret)

    def _mutate(self, role, verb, transaction, *, gateway_token=None, profile=None):
        guard = self._guard()
        self._verify_target(role)
        guard = self._guard()
        receipt = {
            'schema': 'mia-dsv41-receipt-v1', 'project': PROJECT, 'role': role, 'verb': verb,
            'operation_id': transaction, 'lease_generation': str(guard['lease_generation']),
            'nonce': uuid.uuid4().hex,
            'not_after': (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        }
        if gateway_token is not None:
            receipt['gateway_token'] = gateway_token
        if profile is not None:
            receipt['profile'] = profile
        self.journal.append({
            'event': 'RECEIPT_ISSUED', 'operation_id': transaction, 'role': role, 'verb': verb,
            'nonce': receipt['nonce'],
        })
        timeout = 3600 if verb == 'start' and role == 'head' else 300 if verb == 'start' else 240
        result = self.runner.run_supervised(
            self._ssh(role, ['sudo', '-n', 'python3', '/usr/local/libexec/mia-dsv41-runtime-action.py',
                             '--role', role, '--verb', verb]),
            input_text=json.dumps(receipt) + '\n', timeout=timeout, lease_check=self._guard)
        self._guard()
        value = _base._json_result(result, verb + ' ' + role)
        expected = {
            'terminal': 'succeeded', 'operation_id': transaction, 'role': role, 'verb': verb,
            'lease_generation': str(guard['lease_generation']),
        }
        if any(value.get(k) != v for k, v in expected.items()):
            raise RuntimeError('Mia terminal receipt mismatch')
        self.journal.append({'event': 'RECEIPT_TERMINAL', **expected, 'sha256': value.get('sha256')})
        return value

    def start(self, transaction_id, *, profile='256K', _under_active=True):
        if profile not in ('256K', '512K'):
            raise ValueError('invalid Mia profile')
        if not _under_active:
            raise ValueError('Mia must run under the Gateway shared lease')
        self._guard()
        try:
            worker = self._mutate('worker', 'start', transaction_id, profile=profile)
            head = self._mutate('head', 'start', transaction_id, profile=profile)
            return {'worker': worker, 'head': head}
        except BaseException:
            for role in ('head', 'worker'):
                try:
                    self._mutate(role, 'stop', transaction_id)
                except BaseException:
                    pass
            raise

    def stop(self, transaction_id, *, _under_active=True):
        errors = []
        for role in ('head', 'worker'):
            try:
                self._mutate(role, 'stop', transaction_id)
            except Exception as error:
                errors.append(str(error))
        if errors:
            raise RuntimeError('Mia paired stop incomplete: ' + '; '.join(errors))

    def production_activate_gateway(self, transaction_id, *, _under_active=True):
        return self._mutate('head', 'gateway-production', transaction_id, gateway_token=self._read_secret())

    def staging_activate_gateway(self, transaction_id, *, _under_active=True):
        return self._mutate('head', 'gateway-staging', transaction_id, gateway_token=self._read_secret())

    def production_deactivate_gateway(self, transaction_id, *, _under_active=True):
        return self._mutate('head', 'gateway-stop', transaction_id)

    def drain_admission(self, transaction_id):
        return self._mutate('head', 'drain', transaction_id)

    def fail_close_head(self, transaction_id, *, _under_active=True):
        return self._mutate('head', 'fail-close-head', transaction_id, gateway_token=self._read_secret())
