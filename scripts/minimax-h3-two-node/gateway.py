#!/usr/bin/env python3
"""Authenticated, allowlisted standard-library gateway for MiniMax H3."""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hmac
import hashlib
import shutil
import http.client
import json
import os
import re
import secrets
import socket
import stat
import tempfile
import threading
import wave
from dataclasses import dataclass
from email import policy as email_policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable


ALLOWED_ROUTES = {
    "GET": {"/health", "/v1/models", "/v1/videos/result"},
    "POST": {"/v1/videos/sync", "/v1/videos/cancel"},
}
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


class PolicyError(ValueError):
    def __init__(self, invalid_fields: Iterable[str], message: str = "request policy rejected"):
        super().__init__(message)
        self.invalid_fields = sorted(set(invalid_fields))


@dataclass(frozen=True)
class GatewayConfig:
    bind_host: str
    bind_port: int
    upstream_host: str
    upstream_port: int
    token_file: Path
    policy_file: Path
    readiness_file: Path
    max_body_bytes: int
    upstream_timeout_seconds: int
    spool_dir: Path | None = None
    lifecycle_lock: Path | None = None

    @classmethod
    def production_defaults(cls, *, token_file: Path, policy_file: Path, readiness_file: Path) -> "GatewayConfig":
        return cls(
            bind_host="192.0.2.30",
            bind_port=18081,
            upstream_host="192.0.2.33",
            upstream_port=8000,
            token_file=Path(token_file),
            policy_file=Path(policy_file),
            readiness_file=Path(readiness_file),
            max_body_bytes=31_800_000,
            upstream_timeout_seconds=3600,
            spool_dir=Path("/var/lib/minimax-h3/ref2va-spool"),
            lifecycle_lock=Path("/run/minimax-h3-two-node/control.lock"),
        )


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read required JSON file: {path}") from exc
    if not isinstance(document, dict):
        raise RuntimeError(f"JSON root must be an object: {path}")
    return document


