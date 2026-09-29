#!/usr/bin/env python3
"""Local H3 job client. Never automatically retries a job-creating POST."""
import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def request_json(url: str, payload=None) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            value = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read(8192).decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {detail}") from e
    if not isinstance(value, dict):
        raise RuntimeError("Expected an API JSON object")
    return value


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://127.0.0.1:30021")
    p.add_argument("--request", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--timeout", type=int, default=7200, help="Client polling budget, not an ETA")
    p.add_argument("--resume-id", help="Poll a known existing job instead of submitting a new job")
    a = p.parse_args()
    if a.timeout <= 0:
        p.error("--timeout must be positive")
    payload = json.loads(a.request.read_text())
    if not isinstance(payload, dict):
        p.error("Request must be a JSON object")
    if a.out.exists() or a.out.with_suffix(a.out.suffix + ".part").exists():
        p.error("Output/partial file already exists; select a new output path")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    prefix = str(a.out)
    Path(prefix + ".request.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    base = a.base.rstrip("/")
    started = time.monotonic()
    job = {"id": a.resume_id} if a.resume_id else request_json(base + "/v1/videos", payload)
    Path(prefix + ".submitted.json").write_text(json.dumps(job, ensure_ascii=False, indent=2))
    job_id = job.get("id")
    if not isinstance(job_id, str) or not job_id:
        raise RuntimeError("API did not return a job id; inspect submitted.json; do not retry POST blindly")
    url = base + "/v1/videos/" + urllib.parse.quote(job_id, safe="")
    print(f"job_id={job_id}", flush=True)
    previous = None
    while True:
        if time.monotonic() - started > a.timeout:
            raise TimeoutError(f"Client deadline reached; job {job_id} may still run. Resume by id, not POST.")
        state = request_json(url)
        Path(prefix + ".status.json").write_text(json.dumps(state, ensure_ascii=False, indent=2))
        status = state.get("status")
        if status != previous:
            print(f"status={status}", flush=True)
            previous = status
        if status == "completed":
            break
        if status in {"failed", "cancelled", "canceled", "expired"}:
            raise RuntimeError(f"Job ended with {status}: {json.dumps(state, ensure_ascii=False)}")
        if status not in {"queued", "pending", "running", "in_progress", "processing"}:
            raise RuntimeError(f"Unknown job status {status!r}; inspect server schema instead of polling forever")
        time.sleep(2)
    part = a.out.with_suffix(a.out.suffix + ".part")
    with urllib.request.urlopen(url + "/content", timeout=600) as r:
        content_type = r.headers.get("Content-Type", "").lower()
        if "json" in content_type or "text/" in content_type:
            raise RuntimeError(f"Expected video content, got {content_type}")
        with part.open("xb") as f:
            while chunk := r.read(1024 * 1024):
                f.write(chunk)
    if part.stat().st_size == 0:
        raise RuntimeError("Empty media output")
    part.rename(a.out)
    result = {"job_id": job_id, "wall_seconds": round(time.monotonic() - started, 3),
              "bytes": a.out.stat().st_size, "output": str(a.out),
              "resumed": bool(a.resume_id), "media_decode_verified": False}
    Path(prefix + ".result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
