"""Bounded, batched telemetry journal and daily verified NAS archives.

No model secrets, environment variables, prompts or media enter this journal.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import threading
import time
import tomllib
from datetime import date, datetime, timezone
from concurrent.futures import ThreadPoolExecutor

from .telemetry import utc_now

DIAGNOSTIC_PROBE = r'''
import json, pathlib, socket, pwd, os, subprocess, sys
host,user,since=sys.argv[1:]
assert socket.gethostname()==host and pwd.getpwuid(os.getuid()).pw_name==user
def read(p):
 try: return pathlib.Path(p).read_text()[:65536]
 except OSError: return None
def run(a):
 try:
  p=subprocess.run(a,capture_output=True,text=True,timeout=5)
  return {'code':p.returncode,'text':p.stdout[-65536:],'truncated':len(p.stdout)>65536}
 except Exception as e: return {'error':type(e).__name__}
# Kernel-only journal: excludes application prompts and authentication logs.
j=run(['sudo','-n','journalctl','-k','--since',since,'-n','300','-o','json','--no-pager'])
events=[]
for line in j.get('text','').splitlines():
 try:
  x=json.loads(line);msg=x.get('MESSAGE','')
  if any(s in msg.lower() for s in ('oom','out of memory','killed process','nvrm','xid','nvme','i/o error','thermal','link is down')):
   events.append({k:x.get(k) for k in ('__CURSOR','__REALTIME_TIMESTAMP','_BOOT_ID','MESSAGE')})
 except (ValueError,TypeError): pass
print(json.dumps({'hostname':host,'meminfo':read('/proc/meminfo'),'vmstat':read('/proc/vmstat'),
 'pressure':{k:read('/proc/pressure/'+k) for k in ('memory','cpu','io')},
 'cpu_stat':read('/proc/stat'),'diskstats':read('/proc/diskstats'),'network':read('/proc/net/dev'),
 'kernel':os.uname().release,'kernel_events':events,'journal_code':j.get('code'),
 'journal_error':j.get('error'),'journal_limit':300,'journal_truncated':j.get('truncated',False) or len(j.get('text','').splitlines())>=300,
 'processes':run(['ps','-eo','pid,ppid,comm,rss,%cpu','--sort=-rss']),
 'containers':run(['sudo','-n','docker','ps','-a','--format','{{.Names}}\t{{.Status}}']),
 'gpu_processes':run(['nvidia-smi','--query-compute-apps=pid,process_name,used_memory','--format=csv,noheader'])}))
'''


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with open(temporary, 'w', encoding='utf-8') as f:
        os.chmod(temporary, 0o600)
        json.dump(value, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)


def _sampled_ms(value):
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _project_sample(row):
    nodes = []
    for node in (row.get("nodes") or []):
        nodes.append({
            "role": node.get("role"),
            "hostname": node.get("hostname"),
            "address": node.get("address"),
            "online": node.get("online"),
            "stale": node.get("stale"),
            "gpu": node.get("gpu"),
            "unified_memory": node.get("unified_memory"),
        })
    return {"sampled_at": row.get("sampled_at"), "nodes": nodes}


class RecordingService:
    def __init__(self, config, snapshot, options=None):
        self.config, self.snapshot = config, snapshot
        self.options = options or {}
        self.root = config.state_dir / 'recordings'
        self.sample_seconds = max(15, int(self.options.get('sample_seconds', 15)))
        self.flush_seconds = max(60, int(self.options.get('flush_seconds', 300)))
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.buffer = []
        self.buffer_bytes = 0
        self.recent = []
        self.state = {'enabled': True, 'sample_seconds': self.sample_seconds,
                      'flush_seconds': self.flush_seconds, 'nas_configured': bool(self.options.get('nas_host'))}
        self.last_flush = time.monotonic()
        self.last_diag = 0
        self.diag_since = '-2 minutes'
        self.threads = []
        self.event_cursors = set()

    def start(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Read a bounded tail once on startup, never reread the whole journal per request.
        journals = sorted(self.root.glob('????-??-??.jsonl'))
        if journals:
            with journals[-1].open('rb') as f:
                size = f.seek(0, 2)
                f.seek(max(0, size - 4 * 1024 * 1024))
                lines = f.read().splitlines()
            for line in lines:
                try:
                    row = json.loads(line)
                    self.recent.append(row)
                except (ValueError, UnicodeError):
                    continue
            self.recent = self.recent[-240:]
        for target in (self._record_loop, self._backup_loop):
            t = threading.Thread(target=target, daemon=True)
            self.threads.append(t)
            t.start()

    def stop(self):
        self.stop_event.set()
        for t in self.threads:
            t.join(timeout=20)
        self.flush()

    def status(self):
        with self.lock:
            return dict(self.state, buffered_samples=len(self.buffer))

    def history(self):
        with self.lock:
            return list(self.recent)

    def range(self, start_ms: int, end_ms: int, limit: int = 1440) -> dict:
        """Return downsampled telemetry frames for a UTC millisecond range.

        Journal rows interleave large diagnostics snapshots. Stream only the
        matching daily files and JSON-parse rows whose sampled_at is inside the
        window. Rows are projected to the fields the dashboard needs.
        """
        limit = max(1, min(int(limit), 5000))
        if end_ms <= start_ms:
            return {"samples": [], "range": {
                "start": start_ms, "end": end_ms, "bucket_ms": 1,
                "available_start": None, "available_end": None,
            }}
        start_date = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).date().isoformat()
        end_date = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).date().isoformat()
        files = sorted(
            path for path in self.root.glob("????-??-??.jsonl")
            if start_date <= path.stem <= end_date
        )
        bucket_ms = max(1, (end_ms - start_ms) // limit)
        buckets = {}
        available_start = None
        available_end = None
        for path in files:
            with path.open("rb") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    sampled = _sampled_ms(row.get("sampled_at"))
                    if sampled is None:
                        continue
                    if available_start is None or sampled < available_start:
                        available_start = sampled
                    if available_end is None or sampled > available_end:
                        available_end = sampled
                    if sampled < start_ms or sampled > end_ms:
                        continue
                    bucket = (sampled - start_ms) // bucket_ms
                    previous = buckets.get(bucket)
                    if previous is None or sampled >= previous[0]:
                        buckets[bucket] = (sampled, _project_sample(row))
        samples = [buckets[key][1] for key in sorted(buckets)][:limit]
        return {"samples": samples, "range": {
            "start": start_ms, "end": end_ms, "bucket_ms": bucket_ms,
            "available_start": available_start, "available_end": available_end,
        }}

    def collect(self):
        value = self.snapshot.build()
        # Explicit projection: active_connection contains credentials and is forbidden.
        row = {k: value.get(k) for k in ('sampled_at', 'nodes', 'models', 'active_local_model')}
        row['schema_version'] = 1
        for key in ('current_operation', 'latest_operation'):
            op = value.get(key) or {}
            row[key] = {k: op.get(k) for k in ('operation_id', 'phase', 'outcome', 'target_model', 'target_profile')}
        if time.monotonic() - self.last_diag >= 60:
            since = self.diag_since
            before = utc_now()
            def probe(node):
                try:
                    result = self.snapshot.telemetry.ssh.run(node, ['python3', '-c', DIAGNOSTIC_PROBE,
                        node.hostname, node.user, since], timeout=30)
                    return json.loads(result.stdout) if result.returncode == 0 else {'hostname': node.hostname, 'error': 'probe_failed'}
                except Exception as e:
                    return {'hostname': node.hostname, 'error': type(e).__name__}
            with ThreadPoolExecutor(max_workers=2) as pool:
                row['diagnostics'] = list(pool.map(probe, self.config.nodes))
            self.last_diag = time.monotonic()
            # Overlap to avoid boundary loss; kernel cursor identifies duplicates.
            if all(n.get("journal_code") == 0 and not n.get("journal_truncated") for n in row["diagnostics"]):
                self.diag_since = before
        return row

    def append(self, row):
        encoded = json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n'
        with self.lock:
            self.buffer.append((datetime.now(timezone.utc).date().isoformat(), encoded))
            self.buffer_bytes += len(encoded.encode())
            self.recent.append(row)
            self.recent = self.recent[-240:]
            self.state['last_sample_at'] = row.get('sampled_at')
        if self.buffer_bytes >= 4 * 1024 * 1024 or time.monotonic() - self.last_flush >= self.flush_seconds:
            self.flush()

    def flush(self):
        with self.lock:
            if not self.buffer:
                return
            # Never remove unbacked history to make room. Surface storage exhaustion.
            if shutil.disk_usage(self.root).free < 512 * 1024 * 1024:
                self.state['error'] = 'local_disk_low_recording_paused'
                raise OSError('local disk reserve reached')
            days = sorted({day for day, _ in self.buffer})
            for day in days:
                path = self.root / (day + '.jsonl')
                with open(path, 'a', encoding='utf-8') as f:
                    os.chmod(path, 0o600)
                    f.writelines(s for d, s in self.buffer if d == day)
                    f.flush()
                    os.fsync(f.fileno())
            self.buffer.clear()
            self.buffer_bytes = 0
            self.last_flush = time.monotonic()
            self.state['last_flush_at'] = utc_now()

    def _record_loop(self):
        while not self.stop_event.is_set():
            try:
                if self.buffer_bytes >= 4 * 1024 * 1024:
                    self.flush()  # Back pressure on disk failure; bounded RAM.
                row = self.collect()
                self.append(row)
                # Faults get one immediate batch flush; overlapping cursors deduplicate.
                cursors = {event.get('__CURSOR') for node in row.get('diagnostics', [])
                           for event in node.get('kernel_events', []) if event.get('__CURSOR')}
                if cursors - self.event_cursors:
                    self.flush()
                self.event_cursors = cursors or self.event_cursors
                with self.lock:
                    self.state.pop('error', None)
            except Exception as e:
                with self.lock:
                    self.state['error'] = type(e).__name__
            self.stop_event.wait(self.sample_seconds)

    def seal(self, day):
        source = self.root / (day + '.jsonl')
        archive = self.root / ('dgx-' + day + '.jsonl.gz')
        if not archive.exists():
            temporary = archive.with_suffix('.partial')
            with source.open('rb') as src, temporary.open('wb') as dst:
                os.chmod(temporary, 0o600)
                with gzip.GzipFile(fileobj=dst, mode='wb', compresslevel=3, mtime=0) as gz:
                    shutil.copyfileobj(src, gz, length=1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
            os.replace(temporary, archive)
        return archive

    def backup_once(self):
        if not self.options.get('nas_host'):
            with self.lock:
                self.state['backup_status'] = 'NAS_NOT_CONFIGURED'
            return
        today = datetime.now(timezone.utc).date().isoformat()
        with self.lock:
            self.state['backup_status'] = 'WAITING_NEXT_DAY'
        self.flush()
        for source in sorted(self.root.glob('????-??-??.jsonl')):
            if source.stem >= today:
                continue
            if self.options.get('nas_monthly_retention', False):
                current = date.fromisoformat(today)
                cutoff = date(current.year - (current.month == 1),
                              12 if current.month == 1 else current.month - 1, 1).isoformat()
                if source.stem < cutoff:
                    # Do not re-upload expired backlog immediately after monthly pruning.
                    continue
            receipt = self.root / (source.stem + '.receipt.json')
            if receipt.exists():
                continue
            archive = self.seal(source.stem)
            sha = digest(archive)
            argv = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                    '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3']
            if self.options.get('nas_identity_file'):
                argv += ['-i', self.options['nas_identity_file']]
            argv += [self.options['nas_host'], shlex.join([self.options.get('nas_python', 'python3'), self.options['nas_receiver'], archive.name, sha, str(archive.stat().st_size)])]
            with archive.open('rb') as f:
                result = subprocess.run(argv, stdin=f, capture_output=True, timeout=1800)
            if result.returncode:
                raise RuntimeError('NAS_transfer_failed')
            response = json.loads(result.stdout)
            if response.get('sha256') != sha or response.get('bytes') != archive.stat().st_size:
                raise RuntimeError('NAS_checksum_mismatch')
            atomic_json(receipt, dict(response, verified_at=utc_now()))
            with self.lock:
                self.state.update(last_backup_at=utc_now(), last_backup_file=archive.name, backup_status='VERIFIED')
        # Retain thirty days locally, and only delete files with verified NAS receipt.
        now = time.time()
        for receipt in self.root.glob('????-??-??.receipt.json'):
            if now - receipt.stat().st_mtime > 30 * 86400:
                day = receipt.name[:10]
                for p in (self.root / (day+'.jsonl'), self.root / ('dgx-'+day+'.jsonl.gz')):
                    p.unlink(missing_ok=True)

    def prune_nas_monthly(self):
        if not self.options.get('nas_monthly_retention', False) or not self.options.get('nas_host'):
            return
        month = datetime.now(timezone.utc).strftime('%Y-%m')
        receipt = self.root / 'nas-retention.json'
        if receipt.exists():
            previous = json.loads(receipt.read_text())
            if previous.get('month') == month:
                with self.lock:
                    self.state['nas_retention'] = previous
                return
        argv = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3']
        if self.options.get('nas_identity_file'):
            argv += ['-i', self.options['nas_identity_file']]
        argv += [self.options['nas_host'], shlex.join([self.options.get('nas_python', 'python3'),
                 self.options['nas_receiver'], '--prune'])]
        result = subprocess.run(argv, capture_output=True, timeout=300)
        if result.returncode:
            raise RuntimeError('NAS_retention_failed')
        response = json.loads(result.stdout)
        if response.get('month') != month or response.get('dry_run') is not False:
            raise RuntimeError('NAS_retention_receipt_invalid')
        summary = {k: response[k] for k in ('month', 'cutoff', 'deleted_count')}
        summary['completed_at'] = utc_now()
        atomic_json(receipt, summary)
        with self.lock:
            self.state['nas_retention'] = summary

    def _backup_loop(self):
        last_day = None
        retry_after = 0
        while not self.stop_event.is_set():
            day = datetime.now(timezone.utc).date().isoformat()
            if day != last_day and time.monotonic() >= retry_after:
                try:
                    self.prune_nas_monthly()
                    self.backup_once()
                    last_day = day
                except Exception as e:
                    with self.lock:
                        self.state['backup_status'] = type(e).__name__
                    retry_after = time.monotonic() + 3600
            self.stop_event.wait(60)


def load_recording(config_path, config, snapshot):
    with open(config_path, 'rb') as f:
        options = tomllib.load(f).get('recording', {})
    return RecordingService(config, snapshot, options) if options.get('enabled', False) else None
