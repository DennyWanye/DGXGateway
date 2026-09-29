#!/usr/bin/env python3
"""Loopback-only Avatar job service; fixed guarded runtime, expiring job lease."""
import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
import wave

MODEL='longcat-video-avatar-1.5'
ROOT=Path('/home/operator/dgx-models/longcat-avatar15')
STATE=ROOT/'gateway'
TOKEN=STATE/'api.token'
JOBS=STATE/'jobs'
IMAGE='sha256:a8b6aee9435f0a96b3b5aea7b09788e81ff3d675511eadefe3b8759ad6d3c1a1'
GUARD=ROOT/'control/runtime/run_guarded.py'
DURATIONS={1:3.72,5:16.52,19:61.32}
BUDGETS={1:1800,5:7200,19:14400}


def identifier(value):
    if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{32}',value):raise ValueError('invalid job id')
    return value


def validate_inputs(payload):
    if set(payload)!={'job_id','lease_generation','not_after','image_base64','audio_base64','prompt','num_segments'}:raise ValueError('invalid input fields')
    identifier(payload['job_id'])
    if not isinstance(payload['lease_generation'],str) or not re.fullmatch(r'[A-Za-z0-9._-]{3,128}',payload['lease_generation']):raise ValueError('invalid generation')
    if not time.time()<payload['not_after']<=time.time()+125:raise ValueError('invalid lease expiry')
    segments=payload['num_segments']
    if type(segments) is not int or segments not in DURATIONS:raise ValueError('invalid segments')
    if not isinstance(payload['prompt'],str) or not 1<=len(payload['prompt'])<=2000:raise ValueError('invalid prompt')
    image=base64.b64decode(payload['image_base64'],validate=True)
    audio=base64.b64decode(payload['audio_base64'],validate=True)
    if not 32<len(image)<=8*1024*1024 or not image.startswith(b'\x89PNG\r\n\x1a\n'):raise ValueError('PNG reference image required')
    if not 44<len(audio)<=4*1024*1024:raise ValueError('audio size invalid')
    with wave.open(io.BytesIO(audio),'rb') as stream:
        if (stream.getnchannels(),stream.getframerate(),stream.getsampwidth(),stream.getcomptype())!=(1,16000,2,'NONE'):raise ValueError('mono 16kHz PCM16 WAV required')
        duration=stream.getnframes()/16000
        if not DURATIONS[segments]<=duration<=120:raise ValueError('audio too short for selected segments or exceeds 120 seconds')
        if len(stream.readframes(stream.getnframes()))!=stream.getnframes()*2:raise ValueError('truncated PCM')
    return image,audio


def save(row):
    path=JOBS/row['job_id']/'state.json';temporary=path.with_suffix('.tmp')
    row['updated_at']=time.time();temporary.write_text(json.dumps(row,ensure_ascii=False,indent=2));temporary.replace(path)


