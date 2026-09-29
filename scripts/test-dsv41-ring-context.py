#!/usr/bin/env python3
"""DeepSeek-V4.1-Flash Ring long-context recall and concurrency ramp.

Talks to the loopback OpenAI chat-completions API through an SSH tunnel.
Does not print credentials.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed


SENTINEL_PREFIX = "DGX_DSV41_RING_SENTINEL_"
MODEL = "deepseek-v4.1-flash-ring"
FILLER = "的"


def extract_message_text(payload: dict) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return str(message.get("content") or "").strip()


def build_prompt(sentinel: str, filler_tokens: int) -> str:
    return (
        f"开头验证码是 {sentinel}。请记住它。"
        + (FILLER * filler_tokens)
        + "\n现在只输出开头验证码，不要输出其他内容。"
    )


def post_chat(base_url: str, api_key: str, body: dict, timeout: int) -> dict:
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:400].decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def one_request(base_url: str, api_key: str, model: str, sentinel: str, filler_tokens: int, max_tokens: int, timeout: int) -> dict:
    started = time.monotonic()
    try:
        payload = post_chat(
            base_url,
            api_key,
            {
                "model": model,
                "messages": [{"role": "user", "content": build_prompt(sentinel, filler_tokens)}],
                "max_tokens": max_tokens,
                "temperature": 0,
            },
            timeout,
        )
        text = extract_message_text(payload)
        usage = payload.get("usage") or {}
        return {
            "ok": True,
            "sentinel": sentinel,
            "response": text,
            "sentinel_recalled": sentinel in text,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
        }
    except Exception as exc:
        return {
            "ok": False,
            "sentinel": sentinel,
            "error": str(exc)[:300],
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "sentinel_recalled": False,
        }


def run_concurrency(base_url: str, api_key: str, model: str, concurrency: int, filler_tokens: int, max_tokens: int, timeout: int) -> dict:
    sentinels = [f"{SENTINEL_PREFIX}{concurrency:02d}_{index:02d}" for index in range(concurrency)]
    results = []
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [
            pool.submit(one_request, base_url, api_key, model, sentinel, filler_tokens, max_tokens, timeout)
            for sentinel in sentinels
        ]
        for future in as_completed(futures):
            results.append(future.result())
    recalled = sum(1 for item in results if item.get("sentinel_recalled"))
    failed = [item for item in results if not item.get("ok") or not item.get("sentinel_recalled")]
    return {
        "concurrency": concurrency,
        "stable": not failed,
        "recalled": recalled,
        "total": concurrency,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "failures": [{"sentinel": item["sentinel"], "error": item.get("error"), "response": item.get("response")} for item in failed],
        "results": results,
    }


def ramp(base_url: str, api_key: str, model: str, start: int, stop: int, filler_tokens: int, max_tokens: int, timeout: int) -> dict:
    rows = []
    max_stable = 0
    for concurrency in range(start, stop + 1):
        row = run_concurrency(base_url, api_key, model, concurrency, filler_tokens, max_tokens, timeout)
        rows.append(row)
        if not row["stable"]:
            break
        max_stable = concurrency
    return {"max_stable_concurrency": max_stable, "runs": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8891/v1")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--api-key-file", default=os.path.expanduser("~/.config/dgx-cluster-gateway/dsv41-ring.token"))
    parser.add_argument("--mode", choices=["recall", "concurrency", "ramp"], default="recall")
    parser.add_argument("--filler-tokens", type=int, default=240000)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--ramp-start", type=int, default=2)
    parser.add_argument("--ramp-stop", type=int, default=16)
    args = parser.parse_args()
    api_key = os.environ.get("DSV41_RING_API_KEY")
    if not api_key:
        api_key = open(args.api_key_file, encoding="utf-8").read().strip()
    if args.mode == "recall":
        result = one_request(args.base_url, api_key, args.model, SENTINEL_PREFIX + "RECALL", args.filler_tokens, args.max_tokens, args.timeout)
    elif args.mode == "concurrency":
        result = run_concurrency(args.base_url, api_key, args.model, args.concurrency, args.filler_tokens, args.max_tokens, args.timeout)
    else:
        result = ramp(args.base_url, api_key, args.model, args.ramp_start, args.ramp_stop, args.filler_tokens, args.max_tokens, args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.mode == "recall" and (not result.get("ok") or not result.get("sentinel_recalled")):
        raise SystemExit(2)
    if args.mode == "concurrency" and not result.get("stable"):
        raise SystemExit(2)
    if args.mode == "ramp" and result.get("max_stable_concurrency", 0) < args.ramp_start:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
