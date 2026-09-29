#!/usr/bin/env python3
"""Run a fixed Responses workload against one Q8 tuning candidate."""

import argparse
import json
import pathlib
import re
import sys
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from q8_tuning import MODEL, aggregate_runs, build_request, normalize_run  # noqa: E402


def post(base_url: str, body: dict) -> tuple[dict, float]:
    request = urllib.request.Request(
        f"{base_url}/responses",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer ollama"},
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=1800) as response:
        result = json.load(response)
    return result, time.monotonic() - started


def log_timings(log_path: pathlib.Path, offset: int) -> dict:
    chunk = log_path.read_text(errors="replace")[offset:]
    prompt = re.findall(
        r"prompt eval time\s*=.*?/\s*(\d+) tokens.*?([0-9.]+) tokens per second",
        chunk,
    )
    generation = re.findall(
        r"\|\s+eval time\s*=.*?/\s*(\d+) tokens.*?([0-9.]+) tokens per second",
        chunk,
    )
    draft = re.findall(
        r"draft acceptance\s*=.*?\(\s*(\d+) accepted /\s*(\d+) generated\)",
        chunk,
    )
    if not prompt or not generation or not draft:
        raise RuntimeError(f"missing server timings in {log_path}")
    return {
        "prompt_n": int(prompt[-1][0]),
        "prompt_per_second": float(prompt[-1][1]),
        "predicted_n": int(generation[-1][0]),
        "predicted_per_second": float(generation[-1][1]),
        "draft_n_accepted": int(draft[-1][0]),
        "draft_n": int(draft[-1][1]),
    }


def measured_post(base_url: str, body: dict, log_path: pathlib.Path) -> tuple[dict, float, dict]:
    offset = log_path.stat().st_size
    response, elapsed = post(base_url, body)
    return response, elapsed, log_timings(log_path, offset)


def output_text(response: dict) -> str:
    return "".join(
        content.get("text", "")
        for item in response.get("output", [])
        for content in item.get("content", [])
        if content.get("type") == "output_text"
    )


def cache_probe(base_url: str, log_path: pathlib.Path) -> dict:
    body = {
        "model": MODEL,
        "input": "CACHE_PREFIX " + " stable" * 5_000 + "\nReply exactly CACHE_Q8_OK",
        "reasoning": {"effort": "none"},
        "temperature": 0,
        "max_output_tokens": 32,
    }
    samples = []
    for _ in range(2):
        response, elapsed, timings = measured_post(base_url, body, log_path)
        samples.append({
            "status": response.get("status"),
            "text": output_text(response),
            "elapsed_seconds": round(elapsed, 3),
            "prompt_tps": round(float(timings.get("prompt_per_second", 0)), 3),
            "prompt_eval_tokens": int(timings.get("prompt_n", 0)),
            "cached_tokens": int(response.get("usage", {}).get("input_tokens_details", {}).get("cached_tokens", 0)),
        })
    return {
        "runs": samples,
        "responses_match": samples[0]["text"] == samples[1]["text"] == "CACHE_Q8_OK",
        "second_faster": samples[1]["elapsed_seconds"] < samples[0]["elapsed_seconds"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:18080/v1")
    parser.add_argument("--depth", type=int, required=True)
    parser.add_argument("--p-min", type=float, required=True)
    parser.add_argument("--ubatch", type=int, required=True)
    parser.add_argument("--cache-reuse", type=int, required=True)
    parser.add_argument("--spec-types", required=True)
    parser.add_argument("--server-log", type=pathlib.Path, required=True)
    args = parser.parse_args()

    runs = []
    for _ in range(3):
        response, elapsed, timings = measured_post(args.base_url, build_request(), args.server_log)
        runs.append(normalize_run(response, elapsed, timings))
    result = {
        "candidate": args.candidate,
        "settings": {
            "model": MODEL,
            "context": 262144,
            "parallel": 1,
            "cache_k": "f16",
            "cache_v": "f16",
            "mtp_depth": args.depth,
            "mtp_p_min": args.p_min,
            "ubatch": args.ubatch,
            "cache_reuse": args.cache_reuse,
            "spec_types": args.spec_types,
        },
        "runs": runs,
        "aggregate": aggregate_runs(args.candidate, runs),
        "cache_probe": cache_probe(args.base_url, args.server_log),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
