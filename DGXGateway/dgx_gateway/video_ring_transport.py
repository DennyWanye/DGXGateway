"""Stage workbench assets onto Ring4 and run async /v1/videos. Never retry POST."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path

from .adapters.h3_ring import API_PORT, RANKS
from .ssh import CommandResult, FixedSSH


class Ring4Unknown(Exception):
    """POST may have reached rank0; never submit the same clip again."""

ASSET_ROOT = "/srv/h3-ring/assets"
DATA_ROOT = "/data/h3"
API_BASE = f"http://127.0.0.1:{API_PORT}"
_REL = re.compile(r"workbench/[a-f0-9]{32}/[a-f0-9]{32}(?:-(?:primary|action|video|audio|picture-[1-9]))?\.(png|jpg|wav|mp4|json)\Z")
_JOB = re.compile(r"[A-Za-z0-9._:-]{8,80}\Z")
_POST = r"""
import json,sys,urllib.error,urllib.request
url,path=sys.argv[1],sys.argv[2]
req=urllib.request.Request(url,data=open(path,"rb").read(),headers={"Content-Type":"application/json"})
try:
    with urllib.request.urlopen(req,timeout=60) as r:
        sys.stdout.write(r.read().decode())
except urllib.error.HTTPError as e:
    sys.stderr.write(e.read()[:8192].decode(errors="replace"))
    raise SystemExit(e.code if 1<=e.code<=255 else 1)
"""
_GET = r"""
import sys,urllib.error,urllib.request
url=sys.argv[1]
try:
    with urllib.request.urlopen(url,timeout=15) as r:
        sys.stdout.write(r.read().decode())
except urllib.error.HTTPError as e:
    sys.stderr.write(e.read()[:8192].decode(errors="replace"))
    raise SystemExit(e.code if 1<=e.code<=255 else 1)
"""
_DELETE = r"""
import json,sys,urllib.error,urllib.request
url=sys.argv[1]
req=urllib.request.Request(url,method="DELETE")
try:
    with urllib.request.urlopen(req,timeout=15) as r:
        sys.stdout.write(r.read().decode())
except urllib.error.HTTPError as e:
    # 404 is idempotent for cancellation: the backend no longer owns the job.
    if e.code == 404:
        sys.stdout.write(json.dumps({"status":"not_found","http_status":404}))
    else:
        sys.stderr.write(e.read()[:8192].decode(errors="replace"))
        raise SystemExit(e.code if 1<=e.code<=255 else 1)
except urllib.error.URLError as e:
    # Listener down (Linux 111 / macOS 61) is the same already-gone receipt.
    reason=e.reason
    errno=getattr(reason, "errno", None)
    text=str(reason)
    if errno in (111, 61) or "Connection refused" in text:
        sys.stdout.write(json.dumps({"status":"not_found","http_status":0,"reason":"connection_refused"}))
    else:
        sys.stderr.write(text[:8192])
        raise SystemExit(1)
"""
_DOWNLOAD = r"""
import sys,urllib.error,urllib.request
url,out=sys.argv[1],sys.argv[2]
try:
    with urllib.request.urlopen(url,timeout=600) as r:
        ctype=(r.headers.get("Content-Type") or "").lower()
        if "json" in ctype or "text/" in ctype:
            raise SystemExit("expected video content")
        data=r.read()
except urllib.error.HTTPError as e:
    sys.stderr.write(e.read()[:8192].decode(errors="replace"))
    raise SystemExit(e.code if 1<=e.code<=255 else 1)
