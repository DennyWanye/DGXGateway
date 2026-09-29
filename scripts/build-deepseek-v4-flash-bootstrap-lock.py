#!/usr/bin/env python3
"""Build the deterministic, role-scoped DeepSeek remote input lock."""

import argparse
import hashlib
import json
from pathlib import Path
import stat
import subprocess


ORIGIN = "https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark.git"
COMMIT = "0107cef1835a56d1a2bcdabf7d9e1a085b70338b"
TREE = "a5258a8070b62fe77ed4bdb8887b39ef388e3a24"
SOURCE_ROOT = "83bea7e3d33dfac0e778ea1bd2bb885429a6e51d9cab34e6130342f7a7e8d2c8"

HEAD_FILES = (
    "configs/deepseek-v4-flash-dspark.env.example",
    "gateway/deepseek-v4-flash-nginx.conf",
    "gateway/deepseek-v4-flash-nginx-staging.conf",
    "manifests/deepseek-v4-flash-ablit-anchored-download.json",
    "manifests/deepseek-v4-flash-dspark.lock.json",
    "manifests/deepseek-v4-flash-official-anchor-inventory.json",
    "manifests/deepseek-v4-flash-official-metadata.json",
    "manifests/dgx-spark-fabric-qualification.lock.json",
    "patches/deepseek-v4-flash-dspark-local-runtime.patch",
    "plans/deepseek-v4-flash-dspark-2node/verification/20260829T1951/h3-lock-provenance-addendum-v1.json",
    "scripts/deepseek-v4-flash-nccl-probe.py",
    "scripts/prepare-deepseek-v4-flash-dspark.sh",
    "scripts/recover-deepseek-v4-flash.py",
    "scripts/run-deepseek-v4-flash-dspark.sh",
    "systemd/deepseek-v4-flash-dspark.service",
    "systemd/deepseek-v4-flash-fallback.service",
    "systemd/deepseek-v4-flash-fallback.timer",
    "systemd/deepseek-v4-flash-gateway-staging.service",
    "systemd/deepseek-v4-flash-gateway.service",
)

WORKER_FILES = frozenset((
    "configs/deepseek-v4-flash-dspark.env.example",
    "manifests/deepseek-v4-flash-dspark.lock.json",
    "scripts/deepseek-v4-flash-nccl-probe.py",
    "scripts/prepare-deepseek-v4-flash-dspark.sh",
    "scripts/run-deepseek-v4-flash-dspark.sh",
    "systemd/deepseek-v4-flash-dspark.service",
))


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def git(root, *arguments):
    result = subprocess.run(["git", "-C", str(root), *arguments], check=True,
                            text=True, stdout=subprocess.PIPE)
    return result.stdout


def source_identity(root):
    if (git(root, "rev-parse", "HEAD").strip() != COMMIT or
            git(root, "rev-parse", "HEAD^{tree}").strip() != TREE or
            git(root, "remote", "get-url", "origin").strip() != ORIGIN or
            git(root, "status", "--porcelain=v1")):
        raise SystemExit("pinned upstream checkout identity or clean-tree gate failed")
    symbolic = subprocess.run(["git", "-C", str(root), "symbolic-ref", "-q", "HEAD"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if symbolic.returncode == 0:
        raise SystemExit("pinned upstream checkout must be detached")
    rows = []
    for record in git(root, "ls-tree", "-r", "-z", "HEAD").split("\0"):
        if not record:
            continue
        metadata, relative = record.split("\t", 1)
        git_mode, kind, _ = metadata.split()
        path = root / relative
        if kind != "blob" or git_mode not in {"100644", "100755"} or path.is_symlink() or not path.is_file():
            raise SystemExit(f"unsafe upstream source row: {relative}")
        rows.append({"path": relative, "mode": "0755" if git_mode == "100755" else "0644",
                     "size": path.stat().st_size, "sha256": digest(path)})
    material = "".join(f"{row['sha256']}  {row['mode']}  {row['size']}  {row['path']}\n" for row in rows)
    total = sum(row["size"] for row in rows)
    if len(rows) != 171 or total != 4293967 or hashlib.sha256(material.encode()).hexdigest() != SOURCE_ROOT:
        raise SystemExit("upstream source count/bytes/root closure drift")
    return rows


def build(repo_root, upstream_root):
    files = []
    for relative in HEAD_FILES:
        path = repo_root / relative
        if path.is_symlink() or not path.is_file():
            raise SystemExit(f"bootstrap input is absent or unsafe: {relative}")
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode not in {0o644, 0o755}:
            raise SystemExit(f"bootstrap input mode is not 0644/0755: {relative}")
        files.append({"path": relative, "mode": f"{mode:04o}", "size": path.stat().st_size,
                      "sha256": digest(path),
                      "roles": ["head", "worker"] if relative in WORKER_FILES else ["head"]})
    source_files = source_identity(upstream_root)
    file_material = "".join(
        f"{row['sha256']}  {row['mode']}  {row['size']}  {','.join(row['roles'])}  {row['path']}\n"
        for row in files
    ).encode()
    return {
        "schema_version": 1,
        "kind": "deepseek-v4-flash-bootstrap-input-lock",
        "finalized": True,
        "input_root": "/opt/deepseek-v4-flash-dspark",
        "file_count": len(files),
        "total_bytes": sum(row["size"] for row in files),
        "root_sha256": hashlib.sha256(file_material).hexdigest(),
        "files": files,
        "upstream_source": {
            "schema_version": 1,
            "destination": "/home/operator/dgx-models/deepseek-v4-flash-dspark/source",
            "identity_path": ".deepseek-source-identity.json",
            "repository": ORIGIN,
            "commit": COMMIT,
            "git_tree": TREE,
            "variant": "upstream",
            "file_count": 171,
            "total_bytes": 4293967,
            "root_sha256": SOURCE_ROOT,
            "files": source_files,
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--upstream-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    encoded = json.dumps(build(args.repo_root.resolve(), args.upstream_root.resolve()),
                         sort_keys=True, indent=2) + "\n"
    args.output.write_text(encoded)


if __name__ == "__main__":
    main()
