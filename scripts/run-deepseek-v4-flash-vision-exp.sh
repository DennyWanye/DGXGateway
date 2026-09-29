#!/usr/bin/env bash
set -euo pipefail

readonly PROJECT='deepseek-v4-flash-vision-exp'
readonly UNIT='deepseek-v4-flash-vision-exp.service'
readonly GATEWAY_UNIT='deepseek-v4-flash-vision-exp-gateway.service'
readonly RUN_ROOT='/run/deepseek-v4-flash-vision-exp'
readonly INSTALL_ROOT='/opt/deepseek-v4-flash-vision-exp'
readonly ENV_FILE='/etc/deepseek-v4-flash-vision-exp.env'
readonly AUTH_TOKEN="${DGX_MODEL_API_KEY:?Set DGX_MODEL_API_KEY locally}"
readonly AUTH_INCLUDE='/run/secrets/deepseek-v4-flash-vision-exp-auth.conf'
readonly NONCE_ROOT='/var/lib/deepseek-v4-flash-vision-exp/receipts'
readonly GATEWAY_IMAGE='docker.io/library/nginx@sha256:77d740efa8f9c4753f2a7212d8422b8c77411681971f400ea03d07fe38476cac'

local_start() {
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  [[ "${VISION_ROLE:-}" == head || "${VISION_ROLE:-}" == worker ]]
  [[ "$VISION_PROJECT" == "$PROJECT" ]]
  [[ "$VISION_IMAGE" =~ ^sha256:[0-9a-f]{64}$ ]]
  [[ "$VISION_MODEL_ROOT" == /home/operator/dgx-models/deepseek-v4-flash-vision-exp-ablit ]]
  install -d -m 0700 "/home/operator/.local/state/$PROJECT/native-trace"
  export VISION_MAX_MODEL_LEN
  VISION_MAX_MODEL_LEN="$(cat "$RUN_ROOT/context-tokens")"
  [[ "$VISION_MAX_MODEL_LEN" =~ ^(32768|65536|131072|262144|524288)$ ]]
  export DSV4_VISION_TRACE_NONCE
  DSV4_VISION_TRACE_NONCE="$(cat "$RUN_ROOT/trace-nonce")"
  [[ "$DSV4_VISION_TRACE_NONCE" =~ ^[0-9a-f]{64}$ ]]
  exec docker compose --project-name "$PROJECT" --env-file "$ENV_FILE" \
    --file "$INSTALL_ROOT/docker-compose.${VISION_ROLE}.yml" up --detach --no-build
}

local_stop() {
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  export DSV4_VISION_TRACE_NONCE='stop-only-placeholder'
  exec docker compose --project-name "$PROJECT" --env-file "$ENV_FILE" \
    --file "$INSTALL_ROOT/docker-compose.${VISION_ROLE}.yml" down --timeout 30 --remove-orphans
}

