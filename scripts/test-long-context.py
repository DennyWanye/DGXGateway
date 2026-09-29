#!/usr/bin/env python3
import argparse
import json
import time
import urllib.request


def post(base_url: str, path: str, body: dict, timeout: int) -> dict:
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


parser = argparse.ArgumentParser()
parser.add_argument("--base-url", default="http://127.0.0.1:8080")
parser.add_argument("--filler-tokens", type=int, required=True)
parser.add_argument("--timeout", type=int, default=3600)
args = parser.parse_args()

sentinel = f"DGX_SENTINEL_{args.filler_tokens}_A9F3"
content = (
    f"开头验证码是 {sentinel}。请记住它。"
    + " filler" * args.filler_tokens
    + "\n现在只输出开头验证码，不要输出其他内容。"
)

tokenized = post(args.base_url, "/tokenize", {"content": content}, args.timeout)
raw_tokens = len(tokenized["tokens"])
started = time.monotonic()
result = post(
    args.base_url,
    "/v1/chat/completions",
    {
        "model": "qwen38-27b-aggressive-q8",
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 32,
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    },
    args.timeout,
)
elapsed = time.monotonic() - started
message = result["choices"][0]["message"].get("content", "")
passed = sentinel in message
print(json.dumps({
    "filler_tokens": args.filler_tokens,
    "raw_prompt_tokens": raw_tokens,
    "sentinel": sentinel,
    "response": message,
    "sentinel_recalled": passed,
    "elapsed_seconds": round(elapsed, 3),
    "usage": result.get("usage", {}),
    "timings": result.get("timings", {}),
}, ensure_ascii=False, indent=2))
raise SystemExit(0 if passed else 1)
