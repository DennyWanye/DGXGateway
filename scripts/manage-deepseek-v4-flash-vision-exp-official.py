#!/usr/bin/env python3
"""Build, verify, seed, and no-replace-promote the official Vision snapshot."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile


OFFICIAL_REPO = "deepseek-ai/DeepSeek-V4-Flash-Vision-Exp"
OFFICIAL_REVISION = "6821d6ad3681a4b137b066b76094fa82ebd0a380"
REUSE_REPO = "drowzeys/keys-DeepSeekV4Flash-Vision-EXP-ablit"
REUSE_REVISION = "48095b3452a17f3e3ae8f77892399389c45de9e1"
SCHEMA = 1
EXPECTED_FILES = 84
EXPECTED_REUSE = 55


def fail(message: str) -> "None":
    raise SystemExit(message)


def metadata_url(repo: str, revision: str) -> str:
    return f"https://huggingface.co/api/models/{repo}/revision/{revision}?blobs=true"


def load_json(path: str | None, repo: str, revision: str) -> dict:
    if path:
        with open(path, "rb") as handle:
            return json.load(handle)
    response = subprocess.run(
        ["curl", "--fail", "--silent", "--show-error", "--location",
         "--user-agent", "dgx-spark-manifest/1", metadata_url(repo, revision)],
        check=True, capture_output=True,
    )
    return json.loads(response.stdout)


def sibling_record(sibling: dict) -> dict:
    path = sibling.get("rfilename")
    if not isinstance(path, str) or not path or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
        fail(f"unsafe or missing repository path: {path!r}")
    size = sibling.get("size")
    if not isinstance(size, int) or size < 0:
        fail(f"missing size for {path}")
    lfs = sibling.get("lfs")
    if lfs:
        digest = lfs.get("sha256")
        if lfs.get("size") != size or not isinstance(digest, str) or len(digest) != 64:
            fail(f"invalid LFS metadata for {path}")
        digest_type = "sha256"
    else:
        digest = sibling.get("blobId")
        if not isinstance(digest, str) or len(digest) != 40:
            fail(f"invalid Git blob metadata for {path}")
        digest_type = "gitsha1"
    return {"path": path, "size": size, "digest_type": digest_type, "digest": digest}


def records(metadata: dict, repo: str, revision: str) -> list[dict]:
    if metadata.get("id") != repo or metadata.get("sha") != revision:
        fail(f"metadata identity mismatch: expected {repo}@{revision}")
    siblings = metadata.get("siblings")
    if not isinstance(siblings, list):
        fail("metadata has no siblings array")
    result = sorted((sibling_record(item) for item in siblings), key=lambda item: item["path"])
    paths = [item["path"] for item in result]
    if len(paths) != len(set(paths)):
        fail("metadata contains duplicate paths")
    return result


def manifest_from_metadata(official: dict, reuse: dict) -> tuple[dict, list[dict]]:
    official_records = records(official, OFFICIAL_REPO, OFFICIAL_REVISION)
    reuse_records = records(reuse, REUSE_REPO, REUSE_REVISION)
    if len(official_records) != EXPECTED_FILES:
        fail(f"official file count changed: {len(official_records)} != {EXPECTED_FILES}")
    reuse_by_path = {item["path"]: item for item in reuse_records}
    reuse_paths = []
    for item in official_records:
        candidate = reuse_by_path.get(item["path"])
        identical = candidate is not None and all(candidate[key] == item[key] for key in ("size", "digest_type", "digest"))
        if identical:
            reuse_paths.append(item["path"])
    if len(reuse_paths) != EXPECTED_REUSE:
        fail(f"reusable object count changed: {len(reuse_paths)} != {EXPECTED_REUSE}")
    manifest = {
        "schema": SCHEMA,
        "repo": OFFICIAL_REPO,
        "revision": OFFICIAL_REVISION,
        "declared_license": "mit",
        "pipeline_tag": "image-text-to-text",
        "file_count": len(official_records),
        "total_bytes": sum(item["size"] for item in official_records),
        "reuse": {"repo": REUSE_REPO, "revision": REUSE_REVISION, "paths": reuse_paths},
        "files": official_records,
    }
    queue = [
        {"id": f"dsv4vo-{index:03d}", "repo": OFFICIAL_REPO, "revision": OFFICIAL_REVISION,
         "path": item["path"], "destination": item["path"]}
        for index, item in enumerate(official_records, 1)
    ]
    return manifest, queue


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=False) + "\n"


def load_manifest(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get("schema") != SCHEMA or manifest.get("repo") != OFFICIAL_REPO or manifest.get("revision") != OFFICIAL_REVISION:
        fail("manifest identity/schema mismatch")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != manifest.get("file_count") or len(files) != EXPECTED_FILES:
        fail("manifest file count mismatch")
    paths = []
    for item in files:
        path = item.get("path")
        if not isinstance(path, str) or not path or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
            fail(f"unsafe manifest path: {path!r}")
        if item.get("digest_type") not in ("sha256", "gitsha1"):
            fail(f"unsupported digest type for {path}")
        expected_length = 64 if item["digest_type"] == "sha256" else 40
        digest = item.get("digest")
        if not isinstance(digest, str) or len(digest) != expected_length:
            fail(f"invalid digest for {path}")
        if not isinstance(item.get("size"), int) or item["size"] < 0:
            fail(f"invalid size for {path}")
        paths.append(path)
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        fail("manifest paths must be sorted and unique")
    if sum(item["size"] for item in files) != manifest.get("total_bytes"):
        fail("manifest total_bytes mismatch")
    reuse = manifest.get("reuse")
    if not isinstance(reuse, dict) or reuse.get("repo") != REUSE_REPO or reuse.get("revision") != REUSE_REVISION:
        fail("manifest reuse provenance mismatch")
    reuse_paths = reuse.get("paths")
    if not isinstance(reuse_paths, list) or len(reuse_paths) != EXPECTED_REUSE or reuse_paths != sorted(set(reuse_paths)):
        fail("manifest reuse_count mismatch")
    if not set(reuse_paths).issubset(paths):
        fail("manifest reuse paths are outside the exact set")
    return manifest


def file_digest(path: Path, digest_type: str, size: int) -> str:
    digest = hashlib.sha256() if digest_type == "sha256" else hashlib.sha1()
    if digest_type == "gitsha1":
        digest.update(f"blob {size}\0".encode())
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_one(path: Path, item: dict) -> None:
    if path.is_symlink() or not path.is_file():
        fail(f"not a regular non-symlink file: {path}")
    stat = path.stat()
    if stat.st_size != item["size"]:
        fail(f"size mismatch: {item['path']}")
    if file_digest(path, item["digest_type"], item["size"]) != item["digest"]:
        fail(f"digest mismatch: {item['path']}")


def verify_tree(root: Path, manifest: dict) -> str:
    if not root.is_dir() or root.is_symlink():
        fail(f"snapshot root is not a directory: {root}")
    expected = {item["path"] for item in manifest["files"]}
    actual: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            fail(f"symlink forbidden: {relative}")
        if path.is_file():
            actual.add(relative)
        elif not path.is_dir():
            fail(f"special file forbidden: {relative}")
    missing, extra = sorted(expected - actual), sorted(actual - expected)
    if missing or extra:
        fail(f"exact-set mismatch missing={missing} extra={extra}")
    closure = hashlib.sha256()
    for item in manifest["files"]:
        verify_one(root / item["path"], item)
        closure.update(f"{item['path']}\0{item['size']}\0{item['digest_type']}\0{item['digest']}\n".encode())
    return closure.hexdigest()


def rename_noreplace(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        fail("renameat2 unavailable; refusing non-atomic promotion")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    result = renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def seed(manifest: dict, source_root: Path, staging_root: Path) -> int:
    staging_root.mkdir(parents=True, exist_ok=True)
    copied = 0
    reusable = set(manifest["reuse"]["paths"])
    for item in manifest["files"]:
        if item["path"] not in reusable:
            continue
        source = source_root / item["path"]
        verify_one(source, item)
        destination = staging_root / item["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            verify_one(destination, item)
            continue
        fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            if sys.platform.startswith("linux"):
                subprocess.run(["cp", "--reflink=auto", "--no-preserve=ownership", "--", source, temporary], check=True)
            else:
                shutil.copyfile(source, temporary)
            verify_one(temporary, item)
            try:
                rename_noreplace(temporary, destination)
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
                verify_one(destination, item)
            if source.stat().st_ino == destination.stat().st_ino and source.stat().st_dev == destination.stat().st_dev:
                fail(f"hardlink forbidden: {item['path']}")
            copied += 1
        finally:
            temporary.unlink(missing_ok=True)
    return copied


def fsync_tree(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    directories = [path for path in root.rglob("*") if path.is_dir()] + [root]
    for path in reversed(directories):
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--official-metadata")
    build.add_argument("--reuse-metadata")
    build.add_argument("--exact-output", required=True)
    build.add_argument("--queue-output")
    check = subparsers.add_parser("check-metadata")
    check.add_argument("--manifest", type=Path, required=True)
    check.add_argument("--queue", type=Path)
    check.add_argument("--official-metadata")
    check.add_argument("--reuse-metadata")
    verify = subparsers.add_parser("verify")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--root", type=Path, required=True)
    seed_parser = subparsers.add_parser("seed")
    seed_parser.add_argument("--manifest", type=Path, required=True)
    seed_parser.add_argument("--source-root", type=Path, required=True)
    seed_parser.add_argument("--staging-root", type=Path, required=True)
    promote = subparsers.add_parser("promote")
    promote.add_argument("--manifest", type=Path, required=True)
    promote.add_argument("--staging-root", type=Path, required=True)
    promote.add_argument("--final-root", type=Path, required=True)
    args = parser.parse_args()

    if args.command in ("build", "check-metadata"):
        official = load_json(args.official_metadata, OFFICIAL_REPO, OFFICIAL_REVISION)
        reuse = load_json(args.reuse_metadata, REUSE_REPO, REUSE_REVISION)
        generated_manifest, generated_queue = manifest_from_metadata(official, reuse)
        exact_text, queue_text = canonical_json(generated_manifest), canonical_json(generated_queue)
        if args.command == "build":
            Path(args.exact_output).write_text(exact_text, encoding="utf-8")
            if args.queue_output:
                Path(args.queue_output).write_text(queue_text, encoding="utf-8")
            return
        if args.manifest.read_text(encoding="utf-8") != exact_text:
            fail("committed manifests differ from pinned Hugging Face metadata")
        if args.queue and args.queue.read_text(encoding="utf-8") != queue_text:
            fail("download queue differs from pinned Hugging Face metadata")
        print(f"METADATA_PASS files={EXPECTED_FILES} reuse={EXPECTED_REUSE}")
        return

    manifest = load_manifest(args.manifest)
    if args.command == "verify":
        closure = verify_tree(args.root, manifest)
        print(json.dumps({"status": "VERIFY_PASS", "repo": OFFICIAL_REPO, "revision": OFFICIAL_REVISION,
                          "files": EXPECTED_FILES, "bytes": manifest["total_bytes"], "root_closure_sha256": closure}, sort_keys=True))
    elif args.command == "seed":
        copied = seed(manifest, args.source_root, args.staging_root)
        print(f"SEED_PASS copied={copied} reusable={EXPECTED_REUSE}")
    elif args.command == "promote":
        closure = verify_tree(args.staging_root, manifest)
        args.final_root.parent.mkdir(parents=True, exist_ok=True)
        if args.final_root.exists() or args.final_root.is_symlink():
            existing_closure = verify_tree(args.final_root, manifest)
            if existing_closure != closure:
                fail("existing final root has a different closure")
            print(f"PROMOTE_SKIPPED_VERIFIED_EXISTING closure={closure}")
            return
        fsync_tree(args.staging_root)
        try:
            rename_noreplace(args.staging_root, args.final_root)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
            existing_closure = verify_tree(args.final_root, manifest)
            if existing_closure != closure:
                fail("racing final root has a different closure")
            print(f"PROMOTE_SKIPPED_VERIFIED_EXISTING closure={closure}")
            return
        parent_fd = os.open(args.final_root.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        print(f"PROMOTE_PASS closure={closure} final={args.final_root}")


if __name__ == "__main__":
    main()