if [[ "${1:-}" == '--local-systemd-start' ]]; then local_start; fi
if [[ "${1:-}" == '--local-systemd-stop' ]]; then local_stop; fi
[[ "${1:-}" == '--receipt-stdin' ]] || { echo 'receipt mode required' >&2; exit 64; }
[[ "${2:-}" == '--role' && "${4:-}" == '--verb' ]] || { echo 'role/verb required' >&2; exit 64; }
role="${3:-}"
verb="${5:-}"
[[ "$role" == head || "$role" == worker ]]
# shellcheck disable=SC1090
source "$ENV_FILE"
[[ "${VISION_ROLE:-}" == "$role" ]]
[[ "$VISION_PROJECT" == "$PROJECT" ]]
[[ "$VISION_IMAGE" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$VISION_MODEL_ROOT" == /home/operator/dgx-models/deepseek-v4-flash-vision-exp-ablit ]]

payload_file="$(mktemp -t deepseek-v4-vision-receipt.XXXXXX)"
trap 'shred -u "$payload_file" 2>/dev/null || true' EXIT
chmod 0600 "$payload_file"
dd of="$payload_file" status=none

validated="$(sudo -n python3 - "$payload_file" "$role" "$verb" "$NONCE_ROOT" <<'PY'
import datetime, json, os, pathlib, re, sys
path, role, verb, nonce_root = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3], pathlib.Path(sys.argv[4])
value=json.loads(path.read_text())
allowed={
 ('worker','start'),('head','start'),('head','stop'),('worker','stop'),
 ('head','staging-activate-gateway'),('head','production-activate-gateway'),
 ('head','production-deactivate-gateway'),
 ('head','acceptance-rank-proof'),('worker','acceptance-rank-proof'),('head','fail-close-head')}
required={'schema','project','role','verb','operation_id','lease_generation','nonce','not_after','trace_nonce'}
if not required.issubset(value) or (role,verb) not in allowed: raise SystemExit('receipt schema/action rejected')
if value['schema']!='deepseek-v4-flash-vision-receipt-v1' or value['project']!='deepseek-v4-flash-vision-exp': raise SystemExit('receipt identity rejected')
if value['role']!=role or value['verb']!=verb: raise SystemExit('receipt binding rejected')
for key in ('operation_id','lease_generation','nonce'):
 if not isinstance(value[key],str) or not re.fullmatch(r'[A-Za-z0-9._-]{3,128}',value[key]): raise SystemExit('receipt identifier rejected')
if not isinstance(value['trace_nonce'],str) or not re.fullmatch(r'[0-9a-f]{64}',value['trace_nonce']): raise SystemExit('trace nonce rejected')
expected_trace=__import__('hashlib').sha256(value['operation_id'].encode()).hexdigest()
if value['trace_nonce'] != expected_trace: raise SystemExit('trace nonce is not operation-bound')
expiry=datetime.datetime.fromisoformat(value['not_after'].replace('Z','+00:00'))
now=datetime.datetime.now(datetime.timezone.utc)
if expiry <= now or expiry > now+datetime.timedelta(minutes=6): raise SystemExit('receipt expiry rejected')
nonce_root.mkdir(parents=True,exist_ok=True,mode=0o700)
nonce=nonce_root/value['nonce']
fd=os.open(nonce,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_CLOEXEC,0o600); os.close(fd)
context_tokens=value.get('max_model_len',32768)
if type(context_tokens) is not int or context_tokens not in (32768,65536,131072,262144,524288): raise SystemExit('context length rejected')
if 'max_model_len' in value and verb != 'start': raise SystemExit('context on invalid action')
token=value.get('gateway_token')
if token is not None and (role,verb) not in {('head','staging-activate-gateway'),('head','production-activate-gateway'),('head','fail-close-head')}: raise SystemExit('secret on invalid action')
if token is not None and (not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43,128}',token)): raise SystemExit('invalid gateway token')
if token is not None:
 secret_root=pathlib.Path('/run/secrets'); secret_root.mkdir(parents=True,exist_ok=True,mode=0o700)
 outputs={
  secret_root/'deepseek-v4-flash-vision-exp.token': token+'\n',
  secret_root/'deepseek-v4-flash-vision-exp-auth.conf': f'map $http_authorization $deepseek_vision_auth_ok {{ default 0; "Bearer {token}" 1; }}\n',
 }
 if verb=='fail-close-head':
  run_root=pathlib.Path('/run/deepseek-v4-flash-vision-exp'); run_root.mkdir(parents=True,exist_ok=True,mode=0o711)
  outputs[run_root/'recovery-id.conf']=f'map "" $deepseek_vision_recovery_id {{ default "{value["operation_id"]}"; }}\n'
 for destination,content in outputs.items():
  flags=os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_CLOEXEC
  if hasattr(os,'O_NOFOLLOW'): flags|=os.O_NOFOLLOW
  fd=os.open(destination,flags,0o600)
  try: os.write(fd,content.encode()); os.fsync(fd)
  finally: os.close(fd)
request_hash=value.get('request_body_sha256')
if request_hash is not None and ((role,verb) not in {('head','acceptance-rank-proof'),('worker','acceptance-rank-proof')} or not re.fullmatch(r'[0-9a-f]{64}',request_hash)): raise SystemExit('request hash rejected')
control_hash=value.get('control_body_sha256')
if control_hash is not None and ((role,verb) not in {('head','acceptance-rank-proof'),('worker','acceptance-rank-proof')} or not re.fullmatch(r'[0-9a-f]{64}',control_hash)): raise SystemExit('control hash rejected')
if (request_hash is None) != (control_hash is None): raise SystemExit('paired request hashes required')
print(json.dumps({'max_model_len':context_tokens,'operation_id':value['operation_id'],'trace_nonce':value['trace_nonce'],'request_body_sha256':request_hash,'control_body_sha256':control_hash}))
PY
)"
context_tokens="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["max_model_len"])' <<<"$validated")"
operation_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["operation_id"])' <<<"$validated")"
trace_nonce="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["trace_nonce"])' <<<"$validated")"
request_body_sha256="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("request_body_sha256") or "")' <<<"$validated")"
control_body_sha256="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("control_body_sha256") or "")' <<<"$validated")"

