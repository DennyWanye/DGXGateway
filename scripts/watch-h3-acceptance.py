#!/usr/bin/env python3
"""Read-only observer for one already-submitted H3 browser request."""
import json, pathlib, re, subprocess, sys, time, urllib.request
from datetime import datetime, timezone
prompt_id, label = sys.argv[1:3]
root=pathlib.Path(__file__).resolve().parents[1]/'evidence/h3-timeout-memory-repair'
def get(path):
    with urllib.request.urlopen(path,timeout=15) as response:return json.load(response)
verified=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8','operator@192.0.2.30','hostname; whoami'],capture_output=True,text=True,check=True)
assert verified.stdout.splitlines()==['dgx-head','operator']
while True:
    row={'at':datetime.now(timezone.utc).isoformat(),'prompt_id':prompt_id}
    try:
        history=get('http://192.0.2.37:8188/history/'+prompt_id)
        snapshot=get('http://192.0.2.37:18082/api/v1/snapshot')
        row['model']=snapshot.get('active_local_model',{}).get('state')
        row['memory_available_gib']={n['role']:round(n.get('unified_memory',{}).get('available_bytes',0)/2**30,2) for n in snapshot.get('nodes',[])}
        result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8','operator@192.0.2.30','sudo -n docker logs --since 65s minimax-h3-2x-api'],capture_output=True,text=True,timeout=15)
        lines=re.sub(r'\x1b\[[0-9;]*m','',result.stdout+result.stderr).splitlines()
        row['progress']=[l.strip() for l in lines if re.search(r'\d+/19',l) or 'H3_MEMORY' in l][-5:]
        if prompt_id in history:
            (root/(label+'-history.json')).write_text(json.dumps(history,ensure_ascii=False,indent=2))
            row['terminal']=history[prompt_id]['status']['status_str']
            row['events']=[m[0] for m in history[prompt_id]['status']['messages']]
    except Exception as exc:row['observation_error']=type(exc).__name__
    with (root/(label+'-monitor.jsonl')).open('a') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')
    print(json.dumps(row,ensure_ascii=False),flush=True)
    if 'terminal' in row:break
    time.sleep(60)
