#!/usr/bin/env python3
import json
import time
import urllib.request


def request(port: int, model: str) -> dict:
    content = (
        "这是一份性能测试文档。"
        + " filler" * 10000
        + "\n请写一篇至少两千字的中文技术文章，讨论本地大模型网关设计，不要提前结束。"
    )
    body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 512,
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=1800) as response:
        result = json.load(response)
    message = result["choices"][0]["message"].get("content", "")
    return {
        "port": port,
        "model": model,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "finish_reason": result["choices"][0].get("finish_reason"),
        "output_characters": len(message),
        "usage": result.get("usage", {}),
        "timings": result.get("timings", {}),
    }


results = [
    request(8080, "qwen38-27b-aggressive-q8"),
    request(8081, "/home/operator/dgx-models/qwen38-27b/models/Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf"),
]
print(json.dumps(results, ensure_ascii=False, indent=2))
