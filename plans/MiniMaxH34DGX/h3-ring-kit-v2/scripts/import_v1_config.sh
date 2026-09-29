#!/usr/bin/env bash
# Read only an operator-reviewed v1 shell config. Never use a downloaded/untrusted .env.
set -Eeuo pipefail
KIT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
OLD="${1:?Usage: import_v1_config.sh /absolute/reviewed/v1-kit}"
[[ -f "$OLD/cluster.env" && -f "$OLD/node.env" ]] || exit 2
set -a
source "$OLD/cluster.env"
source "$OLD/node.env"
set +a
python3 - "$KIT" <<'PY'
import json,os,sys
from pathlib import Path
k=Path(sys.argv[1]);c=json.loads((k/'config/cluster.example.json').read_text());n=json.loads((k/'config/node.example.json').read_text())
for dst,src in {'state_root':'H3_ROOT','base_model_root':'MODEL_HOST_PATH','image':'IMAGE','image_id':'EXPECTED_IMAGE_ID','master_addr':'MASTER_ADDR','api_bind':'API_BIND'}.items():c[dst]=os.environ[src]
for dst,src in {'dist_port':'DIST_PORT','api_port':'API_PORT'}.items():c[dst]=int(os.environ[src])
for dst,src in {'control_iface':'CONTROL_IFACE','network_mode':'NETWORK_MODE'}.items():n[dst]=os.environ[src]
n['rank']=int(os.environ['NODE_RANK']);n['hcas']=os.environ.get('NCCL_HCA','')
r=Path(c['state_root']);c.update(assets_root=str(r/'assets'),heretic_root=str(r/'models/encoders/Heretic-Qwen3VL32B'),hybrid_root=str(r/'models/hybrid/Singularity-v1.3'))
for name,value in [('cluster',c),('node',n)]:
 with (k/'config'/f'{name}.json').open('x') as f:json.dump(value,f,indent=2)
print('Config copied without changing v1. Review JSON and retain a known-good placement mode before running.')
PY