class Jobs:
    def __init__(self):
        JOBS.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.lock=threading.RLock();self.active=None;self.process=None;self.cancel_event=threading.Event();self.threads=[]
        for path in JOBS.glob('*/state.json'):
            row=json.loads(path.read_text())
            if row['state'] not in {'SUCCEEDED','FAILED','CANCELED'} or not row.get('cleanup_complete'):
                row.update(state='INTERRUPTED',cleanup_complete=False);save(row);self.active=row['job_id']
    def get(self,job):
        return json.loads((JOBS/identifier(job)/'state.json').read_text())
    def submit(self,payload):
        image,audio=validate_inputs(payload);job=payload['job_id']
        digest=hashlib.sha256(json.dumps({k:v for k,v in payload.items() if k!='not_after'},sort_keys=True).encode()).hexdigest()
        with self.lock:
            if (JOBS/job).exists():
                row=self.get(job)
                if row['input_digest']!=digest:raise ValueError('job id reused with different input')
                return row
            if self.active:raise BlockingIOError('Avatar worker busy or requires recovery')
            folder=JOBS/job;folder.mkdir(mode=0o700);inputs=folder/'input';inputs.mkdir()
            (inputs/'man.png').write_bytes(image);(inputs/'speech.wav').write_bytes(audio)
            (inputs/'case.json').write_text(json.dumps({'prompt':payload['prompt'],'cond_image':'/workspace/input/man.png','cond_audio':{'person1':'/workspace/input/speech.wav'}},ensure_ascii=False))
            row={'job_id':job,'model_id':MODEL,'input_digest':digest,'lease_generation':payload['lease_generation'],
                 'lease_deadline':payload['not_after'],'num_segments':payload['num_segments'],'state':'QUEUED',
                 'created_at':time.time(),'cleanup_complete':False}
            save(row);self.active=job;self.cancel_event.clear()
            thread=threading.Thread(target=self.run,args=(job,),daemon=True);self.threads.append(thread);thread.start()
            return row
    def heartbeat(self,payload):
        with self.lock:
            row=self.get(payload['job_id'])
            if row['lease_generation']!=payload['lease_generation']:raise ValueError('lease generation mismatch')
            if not time.time()<payload['not_after']<=time.time()+125:raise ValueError('invalid renewal')
            if row['state'] in {'SUCCEEDED','FAILED','CANCELED'}:return row
            row['lease_deadline']=payload['not_after'];save(row);return row
    def cancel(self,job):
        with self.lock:
            row=self.get(job)
            if job==self.active:
                self.cancel_event.set();row['cancel_requested']=True;save(row)
            return row
    def reconcile(self,job):
        with self.lock:
            row=self.get(job)
            if self.process is not None and self.process.poll() is None:
                return self.cancel(job)
            # After a service interruption, inspect actual GPU state before
            # retiring a stale journal. Never stop an unidentified container.
            result=subprocess.run(['sudo','-n','docker','ps','--filter','label=org.dgxspark.longcat.guard-id','--format','{{.ID}}'],capture_output=True,text=True,timeout=15)
            if result.returncode or result.stdout.strip():raise BlockingIOError('Avatar containers need exact-owner recovery')
            process_probe=subprocess.run(['pgrep','-f',str(GUARD)],capture_output=True,text=True,timeout=10)
            if process_probe.returncode not in (0,1) or process_probe.stdout.strip():raise BlockingIOError('Avatar guard process remains')
            report=JOBS/job/'run/logs/run-report.json'
            proof=json.loads(report.read_text()) if report.is_file() else {}
            row.update(state='CANCELED' if row.get('cancel_requested') else 'FAILED',cleanup_complete=True,
                       recovered_stopped_proof=True,inputs_unchanged=proof.get('inputs_unchanged'))
            save(row)
            if self.active==job:self.active=None
            return row
    def run(self,job):
        folder=JOBS/job
        try:
            with self.lock:
                row=self.get(job);row['state']='RUNNING';save(row)
            with (folder/'runner.log').open('x') as log:
                args=['python3',str(GUARD),'--source-dir',str(ROOT/'src'),'--weights-dir',str(ROOT/'weights'),
                      '--input-dir',str(folder/'input'),'--run-dir',str(folder/'run'),
                      '--num-segments',str(row['num_segments']),'--timeout-seconds',str(BUDGETS[row['num_segments']])]
                with self.lock:
                    self.process=subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
                signaled=False;signal_at=0
                while self.process.poll() is None:
                    with self.lock:current=self.get(job)
                    if (self.cancel_event.is_set() or time.time()>current['lease_deadline']) and not signaled:
                        self.process.terminate();signaled=True;signal_at=time.monotonic()
                    if signaled and time.monotonic()-signal_at>90:
                        # Do not kill the guard mid-cleanup and claim success.
                        raise RuntimeError('Guard cleanup exceeded bound; retained recovery obligation')
                    time.sleep(2)
            report=json.loads((folder/'run/logs/run-report.json').read_text())
            with self.lock:
                row=self.get(job)
                row['cleanup_complete']=report.get('cleanup_complete') is True
                row['inputs_unchanged']=report.get('inputs_unchanged') is True
                row['elapsed_seconds']=report.get('elapsed_seconds')
                row['runner_status']=report.get('status')
                row['state']='CANCELED' if row.get('cancel_requested') else ('SUCCEEDED' if report.get('status')=='PROCESS_COMPLETED_MEDIA_NOT_VERIFIED' else 'FAILED')
                if row['state']=='SUCCEEDED':
                    name='ai2v_demo_1.mp4' if row['num_segments']==1 else f'video_continue_{row["num_segments"]}.mp4'
                    output=folder/'run/output'/name
                    if output.is_symlink() or not output.is_file() or not 1<=output.stat().st_size<=128*1024*1024:raise RuntimeError('fixed video output missing or too large')
                    row['output']={'name':name,'size_bytes':output.stat().st_size,'sha256':hashlib.sha256(output.read_bytes()).hexdigest()}
                save(row)
        except BaseException as error:
            with self.lock:
                row=self.get(job);row.update(state='FAILED',error=type(error).__name__);save(row)
        finally:
            with self.lock:
                if self.get(job).get('cleanup_complete'):self.active=None;self.process=None
    def shutdown(self):
        if self.active:self.cancel(self.active)
        for thread in self.threads:thread.join(timeout=100)


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def result(self,status,value):
        data=json.dumps(value,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def authorized(self):
        return hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+TOKEN.read_text().strip())
    def do_GET(self):
        if not self.authorized():self.result(401,{'error':'unauthorized'});return
        try:
            if self.path=='/health':self.result(200,{'model_id':MODEL,'ready':True,'active_job':self.server.jobs.active,'runtime_image':IMAGE,'loading':'per_job'});return
            if self.path.startswith('/jobs/'):
                job=self.path.removeprefix('/jobs/');self.result(200,self.server.jobs.get(job));return
            if self.path.startswith('/result/'):
                job=identifier(self.path.removeprefix('/result/'));row=self.server.jobs.get(job)
                if row['state']!='SUCCEEDED' or not row.get('cleanup_complete'):raise ValueError('result unavailable')
                path=JOBS/job/'run/output'/row['output']['name']
                if path.is_symlink() or path.stat().st_size!=row['output']['size_bytes']:raise ValueError('output changed')
                data=path.read_bytes()
                self.send_response(200);self.send_header('Content-Type','video/mp4');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
            self.result(404,{'error':'not_found'})
        except (ValueError,FileNotFoundError):self.result(404,{'error':'not_found'})
    def do_POST(self):
        if not self.authorized():self.result(401,{'error':'unauthorized'});return
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 1<=size<=18*1024*1024:raise ValueError('request size invalid')
            body=json.loads(self.rfile.read(size))
            if self.path=='/jobs':row=self.server.jobs.submit(body)
            elif self.path=='/heartbeat':row=self.server.jobs.heartbeat(body)
            elif self.path=='/cancel':row=self.server.jobs.cancel(body['job_id'])
            elif self.path=='/reconcile':row=self.server.jobs.reconcile(body['job_id'])
            else:self.result(404,{'error':'not_found'});return
            self.result(200,row)
        except BlockingIOError:self.result(409,{'error':'busy'})
        except (ValueError,KeyError,TypeError,wave.Error):self.result(422,{'error':'invalid_request'})


def main():
    if not TOKEN.is_file() or TOKEN.stat().st_mode&0o077:raise RuntimeError('private API credential missing')
    for path in [GUARD,ROOT/'src/run_avatar15_clean_speech.py',ROOT/'weights/LongCat-Video-Avatar-1.5/lora/dmd_lora.safetensors']:
        if not path.is_file():raise RuntimeError('fixed runtime artifact absent')
    result=subprocess.run(['sudo','-n','docker','image','inspect',IMAGE,'--format','{{.Id}}'],capture_output=True,text=True,timeout=15)
    if result.returncode or result.stdout.strip()!=IMAGE:raise RuntimeError('fixed runtime image absent')
    server=ThreadingHTTPServer(('127.0.0.1',18891),Handler);server.jobs=Jobs()
    quit_event=threading.Event()
    for sig in [signal.SIGTERM,signal.SIGINT]:signal.signal(sig,lambda *_:quit_event.set())
    threading.Thread(target=server.serve_forever,daemon=True).start()
    quit_event.wait();server.jobs.shutdown();server.shutdown();server.server_close()

if __name__=='__main__':main()
