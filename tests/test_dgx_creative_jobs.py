import json,tempfile,threading,unittest,urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from DGXGateway.dgx_gateway.creative_jobs import CreativeJobs

class JobsTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.app=SimpleNamespace(config=SimpleNamespace(state_dir=Path(self.tmp.name),nodes=[SimpleNamespace(address='127.0.0.1')],music3_port=8188),_submit_lock=threading.Lock(),store=SimpleNamespace(active_operation=lambda:None,list_operations=lambda **kw:[]))
  self.jobs=CreativeJobs(self.app);self.app.creative_jobs=self.jobs
 def job(self):
  d={'job_id':'a'*32,'idempotency_key':'test-key','state':'RUNNING','workflow':{'1':{'class_type':'SaveImage','inputs':{}}},'memory_released':False}
  self.jobs.active=d['job_id'];self.jobs.save(d);return d
 def test_restart_retains_unresolved_owner(self):
  d=self.job();j=CreativeJobs(self.app)
  self.assertEqual(j.active,d['job_id']);self.assertTrue(j.get(d['job_id'])['cleanup_required'])
  with self.assertRaises(BlockingIOError):j.submit(d['workflow'],'second-key')
 def test_duplicate_same_key_no_second_worker(self):
  d=self.job()
  with patch('threading.Thread') as thread:
   self.assertEqual(self.jobs.submit(d['workflow'],'test-key')['job_id'],d['job_id']);thread.assert_not_called()
   with self.assertRaises(ValueError):self.jobs.submit({'2':{'class_type':'SaveImage','inputs':{}}},'test-key')
 def test_ambiguous_post_retains_owner_no_retry(self):
  d=self.job();self.jobs.switch=Mock();self.jobs.request=Mock(side_effect=[{},TimeoutError('lost response')])
  self.jobs.run(d)
  self.assertTrue(self.jobs.get(d['job_id'])['cleanup_required']);self.assertEqual(self.jobs.active,d['job_id']);self.assertEqual(self.jobs.request.call_count,2)
 def test_explicit_rejection_cleans_up(self):
  d=self.job();self.jobs.switch=Mock();self.jobs.request=Mock(side_effect=[{},urllib.error.HTTPError('url',400,'invalid',None,None),{}])
  self.jobs.run(d)
  self.assertTrue(self.jobs.get(d['job_id'])['memory_released']);self.assertIsNone(self.jobs.active);self.assertEqual(self.jobs.switch.call_count,2)
 def test_external_job_never_stopped(self):
  d=self.job();self.jobs.switch=Mock();self.jobs.request=Mock(return_value={'queue_running':[1]})
  self.jobs.run(d)
  self.assertEqual(self.jobs.switch.call_count,1);self.assertEqual(self.jobs.request.call_count,1);self.assertFalse(d['memory_released'])
 def test_stop_failure_not_success(self):
  d=self.job();self.jobs.switch=Mock(side_effect=[None,RuntimeError('stop failed'),RuntimeError('still failed')]);self.jobs.collect=Mock()
  self.jobs.request=Mock(side_effect=[{}, {'prompt_id':'p'}, {'p':{'status':{'status_str':'success'},'outputs':{'x':{'images':[{'filename':'x.png'}]}}}},{}])
  self.jobs.run(d)
  self.assertEqual(d['state'],'FAILED');self.assertFalse(d['memory_released']);self.assertTrue(d['cleanup_required']);self.assertEqual(self.jobs.active,d['job_id'])

 def test_cancel_intent_survives_stale_worker_save_and_restart(self):
  d=self.job()
  self.assertTrue(self.jobs.cancel(d['job_id'])['cancel_requested'])
  self.jobs.save(d)
  self.assertTrue(self.jobs.get(d['job_id'])['cancel_requested'])
  restarted=CreativeJobs(self.app)
  self.assertTrue(restarted.get(d['job_id'])['cancel_requested'])
  self.assertEqual(restarted.active,d['job_id'])
 def test_cancel_before_load_does_not_start_model(self):
  d=self.job();self.jobs.cancel(d['job_id']);self.jobs.switch=Mock()
  self.jobs.run(d)
  self.jobs.switch.assert_not_called()
  self.assertEqual(d['state'],'CANCELED');self.assertIsNone(self.jobs.active)
 def test_cancel_running_targets_original_prompt_then_releases(self):
  d=self.job();self.jobs.switch=Mock()
  def request(path,body=None):
   if path=='/queue':return {}
   if path=='/prompt':
    self.jobs.cancel(d['job_id']);return {'prompt_id':'owned-prompt'}
   if path=='/dgx/cancel':
    self.assertEqual(body,{'prompt_id':'owned-prompt','client_id':d['job_id']})
    return {'state':'interrupt_requested'}
   return {'owned-prompt':{'status':{'status_str':'error'},'outputs':{}}}
  self.jobs.request=Mock(side_effect=request);self.jobs.run(d)
  self.assertEqual(d['state'],'CANCELED');self.assertTrue(d['memory_released'])
  self.assertEqual(self.jobs.switch.call_count,2)
 def test_cancel_release_failure_keeps_recovery_owner(self):
  d=self.job()
  def switch(row,target,suffix):
   if target:self.jobs.cancel(row['job_id'])
   else:raise RuntimeError('release failed')
  self.jobs.switch=switch;self.jobs.request=Mock(return_value={});self.jobs.run(d)
  self.assertTrue(d['cleanup_required']);self.assertFalse(d['memory_released'])
  self.assertEqual(self.jobs.active,d['job_id'])
 def test_cancel_finished_job_is_noop(self):
  d=self.job();d['state']='SUCCEEDED';self.jobs.save(d)
  self.assertFalse(self.jobs.cancel(d['job_id'])['cancel_requested'])

