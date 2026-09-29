#!/usr/bin/env python3
"""Build an immutable Ref2VA + HMNSFW profile from a sealed Ref2VA parent."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any


HMNSFW_SHA256 = "a07732a84fd733085eb5d910f602f918fa7a3658117116927e4329f5951a9d2d"
EXPECTED_TARGETS = 100
PROFILE_ID = "heretic-ref2va-hmnsfw-v1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def lora_target(key: str) -> tuple[str, str]:
    prefix = "diffusion_model."
    for suffix, side in ((".lora_A.weight", "A"), (".lora_B.weight", "B")):
        if key.startswith(prefix) and key.endswith(suffix):
            return key[len(prefix) : -len(suffix)] + ".weight", side
    raise ValueError(f"unrecognized HMNSFW key: {key}")


def merge_weight(weight: Any, lora_a: Any, lora_b: Any, strength: float):
    import torch

    delta = torch.matmul(lora_b.to(torch.float32), lora_a.to(torch.float32))
    return torch.add(weight, torch.mul(delta, float(strength)).to(weight.dtype))


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def file_inventory(root: Path, *, excluded: set[str] | None = None) -> list[dict[str, Any]]:
    excluded = excluded or set()
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"symlink forbidden in profile: {path}")
        if path.is_file() and path.relative_to(root).as_posix() not in excluded:
            rows.append({
                "path": path.relative_to(root).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            })
    return rows


def main() -> int:
    import torch
    from safetensors import safe_open
    from safetensors.torch import save_file

    parser = argparse.ArgumentParser()
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--hmnsfw", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--strength", type=float, default=0.5)
    parser.add_argument("--device", default="cuda:0")
    args = parser.parse_args()

    if args.strength != 0.5:
        raise RuntimeError("production HMNSFW strength is fixed at 0.5")
    if args.parent.is_symlink() or not (args.parent / "Ref2VA/transformer/model.safetensors.index.json").is_file():
        raise RuntimeError("sealed Ref2VA parent is missing or unsafe")
    if args.hmnsfw.is_symlink() or sha256_file(args.hmnsfw) != HMNSFW_SHA256:
        raise RuntimeError("HMNSFW source SHA-256 mismatch")
    if args.destination.exists():
        raise RuntimeError("refusing to overwrite an existing profile")

    index_path = args.parent / "Ref2VA/transformer/model.safetensors.index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))["weight_map"]
    with safe_open(args.hmnsfw, framework="pt", device="cpu") as source:
        keys = list(source.keys())
        pairs: dict[str, dict[str, str]] = {}
        for key in keys:
            target, side = lora_target(key)
            pairs.setdefault(target, {})[side] = key
        if len(keys) != 200 or len(pairs) != EXPECTED_TARGETS or any(set(value) != {"A", "B"} for value in pairs.values()):
            raise RuntimeError("HMNSFW target contract drifted")
        missing = sorted(set(pairs) - set(index))
        if missing:
            raise RuntimeError(f"HMNSFW targets are absent from Ref2VA: {missing[:5]}")

    staging = args.destination.with_name(f".{args.destination.name}.staging-{os.getpid()}")
    if staging.exists():
        raise RuntimeError("named staging already exists")
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "profile_id": PROFILE_ID,
        "parent": str(args.parent),
        "hmnsfw_sha256": HMNSFW_SHA256,
        "strength": args.strength,
        "builder_sha256": sha256_file(Path(__file__)),
    }
    try:
        shutil.copytree(args.parent, staging, copy_function=shutil.copy2)
        transformer = staging / "Ref2VA/transformer"
        by_shard: dict[str, list[str]] = {}
        for target in pairs:
            by_shard.setdefault(index[target], []).append(target)
        rows = []
        with safe_open(args.hmnsfw, framework="pt", device="cpu") as source:
            for shard_name in sorted(by_shard):
                shard_path = transformer / shard_name
                tensors = {}
                with safe_open(shard_path, framework="pt", device="cpu") as shard:
                    metadata = shard.metadata()
                    for key in shard.keys():
                        tensors[key] = shard.get_tensor(key)
                for target in sorted(by_shard[shard_name]):
                    pair = pairs[target]
                    weight = tensors[target].to(args.device)
                    lora_a = source.get_tensor(pair["A"]).to(args.device)
                    lora_b = source.get_tensor(pair["B"]).to(args.device)
                    if lora_a.shape[0] != 16 or lora_b.shape[1] != 16 or tuple(lora_b.shape[:1] + lora_a.shape[1:]) != tuple(weight.shape):
                        raise RuntimeError(f"HMNSFW shape/rank mismatch: {target}")
                    before = hashlib.sha256(weight.contiguous().view(torch.uint8).cpu().numpy().tobytes()).hexdigest()
                    merged = merge_weight(weight, lora_a, lora_b, args.strength)
                    after = hashlib.sha256(merged.contiguous().view(torch.uint8).cpu().numpy().tobytes()).hexdigest()
                    tensors[target] = merged.cpu()
                    rows.append({"target": target, "shard": shard_name, "before_sha256": before, "after_sha256": after})
                    del weight, lora_a, lora_b, merged
                temporary = shard_path.with_name(f".{shard_path.name}.tmp-{os.getpid()}")
                save_file(tensors, temporary, metadata=metadata)
                os.replace(temporary, shard_path)
                del tensors

        original_profile = json.loads((args.parent / "profile.json").read_text(encoding="utf-8"))
        profile = {
            **original_profile,
            "profile_id": PROFILE_ID,
            "modifier": {"kind": "lora", "sha256": HMNSFW_SHA256, "strength": 0.5, "target_count": len(rows)},
            "parent_profile_id": original_profile.get("profile_id"),
        }
        atomic_json(staging / "profile.json", profile)
        inventory = file_inventory(staging, excluded={"profile.json", "profile-lock.json"})
        tree_sha = hashlib.sha256(json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        profile["profile_tree_sha256"] = tree_sha
        atomic_json(staging / "profile.json", profile)
        evidence.update({"target_count": len(rows), "targets": rows, "files": inventory, "tree_sha256": tree_sha, "terminal": "PASS"})
        atomic_json(staging / "profile-lock.json", evidence)
        os.replace(staging, args.destination)
        atomic_json(args.evidence, evidence)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    print(json.dumps({"terminal": "PASS", "profile": PROFILE_ID, "tree_sha256": tree_sha}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
