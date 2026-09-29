"""Shared validation; no host modifications on import."""
from __future__ import annotations
import ipaddress
import json
import re
from pathlib import Path

RING_COMMIT = '94047b444e9fc0c062619ad338f54c8255037984'
RECIPE_COMMIT = '592540c69853a8ce9285236ebfd6e54dfc83a013'
MODEL_REPO = 'deepseek-ai/DeepSeek-V4.1-Flash'
MODEL_REV = 'dba1be0a40aa45a94ad051997016db3960a90277'
MESH_COMMIT = '19924dcc7c571d6e260953724d394ae50bad82cf'
NCCL_COMMIT = 'b91894bd5b190c874d98a017f93f5daa515b65d0'
CONTAINER = 'dsv41-ring-tp4'
TEST_CONTAINER = 'dsv41-ring-collectives'
PROJECT_LABEL = 'local.dsv41-ring.owner=step-by-step-20260917'
STAGES = {
    'smoke32k': dict(maxlen=32768, gmu='0.78', seqs=1, batch=512, eager=True, spec=False, vision=False, nvme=False),
    'text256k': dict(maxlen=262144, gmu='0.80', seqs=16, batch=2048, eager=False, spec=True, vision=False, nvme=False),
    'text512k': dict(maxlen=524288, gmu='0.80', seqs=2, batch=2048, eager=False, spec=True, vision=False, nvme=False),
    'vision256k': dict(maxlen=262144, gmu='0.80', seqs=2, batch=2048, eager=False, spec=True, vision=True, nvme=False),
    'research1m': dict(maxlen=1048576, gmu='0.83', seqs=None, batch=8192, eager=False, spec=True, vision=True, nvme=False),
    'research1m-nvme': dict(maxlen=1048576, gmu='0.83', seqs=None, batch=8192, eager=False, spec=True, vision=True, nvme=True),
}