class StoppedReconcileTests(JobsTests):
 def stopped_setup(self):
  d=self.job();d.update(state='INTERRUPTED',cleanup_required=True,comfy_status={'status_str':'success'})
  artifact=Path(self.tmp.name)/'saved.png';artifact.write_bytes(b'actual saved artifact')
  d['artifacts']=[{'path':str(artifact)}]
  adapter=SimpleNamespace(observe=lambda:SimpleNamespace(state='STOPPED'))
  self.app.operations=SimpleNamespace(adapters={'comfy':adapter})
  self.jobs.request=Mock(side_effect=AssertionError('must not request stopped HTTP'))
  self.jobs.switch=Mock()
  return d
 def test_stopped_reconcile_preserves_artifacts_and_proves_release(self):
  d=self.stopped_setup();self.jobs._reconcile(d)
  self.assertEqual(d['state'],'SUCCEEDED');self.assertTrue(d['memory_released']);self.assertIsNone(self.jobs.active)
  self.jobs.request.assert_not_called();self.jobs.switch.assert_called_once_with(d,None,'reconcile-prove-stopped')
 def test_stopped_reconcile_refuses_lifecycle_recovery(self):
  d=self.stopped_setup();self.app.store.list_operations=lambda **kw:[SimpleNamespace(phase='RECOVERY_REQUIRED')]
  self.jobs._reconcile(d)
  self.assertEqual(d['state'],'INTERRUPTED');self.assertTrue(d['cleanup_required']);self.jobs.switch.assert_not_called();self.jobs.request.assert_not_called()
 def test_stopped_reconcile_proof_failure_retains_owner(self):
  d=self.stopped_setup();self.jobs.switch.side_effect=RuntimeError('proof failed')
  self.jobs._reconcile(d)
  self.assertEqual(d['state'],'INTERRUPTED');self.assertFalse(d['memory_released']);self.assertEqual(self.jobs.active,d['job_id'])
 def test_stopped_reconcile_missing_artifact_not_success(self):
  d=self.stopped_setup();Path(d['artifacts'][0]['path']).unlink();self.jobs._reconcile(d)
  self.assertEqual(d['state'],'FAILED');self.assertTrue(d['memory_released']);self.assertFalse(d['cleanup_required'])

if __name__=='__main__':unittest.main()
