"""Asynchronous H3 transport for one of the two independent world=2 services."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import tempfile

from .adapters.h3_dual_pair import API_PORT, PAIRS, PairSpec
from .ssh import CommandResult, FixedSSH


ASSET_ROOT = "/srv/h3-ring/assets"
DATA_ROOT = "/data/h3"
_REL = re.compile(
    r"(?:workbench/[a-f0-9]{32}/[a-f0-9]{32}-(?:primary|action|video|audio)\.(?:png|jpg|wav|mp4)"
    r"|workbench/pair-[ab]/[a-f0-9]{32}/[a-f0-9]{32}\.json)\Z"
)
_JOB = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{8,80}\Z")
_POST = r"""
import json,sys,urllib.error,urllib.request
url,path=sys.argv[1],sys.argv[2]
req=urllib.request.Request(url,data=open(path,"rb").read(),headers={"Content-Type":"application/json"})
try:
    with urllib.request.urlopen(req,timeout=60) as r:sys.stdout.write(r.read().decode())
except urllib.error.HTTPError as e:
    sys.stderr.write(e.read()[:8192].decode(errors="replace"));raise SystemExit(e.code if 1<=e.code<=255 else 1)
"""
_GET = r"""
import sys,urllib.error,urllib.request
try:
    with urllib.request.urlopen(sys.argv[1],timeout=15) as r:sys.stdout.write(r.read().decode())
except urllib.error.HTTPError as e:
    sys.stderr.write(e.read()[:8192].decode(errors="replace"));raise SystemExit(e.code if 1<=e.code<=255 else 1)
"""
_DOWNLOAD = r"""
import sys,urllib.error,urllib.request
url,out=sys.argv[1],sys.argv[2]
try:
    with urllib.request.urlopen(url,timeout=600) as r:
        ctype=(r.headers.get("Content-Type") or "").lower()
        if "json" in ctype or "text/" in ctype:raise SystemExit("expected video content")
        data=r.read()
except urllib.error.HTTPError as e:
    sys.stderr.write(e.read()[:8192].decode(errors="replace"));raise SystemExit(e.code if 1<=e.code<=255 else 1)
open(out,"wb").write(data);print(len(data))
"""


class PairRequestUnknown(RuntimeError):
    pass


def _ok(result: CommandResult, what: str) -> str:
    if result.returncode:
        raise RuntimeError(f"{what} failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout


class PairVideoTransport:
    def __init__(self, pair_id: str, ssh: FixedSSH | None = None):
        try:
            self.pair: PairSpec = next(pair for pair in PAIRS if pair.pair_id == pair_id)
        except StopIteration as exc:
            raise ValueError("unknown H3 pair slot") from exc
        self.ssh = ssh or FixedSSH()
        self.head = self.pair.head.node
        self.api_base = f"http://127.0.0.1:{API_PORT}"

    @staticmethod
    def uri(rel: str) -> str:
        if not _REL.fullmatch(rel):
            raise ValueError("pair asset path is not a workbench object")
        return f"file://{DATA_ROOT}/{rel}"

    @staticmethod
    def remote_asset(rel: str) -> str:
        if not _REL.fullmatch(rel):
            raise ValueError("pair asset path is not a workbench object")
        return f"{ASSET_ROOT}/{rel}"

    @staticmethod
    def payload(task: str, prompt: str, seed: int, conditions: list,
                duration_seconds: int = 4, aspect_ratio: str = "16:9") -> dict:
        if not isinstance(duration_seconds, int) or not 4 <= duration_seconds <= 15:
            raise ValueError("dual-pair duration must be an integer from 4 to 15 seconds")
        return {
            "task": task,
            "prompt": prompt,
            "conditions": conditions,
            "target": {"short_edge": 768, "aspect_ratio": aspect_ratio, "duration_seconds": duration_seconds},
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
        for rank in self.pair.ranks:
            _ok(self.ssh.run(rank.node, ["mkdir", "-p", parent], timeout=30), f"{self.pair.pair_id} rank{rank.rank} mkdir")
            existing = self.ssh.run(rank.node, ["sha256sum", remote], timeout=30)
            if existing.returncode == 0 and existing.stdout.startswith(digest):
                continue
            _ok(self.ssh.put(rank.node, local_path, remote, timeout=120), f"{self.pair.pair_id} rank{rank.rank} scp")
            check = _ok(self.ssh.run(rank.node, ["sha256sum", remote], timeout=30), f"{self.pair.pair_id} rank{rank.rank} hash")
            if not check.startswith(digest):
                raise RuntimeError(f"{self.pair.pair_id} rank{rank.rank} asset checksum mismatch")

    def submit(self, payload: dict, *, before_post=None) -> str:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        with tempfile.NamedTemporaryFile(prefix="h3-pair-req-", suffix=".json", delete=False) as handle:
            handle.write(body); local = handle.name
        token = hashlib.sha256(body).hexdigest()[:32]
        rel = f"workbench/{self.pair.pair_id}/{token}/{token}.json"
        try:
            self.stage(rel, local)
            if before_post is not None:
                before_post()
            try:
                result = self.ssh.run(
                    self.head,
                    ["python3", "-c", _POST, f"{self.api_base}/v1/videos", self.remote_asset(rel)],
                    timeout=90,
                )
            except Exception as exc:
                raise PairRequestUnknown("pair POST outcome is unknown; original request is retained") from exc
        finally:
            Path(local).unlink(missing_ok=True)
        if result.returncode in {400, 401, 403, 404, 409, 422, 429}:
            raise ValueError("H3 pair rejected the video request")
        if result.returncode:
            raise PairRequestUnknown("pair POST outcome is unknown; original request is retained")
        try:
            job_id = json.loads(result.stdout).get("id")
        except (json.JSONDecodeError, AttributeError) as exc:
            raise PairRequestUnknown("pair POST did not return a job id") from exc
        if not isinstance(job_id, str) or not _JOB.fullmatch(job_id):
            raise PairRequestUnknown("pair POST did not return a valid job id")
        return job_id

    def poll(self, job_id: str) -> dict:
        if not _JOB.fullmatch(job_id):
            raise ValueError("invalid pair job id")
        result = self.ssh.run(self.head, ["python3", "-c", _GET, f"{self.api_base}/v1/videos/{job_id}"], timeout=30)
        if result.returncode:
            raise RuntimeError(f"pair poll failed: {(result.stderr or result.stdout).strip()}")
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise RuntimeError("pair poll did not return an object")
        return value

    def download(self, job_id: str) -> bytes:
        if not _JOB.fullmatch(job_id):
            raise ValueError("invalid pair job id")
        remote = f"/tmp/h3-pair-{self.pair.pair_id}-{job_id.replace('/', '')}.mp4"
        _ok(self.ssh.run(self.head, ["python3", "-c", _DOWNLOAD, f"{self.api_base}/v1/videos/{job_id}/content", remote], timeout=660), "pair content")
        with tempfile.NamedTemporaryFile(prefix="h3-pair-out-", suffix=".mp4", delete=False) as handle:
            local = handle.name
        try:
            _ok(self.ssh.get(self.head, remote, local, timeout=120), "pair scp get")
            data = Path(local).read_bytes()
        finally:
            Path(local).unlink(missing_ok=True)
            self.ssh.run(self.head, ["rm", "-f", remote], timeout=15)
        if not data:
            raise RuntimeError("pair service returned empty media")
        return data