def iface_name(s: str) -> str:
    if not isinstance(s, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', s) or 'FILL' in s or s == 'lo':
        raise ValueError(f'Invalid/unfilled interface: {s!r}')
    return s

def abs_path(s: str) -> str:
    if not isinstance(s, str) or not s.startswith('/') or s == '/' or any(c in s for c in '\n\r\0,:') or '..' in Path(s).parts:
        raise ValueError(f'Unsafe absolute path: {s!r}')
    return s

def load_config(path: str | Path) -> dict:
    c = json.loads(Path(path).read_text())
    for key in ('root', 'model_dir', 'api_key_file'):
        abs_path(c[key])
    if not isinstance(c['image'], str) or not c['image'] or any(x.isspace() for x in c['image']):
        raise ValueError('Invalid image name')
    for key in ('api_port', 'master_port', 'test_port'):
        if not isinstance(c[key], int) or not 1024 <= c[key] <= 65535:
            raise ValueError(f'Invalid {key}')
    if len({c[k] for k in ('api_port','master_port','test_port')}) != 3:
        raise ValueError('API/master/test ports must differ')
    # The service is local by default. Remote access uses an authenticated SSH tunnel.
    if c.get('api_bind') != '127.0.0.1':
        raise ValueError('This bundle requires api_bind=127.0.0.1; use the documented SSH tunnel')
    nodes = c['nodes']
    if len(nodes) != 4 or [n['rank'] for n in nodes] != [0,1,2,3]:
        raise ValueError('Exactly four nodes, in rank order 0,1,2,3, are required')
    mgmt_ips = []
    for n in nodes:
        mgmt = ipaddress.IPv4Address(n['management_ip'])
        if mgmt.is_unspecified or mgmt.is_loopback or mgmt.is_multicast:
            raise ValueError('Invalid management IPv4')
        mgmt_ips.append(mgmt)
        iface_name(n['management_iface'])
        data = [n['prev_iface'], n['next_iface']] + n.get('unused_rdma_ifaces', [])
        for x in data:
            iface_name(x)
        if len(set(data)) != len(data) or n['management_iface'] in data:
            raise ValueError('Management and RDMA interfaces must be distinct; no duplicated interfaces')
        for side in ('prev','next'):
            addr = ipaddress.IPv4Interface(n[f'{side}_cidr'])
            if addr.network.prefixlen != 30 or addr.ip not in list(addr.network.hosts()):
                raise ValueError('Use host addresses in /30 link networks')
            if addr.ip == mgmt:
                raise ValueError('Management IP equals RDMA IP')
        if not isinstance(n['mesh_gid_index'], int) or not 0 <= n['mesh_gid_index'] <= 255:
            raise ValueError('mesh_gid_index must be an inspected integer')
    if len(set(mgmt_ips)) != 4:
        raise ValueError('Management IPs must be unique')
    networks = []
    for i, n in enumerate(nodes):
        a = ipaddress.IPv4Interface(n['next_cidr'])
        b = ipaddress.IPv4Interface(nodes[(i+1)%4]['prev_cidr'])
        if a.network != b.network or a.ip == b.ip:
            raise ValueError(f'Cable {i}->{(i+1)%4} endpoints must share one subnet and use distinct addresses')
        networks.append(a.network)
    if len(set(networks)) != 4:
        raise ValueError('Each cable needs its own subnet')
    if any(m in net for m in mgmt_ips for net in networks):
        raise ValueError('Management IP falls in an RDMA subnet')
    return c

def graph_sizes(k: int, seqs: int | None) -> list[int]:
    if seqs is None:
        return [5,6,10,12,15,18,20,24,25,30,35,36,40,42,48]
    return sorted({k*i for i in range(1,seqs+1)} | {(k+1)*i for i in range(1,seqs+1)})

def model_args(stage: str, rank: int, master: str, port: int, master_port: int) -> list[str]:
    s = STAGES[stage]
    args = ['vllm','serve','/models/DeepSeek-V4.1-Flash',
            '--served-model-name','deepseek-v4.1-flash-ring',
            '--host','127.0.0.1','--port',str(port),
            '--tokenizer-mode','deepseek_v41','--tensor-parallel-size','4',
            '--gpu-memory-utilization',s['gmu'],'--max-model-len',str(s['maxlen']),
            '--max-num-batched-tokens',str(s['batch']),'--block-size','128',
            '--engram-config','{"cpu_offload":false}',
            '--tool-call-parser','deepseek_v41','--enable-auto-tool-choice',
            '--reasoning-parser','deepseek_v41',
            '--distributed-executor-backend','mp','--nnodes','4','--node-rank',str(rank),
            '--master-addr',master,'--master-port',str(master_port)]
    if s['seqs'] is not None:
        args += ['--max-num-seqs',str(s['seqs'])]
    if not s['vision']:
        args += ['--language-model-only']
    if s['eager']:
        args += ['--enforce-eager']
    else:
        args += ['--compilation-config', json.dumps({'cudagraph_mode':'FULL_AND_PIECEWISE',
                                                   'cudagraph_capture_sizes':graph_sizes(5,s['seqs'])},separators=(',',':'))]
    if s['spec']:
        args += ['--speculative-config', json.dumps({'method':'dspark','num_speculative_tokens':5,
              'draft_sample_method':'probabilistic','rejection_sample_method':'block',
              'enable_adaptive_verification':False},separators=(',',':'))]
    if s['nvme']:
        args += ['--enable-cumem-allocator','--kv-transfer-config',json.dumps({
            'kv_connector':'CacheableGroupsOffloadingConnector','kv_connector_module_path':'dsv41_kv_nvme',
            'kv_role':'kv_both','kv_connector_extra_config':{'spec_name':'NVMeOffloadingSpec',
            'spec_module_path':'dsv41_kv_nvme','nvme_dir':'/kv-offload','nvme_bytes':68719476736,
            'offload_prompt_only':False}},separators=(',',':'))]
    if rank:
        args += ['--headless']
    return args
