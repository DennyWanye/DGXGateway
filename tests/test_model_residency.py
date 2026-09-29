"""Real persistence/controller, simulated adapters: residency ownership oracle."""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from DGXGateway.dgx_gateway.server import GatewayApplication
from DGXGateway.dgx_gateway.store import OperationStore
from DGXGateway.dgx_gateway.operations import OperationController
from DGXGateway.dgx_gateway.video_jobs import VideoJobs, assert_video_stop_ownership
from DGXGateway.dgx_gateway.creative_jobs import CreativeJobs
from DGXGateway.dgx_gateway.residency import ModelResidency
from DGXGateway.dgx_gateway.domain import ObservedModel
from tests.test_dgx_gateway_operations import FakeAdapter, FakeLease

PROFILE='heretic-ref2va-v1'
class H3(FakeAdapter):
    profiles=(PROFILE,'10eros-fl2va-v1')
    default_profile=PROFILE
    def observe(self): return ObservedModel(self.model_id,self.state, self.started_profiles[-1] if self.started_profiles else PROFILE)

class ResidencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);self.events=[]
        self.store=OperationStore(root/'gateway.sqlite3');self.addCleanup(self.store.close)
        self.h3=H3('minimax-h3',self.events);self.comfy=FakeAdapter('comfyui',self.events)
        ops=OperationController(store=self.store,adapters={'minimax-h3':self.h3,'comfyui':self.comfy},lease_factory=lambda op:FakeLease(self.events,op))
        self.app=GatewayApplication(config=SimpleNamespace(state_dir=root,repo_root=root,nodes=[SimpleNamespace(address='127.0.0.1')],music3_port=8188,bind='127.0.0.1',port=18082),store=self.store,operations=ops,snapshot=None,static_dir=root,admission=SimpleNamespace(assert_allowed=lambda:None))
        self.app.creative_jobs=CreativeJobs(self.app); self.app.video_jobs=VideoJobs(self.app)
        self.r=self.app.residency=ModelResidency(self.app)
        self.app.creative_jobs.request=lambda *a:{}
    def row(self, ch, profile=PROFILE):
        row={'job_id':ch*32,'state':'QUEUED','profile':profile,'non_preemptive':True,'shots':[],'memory_released':False}
        self.app.video_jobs.save(row)
        return row
    def start(self,row):
        self.app.video_jobs.active=row['job_id']
        self.r.before_start('video_jobs',row)
        self.app.video_jobs.switch(row,'minimax-h3','start')
        self.r.claimed('video_jobs',row)
    def finish(self,row):
        self.assertTrue(self.r.retain('video_jobs',row));row['state']='SUCCEEDED'
        self.app.video_jobs.save(row);self.app.video_jobs.active=None
    def test_two_completed_jobs_one_cold_start_and_manual_release(self):
        a=self.row('a'); self.start(a); self.finish(a)
        self.assertFalse(a['memory_released']); self.assertTrue(a['resource_settled'])
        b=self.row('b'); self.start(b); self.finish(b)
        self.assertEqual(self.events.count('minimax-h3.start'),1)
        self.assertTrue(b['resource_reused'])
        self.assertEqual(assert_video_stop_ownership(self.store,b,[self.h3.observe(),self.comfy.observe()]),'OWNED_H3')
        self.r.release(b['job_id'])
        self.assertEqual(self.h3.state,'STOPPED')
        self.assertTrue(self.app.video_jobs.get(a['job_id'])['memory_released'])
        self.assertEqual(self.r.view()['state'],'NONE')
    def test_old_cancel_and_wrong_release_cannot_stop_new_task(self):
        a=self.row('a');self.start(a);self.finish(a)
        b=self.row('b');self.start(b)
        self.app.video_jobs.cancel(a['job_id'])
        with self.assertRaises(BlockingIOError):self.r.release(a['job_id'])
        with self.assertRaises(BlockingIOError):self.r.release(b['job_id'])
        self.assertEqual(self.h3.state,'HEALTHY')
    def test_foreign_manual_h3_cannot_be_adopted(self):
        self.h3.state='HEALTHY';a=self.row('a')
        with self.assertRaises(BlockingIOError):self.start(a)
        self.assertEqual(self.r.view()['state'],'NONE')
    def test_later_lifecycle_breaks_handoff_and_release(self):
        a=self.row('a');self.start(a);self.finish(a)
        op=self.store.create_operation('f'*32,'minimax-h3','minimax-h3','foreign-key',PROFILE)
        self.store.freeze_outcome(op.operation_id,'COMMITTED');self.store.set_phase(op.operation_id,'RELEASED')
        with self.assertRaises(BlockingIOError):self.r.release(a['job_id'])
        self.assertEqual(self.h3.state,'HEALTHY')
    def test_profile_change_stops_owned_then_cold_starts(self):
        a=self.row('a');self.start(a);self.finish(a)
        b=self.row('b','10eros-fl2va-v1');self.start(b)
        self.assertEqual(self.events.count('minimax-h3.start'),2)
        self.assertTrue(self.app.video_jobs.get(a['job_id'])['memory_released'])
    def test_restart_requires_explicit_release(self):
        a=self.row('a');self.start(a);self.finish(a)
        self.app.residency=self.r=ModelResidency(self.app)
        self.assertEqual(self.r.view()['state'],'RECOVERY_REQUIRED')
        with self.assertRaises(BlockingIOError):self.r.before_start('video_jobs',self.row('b'))
        self.r.release(a['job_id']);self.assertEqual(self.h3.state,'STOPPED')
    def test_idle_expiry_is_real_controller_stop(self):
        self.r.idle_seconds=.01
        a=self.row('a');self.start(a);self.finish(a)
        self.r.start();self.addCleanup(self.r.close)
        deadline=time.monotonic()+5
        while self.r.view()['state']!='NONE' and time.monotonic()<deadline:time.sleep(.05)
        self.assertEqual(self.r.view()['state'],'NONE');self.assertEqual(self.h3.state,'STOPPED')
    def test_unknown_task_does_not_become_idle_or_stop(self):
        a=self.row('a');self.start(a);self.finish(a)
        b=self.row('b');self.start(b)
        b.update(state='SUBMISSION_UNKNOWN',cleanup_required=True)
        self.r.job_finished('video_jobs',b)
        self.assertEqual(self.r.view()['state'],'RECOVERY_REQUIRED')
        with self.assertRaises(BlockingIOError): self.r.release(b['job_id'])
        self.assertEqual(self.h3.state,'HEALTHY')
    def test_only_typed_images_and_exact_loaders_are_reused(self):
        raw={'workflow':{'1':{'class_type':'UNETLoader','inputs':{'unet_name':'a'}}}}
        self.assertIsNone(self.r.key('creative_jobs',raw))
        raw['image_request']={'tool':'edit_image'}
        a=self.r.key('creative_jobs',raw);raw['workflow']['1']['inputs']['unet_name']='b'
        self.assertNotEqual(a,self.r.key('creative_jobs',raw))
