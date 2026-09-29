import base64,importlib.util,io,json,tempfile,threading,time,unittest,wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from PIL import Image
from DGXGateway.dgx_gateway.avatar_jobs import AvatarJobs
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('avatar_worker_test',ROOT/'scripts/longcat-avatar15/gateway/worker.py');worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)

def fixture():
    image=io.BytesIO();Image.new('RGB',(128,128),'red').save(image,format='PNG')
    audio=io.BytesIO()
    with wave.open(audio,'wb') as stream:stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000);stream.writeframes(bytes(128000))
    return {'image_base64':base64.b64encode(image.getvalue()).decode(),'audio_base64':base64.b64encode(audio.getvalue()).decode(),'prompt':'A person speaks.','num_segments':1,'idempotency_key':'avatar-fixture-key'}

class Inputs(unittest.TestCase):
    def test_valid_and_short_audio_rejected_for_long_video(self):
        row=fixture();image,audio,digest=AvatarJobs.normalize(row);self.assertEqual(len(digest),64)
        row['num_segments']=19
        with self.assertRaisesRegex(ValueError,'too short'):AvatarJobs.normalize(row)
    def test_bad_key_segment_and_image_rejected(self):
        for key,value in [('num_segments',True),('num_segments',2),('idempotency_key','bad'),('image_base64','not-base64')]:
            row=fixture();row[key]=value
            with self.assertRaises((ValueError,TypeError)):AvatarJobs.normalize(row)
    def test_wrong_audio_format(self):
        row=fixture();data=bytearray(base64.b64decode(row['audio_base64']));data[24:28]=(24000).to_bytes(4,'little');row['audio_base64']=base64.b64encode(data).decode()
        with self.assertRaises(ValueError):AvatarJobs.normalize(row)

class WorkerTests(unittest.TestCase):
    def test_exclusive_job_and_bound_heartbeat(self):
        with tempfile.TemporaryDirectory() as root,patch.object(worker,'JOBS',Path(root)),patch.object(threading.Thread,'start'):
            jobs=worker.Jobs();body=fixture();body.pop('idempotency_key');body.update(job_id='a'*32,lease_generation='generation-one',not_after=time.time()+100)
            one=jobs.submit(body);self.assertEqual(one['state'],'QUEUED')
            self.assertEqual(jobs.submit(dict(body,not_after=time.time()+110))['job_id'],'a'*32)
            with self.assertRaises(BlockingIOError):jobs.submit(dict(body,job_id='b'*32))
            with self.assertRaises(ValueError):jobs.heartbeat({'job_id':'a'*32,'lease_generation':'other','not_after':time.time()+100})
            self.assertTrue(jobs.cancel('a'*32)['cancel_requested'])
    def test_expired_job_terminates_guard_and_retains_cleanup_result(self):
        with tempfile.TemporaryDirectory() as root,patch.object(worker,'JOBS',Path(root)),patch.object(threading.Thread,'start'):
            jobs=worker.Jobs();body=fixture();body.pop('idempotency_key');body.update(job_id='a'*32,lease_generation='generation-one',not_after=time.time()+100)
            jobs.submit(body);row=jobs.get('a'*32);row['lease_deadline']=time.time()-1;worker.save(row)
            report=Path(root)/('a'*32)/'run/logs';report.mkdir(parents=True)
            (report/'run-report.json').write_text(json.dumps({'cleanup_complete':True,'inputs_unchanged':True,'status':'FAIL','elapsed_seconds':1}))
            process=Mock();process.poll.side_effect=[None,0]
            with patch.object(worker.subprocess,'Popen',return_value=process),patch.object(worker.time,'sleep'):jobs.run('a'*32)
            process.terminate.assert_called_once();self.assertIsNone(jobs.active)
            self.assertTrue(jobs.get('a'*32)['cleanup_complete'])

