#!/usr/bin/env python3
"""Verify downloaded files only; never imports or starts a model."""
import hashlib
import json
import socket
from datetime import datetime, timezone
from pathlib import Path

assert socket.gethostname() == 'dgx-head', 'wrong host'
parent = Path('/home/operator/dgx-models/llada-image')
manifest = json.loads((parent / 'control/base-bf16-exact.json').read_text())
assert manifest['repo'] == 'inclusionAI/LLaDA-Image'
assert manifest['revision'] == 'e4e2703f410f7ddb6ee8d6b09dac6a8ec5093039'
root = parent / 'base-bf16' / manifest['revision']
results = []
for item in manifest['files']:
    relative = Path(item['path'])
    assert not relative.is_absolute() and '..' not in relative.parts
    path = root / relative
    assert root.resolve() in path.resolve().parents and not path.is_symlink()
    assert path.is_file() and path.stat().st_size == item['size'], item['path']
    git_blob = item['digest'].startswith('gitsha1:')
    digest = hashlib.sha1() if git_blob else hashlib.sha256()
    if git_blob:
        digest.update(f"blob {item['size']}\0".encode())
    with path.open('rb') as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b''):
            digest.update(block)
    expected = item['digest'].split(':', 1)[-1]
    assert digest.hexdigest() == expected, item['path']
    results.append({'path': item['path'], 'bytes': item['size'], 'digest': item['digest'], 'verified': True})
report = {'state': 'DOWNLOAD_VERIFIED', 'model_installed': False,
          'model_tested': False, 'installation_gate': 'WAIT_USER_CONFIRMATION',
          'checked_at': datetime.now(timezone.utc).isoformat(),
          'repo': manifest['repo'], 'revision': manifest['revision'],
          'file_count': len(results), 'total_bytes': sum(x['bytes'] for x in results),
          'files': results}
target = parent / 'control/base-bf16-verification.json'
temporary = target.with_suffix('.json.tmp')
temporary.write_text(json.dumps(report, indent=2) + '\n')
temporary.replace(target)
print(json.dumps(report))
