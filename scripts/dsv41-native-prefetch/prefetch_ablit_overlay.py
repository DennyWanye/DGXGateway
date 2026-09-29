#!/usr/bin/env python3
"""Lock/download only the native FP8 overlay; no model loading or old-file deletion."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import urllib.request

REPO = 'drowzeys/DeepSeek-V4.1-Flash-Abliterated-Cybersecurity-Unleashed'
PATCH = 'wo_b_l10_35.safetensors'
DOCS = ('README.md', 'INSTALL.md', 'ABLIT.md', 'ABLIT_META.json', 'RESPONSIBLE_USE.md')
HELPER = 'drowzeys/keys-DeepSeekV4.1-Flash-TR3-Hybrid-1M-Context-8M-KV-Four-DGX-Sparks-with-abliterated-option'
COMMIT = '4b69803bf6eca380a775e40b322ac2542dae3a59'
SCRIPT_BLOB = 'af9120da18f9efb745c85c958bf0e8652c8363cd'
SCRIPT = 'apply_wo_b_graft.py'
GIB = 1024 ** 3

def digest(p: Path, kind: str = 'sha256') -> str:
    h = hashlib.sha1() if kind == 'git-blob-sha1' else hashlib.sha256()
    if kind == 'git-blob-sha1':
        h.update(f'blob {p.stat().st_size}\0'.encode())
    with p.open('rb') as f:
        for b in iter(lambda: f.read(8 * 1024 ** 2), b''):
            h.update(b)
    return h.hexdigest()

def atomic_json(p: Path, data: dict) -> None:
    tmp = p.with_name(p.name + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(p)

def field(obj, name):
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)

def check_network() -> None:
    if os.environ.get('DIRECT_NETWORK_CONFIRMED') != '1':
        raise RuntimeError('先检查系统路由和Docker出口；确认后设置 DIRECT_NETWORK_CONFIRMED=1')
    if any(os.environ.get(k) for k in ('http_proxy','https_proxy','all_proxy','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY')):
        raise RuntimeError('检测到代理变量；不自动改用代理')
    if os.environ.get('HF_ENDPOINT', 'https://huggingface.co').rstrip('/') != 'https://huggingface.co':
        raise RuntimeError('本方案只使用 Hugging Face 官方入口')

def matches(p: Path, e: dict) -> bool:
    return p.is_file() and p.stat().st_size == e['size'] and digest(p, e['kind']) == e['hash']

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--asset-dir', required=True, type=Path)
    ap.add_argument('--revision', default=None, help='首次锁定时可指定HF完整commit；默认解析当时main，随后不追更')
    ap.add_argument('--source-overlay', type=Path, help='已存在的原生overlay文件或目录；不是Mia EXL3文件')
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--download', action='store_true')
    mode.add_argument('--offline-verify', action='store_true')
    a = ap.parse_args()
    root = a.asset_dir.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    lockfile = root / 'overlay.lock.json'
    if lockfile.exists():
        lock = json.loads(lockfile.read_text())
        if lock.get('repo') != REPO or not re.fullmatch('[0-9a-f]{40}', lock.get('revision', '')):
            raise RuntimeError('已有锁文件身份不正确')
        if a.revision and a.revision != lock['revision']:
            raise RuntimeError('请求版本与已有锁不同；为新版本建立另一资产目录，不覆盖')
    else:
        if a.offline_verify:
            raise RuntimeError('离线验证需要先取得 overlay.lock.json')
        check_network()
        from huggingface_hub import HfApi
        info = HfApi(token=True).model_info(REPO, revision=a.revision or 'main', files_metadata=True)
        if not re.fullmatch('[0-9a-f]{40}', info.sha or ''):
            raise RuntimeError('没有取得可锁定的完整HF revision')
        siblings = {x.rfilename: x for x in info.siblings}
        if PATCH not in siblings:
            raise RuntimeError('目标版本缺少原生FP8 overlay，不能改下Mia EXL3 overlay代替')
        entries = []
        for name in (PATCH, *DOCS):
            if name not in siblings:
                continue
            s = siblings[name]
            lfs = getattr(s, 'lfs', None)
            sha = field(lfs, 'sha256') if lfs else None
            kind = 'sha256' if sha else 'git-blob-sha1'
            sha = sha or getattr(s, 'blob_id', None)
            size = getattr(s, 'size', None)
            if not isinstance(size, int) or size < 0 or not isinstance(sha, str):
                raise RuntimeError(f'缺少完整文件元数据: {name}')
            if not re.fullmatch('[0-9a-f]{64}' if kind == 'sha256' else '[0-9a-f]{40}', sha):
                raise RuntimeError(f'无法验证hash: {name}')
            if name == PATCH and kind != 'sha256':
                raise RuntimeError('原生overlay需要LFS SHA256；不能仅按大小下载')
            entries.append({'name': name, 'size': size, 'kind': kind, 'hash': sha})
        lock = {'repo': REPO, 'revision': info.sha, 'files': entries,
                'helper_repo': HELPER, 'helper_commit': COMMIT, 'helper_git_blob': SCRIPT_BLOB}
        atomic_json(lockfile, lock)
    if (lock.get('helper_repo'), lock.get('helper_commit'), lock.get('helper_git_blob')) != (HELPER, COMMIT, SCRIPT_BLOB):
        raise RuntimeError('锁文件中的应用脚本身份与本文不一致')
    names = [e['name'] for e in lock['files']]
    if PATCH not in names or len(set(names)) != len(names) or any(n not in (PATCH, *DOCS) for n in names):
        raise RuntimeError('非法下载清单；拒绝不必要的模型或EXL3文件')
    pending = []
    for e in lock['files']:
        p = root / e['name']
        if p.is_symlink():
            raise RuntimeError(f'目标不允许符号链接: {p}')
        if p.exists() and not matches(p, e):
            raise RuntimeError(f'已有完整文件hash不符，不覆盖: {p}')
        if not p.exists():
            pending.append(e)
    script = root / SCRIPT
    if script.is_symlink() or (script.exists() and digest(script, 'git-blob-sha1') != SCRIPT_BLOB):
        raise RuntimeError('已有apply脚本不等于锁定Git blob，不执行也不覆盖')
    print(json.dumps({'revision': lock['revision'], 'missing_files': [e['name'] for e in pending],
                      'missing_file_bytes': sum(e['size'] for e in pending),
                      'script_needed': not script.exists()}, ensure_ascii=False))
    if a.offline_verify and (pending or not script.exists()):
        raise RuntimeError('资产不完整')
    if not a.download and not a.offline_verify:
        print('仅生成下载计划/锁；未下载overlay大文件。使用 --download 执行。')
        return 0
    if a.download:
        if shutil.disk_usage(root).free < sum(e['size'] for e in pending) + 2 * GIB:
            raise RuntimeError('下载目录空间不足，另需2GiB余量')
        for e in pending:
            dest = root / e['name']
            src = a.source_overlay.expanduser() if a.source_overlay else None
            if src and src.is_dir():
                src = src / e['name']
            if src and src.is_file() and matches(src, e):
                try:
                    os.link(src.resolve(), dest)
                except OSError:
                    shutil.copy2(src, dest)
            else:
                check_network()
                from huggingface_hub import hf_hub_download
                hf_hub_download(REPO, e['name'], revision=lock['revision'], local_dir=root, token=True)
            if not matches(dest, e):
                raise RuntimeError(f'下载/复用后验证失败: {e["name"]}')
        if not script.exists():
            check_network()
            url = f'https://raw.githubusercontent.com/{HELPER}/{COMMIT}/serve/{SCRIPT}'
            req = urllib.request.Request(url, headers={'User-Agent': 'dsv41-asset-preparer'})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise RuntimeError('应用脚本响应大小异常')
            blob = hashlib.sha1(f'blob {len(data)}\0'.encode() + data).hexdigest()
            if blob != SCRIPT_BLOB:
                raise RuntimeError('下载的脚本与锁定Git blob不一致')
            tmp = script.with_name(script.name + '.partial')
            tmp.write_bytes(data)
            tmp.replace(script)
    patch_sha = digest(root / PATCH)
    receipt = {'repo': REPO, 'revision': lock['revision'], 'patch_sha256': patch_sha,
               'script_commit': COMMIT, 'script_git_blob': SCRIPT_BLOB,
               'script_sha256': digest(script), 'verified_at': time.time()}
    atomic_json(root / 'overlay-verified.json', receipt)
    print('OVERLAY_ASSETS_VERIFIED', root)
    return 0

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (Exception, KeyboardInterrupt) as e:
        print(f'STOP: {e}. 401/403时检查HF授权及本机登录；不要把token发到聊天。', file=sys.stderr)
        raise SystemExit(1)
