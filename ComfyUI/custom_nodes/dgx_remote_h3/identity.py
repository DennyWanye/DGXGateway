from __future__ import annotations

import base64
import hashlib
import json


FIELDS = {
    "schema_version", "name", "task_family", "supported_tasks", "text_encoder",
    "transformer", "modifier", "curve", "model_tree_sha256", "runtime_image_id",
    "runtime_overlay_sha256", "rank_identities",
}


def decode_and_validate(value: str, *, required_profile: str) -> dict:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        document = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("invalid identity header") from exc
    if not isinstance(document, dict) or set(document) != FIELDS | {"contract_sha256"}:
        raise ValueError("identity fields mismatch")
    if document.get("schema_version") != 1 or document.get("name") != required_profile:
        raise ValueError("identity profile mismatch")
    if document.get("rank_identities") != [0, 1]:
        raise ValueError("identity rank mismatch")
    payload = {key: document[key] for key in FIELDS}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    if document.get("contract_sha256") != hashlib.sha256(canonical).hexdigest():
        raise ValueError("identity hash mismatch")
    return document
