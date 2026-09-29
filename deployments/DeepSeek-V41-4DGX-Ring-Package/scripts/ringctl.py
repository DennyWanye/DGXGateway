#!/usr/bin/env python3
"""Per-node Docker launcher. Does not SSH, modify networking, or stop other workloads."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import time
from common import CONTAINER, TEST_CONTAINER, PROJECT_LABEL, STAGES, load_config

LABEL_KEY, LABEL_VALUE = PROJECT_LABEL.split('=', 1)

def run(cmd: list[str], *, capture: bool = False) -> str:
    p = subprocess.run(cmd, check=True, text=True, stdout=subprocess.PIPE if capture else None)
    return p.stdout.strip() if capture else ''

def inspect_container(name: str) -> dict | None:
    p = subprocess.run(['docker', 'container', 'inspect', name], text=True, capture_output=True)
    if p.returncode:
        # A daemon/permission error is not proof that the name is free.
        run(['docker', 'info'], capture=True)
        if 'No such' not in p.stderr:
            raise RuntimeError(p.stderr.strip())
        return None
    return json.loads(p.stdout)[0]

def stop_owned(name: str) -> None:
    info = inspect_container(name)
    if info is None:
        print(f'Already absent: {name}')
        return
    if info.get('Config', {}).get('Labels', {}).get(LABEL_KEY) != LABEL_VALUE:
        raise RuntimeError(f'Refusing to stop {name}: ownership label mismatch')
    if info.get('State', {}).get('Running'):
        run(['docker', 'stop', '--time', '30', name])
    run(['docker', 'rm', name])

def check_local(n: dict) -> None:
    info = json.loads(run(['ip', '-j', '-4', 'addr', 'show', 'dev', n['management_iface']], capture=True))
    addrs = {a['local'] for x in info for a in x.get('addr_info', [])}
    if n['management_ip'] not in addrs:
        raise RuntimeError('Wrong node/rank or management interface: configured management IP not present locally')
    if not Path('/dev/infiniband').is_dir():
        raise RuntimeError('/dev/infiniband missing: inspect the existing RDMA driver first')

def check_key(path: str) -> None:
    p = Path(path)
    if p.is_symlink() or not p.is_file():
        raise RuntimeError('API env file must be a regular file, not a symlink')
    if stat.S_IMODE(p.stat().st_mode) & 0o077:
        raise RuntimeError('API env file must not be readable/writable by group/other; chmod 600')
    lines = [x for x in p.read_text().splitlines() if x and not x.startswith('#')]
    if len(lines) != 1 or not lines[0].startswith('VLLM_API_KEY=') or len(lines[0].split('=', 1)[1]) < 32:
        raise RuntimeError('API env must contain only VLLM_API_KEY=<at least 32 characters>')
    value = lines[0].split('=', 1)[1]
    if any(x.isspace() for x in value) or any(x in value for x in '\"\''):
        raise RuntimeError('Use an unquoted random hex API key in the Docker env file')

def mem_available_gib() -> float:
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) / 1024**2
    raise RuntimeError('MemAvailable unavailable')

def docker_command(c: dict, rank: int, action: str, stage: str) -> list[str]:
    n = c['nodes'][rank]
    root = c['root']
    args = ['docker', 'run', '--pull', 'never', '--network', 'host', '--ipc', 'host',
            '--gpus', 'all', '--device', '/dev/infiniband:/dev/infiniband',
            '--ulimit', 'memlock=-1:-1', '--ulimit', 'stack=67108864', '--cap-add', 'IPC_LOCK',
            '--tmpfs', '/usr/local/lib/python3.12/dist-packages/nvidia/nccl/lib',
            '--label', PROJECT_LABEL,
            '--label', f'local.dsv41-ring.stage={stage}']
    if action == 'preflight':
        args += ['--rm']
    else:
        args += ['-d', '--restart', 'no', '--name', TEST_CONTAINER if action == 'test' else CONTAINER]
    env = {
        'LD_LIBRARY_PATH': '/opt/nccl-mesh/lib:/usr/local/cuda/lib64',
        'LD_PRELOAD': '/opt/nccl-mesh/lib/libnccl.so.2',
        'VLLM_NCCL_SO_PATH': '/opt/nccl-mesh/lib/libnccl.so.2',
        'PYTHONPATH': '/opt/dsv41-ring/runtime',
        'NCCL_NET_PLUGIN': 'mesh', 'NCCL_NET': 'Mesh', 'NCCL_ALGO': 'Ring',
        'NCCL_RUNTIME_CONNECT': '1', 'NCCL_SOCKET_IFNAME': '=' + n['management_iface'],
        'NCCL_SOCKET_FAMILY': 'AF_INET', 'GLOO_SOCKET_IFNAME': n['management_iface'],
        'VLLM_HOST_IP': n['management_ip'], 'TP_SOCKET_IFNAME': n['management_iface'],
        'MN_IF_NAME': n['management_iface'],
        'NCCL_MESH_GID_INDEX': str(n['mesh_gid_index']), 'NCCL_MESH_DISABLE_RDMA': '0',
        'NCCL_MESH_DEBUG': '1', 'NCCL_DEBUG': 'INFO', 'NCCL_DEBUG_SUBSYS': 'INIT,NET,GRAPH',
        'NCCL_NVLS_ENABLE': '0', 'NCCL_IGNORE_CPU_AFFINITY': '1',
        'TORCH_NCCL_ASYNC_ERROR_HANDLING': '1',
        'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
        'VLLM_USE_BREAKABLE_CUDAGRAPH': '1', 'VLLM_USE_RUST_FRONTEND': '0',
        'VLLM_HAS_FLASHINFER_CUBIN': '1', 'VLLM_USE_FLASHINFER_SAMPLER': '0',
        'TORCH_CUDA_ARCH_LIST': '12.1a', 'FLASHINFER_CUDA_ARCH_LIST': '12.1a',
        'FLASHINFER_DISABLE_VERSION_CHECK': '1', 'MAX_JOBS': '2', 'FLASHINFER_NVCC_THREADS': '1',
        'DSV41_ENGRAM_DISK': '1', 'DSV41_ENGRAM_DISK_THREADS': '32', 'DSV41_ENGRAM_DISK_CHUNK': '16',
        'VLLM_ENGINE_READY_TIMEOUT_S': '3600' if stage == 'smoke32k' else '10800',
        'PYTORCH_CUDA_ALLOC_CONF': 'expandable_segments:True',
    }
    if action == 'start':
        args += ['--memory', '112g', '--memory-swap', '112g', '--oom-score-adj', '500',
                 '--env-file', c['api_key_file'],
                 '-v', f"{c['model_dir']}:/models/DeepSeek-V4.1-Flash:ro",
                 '-v', f'{root}/cache:/cache',
                 '-v', f'{Path(__file__).resolve().parent}:/opt/dsv41-ring/scripts:ro']
        env.update({'HF_HOME': '/cache/huggingface', 'VLLM_CACHE_ROOT': f'/cache/vllm-{stage}',
                    'TILELANG_CACHE_DIR': '/cache/tilelang', 'TRITON_CACHE_DIR': '/cache/triton'})
        if STAGES[stage]['nvme']:
            args += ['-v', f'{root}/kv-offload:/kv-offload']
    for key, val in env.items():
        args += ['-e', f'{key}={val}']
    if action == 'preflight':
        args += ['--entrypoint', 'python3', c['image'], '/opt/dsv41-ring/scripts/preflight_container.py']
    elif action == 'test':
        args += ['--entrypoint', 'torchrun', c['image'], '--nnodes=4', '--nproc-per-node=1',
                 f'--node-rank={rank}', f"--master-addr={c['nodes'][0]['management_ip']}",
                 f"--master-port={c['test_port']}", '/opt/dsv41-ring/scripts/collective_test.py']
    else:
        args += [c['image'], '--stage', stage, '--rank', str(rank), '--master', c['nodes'][0]['management_ip'],
                 '--port', str(c['api_port']), '--master-port', str(c['master_port'])]
    return args

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['config', 'preflight', 'test', 'start', 'stop', 'stop-test', 'status'])
    p.add_argument('--config', default='config/cluster.json')
    p.add_argument('--rank', type=int, choices=range(4), required=True)
    p.add_argument('--stage', choices=STAGES, default='smoke32k')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--maintenance-approved', action='store_true')
    p.add_argument('--accept-1m-risk', action='store_true')
    a = p.parse_args()
    c = load_config(a.config)
    n = c['nodes'][a.rank]
    if a.action == 'config':
        print(json.dumps({'node': n, 'root': c['root'], 'model_dir': c['model_dir'],
                          'stage': STAGES[a.stage]}, ensure_ascii=False, indent=2))
        return
    if a.action in {'preflight', 'test', 'start'}:
        cmd = docker_command(c, a.rank, a.action, a.stage)
        if a.dry_run:
            print(shlex.join(cmd))
            return
    check_local(n)
    if a.action == 'status':
        for name in (CONTAINER, TEST_CONTAINER):
            info = inspect_container(name)
            print(name, json.dumps(info.get('State', {}) if info else {'absent': True}))
        return
    if a.action in {'stop', 'stop-test'}:
        if not a.maintenance_approved:
            raise RuntimeError('Explicit --maintenance-approved is required to stop this deployment')
        stop_owned(CONTAINER if a.action == 'stop' else TEST_CONTAINER)
        return
    if not a.maintenance_approved:
        raise RuntimeError('First obtain the existing Gateway maintenance/exclusive-node lock; then pass --maintenance-approved')
    run(['docker', 'image', 'inspect', c['image']], capture=True)
    if a.action in {'test', 'start'}:
        name = TEST_CONTAINER if a.action == 'test' else CONTAINER
        if inspect_container(name) is not None:
            raise RuntimeError(f'{name} already exists. Preserve logs and stop it explicitly before a new run')
        # Refuse simultaneous communication tests and model runs from this package.
        other = CONTAINER if a.action == 'test' else TEST_CONTAINER
        oi = inspect_container(other)
        if oi and oi.get('State', {}).get('Running'):
            raise RuntimeError(f'{other} is running; finish and stop it first')
    if a.action == 'start':
        if a.stage.startswith('research1m') and not a.accept_1m_risk:
            raise RuntimeError('1M is a separately admitted experiment; --accept-1m-risk is required')
        check_key(c['api_key_file'])
        if mem_available_gib() < 100:
            raise RuntimeError('MemAvailable <100 GiB. Stop here; do not drop caches or kill unrelated processes automatically')
        procs = run(['nvidia-smi', '--query-compute-apps=pid,process_name', '--format=csv,noheader'], capture=True)
        if procs.strip():
            raise RuntimeError(f'Existing GPU compute processes; do not bypass Gateway:\n{procs}')
        for f in ('config.json', 'model.safetensors.index.json', 'model-00047-of-00048.safetensors',
                  'model-00048-of-00048.safetensors'):
            if not (Path(c['model_dir']) / f).is_file():
                raise RuntimeError(f'Model file missing: {f}; complete full hash verification first')
        for d in ('cache', 'logs', 'kv-offload', 'manifests'):
            (Path(c['root']) / d).mkdir(parents=True, exist_ok=True)
        stamp = Path(c['root']) / 'manifests' / f'model-verified-r{a.rank}.json'
        if not stamp.is_file():
            raise RuntimeError(f'Run model_assets.py verify --rank {a.rank} first; missing {stamp}')
        from model_assets import check_stamp
        check_stamp(stamp, Path(c['model_dir']), Path(c['root']) / 'manifests' / 'model-lock.json')
        if STAGES[a.stage]['nvme']:
            import shutil
            if shutil.disk_usage(Path(c['root']) / 'kv-offload').free < 80 * 1024**3:
                raise RuntimeError('NVMe tier needs >=80 GiB free on its filesystem before allocating 64 GiB')
    run(cmd)
    if a.action != 'preflight':
        print(f'Launched rank {a.rank}; this is NOT a readiness or inference pass. Inspect all four logs.')

if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        print(f'STOP: {exc}', file=sys.stderr)
        sys.exit(2)
