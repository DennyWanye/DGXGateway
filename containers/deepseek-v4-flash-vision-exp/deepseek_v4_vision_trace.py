"""Observation-only trace helpers for the native DeepSeek V4 vision path.

The module deliberately never reads tensor values.  It records only immutable
process identity and tensor metadata so enabling the acceptance trace cannot
change model inputs, outputs, weights, or sampling decisions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any


_NONCE_RE = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_LOCK = threading.Lock()
_SEQUENCE = 0
_PREVIOUS_DIGEST = "0" * 64


def _enabled() -> bool:
    return os.environ.get("DSV4_VISION_TRACE") == "1"


def _identity() -> dict[str, Any]:
    try:
        stat_text = Path("/proc/self/stat").read_text(encoding="utf-8")
        start_ticks = stat_text[stat_text.rfind(")") + 2 :].split()[19]
    except (OSError, IndexError):
        start_ticks = "unavailable"
    try:
        cgroup = Path("/proc/self/cgroup").read_bytes()
        cgroup_sha256 = hashlib.sha256(cgroup).hexdigest()
    except OSError:
        cgroup_sha256 = "unavailable"
    role = os.environ.get("VISION_ROLE", "")
    topology_rank = {"head": "0", "worker": "1"}.get(role, "unknown")
    rank = os.environ.get("RANK") or os.environ.get("LOCAL_RANK") or topology_rank
    world_size = os.environ.get("WORLD_SIZE") or ("2" if topology_rank != "unknown" else "unknown")
    return {
        "pid": os.getpid(),
        "pid_start_ticks": start_ticks,
        "hostname": socket.gethostname(),
        "cgroup_sha256": cgroup_sha256,
        "role": role or "unknown",
        "rank": rank,
        "world_size": world_size,
    }


def tensor_metadata(value: Any, *, _depth: int = 0) -> Any:
    """Return bounded shape/dtype/device metadata without inspecting values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if _depth >= 3:
        return {"type": type(value).__name__, "truncated": True}
    if isinstance(value, (tuple, list)):
        return [tensor_metadata(item, _depth=_depth + 1) for item in value[:8]]
    shape = getattr(value, "shape", None)
    if shape is not None:
        return {
            "type": type(value).__name__,
            "shape": [str(dimension) for dimension in shape],
            "dtype": str(getattr(value, "dtype", "unknown")),
            "device": str(getattr(value, "device", "unknown")),
        }
    return {"type": type(value).__name__}


def emit_trace(event: str, **metadata: Any) -> None:
    """Append one hash-chained event when the explicit trace gate is enabled."""
    if not _enabled():
        return
    nonce = os.environ.get("DSV4_VISION_TRACE_NONCE", "")
    trace_dir = Path(os.environ.get("DSV4_VISION_TRACE_DIR", ""))
    if not _NONCE_RE.fullmatch(nonce):
        raise RuntimeError("DSV4 vision trace nonce is missing or unsafe")
    if not trace_dir.is_absolute() or trace_dir.is_symlink():
        raise RuntimeError("DSV4 vision trace directory must be an absolute non-symlink")
    trace_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    global _PREVIOUS_DIGEST, _SEQUENCE
    with _LOCK:
        _SEQUENCE += 1
        identity = _identity()
        record = {
            "schema": "deepseek-v4-native-vision-trace-v1",
            "nonce": nonce,
            "event": event,
            "sequence": _SEQUENCE,
            "monotonic_ns": time.monotonic_ns(),
            "identity": identity,
            "metadata": {
                key: tensor_metadata(value) for key, value in sorted(metadata.items())
            },
            "previous_sha256": _PREVIOUS_DIGEST,
        }
        canonical = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        record["sha256"] = hashlib.sha256(canonical).hexdigest()
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        rank = str(identity["rank"])
        if not re.fullmatch(r"[0-9]+", rank):
            rank = "unknown"
        destination = trace_dir / f"native-rank-{rank}.jsonl"
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(destination, flags, 0o600)
        try:
            os.write(descriptor, encoded.encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _PREVIOUS_DIGEST = record["sha256"]
