"""Persistent Avatar jobs with one parent GPU lease across load, render and archive."""
from __future__ import annotations
import base64,hashlib,io,json,shutil,subprocess,threading,time,uuid,wave
from pathlib import Path
from PIL import Image
from .adapters.longcat import MODEL, AvatarHTTPError
from .authority import GatewayLease,ParentLeaseDelegation
from .domain import classify_local_models
from .maintenance import creative_admission
from .operations import OperationContext

TERMINAL={'SUCCEEDED','FAILED','CANCELED','INTERRUPTED'}
DURATIONS={1:3.72,5:16.52,19:61.32}


class AvatarJobs:
    def __init__(self,app):
        self.app=app;self.root=app.config.state_dir/'avatar-jobs';self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.active=None;self.leases={};self.lock=threading.RLock()
        for path in self.root.glob('*.json'):
            row=json.loads(path.read_text())
            if row['state'] not in TERMINAL or row.get('cleanup_required'):
                row.update(state='INTERRUPTED',cleanup_required=True,error='Gateway restarted; reconcile this Avatar job before new GPU work')
                self.save(row);self.active=row['job_id']
    def save(self,row):
        with self.lock:
            row['updated_at']=time.time();path=self.root/(row['job_id']+'.json');tmp=path.with_suffix('.tmp')
            tmp.write_text(json.dumps(row,ensure_ascii=False,indent=2));tmp.chmod(0o600);tmp.replace(path)
    def read(self,job):
        if not isinstance(job,str) or len(job)!=32 or any(c not in '0123456789abcdef' for c in job):raise ValueError('invalid Avatar job ID')
        path=self.root/(job+'.json')
        if path.is_symlink():raise ValueError('unsafe job path')
        return json.loads(path.read_text())
    def get(self,job):
        row=self.read(job)
        keys={'job_id','state','created_at','updated_at','num_segments','duration_seconds','elapsed_seconds','memory_released','cleanup_required','error','artifact','load_operation','stop_operation','resource_status'}
        return {k:v for k,v in row.items() if k in keys}|{'cancel_requested':self.canceled(job),'model_id':MODEL}
    def list(self):return sorted((self.get(p.stem) for p in self.root.glob('*.json')),key=lambda x:x['created_at'],reverse=True)[:50]
    def canceled(self,job):return (self.root/(job+'.cancel')).exists()
    def cancel(self,job):
        row=self.read(job)
        if row['state'] not in TERMINAL:(self.root/(job+'.cancel')).touch(mode=0o600)
        return self.get(job)
    @staticmethod
    def normalize(body):
        if not isinstance(body,dict) or set(body)!={'image_base64','audio_base64','prompt','num_segments','idempotency_key'}:raise ValueError('invalid Avatar request fields')
        n=body['num_segments']
        if type(n)is not int or n not in DURATIONS:raise ValueError('segments must be 1, 5 or 19')
        prompt=body['prompt'];key=body['idempotency_key']
        if not isinstance(prompt,str) or not 1<=len(prompt)<=2000:raise ValueError('prompt must contain 1–2000 characters')
        if not isinstance(key,str) or not 8<=len(key)<=128:raise ValueError('invalid idempotency key')
        if not isinstance(body['image_base64'],str) or len(body['image_base64'])>12*1024*1024:raise ValueError('image too large')
        if not isinstance(body['audio_base64'],str) or len(body['audio_base64'])>6*1024*1024:raise ValueError('audio too large')
        raw=base64.b64decode(body['image_base64'],validate=True)
        with Image.open(io.BytesIO(raw)) as image:
            width,height=image.size
            if not 128<=width<=4096 or not 128<=height<=4096 or width*height>8_000_000 or not 1/3<=width/height<=3:raise ValueError('reference dimensions outside supported range')
            output=io.BytesIO();image.convert('RGB').save(output,format='PNG');image_bytes=output.getvalue()
        if len(image_bytes)>8*1024*1024:raise ValueError('normalized PNG exceeds 8 MiB')
        audio=base64.b64decode(body['audio_base64'],validate=True)
        if not 44<len(audio)<=4*1024*1024:raise ValueError('audio too large')
        with wave.open(io.BytesIO(audio),'rb') as stream:
            if (stream.getnchannels(),stream.getframerate(),stream.getsampwidth(),stream.getcomptype())!=(1,16000,2,'NONE'):raise ValueError('audio must be mono 16kHz PCM16 WAV')
            if not DURATIONS[n]<=stream.getnframes()/16000<=120:raise ValueError('audio too short for selected duration or exceeds 120 seconds')
            if len(stream.readframes(stream.getnframes()))!=stream.getnframes()*2:raise ValueError('truncated WAV')
        digest=hashlib.sha256(image_bytes+audio+json.dumps([prompt,n],ensure_ascii=False).encode()).hexdigest()
        return image_bytes,audio,digest
    def submit(self,body):
        image,audio,digest=self.normalize(body)
        with self.app._submit_lock:
            for path in self.root.glob('*.json'):
                old=json.loads(path.read_text())
                if old['idempotency_key']==body['idempotency_key']:
                    if old['input_digest']!=digest:raise ValueError('idempotency key reused for different input')
                    return self.get(old['job_id'])
            self.app.admission.assert_allowed()
            if self.active or self.app.store.active_operation() or any(getattr(getattr(self.app,name,None),'active',None) for name in ['creative_jobs','video_jobs','text_jobs']):raise BlockingIOError('DGX busy')
            latest=self.app.store.list_operations(limit=1)
            if latest and latest[0].phase=='RECOVERY_REQUIRED':raise BlockingIOError('Gateway recovery required')
            residency=getattr(self.app,'residency',None)
            if residency and residency.row['state']!='NONE':raise BlockingIOError('Release retained model before Avatar generation')
            with creative_admission(self.app.config):
                job=uuid.uuid4().hex;folder=self.root/job;folder.mkdir(mode=0o700)
                (folder/'image.png').write_bytes(image);(folder/'audio.wav').write_bytes(audio)
                row={'job_id':job,'idempotency_key':body['idempotency_key'],'input_digest':digest,'prompt':body['prompt'],
                     'num_segments':body['num_segments'],'duration_seconds':DURATIONS[body['num_segments']],
                     'state':'QUEUED','created_at':time.time(),'memory_released':False,'cleanup_required':False,'resource_status':'pending'}
                self.save(row);self.active=job
            threading.Thread(target=self.run,args=(job,),daemon=True,name='avatar-job-'+job).start()
            return self.get(job)
    def context(self,row,lease,operation=None):
        def guard():
            lease.check_supervision()
            if self.active!=row['job_id'] or self.read(row['job_id']).get('lease_generation')!=lease.lease_generation:raise RuntimeError('Avatar parent ownership changed')
        return OperationContext(operation or ('avatar-'+row['job_id']),lease.token_path,lease.lease_generation,guard)
    def lifecycle(self,row,lease,target,suffix):
        context=self.context(row,lease)
        with self.app._submit_lock:
            context.guard();self.app.admission.assert_allowed()
            if self.app.store.active_operation():raise BlockingIOError('Existing operation prevents Avatar lifecycle')
            observed=[a.observe() for a in self.app.operations.adapters.values()];active=classify_local_models(observed)
            if active.state not in {'NONE','ACTIVE'}:raise RuntimeError('Ambiguous model state before Avatar lifecycle')
            operation=self.app.store.create_operation(uuid.uuid4().hex,active.model_id if active.state=='ACTIVE' else None,target,
                'avatar-'+row['job_id']+'-'+suffix,source_profile=active.profile if active.state=='ACTIVE' else None)
            row[suffix+'_operation']=operation.operation_id;self.save(row)
        context=self.context(row,lease,operation.operation_id)
        def before_destructive(model):
            context.guard();self.app.operations.adapters[model].drain(context)
            return {'model_id':model,'drained':True,'parent_job_id':row['job_id']}
        delegation=ParentLeaseDelegation(operation_id=operation.operation_id,parent_job_id=row['job_id'],owner_id='avatar_jobs',
            lease_epoch=1,lease_generation=lease.lease_generation,token_path=lease.token_path,guard=lease.check_supervision,
            validate_binding=context.guard,before_destructive=before_destructive,store=self.app.store)
        result=self.app.operations.execute_delegated(operation.operation_id,delegation)
        row[suffix+'_outcome']=result.outcome;self.save(row)
        if result.phase!='DELEGATED_COMPLETED' or result.outcome!='COMMITTED':raise RuntimeError('Avatar lifecycle did not commit: '+str(result.outcome))
    def collect(self,row,adapter,context):
        context.guard();data=adapter.head_request('/result/'+row['job_id'],binary=True,timeout=90);context.guard()
        metadata=row['remote']['output']
        if len(data)!=metadata['size_bytes'] or hashlib.sha256(data).hexdigest()!=metadata['sha256']:raise RuntimeError('Avatar result checksum mismatch')
        path=self.root/row['job_id']/'result.mp4';path.write_bytes(data)
        ffprobe=shutil.which('ffprobe');ffmpeg=shutil.which('ffmpeg')
        if not ffprobe or not ffmpeg:raise RuntimeError('ffmpeg/ffprobe missing for actual video validation')
        probe=subprocess.run([ffprobe,'-v','error','-show_streams','-show_format','-of','json',str(path)],capture_output=True,text=True,timeout=30)
        if probe.returncode:raise RuntimeError('Avatar video probe failed')
        value=json.loads(probe.stdout);video=next(x for x in value['streams'] if x['codec_type']=='video');audio=next(x for x in value['streams'] if x['codec_type']=='audio')
        if video['codec_name']!='h264' or audio['codec_name']!='aac' or abs(float(value['format']['duration'])-row['duration_seconds'])>0.5:raise RuntimeError('Avatar output format/duration mismatch')
        result=subprocess.run([ffmpeg,'-v','error','-nostdin','-i',str(path),'-map','0:v:0','-map','0:a:0','-f','null','-'],capture_output=True,timeout=120)
        context.guard()
        if result.returncode:raise RuntimeError('Avatar complete decode failed')
        row['artifact']={'url':'/api/v1/avatar-jobs/'+row['job_id']+'/video','sha256':metadata['sha256'],'size_bytes':len(data),
                         'width':video['width'],'height':video['height'],'duration_seconds':float(value['format']['duration']),'complete_decode_passed':True}
        self.save(row)
    def run(self,job):
        row=self.read(job);adapter=self.app.operations.adapters[MODEL];lease=None;acquired=False;owns_service=False;submitted=False;settled=False
        started=time.monotonic()
        try:
            if self.canceled(job):raise InterruptedError('Canceled before resource acquisition')
            cfg=self.app.config
            lease=GatewayLease(lock_cli=cfg.lock_cli,lock_root=cfg.lock_root,token_dir=cfg.token_dir,
                operation_id='avatar-'+job,purpose='LongCat Avatar generation and archive '+job,lease_seconds=600)
            receipt=lease.acquire();acquired=True;self.leases[job]=lease;lease.start_heartbeat(interval_seconds=30)
            row.update(state='LOADING',lease_generation=receipt['lease_generation'],resource_status='held');self.save(row)
            self.lifecycle(row,lease,MODEL,'load');owns_service=True
            if self.canceled(job):raise InterruptedError('Canceled after service preparation')
            context=self.context(row,lease);context.guard();folder=self.root/job
            payload={'job_id':job,'lease_generation':lease.lease_generation,'not_after':time.time()+120,
                'image_base64':base64.b64encode((folder/'image.png').read_bytes()).decode(),
                'audio_base64':base64.b64encode((folder/'audio.wav').read_bytes()).decode(),
                'prompt':row['prompt'],'num_segments':row['num_segments']}
            row.update(state='SUBMITTING',remote_submitted=True);self.save(row);submitted=True
            try:
                remote=adapter.head_request('/jobs',payload,timeout=60)
            except AvatarHTTPError as rejected:
                if rejected.status in {400,401,403,404,422}:submitted=False;settled=True
                raise
            deadline=time.monotonic()+{1:1800,5:7200,19:14400}[row['num_segments']]+180
            canceled_sent=False
            while time.monotonic()<deadline:
                context.guard()
                if self.canceled(job) and not canceled_sent:
                    adapter.head_request('/cancel',{'job_id':job});canceled_sent=True
                remote=adapter.head_request('/heartbeat',{'job_id':job,'lease_generation':lease.lease_generation,'not_after':time.time()+120})
                row.update(state='CANCELING' if canceled_sent else 'RUNNING',remote=remote,elapsed_seconds=time.monotonic()-started);self.save(row)
                if remote['state'] in TERMINAL:
                    settled=remote.get('cleanup_complete') is True
                    if not settled:raise RuntimeError('Avatar remote cleanup not proven')
                    if remote['state']!='SUCCEEDED' or self.canceled(job):raise InterruptedError('Avatar job canceled or did not finish')
                    break
                time.sleep(5)
            else:raise TimeoutError('Avatar render exceeded bounded duration')
            row['state']='COLLECTING';self.save(row);self.collect(row,adapter,context)
            row['state']='RELEASING';self.save(row);self.lifecycle(row,lease,None,'stop');owns_service=False
            lease.check_supervision();row['release_receipt']=lease.release();acquired=False
            row.update(state='SUCCEEDED',memory_released=True,cleanup_required=False,resource_status='released')
        except BaseException as error:
            row.update(state='CANCELING' if self.canceled(job) else 'FAILED',error=str(error)[:300],cleanup_required=acquired)
            self.save(row)
            if acquired:
                try:
                    context=self.context(row,lease);context.guard()
                    if submitted and not settled:
                        adapter.head_request('/cancel',{'job_id':job})
                        deadline=time.monotonic()+120
                        while time.monotonic()<deadline:
                            context.guard();remote=adapter.head_request('/jobs/'+job);row['remote']=remote
                            if remote.get('cleanup_complete'):settled=True;break
                            time.sleep(3)
                        if not settled:raise RuntimeError('Avatar cleanup requires recovery')
                    if owns_service:
                        self.lifecycle(row,lease,None,'stop');owns_service=False
                    elif row.get('load_outcome') not in {'FAILED_SAFE_STOPPED','ROLLED_BACK'} and row.get('load_operation'):
                        raise RuntimeError('Avatar lifecycle recovery remains unresolved')
                    adapter.stopped_proof(context)
                    row['release_receipt']=lease.release();acquired=False
                    row.update(memory_released=True,cleanup_required=False,resource_status='released')
                except BaseException as cleanup_error:
                    row.update(state='INTERRUPTED',cleanup_required=True,error=str(cleanup_error)[:300],resource_status='recovery_required')
            else:row.update(memory_released=True,cleanup_required=False,resource_status='released')
            if not acquired:row['state']='CANCELED' if self.canceled(job) else 'FAILED'
        finally:
            row['elapsed_seconds']=time.monotonic()-started;self.save(row)
            if not acquired:
                self.leases.pop(job,None)
                with self.app._submit_lock:
                    if self.active==job:self.active=None
    def recover_child(self,row,lease,operation):
        if (operation.phase!='RECOVERY_REQUIRED' or
                not operation.idempotency_key.startswith('avatar-'+row['job_id']+'-') or
                (operation.target_model!=MODEL and not (operation.source_model==MODEL and operation.target_model is None))):
            raise RuntimeError('Recovery operation does not belong to this Avatar parent')
        context=self.context(row,lease,operation.operation_id);context.guard()
        avatar=self.app.operations.adapters[MODEL]
        if avatar.observe().state=='HEALTHY':
            active=avatar.head_request('/health').get('active_job')
            if active not in {None,row['job_id']}:raise RuntimeError('A different Avatar job owns the service')
        avatar.retire(context)
        self.app.store.append_event(operation.operation_id,'avatar-recovery-stopped-proof',avatar.stopped_proof(context))
        source=operation.source_model if operation.target_model==MODEL and operation.source_model!=MODEL else None
        if source:
            adapter=self.app.operations.adapters[source];observed=adapter.observe()
            if observed.state!='HEALTHY':
                if observed.state!='STOPPED':adapter.retire(context);adapter.stopped_proof(context)
                adapter.start(context,operation.source_profile)
            if adapter.health(context).get('healthy') is not True:raise RuntimeError('Avatar recovery source restore failed')
        self.app.operations._prove_others_stopped(operation.operation_id,context,source,'avatar-recovery-other-stopped-proof')
        self.app.store.append_event(operation.operation_id,'avatar-parent-recovery-complete',{'parent_job_id':row['job_id'],'lease_generation':lease.lease_generation,'restored_model':source})
        # Preserve the original failure outcome; only the parent releases its lease.
        self.app.store.set_phase(operation.operation_id,'DELEGATED_COMPLETED')
        row['restored_model']=source

    def reconcile(self,job):
        row=self.read(job)
        if not row.get('cleanup_required'):return self.get(job)
        if self.active!=job:raise ValueError('Avatar recovery owner mismatch')
        lease=self.leases.get(job)
        if lease is None:
            cfg=self.app.config;lease=GatewayLease(lock_cli=cfg.lock_cli,lock_root=cfg.lock_root,token_dir=cfg.token_dir,
                operation_id='avatar-'+job,purpose='Resume Avatar cleanup',lease_seconds=600)
            lease.lease_generation=row.get('lease_generation')
            # Never steal/recreate a stale lease. The existing authority requires
            # explicit lock recovery if its former controller died.
            lease.guard();lease.start_heartbeat(interval_seconds=30);self.leases[job]=lease
        if self.app.store.active_operation():raise BlockingIOError('Resolve retained lifecycle operation before Avatar cleanup')
        adapter=self.app.operations.adapters[MODEL];context=self.context(row,lease);context.guard()
        pending=[op for op in self.app.store.list_operations() if op.phase=='RECOVERY_REQUIRED' and op.idempotency_key.startswith('avatar-'+job+'-')]
        if pending:
            if len(pending)!=1:raise RuntimeError('Multiple Avatar recovery operations require inspection')
            self.recover_child(row,lease,pending[0]);adapter.stopped_proof(context)
            row['release_receipt']=lease.release();row.update(state='CANCELED' if self.canceled(job) else 'FAILED',cleanup_required=False,memory_released=True,resource_status='released')
            self.save(row);self.active=None;self.leases.pop(job,None);return self.get(job)
        remote=adapter.head_request('/jobs/'+job)
        if not remote.get('cleanup_complete'):
            adapter.head_request('/cancel',{'job_id':job})
            remote=adapter.head_request('/reconcile',{'job_id':job})
            if not remote.get('cleanup_complete'):return self.get(job)
        health=adapter.head_request('/health')
        if health.get('active_job') not in {None,job}:raise RuntimeError('A different Avatar job is active')
        self.lifecycle(row,lease,None,'reconcile-stop');adapter.stopped_proof(context)
        row['release_receipt']=lease.release();row.update(state='CANCELED' if self.canceled(job) else 'FAILED',cleanup_required=False,memory_released=True,resource_status='released')
        self.save(row);self.active=None;self.leases.pop(job,None);return self.get(job)
