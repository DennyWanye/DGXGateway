#!/usr/bin/env python3
"""Real Qwen API acceptance. No secret or request Authorization is persisted."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import sys
import time
import urllib.error
import urllib.request
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from DGXGateway.dgx_gateway.config import load_config
from DGXGateway.dgx_gateway.connection_view import _read_shared_api_key

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'plans/qwen38-flash-next/evidence/deploy/gateway-api-acceptance'
BASE='http://192.0.2.30:11434'
HTTP=urllib.request.build_opener(urllib.request.ProxyHandler({}))
MODEL='qwen38-flash-next'


def main():
    OUT.mkdir(exist_ok=False)
    config=load_config(Path.home()/'.config/dgx-cluster-gateway/gateway.toml')
    token=_read_shared_api_key(config.deepseek_gateway_secret)
    def call(method,path,payload=None,credential=token,timeout=90):
        headers={'Content-Type':'application/json'}
        if credential is not None:headers['Authorization']='Bearer '+credential
        req=urllib.request.Request(BASE+path,method=method,headers=headers,data=None if payload is None else json.dumps(payload,ensure_ascii=False).encode())
        start=time.monotonic()
        try:
            with HTTP.open(req,timeout=timeout) as r:status,raw=r.status,r.read(4*1024*1024)
        except urllib.error.HTTPError as e:status,raw=e.code,e.read(1024*1024)
        try:body=json.loads(raw)
        except ValueError:body={'non_json_body':True}
        return {'status':status,'body':body,'elapsed_seconds':time.monotonic()-start}
    deadline=time.monotonic()+1500
    while time.monotonic()<deadline:
        try:
            row=call('GET','/v1/models',timeout=10)
            if row['status']==200 and {m['id'] for m in row['body']['data']}=={MODEL}:break
        except (OSError,ValueError,KeyError):pass
        time.sleep(5)
    else:raise RuntimeError('Qwen Gateway not ready in 25 minutes')
    (OUT/'models.json').write_text(json.dumps(row,indent=2)+'\n')
    assert row['body']['data'][0]['max_model_len']==32768
    matrix=[]
    allowed=[('GET','/v1/models'),('POST','/v1/chat/completions'),('POST','/v1/completions')]
    denied=[('GET','/health'),('GET','/metrics'),('POST','/tokenize'),('POST','/detokenize'),('POST','/v1/responses'),('POST','/score'),('POST','/v1/embeddings'),('POST','/v1/models'),('GET','/v1/chat/completions')]
    for method,path in allowed+denied:
        for name,credential in [('missing',None),('wrong','invalid-acceptance-token')]:
            r=call(method,path,{} if method=='POST' else None,credential)
            matrix.append({'method':method,'path':path,'credential':name,'status':r['status'],'pass':r['status']==401})
        if (method,path) in denied:
            r=call(method,path,{} if method=='POST' else None)
            matrix.append({'method':method,'path':path,'credential':'correct','status':r['status'],'pass':r['status']==404})
    (OUT/'auth-route-matrix.json').write_text(json.dumps(matrix,indent=2)+'\n')
    assert all(x['pass'] for x in matrix)
    print('AUTH_ALLOWLIST_MATRIX_PASS',flush=True)
    try:
        connection=socket.create_connection(('192.0.2.30',18890),timeout=4)
    except OSError:direct={'direct_upstream_rejected':True}
    else:
        connection.close();direct={'direct_upstream_rejected':False}
    (OUT/'direct-upstream.json').write_text(json.dumps(direct))
    assert direct['direct_upstream_rejected']
    fixture=json.loads((ROOT/'plans/qwen38-flash-next/evidence/deploy/functional-results/long-input-candidate.json').read_text())
    row=call('POST','/v1/chat/completions',{'model':MODEL,'messages':fixture['messages'],'temperature':0,'max_tokens':256},timeout=600)
    (OUT/'long-result.json').write_text(json.dumps(row,ensure_ascii=False,indent=2)+'\n')
    assert row['status']==200
    body=row['body'];choice=body['choices'][0]
    assert 22000<=body['usage']['prompt_tokens']<=24000
    assert fixture['expected'] in choice['message']['content'] and choice['finish_reason']=='stop'
    print(json.dumps({'LONG_INPUT_PASS':True,'usage':body['usage'],'elapsed_seconds':row['elapsed_seconds']},ensure_ascii=False),flush=True)
    def short(index):
        return call('POST','/v1/chat/completions',{'model':MODEL,'messages':[{'role':'user','content':f'{17+index}乘以19等于多少？只输出数字。'}],'temperature':0,'max_tokens':256})
    with ThreadPoolExecutor(max_workers=2) as pool:rows=list(pool.map(short,[0,1]))
    (OUT/'concurrent-two.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
    assert all(r['status']==200 and r['body']['choices'][0]['finish_reason']=='stop' and str((17+i)*19)==r['body']['choices'][0]['message']['content'].strip() for i,r in enumerate(rows))
    print('TWO_CONCURRENT_REQUESTS_PASS',flush=True)
    payload={'model':MODEL,'messages':[{'role':'user','content':'17乘以19等于多少？只输出数字。'}],'max_tokens':256,'temperature':0,'stream':True,'stream_options':{'include_usage':True}}
    req=urllib.request.Request(BASE+'/v1/chat/completions',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+token})
    events=[];done=False
    with HTTP.open(req,timeout=90) as response:
        for line in response:
            if not line.startswith(b'data: '):continue
            data=line[6:].strip()
            if data==b'[DONE]':done=True;break
            events.append(json.loads(data))
    (OUT/'stream.json').write_text(json.dumps({'events':events,'done':done},ensure_ascii=False,indent=2)+'\n')
    content=''.join(c.get('delta',{}).get('content') or '' for e in events for c in e.get('choices',[]))
    assert done and content.strip()=='323' and any(e.get('usage') for e in events)
    row=call('POST','/v1/completions',{'model':MODEL,'prompt':'1, 2, 3,','temperature':0,'max_tokens':8})
    (OUT/'completion-route.json').write_text(json.dumps(row,ensure_ascii=False,indent=2)+'\n')
    assert row['status']==200 and row['body'].get('choices')
    print('GATEWAY_REAL_API_ACCEPTANCE_PASS',flush=True)

if __name__=='__main__':main()
