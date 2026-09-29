#!/usr/bin/env python3
"""CPU/stdlib-only checks for the 52-tensor native FP8 graft; never loads an LLM."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import struct
import sys
import time

WANTED = {f'layers.{i}.attn.wo_b.{s}' for i in range(10, 36) for s in ('weight', 'scale')}
GIB = 1024 ** 3

def fail(message: str):
    raise RuntimeError(message)

def read_json(p: Path):
    return json.loads(p.read_text())

def file_hash(p: Path) -> str:
    h = hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda: f.read(8 * 1024 ** 2), b''):
            h.update(b)
    return h.hexdigest()

def header(p: Path):
    size = p.stat().st_size
    with p.open('rb') as f:
        raw = f.read(8)
        if len(raw) != 8:
            fail(f'truncated safetensors: {p}')
        n = struct.unpack('<Q', raw)[0]
        if not 2 <= n <= min(100 * 1024 ** 2, size - 8):
            fail(f'invalid header length: {p}')
        def unique(items):
            out = {}
            for key, value in items:
                if key in out:
                    fail(f'duplicate JSON key: {p}: {key}')
                out[key] = value
            return out
        data = json.loads(f.read(n), object_pairs_hook=unique)
    if not isinstance(data, dict):
        fail(f'header is not an object: {p}')
    tensors = {k: v for k, v in data.items() if k != '__metadata__'}
    spans = []
    for k, v in tensors.items():
        if not isinstance(v, dict):
            fail(f'invalid tensor record: {k}')
        a, b = v['data_offsets']
        shape = v['shape']
        if not isinstance(shape, list) or any(type(x) is not int or x < 0 for x in shape):
            fail(f'invalid shape: {k}')
        if type(a) is not int or type(b) is not int or not 0 <= a <= b <= size - 8 - n:
            fail(f'invalid offsets: {k}')
        if not isinstance(v['dtype'], str):
            fail(f'invalid dtype: {k}')
        if b > a:
            spans.append((a, b))
    cursor = 0
    for a, b in sorted(spans):
        if a != cursor:
            fail(f'overlap/gap in data buffer: {p}')
        cursor = b
    if cursor != size - 8 - n:
        fail(f'unindexed trailing payload: {p}')
    return tensors, 8 + n

def tensor_hash(p: Path, start: int, meta: dict) -> str:
    a, b = meta['data_offsets']
    remaining = b - a
    h = hashlib.sha256()
    with p.open('rb') as f:
        f.seek(start + a)
        while remaining:
            buf = f.read(min(8 * 1024 ** 2, remaining))
            if not buf:
                fail(f'truncated payload: {p}')
            h.update(buf)
            remaining -= len(buf)
    return h.hexdigest()

def same_layout(a: dict, b: dict) -> bool:
    return a['shape'] == b['shape'] and a['dtype'] == b['dtype'] and (
        a['data_offsets'][1] - a['data_offsets'][0] == b['data_offsets'][1] - b['data_offsets'][0])

def checkpoint(root: Path):
    config = read_json(root / 'config.json')
    if config.get('model_type') != 'deepseek_v41':
        fail('wrong model_type; expected deepseek_v41')
    wm = read_json(root / 'model.safetensors.index.json')['weight_map']
    if not isinstance(wm, dict) or not WANTED.issubset(wm):
        fail('native FP8 index missing one or more of the 52 target tensors')
    shards = sorted(set(wm.values()))
    if len(shards) != 48:
        fail('expected fixed native checkpoint with 48 indexed shards')
    headers = {}
    for name in shards:
        if Path(name).name != name or not name.endswith('.safetensors'):
            fail(f'unsafe/unexpected shard path: {name}')
        p = root / name
        if not p.is_file() or p.is_symlink():
            fail(f'missing shard or unsupported symbolic link: {p}')
        headers[name] = header(p)
        expected = {k for k, v in wm.items() if v == name}
        if set(headers[name][0]) != expected:
            fail(f'index/header tensor set mismatch: {name}')
    return wm, headers

def patch_info(p: Path):
    h, offset = header(p)
    if set(h) != WANTED:
        fail('patch must contain exactly the 52 native wo_b weight/scale tensors; not Mia EXL3')
    for k in WANTED:
        if k.endswith('.weight') and h[k]['dtype'] not in ('F8_E4M3', 'F8_E4M3FN', 'F8_E5M2'):
            fail(f'expected native FP8 weight, got {h[k]["dtype"]}: {k}')
    return h, offset

def dump(p: Path, value: dict):
    p.parent.mkdir(parents=True, exist_ok=True)
    t = p.with_name(p.name + '.tmp')
    t.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    t.replace(p)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('mode', choices=('plan', 'verify', 'mounted'))
    ap.add_argument('--stock', type=Path)
    ap.add_argument('--target', type=Path, required=True)
    ap.add_argument('--patch', type=Path, required=True)
    ap.add_argument('--receipt', type=Path)
    ap.add_argument('--reserve-gib', type=float, default=20)
    args = ap.parse_args()
    target = args.target.absolute()
    ph, po = patch_info(args.patch)
    patch_sha = file_hash(args.patch)
    if args.mode == 'mounted':
        wm, hs = checkpoint(target)
        for k in sorted(WANTED):
            name = wm[k]; dh, do = hs[name]
            if not same_layout(ph[k], dh[k]) or tensor_hash(args.patch, po, ph[k]) != tensor_hash(target / name, do, dh[k]):
                fail(f'mounted checkpoint is not this patch: {k}')
        meta = read_json(target / 'ABLIT_META.json')
        receipt = read_json(target / 'NATIVE_ABLIT_VERIFIED.json')
        if meta.get('method') != 'graft-keys-l10-35-wo_b' or meta.get('n_edited') != 52:
            fail('invalid native ABLIT_META')
        if receipt.get('patch_sha256') != patch_sha or receipt.get('target_tensors_checked') != 52:
            fail('verification receipt missing/wrong patch')
        print(json.dumps({'mounted_patch_pass': True, 'patch_sha256': patch_sha, 'tensors': 52}))
        return
    if args.stock is None:
        fail('--stock is required')
    stock = args.stock.resolve(strict=True)
    resolved_target = target.resolve()
    if resolved_target == stock or stock in resolved_target.parents or resolved_target in stock.parents:
        fail('stock/target must be separate non-nested directories')
    if args.mode == 'verify':
        expected_receipt = target / 'NATIVE_ABLIT_VERIFIED.json'
        if args.receipt is None or args.receipt.absolute() != expected_receipt:
            fail('verify receipt must be TARGET/NATIVE_ABLIT_VERIFIED.json')
        if expected_receipt.is_symlink():
            fail('receipt must not be a symlink')
        if expected_receipt.exists():
            expected_receipt.unlink()  # Never leave an old success marker after failed re-verification.
    wm, sh = checkpoint(stock)
    for k in WANTED:
        if not same_layout(ph[k], sh[wm[k]][0][k]):
            fail(f'patch/stock shape, dtype or bytes mismatch: {k}')
    touched = sorted({wm[k] for k in WANTED})
    touched_bytes = sum((stock / n).stat().st_size for n in touched)
    largest = max((stock / n).stat().st_size for n in touched)
    if args.mode == 'plan':
        if target.exists() or target.is_symlink():
            fail('build target already exists; use a NEW directory, never resume graft in-place')
        parent = target.parent
        while not parent.exists():
            parent = parent.parent
        if parent.stat().st_dev != stock.stat().st_dev:
            fail('source and derivative must be on same filesystem for this space-saving plan')
        free = shutil.disk_usage(parent).free
        reserve = math.ceil(args.reserve_gib * GIB)
        required = touched_bytes + largest + reserve
        result = {'patch_sha256': patch_sha, 'target_tensors': 52, 'touched_shards': touched,
                  'rewritten_shard_bytes': touched_bytes, 'largest_rewritten_shard_bytes': largest,
                  'disk_required_with_reserve': required, 'disk_free_bytes': free,
                  'rough_cpu_working_set_bytes': 2 * largest + args.patch.stat().st_size + 4 * GIB,
                  'disk_check_pass': free >= required}
        if args.receipt:
            dump(args.receipt, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if free < required:
            fail('insufficient disk for rewritten whole shards; do not delete last old fallback automatically')
        return
    twm, th = checkpoint(target)
    if twm != wm:
        fail('weight_map changed')
    checked = 0
    unchanged = 0
    for name in sorted(sh):
        a, ao = sh[name]; b, bo = th[name]
        if set(a) != set(b):
            fail(f'tensor set changed: {name}')
        shared_inode = os.path.samefile(stock / name, target / name)
        if name in touched and shared_inode:
            fail(f'edited shard still hardlinked to stock: {name}')
        for k in sorted(a):
            if not same_layout(a[k], b[k]):
                fail(f'stock layout changed: {k}')
            if k in WANTED:
                expected = tensor_hash(args.patch, po, ph[k])
                checked += 1
            elif shared_inode:
                unchanged += 1
                continue
            else:
                expected = tensor_hash(stock / name, ao, a[k])
                unchanged += 1
            if tensor_hash(target / name, bo, b[k]) != expected:
                fail(f'tensor bytes mismatch: {k}')
    for name in ('config.json', 'model.safetensors.index.json', 'tokenizer.json',
                 'tokenizer_config.json', 'generation_config.json', 'chat_template.jinja'):
        if (stock / name).exists() and (
            not (target / name).is_file() or file_hash(stock / name) != file_hash(target / name)):
            fail(f'config/tokenizer/template changed: {name}')
    meta = read_json(target / 'ABLIT_META.json')
    if meta.get('method') != 'graft-keys-l10-35-wo_b' or meta.get('n_edited') != 52:
        fail('expected method=graft-keys-l10-35-wo_b and n_edited=52')
    result = {'check': 'native-52-tensor-graft', 'patch_sha256': patch_sha,
              'target_tensors_checked': checked, 'unchanged_tensors_checked': unchanged,
              'touched_shards': touched, 'verified_at': time.time(),
              'stock_path_at_check': str(stock), 'target_path_at_check': str(target),
              'runtime_compatibility_tested': False, 'quality_tested': False}
    if args.receipt is None:
        fail('--receipt is required for verify')
    dump(args.receipt, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f'ERROR: {e}', file=sys.stderr)
        raise SystemExit(1)
