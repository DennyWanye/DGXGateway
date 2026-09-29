#!/usr/bin/env python3
"""Head-only service actions bound to the current Gateway operation receipt."""
from datetime import datetime,timedelta,timezone
import json,os
from pathlib import Path
import pwd,re,subprocess,sys,time,urllib.request

UNIT='longcat-avatar15-gateway.service'
ROOT=Path('/home/operator/dgx-models/longcat-avatar15/gateway')

def main():
    if os.geteuid()!=0 or subprocess.check_output(['hostname'],text=True).strip()!='dgx-head':raise RuntimeError('head/root identity required')
    verb=sys.argv[1]
    row=json.loads(sys.stdin.buffer.read(8193))
    if row.get('schema')!='longcat-avatar15-receipt-v1' or row.get('verb')!=verb or verb not in {'start','stop'}:raise ValueError('invalid receipt')
    for key in ['operation_id','lease_generation','nonce']:
        if not isinstance(row.get(key),str) or not re.fullmatch(r'[A-Za-z0-9._-]{3,128}',row[key]):raise ValueError('invalid receipt identity')
    expiry=datetime.fromisoformat(row['not_after']);now=datetime.now(timezone.utc)
    if expiry.tzinfo is None or not now<expiry<=now+timedelta(minutes=6):raise ValueError('receipt expiry rejected')
    nonces=Path('/var/lib/longcat-avatar15-gateway/receipts');nonces.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(nonces/row['nonce'],os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);os.close(fd)
    if verb=='start':
        token=row.get('gateway_token')
        if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43,128}',token):raise ValueError('invalid credential')
        ROOT.mkdir(parents=True,exist_ok=True,mode=0o700);user=pwd.getpwnam('operator');os.chown(ROOT,user.pw_uid,user.pw_gid)
        fd=os.open(ROOT/'api.token',os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
        try:os.fchmod(fd,0o600);os.fchown(fd,user.pw_uid,user.pw_gid);os.write(fd,(token+'\n').encode())
        finally:os.close(fd)
        subprocess.run(['systemctl','start',UNIT],check=True,timeout=45)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}));deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            try:
                req=urllib.request.Request('http://127.0.0.1:18891/health',headers={'Authorization':'Bearer '+token})
                with opener.open(req,timeout=3) as response:value=json.load(response)
                if value.get('ready') and value.get('model_id')=='longcat-video-avatar-1.5':break
            except OSError:pass
            time.sleep(.5)
        else:raise RuntimeError('Avatar service readiness failed')
    else:
        subprocess.run(['systemctl','stop',UNIT],check=True,timeout=130)
        ids=subprocess.check_output(['docker','ps','--filter','label=org.dgxspark.longcat.guard-id','--format','{{.ID}}'],text=True)
        if ids.strip():raise RuntimeError('Avatar GPU containers remain; recovery required')
    print(json.dumps({'terminal':'succeeded','operation_id':row['operation_id'],'lease_generation':row['lease_generation'],'verb':verb}))
if __name__=='__main__':main()
