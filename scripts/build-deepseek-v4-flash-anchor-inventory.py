#!/usr/bin/env python3
"""Build the seven-anchor inventory from fixed anonymous metadata and local files.

The command is read-only and writes JSON to stdout so a Mac controller can
capture it without creating an unreceipted file on either DGX node.
"""

import argparse
import hashlib
import json
import stat
import struct
from pathlib import Path

REPOSITORY = "drowzeys/keys-DeepSeekV4-Flash-GA-0731-Dspark-Abliterated-Anchored-Tensors"
REVISION = "a1e69379517383be9cd78c67defb04e77ad6aa68"
ANCHORS = [
    "model-00001-of-00048.safetensors", "model-00008-of-00048.safetensors",
    "model-00016-of-00048.safetensors", "model-00024-of-00048.safetensors",
    "model-00032-of-00048.safetensors", "model-00040-of-00048.safetensors",
    "model-00048-of-00048.safetensors",
]
SHARDS = [f"model-{index:05d}-of-00048.safetensors" for index in range(1, 49)]
MAX_HEADER_BYTES = 256 * 1024 * 1024


def fail(message):
    raise SystemExit(message)


def parse_header(target):
    observed = target.lstat()
    if not stat.S_ISREG(observed.st_mode) or target.is_symlink():
        fail(f"anchor is not a regular non-symlink file: {target.name}")
    with target.open("rb") as stream:
        prefix = stream.read(8)
        if len(prefix) != 8:
            fail(f"truncated safetensors prefix: {target.name}")
        header_size = struct.unpack("<Q", prefix)[0]
        if not 0 < header_size <= MAX_HEADER_BYTES or header_size > observed.st_size - 8:
            fail(f"invalid safetensors header size: {target.name}")
        raw = stream.read(header_size)
        if len(raw) != header_size:
            fail(f"truncated safetensors header: {target.name}")
    try:
        header = json.loads(raw.decode("utf-8").rstrip())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"invalid safetensors header JSON: {target.name}: {exc}")
    if not isinstance(header, dict):
        fail(f"safetensors header is not an object: {target.name}")
    data_bytes = observed.st_size - 8 - header_size
    for name, tensor in header.items():
        if name == "__metadata__":
            if not isinstance(tensor, dict):
                fail(f"invalid __metadata__: {target.name}")
            continue
        if not isinstance(tensor, dict) or set(tensor) != {"dtype", "shape", "data_offsets"}:
            fail(f"malformed tensor entry: {target.name}:{name}")
        offsets = tensor["data_offsets"]
        if (not isinstance(tensor["dtype"], str) or not tensor["dtype"] or
                not isinstance(tensor["shape"], list) or
                not all(isinstance(value, int) and value >= 0 for value in tensor["shape"]) or
                not isinstance(offsets, list) or len(offsets) != 2 or
                not all(isinstance(value, int) and value >= 0 for value in offsets) or
                offsets[0] > offsets[1] or offsets[1] > data_bytes):
            fail(f"invalid tensor bounds: {target.name}:{name}")
    return header


def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("metadata", type=Path)
    parser.add_argument("model_root", type=Path)
    parser.add_argument("--observed-at", required=True)
    args = parser.parse_args()
    raw = args.metadata.read_bytes()
    metadata = json.loads(raw)
    if not isinstance(metadata, dict):
        fail("metadata is not an object")
    for field in ("id", "modelId"):
        if metadata.get(field) != REPOSITORY:
            fail(f"metadata {field} is absent or differs from the fixed repository")
    if metadata.get("sha") != REVISION:
        fail("metadata revision differs from the fixed revision")
    siblings = metadata.get("siblings")
    if not isinstance(siblings, list):
        fail("metadata siblings is not an array")
    shard_rows = [row for row in siblings if isinstance(row, dict) and row.get("rfilename") in SHARDS]
    if len(shard_rows) != len(SHARDS) or {row.get("rfilename") for row in shard_rows} != set(SHARDS):
        fail("metadata must contain each of the 48 fixed shards exactly once")
    official = {}
    for row in shard_rows:
        lfs = row.get("lfs")
        if (not isinstance(lfs, dict) or not isinstance(lfs.get("size"), int) or lfs["size"] <= 8 or
                not isinstance(lfs.get("sha256"), str) or len(lfs["sha256"]) != 64 or
                any(character not in "0123456789abcdef" for character in lfs["sha256"])):
            fail(f"invalid LFS identity: {row.get('rfilename')}")
        official[row["rfilename"]] = lfs
    rows = []
    for relative in ANCHORS:
        target = args.model_root / relative
        header = parse_header(target)
        expected = official[relative]
        digest = digest_file(target)
        if target.stat().st_size != expected["size"] or digest != expected["sha256"]:
            fail(f"official/local anchor mismatch: {relative}")
        rows.append({"path": relative, "size": expected["size"], "sha256": digest, "tensors": header})
    value = {
        "schema_version": 1, "builder_version": 2, "anchor_paths": ANCHORS,
        "repository": REPOSITORY, "revision": REVISION,
        "observed_at": args.observed_at,
        "metadata_source": {
            "url": f"https://huggingface.co/api/models/{REPOSITORY}/revision/{REVISION}?blobs=true",
            "response_body_sha256": hashlib.sha256(raw).hexdigest(), "authenticated": False,
        },
        "official_shards": [{"path": path, "size": official[path]["size"],
                             "sha256": official[path]["sha256"]} for path in SHARDS],
        "files": rows,
    }
    print(json.dumps(value, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
