from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CONTEXT = ROOT / "containers/deepseek-v4-flash-vision-exp"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TRACE = load_module("deepseek_v4_vision_trace", CONTEXT / "deepseek_v4_vision_trace.py")
with mock.patch.dict(__import__("sys").modules, {"deepseek_v4_vision_trace": TRACE}):
    GUARD = load_module("vision_media_guard", CONTEXT / "vision_media_guard.py")
PATCHER = load_module("dsv4_runtime_patcher", CONTEXT / "apply_runtime_patches.py")
LAUNCHER = load_module("dsv4_launch_vllm", CONTEXT / "launch_vllm.py")


def png_uri(payload: bytes = b"\x89PNG\r\n\x1a\nfixture") -> str:
    return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")


def request_body(image: object | None = None) -> bytes:
    parts: list[dict] = [{"type": "text", "text": "describe the image"}]
    if image is not None:
        parts.append({"type": "image_url", "image_url": image})
    return json.dumps(
        {"model": "deepseek-v4-flash-vision-exp", "messages": [{"role": "user", "content": parts}]},
        separators=(",", ":"),
    ).encode()


class MediaGuardTest(unittest.TestCase):
    def test_accepts_plain_text_and_one_canonical_data_image(self):
        self.assertIsInstance(GUARD.validate_chat_body(request_body()), dict)
        self.assertIsInstance(
            GUARD.validate_chat_body(request_body({"url": png_uri(), "detail": "auto"})),
            dict,
        )

    def test_assistant_tool_call_roundtrip_allows_null_or_missing_text(self):
        call = {"id": "call_probe", "type": "function", "function": {"name": "Read", "arguments": '{"file_path":"nonce.txt"}'}}
        for with_null in (True, False):
            assistant = {"role": "assistant", "tool_calls": [call]}
            if with_null:
                assistant["content"] = None
            body = {"messages": [assistant, {"role": "tool", "tool_call_id": "call_probe", "content": "test-result"}]}
            self.assertEqual(GUARD.validate_chat_body(json.dumps(body).encode()), body)

    def test_null_content_does_not_bypass_media_or_schema_gate(self):
        invalid = [
            {"role": "user", "content": None},
            {"role": "assistant", "content": None},
            {"role": "assistant", "content": None, "tool_calls": []},
            {"role": "assistant", "content": None, "tool_calls": [None]},
            {"role": "assistant", "content": None, "tool_calls": [{"type": "image_url", "url": "https://example.invalid/a.png"}]},
        ]
        for message in invalid:
            with self.subTest(message=message), self.assertRaises(GUARD.GuardRejected):
                GUARD.validate_chat_body(json.dumps({"messages": [message]}).encode())
        call = {"id": "call_probe", "type": "function", "function": {"name": "Read", "arguments": "{}"}}
        body = json.loads(request_body({"url": "http://127.0.0.1/image.png"}))
        body["messages"].insert(0, {"role": "assistant", "content": None, "tool_calls": [call]})
        with self.assertRaisesRegex(GUARD.GuardRejected, "image_url_must_be_canonical"):
            GUARD.validate_chat_body(json.dumps(body).encode())

    def test_rejects_duplicate_keys_and_malformed_base64(self):
        with self.assertRaisesRegex(GUARD.GuardRejected, "duplicate_json_key"):
            GUARD.validate_chat_body(b'{"messages":[],"messages":[]}')
        with self.assertRaisesRegex(GUARD.GuardRejected, "base64"):
            GUARD.validate_chat_body(request_body({"url": "data:image/png;base64,%%%="}))

    def test_rejects_remote_local_string_uuid_and_multiple_images(self):
        rejected = (
            {"url": "https://example.invalid/image.png"},
            {"url": "HTTP://127.0.0.1/image.png"},
            {"url": "//169.254.169.254/latest/meta-data"},
            {"url": "file:///etc/passwd"},
            {"url": "/tmp/image.png"},
            png_uri(),
            {"url": png_uri(), "uuid": "not-allowed"},
        )
        for value in rejected:
            with self.subTest(value=value):
                with self.assertRaises(GUARD.GuardRejected):
                    GUARD.validate_chat_body(request_body(value))

        body = json.loads(request_body({"url": png_uri()}))
        body["messages"][0]["content"].append(
            {"type": "image_url", "image_url": {"url": png_uri(b"\x89PNG\r\n\x1a\nsecond")}}
        )
        with self.assertRaisesRegex(GUARD.GuardRejected, "only_one_image"):
            GUARD.validate_chat_body(json.dumps(body).encode())

    def test_rejects_alternate_media_part(self):
        for kind in ("input_audio", "video_url", "image", "input_image"):
            body = {
                "messages": [{"role": "user", "content": [{"type": kind, "data": "x"}]}]
            }
            with self.subTest(kind=kind), self.assertRaisesRegex(
                GUARD.GuardRejected, "unsupported_media_part"
            ):
                GUARD.validate_chat_body(json.dumps(body).encode())

    def test_asgi_replays_valid_body_and_bounds_errors(self):
        seen: list[bytes] = []

        async def downstream(_scope, receive, send):
            seen.append((await receive())["body"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        async def invoke(body: bytes, path: str = "/v1/chat/completions", method: str = "POST"):
            messages = [{"type": "http.request", "body": body, "more_body": False}]
            sent: list[dict] = []

            async def receive():
                return messages.pop(0)

            async def send(value):
                sent.append(value)

            scope = {
                "type": "http",
                "method": method,
                "path": path,
                "headers": [(b"content-type", b"application/json")],
            }
            await GUARD.VisionMediaGuard(downstream)(scope, receive, send)
            return sent

        valid = request_body({"url": png_uri()})
        sent = asyncio.run(invoke(valid))
        self.assertEqual(sent[0]["status"], 200)
        self.assertEqual(seen, [valid])
        sent = asyncio.run(invoke(b"{}"))
        self.assertEqual(sent[0]["status"], 422)
        sent = asyncio.run(invoke(valid, "/v1/responses"))
        self.assertEqual(sent[0]["status"], 404)
        sent = asyncio.run(invoke(b"", "/metrics", "GET"))
        self.assertEqual(sent[0]["status"], 200)

    def test_asgi_replay_propagates_disconnect_after_body(self):
        observed: list[dict] = []

        async def downstream(_scope, receive, send):
            observed.append(await receive())
            observed.append(await receive())
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        async def invoke():
            body = request_body()
            messages = [
                {"type": "http.request", "body": body, "more_body": False},
                {"type": "http.disconnect"},
            ]

            async def receive():
                return messages.pop(0)

            async def send(_value):
                return None

            scope = {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/completions",
                "headers": [(b"content-type", b"application/json")],
            }
            await GUARD.VisionMediaGuard(downstream)(scope, receive, send)

        asyncio.run(invoke())
        self.assertEqual(observed[0]["type"], "http.request")
        self.assertEqual(observed[1], {"type": "http.disconnect"})


class NativeTraceTest(unittest.TestCase):
    def test_disabled_trace_creates_no_file(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {"DSV4_VISION_TRACE": "0", "DSV4_VISION_TRACE_DIR": directory},
            clear=False,
        ):
            TRACE.emit_trace("vision.enter", tensor=object())
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_trace_reads_metadata_not_tensor_values_and_hash_chains(self):
        class FakeTensor:
            shape = (1, 2, 3)
            dtype = "bf16"
            device = "cuda:0"

            def item(self):
                raise AssertionError("tensor value was read")

            def tolist(self):
                raise AssertionError("tensor value was read")

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {
                "DSV4_VISION_TRACE": "1",
                "DSV4_VISION_TRACE_NONCE": "trace_session_123456",
                "DSV4_VISION_TRACE_DIR": directory,
                "RANK": "1",
                "WORLD_SIZE": "2",
            },
            clear=False,
        ):
            TRACE._SEQUENCE = 0
            TRACE._PREVIOUS_DIGEST = "0" * 64
            TRACE.emit_trace("vision.enter", tensor=FakeTensor())
            TRACE.emit_trace("vision.exit", tensor=FakeTensor())
            rows = [json.loads(line) for line in (Path(directory) / "native-rank-1.jsonl").read_text().splitlines()]
        self.assertEqual([row["event"] for row in rows], ["vision.enter", "vision.exit"])
        self.assertEqual(rows[0]["metadata"]["tensor"]["shape"], ["1", "2", "3"])
        self.assertEqual(rows[1]["previous_sha256"], rows[0]["sha256"])
        for row in rows:
            expected = dict(row)
            observed = expected.pop("sha256")
            canonical = json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
            self.assertEqual(observed, hashlib.sha256(canonical).hexdigest())

    def test_trace_uses_fixed_role_topology_when_runtime_omits_rank_env(self):
        class FakeTensor:
            shape = (1, 2)
            dtype = "bf16"
            device = "cuda:0"

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {
                "DSV4_VISION_TRACE": "1",
                "DSV4_VISION_TRACE_NONCE": "trace_role_fallback_1234",
                "DSV4_VISION_TRACE_DIR": directory,
                "VISION_ROLE": "worker",
            },
            clear=True,
        ):
            TRACE._SEQUENCE = 0
            TRACE._PREVIOUS_DIGEST = "0" * 64
            TRACE.emit_trace("vision.enter", tensor=FakeTensor())
            row = json.loads(
                (Path(directory) / "native-rank-1.jsonl").read_text().strip()
            )
        self.assertEqual(row["identity"]["role"], "worker")
        self.assertEqual(row["identity"]["rank"], "1")
        self.assertEqual(row["identity"]["world_size"], "2")