open(out,"wb").write(data)
print(len(data))
"""


def _ok(result: CommandResult, what: str) -> str:
    if result.returncode:
        raise RuntimeError(f"{what} failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout


class Ring4VideoTransport:
    def __init__(self, ssh: FixedSSH | None = None):
        self.ssh = ssh or FixedSSH()
        self.ranks = RANKS
        self.head = RANKS[0].node

    def diagnostic_progress(self, diagnostic_id: str) -> dict:
        from concurrent.futures import ThreadPoolExecutor
        if not re.fullmatch(r"[a-f0-9]{32}", diagnostic_id):
            raise ValueError("invalid diagnostic id")
        def read(spec):
            path = f"/srv/h3-ring/cache/h3-diagnostics/{diagnostic_id}/rank{spec.rank}/progress.json"
            try:
                result = self.ssh.run(spec.node, ["cat", path], timeout=8)
                return str(spec.rank), json.loads(result.stdout) if not result.returncode else {}
            except (ValueError, OSError, TimeoutError, subprocess.TimeoutExpired):
                return str(spec.rank), {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            return dict(pool.map(read, self.ranks))

    def uri(self, rel: str) -> str:
        if not _REL.fullmatch(rel):
            raise ValueError("Ring4 asset path is not a workbench object")
        return f"file://{DATA_ROOT}/{rel}"

    def remote_asset(self, rel: str) -> str:
        if not _REL.fullmatch(rel):
            raise ValueError("Ring4 asset path is not a workbench object")
        return f"{ASSET_ROOT}/{rel}"

    def payload(self, task: str, prompt: str, seed: int, conditions: list,
                duration_seconds: int = 4, aspect_ratio: str = "16:9") -> dict:
        if not isinstance(duration_seconds, int) or not 4 <= duration_seconds <= 15:
            raise ValueError("Ring4 duration must be an integer from 4 to 15 seconds")
        return {
            "task": task,
            "prompt": prompt,
            "conditions": conditions,
            "target": {"short_edge": 768, "aspect_ratio": aspect_ratio,
                       "duration_seconds": duration_seconds},
            "num_outputs_per_prompt": 1,
            "seed": seed,
            "num_inference_steps": 50,
            "flow_shift": 12.0,
            "audio_flow_shift": 3.0,
            "quality": "lossless",
        }

    def stage(self, rel: str, local_path: str) -> None:
        remote = self.remote_asset(rel)
        data = Path(local_path).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        parent = str(Path(remote).parent)
        for spec in self.ranks:
            _ok(self.ssh.run(spec.node, ["mkdir", "-p", parent], timeout=30), f"rank{spec.rank} mkdir")
            existing = self.ssh.run(spec.node, ["sha256sum", remote], timeout=30)
            if existing.returncode == 0 and existing.stdout.startswith(digest):
                continue
            copied = self.ssh.put(spec.node, local_path, remote, timeout=120)
            _ok(copied, f"rank{spec.rank} scp")
            check = _ok(self.ssh.run(spec.node, ["sha256sum", remote], timeout=30), f"rank{spec.rank} hash")
            if not check.startswith(digest):
                raise RuntimeError(f"rank{spec.rank} asset checksum mismatch")

    def submit(self, payload: dict, *, before_post=None) -> str:
        """POST once. A lost receipt is unknown; callers must not POST again."""
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        with tempfile.NamedTemporaryFile(prefix="h3-ring-req-", suffix=".json", delete=False) as handle:
            handle.write(body)
            local = handle.name
        token = hashlib.sha256(body).hexdigest()[:32]
        rel = f"workbench/{token}/{token}.json"
        try:
            self.stage(rel, local)
            remote = self.remote_asset(rel)
            if before_post is not None:
                before_post()
            try:
                result = self.ssh.run(
                    self.head,
                    ["python3", "-c", _POST, f"{API_BASE}/v1/videos", remote],
                    timeout=90,
                )
            except Exception as exc:
                raise Ring4Unknown("Ring4 POST outcome is unknown; original request is retained") from exc
        finally:
            Path(local).unlink(missing_ok=True)
        if result.returncode in {400, 401, 403, 404, 409, 422, 429}:
            raise ValueError("H3 rejected the video request")
        if result.returncode:
            raise Ring4Unknown("Ring4 POST outcome is unknown; original request is retained")
        try:
            job = json.loads(result.stdout)
            job_id = job.get("id")
        except (json.JSONDecodeError, AttributeError) as exc:
            raise Ring4Unknown("Ring4 POST did not return a job id; original request is retained") from exc
        if not isinstance(job_id, str) or not _JOB.fullmatch(job_id):
            raise Ring4Unknown("Ring4 POST did not return a job id; original request is retained")
        return job_id

    def poll(self, job_id: str) -> dict:
        if not _JOB.fullmatch(job_id):
            raise ValueError("invalid Ring4 job id")
        result = self.ssh.run(
            self.head,
            ["python3", "-c", _GET, f"{API_BASE}/v1/videos/{job_id}"],
            timeout=30,
        )
        if result.returncode:
            raise RuntimeError(f"Ring4 poll failed: {(result.stderr or result.stdout).strip()}")
        state = json.loads(result.stdout)
        if not isinstance(state, dict):
            raise RuntimeError("Ring4 poll did not return an object")
        return state

    def cancel(self, job_id: str) -> dict:
        """Cancel one remote Ring4 job without retrying the request.

        The current SGLang endpoint exposes DELETE for job cancellation.  The
        Gateway still owns the stronger cleanup guarantee: after this receipt,
        ``VideoJobs`` stops the Ring4 runtime through the lifecycle controller
        before marking the local batch canceled.  A 404 or connection-refused
        listener is treated as an idempotent already-gone receipt and is
        never retried.
        """
        if not _JOB.fullmatch(job_id):
            raise ValueError("invalid Ring4 job id")
        result = self.ssh.run(
            self.head,
            ["python3", "-c", _DELETE, f"{API_BASE}/v1/videos/{job_id}"],
            timeout=30,
        )
        if result.returncode:
            err = (result.stderr or result.stdout).strip()
            if "Connection refused" in err or "Errno 111" in err or "Errno 61" in err:
                return {"status": "not_found", "http_status": 0, "reason": "connection_refused"}
            raise RuntimeError(f"Ring4 cancel failed: {err}")
        try:
            reply = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError("Ring4 cancel did not return an object") from exc
        if not isinstance(reply, dict):
            raise RuntimeError("Ring4 cancel did not return an object")
        return reply

    def download(self, job_id: str) -> bytes:
        if not _JOB.fullmatch(job_id):
            raise ValueError("invalid Ring4 job id")
        remote = f"/tmp/h3-ring-{job_id.replace('/', '')}.mp4"
        _ok(
            self.ssh.run(
                self.head,
                ["python3", "-c", _DOWNLOAD, f"{API_BASE}/v1/videos/{job_id}/content", remote],
                timeout=660,
            ),
            "Ring4 content",
        )
        with tempfile.NamedTemporaryFile(prefix="h3-ring-out-", suffix=".mp4", delete=False) as handle:
            local = handle.name
        try:
            _ok(self.ssh.get(self.head, remote, local, timeout=120), "Ring4 scp get")
            data = Path(local).read_bytes()
        finally:
            Path(local).unlink(missing_ok=True)
            self.ssh.run(self.head, ["rm", "-f", remote], timeout=15)
        if not data:
            raise RuntimeError("Ring4 returned empty media")
        return data
