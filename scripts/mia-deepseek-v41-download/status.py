#!/usr/bin/env python3
"""Read-only, credential-free head download checkpoint. Run through sudo over SSH."""
import datetime,json,pathlib,subprocess
base=pathlib.Path('/home/operator/dgx-models/mia-deepseek-v41-download')
assert subprocess.check_output(['hostname'],text=True).strip()=='dgx-head'
plan=json.loads((base/'control/download-plan.json').read_text())
finished=[];missing=[]
for n,item in enumerate(plan['files'],1):
 p=base/'weights'/item['destination']
 if p.is_file() and not p.is_symlink() and p.stat().st_size==item['size']:
  finished.append(item)
 else: missing.append(item['destination'])
partials=[]
for p in sorted((base/'weights/.download').glob('*.part')):
 s=p.stat();partials.append(dict(file=p.name,apparent_bytes=s.st_size,allocated_bytes=s.st_blocks*512))
status={p.name:p.read_text().strip() for p in (base/'download-logs').glob('*.status')}
res=dict(time=datetime.datetime.now(datetime.timezone.utc).isoformat(),complete_files=len(finished),total_files=len(plan['files']),complete_bytes=sum(f['size'] for f in finished),total_bytes=plan['total_bytes'],partial=partials,status=status,missing_files=missing,free_bytes=__import__('shutil').disk_usage(base).free)
overlay_plan=base/'control/overlay-download-plan.json'
if overlay_plan.is_file():
 op=json.loads(overlay_plan.read_text()); ob=pathlib.Path('/home/operator/dgx-models/mia-deepseek-v41-overlay-download')
 of=[]
 for item in op['files']:
  t=ob/'weights'/item['destination']
  if t.is_file() and not t.is_symlink() and t.stat().st_size==item['size']: of.append(item)
 ost={p.name:p.read_text().strip() for p in (ob/'download-logs').glob('*.status')}
 parts=[]
 for p in (ob/'weights/.download').glob('*.part'):
  st=p.stat();parts.append(dict(file=p.name,apparent_bytes=st.st_size,allocated_bytes=st.st_blocks*512))
 res['overlay']=dict(complete_files=len(of),total_files=len(op['files']),complete_bytes=sum(f['size'] for f in of),total_bytes=op['total_bytes'],status=ost,partial=parts)
 res['all_models_total_files']=len(plan['files'])+len(op['files'])
 res['all_models_complete_files']=len(finished)+len(of)
 res['all_models_total_bytes']=plan['total_bytes']+op['total_bytes']
 res['all_models_complete_bytes']=res['complete_bytes']+res['overlay']['complete_bytes']
print(json.dumps(res,indent=2))
