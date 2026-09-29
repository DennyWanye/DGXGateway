#!/usr/bin/env python3
"""Replicate the verified official model from the head to the worker over fabric."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shlex
import subprocess
import sys


REVISION = "6821d6ad3681a4b137b066b76094fa82ebd0a380"
HEAD = "operator@192.0.2.30"
WORKER_MGMT = "operator@192.0.2.31"
WORKER_FABRIC = "operator@192.0.2.34"
HEAD_FABRIC_IP = "192.0.2.33"
WORKER_FABRIC_IP = "192.0.2.34"
IDENTITY = "/home/operator/.ssh/deepseek-v4-flash-worker"
KNOWN_HOSTS = "/home/operator/.ssh/deepseek-v4-flash-worker-known_hosts"
PARENT = "/home/operator/dgx-models/deepseek-v4-flash-vision-exp-official"
HEAD_FINAL = f"{PARENT}/{REVISION}"
WORKER_STAGING = f"{PARENT}/.staging-{REVISION}"
WORKER_FINAL = f"{PARENT}/{REVISION}"
REMOTE_CONTROL = "/home/operator/.local/libexec/dgx-deepseek-v4-flash-vision-exp-official"
REMOTE_MANAGER = f"{REMOTE_CONTROL}/manage-official-snapshot.py"
REMOTE_MANIFEST = f"{REMOTE_CONTROL}/exact.json"
SSH_BASE = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
FABRIC_SSH = [
    "/usr/bin/ssh", "-i", IDENTITY, "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=yes",
    "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
]


class ReplicationError(RuntimeError):
    pass


def run(command: list[str], *, data: bytes | None = None) -> str:
    result = subprocess.run(command, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise ReplicationError(
            f"command failed ({result.returncode}): {shlex.join(command)}\n"
            + result.stderr.decode(errors="replace")
        )
    return result.stdout.decode()


def remote(target: str, command: list[str], *, data: bytes | None = None) -> str:
    return run(SSH_BASE + [target, shlex.join(command)], data=data)


def worker_from_head(command: list[str], *, data: bytes | None = None) -> str:
    return remote(HEAD, FABRIC_SSH + [WORKER_FABRIC, shlex.join(command)], data=data)


def load_manifest(path: Path) -> tuple[dict, list[str]]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if (manifest.get("repo") != "deepseek-ai/DeepSeek-V4-Flash-Vision-Exp"
            or manifest.get("revision") != REVISION or manifest.get("file_count") != 84):
        raise ReplicationError("official exact-set manifest identity/count mismatch")
    paths = [item.get("path") for item in manifest.get("files", [])]
    if len(paths) != 84 or paths != sorted(set(paths)):
        raise ReplicationError("manifest paths are not an exact sorted unique set")
    for path in paths:
        parsed = PurePosixPath(path)
        if not path or parsed.is_absolute() or ".." in parsed.parts:
            raise ReplicationError(f"unsafe manifest path: {path!r}")
    return manifest, paths


def identity_gates() -> None:
    if remote(HEAD, ["hostname", "-s"]).strip() != "dgx-head":
        raise ReplicationError("head identity mismatch")
    if remote(HEAD, ["whoami"]).strip() != "operator":
        raise ReplicationError("head user mismatch")
    if remote(WORKER_MGMT, ["hostname", "-s"]).strip() != "dgx-worker":
        raise ReplicationError("worker identity mismatch")
    if remote(WORKER_MGMT, ["whoami"]).strip() != "operator":
        raise ReplicationError("worker user mismatch")
    trust_gate = (
        "set -euo pipefail; "
        f"test -f {shlex.quote(IDENTITY)} && test ! -L {shlex.quote(IDENTITY)}; "
        f"test -f {shlex.quote(KNOWN_HOSTS)} && test ! -L {shlex.quote(KNOWN_HOSTS)}; "
        f"test $(stat -c %a {shlex.quote(IDENTITY)}) = 600; "
        f"ssh-keygen -F {WORKER_FABRIC_IP} -f {shlex.quote(KNOWN_HOSTS)} >/dev/null; "
        f"ping -c 1 -W 2 {WORKER_FABRIC_IP} >/dev/null; "
        f"ip route get {WORKER_FABRIC_IP} | grep -F 'src {HEAD_FABRIC_IP}' >/dev/null"
    )
    remote(HEAD, ["bash", "-c", trust_gate])
    reverse_gate = (
        f"ping -c 1 -W 2 {HEAD_FABRIC_IP} >/dev/null && "
        f"ip route get {HEAD_FABRIC_IP} | grep -F 'src {WORKER_FABRIC_IP}' >/dev/null"
    )
    remote(WORKER_MGMT, ["bash", "-c", reverse_gate])
    identity = worker_from_head(["bash", "-c", "hostname -s; whoami"]).splitlines()
    if identity != ["dgx-worker", "operator"]:
        raise ReplicationError("dedicated fabric SSH identity mismatch")


def install_control(manager: Path, manifest: Path) -> None:
    remote(HEAD, ["install", "-d", "-m", "0755", REMOTE_CONTROL])
    remote(HEAD, ["install", "-m", "0755", "/dev/stdin", REMOTE_MANAGER], data=manager.read_bytes())
    remote(HEAD, ["install", "-m", "0644", "/dev/stdin", REMOTE_MANIFEST], data=manifest.read_bytes())
    worker_from_head(["install", "-d", "-m", "0755", REMOTE_CONTROL])
    worker_from_head(["install", "-m", "0755", "/dev/stdin", REMOTE_MANAGER], data=manager.read_bytes())
    worker_from_head(["install", "-m", "0644", "/dev/stdin", REMOTE_MANIFEST], data=manifest.read_bytes())


def parse_closure(output: str) -> str:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        closure = value.get("root_closure_sha256")
        if isinstance(closure, str) and len(closure) == 64:
            return closure
    raise ReplicationError("verification did not return a root closure")


def plan(paths: list[str]) -> dict:
    ssh_transport = shlex.join(FABRIC_SSH)
    rsync = [
        "/usr/bin/rsync", "--archive", "--partial", "--relative", "--from0",
        "--files-from=-", "--protect-args", "-e", ssh_transport,
        HEAD_FINAL + "/", WORKER_FABRIC + ":" + WORKER_STAGING + "/",
    ]
    payload = b"".join(path.encode() + b"\0" for path in paths)
    return {
        "source": HEAD_FINAL,
        "staging": WORKER_STAGING,
        "final": WORKER_FINAL,
        "paths": len(paths),
        "allowlist_sha256": hashlib.sha256(payload).hexdigest(),
        "rsync": rsync,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path(__file__).resolve().parents[2] / "manifests/deepseek-v4-flash-vision-exp-official-exact.json")
    parser.add_argument("--manager", type=Path, default=Path(__file__).resolve().parents[1] / "manage-deepseek-v4-flash-vision-exp-official.py")
    parser.add_argument("--emit-plan", action="store_true")
    args = parser.parse_args()
    _, paths = load_manifest(args.manifest)
    transfer = plan(paths)
    if args.emit_plan:
        print(json.dumps(transfer, sort_keys=True))
        return

    identity_gates()
    install_control(args.manager, args.manifest)
    head_output = remote(HEAD, ["python3", REMOTE_MANAGER, "verify", "--manifest", REMOTE_MANIFEST, "--root", HEAD_FINAL])
    head_closure = parse_closure(head_output)
    existing = worker_from_head([
        "bash", "-c",
        f"if test -e {shlex.quote(WORKER_FINAL)} || test -L {shlex.quote(WORKER_FINAL)}; then "
        f"python3 {shlex.quote(REMOTE_MANAGER)} verify --manifest {shlex.quote(REMOTE_MANIFEST)} "
        f"--root {shlex.quote(WORKER_FINAL)}; else echo FINAL_ABSENT; fi",
    ])
    if existing.strip() != "FINAL_ABSENT":
        worker_closure = parse_closure(existing)
        if worker_closure != head_closure:
            raise ReplicationError("existing worker final root differs from head closure")
        print(json.dumps({"status": "REPLICATION_SKIPPED_VERIFIED_EXISTING", "revision": REVISION,
                          "files": len(paths), "head_root_closure_sha256": head_closure,
                          "worker_root_closure_sha256": worker_closure}, sort_keys=True))
        return
    worker_from_head([
        "bash", "-c",
        f"set -euo pipefail; test ! -L {shlex.quote(WORKER_STAGING)}; "
        f"test ! -e {shlex.quote(WORKER_STAGING)} || test -d {shlex.quote(WORKER_STAGING)}; "
        f"install -d -m 0755 {shlex.quote(WORKER_STAGING)}",
    ])
    allowlist = b"".join(path.encode() + b"\0" for path in paths)
    remote(HEAD, transfer["rsync"], data=allowlist)
    worker_from_head(["python3", REMOTE_MANAGER, "promote", "--manifest", REMOTE_MANIFEST,
                      "--staging-root", WORKER_STAGING, "--final-root", WORKER_FINAL])
    worker_output = worker_from_head(["python3", REMOTE_MANAGER, "verify", "--manifest", REMOTE_MANIFEST, "--root", WORKER_FINAL])
    worker_closure = parse_closure(worker_output)
    if worker_closure != head_closure:
        raise ReplicationError("head and worker root closures differ")
    print(json.dumps({"status": "REPLICATION_PASS", "revision": REVISION, "files": len(paths),
                      "head_root_closure_sha256": head_closure,
                      "worker_root_closure_sha256": worker_closure}, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except ReplicationError as exc:
        print(f"REPLICATION_BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(1)
