#!/usr/bin/env python3
"""Canonical ProfileIdentityV1 helpers shared by deployment and evidence tools."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Mapping


IDENTITY_FIELDS = {
    "schema_version",
    "name",
    "task_family",
    "supported_tasks",
    "text_encoder",
    "transformer",
    "modifier",
    "curve",
    "model_tree_sha256",
    "runtime_image_id",
    "runtime_overlay_sha256",
    "rank_identities",
}


def _identity_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = {key: value[key] for key in sorted(IDENTITY_FIELDS) if key in value}
    required = IDENTITY_FIELDS - {"text_encoder"}
    missing = required - set(payload)
    if missing:
        raise ValueError(f"identity fields are missing: {','.join(sorted(missing))}")
    if value.get("schema_version") != 1:
        raise ValueError("unsupported identity schema")
    if value.get("rank_identities") != [0, 1]:
        raise ValueError("identity must attest exact ranks 0 and 1")
    return payload


def canonical_identity_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        _identity_payload(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def seal_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    result = _identity_payload(value)
    result["contract_sha256"] = hashlib.sha256(canonical_identity_bytes(result)).hexdigest()
    return result


def validate_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    sealed = seal_identity(value)
    if value.get("contract_sha256") != sealed["contract_sha256"]:
        raise ValueError("identity contract hash mismatch")
    if set(value) != set(sealed):
        raise ValueError("identity has missing or unknown fields")
    return sealed


def encode_identity_header(value: Mapping[str, Any]) -> str:
    sealed = validate_identity(value)
    raw = json.dumps(sealed, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_identity_header(value: str) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        raise ValueError("identity header is missing")
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.urlsafe_b64decode(value + padding)
        document = json.loads(decoded.decode("utf-8"))
    except Exception as exc:
        raise ValueError("identity header is invalid") from exc
    if not isinstance(document, dict):
        raise ValueError("identity header must contain an object")
    return validate_identity(document)