container_count() {
  sudo -n docker ps \
    --filter "label=com.docker.compose.project=$PROJECT" \
    --filter 'label=com.docker.compose.service=vllm-vision' \
    --filter "label=com.dgx-spark.model-id=$PROJECT" \
    --filter "label=com.dgx-spark.role=$role" --format '{{.ID}}' | awk 'NF{n++} END{print n+0}'
}

container_id() {
  sudo -n docker ps \
    --filter "label=com.docker.compose.project=$PROJECT" \
    --filter 'label=com.docker.compose.service=vllm-vision' \
    --filter "label=com.dgx-spark.model-id=$PROJECT" \
    --filter "label=com.dgx-spark.role=$role" --format '{{.ID}}'
}

verify_rank_identity() {
  local container inspect_json guard_hash
  container="$(container_id)"
  [[ -n "$container" && "$container" != *$'\n'* ]]
  guard_hash="$(sudo -n docker exec "$container" sha256sum /opt/deepseek-v4-flash-vision-exp/vision_media_guard.py | awk '{print $1}')"
  [[ "$guard_hash" == "ec0740e6194040aa9d860818fe57201529fb13367299182c1cc5d2edd435fca0" ]] || { echo 'media guard content mismatch' >&2; exit 1; }
  inspect_json="$(sudo -n docker inspect "$container")"
  python3 - "$role" "$VISION_IMAGE" "$VISION_MODEL_ROOT" 3<<<"$inspect_json" <<'PY'
import json,os,sys
role,image,model_root=sys.argv[1:]
values=json.load(os.fdopen(3))
if not isinstance(values,list) or len(values)!=1: raise SystemExit('rank inspect cardinality mismatch')
item=values[0]
if item.get('Image')!=image: raise SystemExit('rank image ID mismatch')
config=item.get('Config',{})
labels=config.get('Labels') or {}
expected={
 'com.docker.compose.project':'deepseek-v4-flash-vision-exp',
 'com.docker.compose.service':'vllm-vision',
 'com.dgx-spark.model-id':'deepseek-v4-flash-vision-exp',
 'com.dgx-spark.role':role,
 'com.dgx-spark.guard-sha256':'ec0740e6194040aa9d860818fe57201529fb13367299182c1cc5d2edd435fca0',
}
if any(labels.get(k)!=v for k,v in expected.items()): raise SystemExit('rank labels mismatch')
guard_mounts=[m for m in item.get('Mounts',[]) if m.get('Destination')=='/opt/deepseek-v4-flash-vision-exp/vision_media_guard.py']
if len(guard_mounts)!=1 or guard_mounts[0].get('Source')!='/opt/deepseek-v4-flash-vision-exp/overlays/vision_media_guard.ec0740e6194040aa9d860818fe57201529fb13367299182c1cc5d2edd435fca0.py' or guard_mounts[0].get('RW') is not False:
 raise SystemExit('pinned media guard mount mismatch')
if config.get('Entrypoint')!=['python3','/opt/deepseek-v4-flash-vision-exp/launch_vllm.py']:
 raise SystemExit('rank launcher mismatch')
command=config.get('Cmd') or []
def option(name):
 positions=[index for index,value in enumerate(command) if value==name]
 if len(positions)!=1 or positions[0]+1>=len(command): raise SystemExit(f'{name} cardinality mismatch')
 return command[positions[0]+1]
required_options={
 '--distributed-executor-backend':'mp','--nnodes':'2',
 '--node-rank':'0' if role=='head' else '1','--master-addr':'192.0.2.33',
 '--master-port':'25001','--tensor-parallel-size':'2','--pipeline-parallel-size':'1',
 '--gpu-memory-utilization':'0.85',
 '--tool-call-parser':'deepseek_v4','--reasoning-parser':'deepseek_v4',
}
if any(option(key)!=value for key,value in required_options.items()): raise SystemExit('rank MP topology mismatch')
if option('--max-model-len') not in ('32768','65536','131072','262144','524288'): raise SystemExit('rank context rejected')
if command.count('--enable-auto-tool-choice') != 1: raise SystemExit('automatic tool choice must be enabled')
if command.count('--headless') != (1 if role=='worker' else 0): raise SystemExit('rank headless role mismatch')
environment=dict(value.split('=',1) for value in config.get('Env') or [] if '=' in value)
expected_ip='192.0.2.33' if role=='head' else '192.0.2.34'
if environment.get('VISION_ROLE')!=role or environment.get('VLLM_HOST_IP')!=expected_ip:
 raise SystemExit('rank fabric identity environment mismatch')
host=item.get('HostConfig',{})
if host.get('NetworkMode')!='host' or host.get('IpcMode')!='host': raise SystemExit('rank host topology mismatch')
if (host.get('RestartPolicy') or {}).get('Name') not in {'','no'}: raise SystemExit('rank restart policy mismatch')
devices={(d.get('PathOnHost'),d.get('PathInContainer'),d.get('CgroupPermissions')) for d in host.get('Devices') or []}
if ('/dev/infiniband','/dev/infiniband','rwm') not in devices: raise SystemExit('rank RoCE device mismatch')
gpu_requests=host.get('DeviceRequests') or []
if not any(request.get('Count')==-1 and ['gpu'] in (request.get('Capabilities') or []) for request in gpu_requests): raise SystemExit('rank GPU request mismatch')
mounts={(m.get('Source'),m.get('Destination'),m.get('RW')) for m in item.get('Mounts') or []}
if (model_root,'/models/deepseek-v4-flash-vision-exp',False) not in mounts: raise SystemExit('rank model mount mismatch')
PY
}

