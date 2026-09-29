#!/usr/bin/env python3
"""Receive one immutable telemetry archive on the verified AgentOS NAS volume."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import fcntl
from datetime import date, datetime, timezone

ROOT = Path('/srv/nas/@family/DGXGateway-Records')
UUID = '705084e5-2b86-4687-a114-ce0895ab1eee'


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda: f.read(1024*1024), b''):
            h.update(b)
    return h.hexdigest()


def receive(name, sha, size, stream, root=ROOT):
    if not re.fullmatch(r'dgx-\d{4}-\d{2}-\d{2}(?:-acceptance)?\.jsonl\.gz', name):
        raise ValueError('invalid archive name')
    if not re.fullmatch('[0-9a-f]{64}', sha) or not 0 < size <= 2*1024**3:
        raise ValueError('invalid archive metadata')
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError('NAS destination not provisioned')
    destination = root / name
    with (root / '.receive.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if destination.exists():
            if destination.is_symlink() or destination.stat().st_size != size or sha256(destination) != sha:
                raise RuntimeError('existing archive mismatch; refusing overwrite')
            # Drain input so retrying SSH sender does not see a broken pipe.
            remaining = size
            while remaining:
                block = stream.read(min(1024*1024, remaining))
                if not block:
                    raise ValueError('truncated retry')
                remaining -= len(block)
            return {'filename': name, 'sha256': sha, 'bytes': size, 'already_present': True}
        if shutil.disk_usage(root).free < size + 5*1024**3:
            raise RuntimeError('NAS reserve reached')
        temporary = root / (name + '.partial')
        if temporary.is_symlink():
            raise RuntimeError('invalid staging file')
        try:
            with temporary.open('wb') as out:
                os.chmod(temporary, 0o600)
                remaining = size
                while remaining:
                    block = stream.read(min(1024*1024, remaining))
                    if not block:
                        raise ValueError('truncated archive')
                    out.write(block)
                    remaining -= len(block)
                out.flush()
                os.fsync(out.fileno())
            if sha256(temporary) != sha:
                raise ValueError('checksum mismatch')
            os.replace(temporary, destination)
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        return {'filename': name, 'sha256': sha, 'bytes': size}


def prune(root=ROOT, *, as_of=None, dry_run=False):
    """Calendar retention: current UTC archive month plus previous month."""
    today = as_of or datetime.now(timezone.utc).date()
    cutoff = date(today.year - (today.month == 1), 12 if today.month == 1 else today.month - 1, 1)
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError('NAS destination not provisioned')
    with (root / '.receive.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        candidates = []
        for p in sorted(root.iterdir()):
            match = re.fullmatch(r'dgx-(\d{4}-\d{2}-\d{2})(?:-acceptance)?\.jsonl\.gz', p.name)
            if not match or p.is_symlink() or not p.is_file():
                continue
            try:
                archive_day = date.fromisoformat(match.group(1))
            except ValueError:
                continue
            if archive_day < cutoff:
                candidates.append({'filename': p.name, 'bytes': p.stat().st_size})
        result = {'month': today.strftime('%Y-%m'), 'cutoff': cutoff.isoformat(),
                  'dry_run': dry_run, 'files': candidates, 'deleted_count': 0}
        if dry_run:
            return result
        # Persist the plan before unlink; an interrupted cleanup remains auditable.
        audit = root / ('retention-' + today.strftime('%Y-%m') + '.jsonl')
        if audit.is_symlink():
            raise RuntimeError('invalid audit file')
        with audit.open('a') as out:
            os.chmod(audit, 0o600)
            out.write(json.dumps(dict(result, phase='planned')) + '\n')
            out.flush()
            os.fsync(out.fileno())
            for item in candidates:
                (root / item['filename']).unlink()
                result['deleted_count'] += 1
            out.write(json.dumps(dict(result, phase='completed')) + '\n')
            out.flush()
            os.fsync(out.fileno())
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return result


def main():
    mounted = subprocess.check_output(['findmnt', '-n', '-o', 'UUID', '--mountpoint', '/srv/nas'], text=True).strip()
    if mounted != UUID or not Path('/srv/nas/.nas-online').is_file():
        raise RuntimeError('expected NAS not mounted')
    if sys.argv[1:] in (["--prune"], ["--prune", "--dry-run"]):
        print(json.dumps(prune(dry_run="--dry-run" in sys.argv)))
        return
    name, sha, size = sys.argv[1:]
    print(json.dumps(receive(name, sha, int(size), sys.stdin.buffer)))


if __name__ == '__main__':
    main()
