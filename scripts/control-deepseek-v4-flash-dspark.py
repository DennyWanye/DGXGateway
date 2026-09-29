#!/usr/bin/env python3
"""Single fail-closed Mac controller for the two-node DeepSeek deployment.

All cluster mutations pass through the canonical shared lease and a one-use,
exactly-bound receipt.  This file deliberately uses only the Python standard
library so it can remain the one auditable authority on the Mac.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import re
import shlex
import signal
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable


LOCK_ROOT = Path("/Users/operator/.codex/locks/dgx-spark-two-node")
LOCK_HISTORY = Path("/Users/operator/.codex/locks/dgx-spark-two-node-history")
OWNER = "deepseek-v4-flash-two-node"
PROJECT = "deepseek-v4-flash-dspark"
REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_REPO_ROOT = Path("/Users/operator/projects/DGXSpark")
LOCK_SCRIPT = REPO_ROOT / "scripts" / "dgx-spark-deployment-lock.py"
DEFAULT_TOKEN_FILE = LOCK_ROOT.parent / f"{LOCK_ROOT.name}.{OWNER}.token"
DEFAULT_JOURNAL = Path("/Users/operator/.codex/state/deepseek-v4-flash-dspark/journal.jsonl")
DEFAULT_GATEWAY_SECRET = Path("/Users/operator/.config/deepseek-v4-flash/gateway.token")
DEFAULT_BOOTSTRAP_INPUT_LOCK = REPO_ROOT / "manifests/deepseek-v4-flash-bootstrap-input.lock.json"
BOOTSTRAP_INPUT_FILE_COUNT = 19
BOOTSTRAP_INPUT_TOTAL_BYTES = 2576363
BOOTSTRAP_INPUT_ROOT_SHA256 = "620ffc89d6a1b276efd375ad4a9f6415a9adc54c918a08f1a46a9264157d2467"
BOOTSTRAP_INPUT_CANONICAL_SHA256 = "7752afcd53bf35d205e03b2202c06cc6b6884f5e2a3e3a89b88e7feab50276a9"
BOOTSTRAP_INPUT_RAW_SHA256 = "28e94a5dfcb482a7c3d64d001c6fd2042eaa7de7f343ed6cd1f02633d0f070e5"
PINNED_SOURCE_CHECKOUT = Path("/Users/operator/.cache/dgx-spark/deepseek-v4-flash/upstream-0107cef1835a56d1a2bcdabf7d9e1a085b70338b")
PINNED_SOURCE_ORIGIN = "https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark.git"
PINNED_SOURCE_COMMIT = "0107cef1835a56d1a2bcdabf7d9e1a085b70338b"
PINNED_SOURCE_TREE = "a5258a8070b62fe77ed4bdb8887b39ef388e3a24"
HARNESS = REPO_ROOT / "scripts" / "test-deepseek-v4-flash-dspark.py"
CONSUMER_MANIFEST = REPO_ROOT / "manifests" / "deepseek-v4-flash-consumers.json"
FABRIC_LOCK = REPO_ROOT / "manifests" / "dgx-spark-fabric-qualification.lock.json"
FABRIC_EVIDENCE = CANONICAL_REPO_ROOT / "plans/minimax-h3-two-node/spikes/fabric-network-transaction/nccl-evidence-post-upgrade-20260830T112621Z"
FABRIC_PROVENANCE = CANONICAL_REPO_ROOT / "plans/deepseek-v4-flash-dspark-2node/verification/20260829T1951/h3-lock-provenance-addendum-v1.json"
PREPARE_SCRIPT = REPO_ROOT / "scripts" / "prepare-deepseek-v4-flash-dspark.sh"
FABRIC_INTERFACES = ("enp1s0f0np0", "enP2p1s0f0np0")
FABRIC_HCAS = ("rocep1s0f0", "roceP2p1s0f0")
FABRIC_EXPECTED = {
    "head": {
        "cidrs": ("192.0.2.33/30", "192.0.2.35/30"),
        "peers": ("192.0.2.34", "192.0.2.36"),
        "netplan_sha256": "1d88c29ab62a371f34ab71a9af4f94a3b74bdd6b580264bcf49475d0f9c273a7",
    },
    "worker": {
        "cidrs": ("192.0.2.34/30", "192.0.2.36/30"),
        "peers": ("192.0.2.33", "192.0.2.35"),
        "netplan_sha256": "07f6825f1fc51d3e29744f322d135b00bf13a0daa0497801eebaf6844e7ff6ad",
    },
}
REMOTE_HELPER = "/usr/local/libexec/run-deepseek-v4-flash-dspark.sh"
REMOTE_RECOVERY = "/usr/local/libexec/recover-deepseek-v4-flash"
REMOTE_ENV = "/etc/deepseek-v4-flash-dspark.env"
REMOTE_COMPOSE = "/opt/deepseek-v4-flash-dspark/docker-compose.dspark.yml"
REMOTE_COMPOSE_SERVICE = "vllm-dspark"
FALLBACK_LIFECYCLE_LOCK = "/run/lock/deepseek-v4-flash-fallback.lifecycle.lock"
REMOTE_PREPARE = "/usr/local/libexec/prepare-deepseek-v4-flash-dspark.sh"
REMOTE_MODEL_ROOT = "/home/operator/dgx-models/deepseek-v4-flash-ablit-anchored"
REMOTE_ARTIFACT_STATE = "/home/operator/.local/state/deepseek-v4-flash-dspark/artifacts"
REMOTE_SOURCE = "/home/operator/dgx-models/deepseek-v4-flash-dspark/source"
REMOTE_RUNTIME_ARCHIVE = "/home/operator/dgx-models/deepseek-v4-flash-dspark/artifacts/dspark-vllm-gx10-a83948492cf13df4.tar"
REMOTE_INPUT_ROOT = "/opt/deepseek-v4-flash-dspark"
ARTIFACT_VERBS = {
    "finalize-model-lock": ("head", 7200),
    "copy-model": ("head", 7200),
    "acquire-images": ("head", 7200),
    "save-runtime-archive": ("head", 3600),
    "copy-runtime-archive": ("head", 3600),
    "load-runtime-archive": ("worker", 3600),
    "apply-runtime-patch": ("head", 600),
    "install-integration-artifacts": ("head", 600),
    "finalize-deployment-lock": ("head", 600),
    "copy-patched-source": ("head", 1800),
    "record-license-acceptance": ("head", 600),
}
DUAL_ROLE_ARTIFACT_VERBS = {"record-node-identity": 600}
INSTALL_SERVICE_VERB = "install-service-assets"
QUALIFY_VERB = "qualify-fabric-consumer"
TRUST_VERBS = {
    "ensure-head-worker-key": "head",
    "install-worker-authorized-key": "worker",
    "install-head-worker-known-hosts": "head",
}
REMOTE_WORKER_KEY = "/home/operator/.ssh/deepseek-v4-flash-worker"
REMOTE_WORKER_KNOWN_HOSTS = "/home/operator/.ssh/deepseek-v4-flash-worker-known_hosts"
REMOTE_FABRIC_RECEIPT = f"{REMOTE_ARTIFACT_STATE}/fabric-consumer.receipt.json"
BOOTSTRAP_SOURCES = {
    "/usr/local/libexec/run-deepseek-v4-flash-dspark.sh": REPO_ROOT / "scripts/run-deepseek-v4-flash-dspark.sh",
    "/usr/local/libexec/prepare-deepseek-v4-flash-dspark.sh": REPO_ROOT / "scripts/prepare-deepseek-v4-flash-dspark.sh",
}
RECEIPT_TTL_SECONDS = 300
CUTOVER_STATES = (
    "PREPARED",
    "FALLBACK_ARMED",
    "OLD_STOPPED",
    "NEW_BOUND",
    "NEW_SMOKE_PASSED",
    "CLIENTS_STAGED",
    "PRODUCTION_MONITOR_PASSED",
    "WORKER_REBOOT_PASSED",
    "HEAD_REBOOT_PASSED",
    "CLIENTS_VERIFIED",
    "COMMITTING",
    "FALLBACK_DISARMED",
    "VERIFIED",
)


class Host:
    def __init__(self, role: str, hostname: str, address: str):
        self.role = role
        self.hostname = hostname
        self.address = address


HOSTS = {
    "head": Host("head", "dgx-head", "192.0.2.30"),
    "worker": Host("worker", "dgx-worker", "192.0.2.31"),
}


class ControllerError(RuntimeError):
    pass


class LockUnavailable(ControllerError):
    pass


class LeaseLost(ControllerError):
    pass


class TargetMismatch(ControllerError):
    pass


class ReceiptRejected(ControllerError):
    pass


class PlanGateError(ControllerError):
    pass


class OperationTimeout(ControllerError):
    pass


FABRIC_PROBE = r'''
import getpass, hashlib, json, pathlib, socket, subprocess, sys
role = sys.argv[1]
interfaces = ("enp1s0f0np0", "enP2p1s0f0np0")
peers = {"head": ("192.0.2.34", "192.0.2.36"), "worker": ("192.0.2.33", "192.0.2.35")}[role]
def run(argv, check=True):
    value = subprocess.run(argv, text=True, capture_output=True, timeout=30)
    if check and value.returncode:
        raise SystemExit(f"probe failed: {argv[0]}: {value.stderr.strip()}")
    return value
def sha(path):
    value = run(["sudo", "-n", "sha256sum", "--", path]).stdout.split()
    if not value or len(value[0]) != 64 or any(ch not in "0123456789abcdef" for ch in value[0]):
        raise SystemExit(f"probe failed: invalid SHA-256 output for {path}")
    return value[0]
def colon_map(text):
    return {key.strip(): value.strip() for key, separator, value in (line.partition(":") for line in text.splitlines()) if separator}
def release_value(path, key):
    values = {}
    for line in pathlib.Path(path).read_text().splitlines():
        name, separator, value = line.partition("=")
        if separator:
            values[name.strip()] = value.strip().strip('"')
    if not values.get(key):
        raise SystemExit(f"probe failed: {path} lacks {key}")
    return values[key]
links = {}
for interface in interfaces:
    link = json.loads(run(["ip", "-j", "link", "show", "dev", interface]).stdout)[0]
    addresses = json.loads(run(["ip", "-j", "address", "show", "dev", interface]).stdout)[0]
    driver = colon_map(run(["ethtool", "-i", interface]).stdout)
    links[interface] = {
        "mtu": link["mtu"], "operstate": link["operstate"],
        "mac": link.get("address"), "bus_info": driver.get("bus-info"),
        "firmware": driver.get("firmware-version"),
        "speed": pathlib.Path(f"/sys/class/net/{interface}/speed").read_text().strip(),
        "cidrs": sorted(f"{item['local']}/{item['prefixlen']}" for item in addresses.get("addr_info", []) if item.get("family") == "inet"),
    }
pings = {}
routes = {}
for peer in peers:
    pings[peer] = run(["ping", "-M", "do", "-s", "8972", "-c", "2", "-W", "2", peer], check=False).returncode
    routes[peer] = json.loads(run(["ip", "-j", "route", "get", peer]).stdout)[0]
head_to_worker = None
if role == "head":
    value = run(["ssh", "-i", "/home/operator/.ssh/deepseek-v4-flash-worker", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=/home/operator/.ssh/deepseek-v4-flash-worker-known_hosts", "operator@192.0.2.34", "hostname; whoami"], check=False)
    head_to_worker = {"returncode": value.returncode, "stdout": value.stdout.splitlines()}
print(json.dumps({
    "hostname": socket.gethostname(), "user": getpass.getuser(),
    "boot_id": pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
    "kernel": run(["uname", "-r"]).stdout.strip(),
    "dgx_ota": release_value("/etc/dgx-release", "DGX_OTA_VERSION"),
    "driver": run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"]).stdout.splitlines()[0].strip(),
    "netplan_sha256": sha("/etc/netplan/80-minimax-h3-fabric.yaml"),
    "links": links,
    "default_routes": json.loads(run(["ip", "-j", "route", "show", "default"]).stdout or "[]"),
    "ibdev2netdev": run(["ibdev2netdev"]).stdout,
    "show_gids": run(["show_gids"]).stdout,
    "hca_firmware": {hca: colon_map(run(["ibv_devinfo", "-d", hca]).stdout).get("fw_ver") for hca in ("rocep1s0f0", "roceP2p1s0f0")},
    "pings": pings, "routes": routes,
    "listeners": run(["ss", "-H", "-ltn"]).stdout.splitlines(),
    "head_to_worker": head_to_worker,
}, sort_keys=True))
'''

BOOTSTRAP_INSPECT = r'''
import hashlib, json, os, pathlib, stat, sys
paths = sys.argv[1:]
result = {}
for raw in paths:
    path = pathlib.Path(raw)
    if path.is_symlink():
        result[raw] = {"exists": True, "symlink": True}
    elif path.is_file():
        info = path.stat()
        result[raw] = {"exists": True, "symlink": False,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mode": format(stat.S_IMODE(info.st_mode), "04o"),
            "uid": info.st_uid, "gid": info.st_gid}
    else:
        result[raw] = {"exists": path.exists(), "symlink": False}
print(json.dumps(result, sort_keys=True))
'''

BOOTSTRAP_REMOTE = r'''
import base64, hashlib, json, os, pathlib, signal, socket, stat, sys, time
expected_host, helper_sha, prepare_sha = sys.argv[1:]
destinations = (
    "/usr/local/libexec/run-deepseek-v4-flash-dspark.sh",
    "/usr/local/libexec/prepare-deepseek-v4-flash-dspark.sh",
)
expected_hashes = dict(zip(destinations, (helper_sha, prepare_sha)))
if socket.gethostname() != expected_host or os.environ.get("SUDO_USER") != "operator" or os.geteuid() != 0:
    raise SystemExit("bootstrap identity mismatch")
parent = pathlib.Path("/usr/local/libexec")
for ancestor in (pathlib.Path("/usr"), pathlib.Path("/usr/local"), parent):
    info = os.lstat(ancestor)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_gid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
        raise SystemExit("fixed bootstrap ancestor is absent or unsafe")
payload = json.load(sys.stdin)
if payload.get("schema") != "deepseek-bootstrap-v1" or [row.get("destination") for row in payload.get("files", [])] != list(destinations):
    raise SystemExit("bootstrap payload scope mismatch")
contents = {}
for row in payload["files"]:
    destination = row["destination"]
    data = base64.b64decode(row.get("content_base64", ""), validate=True)
    if row.get("sha256") != expected_hashes[destination] or hashlib.sha256(data).hexdigest() != expected_hashes[destination]:
        raise SystemExit("bootstrap source digest mismatch")
    contents[destination] = data
originals = {}
for raw in destinations:
    path = pathlib.Path(raw)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise SystemExit("bootstrap destination is unsafe")
    if path.exists():
        info = path.stat()
        if info.st_uid != 0:
            raise SystemExit("bootstrap preimage is not root-owned")
        originals[raw] = (path.read_bytes(), stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid)
    else:
        originals[raw] = None
restoring = False
def atomic_write(path, data, mode, uid=0, gid=0):
    temporary = path.with_name(f".{path.name}.bootstrap.{os.getpid()}.{time.time_ns()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        os.write(fd, data); os.fsync(fd); os.fchmod(fd, mode); os.fchown(fd, uid, gid)
    finally:
        os.close(fd)
    os.replace(temporary, path)
def restore():
    global restoring
    if restoring:
        return
    restoring = True
    for raw in reversed(destinations):
        path = pathlib.Path(raw); original = originals[raw]
        if original is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(path, original[0], original[1], original[2], original[3])
def interrupted(signum, frame):
    restore(); raise SystemExit(128 + signum)
signal.signal(signal.SIGTERM, interrupted)
signal.signal(signal.SIGHUP, interrupted)
try:
    for raw in destinations:
        atomic_write(pathlib.Path(raw), contents[raw], 0o755)
    directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(directory_fd)
    finally: os.close(directory_fd)
    before = {}
    after = {}
    for raw in destinations:
        original = originals[raw]
        before[raw] = None if original is None else {"sha256": hashlib.sha256(original[0]).hexdigest(), "mode": format(original[1], "04o"), "uid": original[2], "gid": original[3]}
        path = pathlib.Path(raw); info = os.lstat(path)
        after[raw] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "mode": format(stat.S_IMODE(info.st_mode), "04o"), "uid": info.st_uid, "gid": info.st_gid}
        if not stat.S_ISREG(info.st_mode) or after[raw] != {"sha256":expected_hashes[raw],"mode":"0755","uid":0,"gid":0}:
            raise SystemExit("bootstrap postimage verification failed")
except BaseException:
    restore(); raise
print(json.dumps({"before": before, "after": after}, sort_keys=True))
'''

INPUT_BUNDLE_REMOTE = r'''
import base64, hashlib, json, os, pathlib, socket, stat, sys, time
expected_host, role, manifest_sha = sys.argv[1:]
if socket.gethostname() != expected_host or os.environ.get("SUDO_USER") != "operator" or os.geteuid() != 0:
    raise SystemExit("input bootstrap identity mismatch")
payload = json.load(sys.stdin)
manifest = payload.get("manifest")
canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
if hashlib.sha256(canonical).hexdigest() != manifest_sha or payload.get("role") != role:
    raise SystemExit("input bootstrap manifest binding mismatch")
input_root = pathlib.Path(manifest.get("input_root", ""))
allowed = []
for item in manifest.get("files", []):
    if role in item.get("roles", []):
        allowed.append({"source_path":item["path"], "destination":str(input_root / item["path"]), "mode":item["mode"], "size":item["size"], "sha256":item["sha256"]})
rows = payload.get("files")
if not isinstance(allowed, list) or not isinstance(rows, list) or len(rows) != len(allowed):
    raise SystemExit("input bootstrap role allowlist mismatch")
created = []
replaced = {}
preimages = {}
def ensure_root_parent(path):
    current = pathlib.Path("/opt")
    for part in path.parent.relative_to("/opt").parts:
        info = os.lstat(current)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_gid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
            raise SystemExit("unsafe root-owned input ancestor")
        current = current / part
        if not current.exists(): current.mkdir(mode=0o755)
    info = os.lstat(current)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_gid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
        raise SystemExit("unsafe root-owned input ancestor")
def atomic(path, data, mode):
    ensure_root_parent(path)
    temporary = path.with_name(f".{path.name}.input.{os.getpid()}.{time.time_ns()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        os.write(fd, data); os.fsync(fd); os.fchmod(fd, mode); os.fchown(fd, 0, 0)
    finally: os.close(fd)
    os.replace(temporary, path)
try:
    manifest_path = input_root / "manifests/deepseek-v4-flash-bootstrap-input.lock.json"
    manifest_data = canonical + b"\n"
    if manifest_path.is_symlink() or (manifest_path.exists() and not manifest_path.is_file()):
        raise SystemExit("unsafe bootstrap control manifest preimage")
    if manifest_path.exists():
        info = manifest_path.stat()
        previous = manifest_path.read_bytes()
        previous_sha = hashlib.sha256(previous).hexdigest()
        if stat.S_IMODE(info.st_mode) != 0o644 or info.st_uid != 0 or info.st_gid != 0:
            raise SystemExit("different bootstrap control manifest preimage rejected")
        if previous == manifest_data:
            preimages[str(manifest_path)] = "identical"
        elif previous_sha == payload.get("previous_manifest_sha256"):
            replaced[manifest_path] = (previous, 0o644)
            atomic(manifest_path, manifest_data, 0o644)
            preimages[str(manifest_path)] = {"upgraded_sha256": previous_sha}
        else:
            raise SystemExit("different bootstrap control manifest preimage rejected")
    else:
        atomic(manifest_path, manifest_data, 0o644); created.append(manifest_path); preimages[str(manifest_path)] = "absent"
    for spec, row in zip(allowed, rows):
        if row.get("source_path") != spec.get("source_path") or row.get("destination") != spec.get("destination"):
            raise SystemExit("input bootstrap row order/scope mismatch")
        destination = pathlib.Path(spec["destination"])
        if not destination.is_absolute() or not str(destination).startswith("/opt/deepseek-v4-flash-dspark/"):
            raise SystemExit("input bootstrap destination is outside fixed roots")
        if destination.is_symlink() or (destination.exists() and not destination.is_file()):
            raise SystemExit("unsafe input bootstrap preimage")
        data = base64.b64decode(row.get("content_base64", ""), validate=True)
        mode = int(str(spec["mode"]), 8)
        digest = hashlib.sha256(data).hexdigest()
        if digest != spec.get("sha256") or len(data) != spec.get("size") or row.get("sha256") != digest or mode not in (0o644, 0o755):
            raise SystemExit("input bootstrap content/hash/mode mismatch")
        if destination.exists():
            info = destination.stat()
            previous = destination.read_bytes()
            previous_sha = hashlib.sha256(previous).hexdigest()
            if stat.S_IMODE(info.st_mode) != mode or info.st_uid != 0 or info.st_gid != 0:
                raise SystemExit("different input bootstrap preimage rejected")
            if previous_sha == digest:
                preimages[str(destination)] = "identical"
            elif previous_sha == payload.get("previous_sha256_by_path", {}).get(spec["source_path"]):
                replaced[destination] = (previous, mode)
                atomic(destination, data, mode)
                preimages[str(destination)] = {"upgraded_sha256": previous_sha}
            else:
                raise SystemExit("different input bootstrap preimage rejected")
        else:
            atomic(destination, data, mode); created.append(destination); preimages[str(destination)] = "absent"
except BaseException:
    for path, (data, mode) in reversed(list(replaced.items())): atomic(path, data, mode)
    for path in reversed(created): path.unlink(missing_ok=True)
    raise
try:
    installed = {}
    manifest_info = manifest_path.stat()
    installed[str(manifest_path)] = {"sha256":hashlib.sha256(manifest_path.read_bytes()).hexdigest(),"mode":format(stat.S_IMODE(manifest_info.st_mode),"04o"),"uid":manifest_info.st_uid,"gid":manifest_info.st_gid}
    for spec in allowed:
        path = pathlib.Path(spec["destination"]); info = path.stat()
        installed[str(path)] = {"sha256":hashlib.sha256(path.read_bytes()).hexdigest(),"mode":format(stat.S_IMODE(info.st_mode),"04o"),"uid":info.st_uid,"gid":info.st_gid}
    service_extra = input_root / "docker-compose.dspark.yml"
    if service_extra.exists():
        service_info = service_extra.stat()
        service_digest = hashlib.sha256(service_extra.read_bytes()).hexdigest()
        if (
            service_digest != payload.get("service_compose_sha256")
            or stat.S_IMODE(service_info.st_mode) != 0o644
            or service_info.st_uid != 0
            or service_info.st_gid != 0
        ):
            raise SystemExit("installed service compose identity drifted")
        installed[str(service_extra)] = {"sha256":service_digest,"mode":"0644","uid":0,"gid":0}
    if any(path.is_symlink() for path in input_root.rglob("*")):
        raise SystemExit("input bootstrap exact role tree contains a symlink")
    actual = {str(path) for path in input_root.rglob("*") if path.is_file()}
    if actual != set(installed):
        raise SystemExit("input bootstrap exact role tree contains missing/extra files")
except BaseException:
    for path, (data, mode) in reversed(list(replaced.items())): atomic(path, data, mode)
    for path in reversed(created): path.unlink(missing_ok=True)
    raise
print(json.dumps({"terminal":"PASS","role":role,"created":[str(path) for path in created],"preimages":preimages,"installed":installed}, sort_keys=True))
'''

SOURCE_INSTALL_REMOTE = r'''
import base64, ctypes, errno, hashlib, json, os, pathlib, pwd, shutil, socket, stat, sys, time
expected_host, expected_snapshot = sys.argv[1:]
if socket.gethostname() != expected_host or os.environ.get("SUDO_USER") != "operator" or os.geteuid() != 0:
    raise SystemExit("source install identity mismatch")
payload = json.load(sys.stdin)
canonical = json.dumps({k:v for k,v in payload.items() if k != "snapshot_sha256"}, sort_keys=True, separators=(",", ":")).encode()
if hashlib.sha256(canonical).hexdigest() != expected_snapshot or payload.get("snapshot_sha256") != expected_snapshot:
    raise SystemExit("source snapshot hash mismatch")
if payload.get("origin") != "https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark.git" or payload.get("commit") != "0107cef1835a56d1a2bcdabf7d9e1a085b70338b" or payload.get("tree") != "a5258a8070b62fe77ed4bdb8887b39ef388e3a24":
    raise SystemExit("source identity mismatch")
if payload.get("file_count") != 171 or payload.get("total_bytes") != 4293967 or len(payload.get("files", [])) != 171 or payload.get("root_sha256") != "83bea7e3d33dfac0e778ea1bd2bb885429a6e51d9cab34e6130342f7a7e8d2c8":
    raise SystemExit("source frozen count/bytes/root closure mismatch")
expected_identity = {"schema_version":1,"repository":payload["origin"],"commit":payload["commit"],"git_tree":payload["tree"],"variant":"upstream","files":[{key:row[key] for key in ("path","mode","size","sha256")} for row in payload["files"]],"file_count":payload["file_count"],"total_bytes":payload["total_bytes"],"root_sha256":payload["root_sha256"]}
root_material = "".join(f"{row['sha256']}  {row['mode']}  {row['size']}  {row['path']}\n" for row in expected_identity["files"])
if hashlib.sha256(root_material.encode()).hexdigest() != expected_identity["root_sha256"]:
    raise SystemExit("source root closure mismatch")
target = pathlib.Path("/home/operator/dgx-models/deepseek-v4-flash-dspark/source")
operator = pwd.getpwnam("operator")
def ensure_source_parent():
    for path, uid, gid in ((pathlib.Path("/home"),0,0),(pathlib.Path("/home/operator"),operator.pw_uid,operator.pw_gid)):
        info=os.lstat(path)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=uid or info.st_gid!=gid or stat.S_IMODE(info.st_mode)&0o022:
            raise SystemExit("unsafe source ancestor")
    current=pathlib.Path("/home/operator")
    for part in target.parent.relative_to(current).parts:
        current=current/part
        if not current.exists(): current.mkdir(mode=0o755); os.chown(current,operator.pw_uid,operator.pw_gid)
        info=os.lstat(current)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=operator.pw_uid or info.st_gid!=operator.pw_gid or stat.S_IMODE(info.st_mode)&0o022:
            raise SystemExit("unsafe source ancestor")
identity_data=(json.dumps(expected_identity, sort_keys=True)+"\n").encode()
def verify_tree(root):
    root_info=os.lstat(root)
    if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid!=operator.pw_uid or root_info.st_gid!=operator.pw_gid or stat.S_IMODE(root_info.st_mode)!=0o755:
        raise SystemExit("source root owner/mode postimage drift")
    identity_path=root/".deepseek-source-identity.json"
    if not identity_path.is_file() or identity_path.is_symlink() or identity_path.read_bytes()!=identity_data:
        raise SystemExit("source identity postimage drift")
    identity_info=os.lstat(identity_path)
    if identity_info.st_uid!=operator.pw_uid or identity_info.st_gid!=operator.pw_gid or stat.S_IMODE(identity_info.st_mode)!=0o644:
        raise SystemExit("source identity owner/mode postimage drift")
    expected_files={identity_path}; expected_dirs={root}; total=0
    for row in expected_identity["files"]:
        path=root.joinpath(*pathlib.PurePosixPath(row["path"]).parts)
        if path.is_symlink() or not path.is_file(): raise SystemExit("existing source file missing/unsafe")
        info=os.lstat(path); data=path.read_bytes(); total+=len(data)
        if info.st_uid!=operator.pw_uid or info.st_gid!=operator.pw_gid or stat.S_IMODE(info.st_mode)!=int(row["mode"],8) or len(data)!=row["size"] or hashlib.sha256(data).hexdigest()!=row["sha256"]: raise SystemExit("existing source content/owner/mode drift")
        expected_files.add(path)
        parent=path.parent
        while parent!=root:
            expected_dirs.add(parent); parent=parent.parent
    entries=set(root.rglob("*"))
    if any(path.is_symlink() for path in entries): raise SystemExit("existing source tree contains a symlink")
    actual_files={path for path in entries if path.is_file()}
    actual_dirs={root}|{path for path in entries if path.is_dir()}
    if actual_files!=expected_files or actual_dirs!=expected_dirs or total!=4293967: raise SystemExit("existing source exact-set closure drift")
    for directory in actual_dirs:
        info=os.lstat(directory)
        if info.st_uid!=operator.pw_uid or info.st_gid!=operator.pw_gid or stat.S_IMODE(info.st_mode)!=0o755:
            raise SystemExit("existing source directory owner/mode drift")
def rename_noreplace(source, destination):
    libc=ctypes.CDLL(None, use_errno=True)
    renameat2=getattr(libc,"renameat2",None)
    if renameat2 is None: raise SystemExit("renameat2 no-replace primitive unavailable")
    renameat2.argtypes=(ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint)
    renameat2.restype=ctypes.c_int
    if renameat2(-100,os.fsencode(source),-100,os.fsencode(destination),1)!=0:
        error=ctypes.get_errno()
        if error==errno.EEXIST: raise FileExistsError("source target appeared during no-replace promotion")
        raise OSError(error,os.strerror(error),str(destination))
ensure_source_parent()
if target.is_symlink() or (target.exists() and not target.is_dir()): raise SystemExit("unsafe source target")
if target.exists():
    verify_tree(target)
    print(json.dumps({"terminal":"already-satisfied","snapshot_sha256":expected_snapshot})); raise SystemExit(0)
staging = target.with_name(f".{target.name}.install.{os.getpid()}.{time.time_ns()}")
staging.mkdir(mode=0o755)
created_target=False
promoted_identity=None
try:
    for row in payload.get("files", []):
        relative = pathlib.PurePosixPath(row["path"])
        if relative.is_absolute() or ".." in relative.parts or row.get("git_mode") not in ("100644", "100755") or row.get("mode") not in ("0644", "0755"):
            raise SystemExit("unsafe source snapshot row")
        data = base64.b64decode(row["content_base64"], validate=True)
        if hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() != row.get("blob"):
            raise SystemExit("source blob mismatch")
        path = staging.joinpath(*relative.parts); path.parent.mkdir(parents=True, exist_ok=True)
        if len(data) != row.get("size") or hashlib.sha256(data).hexdigest() != row.get("sha256"):
            raise SystemExit("source content closure mismatch")
        path.write_bytes(data); path.chmod(int(row["mode"], 8)); os.chown(path, operator.pw_uid, operator.pw_gid)
    identity_path = staging / ".deepseek-source-identity.json"; identity_path.write_bytes(identity_data); identity_path.chmod(0o644); os.chown(identity_path, operator.pw_uid, operator.pw_gid)
    for directory in sorted((path for path in staging.rglob("*") if path.is_dir()), reverse=True): directory.chmod(0o755); os.chown(directory, operator.pw_uid, operator.pw_gid)
    staging.chmod(0o755); os.chown(staging, operator.pw_uid, operator.pw_gid)
    verify_tree(staging)
    staging_info=os.lstat(staging); promoted_identity=(staging_info.st_dev,staging_info.st_ino)
    rename_noreplace(staging,target); created_target=True
    target_info=os.lstat(target)
    if not stat.S_ISDIR(target_info.st_mode) or (target_info.st_dev,target_info.st_ino)!=promoted_identity:
        raise SystemExit("source promotion identity changed before readback")
    verify_tree(target)
except BaseException:
    if staging.exists() and not staging.is_symlink(): shutil.rmtree(staging, ignore_errors=True)
    if created_target:
        try:
            info=os.lstat(target)
            if stat.S_ISDIR(info.st_mode) and (info.st_dev,info.st_ino)==promoted_identity:
                shutil.rmtree(target)
        except FileNotFoundError:
            pass
    raise
print(json.dumps({"terminal":"PASS","snapshot_sha256":expected_snapshot}, sort_keys=True))
'''


class Completed:
    def __init__(self, returncode: int, stdout: str, stderr: str):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def assert_private_external_path(path: Path, label: str) -> Path:
    """Reject authority material inside either the checkout or canonical repo."""
    resolved = path.expanduser().resolve(strict=False)
    forbidden = {REPO_ROOT.resolve(), CANONICAL_REPO_ROOT.resolve()}
    if any(_is_within(resolved, root) for root in forbidden):
        raise ControllerError(f"{label} path must be outside every repository: {resolved}")
    if resolved == LOCK_ROOT or _is_within(resolved, LOCK_ROOT):
        raise ControllerError(f"{label} path must not be inside the removable lock root: {resolved}")
    return resolved


def parse_json_result(result: Completed, context: str) -> dict:
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise ControllerError(f"{context} failed: {detail}")
    try:
        value = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ControllerError(f"{context} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ControllerError(f"{context} returned non-object JSON")
    return value


def validate_gateway_rejection_logs(report: dict, log_text: str) -> dict:
    negative = [row for row in report.get("matrix", []) if "expected_upstream_hits" in row]
    if not negative:
        raise PlanGateError("gateway report omitted negative request IDs")
    for row in negative:
        request_id = row.get("request_id")
        if not isinstance(request_id, str) or not request_id.startswith("deepseek-reject-"):
            raise PlanGateError("gateway rejection omitted a unique request ID")
        matches = [line for line in log_text.splitlines() if f'rid="{request_id}"' in line]
        if len(matches) != 1 or 'upstream="-/-"' not in matches[0]:
            raise PlanGateError(f"gateway rejection reached upstream or lacks unique log: {request_id}")
        row["actual_upstream_hits"] = 0
    report["passed"] = report.get("passed") is True and all(
        row.get("actual_upstream_hits") == 0 for row in negative
    )
    return report


class SubprocessRunner:
    """Runs one exact argv; supervised runs own only their process group."""

    def run(
        self,
        argv: Iterable[str],
        *,
        input_text: str | None = None,
        timeout: float = 30,
    ) -> Completed:
        try:
            result = subprocess.run(
                list(argv),
                input=input_text,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise OperationTimeout(f"bounded command exceeded {timeout}s") from exc
        except OSError as exc:
            raise ControllerError(f"cannot execute bounded command: {exc}") from exc
        return Completed(result.returncode, result.stdout, result.stderr)

    def run_supervised(
        self,
        argv: Iterable[str],
        *,
        input_text: str | None,
        timeout: float,
        lease_check: Callable[[], object],
        poll_seconds: float = 2.0,
    ) -> Completed:
        # Temporary files avoid deadlocking on a verbose child's PIPE buffer.
        with tempfile.TemporaryFile(mode="w+") as stdout_file, tempfile.TemporaryFile(
            mode="w+"
        ) as stderr_file:
            try:
                process = subprocess.Popen(
                    list(argv),
                    stdin=subprocess.PIPE,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    text=True,
                    start_new_session=True,
                )
            except OSError as exc:
                raise ControllerError(f"cannot execute supervised command: {exc}") from exc
            if process.stdin is not None:
                if input_text:
                    process.stdin.write(input_text)
                process.stdin.close()
                process.stdin = None
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        raise OperationTimeout(f"bounded operation exceeded {timeout}s")
                    lease_check()
                    time.sleep(min(poll_seconds, max(0.01, deadline - time.monotonic())))
                process.wait()
                stdout_file.seek(0)
                stderr_file.seek(0)
                return Completed(process.returncode, stdout_file.read(), stderr_file.read())
            except BaseException:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
                raise


class RecordingRunner:
    """Side-effect-free runner for audits and tests.

    It records normalized argv and returns queued results; it never executes a
    command.  Production code must opt into SubprocessRunner explicitly.
    """

    def __init__(self, results: Iterable[Completed] = ()):
        self.results = list(results)
        self.calls: list[dict] = []

    def _record(self, argv: Iterable[str], kwargs: dict) -> Completed:
        self.calls.append(
            {
                "argv": list(map(str, argv)),
                "has_stdin": bool(kwargs.get("input_text")),
                "timeout": kwargs.get("timeout"),
            }
        )
        if not self.results:
            raise ControllerError("record-only runner has no queued result")
        return self.results.pop(0)

    def run(self, argv: Iterable[str], **kwargs) -> Completed:
        return self._record(argv, kwargs)

    def run_supervised(self, argv: Iterable[str], **kwargs) -> Completed:
        kwargs["lease_check"]()
        return self._record(argv, kwargs)


class Journal:
    def __init__(self, path: Path):
        self.path = assert_private_external_path(path, "journal")

    def append(self, event: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            payload = {"recorded_at": iso(utc_now()), **event}
            os.write(fd, (json.dumps(payload, sort_keys=True) + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(self.path, 0o600)

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        values = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    values.append(value)
        return values

    def latest_open_cutover(self) -> str:
        latest: dict[str, str] = {}
        order: list[str] = []
        for entry in self.entries():
            if entry.get("event") != "CUTOVER_STATE":
                continue
            transaction = entry.get("transaction_id")
            state = entry.get("state")
            if isinstance(transaction, str) and isinstance(state, str):
                if transaction not in latest:
                    order.append(transaction)
                latest[transaction] = state
        for transaction in reversed(order):
            if latest[transaction] not in {"FALLBACK_DISARMED", "VERIFIED"}:
                return transaction
        raise PlanGateError("no open cutover transaction exists for rollback")


class CutoverStateMachine:
    """Append-only exact-order state projection for one production transaction."""

    def __init__(self, journal: Journal, transaction_id: str):
        if (
            len(transaction_id) != 32
            or any(character not in "0123456789abcdef" for character in transaction_id)
        ):
            raise PlanGateError("cutover transaction ID must be 32 lowercase hex characters")
        self.journal = journal
        self.transaction_id = transaction_id
        self.index = 0

    def advance(self, state: str, **evidence: object) -> None:
        if self.index >= len(CUTOVER_STATES):
            raise PlanGateError("cutover transaction is already terminal")
        expected = CUTOVER_STATES[self.index]
        if state != expected:
            raise PlanGateError(f"cutover state skip/reorder: expected {expected}, got {state}")
        self.journal.append(
            {
                "event": "CUTOVER_STATE",
                "transaction_id": self.transaction_id,
                "state": state,
                **evidence,
            }
        )
        self.index += 1


class FallbackRefresher:
    """Refresh the persistent deadline every two minutes while gates run."""

    def __init__(self, callback: Callable[[], object], interval: float = 120):
        self.callback = callback
        self.interval = interval
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.failure: BaseException | None = None
        self.thread = threading.Thread(
            target=self._run, name="deepseek-fallback-refresh", daemon=True
        )

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval):
            if self.pause_event.is_set():
                continue
            try:
                self.callback()
            except BaseException as exc:
                self.failure = exc
                self.stop_event.set()

    def __enter__(self):
        self.thread.start()
        return self

    def require_live(self) -> None:
        if self.failure is not None:
            raise ControllerError(f"fallback deadline refresh failed: {self.failure}") from self.failure

    def pause_declared_head_reboot(self) -> None:
        self.require_live()
        self.pause_event.set()

    def resume_after_head_recovery(self) -> None:
        self.require_live()
        self.pause_event.clear()

    def __exit__(self, *_):
        self.stop_event.set()
        self.thread.join(timeout=5)
        self.require_live()


def validate_fallback_config(value: dict, transaction_id: str) -> dict:
    expected = {
        "schema_version",
        "state",
        "transaction_id",
        "head_hostname",
        "old_model",
        "old_ollama_version",
        "legacy_units",
    }
    if set(value) != expected:
        raise PlanGateError("fallback state does not have the exact schema")
    if (
        value.get("schema_version") != 1
        or value.get("state") != "FALLBACK_ARMED"
        or value.get("transaction_id") != transaction_id
        or value.get("head_hostname") != "dgx-head"
        or value.get("old_model") != "qwen38-q4-256k:latest"
        or value.get("old_ollama_version") != "0.32.15"
    ):
        raise PlanGateError("fallback state differs from the frozen legacy oracle")
    units = value.get("legacy_units")
    if not isinstance(units, dict) or set(units) != {
        "ollama.service",
        "dgx-codex-production.service",
    }:
        raise PlanGateError("fallback state omitted a legacy unit preimage")
    for unit_state in units.values():
        if not isinstance(unit_state, dict) or set(unit_state) != {"enabled", "active"}:
            raise PlanGateError("legacy unit preimage has unexpected fields")
        if not all(isinstance(unit_state[key], bool) for key in ("enabled", "active")):
            raise PlanGateError("legacy unit preimage must contain booleans")
    if units["ollama.service"] != {"enabled": True, "active": True}:
        raise PlanGateError("Ollama preimage must be enabled and active")
    return value


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_fabric_lock(path: Path = FABRIC_LOCK) -> dict:
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
        provenance = json.loads(FABRIC_PROVENANCE.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PlanGateError(f"fabric consumer lock input is unavailable: {exc}") from exc
    if (
        lock.get("schema_version") != 1
        or lock.get("kind") != "dgx-spark-two-node-fabric-qualification-consumer-lock"
        or lock.get("qualification_id") != "nccl-evidence-post-upgrade-20260830T112621Z"
        or lock.get("qualification_path")
        != "plans/minimax-h3-two-node/spikes/fabric-network-transaction/nccl-evidence-post-upgrade-20260830T112621Z"
        or FABRIC_EVIDENCE.name != lock.get("qualification_id")
    ):
        raise PlanGateError("fabric lock is not the exact accepted qualification generation")
    expected_hashes = [
        ("fabric-qualification-release-receipt-v1.json", lock["producer_receipt"]["sha256"]),
        ("single-a/SHA256SUMS", lock["candidate_sha256sums"]["single_a"]),
        ("single-b/SHA256SUMS", lock["candidate_sha256sums"]["single_b"]),
        ("dual/SHA256SUMS", lock["candidate_sha256sums"]["dual"]),
        ("single-a-score.json", lock["score_sha256s"]["single_a"]),
        ("single-b-score.json", lock["score_sha256s"]["single_b"]),
        ("dual-score.json", lock["score_sha256s"]["dual"]),
        ("post-qualification-audit/SHA256SUMS", lock["audit_seal_sha256"]),
    ]
    for relative, expected in expected_hashes:
        evidence = FABRIC_EVIDENCE / relative
        if not evidence.is_file() or _sha256_file(evidence) != expected:
            raise PlanGateError(f"fabric evidence digest mismatch: {relative}")
    if lock.get("provenance") != provenance:
        raise PlanGateError("fabric lock does not embed the exact provenance addendum")
    line_hashes = provenance.get("source_session", {}).get("line_hashes")
    if line_hashes != {
        "859": "b6e652c6837e4d3c632d1b09673eef1abef0b2dd7d92d50d7736ff789b2b9a4b",
        "860": "ff7a25fde8345e744c9667ea5779e6c7f33d30ac834637c38dfca67a88163725",
        "2678": "70cea2e52966a9ae781bc80ec0879138a59ddb6594a86616e9e189d1355af9d1",
        "2679": "2d31d262d9a258bedc954bfe644a6e1dd1a1ca9c7c952e0c1267751da18273a1",
        "2680": "e48babb3d2b211ab26f8e16eb5083615194571deec7172974e3e88bc6ee99743",
    }:
        raise PlanGateError("fabric provenance line hashes differ")
    limitations = provenance.get("historical_limitations", {})
    if (
        limitations.get("lock_level_released_at_retained") is not False
        or limitations.get("immutable_lock_release_operation_id_retained") is not False
        or limitations.get("transcript_ids_are_not_lock_operation_ids") is not True
    ):
        raise PlanGateError("fabric provenance reconstructs unavailable lock metadata")
    if lock.get("selected_runtime_binding") != {
        "bootstrap_and_oob_interface": "enp1s0f0np0",
        "worker_bootstrap_ip": "192.0.2.34",
        "NCCL_SOCKET_IFNAME": "=enp1s0f0np0",
        "NCCL_IB_HCA": "=rocep1s0f0:1,roceP2p1s0f0:1",
        "NCCL_IB_GID_INDEX": "3",
        "NCCL_CROSS_NIC": "0",
    }:
        raise PlanGateError("fabric runtime binding differs from the measured selection")
    return lock


def validate_live_fabric(
    role: str, report: dict, *, require_head_ssh: bool = True
) -> dict:
    host = HOSTS[role]
    expected = FABRIC_EXPECTED[role]
    if report.get("hostname") != host.hostname or report.get("user") != "operator":
        raise PlanGateError(f"{role} live fabric report has the wrong identity")
    if (
        report.get("kernel") != "6.17.0-1031-nvidia"
        or report.get("driver") != "580.173.02"
        or not str(report.get("dgx_ota", "")).startswith("7.5.0")
    ):
        raise PlanGateError(f"{role} kernel/driver drifted from qualification")
    if report.get("netplan_sha256") != expected["netplan_sha256"]:
        raise PlanGateError(f"{role} persistent netplan digest drifted")
    links = report.get("links")
    if not isinstance(links, dict) or set(links) != set(FABRIC_INTERFACES):
        raise PlanGateError(f"{role} fabric interface set differs")
    for index, interface in enumerate(FABRIC_INTERFACES):
        link = links[interface]
        if (
            link.get("mtu") != 9000
            or link.get("operstate") != "UP"
            or str(link.get("speed")) != "200000"
            or link.get("cidrs") != [expected["cidrs"][index]]
            or not isinstance(link.get("mac"), str)
            or not isinstance(link.get("bus_info"), str)
            or not link.get("firmware")
        ):
            raise PlanGateError(f"{role} {interface} link/address gate failed")
    if len({links[interface]["mac"] for interface in FABRIC_INTERFACES}) != 2 or len(
        {links[interface]["bus_info"] for interface in FABRIC_INTERFACES}
    ) != 2:
        raise PlanGateError(f"{role} fabric MAC/PCI identities are not distinct")
    if report.get("hca_firmware") != {hca: "28.45.4028" for hca in FABRIC_HCAS}:
        raise PlanGateError(f"{role} HCA firmware drifted from qualification")
    if any(route.get("dev") in FABRIC_INTERFACES for route in report.get("default_routes", [])):
        raise PlanGateError(f"{role} has a fabric default route")
    if report.get("pings") != {peer: 0 for peer in expected["peers"]}:
        raise PlanGateError(f"{role} DF-jumbo bidirectional probe failed")
    routes = report.get("routes")
    if not isinstance(routes, dict) or set(routes) != set(expected["peers"]):
        raise PlanGateError(f"{role} fabric route set is incomplete")
    for index, peer in enumerate(expected["peers"]):
        route = routes[peer]
        expected_source = expected["cidrs"][index].split("/", 1)[0]
        if (
            route.get("dev") != FABRIC_INTERFACES[index]
            or route.get("prefsrc", route.get("src")) != expected_source
            or "gateway" in route
        ):
            raise PlanGateError(f"{role} fabric route for {peer} is not direct/static")
    ibdev = str(report.get("ibdev2netdev", ""))
    gids = str(report.get("show_gids", ""))
    gid_rows = [line.split() for line in gids.splitlines()]
    for interface, hca in zip(FABRIC_INTERFACES, FABRIC_HCAS):
        if hca not in ibdev or interface not in ibdev or "Up" not in ibdev:
            raise PlanGateError(f"{role} HCA-to-netdev mapping is not active")
        if not any(
            len(row) >= 6
            and row[0] == hca
            and row[1] == "1"
            and row[2] == "3"
            and "v2" in row
            and interface in row
            for row in gid_rows
        ):
            raise PlanGateError(f"{role} RoCEv2 GID index 3 is absent")
    listeners = "\n".join(report.get("listeners", []))
    if role == "worker" and (":11434" in listeners or ":18888" in listeners):
        raise PlanGateError("worker exposes an HTTP listener")
    if role == "head" and ":18888" in listeners and "127.0.0.1:18888" not in listeners:
        raise PlanGateError("head internal API is not loopback-only")
    if role == "head" and require_head_ssh and report.get("head_to_worker") != {
        "returncode": 0,
        "stdout": ["dgx-worker", "operator"],
    }:
        raise PlanGateError("dedicated strict head-to-worker fabric SSH failed")
    return report


class LockAuthority:
    def __init__(
        self,
        *,
        runner,
        token_file: Path = DEFAULT_TOKEN_FILE,
        owner: str = OWNER,
        lock_root: Path = LOCK_ROOT,
        lock_script: Path = LOCK_SCRIPT,
        lock_history: Path = LOCK_HISTORY,
    ):
        self.runner = runner
        self.token_file = assert_private_external_path(token_file, "token")
        self.owner = owner
        self.lock_root = lock_root
        self.lock_script = lock_script
        self.lock_history = lock_history

    def _invoke(self, command: str, *extra: str) -> dict:
        argv = [sys.executable, str(self.lock_script)]
        if self.lock_root != LOCK_ROOT:
            argv += ["--root", str(self.lock_root)]
        argv += [command, *extra]
        result = self.runner.run(argv, timeout=30)
        try:
            return parse_json_result(result, f"deployment lock {command}")
        except ControllerError as exc:
            if command == "guard":
                raise LeaseLost(str(exc)) from exc
            raise

    def status(self) -> dict:
        try:
            state = self._invoke("status")
        except ControllerError as exc:
            raise LockUnavailable(f"lock status UNKNOWN: {exc}") from exc
        if state.get("state") not in {"UNLOCKED", "ACTIVE", "STALE"}:
            raise LockUnavailable("lock status UNKNOWN")
        return state

    def acquire(self, task_id: str, purpose: str, lease_seconds: int = 1800) -> dict:
        current = self.status()
        if current["state"] != "UNLOCKED":
            raise LockUnavailable(
                f"lock is {current['state']} owner={current.get('owner', 'unknown')}; takeover forbidden"
            )
        self.token_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.token_file.parent, 0o700)
        if self.token_file.exists():
            raise LockUnavailable(f"refusing to overwrite existing token file {self.token_file}")
        value = self._invoke(
            "acquire",
            "--owner",
            self.owner,
            "--task-id",
            task_id,
            "--purpose",
            purpose,
            "--lease-seconds",
            str(lease_seconds),
            "--token-file",
            str(self.token_file),
        )
        if value.get("state") != "ACTIVE" or value.get("owner") != self.owner:
            raise LockUnavailable("lock acquire did not return our active owner")
        if not self.token_file.is_file():
            raise LockUnavailable("lock acquire returned ACTIVE without a token file")
        if stat.S_IMODE(self.token_file.stat().st_mode) != 0o600:
            raise LockUnavailable("token file permissions are not 0600")
        if not self.token_file.read_text(encoding="utf-8").strip():
            raise LockUnavailable("lock acquire created an empty token file")
        return value

    def guard(self) -> dict:
        value = self._invoke(
            "guard", "--owner", self.owner, "--token-file", str(self.token_file)
        )
        if value.get("state") != "ACTIVE" or value.get("owner") != self.owner:
            raise LeaseLost("guard did not confirm the active DeepSeek owner")
        return value

    def heartbeat(self) -> dict:
        value = self._invoke(
            "heartbeat", "--owner", self.owner, "--token-file", str(self.token_file)
        )
        if value.get("state") != "ACTIVE":
            raise LeaseLost("heartbeat did not preserve an active lease")
        return value

    def release(self) -> dict:
        self.guard()
        value = self._invoke(
            "release", "--owner", self.owner, "--token-file", str(self.token_file)
        )
        receipt = value.get("release_receipt")
        digest = value.get("release_receipt_sha256")
        receipt_path = Path(receipt).resolve(strict=False) if isinstance(receipt, str) else None
        if (
            value.get("state") != "UNLOCKED"
            or value.get("released_owner") != self.owner
            or not isinstance(value.get("release_operation_id"), str)
            or len(value["release_operation_id"]) != 32
            or not isinstance(receipt, str)
            or receipt_path is None
            or not receipt_path.is_absolute()
            or not _is_within(receipt_path, self.lock_history)
            or not receipt_path.is_file()
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise LockUnavailable("release omitted its immutable completion receipt")
        actual_digest = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        if actual_digest != digest or self.token_file.exists():
            raise LockUnavailable("release completion receipt or token deletion did not verify")
        return value


class Heartbeat:
    def __init__(self, authority: LockAuthority, interval: float = 30):
        self.authority = authority
        self.interval = interval
        self.stop_event = threading.Event()
        self.failure: BaseException | None = None
        self.thread = threading.Thread(target=self._run, name="deepseek-lock-heartbeat", daemon=True)

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval):
            try:
                self.authority.heartbeat()
            except BaseException as exc:
                self.failure = exc
                self.stop_event.set()

    def __enter__(self):
        self.thread.start()
        return self

    def require_live(self) -> dict:
        if self.failure is not None:
            raise LeaseLost(f"heartbeat failed: {self.failure}") from self.failure
        return self.authority.guard()

    def __exit__(self, *_):
        self.stop_event.set()
        self.thread.join(timeout=5)


def _compact_json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def validate_bootstrap_input_manifest(manifest: dict) -> str:
    expected_keys = {
        "schema_version",
        "kind",
        "finalized",
        "input_root",
        "file_count",
        "total_bytes",
        "root_sha256",
        "files",
        "upstream_source",
    }
    if (
        set(manifest) != expected_keys
        or manifest.get("schema_version") != 1
        or manifest.get("kind") != "deepseek-v4-flash-bootstrap-input-lock"
        or manifest.get("finalized") is not True
        or manifest.get("input_root") != REMOTE_INPUT_ROOT
        or manifest.get("file_count") != BOOTSTRAP_INPUT_FILE_COUNT
        or manifest.get("total_bytes") != BOOTSTRAP_INPUT_TOTAL_BYTES
        or manifest.get("root_sha256") != BOOTSTRAP_INPUT_ROOT_SHA256
        or not isinstance(manifest.get("files"), list)
        or len(manifest["files"]) != BOOTSTRAP_INPUT_FILE_COUNT
    ):
        raise PlanGateError("bootstrap input lock is not the finalized exact authority")

    paths = set()
    total_bytes = 0
    root_material = []
    for spec in manifest["files"]:
        if (
            set(spec) != {"path", "mode", "size", "sha256", "roles"}
            or spec.get("mode") not in {"0644", "0755"}
            or not isinstance(spec.get("size"), int)
            or spec["size"] < 0
            or not isinstance(spec.get("sha256"), str)
            or len(spec["sha256"]) != 64
            or spec.get("roles")
            not in (["head"], ["worker"], ["head", "worker"], ["worker", "head"])
            or not isinstance(spec.get("path"), str)
            or Path(spec["path"]).is_absolute()
            or ".." in Path(spec["path"]).parts
            or spec["path"] in paths
        ):
            raise PlanGateError(
                "bootstrap input lock contains a malformed or duplicate allowlist row"
            )
        paths.add(spec["path"])
        total_bytes += spec["size"]
        root_material.append(
            f"{spec['sha256']}  {spec['mode']}  {spec['size']}  "
            f"{','.join(spec['roles'])}  {spec['path']}\n"
        )
    if (
        total_bytes != BOOTSTRAP_INPUT_TOTAL_BYTES
        or hashlib.sha256("".join(root_material).encode()).hexdigest()
        != BOOTSTRAP_INPUT_ROOT_SHA256
    ):
        raise PlanGateError("bootstrap input lock file closure drifted")

    upstream = manifest.get("upstream_source", {})
    upstream_keys = {
        "schema_version",
        "destination",
        "identity_path",
        "repository",
        "commit",
        "git_tree",
        "variant",
        "file_count",
        "total_bytes",
        "root_sha256",
        "files",
    }
    if (
        set(upstream) != upstream_keys
        or upstream.get("schema_version") != 1
        or upstream.get("variant") != "upstream"
        or (
            upstream.get("destination"),
            upstream.get("repository"),
            upstream.get("commit"),
            upstream.get("git_tree"),
            upstream.get("file_count"),
            upstream.get("total_bytes"),
            upstream.get("root_sha256"),
            upstream.get("identity_path"),
        )
        != (
            REMOTE_SOURCE,
            PINNED_SOURCE_ORIGIN,
            PINNED_SOURCE_COMMIT,
            PINNED_SOURCE_TREE,
            171,
            4293967,
            "83bea7e3d33dfac0e778ea1bd2bb885429a6e51d9cab34e6130342f7a7e8d2c8",
            ".deepseek-source-identity.json",
        )
        or not isinstance(upstream.get("files"), list)
        or len(upstream["files"]) != 171
    ):
        raise PlanGateError("bootstrap input lock upstream source identity is not frozen")
    source_paths = set()
    source_total = 0
    source_root_material = []
    for row in upstream["files"]:
        if (
            set(row) != {"path", "mode", "size", "sha256"}
            or row.get("mode") not in {"0644", "0755"}
            or not isinstance(row.get("size"), int)
            or row["size"] < 0
            or not isinstance(row.get("sha256"), str)
            or len(row["sha256"]) != 64
            or not isinstance(row.get("path"), str)
            or Path(row["path"]).is_absolute()
            or ".." in Path(row["path"]).parts
            or row["path"] in source_paths
        ):
            raise PlanGateError("bootstrap input lock upstream file closure is malformed")
        source_paths.add(row["path"])
        source_total += row["size"]
        source_root_material.append(
            f"{row['sha256']}  {row['mode']}  {row['size']}  {row['path']}\n"
        )
    if (
        source_total != 4293967
        or hashlib.sha256("".join(source_root_material).encode()).hexdigest()
        != "83bea7e3d33dfac0e778ea1bd2bb885429a6e51d9cab34e6130342f7a7e8d2c8"
    ):
        raise PlanGateError("bootstrap input lock upstream file closure drifted")

    manifest_sha = hashlib.sha256(_compact_json(manifest).encode()).hexdigest()
    if manifest_sha != BOOTSTRAP_INPUT_CANONICAL_SHA256:
        raise PlanGateError("bootstrap input lock canonical digest drifted")
    return manifest_sha


def artifact_argv(verb: str) -> list[str]:
    p = REMOTE_PREPARE
    m = REMOTE_MODEL_ROOT
    s = REMOTE_ARTIFACT_STATE
    u = REMOTE_SOURCE
    a = REMOTE_RUNTIME_ARCHIVE
    ar = f"{a}.receipt.json"
    i = REMOTE_INPUT_ROOT
    fd = "/proc/self/fd/3"
    values = {
        "finalize-model-lock": [p, verb, "--model-root", m, "--download-manifest", f"{i}/manifests/deepseek-v4-flash-ablit-anchored-download.json", "--lock-template", f"{i}/manifests/deepseek-v4-flash-dspark.lock.json", "--official-anchor-inventory", f"{i}/manifests/deepseek-v4-flash-official-anchor-inventory.json", "--official-metadata", f"{i}/manifests/deepseek-v4-flash-official-metadata.json", "--output", f"{s}/model.lock.json", "--role", "head", "--receipt-file", fd],
        "copy-model": [p, verb, "--download-manifest", f"{i}/manifests/deepseek-v4-flash-ablit-anchored-download.json", "--lock", f"{s}/model.lock.json", "--source-root", m, "--role", "head", "--receipt-file", fd],
        "acquire-images": [p, verb, "--role", "head", "--lock", f"{s}/model.lock.json", "--journal", f"{s}/image-acquisition.journal.json", "--rx-counter", "/sys/class/net/dgx-vpn/statistics/rx_bytes", "--poll-seconds", "0.25", "--receipt-file", fd],
        "save-runtime-archive": [p, verb, "--role", "head", "--lock", f"{s}/model.lock.json", "--journal", f"{s}/image-acquisition.journal.json", "--archive", a, "--receipt", ar, "--receipt-file", fd],
        "copy-runtime-archive": [p, verb, "--lock", f"{s}/model.lock.json", "--archive", a, "--receipt", ar, "--journal", f"{s}/image-acquisition.journal.json", "--role", "head", "--receipt-file", fd],
        "load-runtime-archive": [p, verb, "--role", "worker", "--lock", f"{s}/model.lock.json", "--archive", a, "--receipt", ar, "--receipt-file", fd],
        "apply-runtime-patch": [p, verb, "--lock", f"{i}/manifests/deepseek-v4-flash-dspark.lock.json", "--source-root", u, "--patch", f"{i}/patches/deepseek-v4-flash-dspark-local-runtime.patch", "--source-receipt", f"{s}/source.receipt.json", "--role", "head", "--receipt-file", fd],
        "install-integration-artifacts": [p, verb, "--integration-receipt", f"{s}/integration.receipt.json", "--role", "head", "--receipt-file", fd],
        "finalize-deployment-lock": [p, verb, "--bootstrap-lock", f"{i}/manifests/deepseek-v4-flash-bootstrap-input.lock.json", "--model-lock", f"{s}/model.lock.json", "--source-receipt", f"{s}/source.receipt.json", "--license-receipt", f"{s}/license.receipt.json", "--integration-receipt", f"{s}/integration.receipt.json", "--image-journal", f"{s}/image-acquisition.journal.json", "--runtime-archive", a, "--archive-receipt", ar, "--head-identity-receipt", f"{s}/head.identity.json", "--worker-identity-receipt", f"{s}/worker.identity.json", "--output", f"{s}/deployment.lock.json", "--role", "head", "--receipt-file", fd],
        "copy-patched-source": [p, verb, "--role", "head", "--lock", f"{s}/model.lock.json", "--source-root", u, "--source-receipt", f"{s}/source.receipt.json", "--receipt-file", fd],
        "record-license-acceptance": [p, verb, "--role", "head", "--lock", f"{s}/model.lock.json", "--operator", "operator", "--approval-basis-id", "AC-DS-RESPONSIBLE-USE-20260830-USER-CONFIRMED", "--approval-basis", "User explicitly confirmed the selected DeepSeek V4 Flash model and replacement deployment in the controlling Codex task.", "--authority-commit", "52ccc1adc05922d203614dc263d91dd6dea2076d", "--output", f"{s}/license.receipt.json", "--receipt-file", fd],
        QUALIFY_VERB: [p, verb, "--role", "head", "--fabric-lock", f"{i}/manifests/dgx-spark-fabric-qualification.lock.json", "--provenance-addendum", f"{i}/plans/deepseek-v4-flash-dspark-2node/verification/20260829T1951/h3-lock-provenance-addendum-v1.json", "--deployment-lock", f"{s}/deployment.lock.json", "--output", f"{s}/fabric-consumer.receipt.json", "--receipt-file", fd],
    }
    try:
        return values[verb]
    except KeyError as exc:
        raise ControllerError(f"unsupported artifact verb: {verb}") from exc


def service_asset_argv(role: str) -> list[str]:
    if role not in HOSTS:
        raise ControllerError(f"unsupported service asset role: {role}")
    argv = [
        REMOTE_PREPARE,
        INSTALL_SERVICE_VERB,
        "--role",
        role,
        "--final-lock",
        f"{REMOTE_ARTIFACT_STATE}/deployment.lock.json",
        "--source-root",
        REMOTE_SOURCE,
        "--asset-receipt",
        f"{REMOTE_ARTIFACT_STATE}/service-assets-{role}.receipt.json",
        "--receipt-file",
        "/proc/self/fd/3",
    ]
    if role == "head":
        argv.extend(["--secret-fd", "/proc/self/fd/4"])
    return argv


def node_identity_argv(role: str) -> list[str]:
    if role not in HOSTS:
        raise ControllerError(f"unsupported node identity role: {role}")
    image_journal = (
        f"{REMOTE_ARTIFACT_STATE}/image-acquisition.journal.json"
        if role == "head"
        else str(Path(REMOTE_RUNTIME_ARCHIVE).parent / "image-acquisition.journal.json")
    )
    return [REMOTE_PREPARE, "record-node-identity", "--role", role, "--lock", f"{REMOTE_ARTIFACT_STATE}/model.lock.json", "--model-root", REMOTE_MODEL_ROOT, "--image-journal", image_journal, "--archive-receipt", f"{REMOTE_RUNTIME_ARCHIVE}.receipt.json", "--output", f"{REMOTE_ARTIFACT_STATE}/{role}.identity.json", "--receipt-file", "/proc/self/fd/3"]


def action_argv(
    role: str,
    verb: str,
    transaction_id: str | None = None,
    payload: dict | None = None,
) -> list[str]:
    compose = [
        "sudo",
        "-n",
        "docker",
        "compose",
        "--env-file",
        REMOTE_ENV,
        "--file",
        REMOTE_COMPOSE,
        "--project-directory",
        REMOTE_SOURCE,
        "--project-name",
        PROJECT,
    ]
    if verb in ARTIFACT_VERBS:
        expected_role = ARTIFACT_VERBS[verb][0]
        if role != expected_role:
            raise ControllerError(f"artifact verb {verb} is restricted to {expected_role}")
        return artifact_argv(verb)
    if verb in DUAL_ROLE_ARTIFACT_VERBS:
        return node_identity_argv(role)
    if verb == QUALIFY_VERB:
        if role != "head":
            raise ControllerError("fabric consumer qualification is head-only")
        return artifact_argv(verb)
    if verb in TRUST_VERBS:
        expected_role = TRUST_VERBS[verb]
        if role != expected_role or not transaction_id:
            raise ControllerError(f"trust verb {verb} requires {expected_role} and a transaction ID")
        argv = [
            REMOTE_HELPER,
            "--trust-action",
            verb,
            "--role",
            role,
            "--verb",
            verb,
            "--transaction-id",
            transaction_id,
        ]
        if verb == "install-worker-authorized-key":
            if not isinstance(payload, dict) or set(payload) != {"public_key", "fingerprint"}:
                raise ControllerError("worker trust install requires exact public key binding")
            argv += ["--public-key", payload["public_key"], "--fingerprint", payload["fingerprint"]]
        elif verb == "install-head-worker-known-hosts":
            if not isinstance(payload, dict) or set(payload) != {"host_public_key", "fingerprint"}:
                raise ControllerError("known_hosts install requires exact host key binding")
            argv += ["--host-public-key", payload["host_public_key"], "--fingerprint", payload["fingerprint"]]
        return argv + ["--receipt-file", "/proc/self/fd/3"]
    if verb == INSTALL_SERVICE_VERB:
        return service_asset_argv(role)
    if verb == "start":
        return compose + ["up", "--detach", "--no-deps", REMOTE_COMPOSE_SERVICE]
    if verb == "stop":
        return compose + ["down", "--remove-orphans"]
    if role == "head" and verb in {
        "arm-fallback",
        "refresh-fallback",
        "quiesce-legacy",
        "disarm-fallback",
        "cancel-fallback",
    }:
        if not transaction_id:
            raise ControllerError(f"{verb} requires a transaction ID")
        command = verb.removesuffix("-fallback").replace("quiesce-legacy", "quiesce")
        argv = [
            "sudo",
            "-n",
            "flock",
            "-x",
            FALLBACK_LIFECYCLE_LOCK,
            REMOTE_RECOVERY,
            command,
        ]
        if verb == "arm-fallback":
            if not isinstance(payload, dict):
                raise ControllerError("arm-fallback requires the exact fallback schema")
            return argv + ["--config-json", _compact_json(payload)]
        return argv + ["--transaction-id", transaction_id]
    if role == "head" and verb == "stage-gateway":
        return ["sudo", "-n", "systemctl", "start", "deepseek-v4-flash-gateway-staging.service"]
    if role == "head" and verb == "stop-staging-gateway":
        return ["sudo", "-n", "systemctl", "stop", "deepseek-v4-flash-gateway-staging.service"]
    if verb == "reboot":
        if not transaction_id:
            raise ControllerError("reboot requires a transaction ID")
        unit = f"deepseek-v4-flash-reboot-{role}-{transaction_id}"
        return [
            "sudo",
            "-n",
            "systemd-run",
            f"--unit={unit}",
            "--on-active=2s",
            "/usr/bin/systemctl",
            "reboot",
        ]
    if role == "head" and verb in {"cutover", "production-activate-gateway"}:
        if not transaction_id:
            raise ControllerError(f"{verb} requires a transaction ID")
        return [
            "sudo",
            "-n",
            REMOTE_RECOVERY,
            "gateway-start",
            "--transaction-id",
            transaction_id,
        ]
    if role == "head" and verb == "production-fence-gateway":
        container_id = (payload or {}).get("container_id", "")
        if not transaction_id or not re.fullmatch(r"[a-f0-9]{64}", container_id):
            raise ControllerError("gateway fence requires an exact container identity")
        return ["sudo", "-n", REMOTE_RECOVERY, "gateway-fence",
                "--transaction-id", transaction_id, "--container-id", container_id]
    if role == "head" and verb == "production-deactivate-gateway":
        if not transaction_id:
            raise ControllerError(f"{verb} requires a transaction ID")
        return [
            "sudo",
            "-n",
            REMOTE_RECOVERY,
            "gateway-stop",
            "--transaction-id",
            transaction_id,
        ]
    if role == "head" and verb == "rollback":
        return ["sudo", "-n", "systemctl", "start", "deepseek-v4-flash-fallback.service"]
    raise ControllerError(f"unsupported role/verb pair: {role}/{verb}")


def action_resource(role: str, verb: str) -> str:
    if verb in ARTIFACT_VERBS and role == ARTIFACT_VERBS[verb][0]:
        return f"{PROJECT}:{verb}"
    if verb in DUAL_ROLE_ARTIFACT_VERBS and role in HOSTS:
        return f"{PROJECT}:{verb}"
    if verb == INSTALL_SERVICE_VERB and role in HOSTS:
        return f"{PROJECT}:{verb}:{role}"
    if verb == QUALIFY_VERB and role == "head":
        return f"{PROJECT}:{verb}"
    if verb in TRUST_VERBS and role == TRUST_VERBS[verb]:
        return f"{PROJECT}:trust:{verb}"
    if verb in {"start", "stop"}:
        return PROJECT
    if role == "head" and verb in {
        "cutover", "production-activate-gateway", "production-deactivate-gateway", "production-fence-gateway"
    }:
        return "deepseek-v4-flash-gateway.service"
    if role == "head" and verb == "rollback":
        return "deepseek-v4-flash-fallback.service"
    if role == "head" and verb in {
        "arm-fallback",
        "refresh-fallback",
        "quiesce-legacy",
        "disarm-fallback",
        "cancel-fallback",
    }:
        return "/var/lib/deepseek-v4-flash-fallback"
    if role == "head" and verb in {"stage-gateway", "stop-staging-gateway"}:
        return "deepseek-v4-flash-gateway-staging.service"
    if verb == "reboot":
        return f"{HOSTS[role].hostname}:reboot"
    raise ControllerError(f"unsupported role/verb pair: {role}/{verb}")


def make_receipt(
    *,
    role: str,
    verb: str,
    resource: str,
    argv: list[str],
    prior_state_sha256: str,
    transaction_id: str,
    lease_generation: str,
    now: datetime | None = None,
) -> dict:
    if role not in HOSTS:
        raise ReceiptRejected(f"unknown role: {role}")
    if len(transaction_id) != 32 or any(
        character not in "0123456789abcdef" for character in transaction_id
    ):
        raise ReceiptRejected("transaction ID must be 32 lowercase hex characters")
    if len(prior_state_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in prior_state_sha256
    ):
        raise ReceiptRejected("prior-state digest must be SHA-256")
    if len(lease_generation) != 32 or any(
        character not in "0123456789abcdef" for character in lease_generation
    ):
        raise ReceiptRejected("lease generation must be 32 lowercase hex characters")
    issued = now or utc_now()
    return {
        "schema": "deepseek-dspark-receipt-v1",
        "transaction_id": transaction_id,
        "host": HOSTS[role].hostname,
        "role": role,
        "verb": verb,
        "resource": resource,
        "argv": argv,
        "prior_state_sha256": prior_state_sha256,
        "lease_generation": lease_generation,
        "nonce": uuid.uuid4().hex,
        "issued_at": iso(issued),
        "not_after": iso(issued + timedelta(seconds=RECEIPT_TTL_SECONDS)),
    }


class ClusterController:
    def __init__(self, *, authority: LockAuthority, runner, journal: Journal, task_id: str):
        self.authority = authority
        self.runner = runner
        self.journal = journal
        self.task_id = task_id
        self._heartbeat: Heartbeat | None = None
        self._last_release: dict | None = None
        self._pending_qualifications: list[dict] = []

    def status(self) -> dict:
        return {"lock": self.authority.status(), "project": PROJECT}

    def _ssh_argv(self, host: Host, remote_argv: list[str]) -> list[str]:
        return [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            "-o",
            "StrictHostKeyChecking=yes",
            f"operator@{host.address}",
            shlex.join(remote_argv),
        ]

    def _guard(self) -> dict:
        if self._heartbeat is not None:
            return self._heartbeat.require_live()
        return self.authority.guard()

    def _verify_target(self, role: str) -> None:
        host = HOSTS[role]
        result = self.runner.run(
            self._ssh_argv(host, ["sh", "-c", "hostname; whoami"]), timeout=15
        )
        if result.returncode != 0:
            raise TargetMismatch(f"cannot identify {role}: {result.stderr.strip()}")
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if lines != [host.hostname, "operator"]:
            raise TargetMismatch(
                f"target mismatch for {role}: expected {host.hostname}/operator, got {lines!r}"
            )

    def preflight(self) -> dict:
        lock = self.authority.status()
        if lock.get("state") != "UNLOCKED":
            raise LockUnavailable(f"preflight requires UNLOCKED, got {lock.get('state', 'UNKNOWN')}")
        for role in ("head", "worker"):
            self._verify_target(role)
        return {"lock": "UNLOCKED", "targets": {r: HOSTS[r].hostname for r in HOSTS}}

    def preflight_fabric(self) -> dict:
        lock_state = self.authority.status()
        if lock_state.get("state") != "UNLOCKED":
            raise LockUnavailable(
                f"fabric preflight requires UNLOCKED, got {lock_state.get('state', 'UNKNOWN')}"
            )
        fabric_lock = validate_fabric_lock()
        reports = self._probe_live_fabric(require_head_ssh=False)
        return {
            "lock": "UNLOCKED",
            "qualification_id": fabric_lock["qualification_id"],
            "selected_runtime_binding": fabric_lock["selected_runtime_binding"],
            "reports": reports,
            "network_or_firewall_rewritten": False,
            "dedicated_head_to_worker_ssh_observed": reports["head"].get(
                "head_to_worker"
            ),
            "production_image_two_rank_probe_required_before_start": True,
        }

    def _probe_live_fabric(self, *, require_head_ssh: bool) -> dict:
        reports = {}
        for role in ("head", "worker"):
            self._verify_target(role)
            result = self.runner.run(
                self._ssh_argv(HOSTS[role], ["python3", "-c", FABRIC_PROBE, role]),
                timeout=90,
            )
            report = parse_json_result(result, f"{role} live fabric probe")
            reports[role] = validate_live_fabric(role, report, require_head_ssh=require_head_ssh)
        return reports

    def _read_public_key(self, role: str, path: str) -> tuple[str, str]:
        self._guard()
        result = self.runner.run(
            self._ssh_argv(
                HOSTS[role],
                ["python3", "-c", "import base64,hashlib,json,pathlib,sys; p=pathlib.Path(sys.argv[1]); f=p.read_text().strip().split(); b=base64.b64decode(f[1],validate=True); print(json.dumps({'public_key':' '.join(f),'fingerprint':'SHA256:'+base64.b64encode(hashlib.sha256(b).digest()).decode().rstrip('=')}))", path],
            ),
            timeout=30,
        )
        value = parse_json_result(result, f"read {role} public key")
        public_key, fingerprint = value.get("public_key"), value.get("fingerprint")
        if not isinstance(public_key, str) or not public_key.startswith("ssh-ed25519 "):
            raise PlanGateError(f"{role} public key is not ssh-ed25519")
        if not isinstance(fingerprint, str) or not fingerprint.startswith("SHA256:"):
            raise PlanGateError(f"{role} public key omitted fingerprint")
        self._guard()
        return public_key, fingerprint

    def _verify_strict_head_worker_ssh(self) -> None:
        for address in ("192.0.2.31", "192.0.2.34", "192.0.2.36"):
            self._guard()
            remote = [
                "ssh", "-i", REMOTE_WORKER_KEY, "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=yes",
                "-o", f"UserKnownHostsFile={REMOTE_WORKER_KNOWN_HOSTS}",
                f"operator@{address}", "hostname; whoami",
            ]
            result = self.runner.run(self._ssh_argv(HOSTS["head"], remote), timeout=30)
            if result.returncode != 0 or [line.strip() for line in result.stdout.splitlines() if line.strip()] != ["dgx-worker", "operator"]:
                raise PlanGateError(f"strict head-to-worker SSH failed for {address}")
            self._guard()

    def _install_trust_under_active_lease(self, transaction: str) -> dict:
        self._mutate("head", "ensure-head-worker-key", transaction)
        public_key, key_fingerprint = self._read_public_key("head", f"{REMOTE_WORKER_KEY}.pub")
        worker = self._mutate("worker", "install-worker-authorized-key", transaction, {"public_key":public_key,"fingerprint":key_fingerprint})
        host_public_key, host_fingerprint = self._read_public_key("worker", "/etc/ssh/ssh_host_ed25519_key.pub")
        head = self._mutate("head", "install-head-worker-known-hosts", transaction, {"host_public_key":host_public_key,"fingerprint":host_fingerprint})
        self._verify_strict_head_worker_ssh()
        return {"worker":worker,"head":head,"key_fingerprint":key_fingerprint,"host_fingerprint":host_fingerprint}

    def qualify_fabric_consumer(self) -> dict:
        self.preflight_fabric()
        transaction = uuid.uuid4().hex
        with self._lease("qualify DeepSeek fabric consumer and dedicated SSH trust"):
            self._install_trust_under_active_lease(transaction)
            qualified = self._qualify_under_active_lease(transaction)
        return {"transaction_id": transaction, "receipt_sha256": qualified["source_receipt_sha256"], "qualified": qualified}

    def _read_fabric_receipt_pair(self) -> tuple[bytes, str, dict]:
        copies = []
        for role in ("head", "worker"):
            result = self.runner.run(
                self._ssh_argv(HOSTS[role], ["python3", "-c", "import base64,pathlib; print(base64.b64encode(pathlib.Path('" + REMOTE_FABRIC_RECEIPT + "').read_bytes()).decode())"]),
                timeout=30,
            )
            if result.returncode != 0:
                raise PlanGateError(f"{role} fabric consumer receipt is absent")
            try:
                copies.append(base64.b64decode(result.stdout.strip(), validate=True))
            except Exception as exc:
                raise PlanGateError(f"{role} fabric consumer receipt is unreadable") from exc
        if copies[0] != copies[1]:
            raise PlanGateError("fabric consumer receipt copies differ between nodes")
        try:
            value = json.loads(copies[0])
        except Exception as exc:
            raise PlanGateError("fabric consumer receipt is invalid JSON") from exc
        return copies[0], hashlib.sha256(copies[0]).hexdigest(), value

    def _qualification_release_event(self, receipt: dict, digest: str) -> dict:
        if not self.journal.path.is_file():
            raise PlanGateError("qualification has no immutable release correlation")
        candidates = [
            json.loads(line) for line in self.journal.path.read_text().splitlines() if line.strip()
        ]
        matches = [event for event in candidates if event.get("event") == "FABRIC_QUALIFICATION_RELEASED" and event.get("transaction_id") == receipt.get("transaction_id") and event.get("source_lease_generation") == receipt.get("lease_generation") and event.get("qualification_receipt_sha256") == digest]
        if len(matches) != 1:
            raise PlanGateError("qualification release correlation is absent or ambiguous")
        event = matches[0]
        path = Path(str(event.get("release_receipt", ""))).resolve(strict=False)
        expected = event.get("release_receipt_sha256")
        if not _is_within(path, LOCK_HISTORY) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise PlanGateError("qualification immutable release completion is invalid")
        try:
            completion = json.loads(path.read_text())
            completion_hash = completion.pop("payload_sha256")
            intent_path = Path(completion["intent_path"]).resolve(strict=False)
            intent = json.loads(intent_path.read_text())
            intent_hash = intent.pop("payload_sha256")
        except Exception as exc:
            raise PlanGateError("qualification release receipt structure is invalid") from exc
        canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        if (
            hashlib.sha256(canonical(completion)).hexdigest() != completion_hash
            or hashlib.sha256(canonical(intent)).hexdigest() != intent_hash
            or not _is_within(intent_path, LOCK_HISTORY)
            or hashlib.sha256(intent_path.read_bytes()).hexdigest() != completion.get("intent_sha256")
            or completion.get("schema_version") != 1
            or completion.get("phase") != "completion"
            or completion.get("operation_id") != event.get("release_operation_id")
            or completion.get("owner") != OWNER
            or completion.get("lease_generation") != receipt.get("lease_generation")
            or completion.get("terminal_state") != "UNLOCKED"
            or intent.get("schema_version") != 1
            or intent.get("phase") != "intent"
            or intent.get("operation_id") != completion.get("operation_id")
            or intent.get("owner") != OWNER
            or intent.get("lease_generation") != receipt.get("lease_generation")
        ):
            raise PlanGateError("qualification release intent/completion correlation is invalid")
        return event

    def _verify_fabric_consumer_receipts(self, current_generation: str, *, allow_current_unreleased: bool = False) -> dict:
        _, digest, receipt = self._read_fabric_receipt_pair()
        required = {"schema_version", "kind", "terminal", "transaction_id", "lease_generation", "cluster_id", "created_at", "qualification", "nodes", "ssh_trust", "df_jumbo", "historical_release", "runtime", "production_image_probe", "worker_http_listener_count", "network_or_firewall_rewritten", "replication"}
        if set(receipt) != required or receipt.get("schema_version") != 1 or receipt.get("kind") != "deepseek-v4-flash-fabric-consumer-receipt" or receipt.get("terminal") != "PASS" or receipt.get("cluster_id") != "dgx-head+dgx-worker" or receipt.get("worker_http_listener_count") != 0 or receipt.get("network_or_firewall_rewritten") is not False:
            raise PlanGateError("fabric consumer receipt schema or terminal state is invalid")
        if not all(isinstance(receipt.get(key), str) and len(receipt[key]) == 32 for key in ("transaction_id", "lease_generation")):
            raise PlanGateError("fabric consumer receipt operation binding is invalid")
        replication = receipt["replication"]
        body = dict(receipt); body.pop("replication")
        body_sha = hashlib.sha256(_compact_json(body).encode()).hexdigest()
        if replication != {"head_path": REMOTE_FABRIC_RECEIPT, "worker_path": REMOTE_FABRIC_RECEIPT, "canonical_body_sha256": body_sha}:
            raise PlanGateError("fabric consumer replication body hash is invalid")
        if not (allow_current_unreleased and receipt["lease_generation"] == current_generation):
            self._qualification_release_event(receipt, digest)
        reports = {}
        for role in ("head", "worker"):
            result = self.runner.run(self._ssh_argv(HOSTS[role], ["python3", "-c", FABRIC_PROBE, role]), timeout=90)
            reports[role] = validate_live_fabric(role, parse_json_result(result, f"{role} qualification closure"), require_head_ssh=True)
            node = receipt.get("nodes", {}).get(role, {})
            exact = {"hostname": reports[role]["hostname"], "management_ip": HOSTS[role].address, "boot_id": reports[role]["boot_id"], "kernel": reports[role]["kernel"], "driver": reports[role]["driver"], "netplan_sha256": reports[role]["netplan_sha256"]}
            if any(node.get(key) != value for key, value in exact.items()):
                raise PlanGateError(f"{role} qualification identity closure drifted")
        consumption = {"schema_version": 1, "kind": "deepseek-fabric-consumption", "source_transaction_id": receipt["transaction_id"], "source_lease_generation": receipt["lease_generation"], "source_receipt_sha256": digest, "current_start_lease_generation": current_generation, "boot_ids": {role: reports[role]["boot_id"] for role in reports}}
        output = self.journal.path.parent / f"fabric-consumption-{current_generation}.json"
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps(consumption, sort_keys=True) + "\n"); os.chmod(temporary, 0o600); os.replace(temporary, output)
        self.journal.append({"event": "FABRIC_QUALIFICATION_CONSUMED", **consumption, "path": str(output)})
        return consumption

    def _bootstrap_inspect(self, role: str) -> dict:
        result = self.runner.run(
            self._ssh_argv(
                HOSTS[role],
                ["python3", "-c", BOOTSTRAP_INSPECT, *BOOTSTRAP_SOURCES],
            ),
            timeout=30,
        )
        value = parse_json_result(result, f"inspect bootstrap executor on {role}")
        if set(value) != set(BOOTSTRAP_SOURCES):
            raise PlanGateError(f"{role} bootstrap inspection returned an unexpected scope")
        if any(item.get("symlink") is True for item in value.values()):
            raise PlanGateError(f"{role} bootstrap target contains a symlink")
        return value

    def _bootstrap_role(self, role: str, transaction_id: str, sources: dict[str, bytes]) -> dict:
        self._guard()
        self._verify_target(role)
        self._guard()
        before = self._bootstrap_inspect(role)
        self._guard()
        rows = []
        hashes = {}
        for destination, source in BOOTSTRAP_SOURCES.items():
            data = sources[destination]
            digest = hashlib.sha256(data).hexdigest()
            hashes[destination] = digest
            rows.append(
                {
                    "destination": destination,
                    "sha256": digest,
                    "content_base64": base64.b64encode(data).decode("ascii"),
                }
            )
        payload = json.dumps(
            {"schema": "deepseek-bootstrap-v1", "files": rows},
            sort_keys=True,
            separators=(",", ":"),
        )
        argv = self._ssh_argv(
            HOSTS[role],
            [
                "sudo",
                "-n",
                "python3",
                "-c",
                BOOTSTRAP_REMOTE,
                HOSTS[role].hostname,
                hashes[REMOTE_HELPER],
                hashes[REMOTE_PREPARE],
            ],
        )
        guard = self._guard()
        self.journal.append(
            {
                "event": "BOOTSTRAP_BEGIN",
                "transaction_id": transaction_id,
                "role": role,
                "lease_generation": guard["lease_generation"],
                "argv": argv,
                "stdin_sha256": hashlib.sha256(payload.encode()).hexdigest(),
                "source_sha256": hashes,
                "preimage": before,
            }
        )
        result = self.runner.run_supervised(
            argv,
            input_text=payload,
            timeout=180,
            lease_check=self._guard,
        )
        self._guard()
        terminal = parse_json_result(result, f"bootstrap executor on {role}")
        terminal_after = terminal.get("after")
        if not isinstance(terminal_after, dict):
            raise PlanGateError(f"{role} bootstrap terminal omitted its verified postimage")
        after = {
            destination: {"exists": True, "symlink": False, **terminal_after.get(destination, {})}
            for destination in BOOTSTRAP_SOURCES
        }
        for destination, digest in hashes.items():
            if after[destination] != {
                "exists": True,
                "symlink": False,
                "sha256": digest,
                "mode": "0755",
                "uid": 0,
                "gid": 0,
            }:
                raise PlanGateError(f"{role} bootstrap postimage verification failed")
        self._guard()
        self.journal.append(
            {
                "event": "BOOTSTRAP_TERMINAL",
                "transaction_id": transaction_id,
                "role": role,
                "terminal_sha256": hashlib.sha256(
                    json.dumps(terminal, sort_keys=True).encode()
                ).hexdigest(),
                "postimage": after,
            }
        )
        return {"before": before, "after": after}

    def bootstrap_executor(self, *, _under_active: bool = False) -> dict:
        sources = {}
        for destination, source in BOOTSTRAP_SOURCES.items():
            if not source.is_file() or source.is_symlink():
                raise PlanGateError(f"bootstrap source is absent or unsafe: {source}")
            if _is_within(source, REPO_ROOT):
                relative = source.resolve().relative_to(REPO_ROOT.resolve())
                tracked = self.runner.run(
                    ["git", "-C", str(REPO_ROOT), "ls-files", "--error-unmatch", str(relative)],
                    timeout=15,
                )
                clean = self.runner.run(
                    ["git", "-C", str(REPO_ROOT), "diff", "--quiet", "HEAD", "--", str(relative)],
                    timeout=15,
                )
                if tracked.returncode != 0 or clean.returncode != 0:
                    raise PlanGateError(f"bootstrap source is not the committed git preimage: {relative}")
            sources[destination] = source.read_bytes()
        transaction = uuid.uuid4().hex
        lease = contextlib.nullcontext() if _under_active else self._lease("bootstrap fixed receipt executors")
        with lease:
            worker = self._bootstrap_role("worker", transaction, sources)
            head = self._bootstrap_role("head", transaction, sources)
        return {"transaction_id": transaction, "worker": worker, "head": head}

    def bootstrap_input_bundle(self, *, _under_active: bool = False) -> dict:
        lock_path = DEFAULT_BOOTSTRAP_INPUT_LOCK
        try:
            raw_manifest = lock_path.read_bytes()
            if hashlib.sha256(raw_manifest).hexdigest() != BOOTSTRAP_INPUT_RAW_SHA256:
                raise PlanGateError("bootstrap input lock raw bytes drifted")
            manifest = json.loads(raw_manifest)
        except Exception as exc:
            raise PlanGateError(f"bootstrap input lock unavailable: {exc}") from exc
        manifest_sha = validate_bootstrap_input_manifest(manifest)
        relative = lock_path.resolve().relative_to(REPO_ROOT.resolve())
        tracked = self.runner.run(["git","-C",str(REPO_ROOT),"ls-files","--error-unmatch",str(relative)],timeout=15)
        clean = self.runner.run(["git","-C",str(REPO_ROOT),"diff","--quiet","HEAD","--",str(relative)],timeout=15)
        if tracked.returncode or clean.returncode:
            raise PlanGateError("bootstrap input lock is not the committed canonical authority")
        payloads = {}
        for role in ("worker", "head"):
            rows = []
            destinations = set()
            role_specs = [spec for spec in manifest["files"] if role in spec.get("roles", [])]
            for spec in role_specs:
                if set(spec) != {"path", "mode", "size", "sha256", "roles"} or not set(spec["roles"]) <= {"head", "worker"}:
                    raise PlanGateError(f"{role} bootstrap allowlist is malformed")
                destination = str(Path(REMOTE_INPUT_ROOT) / spec["path"])
                if destination in destinations: raise PlanGateError(f"{role} bootstrap destination duplicate")
                destinations.add(destination)
                source = (REPO_ROOT / spec["path"]).resolve(strict=False)
                if not _is_within(source, REPO_ROOT) or not source.is_file() or source.is_symlink():
                    raise PlanGateError(f"unsafe bootstrap source: {spec['path']}")
                data = source.read_bytes(); digest = hashlib.sha256(data).hexdigest()
                if digest != spec["sha256"] or len(data) != spec["size"]:
                    raise PlanGateError(f"bootstrap source drift: {spec['path']}")
                rows.append({"source_path": spec["path"], "destination": destination, "sha256": digest, "content_base64": base64.b64encode(data).decode()})
            payloads[role] = json.dumps(
                {
                    "manifest": manifest,
                    "role": role,
                    "files": rows,
                    "previous_manifest_sha256": "c645d0e0aed58822f25b698b40e93cb37145cea44fb27e968742b18381c27f38",
                    "previous_sha256_by_path": {
                        "scripts/recover-deepseek-v4-flash.py": "8e09c849ee32ead87417989a4f0713dc04af6db5a36faed9c4496cad3bc6a1bb",
                        "scripts/run-deepseek-v4-flash-dspark.sh": "e58404fd4a99907a81b1b8b126829c1cec54f1dbf4d624eb3ca9705eda025c6e",
                    },
                    "service_compose_sha256": "b9354ef64287b6da16d8ad35da76a38bc4b019c40df3dfdeb26a784f03587ff6",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        transaction = uuid.uuid4().hex
        results = {}
        lease = contextlib.nullcontext() if _under_active else self._lease("bootstrap exact hash-closed remote input bundle")
        with lease:
            for role in ("worker", "head"):
                self._guard(); self._verify_target(role); guard = self._guard()
                argv = self._ssh_argv(HOSTS[role], ["sudo", "-n", "python3", "-c", INPUT_BUNDLE_REMOTE, HOSTS[role].hostname, role, manifest_sha])
                self.journal.append({"event":"INPUT_BOOTSTRAP_BEGIN","transaction_id":transaction,"role":role,"lease_generation":guard["lease_generation"],"manifest_sha256":manifest_sha,"payload_sha256":hashlib.sha256(payloads[role].encode()).hexdigest(),"argv":argv})
                result = self.runner.run_supervised(argv, input_text=payloads[role], timeout=600, lease_check=self._guard)
                results[role] = parse_json_result(result, f"bootstrap inputs on {role}")
                self._guard(); self.journal.append({"event":"INPUT_BOOTSTRAP_TERMINAL","transaction_id":transaction,"role":role,"terminal":results[role].get("terminal"),"preimages":results[role].get("preimages"),"installed":results[role].get("installed")})
        return {"transaction_id":transaction,"manifest_sha256":manifest_sha,**results}

    def _pinned_source_snapshot(self) -> tuple[str, str]:
        root = PINNED_SOURCE_CHECKOUT
        checks = {
            "head": ["git", "-C", str(root), "rev-parse", "HEAD"],
            "tree": ["git", "-C", str(root), "rev-parse", "HEAD^{tree}"],
            "origin": ["git", "-C", str(root), "remote", "get-url", "origin"],
            "branch": ["git", "-C", str(root), "symbolic-ref", "-q", "HEAD"],
            "status": ["git", "-C", str(root), "status", "--porcelain=v1"],
            "ls": ["git", "-C", str(root), "ls-tree", "-r", "-z", "HEAD"],
        }
        values = {name:self.runner.run(argv, timeout=60) for name,argv in checks.items()}
        if values["head"].stdout.strip()!=PINNED_SOURCE_COMMIT or values["tree"].stdout.strip()!=PINNED_SOURCE_TREE or values["origin"].stdout.strip()!=PINNED_SOURCE_ORIGIN or values["branch"].returncode==0 or values["status"].stdout:
            raise PlanGateError("pinned upstream checkout identity/clean/detached gate failed")
        files=[]; total=0
        for record in values["ls"].stdout.split("\0"):
            if not record: continue
            meta,path=record.split("\t",1); mode,kind,blob=meta.split()
            if mode not in {"100644","100755"} or kind!="blob": raise PlanGateError("upstream tree contains unsupported entry")
            source=root/path
            if source.is_symlink() or not source.is_file(): raise PlanGateError("upstream worktree row unsafe")
            data=source.read_bytes(); total+=len(data)
            actual=self.runner.run(["git","-C",str(root),"hash-object","--",path],timeout=30)
            if actual.returncode or actual.stdout.strip()!=blob: raise PlanGateError("upstream worktree/blob drift")
            normalized_mode="0755" if mode=="100755" else "0644"
            files.append({"path":path,"git_mode":mode,"mode":normalized_mode,"size":len(data),"sha256":hashlib.sha256(data).hexdigest(),"blob":blob,"content_base64":base64.b64encode(data).decode()})
        if len(files)!=171 or total!=4293967: raise PlanGateError("upstream frozen tree inventory count/bytes drift")
        root_material="".join(f"{row['sha256']}  {row['mode']}  {row['size']}  {row['path']}\n" for row in files)
        root_sha=hashlib.sha256(root_material.encode()).hexdigest()
        if root_sha!="83bea7e3d33dfac0e778ea1bd2bb885429a6e51d9cab34e6130342f7a7e8d2c8": raise PlanGateError("upstream source root closure drift")
        body={"schema_version":1,"kind":"deepseek-pinned-source-snapshot","origin":PINNED_SOURCE_ORIGIN,"commit":PINNED_SOURCE_COMMIT,"tree":PINNED_SOURCE_TREE,"file_count":len(files),"total_bytes":total,"root_sha256":root_sha,"files":files}
        digest=hashlib.sha256(_compact_json(body).encode()).hexdigest(); body["snapshot_sha256"]=digest
        return json.dumps(body,sort_keys=True,separators=(",",":")),digest

    def install_pinned_source(self, *, _under_active: bool = False) -> dict:
        payload,digest=self._pinned_source_snapshot(); transaction=uuid.uuid4().hex
        lease = contextlib.nullcontext() if _under_active else self._lease("install verified pinned upstream source on head")
        with lease:
            self._guard(); self._verify_target("head"); guard=self._guard()
            argv=self._ssh_argv(HOSTS["head"],["sudo","-n","python3","-c",SOURCE_INSTALL_REMOTE,HOSTS["head"].hostname,digest])
            self.journal.append({"event":"SOURCE_INSTALL_BEGIN","transaction_id":transaction,"lease_generation":guard["lease_generation"],"snapshot_sha256":digest,"payload_sha256":hashlib.sha256(payload.encode()).hexdigest(),"argv":argv})
            result=self.runner.run_supervised(argv,input_text=payload,timeout=1200,lease_check=self._guard)
            value=parse_json_result(result,"install pinned source"); self._guard()
            self.journal.append({"event":"SOURCE_INSTALL_TERMINAL","transaction_id":transaction,"snapshot_sha256":digest,"terminal":value.get("terminal")})
        return {"transaction_id":transaction,"snapshot_sha256":digest,"result":value}

    @contextlib.contextmanager
    def _lease(self, purpose: str, lease_seconds: int = 1800):
        self.authority.acquire(self.task_id, purpose, lease_seconds=lease_seconds)
        self._pending_qualifications = []
        primary_error: BaseException | None = None
        try:
            with Heartbeat(self.authority) as heartbeat:
                self._heartbeat = heartbeat
                yield
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            self._heartbeat = None
            try:
                self._last_release = self.authority.release()
                if primary_error is None:
                    for pending in self._pending_qualifications:
                        self.journal.append({
                            "event":"FABRIC_QUALIFICATION_RELEASED",
                            **pending,
                            "release_operation_id":self._last_release.get("release_operation_id"),
                            "release_receipt":self._last_release.get("release_receipt"),
                            "release_receipt_sha256":self._last_release.get("release_receipt_sha256"),
                        })
            except BaseException as release_exc:
                self.journal.append(
                    {"event": "LOCK_RELEASE_FAILED", "error": str(release_exc), "purpose": purpose}
                )
                if primary_error is None:
                    raise
            finally:
                self._pending_qualifications = []

    def _inspect_state(self, role: str, verb: str) -> dict:
        host = HOSTS[role]
        result = self.runner.run(
            self._ssh_argv(
                host,
                [REMOTE_HELPER, "--inspect-state", "--role", role, "--verb", verb],
            ),
            timeout=30,
        )
        value = parse_json_result(result, f"inspect {role}")
        digest = value.get("prior_state_sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ReceiptRejected(f"{role} state inspection omitted a valid digest")
        return value

    def _capture_fallback(self, transaction_id: str) -> dict:
        self._guard()
        self._verify_target("head")
        self._guard()
        result = self.runner.run(
            self._ssh_argv(
                HOSTS["head"],
                [
                    REMOTE_HELPER,
                    "--capture-fallback",
                    "--role",
                    "head",
                    "--verb",
                    "rollback",
                ],
            ),
            timeout=30,
        )
        value = parse_json_result(result, "capture legacy preimage")
        value["transaction_id"] = transaction_id
        self._guard()
        return validate_fallback_config(value, transaction_id)

    def _load_fallback(self, path: Path, transaction_id: str) -> dict:
        state_path = assert_private_external_path(path, "fallback state")
        try:
            value = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise PlanGateError(f"cannot read fallback state: {exc}") from exc
        if not isinstance(value, dict):
            raise PlanGateError("fallback state is not a JSON object")
        return validate_fallback_config(value, transaction_id)

    def _run_harness(
        self,
        command: str,
        transaction_id: str,
        secret_file: Path,
        *,
        soak_hours: float | None = None,
        timeout: float = 1800,
    ) -> dict:
        secret = assert_private_external_path(secret_file, "gateway secret")
        if not secret.is_file() or stat.S_IMODE(secret.stat().st_mode) != 0o600:
            raise PlanGateError("gateway secret must be an external mode-0600 file")
        output_dir = self.journal.path.parent / "evidence" / transaction_id
        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(output_dir, 0o700)
        output = output_dir / f"{command}.json"
        argv = [
            sys.executable,
            str(HARNESS),
            command,
            "--base-url",
            "http://192.0.2.30:11434",
            "--secret-file",
            str(secret),
            "--output",
            str(output),
            "--timeout",
            str(int(timeout)),
        ]
        if soak_hours is not None:
            argv.extend(["--soak-hours", str(soak_hours)])
        tunnel = contextlib.nullcontext(None)
        if command == "context":
            tunnel = self._context_tokenizer_tunnel()
        self._guard()
        with tunnel as local_port:
            if local_port is not None:
                argv.extend(["--internal-base-url", f"http://127.0.0.1:{local_port}"])
            result = self.runner.run_supervised(
                argv,
                input_text=None,
                timeout=timeout + 30,
                lease_check=self._guard,
            )
        self._guard()
        value = parse_json_result(result, f"{command} production gate")
        if command == "gateway":
            if not output.is_file():
                raise PlanGateError("gateway gate omitted its evidence file")
            try:
                evidence = json.loads(output.read_text())
            except Exception as exc:
                raise PlanGateError("gateway evidence is invalid JSON") from exc
            logs = self.runner.run(
                self._ssh_argv(
                    HOSTS["head"],
                    ["sudo", "-n", "docker", "logs", "deepseek-v4-flash-gateway"],
                ),
                timeout=30,
            )
            if logs.returncode != 0:
                raise PlanGateError("cannot read transaction gateway access logs")
            evidence["result"] = validate_gateway_rejection_logs(
                evidence.get("result", {}), logs.stdout + logs.stderr
            )
            evidence["passed"] = evidence["result"]["passed"]
            output.write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n")
            value["passed"] = evidence["passed"]
        if value.get("passed") is not True:
            raise PlanGateError(f"{command} production gate did not pass")
        if not output.is_file():
            raise PlanGateError(f"{command} gate omitted its evidence file")
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        self.journal.append(
            {
                "event": "GATE_PASSED",
                "transaction_id": transaction_id,
                "gate": command,
                "evidence_sha256": digest,
            }
        )
        return value

    @contextlib.contextmanager
    def _context_tokenizer_tunnel(self):
        self._guard(); self._verify_target("head"); self._guard()
        reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
        reservation.close()
        argv = [
            "ssh", "-N", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "StrictHostKeyChecking=yes", "-o", "ExitOnForwardFailure=yes",
            "-L", f"127.0.0.1:{port}:127.0.0.1:18888", "operator@192.0.2.30",
        ]
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                self._guard()
                if process.poll() is not None:
                    raise PlanGateError(f"context SSH forward failed: {(process.stderr.read() if process.stderr else '').strip()}")
                probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM); probe.settimeout(0.2)
                try:
                    if probe.connect_ex(("127.0.0.1", port)) == 0:
                        break
                finally:
                    probe.close()
                time.sleep(0.1)
            else:
                raise OperationTimeout("context SSH forward did not become ready")
            self.journal.append({"event": "CONTEXT_TUNNEL_READY", "local_address": "127.0.0.1", "local_port": port, "target": "dgx-head:127.0.0.1:18888"})
            yield port
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=5)

    def _validate_clients_staged(self, secret_file: Path) -> str:
        secret = assert_private_external_path(secret_file, "gateway secret")
        if not secret.is_file() or stat.S_IMODE(secret.stat().st_mode) != 0o600:
            raise PlanGateError("managed client secret is absent or not mode 0600")
        try:
            manifest = json.loads(CONSUMER_MANIFEST.read_text(encoding="utf-8"))
        except Exception as exc:
            raise PlanGateError(f"consumer manifest unavailable: {exc}") from exc
        consumers = manifest.get("consumers")
        if manifest.get("schema_version") != 1 or not isinstance(consumers, list):
            raise PlanGateError("consumer manifest schema is invalid")
        identifiers = {entry.get("id") for entry in consumers if isinstance(entry, dict)}
        if identifiers != {"mac-codex", "repo-smoke", "windows-codex"}:
            raise PlanGateError("consumer manifest is not the frozen finite set")
        return hashlib.sha256(CONSUMER_MANIFEST.read_bytes()).hexdigest()

    def _boot_id(self, role: str) -> str:
        self._verify_target(role)
        result = self.runner.run(
            self._ssh_argv(HOSTS[role], ["cat", "/proc/sys/kernel/random/boot_id"]),
            timeout=15,
        )
        if result.returncode != 0:
            raise PlanGateError(f"cannot read {role} boot ID")
        boot_id = result.stdout.strip()
        if len(boot_id) != 36:
            raise PlanGateError(f"invalid {role} boot ID")
        return boot_id

    def _wait_reboot(self, role: str, old_boot_id: str, timeout: float = 900) -> str:
        deadline = time.monotonic() + timeout
        saw_down = False
        while time.monotonic() < deadline:
            result = self.runner.run(
                self._ssh_argv(
                    HOSTS[role],
                    ["sh", "-c", "hostname; whoami; cat /proc/sys/kernel/random/boot_id"],
                ),
                timeout=15,
            )
            if result.returncode != 0:
                saw_down = True
            else:
                lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
                if (
                    saw_down
                    and len(lines) == 3
                    and lines[:2] == [HOSTS[role].hostname, "operator"]
                    and lines[2] != old_boot_id
                ):
                    return lines[2]
            self._guard()
            time.sleep(5)
        raise OperationTimeout(f"{role} reboot did not complete with a new boot ID")

    def _qualify_under_active_lease(self, transaction_id: str) -> dict:
        generation = self._guard()["lease_generation"]
        self._verify_strict_head_worker_ssh()
        self._mutate("head", QUALIFY_VERB, transaction_id)
        _, digest, receipt = self._read_fabric_receipt_pair()
        if receipt.get("transaction_id") != transaction_id or receipt.get("lease_generation") != generation:
            raise PlanGateError("fabric qualification receipt is not bound to this active lease")
        pending = {
            "transaction_id":transaction_id,
            "source_lease_generation":generation,
            "qualification_receipt_sha256":digest,
        }
        if pending not in self._pending_qualifications:
            self._pending_qualifications.append(pending)
        return self._verify_fabric_consumer_receipts(
            generation, allow_current_unreleased=True
        )

    def _reboot_and_recover(self, role: str, transaction_id: str, refresher: FallbackRefresher) -> str:
        old_boot_id = self._boot_id(role)
        if role == "head":
            self._mutate("head", "refresh-fallback", transaction_id)
            refresher.pause_declared_head_reboot()
        self._mutate(role, "reboot", transaction_id)
        new_boot_id = self._wait_reboot(role, old_boot_id)
        if role == "head":
            self._mutate("head", "refresh-fallback", transaction_id)
            refresher.resume_after_head_recovery()
        # Production-image qualification owns both GPUs/fabric.  Quiesce the
        # exact project on both nodes before probing, regardless of reboot role.
        self._mutate("head", "stop", transaction_id)
        self._mutate("worker", "stop", transaction_id)
        self._qualify_under_active_lease(transaction_id)
        self._mutate("worker", "start", transaction_id)
        self._mutate("head", "start", transaction_id)
        self._mutate("head", "cutover", transaction_id)
        return new_boot_id

    def _mutate(
        self,
        role: str,
        verb: str,
        transaction_id: str,
        payload: dict | None = None,
        secret_file: Path | None = None,
    ) -> dict:
        guard = self._guard()
        self._verify_target(role)
        self._guard()
        prior = self._inspect_state(role, verb)
        guard = self._guard()
        exact_argv = action_argv(role, verb, transaction_id, payload)
        receipt = make_receipt(
            role=role,
            verb=verb,
            resource=action_resource(role, verb),
            argv=exact_argv,
            prior_state_sha256=prior["prior_state_sha256"],
            transaction_id=transaction_id,
            lease_generation=str(guard.get("lease_generation", "")),
        )
        self.journal.append(
            {
                "event": "RECEIPT_ISSUED",
                "transaction_id": transaction_id,
                "nonce": receipt["nonce"],
                "role": role,
                "verb": verb,
                "prior_state_sha256": prior["prior_state_sha256"],
            }
        )
        host = HOSTS[role]
        receipt_json = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
        helper_mode = "--receipt-stdin"
        input_text = receipt_json + "\n"
        if secret_file is not None:
            if verb != INSTALL_SERVICE_VERB or role != "head":
                raise ReceiptRejected("secret FD is restricted to head service asset install")
            secret_path = assert_private_external_path(secret_file, "gateway secret")
            if not secret_path.is_file() or stat.S_IMODE(secret_path.stat().st_mode) != 0o600:
                raise ReceiptRejected("gateway secret must be an external mode-0600 file")
            token = secret_path.read_text(encoding="utf-8").strip()
            if len(token) < 43 or any(
                character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
                for character in token
            ):
                raise ReceiptRejected("gateway secret is not a bounded base64url bearer")
            helper_mode = "--receipt-secret-stdin"
            input_text = f"{len(receipt_json.encode('utf-8'))}\n{receipt_json}{token}"
        result = self.runner.run_supervised(
            self._ssh_argv(
                host,
                [REMOTE_HELPER, helper_mode, "--role", role, "--verb", verb],
            ),
            input_text=input_text,
            timeout=(
                ARTIFACT_VERBS[verb][1]
                if verb in ARTIFACT_VERBS
                else DUAL_ROLE_ARTIFACT_VERBS[verb]
                if verb in DUAL_ROLE_ARTIFACT_VERBS
                else 1800
                if verb == QUALIFY_VERB
                else 600
                if verb == INSTALL_SERVICE_VERB
                else 1200
                if verb == "start" and role == "head"
                else 360
                if verb == "rollback"
                else 1300
                if verb == "production-fence-gateway"
                else 180
            ),
            lease_check=self._guard,
        )
        self._guard()
        value = parse_json_result(result, f"{verb} {role}")
        if value.get("terminal") not in {"succeeded", "already-satisfied"}:
            raise ReceiptRejected(f"{verb} {role} omitted a successful terminal receipt")
        self.journal.append(
            {
                "event": "RECEIPT_TERMINAL",
                "transaction_id": transaction_id,
                "nonce": receipt["nonce"],
                "role": role,
                "verb": verb,
                "terminal": value["terminal"],
            }
        )
        if verb == "start":
            evidence = value.get("evidence_sha256")
            if not isinstance(evidence, str) or len(evidence) != 64:
                raise ReceiptRejected(f"{verb} {role} omitted READY primary evidence")
            self.journal.append(
                {
                    "event": "READY",
                    "transaction_id": transaction_id,
                    "role": role,
                    "primary_evidence_sha256": evidence,
                }
            )
        return value

    def start(
        self, transaction_id: str | None = None, *, _under_active: bool = False
    ) -> dict:
        transaction = transaction_id or uuid.uuid4().hex
        lease = (
            contextlib.nullcontext()
            if _under_active
            else self._lease("start paired DeepSeek ranks worker-first")
        )
        with lease:
            generation = self._guard()["lease_generation"]
            qualification = self._verify_fabric_consumer_receipts(generation)
            try:
                worker = self._mutate("worker", "start", transaction)
                head = self._mutate("head", "start", transaction)
            except BaseException:
                # A failed up/readiness operation can still have created exact
                # project containers. Clean only this project, head then worker.
                for role in ("head", "worker"):
                    try:
                        self._mutate(role, "stop", transaction)
                    except BaseException as cleanup_error:
                        self.journal.append(
                            {
                                "event": "START_CLEANUP_FAILED",
                                "transaction_id": transaction,
                                "role": role,
                                "error": str(cleanup_error),
                            }
                        )
                raise
        return {"transaction_id": transaction, "qualification": qualification, "worker": worker, "head": head}

    def artifact(self, verb: str) -> dict:
        if verb not in ARTIFACT_VERBS:
            raise PlanGateError(f"artifact verb is not registered: {verb}")
        if verb == "record-license-acceptance":
            raise PlanGateError("license acceptance requires the explicit dedicated command")
        role = ARTIFACT_VERBS[verb][0]
        transaction = uuid.uuid4().hex
        with self._lease(f"artifact preparation: {verb}"):
            result = self._mutate(role, verb, transaction)
        return {"transaction_id": transaction, "role": role, "verb": verb, "result": result}

    def record_license_acceptance(self, operator: str, accepted: bool) -> dict:
        if operator != "operator" or accepted is not True:
            raise PlanGateError("license acceptance requires --operator operator --accept-responsible-use")
        transaction=uuid.uuid4().hex
        with self._lease("record explicit DeepSeek responsible-use acceptance"):
            result=self._mutate("head","record-license-acceptance",transaction)
        return {"transaction_id":transaction,"operator":operator,"result":result}

    def record_node_identities(self) -> dict:
        transaction = uuid.uuid4().hex
        with self._lease("record exact worker then head node identity closures"):
            worker = self._mutate("worker", "record-node-identity", transaction)
            head = self._mutate("head", "record-node-identity", transaction)
        return {"transaction_id":transaction,"worker":worker,"head":head}

    def prepare_artifacts(
        self, *, gateway_secret: Path, operator: str, accepted: bool
    ) -> dict:
        if operator != "operator" or accepted is not True:
            raise PlanGateError("prepare-artifacts requires explicit responsible-use acceptance")
        results = {"preflight": self.preflight_fabric()}
        secret = self._ensure_gateway_secret(gateway_secret)
        transaction = uuid.uuid4().hex
        with self._lease("prepare complete DeepSeek two-node artifact chain", lease_seconds=21600):
            results["bootstrap_executor"] = self.bootstrap_executor(_under_active=True)
            results["bootstrap_inputs"] = self.bootstrap_input_bundle(_under_active=True)
            results["source"] = self.install_pinned_source(_under_active=True)
            results["trust"] = self._install_trust_under_active_lease(transaction)
            for verb in (
                "finalize-model-lock", "copy-model", "acquire-images",
                "save-runtime-archive", "copy-runtime-archive", "load-runtime-archive",
                "apply-runtime-patch", "copy-patched-source", "install-integration-artifacts",
            ):
                if verb in {"copy-model","copy-runtime-archive","copy-patched-source"}:
                    self._guard(); self._probe_live_fabric(require_head_ssh=True); self._guard()
                role=ARTIFACT_VERBS[verb][0]
                results[verb]=self._mutate(role,verb,transaction)
            results["license"] = self._mutate("head","record-license-acceptance",transaction)
            results["identity_worker"] = self._mutate("worker","record-node-identity",transaction)
            results["identity_head"] = self._mutate("head","record-node-identity",transaction)
            results["finalize-deployment-lock"] = self._mutate("head","finalize-deployment-lock",transaction)
            results["service_assets_worker"] = self._mutate("worker",INSTALL_SERVICE_VERB,transaction)
            results["service_assets_head"] = self._mutate("head",INSTALL_SERVICE_VERB,transaction,secret_file=secret)
            self._guard(); self._probe_live_fabric(require_head_ssh=True); self._guard()
            results["fabric_qualification"] = self._qualify_under_active_lease(transaction)
        results["transaction_id"] = transaction
        return results

    def _ensure_gateway_secret(self, path: Path = DEFAULT_GATEWAY_SECRET) -> Path:
        secret_path = assert_private_external_path(path, "gateway secret")
        secret_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(secret_path.parent, 0o700)
        if not secret_path.exists():
            fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, (secrets.token_urlsafe(48) + "\n").encode())
                os.fsync(fd)
            finally:
                os.close(fd)
        if secret_path.is_symlink() or stat.S_IMODE(secret_path.stat().st_mode) != 0o600:
            raise ControllerError("gateway secret path is unsafe or not mode 0600")
        return secret_path

    def install_service_assets(
        self,
        gateway_secret: Path = DEFAULT_GATEWAY_SECRET,
        *,
        _under_active: bool = False,
    ) -> dict:
        secret = self._ensure_gateway_secret(gateway_secret)
        transaction = uuid.uuid4().hex
        lease = (
            contextlib.nullcontext()
            if _under_active
            else self._lease("install exact DeepSeek service assets worker then head")
        )
        with lease:
            worker = self._mutate("worker", INSTALL_SERVICE_VERB, transaction)
            head = self._mutate(
                "head", INSTALL_SERVICE_VERB, transaction, secret_file=secret
            )
        return {"transaction_id": transaction, "worker": worker, "head": head}

    def stop(
        self, transaction_id: str | None = None, *, _under_active: bool = False
    ) -> dict:
        transaction = transaction_id or uuid.uuid4().hex
        lease = (
            contextlib.nullcontext()
            if _under_active
            else self._lease("stop paired DeepSeek ranks head-first")
        )
        with lease:
            head = self._mutate("head", "stop", transaction)
            worker = self._mutate("worker", "stop", transaction)
        return {"transaction_id": transaction, "head": head, "worker": worker}

    def production_activate_gateway(
        self, transaction_id: str, *, _under_active: bool = False
    ) -> dict:
        lease = (
            contextlib.nullcontext()
            if _under_active
            else self._lease("activate DeepSeek production gateway")
        )
        with lease:
            result = self._mutate(
                "head", "production-activate-gateway", transaction_id
            )
        return {"transaction_id": transaction_id, "head": result}

    def production_fence_gateway(self, transaction_id: str, container_id: str,
                                 *, _under_active: bool = False) -> dict:
        lease = contextlib.nullcontext() if _under_active else self._lease("fence DeepSeek admission")
        with lease:
            result = self._mutate("head", "production-fence-gateway", transaction_id,
                                  {"container_id": container_id})
        return {"transaction_id": transaction_id, "head": result}

    def production_deactivate_gateway(
        self, transaction_id: str, *, _under_active: bool = False
    ) -> dict:
        lease = (
            contextlib.nullcontext()
            if _under_active
            else self._lease("deactivate DeepSeek production gateway")
        )
        with lease:
            result = self._mutate(
                "head", "production-deactivate-gateway", transaction_id
            )
        return {"transaction_id": transaction_id, "head": result}

    def cutover(
        self,
        fallback_state: Path | None,
        gateway_secret: Path = DEFAULT_GATEWAY_SECRET,
    ) -> dict:
        external_state = None
        if fallback_state is None:
            transaction = uuid.uuid4().hex
        else:
            state_path = assert_private_external_path(fallback_state, "fallback state")
            try:
                external_state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise PlanGateError(f"cannot read fallback state: {exc}") from exc
            if not isinstance(external_state, dict):
                raise PlanGateError("fallback state is not a JSON object")
            transaction = str(external_state.get("transaction_id", ""))
            external_state = validate_fallback_config(external_state, transaction)
        machine = CutoverStateMachine(self.journal, transaction)
        with self._lease("transactional DeepSeek production cutover"):
            state = (
                self._capture_fallback(transaction)
                if fallback_state is None
                else external_state
            )
            assert isinstance(state, dict)
            self._mutate("head", "stage-gateway", transaction)
            machine.advance("PREPARED")
            self._mutate("head", "arm-fallback", transaction, state)
            machine.advance("FALLBACK_ARMED")
            with FallbackRefresher(
                lambda: self._mutate("head", "refresh-fallback", transaction)
            ) as refresher:
                self._mutate("head", "quiesce-legacy", transaction)
                machine.advance("OLD_STOPPED")
                self._mutate("head", "stop-staging-gateway", transaction)
                self._mutate("head", "cutover", transaction)
                machine.advance("NEW_BOUND")
                self._run_harness("gateway", transaction, gateway_secret)
                self._run_harness("scenarios", transaction, gateway_secret, timeout=1800)
                self._run_harness("context", transaction, gateway_secret, timeout=7200)
                self._run_harness("performance", transaction, gateway_secret, timeout=1800)
                machine.advance("NEW_SMOKE_PASSED")
                consumer_sha = self._validate_clients_staged(gateway_secret)
                machine.advance("CLIENTS_STAGED", consumer_manifest_sha256=consumer_sha)
                self._run_harness(
                    "soak", transaction, gateway_secret, soak_hours=0.5, timeout=1900
                )
                refresher.require_live()
                machine.advance("PRODUCTION_MONITOR_PASSED")
                worker_boot = self._reboot_and_recover("worker", transaction, refresher)
                self._run_harness("gateway", transaction, gateway_secret)
                machine.advance("WORKER_REBOOT_PASSED", boot_id=worker_boot)
                head_boot = self._reboot_and_recover("head", transaction, refresher)
                self._run_harness("gateway", transaction, gateway_secret)
                machine.advance("HEAD_REBOOT_PASSED", boot_id=head_boot)
                self._run_harness("codex", transaction, gateway_secret)
                self._run_harness("responses", transaction, gateway_secret)
                machine.advance("CLIENTS_VERIFIED")
                self._guard()
                refresher.require_live()
                machine.advance("COMMITTING")
                self._mutate("head", "disarm-fallback", transaction)
                machine.advance("FALLBACK_DISARMED")
                machine.advance("VERIFIED")
        return {"transaction_id": transaction, "states": list(CUTOVER_STATES)}

    def rollback(self, transaction_id: str | None = None) -> dict:
        transaction = transaction_id or self.journal.latest_open_cutover()
        worker_error = None
        with self._lease("restore local legacy production service"):
            # The head-local fallback is deliberately first and independent of worker reachability.
            head = self._mutate("head", "rollback", transaction)
            self._mutate("head", "cancel-fallback", transaction)
            try:
                worker = self._mutate("worker", "stop", transaction)
            except ControllerError as exc:
                worker = None
                worker_error = str(exc)
                self.journal.append(
                    {
                        "event": "WORKER_CLEANUP_DEFERRED",
                        "transaction_id": transaction,
                        "error": worker_error,
                    }
                )
        return {
            "transaction_id": transaction,
            "head": head,
            "worker": worker,
            "worker_cleanup_deferred": worker_error,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", default=os.environ.get("CODEX_THREAD_ID", "manual-deepseek"))
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("preflight")
    sub.add_parser("preflight-fabric")
    sub.add_parser("qualify-fabric-consumer")
    sub.add_parser("bootstrap-executor")
    sub.add_parser("bootstrap-input-bundle")
    sub.add_parser("install-pinned-source")
    sub.add_parser("record-node-identities")
    license_accept = sub.add_parser("record-license-acceptance")
    license_accept.add_argument("--operator", required=True)
    license_accept.add_argument("--accept-responsible-use", action="store_true")
    prepare_all = sub.add_parser("prepare-artifacts")
    prepare_all.add_argument("--gateway-secret", type=Path, default=DEFAULT_GATEWAY_SECRET)
    prepare_all.add_argument("--operator", required=True)
    prepare_all.add_argument("--accept-responsible-use", action="store_true")
    sub.add_parser("start")
    sub.add_parser("stop")
    artifact = sub.add_parser("artifact")
    artifact.add_argument("verb", choices=tuple(ARTIFACT_VERBS))
    install_assets = sub.add_parser("install-service-assets")
    install_assets.add_argument("--gateway-secret", type=Path, default=DEFAULT_GATEWAY_SECRET)
    cutover = sub.add_parser("cutover")
    cutover.add_argument("--fallback-state", type=Path)
    cutover.add_argument("--gateway-secret", type=Path, default=DEFAULT_GATEWAY_SECRET)
    rollback = sub.add_parser("rollback")
    rollback.add_argument("--transaction-id")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        runner = SubprocessRunner()
        authority = LockAuthority(runner=runner, token_file=args.token_file)
        cluster = ClusterController(
            authority=authority,
            runner=runner,
            journal=Journal(args.journal),
            task_id=args.task_id,
        )
        if args.command == "cutover":
            result = cluster.cutover(args.fallback_state, args.gateway_secret)
        elif args.command == "rollback":
            result = cluster.rollback(args.transaction_id)
        elif args.command == "artifact":
            result = cluster.artifact(args.verb)
        elif args.command == "install-service-assets":
            result = cluster.install_service_assets(args.gateway_secret)
        elif args.command == "preflight-fabric":
            result = cluster.preflight_fabric()
        elif args.command == "qualify-fabric-consumer":
            result = cluster.qualify_fabric_consumer()
        elif args.command == "bootstrap-executor":
            result = cluster.bootstrap_executor()
        elif args.command == "bootstrap-input-bundle":
            result = cluster.bootstrap_input_bundle()
        elif args.command == "install-pinned-source":
            result = cluster.install_pinned_source()
        elif args.command == "record-node-identities":
            result = cluster.record_node_identities()
        elif args.command == "record-license-acceptance":
            result = cluster.record_license_acceptance(args.operator, args.accept_responsible_use)
        elif args.command == "prepare-artifacts":
            result = cluster.prepare_artifacts(gateway_secret=args.gateway_secret, operator=args.operator, accepted=args.accept_responsible_use)
        else:
            result = getattr(cluster, args.command)()
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except ControllerError as exc:
        print(f"deepseek-controller: {exc}", file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