verify_worker_stable() {
  local before after state restarts
  before="$(container_id)"
  [[ -n "$before" && "$before" != *$'\n'* ]]
  sleep 15
  after="$(container_id)"
  [[ "$after" == "$before" ]]
  read -r state restarts < <(sudo -n docker inspect --format '{{.State.Running}} {{.RestartCount}}' "$after")
  [[ "$state" == true && "$restarts" == 0 ]]
  if sudo -n docker logs "$after" 2>&1 | tail -n 400 | grep -Eiq \
      '(Traceback|NCCL.*(WARN|ERROR|failed)|CUDA error|Segmentation fault|Address already in use)'; then
    echo 'worker rank emitted a fatal startup signature' >&2
    exit 1
  fi
}

verify_gateway() {
  sudo -n python3 - "$1" "$2" <<'PY'
import json,pathlib,sys,time,urllib.error,urllib.request
mode,operation_id=sys.argv[1:]
token=pathlib.Path('/run/secrets/deepseek-v4-flash-vision-exp.token').read_text().strip()
if mode=='staging': url='http://127.0.0.1:11435/v1/models'
elif mode=='production': url='http://192.0.2.30:11434/v1/models'
else: url='http://192.0.2.30:11434/health'
def fetch(auth):
 headers={'Authorization':f'Bearer {token}'} if auth else {}
 try:
  with urllib.request.urlopen(urllib.request.Request(url,headers=headers),timeout=8) as response:
   return response.status,response.read(1048577)
 except urllib.error.HTTPError as error:
  return error.code,error.read(1048577)
deadline=time.monotonic()+30
last_error='gateway did not become ready'
while time.monotonic()<deadline:
 try:
  missing,_=fetch(False)
  authorized,body=fetch(True)
  if missing!=401:
   last_error='gateway unauthenticated proof failed'
  elif mode in {'staging','production'}:
   value=json.loads(body)
   ids={item.get('id') for item in value.get('data',[]) if isinstance(item,dict)}
   if authorized==200 and ids=={'deepseek-v4-flash-vision-exp'}: break
   last_error='production identity proof failed'
  else:
   value=json.loads(body)
   if authorized==503 and value=={'error':'maintenance','recovery_id':operation_id}: break
   last_error='maintenance proof failed'
 except (OSError,ValueError,urllib.error.URLError) as error:
  last_error=f'gateway readiness error: {error}'
 time.sleep(0.5)
else:
 raise SystemExit(last_error)
PY
}

verify_nginx_config() {
  sudo -n docker image inspect "$GATEWAY_IMAGE" >/dev/null
  sudo -n docker run --rm --network host --read-only \
    --tmpfs /var/cache/nginx:rw,nosuid,nodev,noexec,size=32m \
    --tmpfs /var/run:rw,nosuid,nodev,noexec,size=1m \
    --tmpfs /var/log/nginx:rw,nosuid,nodev,noexec,size=4m \
    -v "$RUN_ROOT:$RUN_ROOT:rw" \
    -v "$RUN_ROOT/active-nginx.conf:/etc/nginx/nginx.conf:ro" \
    -v "$AUTH_INCLUDE:$AUTH_INCLUDE:ro" \
    "$GATEWAY_IMAGE" nginx -t -c /etc/nginx/nginx.conf >/dev/null
}

