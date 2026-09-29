"""Independent stale-administrator recovery boundaries; no SSH or model IO."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_dgx_gateway_text_jobs as fixture
from DGXGateway.dgx_gateway.text_admin_recovery import TextAdminRecovery, RecoveryObservation
from DGXGateway.dgx_gateway.text_store import TextFenced
from DGXGateway.dgx_gateway.authority import LeaseError


class AdminReviewTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.TextJobTests(); self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.jobs = self.case.jobs
        config = self.case.app.config
        config.authority_credential = self.case.root/'review-authority-key'
        config.authority_credential.write_text('isolated-review-'+('x'*40)); config.authority_credential.chmod(0o600)
        self.case.app.admission.assert_allowed = lambda: {'allowed': True, 'authority_id': 'review-local', 'epoch': 1}
        def observe(guard):
            guard()
            return {'state': 'ALL_STOPPED' if self.case.adapter.state == 'STOPPED' else 'ACTIVE_EXCLUSIVE',
                    'runtime_identity': {'runtime_id': 'qa-only-runtime'},
                    'models': [{'model_id': fixture.MODEL, 'state': self.case.adapter.state}],
                    'remote_attestation_absent': True}
        self.admin = TextAdminRecovery(self.jobs, observer=SimpleNamespace(inspect=observe))

    def expire(self):
        path = self.case.app.config.lock_root/'owner.json'
        data = json.loads(path.read_text()); data['heartbeat_at'] = '2000-01-01T00:00:00Z'
        path.write_text(json.dumps(data))

    def test_qualification_helper_roles_match_existing_service_manifest(self):
        from DGXGateway.dgx_gateway.text_qualification import REMOTE, REMOTE_BY_ROLE
        manifest = json.loads((fixture.REPO/'manifests/deepseek-v4-flash-dspark.lock.json').read_text())
        for role in ('head', 'worker'):
            expected = {item['install_path']: item['source_path'] for item in manifest['service_assets']['roles'][role]
                        if item.get('source_path') in REMOTE.values()}
            self.assertEqual(REMOTE_BY_ROLE[role], expected)

    def test_partial_health_with_open_gateway_is_not_a_closed_admission_proof(self):
        self.case.adapter.state = 'PARTIAL'
        identity = {'nodes': {'head': {}, 'worker': {}}, 'gateway_container_id': 'c'*64}
        self.case.adapter._text_runtime_identity = lambda **kw: identity
        self.case.app.config.nodes = [SimpleNamespace(role='head', hostname='dgx-head', user='operator'),
                                      SimpleNamespace(role='worker', hostname='dgx-worker', user='operator')]
        def ssh(node, *args, **kwargs):
            return SimpleNamespace(returncode=0, stdout=node.hostname+'\noperator\n')
        with patch('DGXGateway.dgx_gateway.text_admin_recovery.FixedSSH.run', side_effect=ssh):
            observation = RecoveryObservation(self.case.app).inspect(lambda: None)
        self.assertNotEqual(observation.get('admission_fenced'), True)
        self.assertFalse(observation.get('admission_observed_closed', False))

    def test_crash_during_own_loading_child_can_be_recovered_without_gateway_restart(self):
        original = self.case.submit()
        row = self.jobs.persistence.claim(original['job_id'], 'crashed-process', 60)
        lease = self.jobs._lease(row['job_id']); lease.acquire()
        row = self.jobs.persistence.update(row['job_id'], row['owner_id'], row['lease_epoch'],
            {'lease_generation': lease.lease_generation, 'resource_status': 'held',
             'runtime_identity': {'runtime_id': 'qa-only-runtime'}}, 'local-crash-fixture')
        child = self.jobs.persistence.create_child(row['job_id'], row['owner_id'], row['lease_epoch'], 'd'*32, 'load', fixture.MODEL)
        self.case.adapter.state = 'HEALTHY'
        self.expire()
        result = self.admin.run(row['job_id'], row['lease_epoch'], 'loading-crash-admin')
        self.assertTrue(result['job']['memory_released'])
        self.assertEqual(result['recovery']['phase'], 'DONE')
        self.assertEqual(self.case.transport.calls, 0)
        self.assertEqual(self.case.adapter.calls, ['stop'])
        self.assertEqual(self.case.store.get_operation(child.operation_id).phase, 'RECOVERY_REQUIRED')

    def test_acquired_before_db_bind_then_expired_has_an_explicit_recovery_path(self):
        self.case.transport.error = fixture.SubmissionUnknown('lost original inference receipt')
        row = self.case.run_job(); self.expire()
        original_guard = self.admin._guard; injected = []
        def fail_binding(record):
            lease = self.admin._lease(record['new_resource_operation_id'])
            if record['phase'] == 'NEW_ACQUIRE_INTENT' and lease.token_path.exists() and not injected:
                injected.append(True); raise OSError('isolated crash after acquire before DB binding')
            return original_guard(record)
        with patch.object(self.admin, '_guard', side_effect=fail_binding), self.assertRaises(OSError):
            self.admin.run(row['job_id'], row['lease_epoch'], 'acquire-crash-admin')
        self.assertEqual(injected, [True])
        self.expire()
        try:
            result = self.admin.run(row['job_id'], row['lease_epoch'], 'acquire-crash-admin')
        except (TextFenced, LeaseError):
            # A new explicit key is also acceptable when required to retire a
            # newly discovered stale recovery generation. No automatic replay.
            current = self.jobs.get(row['job_id'])
            result = self.admin.run(row['job_id'], current['lease_epoch'], 'second-explicit-admin')
        self.assertTrue(result['job']['memory_released'])
        self.assertEqual(result['recovery']['phase'], 'DONE')
        self.assertEqual(self.case.transport.calls, 1)
        self.assertEqual(self.case.adapter.calls, ['start', 'stop'])


if __name__ == '__main__': unittest.main()
