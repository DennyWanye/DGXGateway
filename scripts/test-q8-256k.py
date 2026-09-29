#!/usr/bin/env python3
import argparse
import json
import time
import urllib.request


def post(url, body, timeout):
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def extract_output_text(payload):
    return "".join(
        part.get("text", "")
        for item in payload.get("output", [])
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    )


def build_result(payload, sentinel, raw_prompt_tokens, elapsed):
    text = extract_output_text(payload)
    return {
        "model": "qwen38-q8-256k:latest",
        "status": payload.get("status"),
        "raw_prompt_tokens": raw_prompt_tokens,
        "sentinel": sentinel,
        "response": text,
        "sentinel_recalled": sentinel in text,
        "elapsed_seconds": round(elapsed, 3),
        "usage": payload.get("usage", {}),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--model", default="qwen38-q8-256k:latest")
    parser.add_argument("--filler-tokens", type=int, default=250000)
    parser.add_argument("--max-output-tokens", type=int, default=64)
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()

    sentinel = "DGX_Q8_256K_SENTINEL_A9F3"
    content = (
        f"开头验证码是 {sentinel}。请记住它。"
        + " filler" * args.filler_tokens
        + "\n现在只输出开头验证码，不要输出其他内容。"
    )
    tokenized = post(
        args.base_url.rstrip("/") + "/tokenize",
        {"content": content},
        args.timeout,
    )
    raw_prompt_tokens = len(tokenized["tokens"])
    started = time.monotonic()
    payload = post(
        args.base_url.rstrip("/") + "/v1/responses",
        {
            "model": args.model,
            "input": content,
            "max_output_tokens": args.max_output_tokens,
            "reasoning": {"effort": "none"},
        },
        args.timeout,
    )
    result = build_result(
        payload,
        sentinel,
        raw_prompt_tokens,
        time.monotonic() - started,
    )
    result["model"] = args.model
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "completed" or not result["sentinel_recalled"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
