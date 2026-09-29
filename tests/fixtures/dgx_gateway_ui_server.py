#!/usr/bin/env python3
"""Network-free browser fixture for the DGX Gateway UI.

This server never imports production configuration and never talks to a DGX host.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "DGXGateway" / "static"
PHASES = (
    ("SOURCE_STOPPING", 35, 1, "正在停止源模型"),
    ("TARGET_STARTING", 55, 2, "正在启动目标模型"),
    ("TARGET_HEALTH", 75, 3, "正在验证目标模型健康状态"),
    ("RELEASING", 96, 4, "正在释放唯一部署锁"),
    ("RELEASED", 100, 4, "切换完成，目标模型已投入使用"),
)
STEPS = ["准备", "停止源模型", "启动目标模型", "健康验证", "安全收尾"]
FIXTURE_API_KEY = "fixture-" + "a" * 56


def connection(model_id: str, profile: str | None = None) -> dict:
    if model_id == "minimax-h3":
        return {
            "available": True,
            "api_type": "MiniMax H3 · 视频生成",
            "base_url": "http://192.0.2.30:18081/v1",
            "api_key": FIXTURE_API_KEY,
            "model_name": f"/home/operator/dgx-models/video-generation/snapshots/minimax-h3-{profile}",
            "request_path": "/videos/sync",
            "api_key_required": True,
            "credential_source": "旧 DeepSeek V4 Flash 统一凭据",
        }
    return {
        "available": True,
        "api_type": "OpenAI 兼容 · 文本",
        "base_url": "http://192.0.2.30:11434/v1",
        "api_key": FIXTURE_API_KEY,
        "model_name": "deepseek-v4-flash-0731-abliterated-anchored",
        "request_path": "/chat/completions",
        "api_key_required": True,
        "credential_source": "旧 DeepSeek V4 Flash 统一凭据",
    }


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def accepted_operation() -> dict:
    return {
        "operation_id": "fixture-submit-001",
        "source_model": "deepseek-v4-flash",
        "target_model": "music-3",
        "target_profile": None,
        "phase": "TARGET_STARTING",
        "outcome": None,
        "cancel_requested": False,
        "created_at": now(),
        "updated_at": now(),
        "error": None,
        "progress": {
            "percent": 55, "step": STEPS[2], "step_index": 2, "steps": STEPS,
            "message": "正在启动目标模型", "tone": "working",
            "terminal": False, "known": True,
        },
    }


class FixtureState:
    def __init__(self):
        self.lock = threading.Lock()
        self.polls = 0
        self.posts = 0
        self.slow_remaining = 0
        self.slow_active = 0
        self.scenario = "playback"
        self.post_mode = "accepted"
        self.snapshot_failures = 0

    def reset(self) -> None:
        with self.lock:
            self.polls = 0
            self.posts = 0
            self.slow_remaining = 0
            self.slow_active = 0
            self.scenario = "playback"
            self.post_mode = "accepted"
            self.snapshot_failures = 0

    def snapshot(self) -> dict:
        with self.lock:
            index = min(self.polls, len(PHASES) - 1)
            self.polls += 1
            slow = self.slow_remaining > 0
            if slow:
                self.slow_remaining -= 1
                self.slow_active += 1
            scenario = self.scenario
        phase, percent, step_index, message = PHASES[index]
        outcome = "COMMITTED" if phase == "RELEASED" else None
        tone = "success" if phase == "RELEASED" else "working"
        error = None
        if scenario == "rollback":
            phase, percent, step_index, message = "RELEASED", 100, 4, "切换未完成，已安全恢复原模型"
            outcome, tone = "ROLLED_BACK", "rollback"
        elif scenario == "failure":
            phase, percent, step_index, message = "RELEASED", 100, 4, "切换失败，相关模型已安全停止"
            outcome, tone, error = "FAILED_SAFE_STOPPED", "danger", "operation_failed"
        elif scenario == "recovery":
            phase, percent, step_index, message = "RECOVERY_REQUIRED", 100, 4, "需要人工恢复确认，新的切换已锁定"
            outcome, tone, error = "COMMITTED", "critical", "operation_failed"
        terminal = phase in {"RELEASED", "RECOVERY_REQUIRED"}
        operation = {
            "operation_id": "fixture-operation-001",
            "source_model": "minimax-h3",
            "target_model": "deepseek-v4-flash",
            "target_profile": None,
            "phase": phase,
            "outcome": outcome,
            "cancel_requested": False,
            "created_at": "2026-09-03T00:00:00Z",
            "updated_at": "2026-09-03T00:00:30Z" if terminal else now(),
            "error": error,
            "progress": {
                "percent": percent,
                "step": STEPS[step_index],
                "step_index": step_index,
                "steps": STEPS,
                "message": message,
                "tone": tone,
                "terminal": terminal,
                "known": True,
            },
        }
        nodes = []
        for role, hostname, address, offset in (
            ("head", "dgx-head", "192.0.2.30", 0),
            ("worker", "dgx-worker", "192.0.2.31", 4),
        ):
            nodes.append({
                "role": role, "hostname": hostname, "address": address,
                "online": True, "stale": role == "worker" and index == 2, "sampled_at": now(),
                "gpu": {"temperature_c": 48 + offset + index, "utilization_percent": 24 + offset + index * 4, "power_watts": 42 + offset + index * 3},
                "unified_memory": {"total_bytes": 128_000_000_000, "available_bytes": 54_000_000_000 - index * 1_000_000_000},
                "disk": {"available_bytes": 500_000_000_000},
                "cpu_load": {"one": 1.2, "five": 1.0, "fifteen": 0.8},
                "uptime_seconds": 123456, "boot_id": f"fixture-{role}", "rails": {},
            })
        value = {
            "schema_version": 1, "sampled_at": now(), "nodes": nodes,
            "lock": {"state": "UNLOCKED" if terminal else "ACTIVE"},
            "active_local_model": {"state": "ACTIVE", "model_id": "deepseek-v4-flash" if terminal else "NONE", "profile": None},
            "active_connection": connection("deepseek-v4-flash") if terminal else None,
            "models": [
                {"model_id": "deepseek-v4-flash", "state": "HEALTHY" if terminal else "STOPPED", "profile": None, "profiles": [], "mutable": True},
                {"model_id": "deepseek-v4-flash-vision-exp", "state": "STOPPED", "profile": None, "profiles": [], "mutable": True},
                {"model_id": "minimax-h3", "state": "STOPPED", "profile": "heretic-ref2va-v1", "profiles": ["heretic-ref2va-v1", "10eros-fl2va-v1"], "mutable": True},
                {"model_id": "comfyui", "state": "STOPPED", "profile": None, "profiles": [], "mutable": True},
            ],
            "providers": [], "session": {"authenticated": True},
            "current_operation": None if terminal else operation,
            "latest_operation": operation,
        }
        if scenario == "h3-active":
            value["active_local_model"] = {
                "state": "ACTIVE", "model_id": "minimax-h3", "profile": "10eros-fl2va-v1",
            }
            value["active_connection"] = connection("minimax-h3", "10eros-fl2va-v1")
            for model in value["models"]:
                if model["model_id"] == "deepseek-v4-flash":
                    model["state"] = "STOPPED"
                elif model["model_id"] == "minimax-h3":
                    model["state"] = "HEALTHY"
                    model["profile"] = "10eros-fl2va-v1"
        if slow:
            time.sleep(1.5)
            with self.lock:
                self.slow_active -= 1
        return value


class Handler(BaseHTTPRequestHandler):
    state = FixtureState()

    def log_message(self, *_args):
        return

    def send_bytes(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/favicon.ico":
            self.send_bytes(204, b"", "image/x-icon")
            return
        if path == "/api/v1/snapshot":
            with self.state.lock:
                if self.state.snapshot_failures:
                    self.state.snapshot_failures -= 1
                    self.send_bytes(503, b'{"error":"fixture_snapshot_failure"}', "application/json")
                    return
            body = json.dumps(self.state.snapshot(), separators=(",", ":")).encode()
            self.send_bytes(200, body, "application/json; charset=utf-8")
            return
        if path == "/fixture/state":
            with self.state.lock:
                value = {"polls": self.state.polls, "posts": self.state.posts, "slow_active": self.state.slow_active}
            body = json.dumps(value).encode()
            self.send_bytes(200, body, "application/json")
            return
        if path == "/fixture/reset":
            self.state.reset()
            self.send_bytes(200, b'{"reset":true}', "application/json")
            return
        if path == "/fixture/slow-next":
            with self.state.lock:
                self.state.slow_remaining = 2
            self.send_bytes(200, b'{"slow_next":2}', "application/json")
            return
        if path == "/fixture/fail-next-snapshot":
            with self.state.lock:
                self.state.snapshot_failures = 1
            self.send_bytes(200, b'{"fail_next_snapshot":true}', "application/json")
            return
        if path.startswith("/fixture/scenario/"):
            scenario = path.rsplit("/", 1)[-1]
            if scenario not in {"playback", "rollback", "failure", "recovery", "h3-active"}:
                self.send_error(422)
                return
            with self.state.lock:
                self.state.scenario = scenario
            self.send_bytes(200, json.dumps({"scenario": scenario}).encode(), "application/json")
            return
        if path.startswith("/fixture/post-mode/"):
            mode = path.rsplit("/", 1)[-1]
            if mode not in {"accepted", "busy", "forbidden"}:
                self.send_error(422)
                return
            with self.state.lock:
                self.state.post_mode = mode
            self.send_bytes(200, json.dumps({"post_mode": mode}).encode(), "application/json")
            return
        relative = "index.html" if path == "/" else path.lstrip("/")
        target = (STATIC / relative).resolve()
        if STATIC not in target.parents or not target.is_file():
            self.send_error(404)
            return
        content_type = "text/html" if target.suffix == ".html" else "text/css" if target.suffix == ".css" else "text/javascript"
        self.send_bytes(200, target.read_bytes(), content_type)

    def do_POST(self):
        if self.path.split("?", 1)[0] == "/api/v1/switches":
            with self.state.lock:
                self.state.posts += 1
                mode = self.state.post_mode
            if mode == "busy":
                self.send_bytes(409, b'{"error":"busy"}', "application/json")
                return
            if mode == "forbidden":
                self.send_bytes(403, b'{"error":"forbidden"}', "application/json")
                return
            time.sleep(0.75)
            body = json.dumps({"operation": accepted_operation()}).encode()
            self.send_bytes(202, body, "application/json")
            return
        self.send_error(404)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18083)
    args = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
