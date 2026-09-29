"""Regression for the real failed image handoff; no DGX calls."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from tests import test_dgx_creative_jobs as creative_fixtures
from tests import test_model_residency as residency_fixtures

class OldHandoffRecovery(unittest.TestCase):
    setUp=creative_fixtures.JobsTests.setUp
    def failed(self):
        d={'job_id':'a'*32,'state':'FAILED','memory_released':False,'cleanup_required':False,'resource_reused':True,'resident_id':'r'*32,'error':'local model state is not safe for mutation'}
        self.jobs.save(d)
        self.app.residency=SimpleNamespace(row={'state':'NONE'})
        self.adapter=SimpleNamespace(observe=Mock(return_value=SimpleNamespace(state='STOPPED')))
        self.app.operations=SimpleNamespace(adapters={'comfy':self.adapter})
        self.jobs.switch=Mock(side_effect=AssertionError('must not mutate models'))
        self.jobs.request=Mock(side_effect=AssertionError('must not submit inference'))
        return d
    def test_settles_only_old_unsubmitted_handoff_without_model_action(self):
        d=self.failed();r=self.jobs.reconcile(d['job_id'])
        self.assertEqual(r['state'],'FAILED');self.assertTrue(r['memory_released']);self.assertTrue(r['resource_not_acquired'])
        self.assertEqual(r['error'],d['error']);self.jobs.switch.assert_not_called();self.jobs.request.assert_not_called()
    def test_rejects_persisted_operation_despite_missing_receipt(self):
        d=self.failed();self.app.store.list_operations=lambda **kw:[SimpleNamespace(idempotency_key=d['job_id']+'-start')]
        with self.assertRaises(BlockingIOError):self.jobs.reconcile(d['job_id'])
        self.assertFalse(self.jobs.get(d['job_id'])['memory_released'])
    def test_refuses_live_or_unknown_model(self):
        d=self.failed()
        for state in ('HEALTHY','UNKNOWN'):
            self.adapter.observe.return_value=SimpleNamespace(state=state)
            with self.assertRaises(BlockingIOError):self.jobs.reconcile(d['job_id'])
        self.assertFalse(self.jobs.get(d['job_id'])['memory_released'])
    def test_submitted_or_owned_jobs_do_not_enter_unsubmitted_path(self):
        d=self.failed()
        for field,value in [('prompt_id','p'),('start_operation_id','s'),('cleanup_required',True),('artifacts',[{'path':'missing'}])]:
            row={**d,field:value};self.jobs.save(row)
            with self.assertRaises(ValueError):self.jobs.reconcile(d['job_id'])
            self.assertFalse(self.jobs.get(d['job_id'])['memory_released'])
    def test_other_active_task_or_operation_prevents_settlement(self):
        d=self.failed();self.app.video_jobs=SimpleNamespace(active='b'*32)
        with self.assertRaises(BlockingIOError):self.jobs.reconcile(d['job_id'])
        self.app.video_jobs.active=None;self.app.store.active_operation=lambda:object()
        with self.assertRaises(BlockingIOError):self.jobs.reconcile(d['job_id'])

class FailedClaimRollback(unittest.TestCase):
    setUp=residency_fixtures.ResidencyTests.setUp
    row=residency_fixtures.ResidencyTests.row
    start=residency_fixtures.ResidencyTests.start
    finish=residency_fixtures.ResidencyTests.finish
    def test_rejected_preflight_returns_previous_owner_and_settles_incoming_task(self):
        a=self.row('a');self.start(a);self.finish(a)
        b=self.row('b');self.app.video_jobs.active=b['job_id'];self.r.before_start('video_jobs',b)
        b.update(state='FAILED',cleanup_required=False)
        self.r.job_finished('video_jobs',b)
        self.assertEqual(self.r.row['owner_job_id'],a['job_id']);self.assertEqual(self.r.row['state'],'IDLE')
        self.assertTrue(b['resource_settled']);self.assertTrue(b['resource_not_acquired']);self.assertFalse(b['cleanup_required'])
        self.assertEqual(self.h3.state,'HEALTHY')
    def test_lost_start_receipt_never_rolls_back_as_unsubmitted(self):
        a=self.row('a');self.start(a);self.finish(a)
        b=self.row('b');self.app.video_jobs.active=b['job_id'];self.r.before_start('video_jobs',b)
        self.store.create_operation('e'*32,'minimax-h3','minimax-h3',b['job_id']+'-start')
        self.r.job_finished('video_jobs',b)
        self.assertEqual(self.r.row['state'],'RECOVERY_REQUIRED');self.assertTrue(b['cleanup_required']);self.assertFalse(b['memory_released'])

if __name__=='__main__':unittest.main()
