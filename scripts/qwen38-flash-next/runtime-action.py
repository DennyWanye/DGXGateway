#!/usr/bin/env python3
"""Root-installed, receipt-gated node actions. GPU authority remains on the Mac."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

PROJECT = 'qwen38-flash-next'
IMAGE = 'sha256:393454aa5bc87f02e7a5fe2eed0d833e295e7c3bb97f0b20f6ff66689df0ed90'
ROOT = Path('/opt') / PROJECT
RUN = Path('/run') / PROJECT
UNIT = PROJECT + '.service'
GW_UNIT = PROJECT + '-gateway.service'
MODEL_ROOT = '/home/operator/dgx-models/qwen38-flash-next/weights/qwen3.8-flash-next-abliterated-NVFP4-plefp8'
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def command(args, timeout=60):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Never echo stdin receipts or credentials in diagnostics.
        raise RuntimeError('command failed: ' + args[0] + ' ' + ' '.join(args[1:3]))
    return result.stdout


def write_private(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o711)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(fd, mode)
        os.write(fd, content.encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def validate(receipt, role, verb, now=None):
    allowed = {'start', 'stop'} if role == 'worker' else {
        'start', 'stop', 'gateway-staging', 'gateway-production', 'gateway-stop', 'drain', 'fail-close-head'}
    if role not in {'head', 'worker'} or verb not in allowed:
        raise ValueError('invalid action')
    if any(receipt.get(k) != v for k, v in {
        'schema': 'qwen38-receipt-v1', 'project': PROJECT, 'role': role, 'verb': verb}.items()):
        raise ValueError('receipt binding mismatch')
    for key in ('operation_id', 'lease_generation', 'nonce'):
        if not isinstance(receipt.get(key), str) or not re.fullmatch(r'[A-Za-z0-9._-]{3,128}', receipt[key]):
            raise ValueError('invalid receipt identifier')
    now = now or datetime.now(timezone.utc)
    expiry = datetime.fromisoformat(receipt['not_after'])
    if expiry.tzinfo is None or not now < expiry <= now + timedelta(minutes=6):
        raise ValueError('receipt expired or outside window')
    token = receipt.get('gateway_token')
    if verb == 'start':
        if receipt.get('profile', '32K') not in ('32K', '256K'):
            raise ValueError('invalid Qwen context profile')
    elif 'profile' in receipt:
        raise ValueError('profile on non-start action')
    if token is not None:
        if role != 'head' or verb not in {'gateway-staging', 'gateway-production', 'fail-close-head'}:
            raise ValueError('credential on invalid action')
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', token):
            raise ValueError('invalid credential')
    return receipt


def containers(role, all_containers=False):
    args = ['docker', 'ps'] + (['-a'] if all_containers else [])
    for value in ('com.docker.compose.project=' + PROJECT, 'com.docker.compose.service=qwen',
                  'com.dgx-spark.model-id=' + PROJECT, 'com.dgx-spark.role=' + role):
        args += ['--filter', 'label=' + value]
    return command(args + ['--format', '{{.ID}}']).split()


def fatal_startup(logs):
    # NCCL INFO reports optional plugin discovery failures before using built-in
    # IB. This exact runtime emitted those same lines in successful TP2 runs.
    return re.search(r'Traceback|NCCL\s+(?:WARN|ERROR)\b|CUDA error|Segmentation fault|Address already in use|Using network Socket', logs, re.I)


def capture_rank(role, operation_id):
    directory = Path('/var/lib') / PROJECT / 'evidence' / operation_id
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for identity in containers(role, True):
        for label, args in [('inspect', ['docker', 'inspect', identity]),
                            ('log', ['docker', 'logs', '--timestamps', identity])]:
            path = directory / (role + '-' + identity + '.' + label)
            if path.exists():
                continue
            with path.open('x') as stream:
                subprocess.run(args, stdout=stream, stderr=subprocess.STDOUT, timeout=30)


def verify_rank(role):
    ids = containers(role)
    if len(ids) != 1:
        raise RuntimeError('rank cardinality mismatch')
    row = json.loads(command(['docker', 'inspect', ids[0]]))[0]
    if row['Image'] != IMAGE or not row['State']['Running'] or row['RestartCount'] != 0:
        raise RuntimeError('rank image, state or restart mismatch')
    if row['HostConfig']['RestartPolicy']['Name'] != 'no' or row['HostConfig']['NetworkMode'] != 'host':
        raise RuntimeError('rank lifecycle/network mismatch')
    if not any(m.get('Source') == MODEL_ROOT and m.get('Destination') == '/model' and m.get('RW') is False for m in row['Mounts']):
        raise RuntimeError('rank weight mount mismatch')
    env = dict(item.split('=', 1) for item in row['Config']['Env'])
    if env.get('QWEN_ROLE') != role or env.get('NCCL_IB_HCA') != '=rocep1s0f0:1,roceP2p1s0f0:1':
        raise RuntimeError('rank role/fabric mismatch')
    logs = subprocess.run(['docker', 'logs', '--tail', '100', ids[0]], capture_output=True, text=True, timeout=15)
    if fatal_startup(logs.stdout + logs.stderr):
        raise RuntimeError('rank fatal startup signature')
    available = int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:'))) * 1024
    if available < 16 * 1024 ** 3:
        raise RuntimeError('16 GiB available-memory guard')
    return ids[0]


def stop(role):
    errors = []
    if role == 'head':
        subprocess.run(['systemctl', 'stop', GW_UNIT], capture_output=True, timeout=60)
    subprocess.run(['systemctl', 'stop', UNIT], capture_output=True, timeout=150)
    # Exact project fallback also handles a unit failing before RemainAfterExit.
    command(['docker', 'compose', '-p', PROJECT, '-f', str(ROOT / ('compose.' + role + '.json')), 'down', '--timeout', '30'], timeout=100)
    if containers(role, True):
        errors.append('rank remains after stop')
    if command(['ss', '-H', '-ltn', 'sport = :18890']).strip():
        errors.append('internal listener remains after stop')
    if errors:
        raise RuntimeError('; '.join(errors))


def fetch(url, token=None):
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token} if token else {})
    try:
        with HTTP.open(req, timeout=5) as response:
            return response.status, response.read(1024 * 1024)
    except urllib.error.HTTPError as error:
        return error.code, error.read(1024 * 1024)


def activate(receipt, mode):
    token = receipt.get('gateway_token')
    if not token:
        raise ValueError('head gateway credential required')
    write_private(Path('/run/secrets/qwen38-auth.conf'), f'map $http_authorization $qwen_auth_ok {{ default 0; "Bearer {token}" 1; }}\n')
    conf = (ROOT / 'gateway.conf').read_text()
    if mode == 'staging':
        conf = conf.replace('192.0.2.30:11434', '127.0.0.1:11435')
    write_private(RUN / 'admission.conf', 'set $qwen_accepting ' + ('0' if mode == 'maintenance' else '1') + ';\n')
    write_private(RUN / 'active-nginx.conf', conf)
    command(['systemctl', 'restart', GW_UNIT], timeout=60)
    url = ('http://127.0.0.1:11435' if mode == 'staging' else 'http://192.0.2.30:11434') + '/v1/models'
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            missing, _ = fetch(url)
            status, raw = fetch(url, token)
            if missing == 401 and ((mode == 'maintenance' and status == 503) or
                (status == 200 and {r['id'] for r in json.loads(raw)['data']} == {PROJECT})):
                return
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(.5)
    raise RuntimeError('gateway identity/auth readiness failed')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--role', choices=['head', 'worker'], required=True)
    parser.add_argument('--verb', required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('root-installed action requires sudo -n')
    role, verb = args.role, args.verb
    hostname = command(['hostname']).strip()
    if hostname != ('dgx-head' if role == 'head' else 'dgx-worker') or (ROOT / 'role').read_text().strip() != role:
        raise SystemExit('node identity mismatch')
    raw = sys.stdin.buffer.read(8193)
    if len(raw) > 8192:
        raise SystemExit('receipt too large')
    receipt = validate(json.loads(raw), role, verb)
    nonces = Path('/var/lib') / PROJECT / 'receipts'
    nonces.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(nonces / receipt['nonce'], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    RUN.mkdir(parents=True, exist_ok=True, mode=0o711)
    if verb == 'start':
        try:
            if containers(role, True):
                raise RuntimeError('profile start requires stopped rank')
            write_private(ROOT / 'profile.json', json.dumps({'profile': receipt.get('profile', '32K')}), 0o644)
            command(['systemctl', 'start', UNIT], timeout=120)
            first = verify_rank(role)
            if role == 'worker':
                time.sleep(15)
                if verify_rank(role) != first:
                    raise RuntimeError('worker changed during stabilization')
                if command(['ss', '-H', '-ltn', 'sport = :18890']).strip():
                    raise RuntimeError('worker must be headless')
            else:
                deadline = time.monotonic() + 1200
                while time.monotonic() < deadline:
                    verify_rank(role)
                    try:
                        status, _ = fetch('http://127.0.0.1:18890/health')
                        model_status, model_raw = fetch('http://127.0.0.1:18890/v1/models')
                        expected_len = {'32K': 32768, '256K': 262144}[receipt.get('profile', '32K')]
                        if status == model_status == 200 and json.loads(model_raw)['data'][0]['max_model_len'] == expected_len:
                            break
                    except OSError:
                        pass
                    time.sleep(3)
                else:
                    raise TimeoutError('model readiness timeout')
        except BaseException:
            try:
                capture_rank(role, receipt['operation_id'])
            finally:
                stop(role)
            raise
    elif verb == 'stop':
        try:
            capture_rank(role, receipt['operation_id'])
        finally:
            stop(role)
    elif verb in {'gateway-staging', 'gateway-production'}:
        activate(receipt, verb.split('-')[1])
    elif verb == 'gateway-stop':
        command(['systemctl', 'stop', GW_UNIT])
    elif verb == 'drain':
        write_private(RUN / 'admission.conf', 'set $qwen_accepting 0;\n')
        # Directory mount ensures replacement visibility. Reload rejects new work;
        # existing request sockets remain attached to the old worker until drained.
        command(['docker', 'exec', PROJECT + '-gateway', 'nginx', '-s', 'reload'])
    elif verb == 'fail-close-head':
        stop(role)
        activate(receipt, 'maintenance')
    result = {'terminal': 'succeeded', 'operation_id': receipt['operation_id'],
              'role': role, 'verb': verb, 'lease_generation': receipt['lease_generation']}
    if verb == 'fail-close-head':
        result.update(model_id=PROJECT, head_fail_closed=True, maintenance_active=True,
                      recovery_id=receipt['operation_id'])
    result['sha256'] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    print(json.dumps(result))


if __name__ == '__main__':
    main()
