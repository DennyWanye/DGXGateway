import http.server
import pathlib
import socketserver
import sys
import tempfile
import threading
import unittest
from unittest import mock


CUSTOM_NODES = pathlib.Path(__file__).resolve().parents[2] / "custom_nodes"
sys.path.insert(0, str(CUSTOM_NODES))

from dgx_remote_h3.client import GatewayError, H3GatewayClient
from dgx_remote_h3.contract import RuntimeContract
from dgx_remote_h3.policy import build_gateway_fields, validate_inputs


def identity_header(profile="heretic-hmnsfw-v1"):
    import base64, hashlib, json
    payload = {
        "schema_version": 1,
        "name": profile,
        "task_family": "fl2va",
        "supported_tasks": ["t2va"],
        "text_encoder": {"kind": "test"},
        "transformer": {"kind": "test"},
        "modifier": {"kind": "none"},
        "curve": {"kind": "none"},
        "model_tree_sha256": "a" * 64,
        "runtime_image_id": "sha256:" + "b" * 64,
        "runtime_overlay_sha256": "c" * 64,
        "rank_identities": [0, 1],
    }
    payload["contract_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")


class RecordingHandler(http.server.BaseHTTPRequestHandler):
    status = 200
    response_type = "video/mp4"
    response_body = b"fake-mp4-for-transport"
    request_count = 0
    request_path = None
    request_headers = None
    request_body = None

    def do_POST(self):
        type(self).request_count += 1
        type(self).request_path = self.path
        type(self).request_headers = self.headers
        length = int(self.headers["Content-Length"])
        type(self).request_body = self.rfile.read(length)
        self.send_response(type(self).status)
        self.send_header("Content-Type", type(self).response_type)
        self.send_header("Content-Length", str(len(type(self).response_body)))
        self.send_header("X-H3-Identity", identity_header())
        self.end_headers()
        self.wfile.write(type(self).response_body)

    def log_message(self, *_args):
        return


