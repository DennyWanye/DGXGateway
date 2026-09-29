#!/usr/bin/env python3
"""Build an immutable 10Eros FL2VA profile with Heretic shared components."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any


SOURCE_SHA256 = "bf34b4c9d2fa973ae84c480a1a5a04d2978958023bb6be7375b3b9e4818965e3"
PROFILE_ID = "10eros-fl2va-v1"
EXPECTED_TENSORS = 534


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
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


def inventory(root: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"symlink forbidden in profile: {path}")
        if path.is_file() and path.name not in {"profile.json", "profile-lock.json"}:
            rows.append({
                "path": path.relative_to(root).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            })
    return rows


def main() -> int:
    from safetensors import safe_open

    parser = argparse.ArgumentParser()
    parser.add_argument("--shared-parent", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--runtime-image-id", required=True)
    parser.add_argument("--runtime-patch-sha256", required=True)
    args = parser.parse_args()
    if args.shared_parent.is_symlink() or not (args.shared_parent / "text_encoder").is_dir():
        raise RuntimeError("shared FL2VA parent is missing or unsafe")
    if args.source.is_symlink() or sha256_file(args.source) != SOURCE_SHA256:
        raise RuntimeError("10Eros source SHA-256 mismatch")
    if args.destination.exists():
        raise RuntimeError("refusing to overwrite an existing profile")

    with safe_open(args.source, framework="pt", device="cpu") as source:
        keys = list(source.keys())
        shapes = {key: list(source.get_slice(key).get_shape()) for key in keys}
        dtypes = {key: str(source.get_slice(key).get_dtype()) for key in keys}
    required = {"adaln_t_table", "adaln_basis", "adaln_mean"}
    if len(keys) != EXPECTED_TENSORS or not required <= set(keys):
        raise RuntimeError("10Eros tensor/key contract drifted")
    if shapes["adaln_t_table"] != [1025, 8] or shapes["adaln_basis"] != [8, 2688] or shapes["adaln_mean"] != [2688]:
        raise RuntimeError("10Eros curve shape contract drifted")
    if any("time_embedder." in key for key in keys):
        raise RuntimeError("10Eros checkpoint unexpectedly contains dense time weights")
    projection_count = sum(
        key.endswith("adaln_proj.linear.weight") and shapes[key][1] == 8 for key in keys
    )
    if projection_count != 51:
        raise RuntimeError(f"10Eros curve projection contract drifted: {projection_count}")

    staging = args.destination.with_name(f".{args.destination.name}.staging-{os.getpid()}")
    if staging.exists():
        raise RuntimeError("named staging already exists")
    try:
        def ignore(_path: str, names: list[str]):
            return {"transformer", "profile-lock.json"} & set(names)

        shutil.copytree(args.shared_parent, staging, copy_function=shutil.copy2, ignore=ignore)
        transformer = staging / "transformer"
        transformer.mkdir()
        checkpoint_name = "model-00001-of-00001.safetensors"
        shutil.copy2(args.source, transformer / checkpoint_name)
        atomic_json(transformer / "model.safetensors.index.json", {
            "metadata": {"total_size": args.source.stat().st_size},
            "weight_map": {key: checkpoint_name for key in keys},
        })
        base_config = json.loads((args.shared_parent / "transformer/config.json").read_text(encoding="utf-8"))
        base_config.update({
            "adaln_curve_grid": 1025,
            "adaln_curve_dim": 8,
            "use_adaln_curve": True,
            "time_embed_dim": 8,
        })
        atomic_json(transformer / "config.json", base_config)
        files = inventory(staging)
        tree_sha = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        profile = {
            "schema_version": 1,
            "profile_id": PROFILE_ID,
            "task_type": "fl2va",
            "supported_tasks": ["t2va", "fl2va"],
            "text_conditioning": "heretic-h3",
            "transformer": {"kind": "10eros-beta4", "file_sha256": SOURCE_SHA256},
            "curve": {
                "table_shape": [1025, 8],
                "projection_count": projection_count,
                "runtime_patch_sha256": args.runtime_patch_sha256,
            },
            "runtime_image_id": args.runtime_image_id,
            "profile_tree_sha256": tree_sha,
        }
        atomic_json(staging / "profile.json", profile)
        evidence = {
            **profile,
            "tensor_count": len(keys),
            "dtypes": sorted(set(dtypes.values())),
            "files": files,
            "terminal": "PASS",
        }
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
