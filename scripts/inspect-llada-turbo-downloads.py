#!/usr/bin/env python3
"""Read-only download diagnosis. Never prints URLs or imports model code."""
import argparse
import json
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--sample-seconds', type=int, default=30, choices=range(0, 46))
args = parser.parse_args()
remote = r'''
from pathlib import Path
import json, re, subprocess, time, socket
assert socket.gethostname() == 'dgx-head'
def snapshot():
    found = {}
    for p in Path('/proc').iterdir():
        if not p.name.isdigit(): continue
        try:
            if (p/'comm').read_text().strip() != 'aria2c': continue
            argv = (p/'cmdline').read_bytes().decode().split('\0')
            output = next((a[6:] for a in argv if a.startswith('--out=llada-turbo-')), None)
            if output is None: continue
            uid = int(next(s for s in (p/'status').read_text().splitlines() if s.startswith('Uid:')).split()[1])
            io = dict(s.split(': ') for s in (p/'io').read_text().splitlines())
            found[p.name] = {'artifact': output, 'uid': uid, 'wchar': int(io['wchar']),
                'limit': next((a.split('=',1)[1] for a in argv if a.startswith('--max-overall-download-limit=')), None),
                'interface': next((a.split('=',1)[1] for a in argv if a.startswith('--interface=')), None)}
        except (OSError, StopIteration): pass
    return found
before = snapshot(); started = time.monotonic()
time.sleep(SAMPLE_SECONDS)
after = snapshot(); elapsed = time.monotonic() - started
for pid, record in after.items():
    if pid in before and elapsed > 0.5:
        record['sample_MiB_per_s'] = round((record['wchar']-before[pid]['wchar'])/elapsed/1048576, 3)
    artifact = record['artifact'].removesuffix('.part')
    p = Path('/run/dgx-video-'+artifact+'.url.transfer.log')
    if p.exists():
        text = p.read_text(errors='replace')
        record['progress'] = re.findall(r'\[#[a-f0-9]+ [^\]\r\n]*?\bCN:\d+[^\]\r\n]*?\]', text)[-1:]
        record['error_codes'] = re.findall(r'errorCode=\d+|status=\d+', text)[-5:]
route = subprocess.check_output(['runuser','-u','dgx-hf-direct','--','ip','route','get','1.1.1.1'], text=True).strip()
report = {'observed_at_epoch': time.time(), 'elapsed_seconds': elapsed,
    'speed_method': 'aria2 process wchar delta; includes small control/log writes',
    'direct_route': route, 'active_transfers': after,
    'installation_gate': 'USER_AUTHORIZED_INSTALLATION'}
print(json.dumps(report))
'''.replace('SAMPLE_SECONDS', str(args.sample_seconds))
result = subprocess.run(
    ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
     'operator@192.0.2.30', 'sudo -n python3 -'],
    input=remote, capture_output=True, text=True, check=True,
    timeout=args.sample_seconds + 25)
report = json.loads(result.stdout)
root = Path(__file__).resolve().parents[1]
evidence = root/'plans/llada-image-evaluation/install-evidence/turbo-speed-latest.json'
evidence.write_text(json.dumps(report, indent=2)+'\n')
print(json.dumps(report, indent=2))
