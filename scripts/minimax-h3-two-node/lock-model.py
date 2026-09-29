#!/usr/bin/env python3
"""Generate and verify a source-anchored MiniMax H3 FL2VA model lock."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import tempfile
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/"
PARTIAL_SUFFIXES = (".part", ".aria2")


class LockError(RuntimeError):
    pass


def _safe_relative_path(value: str) -> str:
    if "\\" in value:
        raise LockError(f"unsafe path contains backslash: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise LockError(f"unsafe relative path: {value!r}")
    return path.as_posix()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_blob_sha1(path: Path, size: int) -> str:
    digest = hashlib.sha1()
    digest.update(f"blob {size}\0".encode())
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(payload).hexdigest()


def _write_canonical_json(path: Path, value: Any, *, replace: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not replace:
        raise LockError(f"refusing to overwrite existing lock: {path}")
    payload = (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def expected_paths(queue: Iterable[dict[str, Any]], repo: str, revision: str) -> list[str]:
    paths: list[str] = []
    for item in queue:
        if item.get("repo") != repo:
            continue
        if item.get("revision") != revision:
            raise LockError(f"queue revision drift for {item.get('path')!r}")
        paths.append(_safe_relative_path(str(item.get("path", ""))))
    if not paths:
        raise LockError(f"queue contains no entries for {repo}@{revision}")
    if len(paths) != len(set(paths)):
        raise LockError("queue contains duplicate model paths")
    return sorted(paths)


def inventory(root: Path, expected: set[str]) -> list[str]:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise LockError(f"model root is not a directory: {root}")
    observed: set[str] = set()
    for candidate in root.rglob("*"):
        relative = candidate.relative_to(root).as_posix()
        mode = candidate.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise LockError(f"symlink is forbidden in model tree: {relative}")
        if candidate.is_dir():
            continue
        if not stat.S_ISREG(mode):
            raise LockError(f"non-regular file is forbidden: {relative}")
        if relative.endswith(PARTIAL_SUFFIXES):
            raise LockError(f"partial artifact is forbidden: {relative}")
        if candidate.stat().st_size == 0:
            raise LockError(f"zero-size artifact is forbidden: {relative}")
        with candidate.open("rb") as stream:
            if stream.read(len(LFS_POINTER_PREFIX)).startswith(LFS_POINTER_PREFIX):
                raise LockError(f"Git LFS pointer is not model content: {relative}")
        observed.add(relative)
    missing = sorted(expected - observed)
    extra = sorted(observed - expected)
    if missing:
        raise LockError(f"missing model paths: {missing}")
    if extra:
        raise LockError(f"unexpected model paths: {extra}")
    return sorted(observed)


def resolve_source_metadata(source: str) -> dict[str, Any]:
    if source.startswith(("https://", "http://")):
        request = Request(source, headers={"Accept": "application/json", "User-Agent": "dgx-h3-lock-model/1"})
        try:
            with urlopen(request, timeout=30) as response:
                payload = response.read()
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise LockError(f"source metadata access hard-paused with HTTP {exc.code}") from exc
            raise LockError(f"source metadata HTTP failure: {exc.code}") from exc
        except URLError as exc:
            raise LockError(f"source metadata network failure: {exc.reason}") from exc
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise LockError("source metadata response is not JSON") from exc
    try:
        return json.loads(Path(source).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise LockError(f"cannot read source metadata: {source}") from exc


def _source_index(metadata: dict[str, Any], repo: str, revision: str) -> dict[str, dict[str, Any]]:
    if metadata.get("id") != repo and metadata.get("modelId") != repo:
        raise LockError("source metadata repository mismatch")
    if metadata.get("sha") != revision:
        raise LockError("source metadata revision mismatch")
    siblings = metadata.get("siblings")
    if not isinstance(siblings, list):
        raise LockError("source metadata has no siblings list")
    result: dict[str, dict[str, Any]] = {}
    for item in siblings:
        if not isinstance(item, dict) or "rfilename" not in item:
            continue
        path = _safe_relative_path(str(item["rfilename"]))
        if path in result:
            raise LockError(f"duplicate source metadata path: {path}")
        result[path] = item
    return result


def generate_lock(
    *,
    root: Path,
    queue: list[dict[str, Any]],
    metadata: dict[str, Any],
    output: Path,
    repo: str,
    revision: str,
    head_path: str,
    worker_path: str,
    license_evidence: Path,
    generated_at: str,
    replace: bool = False,
) -> dict[str, Any]:
    for node_path in (head_path, worker_path):
        if not Path(node_path).is_absolute():
            raise LockError(f"node model path must be absolute: {node_path}")
    if head_path != worker_path:
        raise LockError("head and worker model paths must be identical")
    if not license_evidence.is_file():
        raise LockError(f"license acknowledgement evidence is missing: {license_evidence}")

    paths = expected_paths(queue, repo, revision)
    inventory(root, set(paths))
    sources = _source_index(metadata, repo, revision)
    files: list[dict[str, Any]] = []
    for relative in paths:
        source = sources.get(relative)
        if source is None:
            raise LockError(f"source metadata missing path: {relative}")
        path = root / relative
        size = path.stat().st_size
        if source.get("size") != size:
            raise LockError(f"source identity size mismatch for {relative}")
        lfs = source.get("lfs")
        if isinstance(lfs, dict):
            expected_size = lfs.get("size")
            expected_digest = lfs.get("sha256")
            if expected_size != size or not isinstance(expected_digest, str) or len(expected_digest) != 64:
                raise LockError(f"incomplete LFS source identity for {relative}")
            deployment_digest = _sha256(path)
            if deployment_digest != expected_digest:
                raise LockError(f"source identity digest mismatch for {relative}")
            source_identity = {"kind": "lfs-sha256", "sha256": expected_digest, "size": size}
        else:
            expected_digest = source.get("blobId")
            if not isinstance(expected_digest, str) or len(expected_digest) != 40:
                raise LockError(f"incomplete Git blob source identity for {relative}")
            observed_git = _git_blob_sha1(path, size)
            if observed_git != expected_digest:
                raise LockError(f"source identity digest mismatch for {relative}")
            deployment_digest = _sha256(path)
            source_identity = {"kind": "git-blob-sha1", "sha1": expected_digest, "size": size}
        files.append(
            {
                "path": relative,
                "size": size,
                "sha256": deployment_digest,
                "source": source_identity,
            }
        )

    document: dict[str, Any] = {
        "schema_version": 1,
        "status": "resolved",
        "repo": repo,
        "revision": revision,
        "generated_at": generated_at,
        "node_paths": {"head": head_path, "worker": worker_path},
        "file_count": len(files),
        "total_bytes": sum(item["size"] for item in files),
        "license_evidence": {
            "path": str(license_evidence),
            "sha256": _sha256(license_evidence),
        },
        "source_metadata_sha256": _canonical_sha256(metadata),
        "files": files,
    }
    document["tree_sha256"] = _canonical_sha256(files)
    _write_canonical_json(output, document, replace=replace)
    return document


def verify_lock(lock_path: Path, root: Path, expected_node_path: str) -> dict[str, Any]:
    try:
        document = json.loads(lock_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise LockError(f"cannot read model lock: {lock_path}") from exc
    if document.get("status") != "resolved":
        raise LockError("model lock is not resolved")
    node_paths = document.get("node_paths", {})
    if expected_node_path not in (node_paths.get("head"), node_paths.get("worker")):
        raise LockError("absolute path does not match the locked node paths")
    files = document.get("files")
    if not isinstance(files, list) or document.get("file_count") != len(files):
        raise LockError("model lock file_count mismatch")
    expected = {_safe_relative_path(str(item.get("path", ""))) for item in files}
    if len(expected) != len(files):
        raise LockError("model lock contains duplicate paths")
    inventory(root, expected)
    mismatches: list[str] = []
    total = 0
    for item in files:
        relative = item["path"]
        path = root / relative
        size = path.stat().st_size
        total += size
        if size != item.get("size") or _sha256(path) != item.get("sha256"):
            mismatches.append(relative)
    if mismatches:
        raise LockError(f"model byte mismatch: {mismatches}")
    if total != document.get("total_bytes") or _canonical_sha256(files) != document.get("tree_sha256"):
        raise LockError("model lock aggregate mismatch")
    return {
        "passed": True,
        "verified_files": len(files),
        "total_bytes": total,
        "tree_sha256": document["tree_sha256"],
        "root": str(root),
    }


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise LockError(f"cannot read JSON: {path}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate")
    generate.add_argument("--root", type=Path, required=True)
    generate.add_argument("--queue", type=Path, required=True)
    generate.add_argument("--source-metadata", required=True)
    generate.add_argument("--output", type=Path, required=True)
    generate.add_argument("--repo", default="MiniMaxAI/MiniMax-H3")
    generate.add_argument("--revision", required=True)
    generate.add_argument("--head-path", required=True)
    generate.add_argument("--worker-path", required=True)
    generate.add_argument("--license-evidence", type=Path, required=True)
    generate.add_argument("--generated-at", required=True)
    generate.add_argument("--replace", action="store_true")
    verify = subparsers.add_parser("verify")
    verify.add_argument("--lock", type=Path, required=True)
    verify.add_argument("--root", type=Path, required=True)
    verify.add_argument("--expected-node-path", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            document = generate_lock(
                root=args.root,
                queue=_load_json(args.queue),
                metadata=resolve_source_metadata(args.source_metadata),
                output=args.output,
                repo=args.repo,
                revision=args.revision,
                head_path=args.head_path,
                worker_path=args.worker_path,
                license_evidence=args.license_evidence,
                generated_at=args.generated_at,
                replace=args.replace,
            )
            report = {"passed": True, "file_count": document["file_count"], "tree_sha256": document["tree_sha256"]}
        else:
            report = verify_lock(args.lock, args.root, args.expected_node_path)
    except LockError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
