"""Head Avatar job service; whole jobs use the same cluster GPU lease as LLMs."""
from datetime import datetime,timedelta,timezone
import hashlib,json,shlex,subprocess,time,uuid
from ..connection_view import _read_shared_api_key
from ..domain import ObservedModel
from ..ssh import FixedSSH
from .loader import load_module

class AvatarHTTPError(RuntimeError):
    def __init__(self,status):
        super().__init__('Avatar HTTP request rejected: '+str(status));self.status=status


MODEL='longcat-video-avatar-1.5'
UNIT='longcat-avatar15-gateway.service'


class LongCatAdapter:
    model_id=MODEL
    profiles=()
    default_profile=None
    def __init__(self,config,ssh=None):
        self.config=config;self.ssh=ssh or FixedSSH();self.head=next(x for x in config.nodes if x.role=='head')
    def head_request(self,path,body=None,*,binary=False,timeout=20):
        if not path.startswith(('/health','/jobs','/heartbeat','/cancel','/reconcile','/result/')):raise ValueError('invalid Avatar path')
        payload={'path':path,'body':body,'token':_read_shared_api_key(self.config.deepseek_gateway_secret)}
        code="""import json,sys,urllib.request,urllib.error
v=json.load(sys.stdin)
o=urllib.request.build_opener(urllib.request.ProxyHandler({}))
r=urllib.request.Request('http://127.0.0.1:18891'+v['path'],data=None if v['body'] is None else json.dumps(v['body']).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+v['token']})
try:
 with o.open(r,timeout=15) as response: sys.stdout.buffer.write(response.read(128*1024*1024+1))
except urllib.error.HTTPError as error:
 sys.stderr.write('HTTP_STATUS:'+str(error.code));sys.exit(42)
"""
        args=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8','-o','StrictHostKeyChecking=yes',f'{self.head.user}@{self.head.address}',shlex.join(['python3','-c',code])]
        result=subprocess.run(args,input=json.dumps(payload).encode(),capture_output=True,timeout=timeout)
        if result.returncode==42 and result.stderr.startswith(b'HTTP_STATUS:'):
            raise AvatarHTTPError(int(result.stderr.decode().split(':')[1]))
        if result.returncode:raise RuntimeError('Avatar loopback request failed')
        if len(result.stdout)>128*1024*1024:raise RuntimeError('Avatar response exceeds limit')
        return result.stdout if binary else json.loads(result.stdout)
    def _probe(self):
        script="\n".join(['set -eu','hostname','whoami',f'systemctl show {UNIT} --property=ActiveState --value',"ss -H -ltn 'sport = :18891'", "sudo -n docker ps --filter label=org.dgxspark.longcat.guard-id --format 'GPU {{.ID}}'"])
        result=self.ssh.run(self.head,['sh','-c',script],timeout=10)
        lines=result.stdout.splitlines()
        if result.returncode or lines[:2]!=[self.head.hostname,self.head.user]:raise RuntimeError('Avatar head identity unavailable')
        return {'unit':lines[2], 'listeners':[x for x in lines[3:] if not x.startswith('GPU ')], 'gpu':[x for x in lines[3:] if x.startswith('GPU ')]}
    def observe(self):
        try:
            p=self._probe()
            if p['unit'] not in {'active','activating','deactivating'} and not p['listeners'] and not p['gpu']:return ObservedModel(MODEL,'STOPPED')
            if p['unit']=='active' and len(p['listeners'])==1:
                h=self.head_request('/health')
                if h.get('ready') is True and h.get('model_id')==MODEL:return ObservedModel(MODEL,'HEALTHY')
            return ObservedModel(MODEL,'PARTIAL')
        except Exception:return ObservedModel(MODEL,'UNKNOWN')
    def _action(self,context,verb):
        context.guard();self.ssh.verify_role(self.head);context.guard()
        row={'schema':'longcat-avatar15-receipt-v1','verb':verb,'operation_id':context.operation_id,
             'lease_generation':context.lease_generation,'nonce':uuid.uuid4().hex,'not_after':(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()}
        if verb=='start':row['gateway_token']=_read_shared_api_key(self.config.deepseek_gateway_secret)
        module=load_module('longcat_receipt_runner',self.config.repo_root/'scripts/control-deepseek-v4-flash-vision-exp.py')
        args=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8','-o','StrictHostKeyChecking=yes',f'{self.head.user}@{self.head.address}',shlex.join(['sudo','-n','python3','/usr/local/libexec/longcat-avatar15-action.py',verb])]
        result=module.SubprocessRunner().run_supervised(args,input_text=json.dumps(row),timeout=180,lease_check=context.guard)
        if result.returncode:raise RuntimeError('Avatar '+verb+' action failed; inspect head unit')
        value=json.loads(result.stdout)
        if any(value.get(k)!=v for k,v in {'terminal':'succeeded','operation_id':context.operation_id,'lease_generation':context.lease_generation,'verb':verb}.items()):raise RuntimeError('Avatar terminal receipt mismatch')
        return value
    def start(self,context,profile=None):
        if profile is not None:raise ValueError('Avatar uses per-job segment selection')
        self._action(context,'start')
    def health(self,context):
        context.guard();h=self.head_request('/health')
        return {'healthy':self.observe().state=='HEALTHY','model_id':MODEL,'loading':'per_job','active_job':h.get('active_job')}
    def drain(self,context):
        context.guard()
        if self.observe().state=='STOPPED':return
        if self.head_request('/health').get('active_job'):raise BlockingIOError('Avatar generation owns the resource lease')
    def stop(self,context):self._action(context,'stop')
    def retire(self,context):self.stop(context)
    def stopped_proof(self,context):
        context.guard();p=self._probe()
        if p['unit'] in {'active','activating','deactivating'} or p['listeners'] or p['gpu']:raise RuntimeError('Avatar stopped proof unavailable')
        return {'model_id':MODEL,'stopped':True,'lease_generation':context.lease_generation,'sha256':hashlib.sha256(json.dumps(p,sort_keys=True).encode()).hexdigest()}
