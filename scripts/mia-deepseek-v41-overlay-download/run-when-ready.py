#!/usr/bin/env python3
"""Queue the overlay after the existing base and image downloads; never launch models."""
import fcntl,json,pathlib,subprocess,sys,time
ROOT=pathlib.Path(__file__).resolve().parents[2]
LOG=ROOT/'logs/mia-deepseek-v41-overlay-download'
LOG.mkdir(parents=True,exist_ok=True)
with (LOG/'waiting.lock').open('w') as lock:
 try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError: raise SystemExit('Overlay waiter already running')
 previous=None
 while True:
  r=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8','operator@192.0.2.30','hostname; whoami; cat /home/operator/dgx-models/mia-deepseek-v41-download/download-logs/image.status'],capture_output=True,timeout=30)
  lines=r.stdout.decode().splitlines()
  ready=r.returncode==0 and lines==['dgx-head','operator','PASS']
  state='READY' if ready else 'WAITING_BASE_AND_IMAGE'
  if state!=previous: print(time.strftime('%Y-%m-%dT%H:%M:%S%z'),state,flush=True);previous=state
  if ready: break
  time.sleep(60)
with (LOG/'controller.log').open('ab') as out:
 result=subprocess.run([sys.executable,str(ROOT/'scripts/mia-deepseek-v41-overlay-download/download-queue.py'),'download','--hf-auth','--limit','24M'],cwd=ROOT,stdout=out,stderr=subprocess.STDOUT)
print(time.strftime('%Y-%m-%dT%H:%M:%S%z'),'OVERLAY_CONTROLLER_EXIT',result.returncode,flush=True)
raise SystemExit(result.returncode)
