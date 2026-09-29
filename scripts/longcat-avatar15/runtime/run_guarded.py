#!/usr/bin/env python3
"""Bounded single-node Avatar Docker job. Caller must hold Gateway/GPU lease.

Only this run's labelled container is stopped/removed. No Gateway, lock, network
or other process state is changed. Uses cgroup v2 on the qualified Ubuntu head.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

IMAGE = 'sha256:a8b6aee9435f0a96b3b5aea7b09788e81ff3d675511eadefe3b8759ad6d3c1a1'
MEMORY_CAP = 100 * 1024**3
MIN_AVAILABLE = 20 * 1024**3
INTERVAL = 5
LABEL = 'org.dgxspark.longcat.guard-id'


def sha_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def input_hashes(root):
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError(f'Symlink input is not allowed: {path}')
        if path.is_file():
            result[str(path.relative_to(root))] = sha_file(path)
    if not result:
        raise ValueError('Input directory is empty')
    return result


def memory_available(path=Path('/proc/meminfo')):
    for line in path.read_text().splitlines():
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) * 1024
    raise RuntimeError('MemAvailable missing')


def cgroup_sample(pid, proc_root=Path('/proc'), cgroup_root=Path('/sys/fs/cgroup')):
    lines = (proc_root / str(pid) / 'cgroup').read_text().splitlines()
    rel = next((line[3:] for line in lines if line.startswith('0::')), None)
    if rel is None:
        raise RuntimeError('Expected unified cgroup v2')
    root = cgroup_root.resolve()
    group = (root / rel.lstrip('/')).resolve()
    if group != root and root not in group.parents:
        raise RuntimeError('Invalid cgroup path')
    def number(name):
        return int((group / name).read_text().strip())
    events = dict(line.split() for line in (group / 'memory.events').read_text().splitlines())
    return {'path': str(group), 'memory_current': number('memory.current'), 'memory_peak': number('memory.peak'),
            'memory_max': number('memory.max'), 'memory_swap_max': number('memory.swap.max'),
            'memory_events': {key: int(value) for key, value in events.items()}}


class Budget:
    def __init__(self, seconds, now):
        self.deadline = now + seconds
        self.low_count = 0

    def observe(self, available, now):
        self.low_count = self.low_count + 1 if available < MIN_AVAILABLE else 0
        if now >= self.deadline:
            return 'timeout'
        if self.low_count >= 3:
            return 'host_memavailable_below_20GiB_three_samples'
        return None


def validate(args):
    if args.num_segments not in (1, 5, 19):
        raise ValueError('num-segments must be 1, 5 or 19')
    for attr in ['source_dir', 'weights_dir', 'input_dir']:
        path = getattr(args, attr).resolve(strict=True)
        if not path.is_dir() or ',' in str(path):
            raise ValueError(f'Invalid mount directory: {path}')
        setattr(args, attr, path)
    required = [args.source_dir / 'run_avatar15_clean_speech.py', args.source_dir / 'runtime_guards.py',
                args.source_dir / 'meta_loader.py', args.weights_dir / 'LongCat-Video-Avatar-1.5/lora/dmd_lora.safetensors',
                args.weights_dir / 'LongCat-Video/text_encoder/config.json']
    for path in required:
        if not path.is_file():
            raise FileNotFoundError(path)
    case = json.loads((args.input_dir / 'case.json').read_text())
    if case.get('cond_image') != '/workspace/input/man.png' or case.get('cond_audio') != {'person1': '/workspace/input/speech.wav'}:
        raise ValueError('case.json must reference fixed read-only input paths')
    for name in ['man.png', 'speech.wav']:
        if not (args.input_dir / name).is_file():
            raise FileNotFoundError(name)
    args.run_dir = args.run_dir.absolute()
    if args.run_dir.exists() or args.run_dir.is_symlink() or ',' in str(args.run_dir):
        raise ValueError('run-dir must be a new directory, never an existing output')
    # Do not make a writable child inside an otherwise read-only model/input mount.
    resolved_run = args.run_dir.resolve()
    for path in [args.source_dir, args.weights_dir, args.input_dir]:
        if path == resolved_run or path in resolved_run.parents:
            raise ValueError('run-dir must be separate from read-only mounts')
    if args.timeout_seconds is None:
        args.timeout_seconds = 1800 if args.num_segments == 1 else 7200
    if not 30 <= args.timeout_seconds <= 24 * 3600:
        raise ValueError('Explicit timeout must be between 30 seconds and 24 hours')


def docker_create_args(args, name, guard_id):
    command = ['create', '--name', name, '--label', f'{LABEL}={guard_id}', '--init', '--gpus', 'device=0',
               '--network', 'none', '--read-only', '--ipc', 'private', '--shm-size', '1g',
               '--memory', str(MEMORY_CAP), '--memory-swap', str(MEMORY_CAP),
               '--user', f'{os.getuid()}:{os.getgid()}', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
               '--ulimit', 'memlock=-1:-1', '--ulimit', 'stack=67108864:67108864', '--ulimit', 'core=0:0',
               '--tmpfs', '/tmp:rw,nosuid,nodev,size=4g', '--workdir', '/workspace/src',
               '--entrypoint', '/opt/longcat-venv/bin/python']
    for source, target, readonly in [(args.source_dir, '/workspace/src', True), (args.weights_dir, '/workspace/weights', True),
                                     (args.input_dir, '/workspace/input', True), (args.run_dir / 'output', '/workspace/output', False),
                                     (args.run_dir / 'logs', '/workspace/logs', False), (args.run_dir / 'cache', '/cache', False)]:
        command.extend(['--mount', f'type=bind,source={source},target={target}' + (',readonly' if readonly else '')])
    environment = {'LONGCAT_META_LOAD': '1', 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'HF_DATASETS_OFFLINE': '1',
                   'HOME': '/cache', 'XDG_CACHE_HOME': '/cache', 'HF_HOME': '/cache/huggingface', 'TORCH_HOME': '/cache/torch',
                   'TRITON_CACHE_DIR': '/cache/triton', 'TORCHINDUCTOR_CACHE_DIR': '/cache/inductor',
                   'NUMBA_CACHE_DIR': '/cache/numba', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1',
                   'NCCL_IB_DISABLE': '1', 'NCCL_SOCKET_IFNAME': '=lo', 'NCCL_NET_PLUGIN': 'none'}
    for key, value in environment.items():
        command.extend(['--env', f'{key}={value}'])
    command.extend([IMAGE, '-m', 'torch.distributed.run', '--rdzv_backend=static',
                    '--master_addr=127.0.0.1', '--master_port=29500', '--node_rank=0',
                    '--nnodes=1', '--nproc_per_node=1',
                    'run_avatar15_clean_speech.py', '--context_parallel_size=1',
                    '--checkpoint_dir=/workspace/weights/LongCat-Video-Avatar-1.5', '--model_type=avatar-v1.5',
                    '--use_distill', '--use_int8', '--stage_1=ai2v', '--input_json=/workspace/input/case.json',
                    '--output_dir=/workspace/output', '--resolution=480p', f'--num_segments={args.num_segments}'])
    return command


class Docker:
    def __init__(self):
        self.prefix = ['sudo', '-n', 'docker']

    def call(self, command, timeout=30, check=True):
        result = subprocess.run(self.prefix + command, capture_output=True, text=True, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError(f'Docker {command[0]} failed: {result.stderr.strip()}')
        return result

    def inspect(self, identity):
        data = json.loads(self.call(['inspect', identity]).stdout)
        if len(data) != 1:
            raise RuntimeError('Expected one exact container')
        return data[0]


def own_container(docker, identity, guard_id):
    info = docker.inspect(identity)
    if info['Config'].get('Labels', {}).get(LABEL) != guard_id:
        raise RuntimeError('Refusing action on a container without this run ownership label')
    return info


def stop_owned(docker, identity, guard_id):
    info = own_container(docker, identity, guard_id)
    if info['State']['Running']:
        docker.call(['stop', '--time', '20', info['Id']], timeout=35)
    final = own_container(docker, identity, guard_id)
    if final['State']['Running']:
        raise RuntimeError('Owned container remains running after stop')
    return final


def cleanup_owned(docker, identity, guard_id, log_path):
    result = {'cleanup_complete': False}
    info = stop_owned(docker, identity, guard_id)
    result.update(container_state=info['State'], exit_code=info['State']['ExitCode'],
                  oom_killed=bool(info['State'].get('OOMKilled')))
    try:
        with log_path.open('x') as stream:
            logs = subprocess.run(docker.prefix + ['logs', '--timestamps', info['Id']], stdout=stream,
                                  stderr=subprocess.STDOUT, timeout=30)
            result['docker_logs_exit_code'] = logs.returncode
            if logs.returncode:
                result['log_error'] = f'Docker logs exit {logs.returncode}'
    except Exception as exc:
        result['log_error'] = f'{type(exc).__name__}: {exc}'
    finally:
        # Log evidence failure never prevents independent exact-container cleanup.
        final = own_container(docker, identity, guard_id)
        if final['State']['Running']:
            raise RuntimeError('Refusing to remove a running container after cleanup')
        docker.call(['rm', final['Id']], timeout=30)
        result['cleanup_complete'] = True
    return result


def verify_effective_config(info):
    config = info['HostConfig']
    if info['Image'] != IMAGE:
        raise RuntimeError('Actual container image differs from fixed image')
    if config['Memory'] != MEMORY_CAP or config['MemorySwap'] != MEMORY_CAP:
        raise RuntimeError('Effective Docker memory/swap cap mismatch')
    if config['NetworkMode'] != 'none' or not config['ReadonlyRootfs']:
        raise RuntimeError('Effective Docker isolation settings mismatch')
    mounts = {entry['Destination']: entry for entry in info['Mounts']}
    for target in ['/workspace/src', '/workspace/weights', '/workspace/input']:
        if mounts[target]['RW']:
            raise RuntimeError(f'Effective input mount unexpectedly writable: {target}')
    return {key: config[key] for key in ['Memory', 'MemorySwap', 'NetworkMode', 'ReadonlyRootfs']}


def execute(args, docker=None):
    validate(args)
    before_hashes = input_hashes(args.input_dir)
    if memory_available() < MIN_AVAILABLE:
        raise RuntimeError('Host MemAvailable already below 20GiB; refusing start')
    docker = docker or Docker()
    image = json.loads(docker.call(['image', 'inspect', IMAGE]).stdout)[0]
    if image['Id'] != IMAGE:
        raise RuntimeError('Local fixed image ID mismatch')
    args.run_dir.mkdir(parents=False, exist_ok=False)
    for name in ['output', 'logs', 'cache']:
        (args.run_dir / name).mkdir()
    guard_id = uuid.uuid4().hex
    name = 'longcat-avatar15-' + guard_id
    created = False
    started = time.monotonic()
    report = {'status': 'FAIL', 'image': IMAGE, 'name': name, 'guard_id': guard_id, 'num_segments': args.num_segments,
              'timeout_seconds': args.timeout_seconds, 'memory_cap_bytes': MEMORY_CAP, 'memory_swap_total_cap_bytes': MEMORY_CAP,
              'input_sha256_before': before_hashes, 'started_epoch': time.time(), 'samples': 0,
              'cgroup_peak_bytes_observed': None, 'peak_note': 'Highest memory.peak observed every 5s; a final unsampled peak may be missing if the cgroup exits between samples', 'cleanup_complete': False, 'stop_reason': None}
    old_handlers = {}
    def interrupted(signum, frame):
        raise InterruptedError(f'Received signal {signum}')
    for sig in (signal.SIGINT, signal.SIGTERM):
        old_handlers[sig] = signal.signal(sig, interrupted)
    try:
        command = docker_create_args(args, name, guard_id)
        report['docker_create_argv'] = command
        # A create timeout may still create the exact random labelled container. Finally inspects by name.
        created = True
        container_id = docker.call(command, timeout=60).stdout.strip()
        info = own_container(docker, name, guard_id)
        if info['Id'] != container_id:
            raise RuntimeError('Container identity mismatch after create')
        report['container_id'] = container_id
        docker.call(['start', container_id], timeout=60)
        report['effective_host_config'] = verify_effective_config(own_container(docker, container_id, guard_id))
        budget = Budget(args.timeout_seconds, started)
        with (args.run_dir / 'logs/memory.jsonl').open('x') as samples:
            while True:
                info = own_container(docker, container_id, guard_id)
                if not info['State']['Running']:
                    break
                now = time.monotonic()
                sample = {'elapsed_seconds': now - started, 'epoch': time.time(), 'mem_available': memory_available()}
                try:
                    sample.update(cgroup_sample(info['State']['Pid']))
                except (OSError, ValueError, RuntimeError) as exc:
                    # A just-exited process can legitimately lose its cgroup between inspect and read.
                    after = own_container(docker, container_id, guard_id)
                    if after['State']['Running']:
                        raise RuntimeError(f'Cannot monitor live container cgroup: {exc}') from exc
                    break
                if sample['memory_max'] != MEMORY_CAP or sample['memory_swap_max'] != 0:
                    raise RuntimeError('Kernel cgroup memory/swap limits do not match required cap')
                report['samples'] += 1
                report['cgroup_peak_bytes_observed'] = max(report['cgroup_peak_bytes_observed'] or 0, sample['memory_peak'])
                report['last_memory_events'] = sample['memory_events']
                samples.write(json.dumps(sample) + '\n')
                samples.flush()
                reason = budget.observe(sample['mem_available'], now)
                if reason:
                    report['stop_reason'] = reason
                    stop_owned(docker, container_id, guard_id)
                    break
                time.sleep(INTERVAL)
    except BaseException as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        # Ignore a second user interrupt while bounded Docker cleanup is in flight.
        for sig in old_handlers:
            signal.signal(sig, signal.SIG_IGN)
        if created:
            try:
                report.update(cleanup_owned(docker, name, guard_id, args.run_dir / 'logs/model.log'))
            except BaseException as exc:
                report['cleanup_error'] = f'{type(exc).__name__}: {exc}'
        try:
            report['input_sha256_after'] = input_hashes(args.input_dir)
            report['inputs_unchanged'] = report['input_sha256_after'] == before_hashes
        except Exception as exc:
            report['input_verification_error'] = f'{type(exc).__name__}: {exc}'
            report['inputs_unchanged'] = False
        report['elapsed_seconds'] = time.monotonic() - started
        report['finished_epoch'] = time.time()
        if (report.get('exit_code') == 0 and not report.get('oom_killed') and not report.get('error')
                and not report.get('log_error') and not report['stop_reason'] and report['cleanup_complete'] and report['inputs_unchanged']):
            report['status'] = 'PROCESS_COMPLETED_MEDIA_NOT_VERIFIED'
        (args.run_dir / 'logs/run-report.json').write_text(json.dumps(report, indent=2) + '\n')
        for sig, old in old_handlers.items():
            signal.signal(sig, old)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['source-dir', 'weights-dir', 'input-dir', 'run-dir']:
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--num-segments', type=int, choices=[1, 5, 19], default=1)
    parser.add_argument('--timeout-seconds', type=int)
    args = parser.parse_args()
    try:
        report = execute(args)
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0 if report['status'] == 'PROCESS_COMPLETED_MEDIA_NOT_VERIFIED' else 1


if __name__ == '__main__':
    raise SystemExit(main())
