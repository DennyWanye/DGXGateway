from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "tests" / "fake_gateway.py"


def load_module():
    spec = importlib.util.spec_from_file_location("dgx_fake_gateway", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("fake gateway module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeGatewayTests(unittest.TestCase):
    def test_parser_reads_exact_declared_length_without_waiting_for_eof(self):
        gateway = load_module()
        boundary = "fixed-boundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="prompt"\r\n\r\n'
            "ocean\r\n"
            f"--{boundary}--\r\n"
        ).encode()

        class ExactLengthStream(io.BytesIO):
            def read(self, size=-1):
                self.asserted_size = size
                if size != len(body):
                    raise AssertionError("parser requested bytes beyond Content-Length")
                return super().read(size)

        stream = ExactLengthStream(body)
        fields = gateway.parse_multipart(
            stream,
            content_type=f"multipart/form-data; boundary={boundary}",
            content_length=len(body),
        )
        self.assertEqual(fields, {"prompt": "ocean"})
        self.assertEqual(stream.asserted_size, len(body))

    def test_success_requires_exact_policy_and_returns_fixture(self):
        gateway = load_module()
        fixture = b"valid-mp4-fixture"
        fields = {
            "prompt": "ocean",
            "width": "768",
            "height": "448",
            "fps": "24",
            "num_inference_steps": "20",
            "flow_shift": "12.0",
            "seed": "42",
            "extra_params": '{"task":"t2va","duration":2.0,"audio_flow_shift":3.0}',
        }

        response = gateway.evaluate_request(
            mode="success",
            authorization="Bearer test-canary",
            expected_token="test-canary",
            fields=fields,
            fixture=fixture,
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "video/mp4")
        self.assertEqual(response.body, fixture)

    def test_success_accepts_every_pinned_duration(self):
        gateway = load_module()
        for duration in (2.0, 3.0, 4.0):
            with self.subTest(duration=duration):
                fields = {
                    "prompt": "ocean",
                    "width": "768",
                    "height": "448",
                    "fps": "24",
                    "num_inference_steps": "20",
                    "flow_shift": "12.0",
                    "seed": "42",
                    "extra_params": json.dumps(
                        {"task": "t2va", "duration": duration, "audio_flow_shift": 3.0},
                        separators=(",", ":"),
                    ),
                }
                response = gateway.evaluate_request(
                    mode="success",
                    authorization="Bearer test-canary",
                    expected_token="test-canary",
                    fields=fields,
                    fixture=b"fixture",
                )
                self.assertEqual(response.status, 200)

    def test_wrong_auth_and_malformed_fields_fail_without_echoing_secrets(self):
        gateway = load_module()
        response = gateway.evaluate_request(
            mode="success",
            authorization="Bearer wrong-secret",
            expected_token="test-canary",
            fields={},
            fixture=b"unused",
        )
        self.assertEqual(response.status, 401)
        self.assertNotIn(b"wrong-secret", response.body)
        self.assertNotIn(b"test-canary", response.body)

        response = gateway.evaluate_request(
            mode="success",
            authorization="Bearer test-canary",
            expected_token="test-canary",
            fields={"prompt": "missing-policy-fields"},
            fixture=b"unused",
        )
        self.assertEqual(response.status, 400)

    def test_failure_modes_are_deterministic_and_sanitized(self):
        gateway = load_module()
        exact = {
            "prompt": "ocean",
            "width": "768",
            "height": "448",
            "fps": "24",
            "num_inference_steps": "20",
            "flow_shift": "12.0",
            "seed": "42",
            "extra_params": '{"task":"t2va","duration":2.0,"audio_flow_shift":3.0}',
        }
        expected = {
            "busy": (503, "application/json"),
            "auth": (401, "application/json"),
            "non-mp4": (200, "application/octet-stream"),
            "redirect": (302, "text/plain"),
            "empty": (200, "video/mp4"),
        }
        for mode, (status, content_type) in expected.items():
            with self.subTest(mode=mode):
                response = gateway.evaluate_request(
                    mode=mode,
                    authorization="Bearer test-canary",
                    expected_token="test-canary",
                    fields=exact,
                    fixture=b"fixture",
                )
                self.assertEqual((response.status, response.content_type), (status, content_type))
                self.assertNotIn(b"test-canary", response.body)

    def test_multipart_parser_rejects_duplicate_or_unknown_fields(self):
        gateway = load_module()
        boundary = "fixed-boundary"
        parts = []
        for name, value in (("prompt", "one"), ("prompt", "two"), ("unknown", "x")):
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
            )
        body = b"".join(parts) + f"--{boundary}--\r\n".encode()
        with self.assertRaises(gateway.RequestError):
            gateway.parse_multipart(
                io.BytesIO(body),
                content_type=f"multipart/form-data; boundary={boundary}",
                content_length=len(body),
            )

    def test_ref2va_fake_gateway_accepts_one_png_and_fixed_policy(self):
        gateway = load_module()
        boundary = "ref-boundary"
        fields = {
            "prompt": "same actor",
            "width": "1344",
            "height": "768",
            "fps": "24",
            "num_inference_steps": "20",
            "flow_shift": "12.0",
            "seed": "42",
            "extra_params": '{"task":"ref2va","duration":4.0,"audio_flow_shift":3.0}',
        }
        chunks = []
        for name, value in fields.items():
            chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        image = b"\x89PNG\r\n\x1a\nfixture"
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="reference_image"; filename="reference.png"\r\nContent-Type: image/png\r\n\r\n'.encode()
            + image + b"\r\n"
        )
        audio = b"RIFF" + b"\0" * 40
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="reference_audio"; filename="reference.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode()
            + audio + b"\r\n"
        )
        body = b"".join(chunks) + f"--{boundary}--\r\n".encode()
        parsed, parsed_image, mime, parsed_audio, audio_mime = gateway.parse_ref2va_multipart(
            io.BytesIO(body),
            content_type=f"multipart/form-data; boundary={boundary}",
            content_length=len(body),
        )
        self.assertEqual(parsed, fields)
        self.assertEqual((parsed_image, mime), (image, "image/png"))
        self.assertEqual((parsed_audio, audio_mime), (audio, "audio/wav"))
        response = gateway.evaluate_ref2va_request(
            mode="success",
            authorization="Bearer test-canary",
            expected_token="test-canary",
            fields=parsed,
            reference_image=parsed_image,
            reference_mime=mime,
            reference_audio=parsed_audio,
            reference_audio_mime=audio_mime,
            fixture=b"video",
        )
        self.assertEqual((response.status, response.body), (200, b"video"))


if __name__ == "__main__":
    unittest.main()
