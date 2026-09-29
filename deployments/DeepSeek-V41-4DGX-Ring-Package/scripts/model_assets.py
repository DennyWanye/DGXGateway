#!/usr/bin/env python3
"""Pinned HF download and SHA verification. Never edits/requantizes old model files."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from common import MODEL_REPO, MODEL_REV, load_config

CHUNK = 8 * 1024**2

def file_digest(path: Path, algorithm: str, git_blob: bool = False) -> str:
    h = hashlib.new(algorithm)
    if git_blob:
        h.update(f'blob {path.stat().st_size}\0'.encode())
    with path.open('rb') as f:
        while block := f.read(CHUNK):
            h.update(block)
    return h.hexdigest()

def match_file(path: Path, item: dict) -> bool:
    if not path.is_file() or path.stat().st_size != item['size']:
        return False
    if item.get('sha256'):
        return file_digest(path, 'sha256') == item['sha256']
    return file_digest(path, 'sha1', git_blob=True) == item['blob_id']

def valid_relative(name: str) -> bool:
    return bool(name) and not name.startswith('/') and '..' not in Path(name).parts and '\\' not in name

def validate_manifest(m: dict) -> None:
    if (m.get('repository'), m.get('revision')) != (MODEL_REPO, MODEL_REV):
        raise ValueError('Manifest is not the pinned official DeepSeek V4.1 Flash checkpoint')
    names = []
    for f in m['files']:
        if not valid_relative(f['path']) or not isinstance(f['size'], int) or f['size'] < 0:
            raise ValueError('Invalid manifest file entry')
        sha = f.get('sha256')
        blob = f.get('blob_id')
        expected, length = (sha, 64) if sha else (blob, 40)
        if not isinstance(expected, str) or len(expected) != length or any(c not in '0123456789abcdef' for c in expected):
            raise ValueError(f"No verifiable hash for {f['path']}")
        names.append(f['path'])
    if len(set(names)) != len(names):
        raise ValueError('Duplicate manifest paths')
    for required in ['config.json', 'model.safetensors.index.json',
                     'model-00047-of-00048.safetensors', 'model-00048-of-00048.safetensors']:
        if required not in names:
            raise ValueError(f'Incomplete manifest: {required}')

def get_manifest(path: Path, create: bool) -> dict:
    if path.exists():
        m = json.loads(path.read_text())
        validate_manifest(m)
        return m
    if not create:
        raise RuntimeError(f'Manifest missing: {path}; run plan on rank 0 and copy the manifest first')
    # Import only for online metadata/download. Offline verification uses stdlib only.
    from huggingface_hub import HfApi
    info = HfApi().model_info(MODEL_REPO, revision=MODEL_REV, files_metadata=True)
    if info.sha != MODEL_REV:
        raise RuntimeError('HF returned a different commit; refusing to follow main')
    rows = []
    for s in info.siblings:
        lfs = s.lfs
        sha = (lfs.get('sha256') if isinstance(lfs, dict) else getattr(lfs, 'sha256', None)) if lfs else None
        rows.append({'path': s.rfilename, 'size': s.size, 'sha256': sha, 'blob_id': s.blob_id})
    m = {'repository': MODEL_REPO, 'revision': MODEL_REV, 'files': sorted(rows, key=lambda f: f['path'])}
    validate_manifest(m)
    path.parent.mkdir(parents=True, exist_ok=True)
    # No overwrite of a concurrently created lock.
    with path.open('x') as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
    return m

def check_stamp(stamp: Path, model: Path, manifest: Path) -> None:
    s = json.loads(stamp.read_text())
    if s.get('revision') != MODEL_REV or s.get('model_dir') != str(model.resolve()):
        raise RuntimeError('Verification stamp belongs to another model path or revision')
    if s['manifest_sha256'] != file_digest(manifest, 'sha256'):
        raise RuntimeError('Manifest changed after verification; re-run full verify')
    for item in s['files']:
        p = model / item['path']
        st = p.stat()
        if (st.st_size, st.st_mtime_ns) != (item['size'], item['mtime_ns']):
            raise RuntimeError(f'File changed after verification: {p}; re-run full verify')
    # This is a quick operational check, not a tamper-proof substitute for SHA hashing.

def verify_all(m: dict, model: Path, manifest: Path, stamp: Path) -> None:
    rows = []
    for i, item in enumerate(m['files'], 1):
        p = model / item['path']
        print(f"[{i}/{len(m['files'])}] SHA verify {item['path']}", flush=True)
        if not match_file(p, item):
            raise RuntimeError(f'Bad/missing model file: {p}; no automatic replacement or deletion')
        st = p.stat()
        rows.append({'path': item['path'], 'size': st.st_size, 'mtime_ns': st.st_mtime_ns})
    config = json.loads((model / 'config.json').read_text())
    if config.get('model_type') != 'deepseek_v41' or config.get('quantization_config', {}).get('expert_dtype') != 'fp4':
        raise RuntimeError('Unexpected model architecture or quantization; do not substitute EXL3')
    out = {'repository': MODEL_REPO, 'revision': MODEL_REV, 'model_dir': str(model.resolve()),
           'manifest_sha256': file_digest(manifest, 'sha256'), 'files': rows}
    stamp.parent.mkdir(parents=True, exist_ok=True)
    temp = stamp.with_suffix('.tmp')
    temp.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    os.replace(temp, stamp)
    print(f'MODEL_HASH_PASS files={len(rows)} stamp={stamp}')

def reuse_one(src: Path, dst: Path, method: str) -> None:
    if dst.exists():
        raise RuntimeError(f'Destination unexpectedly exists: {dst}')
    dst.parent.mkdir(parents=True, exist_ok=True)
    if method == 'hardlink':
        # Explicit operator choice. Never chmod or mutate either link afterwards.
        os.link(src.resolve(), dst)
    else:
        tmp = dst.with_name(dst.name + '.reuse-in-progress')
        if tmp.exists():
            raise RuntimeError(f'Old incomplete copy requires inspection: {tmp}')
        subprocess.run(['cp', '--reflink=auto', '--sparse=always', '--', str(src), str(tmp)], check=True)
        os.replace(tmp, dst)

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['plan', 'download', 'verify'])
    p.add_argument('--config', default='config/cluster.json')
    p.add_argument('--rank', type=int, choices=range(4), default=0)
    p.add_argument('--reuse', action='append', default=[], help='Existing source directory; may be repeated')
    p.add_argument('--reuse-method', choices=['copy', 'hardlink'], default='copy')
    a = p.parse_args()
    c = load_config(a.config)
    root, model = Path(c['root']), Path(c['model_dir'])
    manifest = root / 'manifests/model-lock.json'
    m = get_manifest(manifest, create=a.action == 'plan')
    total = sum(f['size'] for f in m['files'])
    print(f'Pinned {MODEL_REPO}@{MODEL_REV}\nFiles={len(m["files"])} bytes={total:,} GB={total/1e9:.2f} GiB={total/1024**3:.2f}')
    if a.action == 'plan':
        for f in m['files']:
            existing = model / f['path']
            hints = [str(Path(d) / f['path']) for d in a.reuse if (Path(d)/f['path']).is_file()]
            status = 'exists (NOT YET HASHED)' if existing.is_file() else 'missing'
            print(f"{f['size']:>14,}  {f['path']}  {status}" + (f'  reuse candidates={hints}' if hints else ''))
        print('PLAN ONLY: no weights downloaded. Candidate filenames do not establish compatibility.')
        return
    if a.action == 'verify':
        verify_all(m, model, manifest, root / 'manifests' / f'model-verified-r{a.rank}.json')
        return
    model.mkdir(parents=True, exist_ok=True)
    from huggingface_hub import hf_hub_download
    for i, f in enumerate(m['files'], 1):
        dst = model / f['path']
        print(f"[{i}/{len(m['files'])}] {f['path']}", flush=True)
        if dst.exists():
            if not match_file(dst, f):
                raise RuntimeError(f'Destination has wrong hash: {dst}. Inspect/move aside manually; never overwrite old hardlinked assets')
            print('  verified existing; skipped')
            continue
        reused = False
        for d in a.reuse:
            src = Path(d) / f['path']
            if match_file(src, f):
                if a.reuse_method == 'copy' and shutil.disk_usage(model).free < f['size'] + 40 * 1024**3:
                    raise RuntimeError('Insufficient conservative copy budget (file +40 GiB). Review storage/reflink/hardlink choice')
                reuse_one(src, dst, a.reuse_method)
                print(f'  reused verified source: {src} ({a.reuse_method})')
                reused = True
                break
        if not reused:
            # Budget per file, not an assurance the entire remaining download will fit.
            if shutil.disk_usage(model).free < f['size'] + 40 * 1024**3:
                raise RuntimeError('Insufficient free space for next file +40 GiB safety margin; do not delete old deployment')
            hf_hub_download(MODEL_REPO, filename=f['path'], revision=MODEL_REV, local_dir=str(model))
        if not match_file(dst, f):
            raise RuntimeError(f'Post-copy/download hash mismatch: {dst}')
    verify_all(m, model, manifest, root / 'manifests' / f'model-verified-r{a.rank}.json')

if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyError, subprocess.CalledProcessError) as exc:
        print(f'STOP: {exc}', file=sys.stderr)
        sys.exit(2)
