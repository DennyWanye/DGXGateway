"""Persistent Comfy jobs. One owner spans load, execution, and model release."""
from __future__ import annotations
import json,time,uuid,threading,urllib.request,urllib.parse,urllib.error,hashlib,stat
from pathlib import Path
from .maintenance import creative_admission
from .image_assets import ImageAssets
from .job_cancel import CancelableJobs, JobCanceled
from .image_batches import manifest, update_progress, attribution, finish_candidates
from .comfy_identity import COMFY_SERVICE

TERMINAL = {"SUCCEEDED", "FAILED", "INTERRUPTED", "CANCELED"}

class CreativeJobs(CancelableJobs):
    cancel_boundary = "Waiting for the current loading/upload/collection boundary or Comfy sampling interruption; then releasing memory"
    def __init__(self, app):
        self.app=app
        self.root=app.config.state_dir / 'creative-jobs'; self.root.mkdir(parents=True,exist_ok=True)
        self.active=None
        self.assets=ImageAssets(app.config.state_dir / 'creative-assets')
        self.http=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.base=f'http://{app.config.nodes[0].address}:{app.config.music3_port}'
        for path in self.root.glob('*.json'):
            try:
                d=json.loads(path.read_text())
                if d.get('state') not in TERMINAL or d.get('cleanup_required'):
                    d.update(state='INTERRUPTED',error='Gateway restarted; reconcile this job before another submission',memory_released=False,cleanup_required=True)
                    self.active=d['job_id']
                    self.save(d)
            except (ValueError,OSError): pass
    def request(self,path,body=None):
        req=urllib.request.Request(self.base+path,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'})
        with self.http.open(req,timeout=15) as r: return json.load(r)
    def save(self,d):
        d['updated_at']=time.time()
        path=self.root/(d['job_id']+'.json'); tmp=path.with_suffix('.tmp')
        tmp.write_text(json.dumps(d,ensure_ascii=False,indent=2)); tmp.replace(path)
    def get(self,job_id):
        if not isinstance(job_id,str) or len(job_id)!=32 or any(c not in '0123456789abcdef' for c in job_id): raise ValueError('invalid job_id')
        d=json.loads((self.root/(job_id+'.json')).read_text())
        view = {k:v for k,v in d.items() if k!='workflow'}
        batch = view.get('image_batch')
        if batch:
            available = []
            for artifact in view.get('artifacts', []):
                try:
                    info = Path(artifact['path']).lstat()
                    valid = stat.S_ISREG(info.st_mode) and info.st_size > 0
                    if 'size_bytes' in artifact: valid = valid and info.st_size == artifact['size_bytes']
                    if 'mtime_ns' in artifact: valid = valid and info.st_mtime_ns == artifact['mtime_ns']
                except OSError:
                    valid = False
                if valid:
                    available.append(artifact)
                    continue
                candidate = next((c for c in batch['candidates'] if c['index'] == artifact.get('candidate_index')), None)
                if candidate:
                    candidate.update(state='UNKNOWN', error='Saved image download requires recovery')
                    view['artifact_recovery_required'] = True
            view['artifacts'] = available
            if view.get('artifact_recovery_required') and view['state'] == 'SUCCEEDED':
                view.update(state='FAILED', error='Saved image files need recovery; generation will not repeat')
        from .runtime_view import job_runtime_view
        view['runtime'] = job_runtime_view(d, kind='creative')
        return self.cancel_view(view)
    def catalog(self):
        try:
            info=self.request('/object_info')
            return {'online':True,'base_url':self.base,'nodes':len(info),'loaders':{k:info[k]['input'] for k in ('UNETLoader','CLIPLoader','VAELoader','CheckpointLoaderSimple','LoraLoader') if k in info}}
        except Exception:
            return {'online':False,'base_url':self.base,'hint':'Use dgx_switch_model comfyui (head ComfyUI) then poll operation. No weights load until a workflow executes.'}
    def submit(self,workflow,key,input_assets=None, *, image_request=None, retry_of=None):
        input_assets=list(input_assets or [])
        if len(input_assets)>16: raise ValueError("too many input assets")
        for asset_id in input_assets: self.assets.get(asset_id)
        if not isinstance(workflow,dict) or not workflow: raise ValueError('workflow must contain API-format nodes')
        if len(workflow) > 200 and image_request is None: raise ValueError('raw workflow must contain 1–200 API-format nodes')
        for node in workflow.values():
            if not isinstance(node,dict) or not isinstance(node.get('class_type'),str) or not isinstance(node.get('inputs'),dict): raise ValueError('invalid API-format workflow')
        image_batch = manifest(workflow)
        with self.app._submit_lock:
            for path in self.root.glob('*.json'):
                old=json.loads(path.read_text())
                if old.get('idempotency_key')==key:
                    if old.get('workflow')!=workflow or old.get('input_assets',[])!=input_assets or old.get('retry_of') != retry_of: raise ValueError('idempotency key reused with different workflow')
                    return self.get(old['job_id'])
            if retry_of:
                parent = self.get(retry_of)
                states = {c['index']: c['state'] for c in parent.get('image_batch', {}).get('candidates', [])}
                indices = [c['index'] for c in image_batch['candidates']]
                if parent['state'] not in TERMINAL or parent.get('memory_released') is not True or parent.get('cleanup_required') or any(states.get(i) not in {'FAILED', 'CANCELED'} for i in indices):
                    raise ValueError('Only known failed candidates from released jobs can be retried')
                for path in self.root.glob('*.json'):
                    child = json.loads(path.read_text())
                    if child.get('retry_of') == retry_of and set(child.get('retry_indices', [])) & set(indices):
                        raise ValueError('Candidate already has a retry job: ' + child['job_id'])
            text_jobs = getattr(self.app, 'text_jobs', None)
            if text_jobs: text_jobs.assert_available()
            if self.active or getattr(getattr(self.app, 'video_jobs', None), 'active', None) or getattr(getattr(self.app, 'avatar_jobs', None), 'active', None) or self.app.store.active_operation(): raise BlockingIOError('DGX busy')
            latest=self.app.store.list_operations(limit=1)
            if latest and latest[0].phase=='RECOVERY_REQUIRED': raise BlockingIOError('Gateway recovery required')
            with creative_admission(self.app.config):
                job_id=uuid.uuid4().hex
                d={'job_id':job_id,'idempotency_key':key,'workflow':workflow,'input_assets':input_assets,'state':'QUEUED','created_at':time.time(),'memory_released':False}
                if image_batch: d['image_batch'] = image_batch
                if image_request: d['image_request'] = image_request
                if retry_of: d.update(retry_of=retry_of, retry_indices=[c['index'] for c in image_batch['candidates']])
                self.save(d)
                self.active=job_id
            threading.Thread(target=self.run,args=(d,),daemon=True).start()
            return self.get(d['job_id'])
    def retry_images(self, job_id, indices, key):
        from .image_batches import candidate_indices
        from .image_workflows import generate_image, edit_image
        candidate_indices(len(indices), 2, indices)
        parent = self.get(job_id)
        saved = parent.get('image_request')
        if not saved: raise ValueError('This legacy/raw workflow has no typed image retry contract')
        args = dict(saved['parameters'])
        args.update(batch_count=len(indices), candidate_indices=sorted(indices))
        assets = args.get('asset_ids', [])
        if saved['tool'] == 'generate_image':
            # Jobs saved before the quality default omitted model and actually ran Z-Image.
            graph = generate_image(args['prompt'], args.get('width', 1024), args.get('height', 1024), args.get('seed', 42),
                            len(indices), args.get('parallelism', 2), sorted(indices), args.get('model', 'z-image-turbo'))
        elif saved['tool'] == 'edit_image':
            filenames = [self.assets.get(a)['comfy_filename'] for a in assets]
            graph = edit_image(args, filenames)
        else: raise ValueError('Image retry contract is unavailable')
        return self.submit(graph, key, assets, image_request={'tool': saved['tool'], 'parameters': args}, retry_of=job_id)

    def reconcile(self,job_id):
        observed = self.get(job_id)  # Validate identity and derive local artifact availability without writes.
        with self.app._submit_lock:
            text_jobs = getattr(self.app, 'text_jobs', None)
            if text_jobs: text_jobs.assert_available()
            d=json.loads((self.root/(job_id+'.json')).read_text())
            if observed.get('artifact_recovery_required'):
                d['image_batch'] = observed['image_batch']
            download_recovery = (d.get('memory_released') is True and d.get('outputs') and
                any(c.get('error') == 'Saved image download requires recovery' for c in d.get('image_batch', {}).get('candidates', [])))
            if download_recovery:
                residency = getattr(self.app, 'residency', None)
                if residency: residency.release_for_other()
                if self.active or getattr(getattr(self.app, 'video_jobs', None), 'active', None) or getattr(getattr(self.app, 'avatar_jobs', None), 'active', None) or self.app.store.active_operation():
                    raise BlockingIOError('DGX busy')
                latest = self.app.store.list_operations(limit=1)
                if latest and latest[0].phase == 'RECOVERY_REQUIRED': raise BlockingIOError('Gateway recovery required')
                with creative_admission(self.app.config):
                    d.update(state='RECONCILING', memory_released=False, cleanup_required=True)
                    self.active = job_id
                    self.save(d)
                threading.Thread(target=self._recover_downloads, args=(d,), daemon=True).start()
                return self.get(job_id)
            # Older releases could strand a rejected resident handoff as FAILED
            # with no cleanup owner. Settle only a provably unsubmitted task;
            # never infer this from an exception string or an absent prompt alone.
            residency = getattr(self.app, 'residency', None)
            unsubmitted_handoff = (d.get('state') == 'FAILED' and d.get('resource_reused') is True
                and d.get('resident_id') and not d.get('memory_released')
                and not any(d.get(k) for k in ('start_operation_id', 'prompt_id', 'outputs', 'artifacts', 'cleanup_required')))
            if unsubmitted_handoff and self.active is None and residency and residency.row['state'] == 'NONE':
                if (self.app.store.active_operation() or any(
                        getattr(getattr(self.app, name, None), 'active', None)
                        for name in ('video_jobs', 'avatar_jobs'))):
                    raise BlockingIOError('DGX busy')
                operations = self.app.store.list_operations(limit=-1)
                if any(op.idempotency_key.startswith(job_id + '-') for op in operations):
                    raise BlockingIOError('Task lifecycle history exists; ownership recovery is required')
                observed = [adapter.observe() for adapter in self.app.operations.adapters.values()]
                if not observed or any(item.state != 'STOPPED' for item in observed):
                    raise BlockingIOError('All models must be proved stopped before settling an old handoff')
                d.update(memory_released=True, resource_not_acquired=True,
                         resource_settled=True, resource_status='released', cleanup_required=False,
                         release_reason='Reconciled rejected handoff: no task lifecycle or inference submission; all models stopped')
                self.save(d)
                return self.get(job_id)
            if self.active != job_id: raise ValueError('job does not own recovery')
            if not d.get('cleanup_required'): raise BlockingIOError('job still running')
            if self.app.store.active_operation(): raise BlockingIOError('model operation still active')
            with creative_admission(self.app.config):
                d['cleanup_required']=False;d['state']='RECONCILING';self.save(d)
            threading.Thread(target=self._reconcile,args=(d,),daemon=True).start()
            return self.get(job_id)
    def _recover_downloads(self, d):
        """Explicit artifact-only recovery. Never execute or resubmit a prompt."""
        owns = False
        external = False
        try:
            self.switch(d, COMFY_SERVICE, 'download-start-' + uuid.uuid4().hex)
            owns = True
            queue = self.request('/queue')
            external = bool(queue.get('queue_running') or queue.get('queue_pending'))
            if external: raise RuntimeError('Comfy has existing work; leave it intact')
            self.collect(d, require_complete=False)
            d['state'] = 'SUCCEEDED' if all(c['state'] == 'SUCCEEDED' for c in d['image_batch']['candidates']) else 'FAILED'
            if d['state'] == 'SUCCEEDED': d.pop('error', None)
        except Exception as exc:
            d.update(state='FAILED', error=str(exc))
        finally:
            if owns and not external:
                try:
                    self.switch(d, None, 'download-stop-' + uuid.uuid4().hex)
                    d.update(memory_released=True, cleanup_required=False)
                except Exception as exc:
                    d.update(state='INTERRUPTED', error=str(exc), cleanup_required=True)
            if self.cancel_requested(d['job_id']):
                d['state'] = 'CANCELED' if d.get('memory_released') else 'INTERRUPTED'
            residency = getattr(self.app, 'residency', None)
            if residency: residency.job_finished('creative_jobs', d)
            finish_candidates(d.get('image_batch'), d['state'], False)
            self.save(d)
            with self.app._submit_lock:
                if not d.get('cleanup_required'): self.active = None

    def _reconcile(self,d):
        try:
            if self.app.store.active_operation() or any(op.phase == 'RECOVERY_REQUIRED' for op in self.app.store.list_operations(limit=1)):
                raise BlockingIOError('Resolve Gateway lifecycle recovery before reconciling media')
            observed = [adapter.observe() for adapter in self.app.operations.adapters.values()]
            if observed and all(model.state == 'STOPPED' for model in observed):
                # HTTP is absent after successful process stop. Obtain a fresh
                # Gateway all-models-stopped proof under its normal lease instead.
                self.switch(d,None,'reconcile-prove-stopped')
                d['memory_released']=True;d['cleanup_required']=False
                artifacts=d.get('artifacts',[])
                valid=bool(artifacts) and all(Path(a['path']).is_file() and Path(a['path']).stat().st_size > 0 for a in artifacts)
                successful=(d.get('comfy_status') or {}).get('status_str') == 'success' and (not d.get('image_batch') or all(c['state'] == 'SUCCEEDED' for c in d['image_batch']['candidates']))
                d['state']='SUCCEEDED' if valid and successful else 'FAILED'
                if d['state']=='SUCCEEDED': d.pop('error',None)
                else: d['error']='Models stopped; no verified saved successful result available'
                return
            q=self.request('/queue')
            if q.get('queue_running') or q.get('queue_pending'): raise BlockingIOError('Comfy queue is not idle; reconcile after it finishes')
            history=self.request('/history/'+d['prompt_id']) if d.get('prompt_id') else self.request('/history')
            records=[(pid,row) for pid,row in history.items() if pid==d.get('prompt_id') or (len(row.get('prompt',[]))>3 and row['prompt'][3].get('client_id')==d['job_id'])]
            if len(records)>1: raise RuntimeError('multiple matching prompts; inspect history')
            if records:
                pid,row=records[0];d['prompt_id']=pid;d.setdefault('outputs', {}).update(row.get('outputs', {}));d['comfy_status']=row.get('status')
                if d['outputs']: self.collect(d, require_complete=False)
            self.switch(d,None,'reconcile-stop');d['memory_released']=True;d['cleanup_required']=False
            d['state']='SUCCEEDED' if d.get('artifacts') and (d.get('comfy_status') or {}).get('status_str') == 'success' and (not d.get('image_batch') or all(c['state'] == 'SUCCEEDED' for c in d['image_batch']['candidates'])) else 'FAILED'
        except Exception as e:
            d.update(state='INTERRUPTED',error=str(e),cleanup_required=True)
        finally:
            if self.cancel_requested(d['job_id']):
                if d.get('memory_released'): d['state']='CANCELED'
                elif d.get('cleanup_required'): d['state']='INTERRUPTED'
            residency = getattr(self.app, 'residency', None)
            if residency: residency.job_finished('creative_jobs', d)
            finish_candidates(d.get('image_batch'), d['state'], bool(d.get('comfy_status')))
            self.save(d)
            with self.app._submit_lock:
                if not d.get('cleanup_required'): self.active=None

    def switch(self,d,target,suffix):
        options = {'job_owner': d['job_id']}
        if target and getattr(self.app, 'residency', None) and d.get('image_request'): options['non_preemptive'] = True
        op,_=self.app.submit_switch(target,None,d['job_id']+'-'+suffix,**options)
        d[suffix+'_operation_id']=op.operation_id; self.save(d)
        deadline=time.monotonic()+1800
        while time.monotonic()<deadline:
            op=self.app.store.get_operation(op.operation_id)
            if op.phase in {'RELEASED','RECOVERY_REQUIRED','CANCELED'}:
                if op.outcome!='COMMITTED': raise RuntimeError('model lifecycle did not commit')
                return
            time.sleep(1)
        raise TimeoutError('model lifecycle timeout')
    def batch_progress(self, d):
        if not d.get('image_batch') or not d.get('prompt_id'):
            return
        query = urllib.parse.urlencode({'client_id': d['job_id']})
        try:
            progress = self.request('/dgx/batch-progress/' + urllib.parse.quote(d['prompt_id'], safe='') + '?' + query)
        except urllib.error.HTTPError as exc:
            if exc.code == 404: return  # No sampler has executed yet.
            raise
        if progress.get('prompt_id') != d['prompt_id']:
            raise ValueError('batch progress prompt identity mismatch')
        update_progress(d['image_batch'], progress)
        outputs = progress.get('outputs', {})
        for node, output in outputs.items():
            group = next((g for g in d['image_batch']['groups'] if g['saveNode'] == node), None)
            if not group or output.get('indices') != group['indices'] or output.get('seeds') != group['seeds']:
                raise ValueError('batch saved output identity mismatch')
        d.setdefault('outputs', {}).update(outputs)
        if outputs: self.collect(d, require_complete=False)
        self.save(d)

    def collect(self, d, require_complete=True):
        # Persist each output independently. Stable identity survives polling order,
        # partial downloads, cancellation and a Gateway process restart.
        artifacts = d.setdefault('artifacts', [])
        root = self.app.config.state_dir / 'creative-artifacts' / d['job_id']
        root.mkdir(parents=True, exist_ok=True)
        errors = []
        for node, output in d.get('outputs', {}).items():
            for kind, items in output.items():
                if not isinstance(items, list): continue
                for index, item in enumerate(items):
                    if not isinstance(item, dict) or not isinstance(item.get('filename'), str): continue
                    basename = Path(item['filename']).name
                    if not basename or basename in {'.', '..'}: continue
                    identity = json.dumps([node, kind, index, item.get('filename'), item.get('subfolder', ''), item.get('type', '')], separators=(',', ':'))
                    key = hashlib.sha256(identity.encode()).hexdigest()[:20]
                    old = next((a for a in artifacts if a.get('output_key') == key), None)
                    if old and Path(old['path']).is_file() and Path(old['path']).stat().st_size > 0:
                        if not d.get('image_batch'):
                            continue
                        from .image_assets import _read_regular, MAX_IMAGE_BYTES
                        try:
                            data = _read_regular(Path(old['path']), MAX_IMAGE_BYTES)
                            if old.get('sha256') == hashlib.sha256(data).hexdigest():
                                info = Path(old['path']).stat()
                                old.update(size_bytes=info.st_size, mtime_ns=info.st_mtime_ns)
                                candidate = next(c for c in d['image_batch']['candidates'] if c['index'] == old['candidate_index'])
                                candidate.update(state='SUCCEEDED', sourceKey=Path(old['path']).name)
                                candidate.pop('error', None)
                                self.save(d)
                                continue
                        except (OSError, ValueError):
                            pass
                        # Missing, altered or unverified local data is re-downloaded by source identity.

                    target = root / (key + '-' + basename)
                    temp = target.with_suffix(target.suffix + '.part')
                    try:
                        meta = attribution(d.get('image_batch'), node, index)
                        query = urllib.parse.urlencode({k: item.get(k, '') for k in ('filename', 'subfolder', 'type')})
                        with self.http.open(self.base + '/view?' + query, timeout=60) as response, temp.open('wb') as stream:
                            import shutil
                            if meta:
                                data = response.read(8 * 1024 * 1024 + 1)
                                if len(data) > 8 * 1024 * 1024: raise ValueError('candidate image exceeds size limit')
                                stream.write(data)
                            else:
                                shutil.copyfileobj(response, stream)
                        if not temp.stat().st_size: raise RuntimeError('empty artifact')
                        digest = None
                        if meta:
                            from PIL import Image
                            with Image.open(temp) as image:
                                if image.format not in {'PNG', 'JPEG', 'WEBP'} or image.width * image.height > 16_000_000:
                                    raise ValueError('invalid candidate image')
                                image.verify()
                            with Image.open(temp) as image: image.load()
                            digest = hashlib.sha256(temp.read_bytes()).hexdigest()
                            if old and old.get('sha256') and old['sha256'] != digest:
                                raise ValueError('Saved artifact content changed; original identity cannot be replaced')
                        temp.replace(target)
                        artifact = {'node': node, 'kind': kind, 'path': str(target), 'output_key': key, 'sha256': digest, 'size_bytes': target.stat().st_size, 'mtime_ns': target.stat().st_mtime_ns,
                                    'url': f'http://{self.app.config.bind}:{self.app.config.port}/api/v1/artifacts/{d["job_id"]}/{urllib.parse.quote(target.name)}', **meta}
                        if old: artifacts.remove(old)
                        artifacts.append(artifact)
                        if meta:
                            candidate = next(c for c in d['image_batch']['candidates'] if c['index'] == meta['candidate_index'])
                            candidate.update(state='SUCCEEDED', sourceKey=target.name)
                            candidate.pop('error', None)
                        self.save(d)
                    except (OSError, ValueError, RuntimeError) as exc:
                        temp.unlink(missing_ok=True)
                        errors.append({'output_key': key, 'error': type(exc).__name__})
                        if d.get('image_batch'):
                            meta = attribution(d['image_batch'], node, index)
                            candidate = next(c for c in d['image_batch']['candidates'] if c['index'] == meta['candidate_index'])
                            candidate.update(state='COLLECTING', error='Saved image download requires recovery')
        d['collection_errors'] = errors
        self.save(d)
        if require_complete:
            if not artifacts: raise RuntimeError('workflow has no saved media artifacts')
            if errors or (d.get('image_batch') and any(c['state'] != 'SUCCEEDED' for c in d['image_batch']['candidates'])):
                raise RuntimeError('Some image candidates are missing; saved successful candidates retained')

    def upload_assets(self,d):
        for asset_id in d.get('input_assets',[]):
            asset=self.assets.get(asset_id)
            boundary='dgx-'+uuid.uuid4().hex
            name=asset['comfy_filename']
            prefix=(f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\n'
                    f'Content-Type: {asset["content_type"]}\r\n\r\n').encode()
            suffix=(f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="type"\r\n\r\ninput'
                    f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\ntrue'
                    f'\r\n--{boundary}--\r\n').encode()
            request=urllib.request.Request(self.base+'/upload/image',
                data=prefix+Path(asset['path']).read_bytes()+suffix,
                headers={'Content-Type': 'multipart/form-data; boundary='+boundary})
            with self.http.open(request,timeout=60) as response: result=json.load(response)
            if result.get('name')!=name or result.get('subfolder','')!='' or result.get('type')!='input':
                raise RuntimeError('Comfy image upload did not preserve the registered asset name')

    def run(self,d):
        submitted=False; completed=False; owns_comfy=False; external_work=False
        try:
            self.check_cancel(d)
            d['state']='LOADING'; self.save(d)
            residency = getattr(self.app, 'residency', None)
            if residency: residency.before_start('creative_jobs', d)
            self.switch(d,COMFY_SERVICE,'start'); owns_comfy=True
            if residency: residency.claimed('creative_jobs', d)
            self.check_cancel(d)
            queue=self.request('/queue')
            if queue.get('queue_running') or queue.get('queue_pending'):
                external_work=True
                raise RuntimeError('Comfy has existing work; leave it intact')
            if d.get('input_assets'):
                d['state']='UPLOADING';self.save(d)
                self.upload_assets(d)
            self.check_cancel(d)
            d['state']='SUBMITTING'; self.save(d)
            # A lost POST response is ambiguous. Never repeat automatically.
            submitted=True
            try:
                response=self.request('/prompt',{'prompt':d['workflow'],'client_id':d['job_id']})
            except urllib.error.HTTPError as e:
                if 400 <= e.code < 500: completed=True
                raise
            if response.get('error') or not response.get('prompt_id'):
                completed=True
                raise RuntimeError('Comfy rejected workflow')
            d['prompt_id']=response['prompt_id']; d['state']='RUNNING'; self.save(d)
            deadline=time.monotonic()+7200
            while time.monotonic()<deadline:
                if self.cancel_requested(d['job_id']):
                    canceled = self.request('/dgx/cancel', {'prompt_id': d['prompt_id'], 'client_id': d['job_id']})
                    if canceled.get('state') == 'canceled_pending':
                        completed=True
                        raise JobCanceled('Queued Comfy prompt canceled')
                try:
                    self.batch_progress(d)
                    d.pop('progress_error', None)
                except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                    d['progress_error'] = type(exc).__name__
                    self.save(d)  # Telemetry outage must not stop or resubmit running work.
                history=self.request('/history/'+d['prompt_id'])
                record=history.get(d['prompt_id'])
                if record:
                    completed=True
                    d['comfy_status']=record.get('status'); d.setdefault('outputs', {}).update(record.get('outputs', {}))
                    if d['outputs']: self.collect(d, require_complete=False)
                    if self.cancel_requested(d['job_id']):
                        raise JobCanceled('Comfy generation canceled')
                    if record.get('status',{}).get('status_str')=='error': raise RuntimeError('Comfy execution failed; inspect comfy_status')
                    break
                time.sleep(2)
            else: raise TimeoutError('Generation timeout; inspect prompt_id before any retry')
            if not d['outputs']: raise RuntimeError('No output artifacts produced')
            self.collect(d)
            d['state']='RELEASING';self.save(d)
            if not (residency and residency.retain('creative_jobs', d)):
                self.switch(d,None,'stop'); d['memory_released']=True
            d['state']='CANCELED' if self.cancel_requested(d['job_id']) else 'SUCCEEDED'
        except Exception as e:
            d['state']='CANCELING' if isinstance(e,JobCanceled) else 'FAILED'; d['error']=str(e)
            # Safe cleanup only when no request is ambiguous and queue is empty.
            d['cleanup_required']=owns_comfy and not external_work
            if isinstance(e, BlockingIOError) and not owns_comfy and not d.get('start_operation_id'):
                d.update(memory_released=True, resource_not_acquired=True, resource_settled=True, resource_status='released', release_reason='Admission rejected before this job acquired resources')
            if isinstance(e,JobCanceled) and not owns_comfy:
                # No model mutation was made for a pre-start cancellation.
                d['memory_released']=True
                d['release_reason']='No model was loaded by this job'
            if owns_comfy and not external_work and (not submitted or completed):
                try:
                    q=self.request('/queue')
                    if not q.get('queue_running') and not q.get('queue_pending'):
                        self.switch(d,None,'cleanup'); d['memory_released']=True;d['cleanup_required']=False
                except Exception: d['cleanup_required']=True
        finally:
            if self.cancel_requested(d['job_id']):
                if d.get('memory_released'): d['state']='CANCELED'
                elif d.get('cleanup_required'): d['state']='INTERRUPTED'
            residency = getattr(self.app, 'residency', None)
            if residency: residency.job_finished('creative_jobs', d)
            finish_candidates(d.get('image_batch'), d['state'], completed or not submitted)
            self.save(d)
            with self.app._submit_lock:
                if not d.get('cleanup_required'): self.active=None
