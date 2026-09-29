#!/usr/bin/env python3
"""Build an immutable MiniMax H3 Ref2VA profile from verified local inputs."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import uuid


SHARED_COMPONENTS = ("audio_vae", "processor", "text_encoder", "tokenizer", "video_vae")
HEX64 = re.compile(r"[0-9a-f]{64}")


class ProfileError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def hash_rows(rows: list[tuple[str, int, str]]) -> str:
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_lock(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ProfileError("artifact lock is missing or unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileError("artifact lock is invalid") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ProfileError("artifact lock schema mismatch")
    files = value.get("files")
    if not isinstance(files, list) or len(files) != value.get("file_count"):
        raise ProfileError("artifact lock file count mismatch")
    return value


def ensure_safe_tree(root: Path, *, label: str) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ProfileError(f"{label} root is missing or unsafe")
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directories + files:
            candidate = current_path / name
            if candidate.is_symlink():
                raise ProfileError(f"{label} contains symlink: {candidate.relative_to(root)}")


def normalized_ref_path(raw: object) -> PurePosixPath:
    if not isinstance(raw, str) or "\\" in raw or "?" in raw or "\0" in raw:
        raise ProfileError("artifact lock contains unsafe path")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "Ref2VA":
        raise ProfileError("artifact lock contains unsafe path")
    return path


def verify_ref2va(root: Path, lock: dict) -> list[tuple[str, int, str]]:
    ensure_safe_tree(root, label="Ref2VA download")
    expected: dict[str, dict] = {}
    for entry in lock["files"]:
        if not isinstance(entry, dict):
            raise ProfileError("artifact lock file entry is invalid")
        relative = normalized_ref_path(entry.get("path")).as_posix()
        if relative in expected:
            raise ProfileError("artifact lock contains duplicate path")
        if not isinstance(entry.get("size"), int) or entry["size"] < 1:
            raise ProfileError("artifact lock contains invalid size")
        if not isinstance(entry.get("sha256"), str) or not HEX64.fullmatch(entry["sha256"]):
            raise ProfileError("artifact lock contains invalid sha256")
        expected[relative] = entry

    ref_root = root / "Ref2VA"
    actual = {
        path.relative_to(root).as_posix()
        for path in ref_root.rglob("*")
        if path.is_file()
    } if ref_root.is_dir() else set()
    missing = set(expected) - actual
    unexpected = actual - set(expected)
    if missing:
        raise ProfileError(f"missing locked Ref2VA file: {sorted(missing)[0]}")
    if unexpected:
        raise ProfileError(f"unexpected Ref2VA file: {sorted(unexpected)[0]}")

    rows: list[tuple[str, int, str]] = []
    for relative, entry in sorted(expected.items()):
        path = root / relative
        size = path.stat().st_size
        digest = sha256_file(path)
        if size != entry["size"] or digest != entry["sha256"]:
            raise ProfileError(f"locked Ref2VA file mismatch: {relative}")
        rows.append((relative, size, digest))
    if sum(row[1] for row in rows) != lock.get("total_size_bytes"):
        raise ProfileError("artifact lock total size mismatch")
    if hash_rows(rows) != lock.get("tree_sha256"):
        raise ProfileError("artifact lock tree mismatch")
    return rows


def copy_or_link(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError as exc:
        if exc.errno not in {errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOTSUP}:
            raise
        shutil.copy2(source, destination, follow_symlinks=False)
    if destination.is_symlink() or not destination.is_file():
        raise ProfileError("published profile member is unsafe")
    if destination.stat().st_size != source.stat().st_size or sha256_file(destination) != sha256_file(source):
        raise ProfileError("published profile member verification failed")


def shared_files(fl2va_root: Path) -> list[Path]:
    ensure_safe_tree(fl2va_root, label="FL2VA")
    members: list[Path] = []
    for component in SHARED_COMPONENTS:
        component_root = fl2va_root / component
        if not component_root.is_dir():
            raise ProfileError(f"missing shared FL2VA component: {component}")
        members.extend(sorted(path for path in component_root.rglob("*") if path.is_file()))
    return sorted(members, key=lambda path: path.relative_to(fl2va_root).as_posix())


def build_profile(
    *,
    fl2va_root: Path,
    ref2va_download_root: Path,
    artifact_lock: Path,
    destination: Path,
) -> dict:
    if destination.exists() or destination.is_symlink():
        raise ProfileError("destination already exists")
    if not destination.is_absolute():
        raise ProfileError("destination must be absolute")
    lock = load_lock(artifact_lock)
    shared = shared_files(fl2va_root)
    ref_rows = verify_ref2va(ref2va_download_root, lock)
    temporary = destination.parent / f".{destination.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    temporary.mkdir(mode=0o700, parents=False)
    try:
        shared_rows: list[tuple[str, int, str]] = []
        for source in shared:
            relative = source.relative_to(fl2va_root)
            target_relative = Path("Ref2VA") / relative
            copy_or_link(source, temporary / target_relative)
            shared_rows.append((target_relative.as_posix(), source.stat().st_size, sha256_file(source)))
        for relative, _, _ in ref_rows:
            copy_or_link(ref2va_download_root / relative, temporary / relative)

        combined_rows = sorted(shared_rows + ref_rows)
        profile = {
            "schema_version": 1,
            "profile_id": "heretic-ref2va-v1",
            "task_type": "ref2va",
            "layout": "flat-vllm-omni-0.26",
            "text_conditioning": "heretic-h3",
            "ref2va_transformer": "official",
            "fl2va_lora_applied": False,
            "shared_components": list(SHARED_COMPONENTS),
            "fl2va_source_root": str(fl2va_root.resolve()),
            "ref2va_repository": lock.get("repository"),
            "ref2va_revision": lock.get("revision"),
            "ref2va_artifact_tree_sha256": lock.get("tree_sha256"),
            "shared_tree_sha256": hash_rows(shared_rows),
            "ref2va_tree_sha256": hash_rows(ref_rows),
            "profile_tree_sha256": hash_rows(combined_rows),
            "file_count": len(combined_rows),
            "total_size_bytes": sum(row[1] for row in combined_rows),
        }
        profile_path = temporary / "profile.json"
        profile_path.write_text(
            json.dumps(profile, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.chmod(profile_path, 0o444)
        os.replace(temporary, destination)
        return profile
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--fl2va-root", type=Path, required=True)
    value.add_argument("--ref2va-download-root", type=Path, required=True)
    value.add_argument("--artifact-lock", type=Path, required=True)
    value.add_argument("--destination", type=Path, required=True)
    return value


def main() -> int:
    arguments = parser().parse_args()
    try:
        profile = build_profile(
            fl2va_root=arguments.fl2va_root,
            ref2va_download_root=arguments.ref2va_download_root,
            artifact_lock=arguments.artifact_lock,
            destination=arguments.destination,
        )
    except (OSError, ProfileError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "profile_id": profile["profile_id"],
        "profile_tree_sha256": profile["profile_tree_sha256"],
        "file_count": profile["file_count"],
        "total_size_bytes": profile["total_size_bytes"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
