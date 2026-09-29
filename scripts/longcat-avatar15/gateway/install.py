#!/usr/bin/env python3
"""Install the inert head-only Avatar service. No model is started here."""
import hashlib,json,shlex,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]

def remote(args,data=None):
    result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8','-o','StrictHostKeyChecking=yes','operator@192.0.2.30',shlex.join(args)],input=data,capture_output=True,timeout=45)
    if result.returncode:raise RuntimeError('Avatar install command failed: '+result.stderr.decode()[:300])
    return result.stdout.decode()

def main():
    assert remote(['sh','-c','hostname; whoami']).split()==['dgx-head','operator']
    remote(['sh','-c','test ! -e /opt/longcat-avatar15-gateway && test ! -e /usr/local/libexec/longcat-avatar15-action.py && test ! -e /etc/systemd/system/longcat-avatar15-gateway.service'])
    stage='/home/operator/dgx-models/longcat-avatar15/control/gateway-install';remote(['mkdir','-p',stage])
    remote(['sudo','-n','install','-d','-m','0755','/opt/longcat-avatar15-gateway'])
    files={'worker.py':('/opt/longcat-avatar15-gateway/worker.py','0644'),'action.py':('/usr/local/libexec/longcat-avatar15-action.py','0755'),'longcat-avatar15-gateway.service':('/etc/systemd/system/longcat-avatar15-gateway.service','0644')}
    hashes={}
    for name,(target,mode) in files.items():
        source=ROOT/'scripts/longcat-avatar15/gateway'/name;data=source.read_bytes()
        remote(['sh','-c','umask 077; cat > '+shlex.quote(stage+'/'+name)],data)
        remote(['sudo','-n','install','-m',mode,stage+'/'+name,target])
        actual=remote(['sha256sum',target]).split()[0];assert actual==hashlib.sha256(data).hexdigest();hashes[target]=actual
    remote(['sudo','-n','systemctl','daemon-reload'])
    state=remote(['systemctl','show','longcat-avatar15-gateway.service','--property=LoadState,ActiveState,UnitFileState'])
    assert 'LoadState=loaded' in state and 'ActiveState=inactive' in state
    out=ROOT/'plans/longcat-avatar15/evidence/gateway';out.mkdir(parents=True,exist_ok=True)
    (out/'install.json').write_text(json.dumps({'hashes':hashes,'unit':state,'gpu_started':False,'worker_changed':False},indent=2)+'\n')
    print('Avatar head service installed, inactive; no GPU model started')
if __name__=='__main__':main()
