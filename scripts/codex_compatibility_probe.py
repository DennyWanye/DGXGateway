#!/usr/bin/env python3
"""Black-box Responses API gates for Codex-compatible llama.cpp servers."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def _output_text(response: dict[str, Any]) -> str:
    return "".join(
        part.get("text", "")
        for item in response.get("output", [])
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    )


def find_function_call(response: dict, expected_name: str) -> dict:
    for item in response.get("output", []):
        if item.get("type") == "function_call" and item.get("name") == expected_name:
            return item
    raise AssertionError(f"missing function_call {expected_name!r}")


def collect_reasoning_text(response: dict) -> str:
    chunks: list[str] = []
    for item in response.get("output", []):
        if item.get("type") != "reasoning":
            continue
        direct = item.get("text")
        if isinstance(direct, str) and direct:
            chunks.append(direct)
        for key in ("content", "summary"):
            for part in item.get(key, []):
                text = part.get("text")
                if isinstance(text, str) and text:
                    chunks.append(text)
    return "\n".join(chunks)


def _server_root(base_root: str) -> str:
    parsed = urllib.parse.urlsplit(base_root.rstrip("/"))
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")


def count_tokens(base_root: str, text: str) -> int:
    request = urllib.request.Request(
        _server_root(base_root) + "/tokenize",
        data=json.dumps({"content": text}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    return len(payload["tokens"])


def assert_completed_visible(response: dict) -> None:
    if response.get("status") != "completed":
        raise AssertionError(f"response status is not completed: {response.get('status')!r}")
    if not _output_text(response).strip():
        raise AssertionError("completed response has no visible output")


class ResponsesClient:
    def __init__(self, base_url: str, api_key: str, timeout: int):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = {"Content-Type": "application/json"}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"

    def post(self, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + "/responses",
            data=json.dumps(body).encode(),
            headers=self.headers,
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.load(response)

    def stream(self, body: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        body = dict(body)
        body["stream"] = True
        headers = dict(self.headers)
        headers["Accept"] = "text/event-stream"
        request = urllib.request.Request(
            self.base_url + "/responses",
            data=json.dumps(body).encode(),
            headers=headers,
        )
        event_name = ""
        event_names: list[str] = []
        completed: dict[str, Any] | None = None
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data = line[5:].strip()
                    if data == "[DONE]":
                        continue
                    event = json.loads(data)
                    name = event.get("type") or event_name
                    if name:
                        event_names.append(name)
                    if name == "response.completed":
                        completed = event.get("response") or event
                elif not line:
                    event_name = ""
        if completed is None:
            raise AssertionError("stream omitted response.completed")
        return completed, event_names


def _namespace_body(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "input": "Call the diagnostic echo tool once with value NAMESPACE_OK. Do not answer in prose.",
        "max_output_tokens": 256,
        "tool_choice": "required",
        "tools": [
            {
                "type": "namespace",
                "name": "mcp__diagnostic__",
                "description": "Deterministic diagnostic tools",
                "tools": [
                    {
                        "type": "function",
                        "name": "echo",
                        "description": "Echo one diagnostic value",
                        "parameters": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                            "additionalProperties": False,
                        },
                    }
                ],
            }
        ],
    }


def _reasoning_body(model: str, effort: str) -> dict[str, Any]:
    return {
        "model": model,
        "input": (
            "Design a safe migration plan for a stateful API from one database to another. "
            "You must preserve writes, support rollback, avoid downtime, verify consistency, "
            "bound operational risk, and order every dependency. Think through the constraints, "
            "then finish with the visible marker PLAN_OK."
        ),
        "max_output_tokens": 2048,
        "reasoning": {"effort": effort},
    }


def _reasoning_ends_cleanly(text: str) -> bool:
    return bool(text.rstrip()) and text.rstrip()[-1] in ".!?。！？:;)]}\"'`"


def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    client = ResponsesClient(args.base_url, args.api_key, args.timeout)
    report: dict[str, Any] = {
        "model": args.model,
        "base_url": args.base_url,
        "context": args.context,
        "started_at_unix": int(time.time()),
        "calls": {},
        "reasoning": {},
        "errors": [],
    }

    for mode in ("nonstream", "stream"):
        started = time.monotonic()
        try:
            if mode == "stream":
                response, events = client.stream(_namespace_body(args.model))
            else:
                response = client.post(_namespace_body(args.model))
                events = []
            call = find_function_call(response, "mcp__diagnostic__echo")
            arguments = json.loads(call.get("arguments", "{}"))
            if arguments != {"value": "NAMESPACE_OK"}:
                raise AssertionError(f"unexpected echo arguments: {arguments!r}")
            report["calls"][mode] = {
                "passed": True,
                "status": response.get("status"),
                "name": call.get("name"),
                "arguments": arguments,
                "events": events,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        except Exception as exc:  # preserve all gate evidence before exiting
            report["calls"][mode] = {"passed": False, "error": str(exc)}
            report["errors"].append(f"namespace_{mode}: {exc}")

    for effort in ("none", "low", "medium", "high"):
        started = time.monotonic()
        try:
            response = client.post(_reasoning_body(args.model, effort))
            assert_completed_visible(response)
            visible = _output_text(response)
            reasoning = collect_reasoning_text(response)
            reasoning_tokens = count_tokens(args.base_url, reasoning) if reasoning else 0
            if effort == "low":
                if reasoning_tokens <= 64:
                    raise AssertionError(f"low reasoning was capped at {reasoning_tokens} tokens")
                if not _reasoning_ends_cleanly(reasoning):
                    raise AssertionError("low reasoning ends mid-sentence")
            report["reasoning"][effort] = {
                "passed": True,
                "status": response.get("status"),
                "reasoning_tokens": reasoning_tokens,
                "reasoning_characters": len(reasoning),
                "visible_characters": len(visible),
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        except Exception as exc:  # preserve all gate evidence before exiting
            report["reasoning"][effort] = {"passed": False, "error": str(exc)}
            report["errors"].append(f"reasoning_{effort}: {exc}")

    report["passed"] = not report["errors"]
    report["finished_at_unix"] = int(time.time())
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--api-key", default="")
    parser.add_argument("--context", type=int, default=204800)
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()

    report = run_probe(args)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
