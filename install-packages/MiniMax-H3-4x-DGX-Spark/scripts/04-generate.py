#!/usr/bin/env python3
"""Submit one H3 job, poll it, download it, and verify actual video/audio decoding.
The running server must have the SAME profile. A client flag does not load an adapter.
"""
from __future__ import annotations
import argparse
import json
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_PROMPT = ("A ceramic teapot rests on a wooden table beside a rain-covered window. "
                  "A slow, steady camera move reveals steam rising naturally. "
                  "Keep the teapot shape consistent, with synchronized quiet rain ambience.")

def make_payload(profile: str, prompt: str, seconds: int, short_edge: int,
                 seed: int, first: str | None = None, last: str | None = None,
                 aspect_ratio: str | None = None) -> dict[str, Any]:
    if profile not in {"q", "u-q", "p", "u-p"}:
        raise ValueError("Unknown profile")
    if not 4 <= seconds <= 15 or short_edge < 1 or not prompt.strip():
        raise ValueError("Invalid duration, resolution, or prompt")
    conditions = []
    for path, index in ((first, 0), (last, -1)):
        if path:
            if not Path(path).is_absolute():
                raise ValueError("Condition paths must be absolute SERVER paths")
            conditions.append({"type": "image", "uri": Path(path).as_uri(),
                               "role": "keyframe", "frame_index": index})
    data: dict[str, Any] = {
        "model": f"h3-fl2va-{profile}", "prompt": prompt, "seconds": seconds,
        "task": "fl2va" if conditions else "t2va", "conditions": conditions,
        "target": {"short_edge": short_edge,
                   "aspect_ratio": aspect_ratio or ("auto" if conditions else "16:9"),
                   "duration_seconds": float(seconds)},
        "num_outputs_per_prompt": 1,
        "num_inference_steps": 9 if profile in {"p", "u-p"} else 50,
        "flow_shift": 12.0, "audio_flow_shift": 3.0, "seed": seed,
        "quality": "lossless",
    }
    if profile in {"p", "u-p"}:
        data["lora_scale"] = 1.0
    return data

def json_request(url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            value = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected a JSON object from {url}")
    return value

def verify_video(path: Path, seconds: int, short_edge: int) -> dict[str, Any]:
    result = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                             "-of", "json", str(path)], check=True, text=True,
                            capture_output=True, timeout=120)
    meta = json.loads(result.stdout)
    video = [s for s in meta["streams"] if s.get("codec_type") == "video"]
    audio = [s for s in meta["streams"] if s.get("codec_type") == "audio"]
    if len(video) != 1 or len(audio) != 1:
        raise RuntimeError("Expected one video stream and one audio stream")
    v, a = video[0], audio[0]
    if v.get("codec_name") != "h264" or a.get("codec_name") != "aac":
        raise RuntimeError("Unexpected codecs; inspect the runtime's output contract")
    if a.get("channels") != 2 or int(a.get("sample_rate", 0)) != 32000:
        raise RuntimeError("Expected AAC stereo at 32 kHz")
    numerator, denominator = map(int, v["avg_frame_rate"].split("/"))
    if denominator == 0 or abs(numerator / denominator - 24) > 0.1:
        raise RuntimeError("Expected approximately 24 fps")
    duration = float(meta["format"]["duration"])
    if abs(duration - seconds) > 0.6:
        raise RuntimeError(f"Unexpected duration: {duration}s for a {seconds}s request")
    if min(v["width"], v["height"]) < short_edge - 32:
        raise RuntimeError("Output is smaller than the requested aligned resolution")
    subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(path),
                    "-f", "null", "-"], check=True, capture_output=True, timeout=300)
    return meta

