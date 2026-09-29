#!/usr/bin/env python3
"""Local fail-closed launcher changes, only for the documented immutable source blobs."""
from __future__ import annotations
import argparse
import difflib
import hashlib
from pathlib import Path
import subprocess
import sys

BLOBS = {'start.sh': '65c6c2584ba29eed3b2b535a3da4589de69fbc40',
         'files/nfs-share.sh': '19d15315f91567c2bfde09fbb03bf84141e42ea8'}
MARK = '# LOCAL_NATIVE_ABLIT_GUARD_V1\n'

def blob_sha(data: bytes) -> str:
    return hashlib.sha1(f'blob {len(data)}\0'.encode() + data).hexdigest()

def once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f'expected exactly one original snippet: {old[:100]!r}')
    return text.replace(old, new, 1)

def patch(name: str, text: str) -> str:
    if name == 'start.sh':
        for fn in ('cmd_download', 'cmd_pull', 'cmd_build'):
            text = once(text, f'{fn}() {{\n', f'{fn}() {{\n  die "Abliterated checkout: {fn} disabled; prepare stock/assets separately"\n')
        text = once(text, '  DOCTOR_STRICT=0 cmd_doctor || true\n', '  cmd_doctor strict\n')
        text = once(text, '  [[ -f "$MODEL_DIR/config.json" ]] || cmd_download\n',
                    '  [[ -f "$MODEL_DIR/config.json" && -f "$MODEL_DIR/NATIVE_ABLIT_VERIFIED.json" ]] || die "Missing verified Abliterated checkpoint; no auto-download"\n')
        text = once(text,
            '  if [[ "$need_share" -eq 1 || "$NFS_SHARE" == "1" ]]; then\n    cmd_share\n  fi\n',
            '  [[ "$need_share" -eq 0 && "$NFS_SHARE" == "0" ]] || die "Explicitly prepare/verify Abliterated NFS; auto-share disabled"\n')
        text = once(text, '    -v "$src:/models/DeepSeek-V4.1-Flash"\n',
                    '    -v "$src:/models/DeepSeek-V4.1-Flash:ro"\n')
    else:
        text = once(text, 'docker image inspect alpine:latest >/dev/null 2>&1 || docker pull alpine:latest >/dev/null',
                    'docker image inspect alpine:latest >/dev/null 2>&1 || { echo MISSING_OFFLINE_ALPINE >&2; exit 1; }')
        text = once(text, "alpine:latest test -f /m/config.json", 
                    "alpine:latest sh -c 'test -f /m/config.json && test -f /m/NATIVE_ABLIT_VERIFIED.json'")
    lines = text.splitlines(keepends=True)
    if lines and lines[0].startswith('#!'):
        return lines[0] + MARK + ''.join(lines[1:])
    return MARK + text

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--recipe', type=Path, required=True)
    ap.add_argument('--diff', type=Path, required=True)
    args = ap.parse_args()
    root = args.recipe.resolve(strict=True)
    changes = {}
    for name, expected in BLOBS.items():
        p = root / name
        current = p.read_bytes()
        backup = p.with_name(p.name + '.before-ablit-guard')
        original = backup.read_bytes() if backup.exists() else current
        if blob_sha(original) != expected:
            raise RuntimeError(f'{name}: source does not match pinned blob; refuse to patch')
        result = patch(name, original.decode())
        if current not in (original, result.encode()):
            raise RuntimeError(f'{name}: unrelated local modifications; review manually')
        subprocess.run(['bash', '-n'], input=result, text=True, check=True)
        changes[name] = (original, result, backup)
    diff = ''
    for name, (original, result, backup) in changes.items():
        p = root / name
        if not backup.exists():
            with backup.open('xb') as f:
                f.write(original)
        tmp = p.with_name(p.name + '.ablit-guard.tmp')
        tmp.write_text(result)
        tmp.chmod(p.stat().st_mode & 0o777)
        tmp.replace(p)
        diff += ''.join(difflib.unified_diff(original.decode().splitlines(True), result.splitlines(True),
                                           fromfile='upstream/' + name, tofile='local/' + name))
    args.diff.parent.mkdir(parents=True, exist_ok=True)
    args.diff.write_text(diff)
    print('Launcher guard installed; explicit NFS changes and actual mounted tensors still need validation.')

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f'ERROR: {e}', file=sys.stderr)
        raise SystemExit(1)