case "$role:$verb" in
  worker:start|head:start)
    sudo -n install -d -m 0711 "$RUN_ROOT"
    printf '%s\n' "$trace_nonce" | sudo -n tee "$RUN_ROOT/trace-nonce" >/dev/null
    sudo -n chmod 0644 "$RUN_ROOT/trace-nonce"
    printf '%s\n' "$context_tokens" | sudo -n tee "$RUN_ROOT/context-tokens" >/dev/null
    sudo -n chmod 0644 "$RUN_ROOT/context-tokens"
    sudo -n systemctl start "$UNIT"
    deadline=$((SECONDS + 1200))
    until [[ "$(container_count)" == 1 ]]; do
      (( SECONDS < deadline )) || { echo 'rank readiness timeout' >&2; exit 1; }
      sleep 2
    done
    verify_rank_identity
    actual_context="$(sudo -n docker inspect "$(container_id)" --format '{{json .Config.Cmd}}' | python3 -c 'import json,sys; c=json.load(sys.stdin); print(c[c.index("--max-model-len")+1])')"
    [[ "$actual_context" == "$context_tokens" ]] || { echo 'rank context mismatch' >&2; exit 1; }
    if [[ "$role" == worker ]]; then
      verify_worker_stable
    else
      until curl -fsS --max-time 3 http://127.0.0.1:18889/health >/dev/null; do
        [[ "$(container_count)" == 1 ]] || { echo 'Vision rank exited during model load (check runtime memory/context error)' >&2; exit 1; }
        (( SECONDS < deadline )) || { echo 'API readiness timeout' >&2; exit 1; }
        sleep 3
      done
    fi
    ;;
  head:stop|worker:stop)
    [[ "$role" != head ]] || sudo -n systemctl stop "$GATEWAY_UNIT" || true
    sudo -n systemctl stop "$UNIT" || true
    [[ "$(container_count)" == 0 ]]
    ;;
  head:production-activate-gateway)
    sudo -n test -s "$AUTH_TOKEN"
    sudo -n test -s "$AUTH_INCLUDE"
    sudo -n install -d -m 0711 "$RUN_ROOT"
    sudo -n install -m 0600 "$INSTALL_ROOT/deepseek-v4-flash-vision-exp-nginx.conf" "$RUN_ROOT/active-nginx.conf"
    verify_nginx_config
    sudo -n systemctl restart "$GATEWAY_UNIT"
    verify_gateway production "$operation_id"
    ;;
  head:staging-activate-gateway)
    sudo -n test -s "$AUTH_TOKEN"
    sudo -n test -s "$AUTH_INCLUDE"
    sudo -n install -d -m 0711 "$RUN_ROOT"
    sudo -n install -m 0600 "$INSTALL_ROOT/deepseek-v4-flash-vision-exp-nginx-staging.conf" "$RUN_ROOT/active-nginx.conf"
    verify_nginx_config
    sudo -n systemctl restart "$GATEWAY_UNIT"
    verify_gateway staging "$operation_id"
    ;;
  head:production-deactivate-gateway)
    sudo -n systemctl stop "$GATEWAY_UNIT" || true
    ;;
  head:fail-close-head)
    sudo -n systemctl stop "$GATEWAY_UNIT" || true
    sudo -n systemctl stop "$UNIT" || true
    [[ "$(container_count)" == 0 ]]
    sudo -n test -s "$AUTH_INCLUDE"
    sudo -n test -s "$RUN_ROOT/recovery-id.conf"
    sudo -n install -m 0600 "$INSTALL_ROOT/deepseek-v4-flash-vision-exp-nginx-maintenance.conf" "$RUN_ROOT/active-nginx.conf"
    verify_nginx_config
    sudo -n systemctl restart "$GATEWAY_UNIT"
    verify_gateway maintenance "$operation_id"
    ;;
  head:acceptance-rank-proof|worker:acceptance-rank-proof)
    [[ "$(container_count)" == 1 ]]
    trace_root="/home/operator/.local/state/$PROJECT/native-trace"
    sudo -n python3 - "$trace_root" "$trace_nonce" "$request_body_sha256" "$control_body_sha256" "$role" <<'PY'
