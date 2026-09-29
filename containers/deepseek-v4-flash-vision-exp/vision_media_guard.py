"""Pure-ASGI allowlist for DeepSeek V4 single-image chat requests."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Any, Awaitable, Callable

from deepseek_v4_vision_trace import emit_trace


MAX_BODY_BYTES = 32 * 1024 * 1024
MAX_BASE64_CHARS = 16 * 1024 * 1024
MAX_IMAGE_BYTES = 12 * 1024 * 1024
ALLOWED_IMAGE_PREFIXES = {
    "png": "data:image/png;base64,",
    "jpeg": "data:image/jpeg;base64,",
    "webp": "data:image/webp;base64,",
}


class GuardRejected(ValueError):
    def __init__(self, code: str, status: int = 422):
        super().__init__(code)
        self.code = code
        self.status = status


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise GuardRejected("duplicate_json_key")
        value[key] = item
    return value


def _decode_image(uri: str) -> tuple[str, bytes]:
    media_type = ""
    payload = ""
    for candidate, prefix in ALLOWED_IMAGE_PREFIXES.items():
        if uri.startswith(prefix):
            media_type = candidate
            payload = uri[len(prefix) :]
            break
    if not media_type:
        raise GuardRejected("image_url_must_be_canonical_data_uri")
    if not payload or len(payload) > MAX_BASE64_CHARS or len(payload) % 4:
        raise GuardRejected("image_base64_size_or_padding_invalid")
    try:
        decoded = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise GuardRejected("image_base64_invalid") from exc
    if not decoded or len(decoded) > MAX_IMAGE_BYTES:
        raise GuardRejected("image_decoded_size_invalid")
    if base64.b64encode(decoded).decode("ascii") != payload:
        raise GuardRejected("image_base64_not_canonical")
    valid_magic = {
        "png": decoded.startswith(b"\x89PNG\r\n\x1a\n"),
        "jpeg": decoded.startswith(b"\xff\xd8\xff") and decoded.endswith(b"\xff\xd9"),
        "webp": len(decoded) >= 12
        and decoded.startswith(b"RIFF")
        and decoded[8:12] == b"WEBP",
    }
    if not valid_magic[media_type]:
        raise GuardRejected("image_signature_mismatch")
    return media_type, decoded


def validate_chat_body(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicates)
    except GuardRejected:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GuardRejected("invalid_json") from exc
    if not isinstance(value, dict) or not isinstance(value.get("messages"), list):
        raise GuardRejected("messages_must_be_a_list")
    image_count = 0
    for message in value["messages"]:
        if not isinstance(message, dict):
            raise GuardRejected("message_must_be_an_object")
        content = message.get("content")
        # OpenAI assistant tool-call turns legitimately omit text. This is not
        # media: leave tool schema validation to vLLM and keep every image gate.
        if content is None and message.get("role") == "assistant":
            calls = message.get("tool_calls")
            if isinstance(calls, list) and calls and all(
                isinstance(call, dict)
                and call.get("type") == "function"
                and isinstance(call.get("id"), str) and bool(call["id"])
                and isinstance(call.get("function"), dict)
                and isinstance(call["function"].get("name"), str)
                and bool(call["function"]["name"])
                and isinstance(call["function"].get("arguments"), str)
                for call in calls
            ):
                continue
        if isinstance(content, str):
            continue
        if not isinstance(content, list):
            raise GuardRejected("message_content_must_be_text_or_parts")
        for part in content:
            if not isinstance(part, dict):
                raise GuardRejected("content_part_must_be_an_object")
            part_type = part.get("type")
            if part_type == "text":
                if set(part) != {"type", "text"} or not isinstance(part["text"], str):
                    raise GuardRejected("text_part_invalid")
                continue
            if part_type != "image_url":
                raise GuardRejected("unsupported_media_part")
            if set(part) != {"type", "image_url"}:
                raise GuardRejected("image_part_has_unsupported_fields")
            image = part["image_url"]
            if not isinstance(image, dict) or not set(image).issubset({"url", "detail"}):
                raise GuardRejected("image_url_must_be_an_object")
            if set(image) not in ({"url"}, {"url", "detail"}):
                raise GuardRejected("image_url_fields_invalid")
            if "detail" in image and image["detail"] not in {"auto", "low", "high"}:
                raise GuardRejected("image_detail_invalid")
            if not isinstance(image["url"], str):
                raise GuardRejected("image_url_value_invalid")
            _decode_image(image["url"])
            image_count += 1
            if image_count > 1:
                raise GuardRejected("only_one_image_is_allowed")
    return value


class VisionMediaGuard:
    """ASGI middleware loaded by ``vllm serve --middleware``."""

    def __init__(self, app: Callable[..., Awaitable[None]]):
        self.app = app

    @staticmethod
    async def _error(send: Callable[..., Awaitable[None]], error: GuardRejected) -> None:
        payload = json.dumps(
            {"error": {"type": "invalid_request_error", "code": error.code}},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": error.status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"cache-control", b"no-store"),
                    (b"content-length", str(len(payload)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})

    async def __call__(self, scope: dict[str, Any], receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method", "")).upper()
        request_path = scope.get("path", "")
        # Metrics stays available only on the loopback-bound upstream so the
        # lifecycle adapter can drain safely. The production nginx allowlist
        # does not expose it on the LAN.
        if method == "GET" and request_path in {"/health", "/metrics", "/v1/models"}:
            await self.app(scope, receive, send)
            return
        if method != "POST" or request_path != "/v1/chat/completions":
            await self._error(send, GuardRejected("route_not_allowed", 404))
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        content_type = headers.get(b"content-type", b"").decode("latin-1").split(";", 1)[0].strip().lower()
        content_encoding = headers.get(b"content-encoding", b"identity").decode("latin-1").strip().lower()
        if content_type != "application/json" or content_encoding not in {"", "identity"}:
            await self._error(send, GuardRejected("content_type_or_encoding_invalid"))
            return
        body = bytearray()
        disconnected = False
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                disconnected = True
                break
            if message.get("type") != "http.request":
                await self._error(send, GuardRejected("invalid_asgi_request"))
                return
            body.extend(message.get("body", b""))
            if len(body) > MAX_BODY_BYTES:
                await self._error(send, GuardRejected("request_body_too_large", 413))
                return
            if not message.get("more_body", False):
                break
        if disconnected:
            return
        try:
            parsed = validate_chat_body(bytes(body))
        except GuardRejected as error:
            await self._error(send, error)
            return
        body_bytes = bytes(body)
        image_count = sum(
            1
            for message in parsed["messages"]
            if isinstance(message.get("content"), list)
            for part in message["content"]
            if part.get("type") == "image_url"
        )
        emit_trace(
            "asgi.request.accepted",
            body_sha256=hashlib.sha256(body_bytes).hexdigest(),
            body_size=len(body_bytes),
            image_count=image_count,
        )
        delivered = False

        async def replay_receive() -> dict[str, Any]:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": body_bytes, "more_body": False}

        await self.app(scope, replay_receive, send)
