#!/usr/bin/env python3
"""Root-installed, receipt-gated node actions. GPU authority remains on the Mac."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

PROJECT = 'mia-dsv41-ablit'
IMAGE = 'sha256:4cdba4e946da2d19bf5b5a20c6d3a1a4bf421fa4d6db5082f271a986168176cb'
ROOT = Path('/opt') / PROJECT
RUN = Path('/run') / PROJECT
GW_UNIT = PROJECT + '-gateway.service'
RECIPE = Path('/home/operator/apps/dsv41-mia-ablit-979e68a')
MODEL_ROOT = '/home/operator/dgx-models/mia-deepseek-v41-ablit/ablit-work/model'
CONTAINER = {'head': 'dsv41-exl3-head', 'worker': 'dsv41-exl3-worker'}
INTERNAL_PORT = 8888
PROFILES = {'256K': 262144, '512K': 524288}
PROFILE_SEQS = {'256K': 2, '512K': 1}
PROFILE_KV = {'256K': 2684354560, '512K': 5368709120}
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def command(args, timeout=60):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
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
        'schema': 'mia-dsv41-receipt-v1', 'project': PROJECT, 'role': role, 'verb': verb}.items()):
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
        if receipt.get('profile') not in PROFILES:
            raise ValueError('invalid Mia context profile')
    elif 'profile' in receipt:
        raise ValueError('profile on non-start action')
    if token is not None:
        if role != 'head' or verb not in {'gateway-staging', 'gateway-production', 'fail-close-head'}:
            raise ValueError('credential on invalid action')
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', token):
            raise ValueError('invalid credential')
    return receipt


def apply_profile_env(text, profile):
    if profile not in PROFILES:
        raise ValueError('invalid Mia context profile')
    block = (
        '# BEGIN-GATEWAY-PROFILE\n'
        f'SERVED_MODEL_NAME={PROJECT}\n'
        f'MAX_MODEL_LEN={PROFILES[profile]}\n'
        f'MAX_NUM_SEQS={PROFILE_SEQS[profile]}\n'
        f'KV_CACHE_MEMORY_BYTES={PROFILE_KV[profile]}\n'
        'DSV41_BOOT_MARGIN_GIB=8\n'
        '# END-GATEWAY-PROFILE\n'
    )
    if '# BEGIN-GATEWAY-PROFILE' in text:
        return re.sub(
            r'# BEGIN-GATEWAY-PROFILE.*?# END-GATEWAY-PROFILE\n?',
            block, text, count=1, flags=re.S)
    if text and not text.endswith('\n'):
        text += '\n'
    return text + '\n' + block


def fatal_startup(logs):
    return re.search(
        r'Traceback|NCCL\s+ERROR\b|CUDA error|Segmentation fault|Address already in use|Using network Socket',
        logs, re.I)


def containers(role, all_containers=False):
    args = ['docker', 'ps'] + (['-a'] if all_containers else [])
    return command(args + ['--filter', 'name=^/' + CONTAINER[role] + '$', '--format', '{{.ID}}']).split()


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


def recipe_api_key():
    key = None
    for line in Path(RECIPE / '.env').read_text().splitlines():
        if line.startswith('VLLM_API_KEY='):
            key = line.split('=', 1)[1].strip().strip('"').strip("'")
    if not key or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', key):
        raise RuntimeError('recipe API key missing or invalid')
    return key


def as_operator(args, timeout=60, cwd=None, env=None, stdin=None, stdout=None):
    extra = ['HOME=/home/operator']
    if env:
        extra += [key + '=' + value for key, value in env.items()]
    kwargs = dict(cwd=cwd, input=stdin, text=True, timeout=timeout)
    if stdout is None:
        kwargs['capture_output'] = True
    else:
        kwargs['stdout'] = stdout
        kwargs['stderr'] = stdout
    result = subprocess.run(
        ['sudo', '-n', '-u', 'operator', '-g', 'docker', '-H', 'env', *extra, *args],
        **kwargs)
    if result.returncode:
        raise RuntimeError('operator command failed: ' + args[0])
    return result.stdout if stdout is None else ''


def write_profile(profile):
    env_path = RECIPE / '.env'
    updated = apply_profile_env(env_path.read_text(), profile)
    write_private(env_path, updated, 0o600)
    owner = pwd.getpwnam('operator')
    os.chown(env_path, owner.pw_uid, owner.pw_gid)
    write_private(ROOT / 'profile.json', json.dumps({'profile': profile}), 0o644)


def verify_rank(role):
    ids = containers(role)
    if len(ids) != 1:
        raise RuntimeError('rank cardinality mismatch')
    row = json.loads(command(['docker', 'inspect', ids[0]]))[0]
    if row['Image'] != IMAGE or not row['State']['Running'] or row['RestartCount'] != 0:
        raise RuntimeError('rank image, state or restart mismatch')
    if row['HostConfig']['RestartPolicy']['Name'] != 'no' or row['HostConfig']['NetworkMode'] != 'host':
        raise RuntimeError('rank lifecycle/network mismatch')
    if role == 'head':
        if not any(m.get('Source') == MODEL_ROOT and m.get('Destination') == '/model' and m.get('RW') is False
                   for m in row['Mounts']):
            raise RuntimeError('rank weight mount mismatch')
    logs = subprocess.run(['docker', 'logs', '--tail', '80', ids[0]], capture_output=True, text=True, timeout=15)
    if fatal_startup(logs.stdout + logs.stderr):
        raise RuntimeError('rank fatal startup signature')
    return ids[0]


def stop(role):
    errors = []
    if role == 'head':
        subprocess.run(['systemctl', 'stop', GW_UNIT], capture_output=True, timeout=60)
        try:
            as_operator(['./start.sh', 'stop'], timeout=180, cwd=str(RECIPE))
        except RuntimeError as error:
            errors.append(str(error))
        subprocess.run(['docker', 'rm', '-f', CONTAINER['head']], capture_output=True, timeout=60)
    subprocess.run(['docker', 'rm', '-f', CONTAINER[role]], capture_output=True, timeout=60)
    if containers(role, True):
        errors.append('rank remains after stop')
    if command(['ss', '-H', '-ltn', 'sport = :' + str(INTERNAL_PORT)]).strip():
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
    write_private(Path('/run/secrets/mia-dsv41-auth.conf'),
                  f'map $http_authorization $mia_auth_ok {{ default 0; "Bearer {token}" 1; }}\n')
    write_private(RUN / 'upstream-auth.conf',
                  'proxy_set_header Authorization "Bearer ' + recipe_api_key() + '";\n')
    conf = (ROOT / 'gateway.conf').read_text()
    if mode == 'staging':
        conf = conf.replace('192.0.2.30:11434', '127.0.0.1:11435')
    write_private(RUN / 'admission.conf', 'set $mia_accepting ' + ('0' if mode == 'maintenance' else '1') + ';\n')
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


def start_head(profile):
    write_profile(profile)
    log = Path('/var/lib') / PROJECT / 'logs' / 'start.sh.log'
    log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with log.open('w') as stream:
        as_operator(
            ['./start.sh'],
            timeout=2400,
            cwd=str(RECIPE),
            env={'SKIP_BUILD': '1', 'SKIP_PULL': '1', 'READY_TIMEOUT': '1800',
                 'DSV41_BOOT_MARGIN_GIB': '8'},
            stdout=stream,
        )
    first = verify_rank('head')
    deadline = time.monotonic() + 120
    key = recipe_api_key()
    expected_len = PROFILES[profile]
    while time.monotonic() < deadline:
        verify_rank('head')
        try:
            status, _ = fetch('http://127.0.0.1:8888/health')
            model_status, model_raw = fetch('http://127.0.0.1:8888/v1/models', key)
            payload = json.loads(model_raw)
            identity = payload['data'][0]
            if status == model_status == 200 and identity['id'] == PROJECT and identity['max_model_len'] == expected_len:
                return first
        except (OSError, ValueError, KeyError, IndexError):
            pass
        time.sleep(3)
    raise TimeoutError('model readiness timeout')


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
            if role == 'worker':
                time.sleep(2)
                if containers(role, True):
                    raise RuntimeError('worker must stay empty until head Recipe launch')
                if command(['ss', '-H', '-ltn', 'sport = :' + str(INTERNAL_PORT)]).strip():
                    raise RuntimeError('worker must be headless')
            else:
                start_head(receipt['profile'])
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
        write_private(RUN / 'admission.conf', 'set $mia_accepting 0;\n')
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
