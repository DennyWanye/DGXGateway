#!/usr/bin/env python3
"""Install inert Qwen-only unit/config files; does not start GPU processes."""
import hashlib
import json
from pathlib import Path
import shlex
import subprocess

ROOT=Path(__file__).resolve().parents[2]

def remote(address, args, data=None):
    r=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8','-o','StrictHostKeyChecking=yes','operator@'+address,shlex.join(args)],input=data,capture_output=True,timeout=60)
    if r.returncode: raise RuntimeError('install command failed: '+r.stderr.decode()[:400])
    return r.stdout.decode()

def main():
    results=[]
    for role,address,host in [('head','192.0.2.30','dgx-head'),('worker','192.0.2.31','dgx-worker')]:
        assert remote(address,['sh','-c','hostname; whoami']).split()==[host,'operator']
        prior=remote(address,['sh','-c','test ! -e /opt/qwen38-flash-next && test ! -e /usr/local/libexec/qwen38-runtime-action.py && test ! -e /etc/systemd/system/qwen38-flash-next.service'])
        # Refuse overwriting unknown installations. A retry needs explicit inspection.
        stage='/home/operator/dgx-models/qwen38-flash-next/control/runtime/gateway-install'
        remote(address,['mkdir','-p',stage])
        files={
          ROOT/'scripts/qwen38-flash-next/runtime-action.py':('/usr/local/libexec/qwen38-runtime-action.py','0755'),
          ROOT/'containers/qwen38-flash-next/launch_vllm.py':('/opt/qwen38-flash-next/launch_vllm.py','0644'),
          ROOT/f'containers/qwen38-flash-next/compose.{role}.json':(f'/opt/qwen38-flash-next/compose.{role}.json','0644'),
          ROOT/f'containers/qwen38-flash-next/rank.{role}.service':('/etc/systemd/system/qwen38-flash-next.service','0644'),
        }
        if role=='head':
            files[ROOT/'containers/qwen38-flash-next/gateway.conf']=('/opt/qwen38-flash-next/gateway.conf','0644')
            files[ROOT/'containers/qwen38-flash-next/gateway.service']=('/etc/systemd/system/qwen38-flash-next-gateway.service','0644')
        remote(address,['sudo','-n','install','-d','-m','0755','/opt/qwen38-flash-next'])
        remote(address,['sh','-c','cat > '+shlex.quote(stage+'/profile.json')], b'{"profile":"32K"}\n')
        remote(address,['sudo','-n','install','-m','0644',stage+'/profile.json','/opt/qwen38-flash-next/profile.json'])
        hashes={}
        for source,(dest,mode) in files.items():
            raw=source.read_bytes();temporary=stage+'/'+source.name
            remote(address,['sh','-c','umask 077; cat > '+shlex.quote(temporary)],raw)
            remote(address,['sudo','-n','install','-m',mode,temporary,dest])
            actual=remote(address,['sha256sum',dest]).split()[0]
            assert actual==hashlib.sha256(raw).hexdigest()
            hashes[dest]=actual
        remote(address,['sh','-c','printf %s '+shlex.quote(role)+' > '+shlex.quote(stage+'/role')])
        remote(address,['sudo','-n','install','-m','0644',stage+'/role','/opt/qwen38-flash-next/role'])
        remote(address,['sudo','-n','docker','compose','-p','qwen38-flash-next','-f',f'/opt/qwen38-flash-next/compose.{role}.json','config','--quiet'])
        remote(address,['sudo','-n','systemctl','daemon-reload'])
        state=remote(address,['systemctl','show','qwen38-flash-next.service','--property=LoadState,ActiveState,UnitFileState'])
        assert 'ActiveState=inactive' in state and 'LoadState=loaded' in state
        results.append({'role':role,'hashes':hashes,'unit':state,'gpu_started':False})
        print(json.dumps({'role':role,'installed':True,'gpu_started':False}),flush=True)
    (ROOT/'plans/qwen38-flash-next/evidence/deploy/gateway-runtime-install.json').write_text(json.dumps(results,indent=2)+'\n')
if __name__=='__main__':main()