import hashlib,json,pathlib,re,sys
root,nonce,request_hash,control_hash,role=pathlib.Path(sys.argv[1]),sys.argv[2],sys.argv[3],sys.argv[4],sys.argv[5]
rows=[]
for path in root.glob('native-rank-*.jsonl'):
 for line in path.read_text().splitlines():
  try: row=json.loads(line)
  except json.JSONDecodeError: continue
  if row.get('nonce')==nonce: rows.append(row)
if not rows: raise SystemExit('operation-bound native trace is absent')
rows.sort(key=lambda row: row.get('monotonic_ns',0))
for row in rows:
 claimed=row.get('sha256')
 unsigned={key:value for key,value in row.items() if key!='sha256'}
 actual=hashlib.sha256(json.dumps(unsigned,sort_keys=True,separators=(',',':')).encode()).hexdigest()
 if claimed!=actual: raise SystemExit('native trace row hash mismatch')
events=[row.get('event') for row in rows]
start=0
if role=='head':
 controls=[i for i,row in enumerate(rows) if row.get('event')=='asgi.request.accepted' and row.get('metadata',{}).get('body_sha256')==control_hash]
 images=[i for i,row in enumerate(rows) if row.get('event')=='asgi.request.accepted' and row.get('metadata',{}).get('body_sha256')==request_hash]
 if not controls or not images or controls[0]>=images[0]: raise SystemExit('ordered control/image head trace is absent')
 forbidden={'multimodal.enter','vision.enter','vision.exit','aligner.enter','aligner.exit','multimodal.exit','merge.enter','merge.exit'}
 if any(event in forbidden for event in events[controls[0]+1:images[0]]): raise SystemExit('text-only control executed the native vision path')
 # The first identical canary invocation proves the native path. Later calls
 # may legitimately hit vLLM's encoder cache and emit no duplicate vision ops.
 start=images[0]+1
else:
 starts=[index for index,event in enumerate(events) if event=='multimodal.enter']
 if not starts: raise SystemExit('worker native multimodal trace is absent')
 # Startup profiling also exercises the encoder. The latest complete worker
 # chain is the request-time canary; repeated identical canaries may reuse it.
 start=starts[-1]
required=['multimodal.enter','vision.enter','vision.exit','aligner.enter','aligner.exit','multimodal.exit','merge.enter','merge.exit']
position=start
selected=[]
for expected in required:
 try:
  index=events.index(expected,position)
  selected.append(rows[index])
  position=index+1
 except ValueError: raise SystemExit(f'ordered native trace event absent: {expected}')
expected_rank='0' if role=='head' else '1'
def tensor_nodes(value):
 if isinstance(value,dict):
  if {'shape','dtype','device'}.issubset(value): yield value
  for child in value.values(): yield from tensor_nodes(child)
 elif isinstance(value,list):
  for child in value: yield from tensor_nodes(child)
for row in selected:
 identity=row.get('identity') or {}
 if str(identity.get('rank'))!=expected_rank or str(identity.get('world_size'))!='2': raise SystemExit('native trace rank/world mismatch')
 if not isinstance(identity.get('pid'),int) or identity['pid']<=0: raise SystemExit('native trace PID mismatch')
 if not str(identity.get('pid_start_ticks','')).isdigit(): raise SystemExit('native trace process-start identity mismatch')
 if not re.fullmatch(r'[0-9a-f]{64}',str(identity.get('cgroup_sha256',''))): raise SystemExit('native trace cgroup identity mismatch')
 tensors=list(tensor_nodes(row.get('metadata') or {}))
 if not tensors or not any(str(item.get('device','')).startswith('cuda') and item.get('shape') for item in tensors): raise SystemExit('native trace lacks CUDA tensor metadata')
PY
    ;;
  *) exit 64 ;;
esac

python3 - "$role" "$verb" "$operation_id" <<'PY'
import hashlib,json,sys
role,verb,operation_id=sys.argv[1:]
value={'terminal':'succeeded','role':role,'verb':verb,'operation_id':operation_id}
if verb=='fail-close-head': value.update(model_id='deepseek-v4-flash-vision-exp',head_fail_closed=True,maintenance_active=True,recovery_id=operation_id)
value['sha256']=hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
print(json.dumps(value,sort_keys=True))
PY
