#!/usr/bin/env python3
"""Verify the complete digest-pinned ARM64 base image, without using a GPU."""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time

MANIFEST = "3b0e188ffceb3d07e09c3cb5215433a0020eacf02d7f882ed3a8bfd15454477e"
CONFIG = "d464f3b466fa9c45ddbff8a812e80564503b6879a9fd95c1a47514f3f0df5a4a"


def verify(root):
    started = time.time()
    assert root.is_dir() and not any(p.is_symlink() for p in (root, *root.parents))
    raw = (root / "manifest.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == MANIFEST, "manifest digest mismatch"
    manifest = json.loads(raw)
    assert manifest["config"]["digest"] == "sha256:" + CONFIG
    descriptors = {row["digest"]: row for row in [manifest["config"], *manifest["layers"]]}
    expected = {"manifest.json", "version", *(d.split(":")[1] for d in descriptors)}
    assert {p.name for p in root.iterdir()} == expected, "unexpected/missing OCI files"
    for digest, row in descriptors.items():
        path = root / digest.split(":")[1]
        info = path.lstat()
        assert stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size == row["size"]
        sha = hashlib.sha256()
        with path.open("rb") as stream:
            offset = 0
            while chunk := stream.read(8 * 1024 * 1024):
                sha.update(chunk)
                os.posix_fadvise(stream.fileno(), offset, len(chunk), os.POSIX_FADV_DONTNEED)
                offset += len(chunk)
        assert "sha256:" + sha.hexdigest() == digest, "blob digest mismatch: " + digest
    config = json.loads((root / CONFIG).read_bytes())
    assert (config["os"], config["architecture"]) == ("linux", "arm64")
    assert len(config["rootfs"]["diff_ids"]) == len(manifest["layers"])
    return {"passed": True, "manifest_digest": "sha256:" + MANIFEST,
            "image_id": "sha256:" + CONFIG, "os": config["os"], "architecture": config["architecture"],
            "layer_count": len(manifest["layers"]),
            "compressed_bytes": sum(row["size"] for row in manifest["layers"]),
            "rootfs_diff_ids": config["rootfs"]["diff_ids"],
            "entrypoint": config["config"].get("Entrypoint"),
            "elapsed_seconds": round(time.time() - started, 2)}


if __name__ == "__main__":
    print(json.dumps(verify(Path(sys.argv[1])), indent=2))
