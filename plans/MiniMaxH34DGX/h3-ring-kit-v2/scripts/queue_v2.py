#!/usr/bin/env python3
"""Minimal S0-only persistent serial queue. No automatic profile switching or POST retries.

All submitters must use this entrypoint; direct access to the backend bypasses this queue.
Known jobs are resumed by ID. Ambiguous submissions block dispatch until reconciled.
"""
from __future__ import annotations
import argparse,contextlib,fcntl,hashlib,json,os,sqlite3,subprocess,time,uuid
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request,urlopen
from request_v2 import TASKS,validate,asset_path
from assets_v2 import sha256


def connect(root: Path):
    root.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(root/'queue.sqlite3',timeout=30)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, profile TEXT NOT NULL, request TEXT NOT NULL,
      request_hash TEXT NOT NULL, inputs TEXT NOT NULL, status TEXT NOT NULL, backend_id TEXT, fingerprint TEXT,
      deployment_id TEXT, created REAL NOT NULL, updated REAL NOT NULL, output TEXT, error TEXT);
    CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,job TEXT,at REAL,kind TEXT,data TEXT);
    INSERT OR IGNORE INTO meta VALUES('paused','true');''')
    db.commit();return db


def event(db,job,kind,data=None):
    db.execute('INSERT INTO events(job,at,kind,data) VALUES(?,?,?,?)',(job,time.time(),kind,json.dumps(data,ensure_ascii=False)))


def getmeta(db,key):
    r=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
    return json.loads(r['value']) if r else None


def setmeta(db,key,value):
    db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(key,json.dumps(value)))


@contextlib.contextmanager
def worker_lock(root):
    with (root/'worker.lock').open('a') as f:
        try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('Another worker holds the cluster queue lease')
        yield


def api_json(url,payload=None):
    req=Request(url,data=None if payload is None else json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
    with urlopen(req,timeout=60) as r:body=json.load(r)
    if not isinstance(body,dict):raise RuntimeError('Backend did not return an object')
    return body


def media_check(path: Path,target: dict) -> dict:
    result=subprocess.run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)],capture_output=True,text=True,check=True)
    info=json.loads(result.stdout);v=[x for x in info['streams'] if x['codec_type']=='video'];a=[x for x in info['streams'] if x['codec_type']=='audio']
    if len(v)!=1 or len(a)!=1:raise RuntimeError('Expected one video and one audio stream')
    if v[0].get('codec_name')!='h264' or a[0].get('codec_name')!='aac' or a[0].get('channels')!=2 or int(a[0].get('sample_rate',0))!=32000:
        raise RuntimeError('Output codec/audio contract mismatch')
    from fractions import Fraction
    if Fraction(v[0].get('avg_frame_rate','0/1'))!=24:raise RuntimeError('Output is not 24 FPS')
    duration=float(info['format'].get('duration',0))
    if duration<=0 or abs(duration-float(target['duration_seconds']))>0.75:raise RuntimeError('Output duration outside baseline tolerance')
    # Aligned canvas may differ slightly. Record real dimensions, do not label 448 as 768p.
    actual_short=min(int(v[0]['width']),int(v[0]['height']))
    if abs(actual_short-int(target['short_edge']))>32:raise RuntimeError('Output resolution mismatch')
    if all('duration' in x for x in (v[0],a[0])) and abs(float(v[0]['duration'])-float(a[0]['duration']))>0.25:
        raise RuntimeError('Audio/video duration drift >0.25s')
    subprocess.run(['ffmpeg','-nostdin','-v','error','-xerror','-i',str(path),'-map','0:v:0','-map','0:a:0','-f','null','-'],check=True,capture_output=True)
    info['technical_decode_verified']=True;info['human_quality_review']='pending'
    return info


def state(db,jid,status,**values):
    allowed={'backend_id','fingerprint','deployment_id','output','error'}
    if set(values)-allowed:raise ValueError('Invalid state field')
    keys=['status','updated',*values]
    data=[status,time.time(),*values.values(),jid]
    db.execute('UPDATE jobs SET '+','.join(k+'=?' for k in keys)+' WHERE id=?',data)
    event(db,jid,status,values);db.commit()


def work(db,root,assets,base,budget):
    with worker_lock(root):
        orphan=db.execute("SELECT id FROM jobs WHERE status='submitting'").fetchall()
        for r in orphan:state(db,r['id'],'unknown',error='Previous worker stopped during POST; reconcile without resubmitting')
        if db.execute("SELECT 1 FROM jobs WHERE status='unknown'").fetchone():raise RuntimeError('Unknown submission blocks dispatch; reconcile backend state first')
        if db.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0]>1:raise RuntimeError('Multiple running jobs violate this serial queue contract')
        job=db.execute("SELECT * FROM jobs WHERE status='running' ORDER BY created LIMIT 1").fetchone()
        active=getmeta(db,'active')
        if job:
            if not active or job['fingerprint']!=active['fingerprint']:raise RuntimeError('Running job belongs to a different deployment')
        else:
            db.execute('BEGIN IMMEDIATE')
            if getmeta(db,'paused') or not active:
                db.rollback();print('Queue paused / no approved live deployment');return
            job=db.execute("SELECT * FROM jobs WHERE status='queued' AND profile=? ORDER BY created LIMIT 1",(active['profile_id'],)).fetchone()
            if not job:
                db.rollback();print('No queued job for active profile; other profiles remain queued');return
            payload=json.loads(job['request']);validate(payload,job['profile'],assets)
            expected=json.loads(job['inputs'])
            if input_manifest(payload,assets)!=expected:
                db.rollback();state(db,job['id'],'failed',error='Input bytes changed after enqueue; no POST sent');return
            db.execute("UPDATE jobs SET status='submitting',fingerprint=?,deployment_id=?,updated=? WHERE id=?",(active['fingerprint'],active['deployment_id'],time.time(),job['id']))
            event(db,job['id'],'submitting');db.commit()
            try:
                reply=api_json(base+'/v1/videos',payload)
                bid=reply.get('id')
                if not isinstance(bid,str) or not bid:raise RuntimeError('Missing backend id')
            except Exception as e:
                state(db,job['id'],'unknown',error=f'POST uncertain; no retry: {e}')
                raise RuntimeError('POST failed/ambiguous; do not submit a duplicate') from e
            state(db,job['id'],'running',backend_id=bid,error=None)
            job=db.execute('SELECT * FROM jobs WHERE id=?',(job['id'],)).fetchone()
        payload=json.loads(job['request']);url=base+'/v1/videos/'+quote(job['backend_id'],safe='');deadline=time.monotonic()+budget
        while time.monotonic()<deadline:
            try:reply=api_json(url)
            except Exception as e:
                state(db,job['id'],'running',error=f'Polling failed; keep same backend id: {e}');raise
            status=reply.get('status');event(db,job['id'],'backend_status',reply);db.commit()
            if status in ('failed','cancelled','canceled','expired'):
                state(db,job['id'],'failed',error=json.dumps(reply,ensure_ascii=False));return
            if status=='completed':
                out=root/'outputs'/(job['id']+'.mp4');out.parent.mkdir(exist_ok=True)
                try:
                    if not out.exists():
                        part=root/'outputs'/(job['id']+'.'+uuid.uuid4().hex+'.part')
                        with urlopen(url+'/content',timeout=600) as response,part.open('xb') as f:
                            ctype=response.headers.get('Content-Type','').lower()
                            if 'json' in ctype or 'text/' in ctype:raise RuntimeError('Not a video response')
                            while b:=response.read(1024*1024):f.write(b)
                            f.flush();os.fsync(f.fileno())
                        if not part.stat().st_size:raise RuntimeError('Empty content')
                        part.rename(out)
                    report=media_check(out,payload['target'])
                    (out.with_suffix('.ffprobe.json')).write_text(json.dumps(report,indent=2))
                except Exception as e:
                    # Generation is known finished. Do not regenerate because a GET or decode check failed.
                    state(db,job['id'],'failed',output=str(out) if out.exists() else None,error=f'Backend completed; artifact handling/check failed. Download by the same backend id if appropriate: {e}');return
                state(db,job['id'],'completed',output=str(out),error=None);print(str(out));return
            if status not in ('queued','pending','running','in_progress','processing'):
                state(db,job['id'],'unknown',error=f'Unknown backend status: {status!r}');raise RuntimeError('Unknown backend state')
            time.sleep(2)
        state(db,job['id'],'running',error='Client poll budget reached; backend may still be running; next worker resumes this ID')
        print('Polling paused; rerun worker to resume the existing backend id, not POST')


def input_manifest(payload, root):
    return {c['uri']:{'bytes':asset_path(c['uri'],root).stat().st_size,
                      'sha256':sha256(asset_path(c['uri'],root))} for c in payload['conditions']}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path('/srv/h3-ring/queue'))
    p.add_argument('--assets',type=Path,default=Path('/srv/h3-ring/assets'))
    p.add_argument('--base',default='http://127.0.0.1:30021')
    sub=p.add_subparsers(dest='action',required=True)
    sub.add_parser('init');sub.add_parser('pause');sub.add_parser('status');sub.add_parser('resume')
    q=sub.add_parser('activate');q.add_argument('--receipt',type=Path,required=True);q.add_argument('--live-verified',action='store_true')
    q=sub.add_parser('submit');q.add_argument('--profile',choices=TASKS,required=True);q.add_argument('--request',type=Path,required=True)
    q=sub.add_parser('worker');q.add_argument('--poll-budget',type=int,default=14400)
    q=sub.add_parser('attach');q.add_argument('--id',required=True);q.add_argument('--backend-id',required=True)
    q=sub.add_parser('mark-interrupted');q.add_argument('--id',required=True);q.add_argument('--all-ranks-stopped-confirmed',action='store_true')
    a=p.parse_args()
    if not a.base.startswith('http://127.0.0.1:'):raise ValueError('Keep backend access local in this version')
    db=connect(a.root)
    try:
        if a.action=='init':print('Queue initialized; paused by default')
        elif a.action=='pause':setmeta(db,'paused',True);event(db,None,'pause');db.commit();print('New dispatch paused; running job continues')
        elif a.action=='status':
            print(json.dumps({'paused':getmeta(db,'paused'),'active':getmeta(db,'active'),'jobs':[dict(x) for x in db.execute('SELECT id,profile,status,backend_id,fingerprint,output,error FROM jobs ORDER BY created')]},ensure_ascii=False,indent=2))
        elif a.action=='submit':
            payload=json.loads(a.request.read_text());validate(payload,a.profile,a.assets)
            body=json.dumps(payload,ensure_ascii=False,sort_keys=True);jid=uuid.uuid4().hex;now=time.time();inputs=json.dumps(input_manifest(payload,a.assets),sort_keys=True)
            db.execute('INSERT INTO jobs(id,profile,request,request_hash,inputs,status,created,updated) VALUES(?,?,?,?,?,?,?,?)',(jid,a.profile,body,hashlib.sha256(body.encode()).hexdigest(),inputs,'queued',now,now))
            event(db,jid,'queued');db.commit();print(jid)
        elif a.action=='activate':
            if not a.live_verified:raise RuntimeError('Inspect all four LIVE ranks and test API before acknowledging --live-verified')
            with worker_lock(a.root):
                if db.execute("SELECT 1 FROM jobs WHERE status IN ('running','submitting','unknown')").fetchone():raise RuntimeError('Drain/reconcile in-flight jobs before profile switch')
                if not getmeta(db,'paused'):raise RuntimeError('Pause before changing active deployment')
                receipt=json.loads(a.receipt.read_text())
                if receipt.get('status')!='PASS_OFFLINE_MATCH_ONLY' or receipt.get('world')!=4 or receipt.get('profile_id') not in TASKS:raise ValueError('Not a valid four-node comparison receipt')
                setmeta(db,'active',receipt);event(db,None,'operator_live_approval',receipt);db.commit();print('Deployment selected; still paused until resume')
        elif a.action=='resume':
            if not getmeta(db,'active'):raise RuntimeError('No approved deployment')
            if db.execute("SELECT 1 FROM jobs WHERE status IN ('unknown','submitting')").fetchone():raise RuntimeError('Reconcile uncertain jobs first')
            setmeta(db,'paused',False);event(db,None,'resume');db.commit();print('Dispatch enabled')
        elif a.action=='worker':
            if a.poll_budget<=0:raise ValueError('Budget must be positive')
            work(db,a.root,a.assets,a.base.rstrip('/'),a.poll_budget)
        elif a.action in ('attach','mark-interrupted'):
            with worker_lock(a.root):
                job=db.execute('SELECT * FROM jobs WHERE id=?',(a.id,)).fetchone()
                if not job or job['status'] not in ('unknown','submitting','running'):raise RuntimeError('Not an unresolved in-flight job')
                if a.action=='attach':state(db,a.id,'running',backend_id=a.backend_id,error='Operator attached verified existing backend job')
                else:
                    if not a.all_ranks_stopped_confirmed or not getmeta(db,'paused'):raise RuntimeError('Pause and confirm the entire old four-rank backend has stopped first')
                    state(db,a.id,'interrupted',error='Operator confirmed old backend stopped; no automatic requeue')
    finally:db.close()
if __name__=='__main__':main()
