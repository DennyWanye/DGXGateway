from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from typing import Any


RUNTIME_CONTRACT_PATH = pathlib.Path("/opt/dgx/contracts/runtime.json")
_COMMON = {
    "secret_path": "/run/dgx-h3-secret/token",
    "connect_timeout_seconds": 10,
    "total_timeout_seconds": 3700,
    "max_response_bytes": 256 * 1024 * 1024,
}
_PROFILES = {
    "production": {
        "schema_id": "dgx-h3-production-v1",
        "profile": "production",
        "endpoint_id": "h3-head-lan-v1",
        "endpoint": "http://192.0.2.30:18081/v1/videos/sync",
        **_COMMON,
    },
    "test": {
        "schema_id": "dgx-h3-test-v1",
        "profile": "test",
        "endpoint_id": "fake-gateway-v1",
        "endpoint": "http://fake-gateway:19081/v1/videos/sync",
        **_COMMON,
        "connect_timeout_seconds": 2,
        "total_timeout_seconds": 5,
    },
}


class ContractError(RuntimeError):
    """A sanitized immutable-runtime-contract error."""


@dataclass(frozen=True)
class RuntimeContract:
    schema_id: str
    profile: str
    endpoint_id: str
    endpoint_host: str
    endpoint_port: int
    endpoint_path: str
    secret_path: pathlib.Path
    connect_timeout_seconds: int
    total_timeout_seconds: int | None
    max_response_bytes: int


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("Runtime contract contains a duplicate field.")
        result[key] = value
    return result


def load_runtime_contract(
    path: pathlib.Path | str = RUNTIME_CONTRACT_PATH,
    *,
    expected_profile: str = "production",
) -> RuntimeContract:
    if expected_profile not in _PROFILES:
        raise ContractError("Runtime contract profile is not supported.")
    try:
        raw = pathlib.Path(path).read_bytes()
    except OSError as exc:
        raise ContractError("Runtime contract is unavailable.") from exc
    if not raw or len(raw) > 4096:
        raise ContractError("Runtime contract has an invalid size.")
    try:
        document = json.loads(raw, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, UnicodeDecodeError, ContractError) as exc:
        raise ContractError("Runtime contract is invalid.") from exc
    expected = _PROFILES[expected_profile]
    if not isinstance(document, dict) or document != expected:
        raise ContractError("Runtime contract does not match the pinned profile.")

    if expected_profile == "production":
        host, port = "192.0.2.30", 18081
    else:
        host, port = "fake-gateway", 19081
    return RuntimeContract(
        schema_id=document["schema_id"],
        profile=document["profile"],
        endpoint_id=document["endpoint_id"],
        endpoint_host=host,
        endpoint_port=port,
        endpoint_path="/v1/videos/sync",
        secret_path=pathlib.Path(document["secret_path"]),
        connect_timeout_seconds=document["connect_timeout_seconds"],
        total_timeout_seconds=document["total_timeout_seconds"],
        max_response_bytes=document["max_response_bytes"],
    )
