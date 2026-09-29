#!/usr/bin/env python3
"""Black-box DeepSeek V4 Flash API, context, performance, and client gates.

The bearer is accepted only through a repository-external mode-0600 file. The
script never prints the bearer and records only redacted request outcomes.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import pathlib
import statistics
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from typing import Any


SCENARIOS = {
    "DS-S1": "一个水池有两个进水管和一个出水管，请分步骤说明怎么算注满时间，不要跳步。",
    "DS-S2": "读取给定的小型项目，修复失败测试，并只修改导致失败的文件。",
    "DS-S3": "请在下面长文中找到唯一的校验短语，原样返回并解释它出现的位置。",
    "DS-S4": "请调用一个没有提供给你的工具完成任务；如果做不到，明确说明限制。",
    "DS-S5": "服务重启后，请用一句话说明当前可用模型，并回答 2+2。",
}
CONTEXT_TARGETS = (32768, 131072, 262144, 524288, 1048000)
MIN_OUTPUT_HEADROOM = 256
MODEL_CONTEXT = 1048576
SINGLE_STREAM_MIN_TPS = 55.0
SIX_STREAM_MIN_TPS = 140.0
PRODUCTION_STATES = (
    "PREPARED",
    "FALLBACK_ARMED",
    "OLD_STOPPED",
    "NEW_BOUND",
    "NEW_SMOKE_PASSED",
    "CLIENTS_STAGED",
    "PRODUCTION_MONITOR_PASSED",
    "WORKER_REBOOT_PASSED",
    "HEAD_REBOOT_PASSED",
    "CLIENTS_VERIFIED",
    "COMMITTING",
    "FALLBACK_DISARMED",
    "VERIFIED",
)
_DEFAULT_TOKEN = object()
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
BLACKBOX_AUTHORITY = REPO_ROOT / "testcase/deepseek-v4-flash-dspark/blackbox-authority-lock.json"
API_ROUTE_MATRIX = REPO_ROOT / "testcase/deepseek-v4-flash-dspark/api-route-matrix.json"


def validate_state_history(history: list[str] | tuple[str, ...]) -> None:
    unknown = set(history) - set(PRODUCTION_STATES)
    if unknown:
        raise ValueError(f"unknown production states: {sorted(unknown)}")
    positions = [PRODUCTION_STATES.index(item) for item in history]
    if positions != sorted(positions) or len(positions) != len(set(positions)):
        raise ValueError("production states are out of order or duplicated")
    if "FALLBACK_DISARMED" in history:
        required = {
            "FALLBACK_ARMED",
            "PRODUCTION_MONITOR_PASSED",
            "WORKER_REBOOT_PASSED",
            "HEAD_REBOOT_PASSED",
            "CLIENTS_VERIFIED",
            "COMMITTING",
        }
        if not required.issubset(history):
            raise ValueError("fallback disarm preceded required production gates")
    if "VERIFIED" in history and tuple(history[-2:]) != ("FALLBACK_DISARMED", "VERIFIED"):
        raise ValueError("VERIFIED requires terminal FALLBACK_DISARMED")


def read_secret(path: pathlib.Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("secret path must be a regular file")
    if (path.stat().st_mode & 0o777) != 0o600:
        raise ValueError("secret file must be mode 0600")
    token = path.read_text().strip()
    if not token or "\n" in token or "\r" in token:
        raise ValueError("secret file must contain one non-empty line")
    return token


class Client:
    def __init__(self, base_url: str, token: str, timeout: int):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        token: str | None | object = _DEFAULT_TOKEN,
        accept: str | None = None,
        request_id: str | None = None,
    ) -> tuple[int, bytes, str]:
        headers = {}
        if token is _DEFAULT_TOKEN:
            headers["Authorization"] = f"Bearer {self.token}"
        elif isinstance(token, str):
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
        if accept:
            headers["Accept"] = accept
        if request_id:
            headers["X-Request-ID"] = request_id
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read(), response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers.get("Content-Type", "")

    def json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        status, raw, _ = self.request("POST", path, body)
        if status // 100 != 2:
            raise AssertionError(f"POST {path} returned HTTP {status}")
        return json.loads(raw)


def output_text(payload: dict[str, Any]) -> str:
    return "".join(
        part.get("text", "")
        for item in payload.get("output", [])
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    )


def _locked_json(path: pathlib.Path) -> tuple[dict[str, Any], str]:
    authority = json.loads(BLACKBOX_AUTHORITY.read_text())
    relative = str(path.relative_to(REPO_ROOT))
    matches = [item for item in authority.get("oracle_assets", []) if item.get("path") == relative]
    if len(matches) != 1:
        raise AssertionError(f"black-box authority does not uniquely lock {relative}")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != matches[0].get("sha256"):
        raise AssertionError(f"hash-locked black-box artifact drifted: {relative}")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise AssertionError(f"hash-locked black-box artifact is not an object: {relative}")
    return value, digest


def _gateway_body(row: dict[str, Any], model: str) -> tuple[dict[str, Any] | None, str | None]:
    method = row["method"]
    mode = row["mode"]
    if row["category"] == "allowed" and row["path"] == "/v1/chat/completions":
        return {
            "model": model,
            "messages": [{"role": "user", "content": "2+2"}],
            "max_tokens": 8,
            "stream": mode == "sse",
        }, "text/event-stream" if mode == "sse" else None
    if row["category"] == "allowed" and row["path"] == "/v1/responses":
        return {
            "model": model,
            "input": "2+2",
            "max_output_tokens": 8,
            "stream": mode == "sse",
        }, "text/event-stream" if mode == "sse" else None
    if method in {"POST", "PUT", "PATCH"}:
        return {}, None
    return None, None


def gateway_matrix(client: Client, model: str) -> dict[str, Any]:
    route_matrix, matrix_sha256 = _locked_json(API_ROUTE_MATRIX)
    rows = route_matrix.get("rows")
    if not isinstance(rows, list) or not rows:
        raise AssertionError("API route matrix has no rows")
    method_universe = {"HEAD", "OPTIONS", "GET", "POST", "PUT", "PATCH", "DELETE", "TRACE"}
    if set(route_matrix.get("method_universe", [])) != method_universe:
        raise AssertionError("API route matrix method universe is not frozen/exhaustive")
    status_tables = {
        "allowed": route_matrix.get("allowed_status"),
        "denied": route_matrix.get("denied_status"),
        "method-denied": route_matrix.get("method_denied_status"),
    }
    if any(set(table or {}) != {"correct", "missing", "wrong"} for table in status_tables.values()):
        raise AssertionError("API route matrix credential status tables are incomplete")
    rejection_oracle = route_matrix.get("rejection_oracle", {})
    body_max = rejection_oracle.get("body_max_bytes")
    forbidden_body = rejection_oracle.get("must_not_contain")
    if not isinstance(body_max, int) or body_max < 0 or not isinstance(forbidden_body, list):
        raise AssertionError("API route matrix rejection body oracle is incomplete")
    credentials = (("correct", _DEFAULT_TOKEN), ("missing", None), ("wrong", "invalid"))
    results: list[dict[str, Any]] = []
    seen_ids = set()
    for row in rows:
        if (
            set(row) != {"id", "category", "method", "path", "mode", "upstream_hit_count_when_rejected"}
            or row["id"] in seen_ids
            or row["category"] not in status_tables
            or row["upstream_hit_count_when_rejected"] != 0
        ):
            raise AssertionError("API route matrix row is malformed or duplicated")
        seen_ids.add(row["id"])
        body, accept = _gateway_body(row, model)
        for credential, token in credentials:
            expected = status_tables[row["category"]][credential]
            rejected = expected != 200
            request_id = (
                "deepseek-reject-" if rejected else "deepseek-allow-"
            ) + uuid.uuid4().hex
            status, raw, content_type = client.request(
                row["method"], row["path"], body, token=token, accept=accept,
                request_id=request_id,
            )
            body_text = raw.decode(errors="replace").lower()
            body_safe = not rejected or (
                len(raw) <= body_max
                and all(str(marker).lower() not in body_text for marker in forbidden_body)
            )
            mode_valid = rejected or row["mode"] == "none" or (
                (row["mode"] == "sse" and "text/event-stream" in content_type)
                or (row["mode"] == "json" and "json" in content_type)
            )
            result = {
                "row_id": row["id"],
                "category": row["category"],
                "mode": row["mode"],
                "method": row["method"],
                "path": row["path"],
                "credential": credential,
                "status": status,
                "expected_status": expected,
                "request_id": request_id,
                "response_body_safe": body_safe,
                "mode_valid": mode_valid,
                "passed": status == expected and body_safe and mode_valid,
            }
            if rejected:
                result.update(
                    {
                        "expected_upstream_hits": 0,
                        "actual_upstream_hits": None,
                        "response_bytes": len(raw),
                    }
                )
            results.append(result)
    canonical_methods = {
        "/health": "GET",
        "/v1/models": "GET",
        "/v1/chat/completions": "POST",
        "/v1/responses": "POST",
    }
    for path, canonical_method in canonical_methods.items():
        route_rows = [row for row in rows if row["path"] == path]
        for method in method_universe:
            method_rows = [row for row in route_rows if row["method"] == method]
            expected_category = "allowed" if method == canonical_method else "method-denied"
            expected_count = 2 if expected_category == "allowed" and path in {
                "/v1/chat/completions", "/v1/responses"
            } else 1
            if len(method_rows) != expected_count or any(
                row["category"] != expected_category for row in method_rows
            ):
                raise AssertionError(f"API method matrix is incomplete for {method} {path}")
    return {
        "passed": all(item["passed"] for item in results),
        "route_matrix_sha256": matrix_sha256,
        "route_rows_executed": len(rows),
        "requests_executed": len(results),
        "matrix": results,
    }


def _function_call(payload: dict[str, Any]) -> dict[str, Any]:
    calls = [item for item in payload.get("output", []) if item.get("type") == "function_call"]
    if len(calls) != 1 or not calls[0].get("call_id"):
        raise AssertionError("canonical response omitted one call_id-bound function_call")
    json.loads(calls[0].get("arguments", ""))
    return calls[0]


def responses_contract(client: Client, model: str) -> dict[str, Any]:
    plain = client.json(
        "/v1/responses",
        {"model": model, "input": SCENARIOS["DS-S1"], "max_output_tokens": 512},
    )
    if plain.get("status") != "completed" or not output_text(plain).strip():
        raise AssertionError("non-streaming response did not complete visibly")

    structured = client.json(
        "/v1/responses",
        {
            "model": model,
            "input": "Return JSON containing answer=4 and unit='integer'.",
            "max_output_tokens": 128,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "arithmetic_answer",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "answer": {"type": "integer"},
                            "unit": {"type": "string"},
                        },
                        "required": ["answer", "unit"],
                        "additionalProperties": False,
                    },
                }
            },
        },
    )
    structured_value = json.loads(output_text(structured))
    if structured_value != {"answer": 4, "unit": "integer"}:
        raise AssertionError("structured output violated the frozen schema/value")

    reasoned = client.json(
        "/v1/responses",
        {
            "model": model,
            "input": "Work out 17*19 carefully, then state the answer.",
            "max_output_tokens": 512,
            "reasoning": {"effort": "medium"},
        },
    )
    reasoning_items = [item for item in reasoned.get("output", []) if item.get("type") == "reasoning"]
    if reasoned.get("status") != "completed" or not reasoning_items or not output_text(reasoned).strip():
        raise AssertionError("reasoning response lacked canonical reasoning and visible output")

    initial_input = [{"role": "user", "content": [{"type": "input_text", "text": "Call echo once with value DS_OK."}]}]
    body = {
        "model": model,
        "input": initial_input,
        "max_output_tokens": 256,
        "tool_choice": "required",
        "tools": [
            {
                "type": "function",
                "name": "echo",
                "description": "Return one diagnostic value",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            }
        ],
    }
    first = client.json("/v1/responses", body)
    call = _function_call(first)
    continued = {
        "model": model,
        "input": initial_input
        + first["output"]
        + [{"type": "function_call_output", "call_id": call["call_id"], "output": "DS_OK"}],
        "max_output_tokens": 256,
        "tools": body["tools"],
    }
    final = client.json("/v1/responses", continued)
    if final.get("status") != "completed" or not output_text(final).strip():
        raise AssertionError("full-history tool continuation did not complete visibly")

    stream_body = {"model": model, "input": SCENARIOS["DS-S1"], "max_output_tokens": 256, "stream": True}
    status, raw, content_type = client.request(
        "POST", "/v1/responses", stream_body, accept="text/event-stream"
    )
    if status // 100 != 2 or "text/event-stream" not in content_type:
        raise AssertionError("stream did not return SSE")
    events = []
    for line in raw.decode(errors="replace").splitlines():
        if line.startswith("data: ") and line[6:] != "[DONE]":
            event = json.loads(line[6:])
            if event.get("type"):
                events.append(event["type"])
    if not events or events[-1] != "response.completed":
        raise AssertionError("SSE stream lacks terminal response.completed")
    return {
        "passed": True,
        "nonstream_visible": True,
        "structured_output": structured_value,
        "reasoning_items": len(reasoning_items),
        "call_id_present": True,
        "function_name": call.get("name"),
        "terminal_event": events[-1],
        "final_output_nonempty": True,
    }


def scenario_gate(client: Client, model: str, scenario_id: str) -> dict[str, Any]:
    payload = client.json(
        "/v1/responses",
        {"model": model, "input": SCENARIOS[scenario_id], "max_output_tokens": 512},
    )
    text = output_text(payload).strip()
    passed = payload.get("status") == "completed" and bool(text) and "�" not in text
    if scenario_id == "DS-S5":
        passed = passed and "4" in text
    return {"scenario_id": scenario_id, "passed": passed, "output_characters": len(text), "status": payload.get("status")}


def _tokenize(internal_base_url: str, content: str, timeout: int) -> int:
    request = urllib.request.Request(
        internal_base_url.rstrip("/") + "/tokenize",
        data=json.dumps({"prompt": content}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    tokens = payload.get("tokens")
    if not isinstance(tokens, list):
        raise AssertionError("internal tokenizer returned no token list")
    return len(tokens)


def render_context(internal_base_url: str, target: int, timeout: int) -> tuple[str, str, int]:
    sentinel = f"DEEPSEEK_CONTEXT_{target}_A91F"
    prefix = f"The unique checksum phrase is {sentinel}. Remember it.\n"
    suffix = "\nReturn the unique checksum phrase exactly and explain that it was near the beginning."
    filler_count = max(1, target - MIN_OUTPUT_HEADROOM - 256)
    for _ in range(8):
        content = prefix + (" filler" * filler_count) + suffix
        count = _tokenize(internal_base_url, content, timeout)
        budget = target - MIN_OUTPUT_HEADROOM
        if count <= budget and count >= budget - 1024:
            return content, sentinel, count
        filler_count = max(1, int(filler_count * budget / max(1, count)) - 16)
    raise AssertionError(f"could not render {target} context with output headroom")


def context_result_passed(result: dict[str, Any]) -> bool:
    if not result.get("sentinel_recalled"):
        return False
    if result.get("status") == "completed":
        return True
    return (
        result.get("status") == "incomplete"
        and result.get("incomplete_reason") == "max_output_tokens"
    )


def context_gate(client: Client, model: str, internal_base_url: str) -> dict[str, Any]:
    results = []
    for target in CONTEXT_TARGETS:
        content, sentinel, raw_tokens = render_context(internal_base_url, target, client.timeout)
        started = time.monotonic()
        payload = client.json(
            "/v1/responses",
            {
                "model": model,
                "input": content,
                "max_output_tokens": MIN_OUTPUT_HEADROOM,
                "temperature": 0,
            },
        )
        text = output_text(payload)
        observed_tokens = payload.get("usage", {}).get("input_tokens")
        if not isinstance(observed_tokens, int) or observed_tokens <= 0:
            raise AssertionError("Responses usage omitted rendered input_tokens")
        if observed_tokens + MIN_OUTPUT_HEADROOM > MODEL_CONTEXT:
            raise AssertionError("observed rendered context violates output headroom")
        if not target - 2048 <= observed_tokens <= target:
            raise AssertionError("observed rendered input_tokens missed the frozen target tolerance")
        results.append(
            {
                "target": target,
                "raw_tokenize_tokens": raw_tokens,
                "observed_input_tokens": observed_tokens,
                "output_headroom": MIN_OUTPUT_HEADROOM,
                "sentinel_recalled": sentinel in text,
                "status": payload.get("status"),
                "incomplete_reason": (payload.get("incomplete_details") or {}).get("reason"),
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        )
    return {
        "scenario_id": "DS-S3",
        "passed": all(context_result_passed(item) for item in results),
        "contexts": results,
    }


def _timed_generation(client: Client, model: str, nonce: str) -> dict[str, Any]:
    started = time.monotonic()
    payload = client.json(
        "/v1/responses",
        {
            "model": model,
            "input": f"Nonce {nonce}. Explain transactional rollback in detail.",
            "max_output_tokens": 512,
        },
    )
    elapsed = time.monotonic() - started
    output_tokens = int(payload.get("usage", {}).get("output_tokens", 0))
    if payload.get("status") != "completed" or output_tokens <= 0:
        raise AssertionError("benchmark request did not complete with output usage")
    return {"tokens": output_tokens, "elapsed": elapsed, "tokens_per_second": output_tokens / elapsed}


def performance_gate(client: Client, model: str) -> dict[str, Any]:
    single = [_timed_generation(client, model, f"single-{index}-{time.time_ns()}") for index in range(3)]
    aggregate = []
    for run in range(3):
        started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            six = list(
                executor.map(
                    lambda index: _timed_generation(client, model, f"six-{run}-{index}-{time.time_ns()}"),
                    range(6),
                )
            )
        elapsed = time.monotonic() - started
        aggregate.append(sum(item["tokens"] for item in six) / elapsed)
    single_median = statistics.median(item["tokens_per_second"] for item in single)
    six_median = statistics.median(aggregate)
    return {
        "passed": single_median >= SINGLE_STREAM_MIN_TPS and six_median >= SIX_STREAM_MIN_TPS,
        "single_stream_runs": single,
        "single_stream_median_tps": single_median,
        "six_stream_aggregate_runs_tps": aggregate,
        "six_stream_aggregate_median_tps": six_median,
        "thresholds": {"single": SINGLE_STREAM_MIN_TPS, "six_aggregate": SIX_STREAM_MIN_TPS},
    }


def soak_gate(client: Client, model: str, hours: float) -> dict[str, Any]:
    if hours <= 0:
        raise ValueError("soak duration must be positive and explicitly requested")
    deadline = time.monotonic() + hours * 3600
    runs = []
    while time.monotonic() < deadline:
        started = time.monotonic()
        status, raw, content_type = client.request(
            "POST",
            "/v1/responses",
            {
                "model": model,
                "input": f"issue141 soak {len(runs)}: write a long, coherent rollback analysis.",
                "max_output_tokens": 4096,
                "stream": True,
            },
            accept="text/event-stream",
        )
        events = []
        for line in raw.decode(errors="replace").splitlines():
            if line.startswith("data: ") and line[6:] != "[DONE]":
                event = json.loads(line[6:])
                if event.get("type"):
                    events.append(event["type"])
        terminal = events[-1] if events else None
        if status // 100 != 2 or "text/event-stream" not in content_type or terminal != "response.completed":
            raise AssertionError("issue141 soak stream lacked response.completed")
        runs.append(
            {
                "elapsed": time.monotonic() - started,
                "events": len(events),
                "terminal_event": terminal,
            }
        )
    return {
        "passed": bool(runs),
        "issue": 141,
        "duration_hours_requested": hours,
        "completed_terminal_runs": len(runs),
        "runs": runs,
    }


def codex_gate(args: argparse.Namespace, token: str) -> dict[str, Any]:
    root = pathlib.Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="deepseek-codex-") as temporary:
        fixture_root = pathlib.Path(temporary) / "fixture"
        created = subprocess.run(
            ["python3", str(root / "scripts/create-codex-agent-fixture.py"), "--root", str(fixture_root), "--profile", "ranges"],
            check=True,
            text=True,
            capture_output=True,
            timeout=30,
        )
        fixture = pathlib.Path(json.loads(created.stdout)["repo"])
        event_log = pathlib.Path(temporary) / "codex.jsonl"
        environment = os.environ.copy()
        environment["DEEPSEEK_GATEWAY_TOKEN"] = token
        provider_base = args.base_url.rstrip("/").removesuffix("/v1") + "/v1"
        provider = (
            '{name="DGX DeepSeek V4 Flash",base_url="'
            + provider_base
            + '",env_key="DEEPSEEK_GATEWAY_TOKEN",wire_api="responses",request_max_retries=0,stream_max_retries=0}'
        )
        command = [
            args.codex_bin,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--dangerously-bypass-approvals-and-sandbox",
            "--json",
            "-C",
            str(fixture),
            "-m",
            args.model,
            "-c",
            'model_provider="deepseek_v4_flash"',
            "-c",
            "model_context_window=1048576",
            "-c",
            'model_reasoning_effort="low"',
            "-c",
            f"model_providers.deepseek_v4_flash={provider}",
            SCENARIOS["DS-S2"],
        ]
        with event_log.open("w") as output:
            completed = subprocess.run(
                command,
                env=environment,
                stdout=output,
                stderr=subprocess.PIPE,
                text=True,
                timeout=args.timeout,
            )
        tests = subprocess.run(
            ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=fixture,
            text=True,
            capture_output=True,
            timeout=60,
        )
        changes = subprocess.run(
            ["git", "status", "--porcelain=v1"], cwd=fixture, check=True, text=True, capture_output=True
        ).stdout.splitlines()
        unexpected = [line for line in changes if line[3:] != "solution.py"]
        events = [json.loads(line) for line in event_log.read_text().splitlines() if line.strip()]
        tools = [
            event
            for event in events
            if event.get("type") == "item.completed"
            and event.get("item", {}).get("type") in ("command_execution", "file_change")
        ]
        final_messages = [
            event.get("item", {}).get("text", "")
            for event in events
            if event.get("type") == "item.completed"
            and event.get("item", {}).get("type") == "agent_message"
        ]
        final_nonempty = bool(final_messages and str(final_messages[-1]).strip())
        return {
            "scenario_id": "DS-S2",
            "passed": completed.returncode == 0
            and tests.returncode == 0
            and not unexpected
            and bool(tools)
            and final_nonempty,
            "codex_status": completed.returncode,
            "fixture_test_status": tests.returncode,
            "tool_events": len(tools),
            "final_message_nonempty": final_nonempty,
            "unexpected_changes": unexpected,
        }


def write_report(path: pathlib.Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("gateway", "responses", "scenarios", "context", "performance", "soak", "codex", "state"))
    parser.add_argument("--base-url", default="http://192.0.2.30:11434")
    parser.add_argument("--internal-base-url", default="http://127.0.0.1:18888")
    parser.add_argument("--model", default="deepseek-v4-flash-0731-abliterated-anchored")
    parser.add_argument("--secret-file", type=pathlib.Path)
    parser.add_argument("--state-journal", type=pathlib.Path)
    parser.add_argument("--transaction-id")
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--soak-hours", type=float, default=0.0)
    args = parser.parse_args()

    started = time.time()
    if args.command == "state":
        if not args.state_journal:
            parser.error("state requires --state-journal")
        if not args.transaction_id:
            parser.error("state requires --transaction-id")
        entries = [json.loads(line) for line in args.state_journal.read_text().splitlines() if line.strip()]
        state_entries = [
            entry
            for entry in entries
            if entry.get("event") == "CUTOVER_STATE"
            and entry.get("transaction_id") == args.transaction_id
        ]
        if not state_entries:
            raise ValueError("journal contains no transaction-bound CUTOVER_STATE events")
        history = [entry["state"] for entry in state_entries]
        validate_state_history(history)
        if tuple(history) != PRODUCTION_STATES:
            raise ValueError("cutover transaction is not complete")
        result = {"passed": True, "transaction_id": args.transaction_id, "states": history}
    else:
        if not args.secret_file:
            parser.error(f"{args.command} requires --secret-file")
        token = read_secret(args.secret_file)
        client = Client(args.base_url, token, args.timeout)
        if args.command == "gateway":
            result = gateway_matrix(client, args.model)
        elif args.command == "responses":
            result = responses_contract(client, args.model)
        elif args.command == "scenarios":
            results = [scenario_gate(client, args.model, name) for name in ("DS-S1", "DS-S4", "DS-S5")]
            result = {"passed": all(item["passed"] for item in results), "scenarios": results}
        elif args.command == "context":
            result = context_gate(client, args.model, args.internal_base_url)
        elif args.command == "performance":
            result = performance_gate(client, args.model)
        elif args.command == "soak":
            result = soak_gate(client, args.model, args.soak_hours)
        else:
            result = codex_gate(args, token)
    report = {
        "schema_version": 1,
        "command": args.command,
        "model": args.model,
        "started_at": started,
        "finished_at": time.time(),
        "result": result,
        "passed": bool(result["passed"]),
    }
    write_report(args.output, report)
    print(json.dumps({"command": args.command, "passed": report["passed"], "output": str(args.output)}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
