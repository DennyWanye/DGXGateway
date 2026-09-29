#!/usr/bin/env python3
"""Install inert Mia-only unit/config files; does not start GPU processes."""
import hashlib
import json
from pathlib import Path
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def remote(address, args, data=None):
    result = subprocess.run(
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-o', 'StrictHostKeyChecking=yes',
         'operator@' + address, shlex.join(args)],
        input=data, capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError('install command failed: ' + result.stderr.decode()[:400])
    return result.stdout.decode()


def main():
    results = []
    for role, address, host in [('head', '192.0.2.30', 'dgx-head'), ('worker', '192.0.2.31', 'dgx-worker')]:
        assert remote(address, ['sh', '-c', 'hostname; whoami']).split() == [host, 'operator']
        remote(address, ['sh', '-c',
                         'test ! -e /opt/mia-dsv41-ablit && test ! -e /usr/local/libexec/mia-dsv41-runtime-action.py'
                         ' && test ! -e /etc/systemd/system/mia-dsv41-ablit-gateway.service'])
        stage = '/home/operator/dgx-models/mia-deepseek-v41-ablit/control/runtime/gateway-install'
        remote(address, ['mkdir', '-p', stage])
        files = {
            ROOT / 'scripts/mia-dsv41-ablit/runtime-action.py': ('/usr/local/libexec/mia-dsv41-runtime-action.py', '0755'),
        }
        if role == 'head':
            files[ROOT / 'containers/mia-dsv41-ablit/gateway.conf'] = ('/opt/mia-dsv41-ablit/gateway.conf', '0644')
            files[ROOT / 'containers/mia-dsv41-ablit/gateway.service'] = (
                '/etc/systemd/system/mia-dsv41-ablit-gateway.service', '0644')
        remote(address, ['sudo', '-n', 'install', '-d', '-m', '0755', '/opt/mia-dsv41-ablit'])
        remote(address, ['sh', '-c', 'cat > ' + shlex.quote(stage + '/profile.json')], b'{"profile":"256K"}\n')
        remote(address, ['sudo', '-n', 'install', '-m', '0644', stage + '/profile.json', '/opt/mia-dsv41-ablit/profile.json'])
        hashes = {}
        for source, (dest, mode) in files.items():
            raw = source.read_bytes()
            temporary = stage + '/' + source.name
            remote(address, ['sh', '-c', 'umask 077; cat > ' + shlex.quote(temporary)], raw)
            remote(address, ['sudo', '-n', 'install', '-m', mode, temporary, dest])
            actual = remote(address, ['sha256sum', dest]).split()[0]
            assert actual == hashlib.sha256(raw).hexdigest()
            hashes[dest] = actual
        remote(address, ['sh', '-c', 'printf %s ' + shlex.quote(role) + ' > ' + shlex.quote(stage + '/role')])
        remote(address, ['sudo', '-n', 'install', '-m', '0644', stage + '/role', '/opt/mia-dsv41-ablit/role'])
        remote(address, ['sudo', '-n', 'systemctl', 'daemon-reload'])
        if role == 'head':
            state = remote(address, ['systemctl', 'show', 'mia-dsv41-ablit-gateway.service',
                                     '--property=LoadState,ActiveState,UnitFileState'])
            assert 'ActiveState=inactive' in state and 'LoadState=loaded' in state
        else:
            state = remote(address, ['sh', '-c',
                                     'test ! -e /etc/systemd/system/mia-dsv41-ablit-gateway.service && echo worker-headless'])
            assert 'worker-headless' in state
        results.append({'role': role, 'hashes': hashes, 'unit': state, 'gpu_started': False})
        print(json.dumps({'role': role, 'installed': True, 'gpu_started': False}), flush=True)
    evidence = ROOT / 'plans/mia-deepseek-v41-ablit-install/evidence/deploy'
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / 'gateway-runtime-install.json').write_text(json.dumps(results, indent=2) + '\n')


if __name__ == '__main__':
    main()