class ClientTests(unittest.TestCase):
    def setUp(self):
        RecordingHandler.status = 200
        RecordingHandler.response_type = "video/mp4"
        RecordingHandler.response_body = b"fake-mp4-for-transport"
        RecordingHandler.request_count = 0
        RecordingHandler.request_path = None
        RecordingHandler.request_headers = None
        RecordingHandler.request_body = None
        self.server = socketserver.TCPServer(("127.0.0.1", 0), RecordingHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _contract(self, *, max_response_bytes=1024):
        return RuntimeContract(
            schema_id="unit-test-only",
            profile="test",
            endpoint_id="unit-test-only",
            endpoint_host="127.0.0.1",
            endpoint_port=self.server.server_address[1],
            endpoint_path="/v1/videos/sync",
            secret_path=pathlib.Path("/unused"),
            connect_timeout_seconds=1,
            total_timeout_seconds=5,
            max_response_bytes=max_response_bytes,
        )

    def _fields(self):
        return build_gateway_fields(validate_inputs("ocean waves", "768x448", 2, 7))

    def test_posts_exact_multipart_once_and_streams_response(self):
        with tempfile.TemporaryDirectory() as directory:
            staging = pathlib.Path(directory) / "video.part"
            staging.write_bytes(b"")
            client = H3GatewayClient(self._contract())

            count = client.generate(self._fields(), "opaque-token", staging)

            self.assertEqual(count, len(RecordingHandler.response_body))
            self.assertEqual(staging.read_bytes(), RecordingHandler.response_body)
            self.assertEqual(RecordingHandler.request_count, 1)
            self.assertEqual(RecordingHandler.request_path, "/v1/videos/sync")
            self.assertEqual(RecordingHandler.request_headers["Authorization"], "Bearer opaque-token")
            content_type = RecordingHandler.request_headers["Content-Type"]
            self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
            body = RecordingHandler.request_body
            for name, value in self._fields().items():
                self.assertEqual(body.count(f'name="{name}"'.encode()), 1)
                self.assertIn(b"\r\n\r\n" + value.encode("utf-8") + b"\r\n", body)
            self.assertNotIn(b"Content-Type: text/plain; charset=utf-8", body)
            self.assertEqual(len(self._fields()), body.count(b"Content-Type: text/plain\r\n"))

    def test_unlimited_wait_uses_none_socket_timeout(self):
        connection = mock.Mock()
        H3GatewayClient._set_remaining_timeout(connection, None)
        connection.sock.settimeout.assert_called_once_with(None)

    def test_cancel_waits_for_server_boundary_without_resubmitting(self):
        from dataclasses import replace
        canceled = threading.Event()
        calls = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                calls.append((self.path, self.headers.get("X-H3-Request-ID")))
                if self.path.endswith("/cancel"):
                    canceled.set()
                    self.send_response(200)
                else:
                    if not canceled.wait(4):
                        self.send_error(500)
                        return
                    self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        class Interrupted(BaseException): pass
        def interrupt(): raise Interrupted("user canceled")
        try:
            contract = replace(self._contract(), endpoint_port=server.server_port, total_timeout_seconds=None)
            with tempfile.TemporaryDirectory() as directory:
                target = pathlib.Path(directory)/"video.part"
                target.touch()
                with self.assertRaises(Interrupted):
                    H3GatewayClient(contract, interrupt_check=interrupt).generate(self._fields(), "opaque", target)
            self.assertEqual([p for p, _ in calls], ["/v1/videos/sync", "/v1/videos/cancel"])
            self.assertEqual(calls[0][1], calls[1][1])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_does_not_follow_redirect_or_retry(self):
        RecordingHandler.status = 302
        RecordingHandler.response_type = "text/plain"
        RecordingHandler.response_body = b"redirect-body-must-not-leak"
        with tempfile.TemporaryDirectory() as directory:
            staging = pathlib.Path(directory) / "video.part"
            staging.write_bytes(b"")
            with self.assertRaisesRegex(GatewayError, "HTTP 302") as raised:
                H3GatewayClient(self._contract()).generate(self._fields(), "opaque-token", staging)
            self.assertEqual(RecordingHandler.request_count, 1)
            self.assertNotIn("redirect-body", str(raised.exception))
            self.assertNotIn("opaque-token", str(raised.exception))

    def test_maps_auth_busy_and_content_type_to_sanitized_errors(self):
        cases = [
            (401, "application/json", "authentication failed"),
            (403, "application/json", "authentication failed"),
            (503, "application/json", "busy or not ready"),
            (200, "application/octet-stream", "did not return an MP4"),
        ]
        for status, response_type, expected in cases:
            with self.subTest(status=status, response_type=response_type):
                RecordingHandler.status = status
                RecordingHandler.response_type = response_type
                RecordingHandler.response_body = b'{"secret":"upstream-body"}'
                RecordingHandler.request_count = 0
                with tempfile.TemporaryDirectory() as directory:
                    staging = pathlib.Path(directory) / "video.part"
                    staging.write_bytes(b"")
                    with self.assertRaisesRegex(GatewayError, expected) as raised:
                        H3GatewayClient(self._contract()).generate(self._fields(), "opaque-token", staging)
                    self.assertEqual(RecordingHandler.request_count, 1)
                    self.assertNotIn("upstream-body", str(raised.exception))
                    self.assertNotIn("opaque-token", str(raised.exception))

    def test_rejects_declared_and_streamed_oversize_response(self):
        RecordingHandler.response_body = b"x" * 65
        for limit in (1, 64):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as directory:
                staging = pathlib.Path(directory) / "video.part"
                staging.write_bytes(b"")
                with self.assertRaisesRegex(GatewayError, "too large"):
                    H3GatewayClient(self._contract(max_response_bytes=limit)).generate(
                        self._fields(), "opaque-token", staging
                    )

    def test_retries_local_short_writes_until_the_whole_chunk_is_persisted(self):
        RecordingHandler.response_body = b"0123456789abcdef"
        real_write = __import__("os").write

        def short_write(descriptor, payload):
            return real_write(descriptor, payload[:3])

        with tempfile.TemporaryDirectory() as directory:
            staging = pathlib.Path(directory) / "video.part"
            staging.write_bytes(b"")
            with mock.patch("dgx_remote_h3.client.os.write", side_effect=short_write):
                count = H3GatewayClient(self._contract()).generate(
                    self._fields(), "opaque-token", staging
                )

            self.assertEqual(count, len(RecordingHandler.response_body))
            self.assertEqual(staging.read_bytes(), RecordingHandler.response_body)

    def test_ref2va_posts_safe_image_and_audio_in_exact_field_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            staging = root / "video.part"
            staging.write_bytes(b"")
            reference = root / "private-source-name.png"
            image = b"\x89PNG\r\n\x1a\nexact-image-bytes"
            reference.write_bytes(image)
            reference_audio = root / "private-source-name.wav"
            audio = b"RIFF" + (40).to_bytes(4, "little") + b"WAVEfmt " + b"safe-audio" + b"\0" * 20
            reference_audio.write_bytes(audio)
            H3GatewayClient(self._contract()).generate(
                self._fields(),
                "opaque-token",
                staging,
                reference_path=reference,
                reference_mime="image/png",
                reference_audio_path=reference_audio,
                reference_audio_mime="audio/wav",
            )
            body = RecordingHandler.request_body
            self.assertEqual(1, body.count(b'name="reference_image"'))
            self.assertIn(b'filename="reference.png"', body)
            self.assertNotIn(b"private-source-name", body)
            self.assertIn(image, body)
            self.assertEqual(1, body.count(b'name="reference_audio"'))
            self.assertIn(b'filename="reference.wav"', body)
            self.assertIn(audio, body)
            positions = [body.index(f'name="{name}"'.encode()) for name in self._fields()]
            positions.append(body.index(b'name="reference_image"'))
            positions.append(body.index(b'name="reference_audio"'))
            self.assertEqual(positions, sorted(positions))


if __name__ == "__main__":
    unittest.main()
