#!/usr/bin/env python3
"""Independently compare a built Ref2VA+HMNSFW tree with the pinned Comfy oracle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile


def tensor_sha(value) -> str:
    import torch

    return hashlib.sha256(value.detach().contiguous().view(torch.uint8).cpu().numpy().tobytes()).hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    payload = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass


def main() -> int:
    from safetensors import safe_open

    parser = argparse.ArgumentParser()
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--builder-evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    reference = json.loads(args.reference.read_text())
    builder = json.loads(args.builder_evidence.read_text())
    expected = {row["target_key"]: row for row in reference["targets"]}
    built = {row["target"]: row for row in builder["targets"]}
    if len(expected) != 100 or set(expected) != set(built):
        raise RuntimeError("reference and builder target sets differ")

    parent = args.parent / "Ref2VA/transformer"
    candidate = args.candidate / "Ref2VA/transformer"
    parent_map = json.loads((parent / "model.safetensors.index.json").read_text())["weight_map"]
    candidate_map = json.loads((candidate / "model.safetensors.index.json").read_text())["weight_map"]
    if parent_map != candidate_map:
        raise RuntimeError("transformer index mapping changed")

    target_mismatches = []
    unchanged_mismatches = []
    for shard_name in sorted(set(parent_map.values())):
        keys = sorted(key for key, shard in parent_map.items() if shard == shard_name)
        with safe_open(parent / shard_name, framework="pt", device="cpu") as left, safe_open(
            candidate / shard_name, framework="pt", device="cpu"
        ) as right:
            for key in keys:
                left_hash = tensor_sha(left.get_tensor(key))
                right_hash = tensor_sha(right.get_tensor(key))
                if key in expected:
                    row = expected[key]
                    if left_hash != row["base_sha256"] or right_hash != row["output_sha256"] or built[key]["before_sha256"] != left_hash or built[key]["after_sha256"] != right_hash:
                        target_mismatches.append(key)
                elif left_hash != right_hash:
                    unchanged_mismatches.append(key)
    if target_mismatches or unchanged_mismatches:
        raise RuntimeError(
            f"HMNSFW equivalence failed: targets={target_mismatches[:3]} unchanged={unchanged_mismatches[:3]}"
        )
    output = {
        "schema_version": 1,
        "terminal": "PASS",
        "target_count": len(expected),
        "target_mismatches": 0,
        "unchanged_tensor_count": len(parent_map) - len(expected),
        "unchanged_mismatches": 0,
        "reference_sha256": hashlib.sha256(args.reference.read_bytes()).hexdigest(),
        "builder_evidence_sha256": hashlib.sha256(args.builder_evidence.read_bytes()).hexdigest(),
    }
    atomic_json(args.output, output)
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
