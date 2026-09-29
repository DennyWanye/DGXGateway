#!/usr/bin/env python3
"""Real Gateway artifact, admission and cancellation acceptance using official fixture."""
import base64,hashlib,importlib.util,json,subprocess,time,urllib.request,urllib.error
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
spec=importlib.util.spec_from_file_location('safeclient',ROOT/'scripts/qwen38-flash-next/gateway-client.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
request=mod.client();out=ROOT/'plans/longcat-avatar15/evidence/gateway';state=Path.home()/'.local/state/dgx-cluster-gateway/avatar-jobs'
first='25b13a4f6c0f47029b12541a44120a15';row=json.loads((state/(first+'.json')).read_text())
assert row['state']=='SUCCEEDED' and row['memory_released'] and not row['cleanup_required']
(out/'first-success.json').write_text(json.dumps(row,indent=2))
fixture=ROOT/'plans/longcat-avatar15/evidence/install/inputs'
body={'image_base64':base64.b64encode((fixture/'man.png').read_bytes()).decode(),'audio_base64':base64.b64encode((fixture/'speech.wav').read_bytes()).decode(),'prompt':row['prompt'],'num_segments':1,'idempotency_key':row['idempotency_key']}
assert request('/api/v1/avatar-jobs',body)['job']['job_id']==first
# Local LAN mode: normal GET allowed, cross-origin mutations forbidden.
root='http://192.0.2.37:18082';opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
req=urllib.request.Request(root+'/api/v1/avatar-jobs/'+first+'/video',headers={'Range':'bytes=0-1023'})
with opener.open(req,timeout=20) as r:
    data=r.read();assert r.status==206 and len(data)==1024;assert data==(state/first/'result.mp4').read_bytes()[:1024]
req=urllib.request.Request(root+'/api/v1/avatar-jobs',data=b'{}',headers={'Origin':'http://untrusted.invalid','Content-Type':'application/json'})
try:opener.open(req,timeout=20);raise AssertionError('cross origin accepted')
except urllib.error.HTTPError as e:assert e.code==403
try:request('/api/v1/avatar-jobs',dict(body,num_segments=2,idempotency_key='test-reject-2seg'));raise AssertionError('bad duration accepted')
except urllib.error.HTTPError as e:assert e.code==422
body['idempotency_key']='longcat-real-cancel-20260910'
job=request('/api/v1/avatar-jobs',body)['job']['job_id'];print(json.dumps({'cancel_test_job':job}),flush=True)
(out/'cancel-job-id.json').write_text(json.dumps({'job_id':job}))
# Wait for real owned renderer container, rather than canceling only a queued job.
for _ in range(90):
    status=request('/api/v1/avatar-jobs/'+job)['job']
    containers=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8','operator@192.0.2.30','sudo -n docker ps --filter label=org.dgxspark.longcat.guard-id --format "{{.Names}}"'],capture_output=True,text=True,timeout=20)
    if status['state']=='RUNNING' and containers.returncode==0 and containers.stdout.strip():break
    if status['state'] in {'FAILED','INTERRUPTED','CANCELED'}:raise RuntimeError(status)
    time.sleep(3)
else:raise RuntimeError('actual renderer did not start')
checks=[]
for target in ('qwen38-flash-next','deepseek-v4-flash'):
    try:request('/api/v1/switches',{'target_model':target,'idempotency_key':'avatar-fixed-mutex-'+target+'-20260910'});raise AssertionError('busy switch accepted')
    except urllib.error.HTTPError as e:assert e.code==409;checks.append({'target':target,'status':e.code})
request('/api/v1/avatar-jobs/'+job+'/cancel',{})
for _ in range(100):
    status=request('/api/v1/avatar-jobs/'+job)['job']
    if status['state']=='CANCELED' and status['memory_released'] and not status['cleanup_required']:break
    if status.get('cleanup_required') and status['state']=='INTERRUPTED':raise RuntimeError(status)
    time.sleep(3)
else:raise RuntimeError('cancel cleanup did not finish')
result={'artifact_range':'PASS','idempotency':'PASS','cross_origin':'PASS','invalid_segment':'PASS','real_renderer_before_cancel':containers.stdout.strip().splitlines(),'mutual_exclusion':checks,'cancel':status,'simulation':False}
(out/'api-acceptance.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False),flush=True)