class ParentLeaseTests(unittest.TestCase):
    def test_collection_and_stop_happen_before_shared_lease_release(self):
        with tempfile.TemporaryDirectory() as root:
            cfg=SimpleNamespace(state_dir=Path(root),lock_cli=Path('/lock'),lock_root=Path('/lockroot'),token_dir=Path('/tokens'))
            app=SimpleNamespace(config=cfg,_submit_lock=threading.RLock(),operations=SimpleNamespace(adapters={}))
            manager=AvatarJobs(app);job='c'*32;folder=manager.root/job;folder.mkdir();(folder/'image.png').write_bytes(b'img');(folder/'audio.wav').write_bytes(b'wav')
            row={'job_id':job,'state':'QUEUED','created_at':time.time(),'num_segments':1,'prompt':'Test','memory_released':False,'cleanup_required':False};manager.save(row);manager.active=job
            order=[];lease=Mock(lease_generation='generation-one',token_path=Path('/tokens/test'))
            lease.acquire.side_effect=lambda:order.append('acquire') or {'lease_generation':'generation-one'}
            lease.release.side_effect=lambda:order.append('release') or {'state':'UNLOCKED'}
            adapter=Mock()
            def request(path,body=None,**kw):
                if path=='/jobs':order.append('submit');return {'state':'QUEUED'}
                if path=='/heartbeat':return {'state':'SUCCEEDED','cleanup_complete':True}
                raise AssertionError(path)
            adapter.head_request.side_effect=request;app.operations.adapters[worker.MODEL]=adapter
            manager.lifecycle=Mock(side_effect=lambda row,lease,target,suffix:order.append(suffix))
            def collect(row,adapter,context):
                lease.release.assert_not_called();context.guard();order.append('collect')
            manager.collect=Mock(side_effect=collect)
            with patch('DGXGateway.dgx_gateway.avatar_jobs.GatewayLease',return_value=lease):manager.run(job)
            self.assertEqual(order,['acquire','load','submit','collect','stop','release']);self.assertEqual(manager.get(job)['state'],'SUCCEEDED');self.assertIsNone(manager.active)
    def test_unproven_cleanup_never_releases_shared_lease(self):
        with tempfile.TemporaryDirectory() as root:
            cfg=SimpleNamespace(state_dir=Path(root),lock_cli=Path('/lock'),lock_root=Path('/lockroot'),token_dir=Path('/tokens'))
            app=SimpleNamespace(config=cfg,_submit_lock=threading.RLock(),operations=SimpleNamespace(adapters={}))
            manager=AvatarJobs(app);job='d'*32;folder=manager.root/job;folder.mkdir();(folder/'image.png').write_bytes(b'img');(folder/'audio.wav').write_bytes(b'wav')
            manager.save({'job_id':job,'state':'QUEUED','created_at':time.time(),'num_segments':1,'prompt':'Test','memory_released':False,'cleanup_required':False});manager.active=job
            lease=Mock(lease_generation='generation-one',token_path=Path('/tokens/test'));lease.acquire.return_value={'lease_generation':'generation-one'}
            adapter=Mock();adapter.head_request.side_effect=RuntimeError('network unavailable');app.operations.adapters[worker.MODEL]=adapter;manager.lifecycle=Mock()
            with patch('DGXGateway.dgx_gateway.avatar_jobs.GatewayLease',return_value=lease):manager.run(job)
            lease.release.assert_not_called();self.assertEqual(manager.active,job);self.assertTrue(manager.get(job)['cleanup_required'])

class ManualAdmissionTests(unittest.TestCase):
    def test_manual_switch_is_rejected_before_creating_operation_for_each_job_manager(self):
        from DGXGateway.dgx_gateway.server import GatewayApplication
        for name in ('creative_jobs', 'video_jobs', 'avatar_jobs'):
            with self.subTest(manager=name):
                store=Mock();store.list_operations.return_value=[]
                app=SimpleNamespace(_submit_lock=threading.RLock(),admission=SimpleNamespace(assert_allowed=Mock()),store=store,
                    operations=SimpleNamespace(adapters={}),residency=SimpleNamespace(releasing_owner=None))
                setattr(app,name,SimpleNamespace(active='owned-job'))
                with self.assertRaises(BlockingIOError):
                    GatewayApplication.submit_switch(app,None,None,'manual-test-key')
                store.create_operation.assert_not_called();store.active_operation.assert_not_called()

    def test_owned_job_or_explicit_residency_release_passes_busy_guard(self):
        from DGXGateway.dgx_gateway.server import GatewayApplication
        for active,releasing in [('owned-job',None),('different-job','owned-job')]:
            store=Mock();store.list_operations.return_value=[];store.active_operation.return_value=True
            app=SimpleNamespace(_submit_lock=threading.RLock(),admission=SimpleNamespace(assert_allowed=Mock()),store=store,
                operations=SimpleNamespace(adapters={}),avatar_jobs=SimpleNamespace(active=active),
                residency=SimpleNamespace(releasing_owner=releasing))
            with self.assertRaisesRegex(BlockingIOError,'another mutation'):
                GatewayApplication.submit_switch(app,None,None,'owned-test-key',job_owner='owned-job')
            store.active_operation.assert_called_once()

if __name__=='__main__':unittest.main()