class RuntimeContextTest(unittest.TestCase):
    def test_launcher_requires_controller_media_boundary_arguments(self):
        required = [
            "/models/deepseek-v4-flash-vision-exp",
            "--distributed-executor-backend",
            "mp",
            "--nnodes",
            "2",
            "--node-rank",
            "0",
            "--master-addr",
            "192.0.2.33",
            "--master-port",
            "25001",
            "--tensor-parallel-size",
            "2",
            "--pipeline-parallel-size",
            "1",
            "--gpu-memory-utilization",
            "0.85",
            "--middleware",
            "vision_media_guard.VisionMediaGuard",
            "--allowed-media-domains",
            "deepseek-v4-vision.invalid",
        ]
        head_env = {
            "VLLM_MEDIA_URL_ALLOW_REDIRECTS": "0",
            "VISION_ROLE": "head",
            "VLLM_HOST_IP": "192.0.2.33",
        }
        LAUNCHER.validate_args(required, head_env)

        rejected = []
        for option, bad_value in (
            ("--middleware", "other.Middleware"),
            ("--allowed-media-domains", "example.com"),
            ("--distributed-executor-backend", "external_launcher"),
            ("--nnodes", "1"),
            ("--tensor-parallel-size", "1"),
            ("--gpu-memory-utilization", "0.92"),
        ):
            candidate = list(required)
            candidate[candidate.index(option) + 1] = bad_value
            rejected.append(candidate)
        rejected.append(required + ["--allowed-local-media-path", "/tmp"])
        for argv in rejected:
            with self.subTest(argv=argv), self.assertRaises(LAUNCHER.LaunchContractError):
                LAUNCHER.validate_args(argv, head_env)
        with self.assertRaisesRegex(LAUNCHER.LaunchContractError, "REDIRECTS"):
            LAUNCHER.validate_args(required, {**head_env, "VLLM_MEDIA_URL_ALLOW_REDIRECTS": "1"})

        worker = list(required)
        worker[worker.index("--node-rank") + 1] = "1"
        worker.append("--headless")
        LAUNCHER.validate_args(
            worker,
            {
                "VLLM_MEDIA_URL_ALLOW_REDIRECTS": "0",
                "VISION_ROLE": "worker",
                "VLLM_HOST_IP": "192.0.2.34",
            },
        )
        for argv in (required + ["--headless"], worker[:-1]):
            with self.subTest(argv=argv), self.assertRaisesRegex(
                LAUNCHER.LaunchContractError, "headless"
            ):
                role = "head" if argv[-1] == "--headless" else "worker"
                LAUNCHER.validate_args(
                    argv,
                    {
                        "VLLM_MEDIA_URL_ALLOW_REDIRECTS": "0",
                        "VISION_ROLE": role,
                        "VLLM_HOST_IP": LAUNCHER.REQUIRED_HOST_IP[role],
                    },
                )

    def test_flashinfer_patch_is_exact_upstream_commit_patch(self):
        patch_path = CONTEXT / "flashinfer-7a55473b9e3f81a1ce7c862c3b993f6ef23cab5f.patch"
        self.assertEqual(hashlib.sha256(patch_path.read_bytes()).hexdigest(), PATCHER.FLASHINFER_PATCH_SHA256)
        self.assertEqual(patch_path.read_text().count("diff --git "), 2)

    def test_vllm_patch_rejects_unknown_preimage(self):
        with self.assertRaisesRegex(RuntimeError, "preimage hash mismatch"):
            PATCHER.patch_vllm_bytes(b"not the pinned vLLM source")

    def test_context_static_verifier(self):
        result = subprocess.run(
            ["python3", str(ROOT / "scripts/verify-deepseek-v4-flash-vision-runtime.py")],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(json.loads(result.stdout)["status"], "PASS")

    def test_build_is_offline_and_base_is_content_addressed(self):
        containerfile = (CONTEXT / "Containerfile").read_text()
        build = (ROOT / "scripts/build-deepseek-v4-flash-vision-runtime.sh").read_text()
        self.assertIn("sha256:8568b4bbc821903d93a0a9c17dd80382fdc0ba78eaa128e3eb5cb71c3bf06b79", containerfile)
        self.assertIn("FROM vllm/vllm-openai:vision-base-8568b4bbc821", containerfile)
        self.assertIn('base_local_ref="vllm/vllm-openai:vision-base-8568b4bbc821"', build)
        self.assertIn('base_id" != "$base_config', build)
        self.assertIn("--pull=false", build)
        self.assertIn("--network=none", build)
        self.assertNotIn("docker pull", build)
        self.assertIn("launch_vllm.py", containerfile)


if __name__ == "__main__":
    unittest.main()
