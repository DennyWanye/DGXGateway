import http.client
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GATEWAY_PATH = ROOT / "scripts/minimax-h3-two-node/gateway.py"
POLICY_PATH = ROOT / "manifests/minimax-h3-request-policy-v1.json"
POLICY_V2_PATH = ROOT / "manifests/minimax-h3-request-policy-v2.json"


def load_gateway():
    spec = importlib.util.spec_from_file_location("minimax_h3_gateway", GATEWAY_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class UpstreamHandler(BaseHTTPRequestHandler):
    health_status = 200
    count = 0
    last_body = b""
    last_content_type = ""

    def do_GET(self):
        type(self).count += 1
        body = json.dumps({"ok": True, "path": self.path}).encode()
        self.send_response(type(self).health_status if self.path == "/health" else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        type(self).count += 1
        length = int(self.headers["Content-Length"])
        body = self.rfile.read(length)
        type(self).last_body = body
        type(self).last_content_type = self.headers.get("Content-Type", "")
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gateway = load_gateway()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        self.token_path = tmp / "gateway.token"
        self.token_path.write_text("test-secret-token\n")
        os.chmod(self.token_path, 0o600)
        self.readiness_path = tmp / "readiness.json"
        self.readiness_path.write_text(json.dumps({
            "state": "READY",
            "exact_two": True,
            "ranks_ready": True,
            "runtime_ready": True,
            "worker_reachable": True,
        }))

        UpstreamHandler.count = 0
        UpstreamHandler.last_body = b""
        UpstreamHandler.last_content_type = ""
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()
        self.addCleanup(self.upstream.shutdown)
        self.addCleanup(self.upstream.server_close)

        config = self.gateway.GatewayConfig(
            bind_host="127.0.0.1",
            bind_port=0,
            upstream_host="127.0.0.1",
            upstream_port=self.upstream.server_port,
            token_file=self.token_path,
            policy_file=POLICY_PATH,
            readiness_file=self.readiness_path,
            max_body_bytes=8192,
            upstream_timeout_seconds=3,
        )
        self.server = self.gateway.create_server(config)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        result = response.status, dict(response.headers), payload
        connection.close()
        return result

    @property
    def auth(self):
        return {"Authorization": "Bearer test-secret-token"}

    def multipart(self, fields):
        boundary = "h3-test-boundary"
        chunks = []
        for name, value in fields:
            chunks.extend([
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                str(value).encode(),
                b"\r\n",
            ])
        chunks.append(f"--{boundary}--\r\n".encode())
        body = b"".join(chunks)
        headers = dict(self.auth)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        headers["Content-Length"] = str(len(body))
        return body, headers

    def valid_fields(self):
        return [
            ("prompt", "ocean sunrise"),
            ("width", "768"),
            ("height", "448"),
            ("fps", "24"),
            ("num_inference_steps", "20"),
            ("flow_shift", "12"),
            ("seed", "123"),
            ("extra_params", json.dumps({"task": "t2va", "duration": 2.0, "audio_flow_shift": 3.0})),
        ]

    def test_production_defaults_are_fixed_to_required_addresses(self):
        config = self.gateway.GatewayConfig.production_defaults(
            token_file=self.token_path,
            policy_file=POLICY_PATH,
            readiness_file=self.readiness_path,
        )
        self.assertEqual((config.bind_host, config.bind_port), ("192.0.2.30", 18081))
        self.assertEqual((config.upstream_host, config.upstream_port), ("192.0.2.33", 8000))

    def test_systemd_credential_mode_is_accepted(self):
        os.chmod(self.token_path, 0o400)
        self.assertEqual(self.gateway._load_token(self.token_path), b"test-secret-token")

    def test_dynamic_user_systemd_credential_mode_is_accepted(self):
        os.chmod(self.token_path, 0o440)
        self.assertEqual(self.gateway._load_token(self.token_path), b"test-secret-token")

    def test_missing_and_wrong_token_are_rejected_without_forwarding(self):
        self.assertEqual(self.request("GET", "/health")[0], 401)
        self.assertEqual(self.request("GET", "/health", headers={"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(UpstreamHandler.count, 0)

    def test_allowlisted_get_routes_forward_with_correct_token(self):
        self.assertEqual(self.request("GET", "/health", headers=self.auth)[0], 200)
        self.assertEqual(self.request("GET", "/v1/models", headers=self.auth)[0], 200)
        self.assertEqual(UpstreamHandler.count, 2)

    def test_stale_readiness_cannot_hide_failed_runtime(self):
        UpstreamHandler.health_status = 503
        try:
            code, _, _ = self.request("GET", "/health", headers=self.auth)
            self.assertEqual(code, 503)
        finally:
            UpstreamHandler.health_status = 200

    def test_cancel_is_authenticated_and_rejects_unsafe_request_id(self):
        self.assertEqual(self.request("POST", "/v1/videos/cancel", body=b"")[0], 401)
        headers = {**self.auth, "X-H3-Request-ID": "../unrelated"}
        self.assertEqual(self.request("POST", "/v1/videos/cancel", body=b"", headers=headers)[0], 400)
        self.assertEqual(UpstreamHandler.count, 0)

    def test_health_fails_closed_unless_exact_two_and_both_ranks_ready(self):
        status = json.loads(self.readiness_path.read_text())
        status["exact_two"] = False
        self.readiness_path.write_text(json.dumps(status))
        code, _, payload = self.request("GET", "/health", headers=self.auth)
        self.assertEqual(code, 503)
        self.assertIn(b"cluster_not_ready", payload)
        self.assertEqual(UpstreamHandler.count, 0)

    def test_non_allowlisted_and_normalized_paths_are_rejected(self):
        paths = [
            "/",
            "/metrics",
            "/v1/videos/sync/",
            "/v1//videos/sync",
            "/v1/videos/../models",
            "/v1/videos/%73ync",
            "/v1/videos/sync?x=1",
            "//v1/videos/sync",
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertIn(self.request("GET", path, headers=self.auth)[0], (404, 405))
        self.assertEqual(UpstreamHandler.count, 0)

    def test_wrong_method_is_rejected(self):
        self.assertEqual(self.request("POST", "/health", body=b"", headers=self.auth)[0], 405)
        self.assertEqual(self.request("GET", "/v1/videos/sync", headers=self.auth)[0], 405)
        self.assertEqual(self.request("HEAD", "/health")[0], 401)
        self.assertEqual(self.request("HEAD", "/health", headers=self.auth)[0], 405)

    def test_valid_video_request_forwards_once_and_increments_counter(self):
        body, headers = self.multipart(self.valid_fields())
        code, response_headers, payload = self.request("POST", "/v1/videos/sync", body, headers)
        self.assertEqual(code, 200)
        self.assertEqual(response_headers["X-H3-Forwarded-Count"], "1")
        self.assertEqual(payload, body)
        self.assertEqual(UpstreamHandler.count, 1)

    def test_invalid_width_and_duration_report_both_without_forwarding(self):
        fields = self.valid_fields()
        fields[1] = ("width", "770")
        fields[-1] = ("extra_params", json.dumps({"task": "t2va", "duration": 5.0, "audio_flow_shift": 3.0}))
        body, headers = self.multipart(fields)
        code, _, payload = self.request("POST", "/v1/videos/sync", body, headers)
        error = json.loads(payload)
        self.assertEqual(code, 422)
        self.assertEqual(set(error["invalid_fields"]), {"width", "extra_params.duration"})
        self.assertEqual(UpstreamHandler.count, 0)

    def test_duplicate_unknown_and_forbidden_fields_are_rejected(self):
        cases = [
            self.valid_fields() + [("prompt", "duplicate")],
            self.valid_fields() + [("unknown", "value")],
            self.valid_fields() + [("seconds", "2")],
            self.valid_fields()[:-1] + [("extra_params", '{"task":"t2va","task":"t2va","duration":2.0,"audio_flow_shift":3.0}')],
        ]
        for fields in cases:
            with self.subTest(fields=fields[-1][0]):
                body, headers = self.multipart(fields)
                self.assertEqual(self.request("POST", "/v1/videos/sync", body, headers)[0], 422)
        self.assertEqual(UpstreamHandler.count, 0)

    def test_oversized_and_chunked_bodies_are_rejected(self):
        headers = dict(self.auth)
        headers["Content-Type"] = "multipart/form-data; boundary=x"
        headers["Content-Length"] = "9000"
        self.assertEqual(self.request("POST", "/v1/videos/sync", b"x", headers)[0], 413)

        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.putrequest("POST", "/v1/videos/sync")
        connection.putheader("Authorization", "Bearer test-secret-token")
        connection.putheader("Content-Type", "multipart/form-data; boundary=x")
        connection.putheader("Transfer-Encoding", "chunked")
        connection.endheaders()
        connection.send(b"1\r\nx\r\n0\r\n\r\n")
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 400)
        connection.close()
        self.assertEqual(UpstreamHandler.count, 0)


class Ref2VAGatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gateway = load_gateway()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.token_path = root / "gateway.token"
        self.token_path.write_text("test-secret-token\n")
        os.chmod(self.token_path, 0o600)
        self.readiness_path = root / "readiness.json"
        self.readiness_path.write_text(json.dumps({
            "state": "READY",
            "exact_two": True,
            "ranks_ready": True,
            "runtime_ready": True,
            "worker_reachable": True,
            "profile": "heretic-ref2va-v1",
            "task_type": "ref2va",
        }))
        self.spool = root / "spool"

        UpstreamHandler.count = 0
        UpstreamHandler.last_body = b""
        UpstreamHandler.last_content_type = ""
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()
        self.addCleanup(self.upstream.shutdown)
        self.addCleanup(self.upstream.server_close)

        config = self.gateway.GatewayConfig(
            bind_host="127.0.0.1",
            bind_port=0,
            upstream_host="127.0.0.1",
            upstream_port=self.upstream.server_port,
            token_file=self.token_path,
            policy_file=POLICY_V2_PATH,
            readiness_file=self.readiness_path,
            max_body_bytes=40_000_000,
            upstream_timeout_seconds=3,
            spool_dir=self.spool,
        )
        self.server = self.gateway.create_server(config)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    @property
    def auth(self):
        return {"Authorization": "Bearer test-secret-token"}

    def request(self, body, headers):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request("POST", "/v1/videos/sync", body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        result = response.status, payload
        connection.close()
        return result

    def test_durable_result_range_and_same_request_do_not_recompute(self):
        rid = 'a' * 32
        body, headers = self.multipart(self.fields(), self.valid_files())
        headers['X-H3-Request-ID'] = rid
        status, data = self.request(body, headers)
        self.assertEqual(status, 200)
        count = UpstreamHandler.count
        self.assertEqual(self.request(body, headers)[0], 200)
        self.assertEqual(UpstreamHandler.count, count)
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        connection.request('GET', '/v1/videos/result', headers={**self.auth, 'X-H3-Request-ID': rid, 'Range': 'bytes=5-15'})
        response = connection.getresponse()
        self.assertEqual(response.status, 206)
        self.assertEqual(response.read(), data[5:16])
        self.assertEqual(response.getheader('Content-Range'), f'bytes 5-15/{len(data)}')
        connection.close()
        restored = self.gateway.ResultStore(self.spool)
        self.assertEqual(restored.get(rid)['state'], 'SUCCEEDED')
        self.assertEqual(restored.path(rid, '.body').read_bytes(), data)

    def test_disconnect_retains_result_and_restart_never_replays_unknown(self):
        rid = 'b' * 32
        body, headers = self.multipart(self.fields(), self.valid_files())
        headers['X-H3-Request-ID'] = rid
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        connection.request('POST', '/v1/videos/sync', body=body, headers=headers)
        connection.close()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                if self.server.results.get(rid)['state'] == 'SUCCEEDED': break
            except (OSError, RuntimeError): pass
            time.sleep(.01)
        self.assertEqual(self.server.results.get(rid)['state'], 'SUCCEEDED')
        self.assertEqual(UpstreamHandler.count, 1)
        unknown = 'c' * 32
        self.server.results.begin(unknown, 'digest', {})
        restored = self.gateway.ResultStore(self.spool)
        self.assertEqual(restored.get(unknown)['state'], 'UNKNOWN')
        self.assertFalse(restored.begin(unknown, 'digest', {}))
        with self.assertRaises(ValueError): restored.begin(unknown, 'changed', {})

    def test_completed_slow_download_does_not_block_the_next_generation(self):
        from unittest.mock import patch
        blocked, release = threading.Event(), threading.Event()
        original = self.gateway.GatewayHandler._result
        first_id = 'd' * 32
        def slow_result(handler):
            if handler.command == 'POST' and handler.headers.get('X-H3-Request-ID') == first_id:
                blocked.set()
                release.wait(3)
            return original(handler)
        body, headers = self.multipart(self.fields(), self.valid_files())
        first_result = []
        with patch.object(self.gateway.GatewayHandler, '_result', slow_result):
            first = threading.Thread(target=lambda: first_result.append(self.request(body, {**headers, 'X-H3-Request-ID': first_id})))
            first.start()
            try:
                self.assertTrue(blocked.wait(2))
                # Completion is already durably queryable. The old client may
                # still be stalled; it no longer owns inference resources.
                self.assertEqual(self.server.results.get(first_id)['state'], 'SUCCEEDED')
                self.assertEqual(self.request(body, {**headers, 'X-H3-Request-ID': 'e' * 32})[0], 200)
                self.assertEqual(UpstreamHandler.count, 2)
            finally:
                release.set(); first.join(3)
        self.assertEqual(first_result[0][0], 200)

    def test_hmnsfw_duration_15_admitted_and_16_rejected(self):
        readiness=json.loads(self.readiness_path.read_text())
        readiness.update(profile="heretic-ref2va-hmnsfw-v1",task_type="ref2va")
        self.readiness_path.write_text(json.dumps(readiness))
        for duration, expected in [(4,200),(8,200),(15,200),(16,422),(3,422)]:
            body,headers=self.multipart(self.fields(duration=duration,required_profile="heretic-ref2va-hmnsfw-v1"),self.valid_files())
            self.assertEqual(expected,self.request(body,headers)[0])

    def fields(self, *, task="ref2va", width="1344", height="768", duration=4.0, required_profile="heretic-ref2va-v1", steps=None):
        if steps is None:
            steps = "8" if required_profile == "10eros-fl2va-v1" else "20"
        return [
            ("prompt", "same woman in a yellow raincoat, close-up"),
            ("width", width),
            ("height", height),
            ("fps", "24"),
            ("num_inference_steps", steps),
            ("flow_shift", "12"),
            ("seed", "123"),
            ("extra_params", json.dumps({"task": task, "duration": duration, "audio_flow_shift": 3.0, "required_profile": required_profile})),
        ]

    def multipart(self, fields, files):
        boundary = "h3-ref2va-test-boundary"
        chunks = []
        for name, value in fields:
            chunks += [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                str(value).encode(), b"\r\n",
            ]
        for name, filename, mime, payload in files:
            chunks += [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode(),
                f"Content-Type: {mime}\r\n\r\n".encode(), payload, b"\r\n",
            ]
        chunks.append(f"--{boundary}--\r\n".encode())
        body = b"".join(chunks)
        headers = dict(self.auth)
        headers.update({
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        })
        return body, headers

    def valid_files(self):
        image = b"\x89PNG\r\n\x1a\n" + b"safe-image" + b"IEND"
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24_000)
            output.writeframes(b"\0\0" * 96_000)
        audio = buffer.getvalue()
        return [
            ("reference_image", "reference.png", "image/png", image),
            ("reference_audio", "reference.wav", "audio/wav", audio),
        ]

    def test_valid_reference_is_spooled_rewritten_and_forwarded_once(self):
        body, headers = self.multipart(self.fields(), self.valid_files())
        code, payload = self.request(body, headers)
        self.assertEqual(200, code, payload)
        self.assertEqual(1, UpstreamHandler.count)
        self.assertIn(b'name="input_reference"', UpstreamHandler.last_body)
        self.assertNotIn(b'name="reference_image"', UpstreamHandler.last_body)
        self.assertIn(self.valid_files()[0][3], UpstreamHandler.last_body)
        self.assertIn(b'name="audio_reference"', UpstreamHandler.last_body)
        self.assertIn(b'data:audio/wav;base64,', UpstreamHandler.last_body)
        self.assertNotIn(b'name="reference_audio"', UpstreamHandler.last_body)
        self.assertTrue(self.spool.is_dir())
        deadline = time.monotonic() + 1
        while [p for p in self.spool.iterdir() if p.name != "results"] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual([], [p for p in self.spool.iterdir() if p.name != "results"])

    def test_profile_task_and_exact_ref2va_geometry_are_attested(self):
        image = b"\xff\xd8\xff" + b"jpeg" + b"\xff\xd9"
        cases = [
            self.multipart(self.fields(task="t2va"), self.valid_files()),
            self.multipart(self.fields(width="768", height="448"), self.valid_files()),
        ]
        readiness = json.loads(self.readiness_path.read_text())
        readiness.update({"profile": "heretic-hmnsfw-v1", "task_type": "fl2va"})
        self.readiness_path.write_text(json.dumps(readiness))
        cases.append(self.multipart(self.fields(), self.valid_files()))
        for body, headers in cases:
            with self.subTest(length=len(body)):
                self.assertEqual(422, self.request(body, headers)[0])
        self.assertEqual(0, UpstreamHandler.count)

    def test_reference_file_contract_rejects_duplicates_traversal_magic_and_empty(self):
        png = b"\x89PNG\r\n\x1a\nbody"
        valid_audio = self.valid_files()[1]
        cases = [
            [("reference_image", "a.png", "image/png", png), ("reference_image", "b.png", "image/png", png), valid_audio],
            [("reference_image", "../a.png", "image/png", png), valid_audio],
            [("reference_image", "a.png", "image/jpeg", png), valid_audio],
            [("reference_image", "a.png", "image/png", b""), valid_audio],
            [("reference_image", "nul\0.png", "image/png", png), valid_audio],
            [self.valid_files()[0], ("reference_audio", "a.wav", "audio/wav", b"RIFF")],
            [self.valid_files()[0], ("reference_audio", "../a.wav", "audio/wav", valid_audio[3])],
            [self.valid_files()[0], ("reference_audio", "a.wav", "audio/mpeg", valid_audio[3])],
            [self.valid_files()[0]],
            [valid_audio],
            [],
        ]
        for files in cases:
            with self.subTest(files=files[0][0:3] if files else "missing"):
                body, headers = self.multipart(self.fields(), files)
                self.assertEqual(422, self.request(body, headers)[0])
        self.assertEqual(0, UpstreamHandler.count)

    def test_v2_rejects_duplicate_unknown_fields_and_file_limit_before_forwarding(self):
        png = b"\x89PNG\r\n\x1a\nbody"
        field_cases = [
            self.fields() + [("prompt", "duplicate")],
            self.fields() + [("unknown", "value")],
        ]
        for fields in field_cases:
            body, headers = self.multipart(fields, self.valid_files())
            self.assertEqual(422, self.request(body, headers)[0])
        self.server.request_policy["reference_image"]["max_bytes"] = 8
        body, headers = self.multipart(self.fields(), self.valid_files())
        self.assertEqual(422, self.request(body, headers)[0])
        self.assertEqual(0, UpstreamHandler.count)

    def test_v2_keeps_text_only_fl2va_available_but_rejects_reference(self):
        readiness = json.loads(self.readiness_path.read_text())
        readiness.update({"profile": "heretic-hmnsfw-v1", "task_type": "fl2va"})
        self.readiness_path.write_text(json.dumps(readiness))
        body, headers = self.multipart(self.fields(task="t2va", width="768", height="448", duration=2.0, required_profile="heretic-hmnsfw-v1"), [])
        self.assertEqual(200, self.request(body, headers)[0])
        self.assertEqual(1, UpstreamHandler.count)

    def test_10eros_accepts_t2va_or_one_first_frame_but_never_ref2va_audio(self):
        readiness = json.loads(self.readiness_path.read_text())
        readiness.update({"profile": "10eros-fl2va-v1", "task_type": "fl2va"})
        self.readiness_path.write_text(json.dumps(readiness))
        t2va = self.multipart(
            self.fields(task="t2va", width="768", height="448", duration=2.0, required_profile="10eros-fl2va-v1"),
            [],
        )
        self.assertEqual(200, self.request(*t2va)[0])
        fl2va = self.multipart(
            self.fields(task="fl2va", width="768", height="448", duration=4.0, required_profile="10eros-fl2va-v1"),
            [self.valid_files()[0]],
        )
        self.assertEqual(200, self.request(*fl2va)[0])
        ref2va = self.multipart(
            self.fields(required_profile="10eros-fl2va-v1"),
            self.valid_files(),
        )
        self.assertEqual(422, self.request(*ref2va)[0])

        wrong_steps = self.multipart(
            self.fields(task="t2va", width="768", height="448", duration=2.0, required_profile="10eros-fl2va-v1", steps="20"),
            [],
        )
        self.assertEqual(422, self.request(*wrong_steps)[0])

    def test_required_profile_mismatch_is_rejected_before_forwarding(self):
        body, headers = self.multipart(
            self.fields(required_profile="heretic-ref2va-hmnsfw-v1"), self.valid_files()
        )
        self.assertEqual(422, self.request(body, headers)[0])
        self.assertEqual(0, UpstreamHandler.count)


if __name__ == "__main__":
    unittest.main()
