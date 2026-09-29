#!/usr/bin/env python3
"""Download a pinned MiniMax H3 Ref2VA tree through the dedicated direct UID."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import time


HEX64 = re.compile(r"[0-9a-f]{64}")
REPOSITORY = "MiniMaxAI/MiniMax-H3"
DIRECT_USER = "dgx-hf-direct"


class DownloadError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise DownloadError("artifact manifest is missing or unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DownloadError("artifact manifest is invalid") from exc
    if value.get("schema_version") != 1 or value.get("repository") != REPOSITORY:
        raise DownloadError("artifact manifest identity mismatch")
    revision = value.get("revision")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise DownloadError("artifact manifest revision is invalid")
    files = value.get("files")
    if not isinstance(files, list) or len(files) != value.get("file_count") or not files:
        raise DownloadError("artifact manifest file count mismatch")
    seen: set[str] = set()
    total = 0
    for entry in files:
        if not isinstance(entry, dict):
            raise DownloadError("artifact manifest entry is invalid")
        raw = entry.get("path")
        if not isinstance(raw, str) or "\\" in raw or "\0" in raw:
            raise DownloadError("artifact manifest contains unsafe path")
        relative = PurePosixPath(raw)
        if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != ("Ref2VA",):
            raise DownloadError("artifact manifest contains unsafe path")
        if raw in seen:
            raise DownloadError("artifact manifest contains duplicate path")
        seen.add(raw)
        if not isinstance(entry.get("size"), int) or entry["size"] < 1:
            raise DownloadError("artifact manifest contains invalid size")
        if not isinstance(entry.get("sha256"), str) or not HEX64.fullmatch(entry["sha256"]):
            raise DownloadError("artifact manifest contains invalid sha256")
        total += entry["size"]
    if total != value.get("total_size_bytes"):
        raise DownloadError("artifact manifest total size mismatch")
    return value


def validate_destination(path: Path) -> Path:
    if not path.is_absolute():
        raise DownloadError("destination must be absolute")
    resolved = Path(os.path.normpath(str(path)))
    allowed = Path("/home/operator/dgx-models/video-generation/downloads")
    if resolved == allowed or allowed not in resolved.parents:
        raise DownloadError("destination is outside the Ref2VA download root")
    if path.is_symlink():
        raise DownloadError("destination cannot be a symlink")
    return resolved


def aria2_input(manifest: dict, destination: Path) -> str:
    lines: list[str] = []
    revision = manifest["revision"]
    for entry in manifest["files"]:
        relative = PurePosixPath(entry["path"])
        target_dir = destination.joinpath(*relative.parent.parts)
        url_path = "/".join(relative.parts)
        lines.extend([
            f"https://huggingface.co/{REPOSITORY}/resolve/{revision}/{url_path}",
            f"  dir={target_dir}",
            f"  out=.{relative.name}.part",
            f"  checksum=sha-256={entry['sha256']}",
        ])
    return "\n".join(lines) + "\n"


def already_verified(manifest: dict, destination: Path) -> set[str]:
    verified: set[str] = set()
    for entry in manifest["files"]:
        target = destination / entry["path"]
        if target.is_symlink():
            raise DownloadError(f"unsafe existing target: {entry['path']}")
        if target.is_file():
            if target.stat().st_size != entry["size"] or sha256_file(target) != entry["sha256"]:
                raise DownloadError(f"existing target mismatch: {entry['path']}")
            verified.add(entry["path"])
    return verified


def build_pending_input(manifest: dict, destination: Path, verified: set[str]) -> str:
    pending = dict(manifest)
    pending["files"] = [entry for entry in manifest["files"] if entry["path"] not in verified]
    return aria2_input(pending, destination) if pending["files"] else ""


def run_download(
    manifest_path: Path,
    destination_path: Path,
    egress_script: Path,
    signed_input: Path,
    hosts_file: Path,
    concurrency: int,
) -> dict:
    if os.geteuid() != 0:
        raise DownloadError("download must run as root so it can drop to the direct UID")
    if not egress_script.is_file() or egress_script.is_symlink():
        raise DownloadError("egress controller is missing or unsafe")
    if not signed_input.is_file() or signed_input.is_symlink():
        raise DownloadError("signed input is missing or unsafe")
    if not hosts_file.is_file() or hosts_file.is_symlink():
        raise DownloadError("isolated hosts file is missing or unsafe")
    manifest = load_manifest(manifest_path)
    destination = validate_destination(destination_path)
    direct = pwd.getpwnam(DIRECT_USER)
    destination.mkdir(parents=True, exist_ok=True)
    os.chown(destination, direct.pw_uid, direct.pw_gid)
    verified = already_verified(manifest, destination)
    pending = [entry for entry in manifest["files"] if entry["path"] not in verified]
    signed_text = signed_input.read_text(encoding="utf-8")
    if len([line for line in signed_text.splitlines() if line.startswith("https://")]) != len(pending):
        raise DownloadError("signed input count does not match pending artifact count")
    for entry in pending:
        relative = PurePosixPath(entry["path"])
        required = (
            f"  dir={destination.joinpath(*relative.parent.parts)}\n"
            f"  out=.{relative.name}.part\n"
            f"  checksum=sha-256={entry['sha256']}"
        )
        if required not in signed_text:
            raise DownloadError(f"signed input contract mismatch: {entry['path']}")
    started = time.time()
    if pending:
        for entry in manifest["files"]:
            parent = destination / PurePosixPath(entry["path"]).parent
            parent.mkdir(parents=True, exist_ok=True)
            os.chown(parent, direct.pw_uid, direct.pw_gid)
        subprocess.run([str(egress_script), "install"], check=True, stdout=subprocess.DEVNULL)
        try:
            subprocess.run([
                    "unshare", "--mount", "--propagation", "private", "/bin/bash", "-c",
                    'set -euo pipefail; mount --bind "$1" /etc/hosts; shift; exec "$@"',
                    "bash", str(hosts_file),
                    "runuser", "-u", DIRECT_USER, "--", "aria2c",
                    "--input-file", str(signed_input),
                    "--continue=true",
                    f"--max-concurrent-downloads={concurrency}",
                    "--split=8", "--max-connection-per-server=8", "--min-split-size=8M",
                    "--file-allocation=none", "--check-integrity=true",
                    "--auto-file-renaming=false", "--allow-overwrite=false",
                    "--connect-timeout=15", "--timeout=60", "--max-tries=0", "--retry-wait=5",
                    "--quiet=true",
                ], check=True)
        finally:
            subprocess.run([str(egress_script), "remove"], check=False, stdout=subprocess.DEVNULL)
    for entry in manifest["files"]:
        target = destination / entry["path"]
        if not target.exists():
            part = target.with_name(f".{target.name}.part")
            if not part.is_file() or part.is_symlink():
                raise DownloadError(f"downloaded part is missing or unsafe: {entry['path']}")
            if part.stat().st_size != entry["size"] or sha256_file(part) != entry["sha256"]:
                raise DownloadError(f"downloaded part mismatch: {entry['path']}")
            os.replace(part, target)
        if target.stat().st_size != entry["size"] or sha256_file(target) != entry["sha256"]:
            raise DownloadError(f"published target mismatch: {entry['path']}")
    for current, directories, files in os.walk(destination):
        os.chown(current, 1000, 1000)
        for name in directories + files:
            os.chown(Path(current) / name, 1000, 1000)
    return {
        "schema_version": 1,
        "repository": manifest["repository"],
        "revision": manifest["revision"],
        "tree_sha256": manifest["tree_sha256"],
        "file_count": manifest["file_count"],
        "total_size_bytes": manifest["total_size_bytes"],
        "destination": str(destination),
        "elapsed_seconds": round(time.time() - started, 3),
        "status": "PASS",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("validate", "plan", "run"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--egress-script", type=Path)
    parser.add_argument("--signed-input", type=Path)
    parser.add_argument("--hosts-file", type=Path)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--receipt", type=Path)
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    try:
        manifest = load_manifest(arguments.manifest)
        if arguments.command == "validate":
            print("PASS Ref2VA artifact manifest")
            return 0
        if arguments.destination is None:
            raise DownloadError("--destination is required")
        destination = validate_destination(arguments.destination)
        if arguments.command == "plan":
            print(aria2_input(manifest, destination), end="")
            return 0
        if any(value is None for value in (
            arguments.egress_script, arguments.signed_input, arguments.hosts_file, arguments.receipt
        )):
            raise DownloadError("run requires egress, signed input, isolated hosts, and receipt")
        if arguments.concurrency < 1 or arguments.concurrency > 4:
            raise DownloadError("concurrency must be between 1 and 4")
        result = run_download(
            arguments.manifest,
            destination,
            arguments.egress_script,
            arguments.signed_input,
            arguments.hosts_file,
            arguments.concurrency,
        )
        arguments.receipt.parent.mkdir(parents=True, exist_ok=True)
        temporary = arguments.receipt.with_name(f".{arguments.receipt.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, arguments.receipt)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (DownloadError, OSError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
