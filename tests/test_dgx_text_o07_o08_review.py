"""P6 fault boundaries: real local lease/SQLite, injected IO faults and fake model."""
import errno
import unittest
from unittest.mock import patch

from tests import test_dgx_gateway_text_jobs as fixtures
from DGXGateway.dgx_gateway.authority import GatewayLease


class TextFaultReviewTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.TextJobTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_reconcile_already_retired_runtime_requires_proof_not_another_stop(self):
        f = self.f
        with patch.object(GatewayLease, 'release', side_effect=OSError(errno.ENOSPC, 'injected')):
            row = f.run_job()
        self.assertEqual(f.adapter.state, 'STOPPED')
        with patch.object(f.jobs, '_child', side_effect=AssertionError('must not stop absent runtime')):
            f.jobs.reconcile(row['job_id']); f.jobs._reconcile_run(row['job_id'])
        final = f.jobs.persistence.get(row['job_id'])
        self.assertTrue(final['memory_released'])
        self.assertIsNotNone(final['stop_proof'])
        self.assertEqual(final['lease_generation'], row['lease_generation'])

    def test_released_parent_settles_only_its_failed_children(self):
        f = self.f
        row = f.run_job()
        child = row['operation_ids'][0]
        f.store.set_phase(child, 'RECOVERY_REQUIRED')
        f.jobs._settle_released_children(row['job_id'])
        self.assertEqual(f.store.get_operation(child).phase, 'DELEGATED_COMPLETED')
        self.assertFalse(f.jobs._other_busy())

    def test_o07_raw_archive_enospc_retains_unknown_output_and_resource_obligation(self):
        f = self.f
        job = f.submit()
        with patch.object(f.jobs.persistence, 'archive', side_effect=OSError(errno.ENOSPC, 'injected archive disk full')):
            f.jobs.run(job['job_id'])
        row = f.jobs.persistence.get(job['job_id'])
        self.assertEqual(row['state'], 'RECOVERY_REQUIRED')
        self.assertEqual(row['resource_status'], 'recovery_required')
        self.assertTrue(row['cleanup_required'])
        self.assertFalse(row['memory_released'])
        self.assertIsNone(row['output_text'])  # A failed write is not a durable result.
        self.assertNotEqual(row['output_status'], 'valid_draft')
        self.assertTrue(f.app.config.lock_root.exists())
        self.assertEqual(f.transport.calls, 1)
        self.assertEqual(f.adapter.calls, ['start'])
        attempt = row['attempt_id']
        f.jobs.reconcile(job['job_id']); f.jobs._reconcile_run(job['job_id'])
        final = f.jobs.get(job['job_id'])
        self.assertEqual(final['state'], 'FAILED')
        self.assertEqual(final['attempt_id'], attempt)
        self.assertTrue(final['memory_released'])
        self.assertEqual(f.transport.calls, 1)

    def test_o07_release_receipt_write_failure_preserves_output_and_original_lease(self):
        f = self.f
        with patch.object(GatewayLease, 'release', side_effect=OSError(errno.ENOSPC, 'injected release receipt write full')):
            row = f.run_job()
        self.assertEqual(row['resource_status'], 'releasing')
        self.assertTrue(row['cleanup_required'])
        self.assertFalse(row['memory_released'])
        self.assertEqual(row['output_status'], 'valid_draft')
        self.assertEqual(row['output_text'], f.transport.output)
        saved = f.jobs.persistence.get(row['job_id'])
        self.assertIsNotNone(saved['stop_proof'])
        self.assertIsNone(saved['release_receipt'])
        lease = f.jobs._lease(row['job_id'])
        lease.lease_generation = saved['lease_generation']
        self.assertEqual(lease.guard()['lease_generation'], saved['lease_generation'])
        f.jobs.reconcile(row['job_id']); f.jobs._reconcile_run(row['job_id'])
        final = f.jobs.persistence.get(row['job_id'])
        self.assertEqual(final['state'], 'SUCCEEDED')
        self.assertTrue(final['memory_released'])
        self.assertEqual(final['lease_generation'], saved['lease_generation'])
        self.assertEqual(final['attempt_id'], saved['attempt_id'])
        self.assertEqual(final['output_sha256'], saved['output_sha256'])
        self.assertEqual(f.transport.calls, 1)

    def test_o08_cancel_during_loading_is_persistent_and_zero_inference(self):
        f = self.f
        job = f.submit()
        original = f.adapter.start
        def loading(context, profile=None):
            self.assertEqual(f.jobs.get(job['job_id'])['state'], 'LOADING')
            f.jobs.cancel(job['job_id'])
            original(context, profile)
        with patch.object(f.adapter, 'start', side_effect=loading):
            f.jobs.run(job['job_id'])
        row = f.jobs.get(job['job_id'])
        self.assertEqual(row['state'], 'CANCELED')
        self.assertTrue(row['cancel_requested'])
        self.assertTrue(row['memory_released'])
        self.assertIsNone(row['attempt_id'])
        self.assertEqual(f.transport.calls, 0)
        self.assertEqual(f.adapter.calls, ['start', 'stop'])

    def test_o08_cancel_during_releasing_preserves_output_and_receipt(self):
        f = self.f
        job = f.submit()
        original = GatewayLease.release
        def releasing(lease):
            row = f.jobs.persistence.get(job['job_id'])
            self.assertEqual(row['state'], 'RELEASING')
            self.assertEqual(row['output_status'], 'valid_draft')
            self.assertIsNotNone(row['stop_proof'])
            f.jobs.cancel(job['job_id'])
            return original(lease)
        with patch.object(GatewayLease, 'release', releasing):
            f.jobs.run(job['job_id'])
        row = f.jobs.persistence.get(job['job_id'])
        self.assertEqual(row['state'], 'CANCELED')
        self.assertTrue(row['cancel_requested'])
        self.assertTrue(row['memory_released'])
        self.assertFalse(row['cleanup_required'])
        self.assertEqual(row['output_text'], f.transport.output)
        self.assertIsNotNone(row['release_receipt'])
        self.assertEqual(f.transport.calls, 1)
        self.assertEqual(f.adapter.calls, ['start', 'stop'])