def run_job(base_url: str, payload: dict[str, Any], output: Path,
            timeout_s: float = 10800, poll_s: float = 3,
            resume_id: str | None = None) -> dict[str, Any]:
    if timeout_s <= 0 or poll_s <= 0:
        raise ValueError("Timeout and poll interval must be positive")
    for exe in ("ffprobe", "ffmpeg"):
        if not shutil.which(exe):
            raise RuntimeError(f"Install {exe} before submitting a GPU job")
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = output.with_suffix(".job.json")
    if manifest_path.exists() and not resume_id:
        raise FileExistsError(f"Previous job metadata exists; use --resume-id or a new output path: {manifest_path}")
    if manifest_path.exists() and resume_id:
        previous_record = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous_record.get("id") != resume_id or previous_record.get("request") != payload:
            raise ValueError("Resume requires the same recorded job id and original request parameters")
    base_url = base_url.rstrip("/")
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("base-url must be an HTTP(S) server")
    record: dict[str, Any] = {"request": payload, "base_url": base_url,
                              "client_start_unix": time.time(), "resumed": bool(resume_id)}
    def save() -> None:
        manifest_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    start = time.monotonic()
    try:
        # POST exactly once. A lost response is not retried to avoid duplicate GPU work.
        created = {"id": resume_id} if resume_id else json_request(base_url + "/v1/videos", payload)
        video_id = created.get("id")
        if not isinstance(video_id, str) or not video_id:
            raise RuntimeError(f"No video id in response: {created}")
        record["id"] = video_id
        record["create_response"] = created
        save()
        route = base_url + "/v1/videos/" + urllib.parse.quote(video_id, safe="")
        previous = None
        while True:
            if time.monotonic() - start > timeout_s:
                raise TimeoutError(f"Client wait expired; job {video_id} may still be running. It was NOT cancelled.")
            state = json_request(route)
            record["last_status"] = state
            save()
            status = state.get("status")
            if status != previous:
                print(f"job={video_id} status={status}", flush=True)
                previous = status
            if status == "completed":
                break
            if status in {"failed", "cancelled", "canceled", "expired"}:
                raise RuntimeError(f"Generation ended without success: {state}")
            if status not in {"queued", "pending", "running", "processing", "in_progress"}:
                raise RuntimeError(f"Unrecognized status; preserve job id and inspect server: {state}")
            time.sleep(poll_s)
        partial = output.with_name(output.stem + ".partial.mp4")
        with urllib.request.urlopen(route + "/content", timeout=180) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            if "json" in content_type or "text/" in content_type:
                raise RuntimeError("The content endpoint returned text/JSON, not a video")
            with partial.open("wb") as handle:
                shutil.copyfileobj(response, handle, length=1024 * 1024)
        record["media"] = verify_video(partial, int(payload["seconds"]),
                                         int(payload["target"]["short_edge"]))
        # Preserve a concurrently created output instead of silently replacing it.
        if output.exists():
            raise FileExistsError(output)
        partial.rename(output)
        record["verified_output"] = str(output)
        record["client_elapsed_seconds"] = time.monotonic() - start
        record["status"] = "decoded_video_audio_verified"
        save()
        print(f"VERIFIED: {output}\nMetadata: {manifest_path}", flush=True)
        return record
    except Exception as exc:
        record["client_error"] = str(exc)
        save()
        raise

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:30010")
    parser.add_argument("--profile", choices=["q", "u-q", "p", "u-p"], required=True)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--seconds", type=int, default=5, choices=range(4, 16))
    parser.add_argument("--short-edge", type=int, default=768, choices=[480, 768])
    parser.add_argument("--seed", type=int, default=20260917)
    parser.add_argument("--first", help="Absolute image path visible INSIDE all four server containers")
    parser.add_argument("--last", help="Absolute image path visible INSIDE all four server containers")
    parser.add_argument("--aspect-ratio", default=None)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=10800, help="Client wait ceiling, not a speed estimate")
    parser.add_argument("--resume-id", default=None, help="Poll/download an existing job; does not POST again")
    args = parser.parse_args()
    payload = make_payload(args.profile, args.prompt, args.seconds, args.short_edge,
                           args.seed, args.first, args.last, args.aspect_ratio)
    run_job(args.base_url, payload, args.output, timeout_s=args.timeout, resume_id=args.resume_id)

if __name__ == "__main__":
    main()
