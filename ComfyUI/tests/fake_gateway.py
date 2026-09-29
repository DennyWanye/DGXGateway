#!/usr/bin/env python3
"""Internal-only deterministic gateway for the ComfyUI test image."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
from email import policy as email_policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock


FIELD_ORDER = (
    "prompt",
    "width",
    "height",
    "fps",
    "num_inference_steps",
    "flow_shift",
    "seed",
    "extra_params",
)
MAX_REQUEST_BYTES = 16 * 1024
NAME_PATTERN = re.compile(r'^form-data;\s*name="([a-z_]+)"$')


class RequestError(ValueError):
    pass


class Response:
    def __init__(self, status: int, content_type: str, body: bytes, declared_length: int | None = None, identity_profile: str | None = None):
        self.status = status
        self.content_type = content_type
        self.body = body
        self.declared_length = len(body) if declared_length is None else declared_length
        self.identity_profile = identity_profile


def identity_header(profile: str) -> str:
    payload = {
        "schema_version": 1, "name": profile, "task_family": "ref2va" if "ref2va" in profile else "fl2va",
        "supported_tasks": ["ref2va"] if "ref2va" in profile else ["t2va"],
        "text_encoder": {"kind": "test"}, "transformer": {"kind": "test"},
        "modifier": {"kind": "none"}, "curve": {"kind": "none"},
        "model_tree_sha256": "a" * 64, "runtime_image_id": "sha256:" + "b" * 64,
        "runtime_overlay_sha256": "c" * 64, "rank_identities": [0, 1],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["contract_sha256"] = hashlib.sha256(canonical).hexdigest()
    return base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")


def _boundary(content_type: str) -> bytes:
    prefix = "multipart/form-data; boundary="
    if not content_type.startswith(prefix):
        raise RequestError("invalid multipart content type")
    value = content_type[len(prefix) :]
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    if not value or len(value) > 128 or any(char in value for char in "\r\n"):
        raise RequestError("invalid multipart boundary")
    try:
        return value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise RequestError("invalid multipart boundary") from exc


def parse_multipart(stream: io.BufferedIOBase, *, content_type: str, content_length: int) -> dict[str, str]:
    if not 0 < content_length <= MAX_REQUEST_BYTES:
        raise RequestError("invalid request size")
    raw = stream.read(content_length)
    if len(raw) != content_length:
        raise RequestError("truncated request")
    marker = b"--" + _boundary(content_type)
    segments = raw.split(marker)
    if len(segments) < 3 or segments[0] or segments[-1] != b"--\r\n":
        raise RequestError("invalid multipart framing")

    fields: dict[str, str] = {}
    for segment in segments[1:-1]:
        if not segment.startswith(b"\r\n") or not segment.endswith(b"\r\n"):
            raise RequestError("invalid multipart part")
        try:
            header_block, value = segment[2:-2].split(b"\r\n\r\n", 1)
            header_lines = header_block.decode("ascii").split("\r\n")
        except (ValueError, UnicodeDecodeError) as exc:
            raise RequestError("invalid multipart headers") from exc
        disposition = None
        for header in header_lines:
            name, separator, header_value = header.partition(":")
            if not separator:
                raise RequestError("invalid multipart header")
            if name.lower() == "content-disposition":
                if disposition is not None:
                    raise RequestError("duplicate content disposition")
                disposition = header_value.strip()
        match = NAME_PATTERN.fullmatch(disposition or "")
        if match is None:
            raise RequestError("invalid field name")
        field_name = match.group(1)
        if field_name not in FIELD_ORDER or field_name in fields:
            raise RequestError("unknown or duplicate field")
        try:
            fields[field_name] = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RequestError("invalid field encoding") from exc
    return fields


def parse_ref2va_multipart(
    stream: io.BufferedIOBase,
    *,
    content_type: str,
    content_length: int,
) -> tuple[dict[str, str], bytes, str, bytes, str]:
    if not 0 < content_length <= 32 * 1024 * 1024:
        raise RequestError("invalid request size")
    raw = stream.read(content_length)
    if len(raw) != content_length:
        raise RequestError("truncated request")
    try:
        message = BytesParser(policy=email_policy.default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii") + raw
        )
    except (UnicodeError, ValueError) as exc:
        raise RequestError("invalid multipart request") from exc
    if not message.is_multipart() or message.get_content_type() != "multipart/form-data":
        raise RequestError("invalid multipart request")
    fields: dict[str, str] = {}
    reference: bytes | None = None
    reference_mime: str | None = None
    reference_audio: bytes | None = None
    reference_audio_mime: str | None = None
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True)
        if not isinstance(name, str) or not isinstance(payload, bytes):
            raise RequestError("invalid multipart part")
        if filename is None:
            if name not in FIELD_ORDER or name in fields:
                raise RequestError("unknown or duplicate field")
            try:
                fields[name] = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RequestError("invalid field encoding") from exc
        else:
            if name == "reference_image" and reference is None and filename == "reference.png":
                reference = payload
                reference_mime = part.get_content_type()
            elif name == "reference_audio" and reference_audio is None and filename == "reference.wav":
                reference_audio = payload
                reference_audio_mime = part.get_content_type()
            else:
                raise RequestError("invalid reference file")
    if (
        tuple(fields) != FIELD_ORDER
        or reference is None
        or reference_mime is None
        or reference_audio is None
        or reference_audio_mime is None
    ):
        raise RequestError("incomplete Ref2VA request")
    return fields, reference, reference_mime, reference_audio, reference_audio_mime


def _valid_policy(fields: dict[str, str]) -> bool:
    if tuple(fields) != FIELD_ORDER:
        return False
    if not fields["prompt"].strip() or len(fields["prompt"].encode("utf-8")) > 2048:
        return False
    if (fields["width"], fields["height"]) not in (("768", "448"), ("1344", "768")):
        return False
    if fields["fps"] != "24" or fields["num_inference_steps"] != "20" or fields["flow_shift"] != "12.0":
        return False
    try:
        seed = int(fields["seed"])
        extra = json.loads(fields["extra_params"])
    except (ValueError, json.JSONDecodeError):
        return False
    return (
        0 <= seed <= 2_147_483_647
        and isinstance(extra, dict)
        and set(extra) in ({"task", "duration", "audio_flow_shift"}, {"task", "duration", "audio_flow_shift", "required_profile"})
        and extra["task"] == "t2va"
        and extra["duration"] in (2.0, 3.0, 4.0)
        and not isinstance(extra["duration"], bool)
        and extra["audio_flow_shift"] == 3.0
    )


def evaluate_request(
    *,
    mode: str,
    authorization: str,
    expected_token: str,
    fields: dict[str, str],
    fixture: bytes,
) -> Response:
    if authorization != f"Bearer {expected_token}" or mode == "auth":
        return Response(401, "application/json", b'{"error":"unauthorized"}')
    if not _valid_policy(fields):
        return Response(400, "application/json", b'{"error":"invalid request"}')
    extra = json.loads(fields["extra_params"])
    if mode == "success":
        return Response(200, "video/mp4", fixture, identity_profile=extra.get("required_profile", "heretic-hmnsfw-v1"))
    if mode == "busy":
        return Response(503, "application/json", b'{"error":"busy"}')
    if mode == "non-mp4":
        return Response(200, "application/octet-stream", b"not an mp4")
    if mode == "redirect":
        return Response(302, "text/plain", b"redirect disabled")
    if mode == "empty":
        return Response(200, "video/mp4", b"")
    if mode == "oversize":
        return Response(200, "video/mp4", b"x", declared_length=268_435_457)
    if mode == "truncated":
        return Response(200, "video/mp4", b"short", declared_length=1024)
    return Response(500, "application/json", b'{"error":"unknown test mode"}')


def evaluate_ref2va_request(
    *,
    mode: str,
    authorization: str,
    expected_token: str,
    fields: dict[str, str],
    reference_image: bytes,
    reference_mime: str,
    reference_audio: bytes,
    reference_audio_mime: str,
    fixture: bytes,
) -> Response:
    if authorization != f"Bearer {expected_token}" or mode == "auth":
        return Response(401, "application/json", b'{"error":"unauthorized"}')
    try:
        extra = json.loads(fields["extra_params"])
        valid = (
            tuple(fields) == FIELD_ORDER
            and fields["prompt"].strip()
            and (fields["width"], fields["height"]) == ("1344", "768")
            and fields["fps"] == "24"
            and fields["num_inference_steps"] == "20"
            and fields["flow_shift"] == "12.0"
            and 0 <= int(fields["seed"]) <= 2_147_483_647
            and extra.get("task") == "ref2va"
            and extra.get("duration") == 4.0
            and extra.get("audio_flow_shift") == 3.0
            and set(extra) in ({"task", "duration", "audio_flow_shift"}, {"task", "duration", "audio_flow_shift", "required_profile"})
            and reference_mime == "image/png"
            and reference_image.startswith(b"\x89PNG\r\n\x1a\n")
            and reference_audio_mime == "audio/wav"
            and reference_audio.startswith(b"RIFF")
        )
    except (KeyError, ValueError, json.JSONDecodeError):
        valid = False
    if not valid:
        return Response(400, "application/json", b'{"error":"invalid request"}')
    if mode == "success":
        return Response(200, "video/mp4", fixture, identity_profile=extra.get("required_profile", "heretic-ref2va-v1"))
    if mode == "busy":
        return Response(503, "application/json", b'{"error":"busy"}')
    return Response(500, "application/json", b'{"error":"unknown test mode"}')


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", default="0.0.0.0:19081")
    parser.add_argument("--mode", default=os.environ.get("FAKE_GATEWAY_MODE", "success"))
    parser.add_argument("--token-file", type=Path, default=Path("/tests/container/fixtures/test-canary.txt"))
    parser.add_argument("--fixture", type=Path, default=Path("/tests/integration/fixtures/valid.mp4"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = args.token_file.read_text(encoding="utf-8").removesuffix("\n")
    if not token or "\n" in token or "\r" in token:
        raise SystemExit("invalid test token")
    fixture = args.fixture.read_bytes() if args.mode == "success" else b""
    host, port_text = args.listen.rsplit(":", 1)
    count_lock = Lock()
    state = {"request_count": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/__test__/count":
                self.send_error(404)
                return
            with count_lock:
                body = json.dumps(state, sort_keys=True).encode("ascii")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            with count_lock:
                state["request_count"] += 1
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                fields = parse_multipart(
                    self.rfile,
                    content_type=self.headers.get("Content-Type", ""),
                    content_length=length,
                )
                response = evaluate_request(
                    mode=args.mode,
                    authorization=self.headers.get("Authorization", ""),
                    expected_token=token,
                    fields=fields,
                    fixture=fixture,
                )
            except (RequestError, ValueError):
                response = Response(400, "application/json", b'{"error":"invalid request"}')
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(response.declared_length))
            if response.identity_profile is not None:
                self.send_header("X-H3-Identity", identity_header(response.identity_profile))
            self.end_headers()
            self.wfile.write(response.body)

        def log_message(self, format, *args):
            del format, args

    server = ThreadingHTTPServer((host, int(port_text)), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
