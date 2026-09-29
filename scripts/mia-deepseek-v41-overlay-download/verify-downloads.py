#!/usr/bin/env python3
"""Offline verification for the pinned Mia DeepSeek V4.1 EXL3 model."""
import argparse
import os
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

REVISIONS = {'drowzeys/DeepSeek-V4.1-Flash-Abliterated-Cybersecurity-Unleashed': '87bb9850e6f49fd20ad9c8516b7817215cbbf5fc'}
TOTAL_BYTES = 682279829


def load_plan(path):
    plan = json.loads(Path(path).read_text())
    files = plan["files"]
    if len(files) != 2 or plan["total_bytes"] != TOTAL_BYTES:
        raise ValueError("unexpected pinned plan count or total")
    seen = set()
    for item in files:
        repo, revision = item["repo"], item["revision"]
        path, dest = item["path"], item["destination"]
        if REVISIONS.get(repo) != revision:
            raise ValueError("unapproved repo/revision")
        if (not re.fullmatch(r"[A-Za-z0-9_./-]+", path)
                or PurePosixPath(path).is_absolute()
                or ".." in PurePosixPath(path).parts
                or str(PurePosixPath(path)) != path
                or dest != repo.split("/")[1] + "/" + path
                or dest in seen):
            raise ValueError("invalid or duplicate destination")
        if not isinstance(item["size"], int) or item["size"] <= 0:
            raise ValueError("invalid expected size")
        if not re.fullmatch(r"[0-9a-f]{40}", item["git_blob_id"]):
            raise ValueError("invalid Git blob identity")
        sha = item["lfs_sha256"]
        if sha is not None and not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError("invalid LFS digest")
        if sha is None and item["size"] > 64 * 1024 * 1024:
            raise ValueError("ordinary Git blob exceeds small-file limit")
        seen.add(dest)
    if sum(f["size"] for f in files) != TOTAL_BYTES:
        raise ValueError("file sizes do not match total")
    return plan


def verify_file(path, size, digest):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in [path, *path.parents]) or not path.is_file() or path.stat().st_size != size:
        return False
    git = digest.startswith("gitsha1:")
    expected = digest.removeprefix("gitsha1:")
    h = hashlib.sha1() if git else hashlib.sha256()
    if git:
        h.update(f"blob {size}\0".encode())
    with path.open("rb") as src:
        for chunk in iter(lambda: src.read(8 * 1024 * 1024), b""):
            h.update(chunk)
            if hasattr(os, "posix_fadvise"):
                os.posix_fadvise(src.fileno(), src.tell()-len(chunk), len(chunk), os.POSIX_FADV_DONTNEED)
    return h.hexdigest() == expected


def file_digest(item):
    return item["lfs_sha256"] or "gitsha1:" + item["git_blob_id"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan")
    parser.add_argument("--root")
    parser.add_argument("--small-only", action="store_true")
    parser.add_argument("--file")
    parser.add_argument("--size", type=int)
    parser.add_argument("--digest")
    args = parser.parse_args()
    if args.file:
        if args.size is None or not args.digest:
            parser.error("--file requires --size and --digest")
        return 0 if verify_file(args.file, args.size, args.digest) else 1
    if not args.plan or not args.root:
        parser.error("provide --plan and --root")
    plan = load_plan(args.plan)
    files = [f for f in plan["files"] if not args.small_only or not f["lfs_sha256"]]
    passed = 0
    for item in files:
        ok = verify_file(Path(args.root) / item["destination"], item["size"], file_digest(item))
        print(("PASS" if ok else "FAIL") + " " + item["destination"], flush=True)
        passed += ok
    print(json.dumps({"verified": passed, "selected": len(files), "full_plan_files": 2,
                      "full_plan_bytes": TOTAL_BYTES, "small_only": args.small_only}))
    return 0 if passed == len(files) else 1


if __name__ == "__main__":
    raise SystemExit(main())