def _load_token(path: Path) -> bytes:
    if path.is_symlink():
        raise RuntimeError("gateway token must not be a symlink")
    try:
        info = path.stat()
        token = path.read_bytes()
    except OSError as exc:
        raise RuntimeError("gateway token is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) not in {0o400, 0o440, 0o600}:
        raise RuntimeError("gateway token must be a regular mode-0400, mode-0440, or mode-0600 file")
    token = token.rstrip(b"\r\n")
    if len(token) < 16 or b"\n" in token or b"\r" in token:
        raise RuntimeError("gateway token has an invalid format")
    return token


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PolicyError(["extra_params"], "duplicate extra_params key")
        result[key] = value
    return result


def _parse_multipart(content_type: str, body: bytes, policy: dict[str, Any]) -> dict[str, str]:
    envelope = (
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii", "strict")
        + body
    )
    message = BytesParser(policy=email_policy.default).parsebytes(envelope)
    if not message.is_multipart() or message.get_content_type() != "multipart/form-data":
        raise PolicyError(["body"], "body must be multipart/form-data")

    multipart_policy = policy["multipart"]
    allowed = set(multipart_policy["allowed_fields"])
    fields: dict[str, str] = {}
    invalid: list[str] = []
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            invalid.append("body")
            continue
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        if not isinstance(name, str) or not name or filename is not None:
            invalid.append(name or "body")
            continue
        if name not in allowed or name in fields:
            invalid.append(name)
            continue
        raw = part.get_payload(decode=True)
        if not isinstance(raw, bytes) or len(raw) > int(multipart_policy["max_field_bytes"]):
            invalid.append(name)
            continue
        try:
            value = raw.decode("utf-8", "strict")
        except UnicodeDecodeError:
            invalid.append(name)
            continue
        fields[name] = value

    missing = set(multipart_policy["required_fields"]) - set(fields)
    invalid.extend(missing)
    if invalid:
        raise PolicyError(invalid)
    return fields


@dataclass(frozen=True)
class SpooledMultipart:
    fields: dict[str, str]
    reference_path: Path | None
    reference_mime: str | None
    reference_audio_path: Path | None
    reference_audio_mime: str | None


def _multipart_boundary(content_type: str) -> bytes:
    try:
        message = BytesParser(policy=email_policy.default).parsebytes(
            f"Content-Type: {content_type}\r\n\r\n".encode("ascii", "strict")
        )
        boundary = message.get_boundary()
    except (UnicodeError, ValueError) as exc:
        raise PolicyError(["body"], "invalid multipart content type") from exc
    if message.get_content_type() != "multipart/form-data" or not isinstance(boundary, str):
        raise PolicyError(["body"], "body must be multipart/form-data")
    try:
        encoded = boundary.encode("ascii", "strict")
    except UnicodeEncodeError as exc:
        raise PolicyError(["body"], "multipart boundary must be ASCII") from exc
    if not (1 <= len(encoded) <= 70) or not re.fullmatch(rb"[0-9A-Za-z'()+_,./:=?-]+", encoded):
        raise PolicyError(["body"], "invalid multipart boundary")
    return encoded


def _find_stream_marker(stream: Any, marker: bytes) -> int:
    overlap = b""
    absolute = stream.tell()
    while True:
        chunk = stream.read(64 * 1024)
        if not chunk:
            return -1
        combined = overlap + chunk
        index = combined.find(marker)
        if index >= 0:
            return absolute - len(overlap) + index
        overlap = combined[-(len(marker) - 1):]
        absolute += len(chunk)


def _read_part_headers(stream: Any, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        line = stream.readline(max_bytes + 2)
        size += len(line)
        if not line or size > max_bytes or len(line) > max_bytes:
            raise PolicyError(["body"], "multipart headers are too large or truncated")
        if line == b"\r\n":
            return b"".join(chunks)
        if not line.endswith(b"\r\n"):
            raise PolicyError(["body"], "multipart header line is malformed")
        chunks.append(line)


def _part_metadata(raw_headers: bytes) -> tuple[str, str | None, str | None]:
    names: set[bytes] = set()
    for line in raw_headers.split(b"\r\n"):
        if not line:
            continue
        if line[:1] in b" \t" or b":" not in line:
            raise PolicyError(["body"], "folded or malformed multipart header")
        header_name = line.split(b":", 1)[0].strip().lower()
        if header_name in names:
            raise PolicyError(["body"], "duplicate multipart header")
        names.add(header_name)
    if not names <= {b"content-disposition", b"content-type"}:
        raise PolicyError(["body"], "unexpected multipart header")
    message = BytesParser(policy=email_policy.default).parsebytes(raw_headers + b"\r\n")
    if message.get_content_disposition() != "form-data":
        raise PolicyError(["body"], "multipart part is not form-data")
    name = message.get_param("name", header="content-disposition")
    filename = message.get_filename()
    mime = message.get("Content-Type")
    if not isinstance(name, str) or not name or any(char in name for char in "\0\r\n"):
        raise PolicyError(["body"], "multipart field name is invalid")
    return name, filename, mime


def _copy_range(source: Any, start: int, length: int, destination: Any) -> None:
    source.seek(start)
    remaining = length
    while remaining:
        chunk = source.read(min(64 * 1024, remaining))
        if not chunk:
            raise PolicyError(["body"], "multipart body is truncated")
        destination.write(chunk)
        remaining -= len(chunk)


def _validate_reference_magic(path: Path, mime: str) -> None:
    size = path.stat().st_size
    with path.open("rb") as stream:
        prefix = stream.read(8)
        if mime == "image/jpeg":
            if size < 5 or not prefix.startswith(b"\xff\xd8\xff"):
                raise PolicyError(["reference_image"], "reference MIME/magic mismatch")
            stream.seek(-2, os.SEEK_END)
            if stream.read(2) != b"\xff\xd9":
                raise PolicyError(["reference_image"], "reference JPEG is truncated")
        elif mime == "image/png":
            if prefix != b"\x89PNG\r\n\x1a\n":
                raise PolicyError(["reference_image"], "reference MIME/magic mismatch")
        else:
            raise PolicyError(["reference_image"], "reference MIME is not allowed")


def _validate_reference_audio(path: Path, mime: str) -> None:
    if mime != "audio/wav":
        raise PolicyError(["reference_audio"], "reference audio MIME is not allowed")
    try:
        with wave.open(str(path), "rb") as stream:
            if (
                stream.getnchannels() != 1
                or stream.getsampwidth() != 2
                or stream.getframerate() != 24_000
                or stream.getnframes() != 96_000
                or stream.getcomptype() != "NONE"
            ):
                raise PolicyError(["reference_audio"], "reference audio must be four-second mono PCM16 24 kHz WAV")
    except PolicyError:
        raise
    except (OSError, EOFError, wave.Error) as exc:
        raise PolicyError(["reference_audio"], "reference audio WAV is malformed") from exc


def _parse_spooled_multipart(
    content_type: str,
    body_path: Path,
    policy: dict[str, Any],
    work_dir: Path,
) -> SpooledMultipart:
    boundary = _multipart_boundary(content_type)
    opening = b"--" + boundary + b"\r\n"
    marker = b"\r\n--" + boundary
    multipart = policy["multipart"]
    allowed_fields = set(multipart["allowed_fields"])
    allowed_files = set(multipart.get("allowed_files", []))
    max_field = int(multipart["max_field_bytes"])
    max_image = int(policy["reference_image"]["max_bytes"])
    allowed_image_mimes = set(policy["reference_image"]["mime_types"])
    max_audio = int(policy["reference_audio"]["max_bytes"])
    allowed_audio_mimes = set(policy["reference_audio"]["mime_types"])
    fields: dict[str, str] = {}
    invalid: list[str] = []
    reference_path: Path | None = None
    reference_mime: str | None = None
    reference_audio_path: Path | None = None
    reference_audio_mime: str | None = None

    with body_path.open("rb") as stream:
        if stream.read(len(opening)) != opening:
            raise PolicyError(["body"], "multipart opening boundary is invalid")
        final = False
        while not final:
            headers = _read_part_headers(stream, int(multipart["max_header_bytes"]))
            name, filename, mime = _part_metadata(headers)
            data_start = stream.tell()
            boundary_at = _find_stream_marker(stream, marker)
            if boundary_at < 0:
                raise PolicyError(["body"], "multipart closing boundary is missing")
            length = boundary_at - data_start

            if filename is None:
                if name not in allowed_fields or name in fields or mime not in {None, "text/plain"} or length > max_field:
                    invalid.append(name)
                else:
                    stream.seek(data_start)
                    raw = stream.read(length)
                    try:
                        fields[name] = raw.decode("utf-8", "strict")
                    except UnicodeDecodeError:
                        invalid.append(name)
            else:
                safe_filename = (
                    isinstance(filename, str)
                    and filename not in {"", ".", ".."}
                    and not any(char in filename for char in ("/", "\\", "\0", "\r", "\n"))
                )
                is_image = name == policy["reference_image"]["external_field"]
                is_audio = name == policy["reference_audio"]["external_field"]
                valid_image = is_image and reference_path is None and mime in allowed_image_mimes and 1 <= length <= max_image
                valid_audio = is_audio and reference_audio_path is None and mime in allowed_audio_mimes and 44 <= length <= max_audio
                if name not in allowed_files or not safe_filename or not (valid_image or valid_audio):
                    invalid.append(name)
                else:
                    target = work_dir / ("reference-image.bin" if is_image else "reference-audio.wav")
                    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "wb") as output:
                        _copy_range(stream, data_start, length, output)
                        output.flush()
                        os.fsync(output.fileno())
                    if is_image:
                        reference_path = target
                        reference_mime = mime
                    else:
                        reference_audio_path = target
                        reference_audio_mime = mime

            stream.seek(boundary_at + len(marker))
            suffix = stream.read(2)
            if suffix == b"--":
                final = True
                tail = stream.read()
                if tail not in {b"", b"\r\n"}:
                    invalid.append("body")
            elif suffix != b"\r\n":
                raise PolicyError(["body"], "multipart boundary suffix is invalid")

    invalid.extend(set(multipart["required_fields"]) - set(fields))
    if invalid:
        raise PolicyError(invalid)
    if reference_path is not None and reference_mime is not None:
        _validate_reference_magic(reference_path, reference_mime)
    if reference_audio_path is not None and reference_audio_mime is not None:
        _validate_reference_audio(reference_audio_path, reference_audio_mime)
    return SpooledMultipart(fields, reference_path, reference_mime, reference_audio_path, reference_audio_mime)


def _as_int(fields: dict[str, str], name: str, invalid: list[str]) -> int | None:
    try:
        value = int(fields[name], 10)
    except (KeyError, ValueError):
        invalid.append(name)
        return None
    if str(value) != fields[name].strip():
        invalid.append(name)
        return None
    return value


def _as_float(fields: dict[str, str], name: str, invalid: list[str]) -> float | None:
    try:
        value = float(fields[name])
    except (KeyError, ValueError):
        invalid.append(name)
        return None
    if not (float("-inf") < value < float("inf")):
        invalid.append(name)
        return None
    return value


def validate_video_request(content_type: str, body: bytes, policy: dict[str, Any]) -> None:
    fields = _parse_multipart(content_type, body, policy)
    invalid: list[str] = []
    video = policy["video"]

    prompt_bytes = fields["prompt"].encode("utf-8")
    if not fields["prompt"].strip() or len(prompt_bytes) > int(policy["multipart"]["max_prompt_bytes"]):
        invalid.append("prompt")

    width = _as_int(fields, "width", invalid)
    height = _as_int(fields, "height", invalid)
    allowed_sizes = {tuple(pair) for pair in video["allowed_sizes"]}
    if width is not None and height is not None and (width, height) not in allowed_sizes:
        allowed_widths = {pair[0] for pair in allowed_sizes}
        allowed_heights = {pair[1] for pair in allowed_sizes}
        if width not in allowed_widths:
            invalid.append("width")
        if height not in allowed_heights:
            invalid.append("height")
        if width in allowed_widths and height in allowed_heights:
            invalid.extend(("width", "height"))

    fps = _as_int(fields, "fps", invalid)
    steps = _as_int(fields, "num_inference_steps", invalid)
    flow_shift = _as_float(fields, "flow_shift", invalid)
    seed = _as_int(fields, "seed", invalid)
    if fps is not None and fps != video["fps"]:
        invalid.append("fps")
    if steps is not None and steps != video["num_inference_steps"]:
        invalid.append("num_inference_steps")
    if flow_shift is not None and flow_shift != float(video["flow_shift"]):
        invalid.append("flow_shift")
    if seed is not None and not (video["seed_min"] <= seed <= video["seed_max"]):
        invalid.append("seed")

    variant = None
    try:
        extra = json.loads(fields["extra_params"], object_pairs_hook=_pairs_without_duplicates)
        if not isinstance(extra, dict) or set(extra) != {"task", "duration", "audio_flow_shift"}:
            raise PolicyError(["extra_params"])
    except (json.JSONDecodeError, PolicyError, TypeError, ValueError):
        invalid.append("extra_params")
        extra = None
    if extra is not None:
        if extra.get("task") != video["task"]:
            invalid.append("extra_params.task")
        duration = extra.get("duration")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not (
            video["duration_min"] <= duration <= video["duration_max"]
        ):
            invalid.append("extra_params.duration")
        audio_flow_shift = extra.get("audio_flow_shift")
        if not isinstance(audio_flow_shift, (int, float)) or isinstance(audio_flow_shift, bool) or (
            float(audio_flow_shift) != float(video["audio_flow_shift"])
        ):
            invalid.append("extra_params.audio_flow_shift")
    if invalid:
        raise PolicyError(invalid)


def validate_v2_video_request(
    parsed: SpooledMultipart,
    policy: dict[str, Any],
    readiness: dict[str, Any],
) -> None:
    fields = parsed.fields
    invalid: list[str] = []
    if not (
        readiness.get("state") == "READY"
        and readiness.get("exact_two") is True
        and readiness.get("ranks_ready") is True
        and readiness.get("runtime_ready") is True
        and readiness.get("worker_reachable") is True
    ):
        raise PolicyError(["profile"], "active H3 readiness changed during request")
    profile_name = readiness.get("profile")
    task_type = readiness.get("task_type")
    profile = policy.get("profiles", {}).get(profile_name)
    if not isinstance(profile, dict) or task_type != profile.get("task_type"):
        raise PolicyError(["profile"], "active H3 profile is not attested")

    prompt_bytes = fields["prompt"].encode("utf-8")
    if not fields["prompt"].strip() or len(prompt_bytes) > int(policy["multipart"]["max_prompt_bytes"]):
        invalid.append("prompt")
    width = _as_int(fields, "width", invalid)
    height = _as_int(fields, "height", invalid)
    video = policy["video"]
    fps = _as_int(fields, "fps", invalid)
    steps = _as_int(fields, "num_inference_steps", invalid)
    flow_shift = _as_float(fields, "flow_shift", invalid)
    seed = _as_int(fields, "seed", invalid)
    if fps is not None and fps != video["fps"]:
        invalid.append("fps")
    expected_steps = profile.get("num_inference_steps", video["num_inference_steps"])
    if steps is not None and steps != expected_steps:
        invalid.append("num_inference_steps")
    if flow_shift is not None and flow_shift != float(video["flow_shift"]):
        invalid.append("flow_shift")
    if seed is not None and not (video["seed_min"] <= seed <= video["seed_max"]):
        invalid.append("seed")

    variant = None
    try:
        extra = json.loads(fields["extra_params"], object_pairs_hook=_pairs_without_duplicates)
        if not isinstance(extra, dict) or set(extra) != {"task", "duration", "audio_flow_shift", "required_profile"}:
            raise PolicyError(["extra_params"])
    except (json.JSONDecodeError, PolicyError, TypeError, ValueError):
        invalid.append("extra_params")
        extra = None
    if extra is not None:
        if extra.get("required_profile") != profile_name:
            invalid.append("extra_params.required_profile")
        variants = [item for item in profile.get("request_variants", []) if item.get("task") == extra.get("task")]
        if len(variants) != 1:
            invalid.append("extra_params.task")
            variant = None
        else:
            variant = variants[0]
        duration = extra.get("duration")
        if (
            not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or variant is None
            or not (variant["duration_min"] <= duration <= variant["duration_max"])
        ):
            invalid.append("extra_params.duration")
        audio_shift = extra.get("audio_flow_shift")
        if not isinstance(audio_shift, (int, float)) or isinstance(audio_shift, bool) or float(audio_shift) != float(video["audio_flow_shift"]):
            invalid.append("extra_params.audio_flow_shift")

    if extra is not None and variant is not None and width is not None and height is not None:
        allowed_sizes = {tuple(pair) for pair in variant["allowed_sizes"]}
        if (width, height) not in allowed_sizes:
            invalid.extend(("width", "height"))
    expected_files = int(variant["reference_images"]) if extra is not None and variant is not None else -1
    actual_files = 1 if parsed.reference_path is not None else 0
    if actual_files != expected_files:
        invalid.append("reference_image")
    expected_audio = int(variant.get("reference_audios", 0)) if extra is not None and variant is not None else -1
    actual_audio = 1 if parsed.reference_audio_path is not None else 0
    if actual_audio != expected_audio:
        invalid.append("reference_audio")
    if invalid:
        raise PolicyError(invalid)


def _write_upstream_multipart(parsed: SpooledMultipart, policy: dict[str, Any], destination: Path) -> str:
    boundary = f"h3-gateway-{secrets.token_hex(16)}"
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            for name in policy["multipart"]["required_fields"]:
                output.write(f"--{boundary}\r\n".encode("ascii"))
                output.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"))
                value = parsed.fields[name]
                if name == "extra_params":
                    extra = json.loads(value, object_pairs_hook=_pairs_without_duplicates)
                    extra.pop("required_profile", None)
                    value = json.dumps(extra, separators=(",", ":"))
                output.write(value.encode("utf-8"))
                output.write(b"\r\n")
            if parsed.reference_path is not None and parsed.reference_mime is not None:
                reference = policy["reference_image"]
                extension = "png" if parsed.reference_mime == "image/png" else "jpg"
                output.write(f"--{boundary}\r\n".encode("ascii"))
                output.write(
                    f'Content-Disposition: form-data; name="{reference["upstream_field"]}"; filename="reference.{extension}"\r\n'.encode("ascii")
                )
                output.write(f"Content-Type: {parsed.reference_mime}\r\n\r\n".encode("ascii"))
                with parsed.reference_path.open("rb") as source:
                    while True:
                        chunk = source.read(64 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                output.write(b"\r\n")
            if parsed.reference_audio_path is not None and parsed.reference_audio_mime is not None:
                audio = policy["reference_audio"]
                encoded = base64.b64encode(parsed.reference_audio_path.read_bytes()).decode("ascii")
                payload = json.dumps(
                    {"audio_url": f"data:{parsed.reference_audio_mime};base64,{encoded}"},
                    separators=(",", ":"),
                ).encode("ascii")
                output.write(f"--{boundary}\r\n".encode("ascii"))
                output.write(
                    f'Content-Disposition: form-data; name="{audio["upstream_field"]}"\r\n\r\n'.encode("ascii")
                )
                output.write(payload)
                output.write(b"\r\n")
            output.write(f"--{boundary}--\r\n".encode("ascii"))
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        try:
            destination.unlink()
        except FileNotFoundError:
            pass
        raise
    return f"multipart/form-data; boundary={boundary}"


class ResultStore:
    """Durable original-request results; UNKNOWN is never permission to compute again."""
    maximum = 256 * 1024 * 1024

    def __init__(self, root):
        self.root = Path(root) / "results"
        self.root.mkdir(mode=0o700, exist_ok=True)
        if self.root.is_symlink(): raise RuntimeError("result directory must not be a symlink")
        self.lock = threading.RLock()
        self.running = set()

    def path(self, rid, suffix):
        if not re.fullmatch(r"[a-f0-9]{32}", rid): raise ValueError("invalid request id")
        return self.root / (rid + suffix)

    def write(self, rid, value):
        fd, name = tempfile.mkstemp(prefix=".receipt-", dir=self.root)
        try:
            with os.fdopen(fd, "w") as out:
                json.dump(value, out); out.flush(); os.fsync(out.fileno())
            os.replace(name, self.path(rid, ".json"))
            self.sync()
        finally:
            Path(name).unlink(missing_ok=True)

    def sync(self):
        fd = os.open(self.root, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)

    def begin(self, rid, digest, identity):
        with self.lock:
            path = self.path(rid, ".json")
            if path.exists():
                old = _load_json_object(path)
                if old["requestSha256"] != digest: raise ValueError("request id reused with different content")
                return False
            if shutil.disk_usage(self.root).free < self.maximum * 2:
                raise OSError("insufficient space to retain video result")
            self.write(rid, {"state": "RUNNING", "requestSha256": digest, "identity": identity})
            self.running.add(rid)
            return True

    def finish(self, rid, response):
        path = self.path(rid, ".part")
        digest = hashlib.sha256(); size = 0
        try:
            with path.open("xb") as out:
                while True:
                    data = response.read(65536)
                    if not data: break
                    size += len(data)
                    if size > self.maximum: raise ValueError("video response exceeds retention limit")
                    out.write(data); digest.update(data)
                expected = response.getheader("Content-Length")
                if expected is not None and size != int(expected): raise ValueError("truncated upstream response")
                out.flush(); os.fsync(out.fileno())
            os.replace(path, self.path(rid, ".body")); self.sync()
            record = _load_json_object(self.path(rid, ".json"))
            record.update(state="SUCCEEDED" if response.status == 200 else "FAILED", status=response.status,
                          contentType=response.getheader("Content-Type", "application/octet-stream"), size=size, sha256=digest.hexdigest())
            self.write(rid, record)
        finally:
            self.running.discard(rid)
            path.unlink(missing_ok=True)

    def get(self, rid):
        with self.lock:
            record = _load_json_object(self.path(rid, ".json"))
            if record["state"] == "RUNNING" and rid not in self.running:
                record = {**record, "state": "UNKNOWN"}
            return record


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, config: GatewayConfig):
        self.config = config
        self.gateway_token = _load_token(config.token_file)
        self.request_policy = _load_json_object(config.policy_file)
        if self.request_policy.get("schema_version") == 2:
            if config.spool_dir is None:
                raise RuntimeError("v2 gateway policy requires a spool directory")
            spool = Path(config.spool_dir)
            if spool.is_symlink():
                raise RuntimeError("gateway spool directory must not be a symlink")
            spool.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not spool.is_dir():
                raise RuntimeError("gateway spool path is not a directory")
            os.chmod(spool, 0o700)
        self.results = ResultStore(config.spool_dir) if config.spool_dir is not None else None
        self.generation_semaphore = threading.BoundedSemaphore(1)
        self.draining = threading.Event()
        self.counter_lock = threading.Lock()
        self.forwarded_count = 0
        super().__init__((config.bind_host, config.bind_port), GatewayHandler)

    def next_forwarded_count(self) -> int:
        with self.counter_lock:
            self.forwarded_count += 1
            return self.forwarded_count

    def cluster_ready(self) -> bool:
        try:
            state = self.readiness_document()
        except RuntimeError:
            return False
        return (
            state.get("state") == "READY"
            and state.get("exact_two") is True
            and state.get("ranks_ready") is True
            and state.get("runtime_ready") is True
            and state.get("worker_reachable") is True
        )

    def readiness_document(self) -> dict[str, Any]:
        return _load_json_object(self.config.readiness_file)

    @contextlib.contextmanager
    def generation_lease(self):
        if self.config.lifecycle_lock is None:
            yield
            return
        descriptor = os.open(self.config.lifecycle_lock, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("lifecycle_busy") from exc
            yield
        finally:
            os.close(descriptor)


class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MiniMaxH3Gateway/1"
    sys_version = ""
    server: GatewayServer

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, format: str, *args: Any) -> None:
        # Access logs are disabled so credentials and prompt-bearing URLs can
        # never be added by a future route. Lifecycle evidence records only
        # counters and explicit, redacted request IDs.
        return

    def _json(self, status_code: int, document: dict[str, Any], *, extra_headers: dict[str, str] | None = None) -> None:
        payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def _authorized(self) -> bool:
        values = self.headers.get_all("Authorization", [])
        if len(values) != 1:
            return False
        value = values[0]
        if not value.startswith("Bearer "):
            return False
        supplied = value[7:].encode("utf-8", "surrogatepass")
        return hmac.compare_digest(supplied, self.server.gateway_token)

    def _valid_raw_path(self) -> bool:
        path = self.path
        if not path.startswith("/") or path.startswith("//"):
            return False
        if any(marker in path for marker in ("%", "?", "#", "\\", "//", "/./", "/../")):
            return False
        return path in ALLOWED_ROUTES.get(self.command, set())

    def _route_exists_for_other_method(self) -> bool:
        return any(self.path in routes for method, routes in ALLOWED_ROUTES.items() if method != self.command)

    def _common_gate(self) -> bool:
        if not self._authorized():
            self._json(401, {"error": "unauthorized"}, extra_headers={"WWW-Authenticate": "Bearer"})
            return False
        if not self._valid_raw_path():
            code = 405 if self._route_exists_for_other_method() else 404
            self._json(code, {"error": "method_not_allowed" if code == 405 else "route_not_allowed"})
            return False
        return True

    def do_GET(self) -> None:
        if not self._common_gate():
            return
        if self.path == "/v1/videos/result":
            self._result()
            return
        if self.path == "/health":
            if not self.server.cluster_ready():
                self._json(503, {"error": "cluster_not_ready"})
                return
            probe = http.client.HTTPConnection(self.server.config.upstream_host, self.server.config.upstream_port, timeout=3)
            try:
                probe.request("GET", "/health")
                if probe.getresponse().status != 200:
                    raise OSError("unhealthy runtime")
            except (OSError, http.client.HTTPException):
                self._json(503, {"error": "runtime_not_ready"})
                return
            finally:
                probe.close()
            state = self.server.readiness_document()
            self._json(200, {"state": "READY", "profile_identity": state.get("profile_identity")})
            return
        self._forward(body=None, content_type=None, count_generation=False)

    def _result(self):
        rid = self.headers.get("X-H3-Request-ID", "")
        try:
            if self.server.results is None: raise FileNotFoundError()
            record = self.server.results.get(rid)
        except ValueError:
            self._json(400, {"error": "invalid_request_id"}); return
        except (OSError, RuntimeError):
            self._json(404, {"error": "result_not_found"}); return
        if record["state"] in {"RUNNING", "UNKNOWN"}:
            self._json(202 if record["state"] == "RUNNING" else 409, {"state": record["state"], "requestId": rid}); return
        size = record["size"]; start, end = 0, size - 1
        partial = self.headers.get("Range")
        if partial:
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", partial)
            if not match:
                self._json(416, {"error": "invalid_range"}); return
            start = int(match[1]); end = int(match[2]) if match[2] else end
            if start > end or end >= size:
                self._json(416, {"error": "invalid_range"}); return
        self.send_response(206 if partial else record["status"])
        self.send_header("Content-Type", record["contentType"])
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("X-H3-Result-SHA256", record["sha256"])
        if partial: self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if record.get("identity"):
            identity = json.dumps(record["identity"], sort_keys=True, separators=(",", ":")).encode()
            self.send_header("X-H3-Identity", base64.urlsafe_b64encode(identity).decode().rstrip("="))
        self.send_header("Cache-Control", "no-store"); self.send_header("Connection", "close"); self.end_headers()
        try:
            with self.server.results.path(rid, ".body").open("rb") as source:
                source.seek(start); remaining = end - start + 1
                while remaining:
                    chunk = source.read(min(65536, remaining))
                    if not chunk: break
                    self.wfile.write(chunk); remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError): pass
        self.close_connection = True

    def do_POST(self) -> None:
        if not self._common_gate():
            return
        if self.path == "/v1/videos/cancel":
            if self.headers.get("Content-Length") != "0" or self.headers.get("Transfer-Encoding") or not re.fullmatch(r"[0-9a-f]{32}", self.headers.get("X-H3-Request-ID", "")):
                self._json(400, {"error": "invalid_cancel_request"})
                return
            self._forward(body=b"", content_type="application/octet-stream", count_generation=False)
            return
        if self.headers.get_all("Transfer-Encoding", []):
            self._json(400, {"error": "transfer_encoding_not_allowed"})
            return
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isdigit():
            self._json(411, {"error": "content_length_required"})
            return
        length = int(lengths[0])
        if length > self.server.config.max_body_bytes:
            self._json(413, {"error": "body_too_large"})
            return
        if length <= 0:
            self._json(400, {"error": "empty_body"})
            return
        content_types = self.headers.get_all("Content-Type", [])
        if len(content_types) != 1:
            self._json(400, {"error": "single_content_type_required"})
            return
        content_type = content_types[0]
        if self.server.request_policy.get("schema_version") == 2:
            self._do_post_v2(length, content_type)
            return
        try:
            body = self.rfile.read(length)
        except (TimeoutError, socket.timeout):
            self._json(408, {"error": "request_timeout"})
            return
        if len(body) != length:
            self._json(400, {"error": "truncated_body"})
            return
        try:
            validate_video_request(content_type, body, self.server.request_policy)
        except PolicyError as exc:
            self._json(422, {"error": "invalid_request", "invalid_fields": exc.invalid_fields})
            return
        except (KeyError, TypeError, ValueError):
            self._json(422, {"error": "invalid_request", "invalid_fields": ["body"]})
            return
        if not self.server.cluster_ready():
            self._json(503, {"error": "cluster_not_ready"})
            return
        if self.server.draining.is_set() or not self.server.generation_semaphore.acquire(blocking=False):
            self._json(503, {"error": "generation_busy"})
            return
        try:
            retained_response = self._forward(body=body, content_type=content_type, count_generation=True)
        finally:
            self.server.generation_semaphore.release()
        if retained_response:
            self._result()

    def _do_post_v2(self, length: int, content_type: str) -> None:
        if not self.server.cluster_ready():
            self._json(503, {"error": "cluster_not_ready"})
            return
        assert self.server.config.spool_dir is not None
        try:
            with tempfile.TemporaryDirectory(
                prefix="request-",
                dir=self.server.config.spool_dir,
            ) as temporary:
                work_dir = Path(temporary)
                os.chmod(work_dir, 0o700)
                request_path = work_dir / "request.multipart"
                descriptor = os.open(request_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                remaining = length
                try:
                    with os.fdopen(descriptor, "wb") as output:
                        while remaining:
                            chunk = self.rfile.read(min(64 * 1024, remaining))
                            if not chunk:
                                break
                            output.write(chunk)
                            remaining -= len(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                except (TimeoutError, socket.timeout):
                    self._json(408, {"error": "request_timeout"})
                    return
                if remaining:
                    self._json(400, {"error": "truncated_body"})
                    return
                parsed = _parse_spooled_multipart(content_type, request_path, self.server.request_policy, work_dir)
                try:
                    with self.server.generation_lease():
                        readiness = self.server.readiness_document()
                        validate_v2_video_request(parsed, self.server.request_policy, readiness)
                        upstream_path = work_dir / "upstream.multipart"
                        upstream_type = _write_upstream_multipart(parsed, self.server.request_policy, upstream_path)
                        if self.server.draining.is_set() or not self.server.generation_semaphore.acquire(blocking=False):
                            self._json(503, {"error": "generation_busy"})
                            return
                        try:
                            retained_response = self._forward(
                                body=upstream_path,
                                content_type=upstream_type,
                                count_generation=True,
                                identity=readiness.get("profile_identity"),
                                request_digest=hashlib.sha256(request_path.read_bytes()).hexdigest(),
                                upstream_timeout_seconds=self.server.request_policy["profiles"][readiness["profile"]].get("upstream_timeout_seconds"),
                            )
                        finally:
                            self.server.generation_semaphore.release()
                    # The model has completed and its result is durable. A slow
                    # or disconnected original downloader must not retain the
                    # inference semaphore or shared lifecycle lease.
                    if retained_response:
                        self._result()
                except RuntimeError as exc:
                    if str(exc) == "lifecycle_busy":
                        self._json(503, {"error": "lifecycle_busy"})
                        return
                    raise
        except PolicyError as exc:
            self._json(422, {"error": "invalid_request", "invalid_fields": exc.invalid_fields})
        except (KeyError, TypeError, ValueError, RuntimeError):
            self._json(422, {"error": "invalid_request", "invalid_fields": ["body"]})

    def _forward(
        self,
        *,
        body: bytes | Path | None,
        content_type: str | None,
        count_generation: bool,
        identity: dict[str, Any] | None = None,
        upstream_timeout_seconds: int | None = None,
        request_digest: str | None = None,
    ) -> bool | None:
        connection = http.client.HTTPConnection(
            self.server.config.upstream_host,
            self.server.config.upstream_port,
            timeout=10,
        )
        headers: dict[str, str] = {"Host": f"{self.server.config.upstream_host}:{self.server.config.upstream_port}"}
        client_id = self.headers.get("X-H3-Request-ID", "")
        if client_id:
            if not re.fullmatch(r"[0-9a-f]{32}", client_id):
                self._json(400, {"error": "invalid_request_id"})
                return
            headers["X-H3-Request-ID"] = client_id
        if body is not None:
            headers["Content-Type"] = content_type or "application/octet-stream"
            headers["Content-Length"] = str(body.stat().st_size if isinstance(body, Path) else len(body))
        forwarded_count = self.server.next_forwarded_count() if count_generation else None
        response_started = False
        retained = bool(count_generation and client_id and self.server.results)
        if retained:
            digest = hashlib.sha256()
            if isinstance(body, Path):
                with body.open("rb") as source:
                    for chunk in iter(lambda: source.read(65536), b""): digest.update(chunk)
            else: digest.update(body or b"")
            try:
                if not self.server.results.begin(client_id, (request_digest or digest.hexdigest()), identity):
                    return True
            except ValueError:
                self._json(409, {"error": "request_id_conflict"}); return
            except OSError:
                self._json(507, {"error": "result_storage_unavailable"}); return
        try:
            if isinstance(body, Path):
                with body.open("rb") as stream:
                    connection.request(self.command, self.path, body=stream, headers=headers)
            else:
                connection.request(self.command, self.path, body=body, headers=headers)
            wait_seconds = self.server.config.upstream_timeout_seconds if upstream_timeout_seconds is None else upstream_timeout_seconds
            if connection.sock is not None:
                connection.sock.settimeout(None if wait_seconds == 0 else wait_seconds)
            upstream = connection.getresponse()
            if retained:
                self.server.results.finish(client_id, upstream)
                return True
            self.send_response(upstream.status)
            for name, value in upstream.getheaders():
                lowered = name.lower()
                if lowered in HOP_BY_HOP_HEADERS or lowered == "server" or lowered == "date":
                    continue
                self.send_header(name, value)
            if forwarded_count is not None:
                self.send_header("X-H3-Forwarded-Count", str(forwarded_count))
            if count_generation and identity is not None:
                raw_identity = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
                self.send_header("X-H3-Identity", base64.urlsafe_b64encode(raw_identity).decode("ascii").rstrip("="))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            response_started = True
            disconnected = False
            while True:
                chunk = upstream.read(64 * 1024)
                if not chunk:
                    break
                if disconnected:
                    continue
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    disconnected = True
                    self.server.draining.set()
            self.close_connection = True
        except (OSError, ValueError, http.client.HTTPException, TimeoutError, socket.timeout):
            if not response_started and not self.wfile.closed:
                self._json(504, {"error": "upstream_unavailable"})
            else:
                self.close_connection = True
        finally:
            connection.close()
            if retained: self.server.results.running.discard(client_id)
            if count_generation:
                self.server.draining.clear()

    def do_PUT(self) -> None:
        self._reject_method()

    def do_DELETE(self) -> None:
        self._reject_method()

    def do_PATCH(self) -> None:
        self._reject_method()

    def do_OPTIONS(self) -> None:
        self._reject_method()

    def do_HEAD(self) -> None:
        self._reject_method()

    def do_TRACE(self) -> None:
        self._reject_method()

    def do_CONNECT(self) -> None:
        self._reject_method()

    def _reject_method(self) -> None:
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
        else:
            self._json(405, {"error": "method_not_allowed"})


def create_server(config: GatewayConfig) -> GatewayServer:
    return GatewayServer(config)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--policy-file", default="/etc/minimax-h3/request-policy-v2.json")
    parser.add_argument("--readiness-file", default="/run/minimax-h3-two-node/state.json")
    args = parser.parse_args()
    config = GatewayConfig.production_defaults(
        token_file=Path(args.token_file),
        policy_file=Path(args.policy_file),
        readiness_file=Path(args.readiness_file),
    )
    server = create_server(config)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
