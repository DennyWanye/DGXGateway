#!/usr/bin/env bash
set -euo pipefail

# The shell entrypoint keeps deployment wiring simple; all parsing and closure
# validation uses only the Python standard library and fails closed.
exec python3 - "$0" "$@" <<'PY'
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import signal
import shutil
import struct
import subprocess
import sys
import tempfile
import time


RUNTIME_REFERENCE = "ghcr.io/anemll/dspark-vllm-gx10@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8"
RUNTIME_MANIFEST = "sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8"
RUNTIME_CONFIG = "sha256:3430d6614a8e2925f34d059af6caf05aff42387326db4d05639a60f10f2654d8"
RUNTIME_COMPRESSED_BYTES = 9787492120
GATEWAY_REFERENCE = "docker.io/library/nginx@sha256:77d740efa8f9c4753f2a7212d8422b8c77411681971f400ea03d07fe38476cac"
GATEWAY_MANIFEST = "sha256:77d740efa8f9c4753f2a7212d8422b8c77411681971f400ea03d07fe38476cac"
GATEWAY_CONFIG = "sha256:35f3cbee4fb77c3efb39f2723a21ce181906139442a37de8ffc52d89641d9936"
GATEWAY_COMPRESSED_BYTES = 22924818
VPN_RX_BUDGET = 12000000000
APPROVAL_SHA256 = "069cac019d68a3ee4d4fe1e4d173825b62910d8148589efde0dcace5e984fd28"
DOCKER_COMMAND = ["/usr/bin/sudo", "-n", "/usr/bin/docker"]
RSYNC_COMMAND = "/usr/bin/rsync"
WORKER_SSH = "/usr/bin/ssh -i /home/operator/.ssh/deepseek-v4-flash-worker -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/home/operator/.ssh/deepseek-v4-flash-worker-known_hosts"
MODEL_DESTINATION = "operator@192.0.2.34:/home/operator/dgx-models/deepseek-v4-flash-ablit-anchored/"
MODEL_LOCK_HEAD_PATH = "/home/operator/.local/state/deepseek-v4-flash-dspark/artifacts/model.lock.json"
MODEL_LOCK_WORKER_PATH = "/home/operator/.local/state/deepseek-v4-flash-dspark/artifacts/model.lock.json"
ARCHIVE_DESTINATION = "operator@192.0.2.34:/home/operator/dgx-models/deepseek-v4-flash-dspark/artifacts/dspark-vllm-gx10-a83948492cf13df4.tar"
ARCHIVE_RECEIPT_DESTINATION = "operator@192.0.2.34:/home/operator/dgx-models/deepseek-v4-flash-dspark/artifacts/dspark-vllm-gx10-a83948492cf13df4.tar.receipt.json"
ARCHIVE_DIRECTORY_DESTINATION = "operator@192.0.2.34:/home/operator/dgx-models/deepseek-v4-flash-dspark/artifacts/"
ARCHIVE_WORKER_PATH = "/home/operator/dgx-models/deepseek-v4-flash-dspark/artifacts/dspark-vllm-gx10-a83948492cf13df4.tar"
INSTALL_PATH = "/usr/local/libexec/prepare-deepseek-v4-flash-dspark.sh"
INPUT_ROOT = "/opt/deepseek-v4-flash-dspark"
BOOTSTRAP_LOCK_RELATIVE = "manifests/deepseek-v4-flash-bootstrap-input.lock.json"
BOOTSTRAP_SOURCE_DESTINATION = "/home/operator/dgx-models/deepseek-v4-flash-dspark/source"
BOOTSTRAP_SOURCE_ROOT = "83bea7e3d33dfac0e778ea1bd2bb885429a6e51d9cab34e6130342f7a7e8d2c8"
HEAD_BOOTSTRAP_FILES = (
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
WORKER_BOOTSTRAP_FILES = frozenset((
    "configs/deepseek-v4-flash-dspark.env.example",
    "manifests/deepseek-v4-flash-dspark.lock.json",
    "scripts/deepseek-v4-flash-nccl-probe.py",
    "scripts/prepare-deepseek-v4-flash-dspark.sh",
    "scripts/run-deepseek-v4-flash-dspark.sh",
    "systemd/deepseek-v4-flash-dspark.service",
))
SERVICE_UNITS = {
    "head": [
        "deepseek-v4-flash-dspark.service",
        "deepseek-v4-flash-gateway.service",
        "deepseek-v4-flash-gateway-staging.service",
        "deepseek-v4-flash-fallback.service",
        "deepseek-v4-flash-fallback.timer",
    ],
    "worker": ["deepseek-v4-flash-dspark.service"],
}
FABRIC_INTERFACES = ("enp1s0f0np0", "enP2p1s0f0np0")
FABRIC_HCAS = ("rocep1s0f0", "roceP2p1s0f0")
FABRIC_SSH = [
    "/usr/bin/ssh", "-i", "/home/operator/.ssh/deepseek-v4-flash-worker",
    "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
    "-o", "UserKnownHostsFile=/home/operator/.ssh/deepseek-v4-flash-worker-known_hosts",
    "operator@192.0.2.34",
]
WORKER_SOURCE_PROMOTION_SCRIPT = r'''
import ctypes,errno,hashlib,json,os,pathlib,shutil,stat,sys
operation, staging_raw, destination_raw, hook, inode_token, boundary_raw = sys.argv[1:]
staging=pathlib.Path(staging_raw) if staging_raw != '-' else None
destination=pathlib.Path(destination_raw)
boundary=pathlib.Path(boundary_raw)
expected=json.load(sys.stdin)
uid=os.getuid(); gid=os.getgid()
def fail(message): raise SystemExit(message)
def validate_ancestors():
    if staging is not None and staging.parent != destination.parent: fail('worker source staging/destination parent mismatch')
    try: relative=destination.parent.relative_to(boundary)
    except ValueError: fail('worker source destination escapes its fixed boundary')
    paths=[boundary]
    current=boundary
    for part in relative.parts: current=current/part; paths.append(current)
    for index,path in enumerate(paths):
        try: info=os.lstat(path)
        except FileNotFoundError: fail('worker source fixed ancestor is absent')
        expected_uid=0 if boundary==pathlib.Path('/home') and index==0 else uid
        expected_gid=0 if boundary==pathlib.Path('/home') and index==0 else gid
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid!=expected_uid or info.st_gid!=expected_gid or
                stat.S_IMODE(info.st_mode)&0o022): fail('worker source fixed ancestor owner/mode is unsafe')
def parse_inode():
    try: dev,ino=(int(value) for value in inode_token.split(':',1))
    except (ValueError,AttributeError): fail('worker source transaction inode token is invalid')
    if dev<0 or ino<=0: fail('worker source transaction inode token is invalid')
    return dev,ino
def verify_tree(root):
    try: root_info=os.lstat(root)
    except FileNotFoundError: fail('worker source tree is missing')
    if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid!=uid or root_info.st_gid!=gid or stat.S_IMODE(root_info.st_mode)!=0o755:
        fail('worker source root owner/mode drift')
    rows=expected.get('files')
    if not isinstance(rows,list) or expected.get('file_count')!=len(rows): fail('worker source identity row count drift')
    identity_path=root/'.deepseek-source-identity.json'
    if identity_path.is_symlink() or not identity_path.is_file(): fail('worker source identity is missing or unsafe')
    identity_info=os.lstat(identity_path)
    if identity_info.st_uid!=uid or identity_info.st_gid!=gid or stat.S_IMODE(identity_info.st_mode)!=0o644:
        fail('worker source identity owner/mode drift')
    identity_data=(json.dumps(expected,indent=2,sort_keys=True)+'\n').encode()
    try: observed_identity_data=identity_path.read_bytes()
    except OSError: fail('worker source identity readback failed')
    if observed_identity_data!=identity_data or identity_info.st_size!=len(identity_data):
        fail('worker source identity size/hash/content drift')
    expected_files={identity_path}; expected_dirs={root}; total=0; material=''
    for row in rows:
        relative=pathlib.PurePosixPath(row.get('path',''))
        if relative.is_absolute() or not relative.parts or '..' in relative.parts or row.get('mode') not in ('0644','0755'):
            fail('worker source identity contains an unsafe row')
        path=root.joinpath(*relative.parts)
        if path.is_symlink() or not path.is_file(): fail('worker source file is missing or unsafe')
        info=os.lstat(path); size=info.st_size
        if (info.st_uid!=uid or info.st_gid!=gid or stat.S_IMODE(info.st_mode)!=int(row['mode'],8) or
                size!=row.get('size') or hashlib.sha256(path.read_bytes()).hexdigest()!=row.get('sha256')):
            fail('worker source file owner/mode/size/hash drift')
        total+=size; material+=f"{row['sha256']}  {row['mode']}  {row['size']}  {row['path']}\n"
        expected_files.add(path); parent=path.parent
        while parent!=root: expected_dirs.add(parent); parent=parent.parent
    entries=set(root.rglob('*'))
    if any(path.is_symlink() or (not path.is_file() and not path.is_dir()) for path in entries):
        fail('worker source tree contains an unsafe entry')
    actual_files={path for path in entries if path.is_file()}; actual_dirs={root}|{path for path in entries if path.is_dir()}
    if (actual_files!=expected_files or actual_dirs!=expected_dirs or total!=expected.get('total_bytes') or
            hashlib.sha256(material.encode()).hexdigest()!=expected.get('root_sha256')):
        fail('worker source exact-set/root closure drift')
    for directory in actual_dirs:
        info=os.lstat(directory)
        if info.st_uid!=uid or info.st_gid!=gid or stat.S_IMODE(info.st_mode)!=0o755:
            fail('worker source directory owner/mode drift')
    return (root_info.st_dev,root_info.st_ino)
def rename_noreplace(source,target):
    libc=ctypes.CDLL(None,use_errno=True); renameat2=getattr(libc,'renameat2',None)
    if renameat2 is None: fail('renameat2 no-replace primitive unavailable')
    renameat2.argtypes=(ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint); renameat2.restype=ctypes.c_int
    if renameat2(-100,os.fsencode(source),-100,os.fsencode(target),1)!=0:
        error=ctypes.get_errno()
        if error==errno.EEXIST: raise FileExistsError('worker source target appeared during no-replace promotion')
        raise OSError(error,os.strerror(error),str(target))
def remove_owned(path,identity):
    try: info=os.lstat(path)
    except FileNotFoundError: return 'absent'
    if not stat.S_ISDIR(info.st_mode) or (info.st_dev,info.st_ino)!=identity: return 'foreign-preserved'
    shutil.rmtree(path); return 'removed'
validate_ancestors()
if operation=='inspect':
    if not destination.exists() and not destination.is_symlink(): print('MISSING'); raise SystemExit(0)
    verify_tree(destination); print('IDENTICAL'); raise SystemExit(0)
if operation=='reserve':
    if staging is None: fail('worker source reserve omitted staging')
    try: staging.mkdir(mode=0o755)
    except FileExistsError: fail('worker source transaction staging appeared before reserve')
    info=os.lstat(staging)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=uid or info.st_gid!=gid or stat.S_IMODE(info.st_mode)!=0o755:
        fail('worker source reserved staging owner/mode drift')
    print(f'{info.st_dev}:{info.st_ino}'); raise SystemExit(0)
if operation=='cleanup':
    if staging is None: fail('worker source cleanup omitted staging')
    print(remove_owned(staging,parse_inode())); raise SystemExit(0)
if operation!='promote' or staging is None: fail('unsupported worker source promotion operation')
staging_identity=parse_inode(); promoted=False
try:
    info=os.lstat(staging)
    if not stat.S_ISDIR(info.st_mode) or (info.st_dev,info.st_ino)!=staging_identity:
        fail('worker source staging inode changed before verification')
    if hook=='staging-drift': (staging/'.staging-drift').write_text('drift\n')
    verify_tree(staging)
    if hook=='race-target':
        destination.mkdir(parents=True); (destination/'competitor-marker').write_text('competitor\n')
    rename_noreplace(staging,destination); promoted=True
    target_info=os.lstat(destination)
    if not stat.S_ISDIR(target_info.st_mode) or (target_info.st_dev,target_info.st_ino)!=staging_identity:
        fail('worker source promotion inode changed before readback')
    if hook=='post-readback-drift': (destination/'.post-readback-drift').write_text('drift\n')
    verify_tree(destination)
except BaseException:
    remove_owned(staging,staging_identity)
    if promoted: remove_owned(destination,staging_identity)
    raise
print(json.dumps({'terminal':'PASS','promoted_dev':staging_identity[0],'promoted_ino':staging_identity[1]},sort_keys=True))
'''
MUTATING_COMMANDS = {
    "finalize-model-lock",
    "copy-model",
    "acquire-images",
    "save-runtime-archive",
    "copy-runtime-archive",
    "load-runtime-archive",
    "finalize-deployment-lock",
    "apply-runtime-patch",
    "install-integration-artifacts",
    "install-service-assets",
    "qualify-fabric-consumer",
    "record-license-acceptance",
    "record-node-identity",
    "copy-patched-source",
}
MUTATION_ROLES = {
    "finalize-model-lock": "head",
    "copy-model": "head",
    "acquire-images": "head",
    "save-runtime-archive": "head",
    "copy-runtime-archive": "head",
    "load-runtime-archive": "worker",
    "finalize-deployment-lock": "head",
    "apply-runtime-patch": "head",
    "install-integration-artifacts": "head",
    "install-service-assets": None,
    "qualify-fabric-consumer": "head",
    "record-license-acceptance": "head",
    "record-node-identity": None,
    "copy-patched-source": "head",
}
INVOKED_AS = str(Path(sys.argv[1]).resolve())
CLI_ARGS = sys.argv[2:]


class PreparationError(RuntimeError):
    pass


class MutationReceiptError(PreparationError):
    pass


def marked_local_test_seam(name):
    return (os.environ.get(name) == "1" and sys.platform == "darwin" and
            any(part in {"DGXSpark", "DGXSpark-worktrees"} for part in Path(INVOKED_AS).parts))


def test_seam_command(serialized, production):
    if serialized is None:
        return list(production) if isinstance(production, list) else production
    if not marked_local_test_seam("DEEPSEEK_ARTIFACT_TEST_SEAM"):
        raise PreparationError("test command injection requires DEEPSEEK_ARTIFACT_TEST_SEAM=1")
    if isinstance(production, list):
        try:
            command = json.loads(serialized)
        except json.JSONDecodeError as exc:
            raise PreparationError("test docker command must be a JSON argv array") from exc
        if not isinstance(command, list) or not command or not all(isinstance(value, str) and value for value in command):
            raise PreparationError("test docker command must be a non-empty JSON argv array")
        return command
    if not isinstance(serialized, str) or not serialized:
        raise PreparationError("test rsync command must be non-empty")
    return serialized


def best_effort_cleanup(items, cleaner):
    outcomes = {}
    errors = []
    for key, payload in items:
        try:
            outcomes[key] = cleaner(payload)
        except BaseException as exc:
            outcomes[key] = "cleanup-failed"
            errors.append(f"{key}: {exc}")
    return outcomes, errors


def require_mutation_receipt(args):
    receipt_path = str(args.receipt_file)
    test_seam = marked_local_test_seam("DEEPSEEK_ARTIFACT_RECEIPT_TEST_SEAM")
    if receipt_path != "/proc/self/fd/3" and not test_seam:
        raise MutationReceiptError("production mutation receipt must be supplied on /proc/self/fd/3")
    try:
        record = read_json(receipt_path)
    except PreparationError as exc:
        raise MutationReceiptError("mutation receipt is absent or unreadable") from exc
    receipt = record.get("receipt") if isinstance(record, dict) else None
    command = args.command
    role = MUTATION_ROLES[command] or getattr(args, "role", None)
    resource = (f"deepseek-v4-flash-dspark:{command}:{role}"
                if command == "install-service-assets" else f"deepseek-v4-flash-dspark:{command}")
    expected_argv = [INSTALL_PATH, *CLI_ARGS]
    generation = receipt.get("lease_generation") if isinstance(receipt, dict) else None
    if (record.get("state") != "running" or not isinstance(receipt, dict) or
            receipt.get("schema") != "deepseek-dspark-receipt-v1" or
            receipt.get("host") != ("dgx-head" if role == "head" else "dgx-worker") or
            getattr(args, "role", None) != role or receipt.get("role") != role or receipt.get("verb") != command or
            receipt.get("resource") != resource or receipt.get("argv") != expected_argv or
            not isinstance(generation, str) or len(generation) != 32 or
            any(character not in "0123456789abcdef" for character in generation)):
        raise MutationReceiptError("running mutation receipt binding is invalid")
    args.mutation_receipt = receipt


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise PreparationError(f"cannot read JSON {path}: {exc}") from exc


def atomic_json(path, value):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=destination.name + ".", dir=destination.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_bytes(path, value, mode):
    destination = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=destination.name + ".", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative_path(value):
    if not isinstance(value, str) or not value:
        raise PreparationError("unsafe relative path: empty or non-string")
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or any(part in ("", ".", "..") for part in path.parts):
        raise PreparationError(f"unsafe relative path: {value!r}")
    lowered = value.lower()
    if lowered.endswith(".log") or ".download." in lowered or lowered.startswith("logs/"):
        raise PreparationError(f"logs are forbidden from the model payload: {value}")
    return value


def download_rows(path):
    rows = read_json(path)
    if not isinstance(rows, list) or len(rows) != 81:
        raise PreparationError(f"allowlist must contain exactly 81 rows, got {len(rows) if isinstance(rows, list) else 'non-list'}")
    paths = []
    repositories = set()
    revisions = set()
    for row in rows:
        if not isinstance(row, dict):
            raise PreparationError("allowlist rows must be objects")
        relative = safe_relative_path(row.get("path"))
        if row.get("destination") != relative:
            raise PreparationError(f"destination must exactly equal path: {relative}")
        paths.append(relative)
        repositories.add(row.get("repo"))
        revisions.add(row.get("revision"))
    if len(set(paths)) != 81:
        raise PreparationError("allowlist contains duplicate paths")
    if len(repositories) != 1 or None in repositories or len(revisions) != 1 or None in revisions:
        raise PreparationError("allowlist must pin one repository and one revision")
    return rows, paths


def read_safetensors_header(path):
    path = Path(path)
    size = path.stat().st_size
    with path.open("rb") as stream:
        prefix = stream.read(8)
        if len(prefix) != 8:
            raise PreparationError(f"invalid safetensors header: {path.name}")
        header_size = struct.unpack("<Q", prefix)[0]
        if header_size == 0 or header_size > size - 8 or header_size > 128 * 1024 * 1024:
            raise PreparationError(f"invalid safetensors header length: {path.name}")
        raw = stream.read(header_size)
    try:
        header = json.loads(raw.decode("utf-8").rstrip())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparationError(f"invalid safetensors header JSON: {path.name}") from exc
    if not isinstance(header, dict):
        raise PreparationError(f"invalid safetensors header object: {path.name}")
    body_size = size - 8 - header_size
    for name, metadata in header.items():
        if name == "__metadata__":
            continue
        if not isinstance(metadata, dict) or not isinstance(metadata.get("dtype"), str):
            raise PreparationError(f"invalid tensor dtype in {path.name}: {name}")
        shape = metadata.get("shape")
        offsets = metadata.get("data_offsets")
        if not isinstance(shape, list) or not all(isinstance(value, int) and value >= 0 for value in shape):
            raise PreparationError(f"invalid tensor shape in {path.name}: {name}")
        if (not isinstance(offsets, list) or len(offsets) != 2 or
                not all(isinstance(value, int) for value in offsets) or
                offsets[0] < 0 or offsets[0] > offsets[1] or offsets[1] > body_size):
            raise PreparationError(f"invalid tensor offsets in {path.name}: {name}")
    return header


def validate_index_and_headers(model_root, paths, expected_shards):
    root = Path(model_root)
    shards = [path for path in paths if path.startswith("model-") and path.endswith(".safetensors")]
    if len(shards) != expected_shards:
        raise PreparationError(f"expected {expected_shards} safetensors shards, got {len(shards)}")
    index = read_json(root / "model.safetensors.index.json")
    weight_map = index.get("weight_map") if isinstance(index, dict) else None
    if not isinstance(weight_map, dict) or not weight_map:
        raise PreparationError("model index has no weight_map")
    referenced = set(weight_map.values())
    if referenced != set(shards):
        raise PreparationError("index shard closure does not exactly match the 48 allowlisted shards")
    headers = {path: read_safetensors_header(root / path) for path in shards}
    return headers


def actual_tree_paths(root):
    root = Path(root)
    values = []
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise PreparationError(f"symlink forbidden in model closure: {relative}")
        if path.is_file():
            values.append(relative)
    return set(values)


def build_closure(model_root, paths, expected_total, expected_shards, strict_tree):
    root = Path(model_root)
    rows = []
    for relative in paths:
        target = root / relative
        if target.is_symlink() or not target.is_file():
            raise PreparationError(f"missing or non-regular allowlisted path: {relative}")
        size = target.stat().st_size
        rows.append({"path": relative, "size": size, "sha256": file_sha256(target)})
    validate_index_and_headers(root, paths, expected_shards)
    total = sum(row["size"] for row in rows)
    if total != expected_total:
        raise PreparationError(f"model total bytes mismatch: expected {expected_total}, got {total}")
    extras = actual_tree_paths(root) - set(paths)
    if strict_tree and extras:
        raise PreparationError("extra paths outside the 81-path allowlist: " + ", ".join(sorted(extras)))
    material = "".join(f"{row['sha256']}  {row['size']}  {row['path']}\n" for row in rows).encode()
    return rows, hashlib.sha256(material).hexdigest(), len(extras)


def compare_anchors(template, model_root, headers, inventory_path, metadata_path):
    policy = template["official_anchor_comparison"]
    inventory = read_json(inventory_path)
    if inventory.get("repository") != policy["repository"] or inventory.get("revision") != policy["revision"]:
        raise PreparationError("official anchor inventory repository/revision mismatch")
    provenance = policy.get("metadata_source", {})
    observed = inventory.get("observed_at")
    inventory_source = inventory.get("metadata_source")
    expected_source = {key: provenance.get(key) for key in ("url", "response_body_sha256", "authenticated")}
    if (inventory.get("schema_version") != 1 or observed != provenance.get("observed_at") or
            inventory_source != expected_source or
            file_sha256(inventory_path) != provenance.get("inventory_sha256")):
        raise PreparationError("official anchor metadata provenance or inventory SHA-256 mismatch")
    metadata = read_json(metadata_path)
    if (provenance.get("snapshot_path") != "manifests/deepseek-v4-flash-official-metadata.json" or
            file_sha256(metadata_path) != provenance.get("response_body_sha256") or
            metadata.get("id") != policy["repository"] or metadata.get("modelId") != policy["repository"] or
            metadata.get("sha") != policy["revision"]):
        raise PreparationError("official metadata snapshot repository/revision/body SHA-256 mismatch")
    shard_paths = set(headers)
    metadata_shards = {}
    for row in metadata.get("siblings", []):
        path = row.get("rfilename") if isinstance(row, dict) else None
        if path in shard_paths:
            lfs = row.get("lfs")
            if (not isinstance(lfs, dict) or not is_sha256(lfs.get("sha256")) or
                    not isinstance(lfs.get("size"), int) or lfs["size"] <= 0 or
                    row.get("size") != lfs["size"] or path in metadata_shards):
                raise PreparationError(f"official metadata LFS row is malformed or duplicated: {path}")
            metadata_shards[path] = {"size": lfs["size"], "sha256": lfs["sha256"]}
    if set(metadata_shards) != shard_paths:
        raise PreparationError("official metadata snapshot does not contain exactly all 48 local shards")
    for path, expected in metadata_shards.items():
        target = Path(model_root) / path
        if target.stat().st_size != expected["size"] or file_sha256(target) != expected["sha256"]:
            raise PreparationError(f"official metadata shard size/SHA-256 mismatch: {path}")
    inventory_rows = {row.get("path"): row for row in inventory.get("files", [])}
    if set(inventory_rows) != {row["path"] for row in policy["files"]}:
        raise PreparationError("official anchor inventory must contain exactly the seven pinned anchors")
    expected_official_shards = [{"path": path, **metadata_shards[path]} for path in sorted(metadata_shards)]
    if (inventory.get("builder_version") != 2 or
            inventory.get("official_shards") != expected_official_shards or
            inventory.get("anchor_paths") != [row["path"] for row in policy["files"]]):
        raise PreparationError("official anchor inventory builder/version/anchor set mismatch")
    for expected in policy["files"]:
        path = expected["path"]
        target = Path(model_root) / path
        if target.stat().st_size != expected["size"] or file_sha256(target) != expected["sha256"]:
            raise PreparationError(f"official anchor size/SHA-256 mismatch: {path}")
        official = inventory_rows[path]
        if official.get("sha256") != expected["sha256"] or official.get("tensors") != headers[path]:
            raise PreparationError(f"official anchor tensor inventory mismatch: {path}")


def cmd_emit_files(args):
    _, paths = download_rows(args.download_manifest)
    sys.stdout.buffer.write(b"".join(path.encode() + b"\0" for path in paths))


def cmd_finalize(args):
    template = read_json(args.lock_template)
    rows, paths = download_rows(args.download_manifest)
    model = template["model"]
    if paths != model.get("allowlisted_paths"):
        raise PreparationError("download manifest differs from locked 81-path allowlist")
    if {row["repo"] for row in rows} != {model["repository"]} or {row["revision"] for row in rows} != {model["revision"]}:
        raise PreparationError("download manifest repository/revision differs from lock")
    headers = validate_index_and_headers(args.model_root, paths, model["expected_shard_count"])
    compare_anchors(template, args.model_root, headers, args.official_anchor_inventory,
                    args.official_metadata)
    files, root_hash, extra_count = build_closure(
        args.model_root, paths, model["expected_total_bytes"], model["expected_shard_count"], args.strict_tree)
    output = json.loads(json.dumps(template))
    output["finalized"] = False
    output["model"]["closure_finalized"] = True
    output["model"]["files"] = files
    output["model"]["root_sha256"] = root_hash
    output["model"]["verified_shard_count"] = model["expected_shard_count"]
    output["model"]["payload_extra_path_count"] = 0
    output["model"]["source_extra_path_count"] = extra_count
    atomic_json(args.output, output)
    print(f"MODEL_LOCK_FINALIZED paths={len(files)} root_sha256={root_hash}")


def cmd_verify_model(args):
    lock = read_json(args.lock)
    if lock.get("model", {}).get("closure_finalized") is not True:
        raise PreparationError("model payload closure is not finalized")
    model = lock["model"]
    paths = model["allowlisted_paths"]
    # Recompute first so a corrupt file is reported as the per-path closure
    # mismatch that caused it, rather than only as an aggregate byte mismatch.
    actual_total = sum((Path(args.model_root) / path).stat().st_size for path in paths)
    files, root_hash, extra_count = build_closure(
        args.model_root, paths, actual_total, model["expected_shard_count"], args.strict_tree)
    if files != model["files"]:
        expected = {row["path"]: row for row in model["files"]}
        actual = {row["path"]: row for row in files}
        mismatches = [path for path in paths if expected.get(path) != actual.get(path)]
        raise PreparationError("digest mismatch in locked model files: " + ", ".join(mismatches[:5]))
    if root_hash != model["root_sha256"] or model.get("payload_extra_path_count") != 0:
        raise PreparationError("model root closure mismatch")
    print(f"MODEL_LOCK_VERIFIED paths={len(files)} root_sha256={root_hash} source_extras={extra_count}")


def cmd_copy_model(args):
    _, paths = download_rows(args.download_manifest)
    lock = read_json(args.lock)
    if lock.get("model", {}).get("closure_finalized") is not True or lock.get("model", {}).get("allowlisted_paths") != paths:
        raise PreparationError("model copy requires the finalized lock for this exact 81-path allowlist")
    cmd_verify_model(argparse.Namespace(model_root=args.source_root, lock=args.lock, strict_tree=False))
    source = Path(args.source_root)
    for path in paths:
        target = source / path
        if target.is_symlink() or not target.is_file():
            raise PreparationError(f"missing or non-regular allowlisted path: {path}")
    payload = b"".join(path.encode() + b"\0" for path in paths)
    validate_transfer_lock(lock)
    rsync_command = test_seam_command(args.test_rsync_bin, RSYNC_COMMAND)
    command = [rsync_command, "--archive", "--partial", "--relative", "--from0",
               "--files-from=-", "--protect-args", "-e", WORKER_SSH,
               str(source) + "/", MODEL_DESTINATION]
    result = subprocess.run(command, input=payload)
    if result.returncode:
        raise PreparationError(f"rsync allowlist copy failed with exit {result.returncode}")
    if args.test_rsync_bin is None:
        if str(args.lock) != MODEL_LOCK_HEAD_PATH:
            raise PreparationError("production model copy must consume the fixed finalized model lock")
        lock_text = Path(args.lock).read_text()
        lock_sha = hashlib.sha256(lock_text.encode()).hexdigest()
        install_lock = ("import hashlib,os,pathlib,sys,tempfile; d=pathlib.Path(sys.argv[1]); "
                        "data=sys.stdin.buffer.read(); expected=sys.argv[2]; "
                        "(_ for _ in ()).throw(SystemExit('lock digest mismatch')) if hashlib.sha256(data).hexdigest()!=expected else None; "
                        "d.parent.mkdir(parents=True,exist_ok=True); "
                        "fd,tmp=tempfile.mkstemp(prefix='.model.lock.',dir=d.parent); os.write(fd,data); os.fchmod(fd,0o644); os.close(fd); os.replace(tmp,d)")
        remote_text(["/usr/bin/python3", "-c", install_lock, MODEL_LOCK_WORKER_PATH, lock_sha],
                    input_text=lock_text)
        readback = remote_text(["/usr/bin/sha256sum", MODEL_LOCK_WORKER_PATH]).split()[0]
        if readback != lock_sha:
            raise PreparationError("worker finalized model lock readback mismatch")
    print(f"MODEL_ALLOWLIST_COPY_COMPLETE paths={len(paths)}")


def validate_image_lock(lock):
    runtime = lock.get("runtime_image", {})
    gateway = lock.get("gateway_image", {})
    acquisition = lock.get("image_acquisition", {})
    exact = (
        runtime.get("reference") == RUNTIME_REFERENCE and
        runtime.get("manifest_digest") == RUNTIME_MANIFEST and
        runtime.get("config_digest") == RUNTIME_CONFIG and
        runtime.get("platform") == "linux/arm64" and
        runtime.get("compressed_layer_bytes") == RUNTIME_COMPRESSED_BYTES and
        runtime.get("worker_registry_pull_allowed") is False and
        gateway.get("reference") == GATEWAY_REFERENCE and
        gateway.get("manifest_digest") == GATEWAY_MANIFEST and
        gateway.get("config_digest") == GATEWAY_CONFIG and
        gateway.get("platform") == "linux/arm64/v8" and
        gateway.get("compressed_layer_count") == 8 and
        gateway.get("compressed_layer_bytes") == GATEWAY_COMPRESSED_BYTES and
        gateway.get("placement") == "head-only" and
        gateway.get("worker_registry_pull_allowed") is False and
        acquisition.get("approval_message_sha256") == APPROVAL_SHA256 and
        acquisition.get("rx_counter") == "/sys/class/net/dgx-vpn/statistics/rx_bytes" and
        acquisition.get("cumulative_rx_budget_bytes") == VPN_RX_BUDGET and
        acquisition.get("max_pull_calls") == 2 and
        acquisition.get("allowed_registry_pull_role") == "head" and
        acquisition.get("cache_policy") == "exact-digest-and-valid-journal-only"
    )
    if not exact:
        raise PreparationError("image lock differs from the finalized digest, closure, role, or VPN budget")
    return runtime, gateway, acquisition


def validate_transfer_lock(lock):
    transfer = lock.get("fabric_transfer", {})
    if transfer != {
        "worker": "operator@192.0.2.34",
        "identity_file": "/home/operator/.ssh/deepseek-v4-flash-worker",
        "known_hosts_file": "/home/operator/.ssh/deepseek-v4-flash-worker-known_hosts",
        "ssh_policy": ["BatchMode=yes", "StrictHostKeyChecking=yes"],
        "model_destination": MODEL_DESTINATION,
        "runtime_archive_destination": ARCHIVE_DESTINATION,
        "runtime_archive_receipt_destination": ARCHIVE_RECEIPT_DESTINATION,
    }:
        raise PreparationError("fabric transfer lock differs from the fixed worker key, host, or destination")
    return transfer


def docker_inspect(docker_command, image):
    result = subprocess.run(docker_command + ["image", "inspect", image["reference"]],
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if result.returncode:
        return None
    try:
        values = json.loads(result.stdout)
        value = values[0]
    except (json.JSONDecodeError, IndexError, KeyError, TypeError) as exc:
        raise PreparationError(f"invalid docker inspect output for {image['reference']}") from exc
    return value


def repo_digest_aliases(reference):
    aliases = {reference}
    if reference.startswith("docker.io/library/"):
        aliases.add(reference.removeprefix("docker.io/library/"))
    elif reference.startswith("docker.io/"):
        aliases.add(reference.removeprefix("docker.io/"))
    return aliases


def inspect_identity(image, inspected, require_repo_digest=True):
    if inspected is None:
        raise PreparationError(f"pinned image absent after operation: {image['reference']}")
    expected_platform = image["platform"].split("/")[:2]
    aliases = repo_digest_aliases(image["reference"])
    if require_repo_digest and not aliases.intersection(inspected.get("RepoDigests", [])):
        raise PreparationError(f"RepoDigest mismatch for {image['reference']}")
    if inspected.get("Id") != image["config_digest"]:
        raise PreparationError(f"config image ID mismatch for {image['reference']}")
    if [inspected.get("Os"), inspected.get("Architecture")] != expected_platform:
        raise PreparationError(f"platform mismatch for {image['reference']}")
    layers = inspected.get("RootFS", {}).get("Layers")
    if not isinstance(layers, list) or not layers or not all(isinstance(value, str) and value.startswith("sha256:") for value in layers):
        raise PreparationError(f"RootFS diffIDs missing for {image['reference']}")
    return layers


def verify_registry_manifest(docker_command, image):
    try:
        result = subprocess.run(
            docker_command + ["manifest", "inspect", image["reference"]],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise PreparationError(f"registry manifest inspection timed out: {image['reference']}") from exc
    if result.returncode:
        try:
            result = subprocess.run(
                docker_command + ["buildx", "imagetools", "inspect", "--raw", image["reference"]],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60,
            )
        except subprocess.TimeoutExpired as exc:
            raise PreparationError(f"registry raw manifest inspection timed out: {image['reference']}") from exc
        if result.returncode:
            raise PreparationError(f"registry manifest inspection failed: {image['reference']}")
    try:
        manifest = json.loads(result.stdout)
        layers = manifest["layers"]
        config_digest = manifest["config"]["digest"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise PreparationError(f"invalid registry manifest metadata: {image['reference']}") from exc
    unique = {}
    for layer in layers:
        digest = layer.get("digest")
        size = layer.get("size")
        if not isinstance(digest, str) or not digest.startswith("sha256:") or not isinstance(size, int) or size < 0:
            raise PreparationError(f"invalid compressed layer entry: {image['reference']}")
        if digest in unique and unique[digest] != size:
            raise PreparationError(f"conflicting duplicate compressed layer: {image['reference']}")
        unique[digest] = size
    if config_digest != image["config_digest"]:
        raise PreparationError(f"registry config digest mismatch: {image['reference']}")
    if len(unique) != image["compressed_layer_count"] or sum(unique.values()) != image["compressed_layer_bytes"]:
        raise PreparationError(f"compressed layer closure mismatch: {image['reference']}")
    return [{"digest": digest, "size": unique[digest]} for digest in unique]


def journal_entry_valid(image, inspected, entry):
    if not isinstance(entry, dict) or inspected is None:
        return False
    try:
        layers = inspect_identity(image, inspected)
    except PreparationError:
        return False
    compressed = entry.get("compressed_layers")
    compressed_valid = (
        isinstance(compressed, list) and
        len({row.get("digest") for row in compressed if isinstance(row, dict)}) == image["compressed_layer_count"] and
        all(isinstance(row, dict) and isinstance(row.get("digest"), str) and row["digest"].startswith("sha256:")
            and isinstance(row.get("size"), int) and row["size"] >= 0 for row in compressed) and
        sum(row["size"] for row in compressed) == image["compressed_layer_bytes"]
    )
    return compressed_valid and (
        entry.get("reference") == image["reference"] and
        entry.get("manifest_digest") == image["manifest_digest"] and
        entry.get("config_digest") == image["config_digest"] and
        entry.get("platform") == image["platform"] and
        entry.get("compressed_layer_bytes") == image["compressed_layer_bytes"] and
        entry.get("rootfs_diff_ids") == layers
    )


def read_counter(path):
    try:
        value = int(Path(path).read_text().strip())
    except (OSError, ValueError) as exc:
        raise PreparationError(f"cannot read VPN RX counter {path}: {exc}") from exc
    if value < 0:
        raise PreparationError("VPN RX counter cannot be negative")
    return value


def stop_process_group(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)


def supervised_pull(docker_command, reference, counter_path, baseline, budget, poll_seconds):
    process = subprocess.Popen(docker_command + ["pull", reference], start_new_session=True)
    try:
        while process.poll() is None:
            if read_counter(counter_path) - baseline >= budget:
                stop_process_group(process)
                raise PreparationError("VPN RX budget reached; exact pull process group terminated")
            time.sleep(poll_seconds)
        if read_counter(counter_path) - baseline >= budget:
            raise PreparationError("VPN RX budget reached at pull completion")
        if process.returncode:
            raise PreparationError(f"exact digest pull failed with exit {process.returncode}: {reference}")
    except BaseException:
        stop_process_group(process)
        raise


def acquisition_state_valid(lock, state):
    runtime, gateway, acquisition = validate_image_lock(lock)
    return (
        isinstance(state, dict) and state.get("schema_version") == 1 and
        state.get("role") == "head" and state.get("interface") == "dgx-vpn" and
        state.get("rx_counter_path") == acquisition["rx_counter"] and
        state.get("runtime_manifest_digest") == runtime["manifest_digest"] and
        state.get("gateway_manifest_digest") == gateway["manifest_digest"] and
        all(isinstance(state.get(key), int) for key in ("rx_origin", "last_observed_rx", "pull_calls")) and
        state["rx_origin"] >= 0 and state["last_observed_rx"] >= state["rx_origin"] and
        state["last_observed_rx"] - state["rx_origin"] < acquisition["cumulative_rx_budget_bytes"] and
        0 <= state["pull_calls"] <= acquisition["max_pull_calls"] and
        isinstance(state.get("transaction_id"), str) and
        re.fullmatch(r"[0-9a-f]{32}", state["transaction_id"]) is not None and
        isinstance(state.get("lease_generation"), str) and
        re.fullmatch(r"[0-9a-f]{32}", state["lease_generation"]) is not None
    )


def cmd_acquire_images(args):
    lock = read_json(args.lock)
    runtime, gateway, acquisition = validate_image_lock(lock)
    if args.role != acquisition["allowed_registry_pull_role"]:
        raise PreparationError("registry pulls are head-only; worker must consume the verified runtime archive")
    docker_command = test_seam_command(args.test_docker_command_json, DOCKER_COMMAND)
    if args.test_docker_command_json is None and args.rx_counter != acquisition["rx_counter"]:
        raise PreparationError("production acquisition must use the fixed dgx-vpn RX counter")
    journal = read_json(args.journal) if Path(args.journal).exists() else {"schema_version": 1, "images": {}}
    if journal.get("schema_version") != 1 or not isinstance(journal.get("images"), dict):
        raise PreparationError("invalid image acquisition journal")
    current_counter = read_counter(args.rx_counter)
    acquisition_state = journal.get("acquisition")
    if acquisition_state is None:
        if journal["images"]:
            raise PreparationError("cache journal image entries lack immutable VPN/pull acquisition provenance")
        acquisition_state = {
            "schema_version": 1, "role": "head", "interface": "dgx-vpn",
            "rx_counter_path": acquisition["rx_counter"], "rx_origin": current_counter,
            "last_observed_rx": current_counter, "pull_calls": 0,
            "runtime_manifest_digest": runtime["manifest_digest"],
            "gateway_manifest_digest": gateway["manifest_digest"],
            "transaction_id": args.mutation_receipt["transaction_id"],
            "lease_generation": args.mutation_receipt["lease_generation"],
        }
        journal["acquisition"] = acquisition_state
        atomic_json(args.journal, journal)
    if (not acquisition_state_valid(lock, acquisition_state) or
            current_counter < acquisition_state["rx_origin"] or current_counter < acquisition_state["last_observed_rx"] or
            current_counter - acquisition_state["rx_origin"] >= acquisition["cumulative_rx_budget_bytes"]):
        raise PreparationError("invalid or reset VPN acquisition counter journal")
    baseline = acquisition_state["rx_origin"]
    acquisition_state["last_observed_rx"] = current_counter
    atomic_json(args.journal, journal)
    if all(journal_entry_valid(
            image,
            docker_inspect(docker_command, image),
            journal["images"].get(image["manifest_digest"]),
    ) for image in (runtime, gateway)):
        print(f"IMAGE_ACQUISITION_COMPLETE pulls=0 vpn_rx_delta={current_counter - baseline}")
        return
    pulls = 0
    for image in (runtime, gateway):
        inspected = docker_inspect(docker_command, image)
        entry = journal["images"].get(image["manifest_digest"])
        if journal_entry_valid(image, inspected, entry):
            continue
        # Exact cache/journal is deliberately checked again immediately before
        # consuming one of the two permitted pull calls.
        inspected = docker_inspect(docker_command, image)
        entry = journal["images"].get(image["manifest_digest"])
        if journal_entry_valid(image, inspected, entry):
            continue
        if acquisition_state["pull_calls"] >= acquisition["max_pull_calls"]:
            raise PreparationError("maximum exact digest pull calls exhausted")
        if read_counter(args.rx_counter) - baseline >= acquisition["cumulative_rx_budget_bytes"]:
            raise PreparationError("VPN RX budget reached before pull")
        compressed_layers = verify_registry_manifest(docker_command, image)
        acquisition_state["last_observed_rx"] = read_counter(args.rx_counter)
        atomic_json(args.journal, journal)
        if acquisition_state["last_observed_rx"] - baseline >= acquisition["cumulative_rx_budget_bytes"]:
            raise PreparationError("VPN RX budget reached during registry manifest verification")
        pulls += 1
        acquisition_state["pull_calls"] += 1
        atomic_json(args.journal, journal)
        supervised_pull(docker_command, image["reference"], args.rx_counter, baseline,
                        acquisition["cumulative_rx_budget_bytes"], args.poll_seconds)
        inspected = docker_inspect(docker_command, image)
        layers = inspect_identity(image, inspected)
        journal["images"][image["manifest_digest"]] = {
            "reference": image["reference"],
            "manifest_digest": image["manifest_digest"],
            "config_digest": image["config_digest"],
            "platform": image["platform"],
            "compressed_layer_bytes": image["compressed_layer_bytes"],
            "compressed_layers": compressed_layers,
            "rootfs_diff_ids": layers,
        }
        acquisition_state["last_observed_rx"] = read_counter(args.rx_counter)
        atomic_json(args.journal, journal)
    print(f"IMAGE_ACQUISITION_COMPLETE pulls={pulls} vpn_rx_delta={read_counter(args.rx_counter) - baseline}")


def runtime_receipt(lock, archive, inspected, compressed_layers):
    runtime, _, _ = validate_image_lock(lock)
    layers = inspect_identity(runtime, inspected)
    archive = Path(archive)
    return {
        "schema_version": 1,
        "kind": "deepseek-v4-flash-runtime-archive-receipt",
        "reference": runtime["reference"],
        "manifest_digest": runtime["manifest_digest"],
        "config_digest": runtime["config_digest"],
        "platform": runtime["platform"],
        "compressed_layer_count": runtime["compressed_layer_count"],
        "compressed_layer_bytes": runtime["compressed_layer_bytes"],
        "compressed_layers": compressed_layers,
        "rootfs_diff_ids": layers,
        "archive_path": str(archive),
        "archive_size": archive.stat().st_size,
        "archive_sha256": file_sha256(archive),
    }


def cmd_save_runtime(args):
    if args.role != "head":
        raise PreparationError("runtime archive may only be saved on head")
    lock = read_json(args.lock)
    runtime, _, _ = validate_image_lock(lock)
    docker_command = test_seam_command(args.test_docker_command_json, DOCKER_COMMAND)
    inspected = docker_inspect(docker_command, runtime)
    layers = inspect_identity(runtime, inspected)
    journal = read_json(args.journal)
    entry = journal.get("images", {}).get(runtime["manifest_digest"])
    if not journal_entry_valid(runtime, inspected, entry):
        raise PreparationError("runtime cache lacks an exact valid manifest/config/layer acquisition journal")
    destination = Path(args.archive)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + f".partial.{os.getpid()}")
    if temporary.exists():
        raise PreparationError(f"refusing to overwrite existing temporary archive: {temporary}")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            result = subprocess.run(
                docker_command + ["image", "save", runtime["reference"]], stdout=output
            )
            output.flush()
            os.fsync(output.fileno())
        if result.returncode or not temporary.is_file():
            raise PreparationError(f"docker image save failed with exit {result.returncode}")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    receipt = runtime_receipt(lock, destination, inspected, entry["compressed_layers"])
    atomic_json(args.receipt, receipt)
    print(f"RUNTIME_ARCHIVE_SAVED size={receipt['archive_size']} sha256={receipt['archive_sha256']}")


def verify_archive(lock, archive_path, receipt_path, require_recorded_path=False):
    runtime, _, _ = validate_image_lock(lock)
    receipt = read_json(receipt_path)
    validate_archive_receipt_offline(lock, receipt)
    archive = Path(archive_path)
    if not archive.is_file():
        raise PreparationError("runtime archive is missing")
    if require_recorded_path and receipt.get("archive_path") != str(archive):
        raise PreparationError("runtime archive path differs from its immutable head receipt")
    closure_ok = receipt.get("archive_size") == archive.stat().st_size and receipt.get("archive_sha256") == file_sha256(archive)
    if not closure_ok:
        raise PreparationError("runtime archive size/SHA-256 mismatch or receipt identity mismatch")
    return runtime, receipt


def cmd_verify_archive(args):
    _, receipt = verify_archive(read_json(args.lock), args.archive, args.receipt, require_recorded_path=True)
    print(f"RUNTIME_ARCHIVE_VERIFIED size={receipt['archive_size']} sha256={receipt['archive_sha256']}")


def cmd_copy_archive(args):
    lock = read_json(args.lock)
    _, receipt = verify_archive(lock, args.archive, args.receipt, require_recorded_path=True)
    validate_image_journal_offline(lock, read_json(args.journal))
    validate_transfer_lock(lock)
    rsync_command = test_seam_command(args.test_rsync_bin, RSYNC_COMMAND)
    result = subprocess.run([
        rsync_command, "--archive", "--partial", "--protect-args", "-e", WORKER_SSH,
        str(args.archive), str(args.receipt), str(args.journal), ARCHIVE_DIRECTORY_DESTINATION,
    ])
    if result.returncode:
        raise PreparationError(f"runtime archive fabric copy failed with exit {result.returncode}")
    print(f"RUNTIME_ARCHIVE_COPY_COMPLETE size={receipt['archive_size']} sha256={receipt['archive_sha256']}")


def cmd_load_runtime(args):
    if args.role != "worker":
        raise PreparationError("runtime archive load command is worker-only")
    lock = read_json(args.lock)
    runtime, receipt = verify_archive(lock, args.archive, args.receipt)
    docker_command = test_seam_command(args.test_docker_command_json, DOCKER_COMMAND)
    if args.test_docker_command_json is None and str(args.archive) != ARCHIVE_WORKER_PATH:
        raise PreparationError("worker load must use the fixed fabric archive path")
    result = subprocess.run(docker_command + ["image", "load", "--input", str(args.archive)])
    if result.returncode:
        raise PreparationError(f"docker image load failed with exit {result.returncode}")
    inspected = docker_inspect(docker_command, runtime)
    if inspected is None:
        inspected = docker_inspect(
            docker_command, {"reference": runtime["config_digest"]}
        )
    layers = inspect_identity(runtime, inspected, require_repo_digest=False)
    if layers != receipt.get("rootfs_diff_ids"):
        raise PreparationError("post-load RootFS diffIDs differ from head save receipt")
    print("RUNTIME_ARCHIVE_LOADED_AND_VERIFIED")


def is_sha256(value):
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def compressed_entry_valid_offline(image, entry):
    if not isinstance(entry, dict):
        return False
    compressed = entry.get("compressed_layers")
    rootfs = entry.get("rootfs_diff_ids")
    return (
        entry.get("reference") == image["reference"] and
        entry.get("manifest_digest") == image["manifest_digest"] and
        entry.get("config_digest") == image["config_digest"] and
        entry.get("platform") == image["platform"] and
        entry.get("compressed_layer_bytes") == image["compressed_layer_bytes"] and
        isinstance(compressed, list) and len(compressed) == image["compressed_layer_count"] and
        len({row.get("digest") for row in compressed if isinstance(row, dict)}) == image["compressed_layer_count"] and
        all(isinstance(row, dict) and isinstance(row.get("digest"), str) and row["digest"].startswith("sha256:")
            and isinstance(row.get("size"), int) and row["size"] >= 0 for row in compressed) and
        sum(row["size"] for row in compressed) == image["compressed_layer_bytes"] and
        isinstance(rootfs, list) and rootfs and
        all(isinstance(value, str) and value.startswith("sha256:") for value in rootfs)
    )


def validate_source_receipt(lock, receipt, bootstrap_manifest=None):
    source = lock["source"]
    local_patch = receipt.get("local_patch", {})
    targets = receipt.get("patch_targets")
    head_identity = receipt.get("head_identity")
    worker_replica = receipt.get("worker_replica")
    upstream_identity = receipt.get("upstream_identity")
    replication = receipt.get("replication")
    if (receipt.get("schema_version") != 1 or receipt.get("commit") != source["commit"] or
            local_patch != source["local_patch"] or targets != source["patch_targets"] or
            not isinstance(upstream_identity, dict) or upstream_identity.get("variant") != "upstream" or
            not isinstance(head_identity, dict) or head_identity.get("variant") != "deepseek-local-patched" or
            not is_sha256(head_identity.get("root_sha256")) or
            not isinstance(worker_replica, dict) or worker_replica.get("root_sha256") != head_identity.get("root_sha256") or
            worker_replica.get("files") != head_identity.get("files") or
            not isinstance(replication, dict) or
            replication.get("destination") != "/home/operator/dgx-models/deepseek-v4-flash-dspark/source" or
            not isinstance(replication.get("transaction_id"), str) or not replication["transaction_id"] or
            not isinstance(replication.get("lease_generation"), str) or len(replication["lease_generation"]) != 32 or
            replication.get("preimage") not in {"missing", "identical"} or
            replication.get("rollback") != ("remove-new-destination-only" if replication.get("preimage") == "missing"
                                             else "not-required-identical-preimage")):
        raise PreparationError("source receipt does not close the pinned commit, patch, and target hashes")
    if bootstrap_manifest is not None and upstream_identity != bootstrap_upstream_identity(bootstrap_manifest):
        raise PreparationError("source receipt upstream identity differs from the finalized bootstrap lock")
    finalized = json.loads(json.dumps(source))
    finalized["local_patch"] = local_patch
    finalized["patch_targets"] = targets
    finalized["head_identity"] = head_identity
    finalized["worker_replica"] = worker_replica
    return finalized


def validate_license_receipt(lock, receipt):
    model_files = {row["path"]: row for row in lock["model"]["files"]}
    required = lock["license_gate"]["required_payload_paths"]
    rows = receipt.get("files")
    if (receipt.get("schema_version") != 1 or
            receipt.get("kind") != "deepseek-v4-flash-license-acceptance-receipt" or
            receipt.get("approval_basis_id") != "AC-DS-RESPONSIBLE-USE-20260830-USER-CONFIRMED" or
            receipt.get("approval_basis") != "User explicitly confirmed the selected DeepSeek V4 Flash model and replacement deployment in the controlling Codex task." or
            receipt.get("authority_commit") != "52ccc1adc05922d203614dc263d91dd6dea2076d" or
            receipt.get("accepted") is not True or
            receipt.get("operator") != "operator" or
            not isinstance(receipt.get("accepted_at"), str) or not receipt["accepted_at"] or
            not isinstance(receipt.get("transaction_id"), str) or not receipt["transaction_id"] or
            not isinstance(receipt.get("lease_generation"), str) or len(receipt["lease_generation"]) != 32 or
            not isinstance(rows, list) or [row.get("path") for row in rows] != required or
            any(row != model_files.get(row.get("path")) for row in rows)):
        raise PreparationError("license acceptance receipt is absent, declined, or differs from locked payload terms")


def cmd_record_license(args):
    lock = read_json(args.lock)
    if lock.get("model", {}).get("closure_finalized") is not True:
        raise PreparationError("license receipt requires the finalized model closure")
    if (args.operator != "operator" or args.approval_basis_id != "AC-DS-RESPONSIBLE-USE-20260830-USER-CONFIRMED" or
            args.approval_basis != "User explicitly confirmed the selected DeepSeek V4 Flash model and replacement deployment in the controlling Codex task." or
            args.authority_commit != "52ccc1adc05922d203614dc263d91dd6dea2076d"):
        raise PreparationError("license approval basis differs from the explicit user confirmation")
    files = {row["path"]: row for row in lock["model"]["files"]}
    required = lock["license_gate"]["required_payload_paths"]
    if set(required) != {"LICENSE", "RESPONSIBLE_USE.md"} or not all(path in files for path in required):
        raise PreparationError("license payload rows are absent from the model closure")
    mutation = args.mutation_receipt
    atomic_json(args.output, {
        "schema_version": 1, "kind": "deepseek-v4-flash-license-acceptance-receipt",
        "accepted": True, "operator": args.operator, "accepted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "approval_basis_id": args.approval_basis_id, "approval_basis": args.approval_basis,
        "authority_commit": args.authority_commit, "transaction_id": mutation["transaction_id"],
        "lease_generation": mutation["lease_generation"], "files": [files[path] for path in required],
    })
    print("LICENSE_ACCEPTANCE_RECORDED")


def cmd_record_node_identity(args):
    lock = read_json(args.lock)
    cmd_verify_model(argparse.Namespace(model_root=args.model_root, lock=args.lock, strict_tree=True))
    runtime, gateway, _ = validate_image_lock(lock)
    journal = read_json(args.image_journal)
    image_entries = validate_image_journal_offline(lock, journal)
    archive_receipt = read_json(args.archive_receipt)
    validate_archive_receipt_offline(lock, archive_receipt)
    docker_command = test_seam_command(args.test_docker_command_json, DOCKER_COMMAND)
    images = {}
    for name, image in (("runtime", runtime), ("gateway", gateway)):
        if name == "gateway" and args.role == "worker":
            continue
        inspected = docker_inspect(docker_command, image)
        if inspected is None and args.role == "worker" and name == "runtime":
            inspected = docker_inspect(
                docker_command, {"reference": runtime["config_digest"]}
            )
        layers = inspect_identity(image, inspected, require_repo_digest=(args.role == "head"))
        expected_layers = (archive_receipt if name == "runtime" else
                           image_entries[image["manifest_digest"]])["rootfs_diff_ids"]
        if layers != expected_layers:
            raise PreparationError(f"{args.role} {name} RootFS differs from its immutable acquisition chain")
        images[name] = {"config_digest": inspected["Id"], "os": inspected["Os"],
                        "architecture": inspected["Architecture"], "rootfs_diff_ids": layers}
        if args.role == "head":
            images[name]["repo_digests"] = inspected.get("RepoDigests", [])
    hostname = ("dgx-head" if args.role == "head" else "dgx-worker") if args.test_docker_command_json else run_text(["/usr/bin/hostname"]).strip()
    if hostname != ("dgx-head" if args.role == "head" else "dgx-worker"):
        raise PreparationError("node identity hostname/role mismatch")
    identity = {
        "schema_version": 1, "kind": "deepseek-v4-flash-node-identity-receipt", "role": args.role,
        "hostname": hostname, "boot_id": ("test-boot" if args.test_docker_command_json else Path("/proc/sys/kernel/random/boot_id").read_text().strip()),
        "kernel": ("6.17.0-1031-nvidia" if args.test_docker_command_json else run_text(["/usr/bin/uname", "-r"]).strip()),
        "driver": ("580.173.02" if args.test_docker_command_json else
                   run_text(["/usr/bin/nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"]).splitlines()[0]),
        "model_root_sha256": lock["model"]["root_sha256"], "images": images,
        "image_journal_sha256": file_sha256(args.image_journal),
        "runtime_archive_receipt_sha256": file_sha256(args.archive_receipt),
        "transaction_id": args.mutation_receipt["transaction_id"],
        "lease_generation": args.mutation_receipt["lease_generation"],
    }
    atomic_json(args.output, identity)
    print(f"NODE_IDENTITY_RECORDED role={args.role}")


def validate_integration_receipt(lock, receipt):
    expected = lock.get("integration_artifacts", {})
    rows = receipt.get("files")
    templates = expected.get("files")
    if (receipt.get("schema_version") != 1 or not isinstance(rows, list) or
            not isinstance(templates, list) or len(rows) != len(templates)):
        raise PreparationError("integration artifact receipt is absent or incomplete")
    finalized = []
    for template, row in zip(templates, rows):
        identity = {key: row.get(key) for key in ("source_path", "install_path", "mode")}
        expected_identity = {key: template.get(key) for key in ("source_path", "install_path", "mode")}
        if identity != expected_identity or not is_sha256(row.get("sha256")):
            raise PreparationError("integration artifact path, mode, or SHA-256 differs from the lock")
        finalized.append(dict(row))
    if receipt.get("semantic_constraints") != expected.get("semantic_constraints"):
        raise PreparationError("integration artifact semantic constraints differ from the lock")
    service = lock.get("service_assets", {})
    source_templates = service.get("source_files")
    source_rows = receipt.get("service_asset_sources")
    if (not isinstance(source_templates, list) or not isinstance(source_rows, list) or
            len(source_templates) != len(source_rows)):
        raise PreparationError("integration receipt does not close the service asset source set")
    finalized_sources = []
    for template, row in zip(source_templates, source_rows):
        if (row.get("source_path") != template.get("source_path") or
                not is_sha256(row.get("sha256"))):
            raise PreparationError("service asset source path or SHA-256 differs from the lock")
        finalized_sources.append(dict(row))
    return ({"finalized": True, "files": finalized,
             "semantic_constraints": expected["semantic_constraints"]},
            {**service, "finalized": True, "source_files": finalized_sources})


def cmd_install_integration(args):
    test_mode = args.test_input_root is not None or args.test_install_root is not None
    if test_mode and (args.test_input_root is None or args.test_install_root is None or
                      not marked_local_test_seam("DEEPSEEK_ARTIFACT_TEST_SEAM")):
        raise PreparationError("integration install test roots require the marked local test seam")
    input_root = Path(args.test_input_root) if test_mode else Path(INPUT_ROOT)
    install_root = Path(args.test_install_root) if test_mode else None
    lock = read_json(input_root / "manifests/deepseek-v4-flash-dspark.lock.json")
    integration = lock.get("integration_artifacts", {})
    templates = integration.get("files")
    expected_templates = [
        {"source_path": "gateway/deepseek-v4-flash-nginx-staging.conf",
         "install_path": "/etc/deepseek-v4-flash/nginx-staging.conf", "mode": "0644", "sha256": None},
        {"source_path": "systemd/deepseek-v4-flash-gateway-staging.service",
         "install_path": "/etc/systemd/system/deepseek-v4-flash-gateway-staging.service",
         "mode": "0644", "sha256": None},
    ]
    if (templates != expected_templates or integration.get("semantic_constraints") !=
            {"listen": "127.0.0.1:11435", "restart": "no", "manual_only": True}):
        raise PreparationError("integration artifact lock must contain exactly two files")
    rows = []
    source_bytes = {}
    for template in templates:
        source = input_root / safe_relative_path(template.get("source_path"))
        if source.is_symlink() or not source.is_file() or template.get("mode") != "0644":
            raise PreparationError("integration artifact source or mode is invalid")
        source_bytes[template["source_path"]] = source.read_text(encoding="utf-8")
        rows.append({
            "source_path": template["source_path"],
            "install_path": template["install_path"],
            "mode": template["mode"],
            "sha256": file_sha256(source),
        })
    nginx = source_bytes["gateway/deepseek-v4-flash-nginx-staging.conf"]
    unit = source_bytes["systemd/deepseek-v4-flash-gateway-staging.service"]
    if ("127.0.0.1:11435" not in nginx or "Restart=no" not in unit or "[Install]" in unit or
            "WantedBy=" in unit or "RequiredBy=" in unit):
        raise PreparationError("staging gateway integration files violate loopback/manual/Restart=no constraints")

    service_sources = []
    for template in lock.get("service_assets", {}).get("source_files", []):
        relative = safe_relative_path(template.get("source_path"))
        source = input_root / relative
        if source.is_symlink() or not source.is_file():
            raise PreparationError(f"missing service asset source: {relative}")
        service_sources.append({"source_path": relative, "sha256": file_sha256(source)})

    created = []
    try:
        for row in rows:
            source = input_root / row["source_path"]
            destination = ((install_root / row["install_path"].lstrip("/"))
                           if test_mode else Path(row["install_path"]))
            if destination.exists():
                if destination.is_symlink() or not destination.is_file() or file_sha256(destination) != row["sha256"]:
                    raise PreparationError(f"refusing to overwrite a different integration artifact: {destination}")
                continue
            if test_mode:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                os.chmod(destination, 0o644)
            else:
                result = subprocess.run([
                    "/usr/bin/sudo", "-n", "/usr/bin/install", "-D", "-m", "0644",
                    str(source), str(destination),
                ])
                if result.returncode:
                    raise PreparationError(f"integration artifact install failed with exit {result.returncode}")
            created.append(destination)
        if not test_mode:
            result = subprocess.run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "daemon-reload"])
            if result.returncode:
                raise PreparationError(f"systemd daemon-reload failed with exit {result.returncode}")
        atomic_json(args.integration_receipt, {
            "schema_version": 1,
            "files": rows,
            "service_asset_sources": service_sources,
            "semantic_constraints": integration["semantic_constraints"],
        })
    except BaseException:
        for destination in reversed(created):
            if test_mode:
                destination.unlink(missing_ok=True)
            else:
                subprocess.run(["/usr/bin/sudo", "-n", "/usr/bin/rm", "-f", str(destination)])
        if created and not test_mode:
            subprocess.run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "daemon-reload"])
        raise
    print("INTEGRATION_ARTIFACTS_INSTALLED_AND_RECEIPTED")


def expected_service_asset_identity(role):
    common = [
        ("copy", "scripts/prepare-deepseek-v4-flash-dspark.sh", "/usr/local/libexec/prepare-deepseek-v4-flash-dspark.sh", "0755"),
        ("copy", "scripts/run-deepseek-v4-flash-dspark.sh", "/usr/local/libexec/run-deepseek-v4-flash-dspark.sh", "0755"),
        ("render-env", "configs/deepseek-v4-flash-dspark.env.example", "/etc/deepseek-v4-flash-dspark.env", "0644"),
        ("patched-compose", "docker-compose.dspark.yml", "/opt/deepseek-v4-flash-dspark/docker-compose.dspark.yml", "0644"),
        ("copy", "systemd/deepseek-v4-flash-dspark.service", "/etc/systemd/system/deepseek-v4-flash-dspark.service", "0644"),
    ]
    if role == "worker":
        return common
    return common + [
        ("copy", "scripts/recover-deepseek-v4-flash.py", "/usr/local/libexec/recover-deepseek-v4-flash", "0755"),
        ("copy", "systemd/deepseek-v4-flash-gateway.service", "/etc/systemd/system/deepseek-v4-flash-gateway.service", "0644"),
        ("copy", "systemd/deepseek-v4-flash-gateway-staging.service", "/etc/systemd/system/deepseek-v4-flash-gateway-staging.service", "0644"),
        ("copy", "systemd/deepseek-v4-flash-fallback.service", "/etc/systemd/system/deepseek-v4-flash-fallback.service", "0644"),
        ("copy", "systemd/deepseek-v4-flash-fallback.timer", "/etc/systemd/system/deepseek-v4-flash-fallback.timer", "0644"),
        ("copy", "gateway/deepseek-v4-flash-nginx.conf", "/etc/deepseek-v4-flash/nginx.conf", "0644"),
        ("copy", "gateway/deepseek-v4-flash-nginx-staging.conf", "/etc/deepseek-v4-flash/nginx-staging.conf", "0644"),
        ("secret-map", None, "/etc/deepseek-v4-flash/gateway-secret.conf", "0600"),
    ]


def render_role_env(raw, role, worker_image_id):
    text = raw.decode("utf-8")
    lines = text.splitlines()
    matches = [index for index, line in enumerate(lines) if line.startswith("DSPARK_ROLE=")]
    if len(matches) != 1:
        raise PreparationError("service env template must contain one fixed DSPARK_ROLE assignment")
    lines[matches[0]] = f"DSPARK_ROLE={role}"
    image_indexes = [index for index, line in enumerate(lines) if line.startswith("DSPARK_VLLM_IMAGE=")]
    if len(image_indexes) != 1:
        raise PreparationError("service env template must contain one fixed DSPARK_VLLM_IMAGE assignment")
    if role == "worker":
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(worker_image_id)):
            raise PreparationError("worker service image ID is not a finalized config digest")
        lines[image_indexes[0]] = f"DSPARK_VLLM_IMAGE={worker_image_id}"
    replacements = {
        "NODE_RANK": "0" if role == "head" else "1",
        "HEADLESS": "" if role == "head" else "1",
        "VLLM_HOST_IP": "192.0.2.33" if role == "head" else "192.0.2.34",
    }
    for key, value in replacements.items():
        indexes = [index for index, line in enumerate(lines) if line.startswith(key + "=")]
        if len(indexes) > 1:
            raise PreparationError(f"service env template repeats {key}")
        if indexes:
            lines[indexes[0]] = f"{key}={value}"
        else:
            lines.append(f"{key}={value}")
    rendered = "\n".join(lines) + "\n"
    for key in ("VLLM_API_KEY", "DSPARK_API_KEYS"):
        if f"{key}=\n" not in rendered:
            raise PreparationError("service env template must leave upstream API secret variables empty")
    return rendered.encode("utf-8")


def secret_map_bytes(path):
    try:
        token = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise PreparationError("gateway secret FD is absent or unreadable") from exc
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token):
        raise PreparationError("gateway secret must be one bounded base64url bearer without a newline")
    return ("map $http_authorization $deepseek_auth_ok {\n"
            "    default 0;\n"
            f'    "Bearer {token}" 1;\n'
            "}\n").encode("utf-8")


def service_destination(path, test_root):
    return Path(test_root) / path.lstrip("/") if test_root is not None else Path(path)


def installed_file_sha256(path, test_mode):
    try:
        return file_sha256(path)
    except PermissionError:
        if test_mode:
            raise
        result = subprocess.run(
            ["/usr/bin/sudo", "-n", "/usr/bin/sha256sum", "--", str(path)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        fields = result.stdout.split()
        if result.returncode or len(fields) != 2 or not is_sha256(fields[0]):
            raise PreparationError(f"privileged service asset SHA-256 failed: {path}")
        return fields[0]


def system_unit_state(unit):
    result = subprocess.run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "is-enabled", unit],
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    value = (result.stdout or result.stderr).strip().splitlines()
    state = value[-1] if value else ""
    if state not in {"disabled", "static", "not-found"}:
        raise PreparationError(f"service unit preimage is not disabled/manual: {unit}: {state}")
    return state


def cmd_install_service_assets(args):
    test_mode = args.test_input_root is not None or args.test_install_root is not None
    if test_mode and (args.test_input_root is None or args.test_install_root is None or
                      not marked_local_test_seam("DEEPSEEK_ARTIFACT_TEST_SEAM")):
        raise PreparationError("service asset test roots require the marked local test seam")
    if not test_mode and (str(args.final_lock) !=
            "/home/operator/.local/state/deepseek-v4-flash-dspark/artifacts/deployment.lock.json" or
            str(args.source_root) != "/home/operator/dgx-models/deepseek-v4-flash-dspark/source"):
        raise PreparationError("production service asset inputs must use the fixed deployment/source paths")
    input_root = Path(args.test_input_root) if test_mode else Path(INPUT_ROOT)
    install_root = Path(args.test_install_root) if test_mode else None
    lock = read_json(args.final_lock)
    # This verifies every embedded source/license/image/node/integration receipt before any target write.
    cmd_verify_deployment(argparse.Namespace(lock=args.final_lock))
    service = lock.get("service_assets", {})
    role_rows = service.get("roles", {}).get(args.role)
    identity = [(row.get("kind"), row.get("source_path"), row.get("install_path"), row.get("mode"))
                for row in role_rows] if isinstance(role_rows, list) else None
    if (service.get("finalized") is not True or identity != expected_service_asset_identity(args.role) or
            service.get("constraints") != {
                "services_restart": "no", "units_initially_disabled": True,
                "secret_owner": "root:root", "secret_mode": "0600",
                "compose_service": "vllm-dspark",
                "worker_forbidden_kinds": ["gateway", "fallback", "secret"]}):
        raise PreparationError("final deployment lock has a non-canonical service asset allowlist")
    source_hashes = {row.get("source_path"): row.get("sha256")
                     for row in service.get("source_files", [])}
    if (len(source_hashes) != len(service.get("source_files", [])) or
            not all(is_sha256(value) for value in source_hashes.values())):
        raise PreparationError("final deployment lock has an incomplete service source hash closure")
    compose_targets = {row["path"]: row["postimage_sha256"] for row in lock["source"]["patch_targets"]}
    compose_hash = compose_targets.get(service.get("compose_source_path"))
    if not is_sha256(compose_hash):
        raise PreparationError("final deployment lock does not bind the patched compose postimage")

    secret_bytes = None
    if args.role == "head":
        if args.secret_fd is None or (str(args.secret_fd) != "/proc/self/fd/4" and not test_mode):
            raise PreparationError("head service install requires the gateway secret on /proc/self/fd/4")
        secret_bytes = secret_map_bytes(args.secret_fd)
    elif args.secret_fd is not None:
        raise PreparationError("worker service install forbids a gateway secret FD")

    prepared = []
    for row in role_rows:
        kind = row["kind"]
        if kind == "secret-map":
            payload = secret_bytes
            source_identity = "fd4:gateway-secret-map"
        elif kind == "patched-compose":
            source = Path(args.source_root) / row["source_path"]
            if source.is_symlink() or not source.is_file() or file_sha256(source) != compose_hash:
                raise PreparationError("patched compose source differs from the final source receipt")
            payload = source.read_bytes()
            source_identity = f"source:{row['source_path']}"
        else:
            source = input_root / safe_relative_path(row["source_path"])
            if (source.is_symlink() or not source.is_file() or
                    file_sha256(source) != source_hashes.get(row["source_path"])):
                raise PreparationError(f"service asset source differs from final lock: {row['source_path']}")
            payload = source.read_bytes()
            source_identity = f"input:{row['source_path']}"
            if kind == "render-env":
                payload = render_role_env(
                    payload, args.role, lock["runtime_image"]["node_image_ids"]["worker"]
                )
        if row["install_path"].startswith("/etc/systemd/system/") and row["install_path"].endswith(".service"):
            text = payload.decode("utf-8")
            if "Restart=no" not in text or "Restart=always" in text or "Restart=on-failure" in text:
                raise PreparationError(f"service unit does not pin Restart=no: {row['install_path']}")
        prepared.append((row, payload, hashlib.sha256(payload).hexdigest(), source_identity))

    preimages = []
    for row, payload, digest, source_identity in prepared:
        destination = service_destination(row["install_path"], install_root)
        if destination.exists():
            if (destination.is_symlink() or not destination.is_file() or
                    installed_file_sha256(destination, test_mode) != digest):
                raise PreparationError(f"refusing to overwrite a different service asset: {destination}")
            state = destination.stat()
            preimages.append({"install_path": row["install_path"], "state": "same",
                              "sha256": digest, "mode": f"{state.st_mode & 0o777:04o}",
                              "uid": state.st_uid, "gid": state.st_gid})
        else:
            preimages.append({"install_path": row["install_path"], "state": "absent"})
    unit_states = ({unit: "test-disabled" for unit in SERVICE_UNITS[args.role]} if test_mode else
                   {unit: system_unit_state(unit) for unit in SERVICE_UNITS[args.role]})

    created = []
    staged = []
    try:
        for row, payload, digest, source_identity in prepared:
            destination = service_destination(row["install_path"], install_root)
            if destination.exists():
                continue
            if test_mode:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(payload)
                os.chmod(destination, int(row["mode"], 8))
            else:
                fd, temporary = tempfile.mkstemp(prefix="deepseek-service-asset.")
                staged.append(temporary)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                result = subprocess.run(["/usr/bin/sudo", "-n", "/usr/bin/install", "-D", "-o", "root", "-g", "root",
                                         "-m", row["mode"], temporary, str(destination)])
                if result.returncode:
                    raise PreparationError(f"service asset install failed with exit {result.returncode}")
            created.append(destination)
        if not test_mode:
            result = subprocess.run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "daemon-reload"])
            if result.returncode:
                raise PreparationError(f"systemd daemon-reload failed with exit {result.returncode}")
            for unit in SERVICE_UNITS[args.role]:
                system_unit_state(unit)
        targets = []
        for row, payload, digest, source_identity in prepared:
            destination = service_destination(row["install_path"], install_root)
            state = destination.stat()
            expected_uid = os.getuid() if test_mode else 0
            expected_gid = os.getgid() if test_mode else 0
            if (installed_file_sha256(destination, test_mode) != digest or
                    f"{state.st_mode & 0o777:04o}" != row["mode"] or
                    state.st_uid != expected_uid or state.st_gid != expected_gid):
                raise PreparationError(f"installed service asset identity mismatch: {row['install_path']}")
            targets.append({"kind": row["kind"], "source": source_identity,
                            "install_path": row["install_path"], "mode": row["mode"],
                            "sha256": digest, "uid": state.st_uid, "gid": state.st_gid})
        atomic_json(args.asset_receipt, {
            "schema_version": 1,
            "kind": "deepseek-v4-flash-service-assets-receipt",
            "role": args.role,
            "final_lock_sha256": file_sha256(args.final_lock),
            "source_commit": lock["source"]["commit"],
            "source_receipt_sha256": lock["finalization"]["source"]["sha256"],
            "integration_receipt_sha256": lock["finalization"]["integration"]["sha256"],
            "preimages": preimages,
            "unit_preimages": unit_states,
            "targets": targets,
            "rollback": {"policy": "remove-new-files-only", "created_paths": [str(path) for path in created]},
        })
    except BaseException:
        for destination in reversed(created):
            if test_mode:
                destination.unlink(missing_ok=True)
            else:
                subprocess.run(["/usr/bin/sudo", "-n", "/usr/bin/rm", "-f", str(destination)])
        if created and not test_mode:
            subprocess.run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "daemon-reload"])
        raise
    finally:
        for temporary in staged:
            Path(temporary).unlink(missing_ok=True)
    print(f"SERVICE_ASSETS_INSTALLED role={args.role} targets={len(prepared)}")


def run_text(argv, timeout=60, input_text=None):
    result = subprocess.run(argv, text=True, input=input_text, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        raise PreparationError(f"fixed probe command failed ({argv[0]}): {result.stderr.strip()}")
    return result.stdout


def remote_text(argv, timeout=60, input_text=None):
    return run_text([*FABRIC_SSH, shlex.join(argv)], timeout=timeout, input_text=input_text)


LIVE_NODE_SCRIPT = r'''
import hashlib,json,pathlib,subprocess
role=__import__('sys').argv[1]
interfaces=['enp1s0f0np0','enP2p1s0f0np0']; hcas=['rocep1s0f0','roceP2p1s0f0']
def run(argv):
 r=subprocess.run(argv,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 if r.returncode: raise SystemExit('probe command failed: '+argv[0]+': '+r.stderr.strip())
 return r.stdout.strip()
def read(path): return pathlib.Path(path).read_text().strip()
rails={}
for interface,hca in zip(interfaces,hcas):
 info=run(['/usr/sbin/ethtool','-i',interface])
 fields=dict(line.split(':',1) for line in info.splitlines() if ':' in line)
 cidrs=[line.split()[3] for line in run(['/usr/sbin/ip','-o','-4','addr','show','dev',interface]).splitlines()]
 gids=run(['/usr/sbin/show_gids'])
 gid_lines=[line for line in gids.splitlines()
            if len(line.split()) >= 6 and line.split()[0] == hca and
            line.split()[2] == '3' and 'v2' in line.split()]
 rails[interface]={'hca':hca+':1','mac':read('/sys/class/net/'+interface+'/address'),
  'pci':fields.get('bus-info','').strip(),'firmware':fields.get('firmware-version','').strip().split()[0],
  'ip_cidrs':cidrs,'mtu':int(read('/sys/class/net/'+interface+'/mtu')),
  'speed_mbps':int(read('/sys/class/net/'+interface+'/speed')),'gid_index':3,'gid_lines':gid_lines}
driver=run(['/usr/bin/nvidia-smi','--query-gpu=driver_version','--format=csv,noheader']).splitlines()[0]
release=pathlib.Path('/etc/dgx-release').read_text()
ota=next((line.split('=',1)[1].strip('"') for line in release.splitlines()
          if line.startswith('DGX_OTA_VERSION=')), 'unknown')
netplan=run(['/usr/bin/sudo','-n','/usr/bin/sha256sum','/etc/netplan/80-minimax-h3-fabric.yaml']).split()[0]
listeners=run(['/usr/bin/ss','-ltnH']).splitlines()
defaults=run(['/usr/sbin/ip','-j','route','show','default'])
print(json.dumps({'hostname':run(['/usr/bin/hostname']),'user':run(['/usr/bin/id','-un']),
 'boot_id':read('/proc/sys/kernel/random/boot_id'),'kernel':run(['/usr/bin/uname','-r']),
 'driver':driver,'dgx_ota':ota,'netplan_sha256':netplan,'rails':rails,
 'default_routes':json.loads(defaults),
 'http_listeners':[line for line in listeners if any(':'+p in line for p in ['11434','11435','18888'])]},sort_keys=True))
'''


COUNTER_SCRIPT = r'''
import json,pathlib
out={}
for hca in ['rocep1s0f0','roceP2p1s0f0']:
 out[hca]={}
 for directory in ['counters','hw_counters']:
  for path in pathlib.Path('/sys/class/infiniband',hca,'ports/1',directory).glob('*'):
   try: out[hca][path.name]=int(path.read_text().strip())
   except (ValueError,OSError): pass
print(json.dumps(out,sort_keys=True))
'''


def sha256_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def live_node(role):
    argv = ["/usr/bin/python3", "-c", LIVE_NODE_SCRIPT, role]
    raw = run_text(argv) if role == "head" else remote_text(argv)
    return json.loads(raw)


def counter_snapshot(role):
    argv = ["/usr/bin/python3", "-c", COUNTER_SCRIPT]
    raw = run_text(argv) if role == "head" else remote_text(argv)
    return json.loads(raw)


def validate_counter_delta(before, after):
    risky = ("retry", "retrans", "timeout", "discard", "error", "err", "drop")
    changes = []
    for role in ("head", "worker"):
        for hca, counters in before[role].items():
            for name, old in counters.items():
                new = after[role].get(hca, {}).get(name)
                if new is None:
                    raise PreparationError(f"fabric counter disappeared: {role}/{hca}/{name}")
                if any(token in name.lower() for token in risky) and new != old:
                    changes.append({"role": role, "hca": hca, "counter": name, "before": old, "after": new})
    if changes:
        raise PreparationError("risky retry/retrans/timeout/discard/error counters changed")
    return changes


def validate_data_participation(before, after):
    deltas = {}
    for role in ("head", "worker"):
        deltas[role] = {}
        for hca in FABRIC_HCAS:
            values = {}
            for counter in ("port_xmit_data", "port_rcv_data"):
                old = before.get(role, {}).get(hca, {}).get(counter)
                new = after.get(role, {}).get(hca, {}).get(counter)
                if not isinstance(old, int) or not isinstance(new, int) or new <= old:
                    raise PreparationError(f"production NCCL probe did not prove {role}/{hca}/{counter} participation")
                values[counter] = new - old
            deltas[role][hca] = values
    return deltas


def docker_probe_argv(image, rank, probe_path, container_name, mode="run"):
    base = [*DOCKER_COMMAND, "run", "--rm", "--name", container_name,
            "--label", "deepseek-v4-flash-dspark.probe=production-nccl", "--network", "host", "--gpus", "all",
            "--ipc", "host", "--shm-size", "64g", "--ulimit", "memlock=-1:-1",
            "--device", "/dev/infiniband", "--mount", f"type=bind,src={probe_path},dst=/opt/deepseek-nccl-probe.py,readonly",
            "-e", "HF_HUB_OFFLINE=1", "-e", "TRANSFORMERS_OFFLINE=1", "-e", "NCCL_NET=IB",
            "-e", "NCCL_IB_DISABLE=0", "-e", "NCCL_IB_HCA==rocep1s0f0:1,roceP2p1s0f0:1",
            "-e", "NCCL_IB_GID_INDEX=3", "-e", "NCCL_SOCKET_IFNAME==enp1s0f0np0",
            "-e", "NCCL_CROSS_NIC=0", "-e", "NCCL_DEBUG=INFO", "-e", "NCCL_DEBUG_SUBSYS=INIT,NET,GRAPH",
            "--entrypoint", "/usr/bin/python3",
            image]
    if mode == "preflight":
        return [*base, "-c", "import torch; assert torch.cuda.is_available() and torch.distributed.is_nccl_available()"]
    return [*base, "-m", "torch.distributed.run", "--nnodes=2", "--nproc-per-node=1",
            f"--node-rank={rank}", "--master-addr=192.0.2.33", "--master-port=25001",
            "/opt/deepseek-nccl-probe.py"]


def parse_rank_log(raw, expected_rank):
    forbidden = ("NCCL WARN", "NCCL ERROR", "NET/Socket", "socket fallback", "fatal", "transport error")
    if any(token.lower() in raw.lower() for token in forbidden):
        raise PreparationError(f"production image rank {expected_rank} log contains NCCL fatal/socket fallback")
    rows = []
    for line in raw.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "rank" in value:
            rows.append(value)
    if len(rows) != 1 or rows[0].get("rank") != expected_rank or rows[0].get("world_size") != 2 or rows[0].get("wrong") != 0:
        raise PreparationError(f"production image rank {expected_rank} result is incomplete or wrong")
    if not all(hca in raw for hca in FABRIC_HCAS):
        raise PreparationError(f"production image rank {expected_rank} log does not show both locked HCAs")
    return rows[0]


def cleanup_probe_container(role, name):
    script = ("import json,subprocess,sys; a=json.loads(sys.argv[1]); "
              "p=subprocess.run(a+['container','inspect',sys.argv[2]],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
              "r=subprocess.run(a+['rm','-f',sys.argv[2]],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) if p.returncode==0 else None; "
              "print('removed' if r is not None and r.returncode==0 else 'absent' if r is None else 'cleanup-failed'); "
              "raise SystemExit(0 if r is None or r.returncode==0 else 2)")
    argv = ["/usr/bin/python3", "-c", script, json.dumps(DOCKER_COMMAND), name]
    return (remote_text if role == "worker" else run_text)(argv, timeout=60).strip()


def run_production_nccl_probe(lock, probe_path, transaction_id):
    if not re.fullmatch(r"[0-9a-f]{32}", transaction_id):
        raise PreparationError("production probe transaction ID is not 32 lowercase hex characters")
    runtime = lock["runtime_image"]
    image_refs = {
        "head": runtime["reference"],
        "worker": runtime["node_image_ids"]["worker"],
    }
    if image_refs["worker"] != runtime["config_digest"]:
        raise PreparationError("worker production probe image ID differs from the finalized config digest")
    for role in ("worker", "head"):
        rank = 1 if role == "worker" else 0
        preflight_name = f"deepseek-nccl-preflight-{rank}-{transaction_id[:12]}"
        argv = docker_probe_argv(image_refs[role], rank, probe_path,
                                 preflight_name, "preflight")
        try:
            (remote_text if role == "worker" else run_text)(argv, timeout=120)
        finally:
            cleanup_probe_container(role, preflight_name)
    before = {role: counter_snapshot(role) for role in ("head", "worker")}
    worker_name = f"deepseek-nccl-probe-1-{transaction_id[:12]}"
    head_name = f"deepseek-nccl-probe-0-{transaction_id[:12]}"
    worker_argv = docker_probe_argv(image_refs["worker"], 1, probe_path, worker_name)
    head_argv = docker_probe_argv(image_refs["head"], 0, probe_path, head_name)
    worker = subprocess.Popen([*FABRIC_SSH, shlex.join(worker_argv)], text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    cleanup = {}
    probe_error = None
    try:
        time.sleep(1)
        head_log = run_text(head_argv, timeout=300)
        worker_log, _ = worker.communicate(timeout=300)
    except BaseException as exc:
        probe_error = exc
        worker.terminate()
        try:
            worker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            worker.kill()
    finally:
        cleanup, cleanup_errors = best_effort_cleanup(
            (("head", head_name), ("worker", worker_name)),
            lambda item: cleanup_probe_container("head" if item == head_name else "worker", item))
    if probe_error is not None:
        if cleanup_errors:
            raise PreparationError("production NCCL probe failed and exact container cleanup was incomplete: " +
                                   "; ".join(cleanup_errors)) from probe_error
        raise probe_error
    if cleanup_errors:
        raise PreparationError("production NCCL probe completed but exact container cleanup was incomplete: " +
                               "; ".join(cleanup_errors))
    if worker.returncode:
        raise PreparationError(f"production image worker rank exited {worker.returncode}")
    after = {role: counter_snapshot(role) for role in ("head", "worker")}
    changes = validate_counter_delta(before, after)
    data_deltas = validate_data_participation(before, after)
    return {
        "commands_sha256": sha256_json({"head": head_argv, "worker": worker_argv}),
        "head_log_sha256": hashlib.sha256(head_log.encode()).hexdigest(),
        "worker_log_sha256": hashlib.sha256(worker_log.encode()).hexdigest(),
        "ranks": {"head": parse_rank_log(head_log, 0), "worker": parse_rank_log(worker_log, 1)},
        "counter_before_sha256": sha256_json(before), "counter_after_sha256": sha256_json(after),
        "risky_counter_changes": changes, "exit_codes": {"head": 0, "worker": 0},
        "selected_hcas": list(FABRIC_HCAS), "data_counter_deltas": data_deltas,
        "container_cleanup": cleanup,
    }


def validate_consumer_components(fabric, nodes, ssh_trust, df_jumbo, production_probe):
    expected_ips = {
        "head": (["192.0.2.33/30", "192.0.2.35/30"], "192.0.2.30"),
        "worker": (["192.0.2.34/30", "192.0.2.36/30"], "192.0.2.31"),
    }
    if set(nodes) != {"head", "worker"}:
        raise PreparationError("fabric consumer node set is incomplete")
    for role, (cidrs, management) in expected_ips.items():
        node = nodes[role]
        expected = fabric["cluster"][role]
        if (node.get("hostname") != expected["hostname"] or node.get("management_ip") != management or
                node.get("fabric_ips") != [value.split("/", 1)[0] for value in cidrs] or
                not isinstance(node.get("boot_id"), str) or not node["boot_id"] or
                node.get("kernel") != expected["kernel"] or node.get("driver") != expected["nvidia_driver"] or
                node.get("dgx_ota") != "7.5.0" or
                node.get("netplan_sha256") != fabric["fabric"]["persistent_netplan"][f"{role}_sha256"]):
            raise PreparationError(f"{role} consumer identity/version/address/listener evidence is invalid")
        listeners = node.get("http_listeners")
        if (role == "worker" and listeners != []) or (
                role == "head" and (not isinstance(listeners, list) or len(listeners) != 1 or
                                    "192.0.2.30:11434" not in listeners[0])):
            raise PreparationError(f"{role} consumer listener evidence is invalid")
        if (not isinstance(node.get("default_routes"), list) or
                any(route.get("dev") in FABRIC_INTERFACES for route in node["default_routes"])):
            raise PreparationError(f"{role} has an invalid fabric default route")
        rails = node.get("rails")
        if not isinstance(rails, dict) or set(rails) != set(FABRIC_INTERFACES):
            raise PreparationError(f"{role} fabric rail set is incomplete")
        for index, (interface, hca) in enumerate(zip(FABRIC_INTERFACES, FABRIC_HCAS)):
            rail = rails[interface]
            if (rail.get("hca") != f"{hca}:1" or rail.get("ip_cidrs") != [cidrs[index]] or
                    rail.get("mtu") != 9000 or rail.get("speed_mbps") != 200000 or
                    rail.get("gid_index") != 3 or not rail.get("gid_lines") or
                    rail.get("firmware") != fabric["cluster"]["hca_firmware"] or
                    not re.fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", str(rail.get("mac", ""))) or
                    not isinstance(rail.get("pci"), str) or not rail["pci"]):
                raise PreparationError(f"{role} {interface} MAC/PCI/HCA/fw/IP/MTU/GID evidence is invalid")
    if (not is_sha256(ssh_trust.get("known_hosts_sha256")) or
            not str(ssh_trust.get("dedicated_public_key_fingerprint", "")).startswith("SHA256:") or
            ssh_trust.get("pinned_addresses") != ["192.0.2.31", "192.0.2.34", "192.0.2.36"]):
        raise PreparationError("dedicated management/fabric SSH trust evidence is incomplete")
    expected_df = {
        "head:enp1s0f0np0:192.0.2.34", "head:enP2p1s0f0np0:192.0.2.36",
        "worker:enp1s0f0np0:192.0.2.33", "worker:enP2p1s0f0np0:192.0.2.35",
    }
    if (set(df_jumbo) != expected_df or any(value.get("exit_code") != 0 or
            not is_sha256(value.get("output_sha256")) for value in df_jumbo.values())):
        raise PreparationError("four-direction DF8972 evidence is incomplete")
    if (production_probe.get("exit_codes") != {"head": 0, "worker": 0} or
            production_probe.get("risky_counter_changes") != [] or
            production_probe.get("selected_hcas") != list(FABRIC_HCAS) or
            set(production_probe.get("container_cleanup", {})) != {"head", "worker"} or
            any(state not in {"absent", "removed"} for state in
                production_probe.get("container_cleanup", {}).values()) or
            not all(is_sha256(production_probe.get(key)) for key in (
                "commands_sha256", "head_log_sha256", "worker_log_sha256",
                "counter_before_sha256", "counter_after_sha256"))):
        raise PreparationError("production-image probe hashes/exits/counters are incomplete")
    data_deltas = production_probe.get("data_counter_deltas")
    if (not isinstance(data_deltas, dict) or set(data_deltas) != {"head", "worker"} or
            any(set(data_deltas[role]) != set(FABRIC_HCAS) for role in data_deltas) or
            any(set(counters) != {"port_xmit_data", "port_rcv_data"} or
                any(not isinstance(delta, int) or delta <= 0 for delta in counters.values())
                for role in data_deltas.values() for counters in role.values())):
        raise PreparationError("production-image probe does not prove both locked HCAs carried data")
    ranks = production_probe.get("ranks")
    if not isinstance(ranks, dict) or set(ranks) != {"head", "worker"}:
        raise PreparationError("production-image two-rank result set is incomplete")
    for role, expected_rank in (("head", 0), ("worker", 1)):
        rank = ranks[role]
        if (rank.get("rank") != expected_rank or rank.get("world_size") != 2 or rank.get("backend") != "nccl" or
                rank.get("wrong") != 0 or not isinstance(rank.get("torch_version"), str) or
                not isinstance(rank.get("cuda_version"), str) or not isinstance(rank.get("nccl_version"), list) or
                not isinstance(rank.get("device_name"), str) or not isinstance(rank.get("duration_seconds"), (int, float)) or
                rank.get("iterations") != 32 or rank.get("elements") != 16777216 or
                rank.get("bytes_per_tensor") != 67108864):
            raise PreparationError(f"production-image {role} rank compatibility result is incomplete")


def cmd_qualify_fabric_consumer(args):
    test_mode = args.test_consumer_fixture is not None or args.test_worker_root is not None
    if test_mode and (args.test_consumer_fixture is None or args.test_worker_root is None or
                      not marked_local_test_seam("DEEPSEEK_ARTIFACT_TEST_SEAM")):
        raise PreparationError("fabric consumer test fixtures require the marked local test seam")
    if not test_mode and (str(args.output) !=
            "/home/operator/.local/state/deepseek-v4-flash-dspark/artifacts/fabric-consumer.receipt.json"):
        raise PreparationError("production fabric consumer receipt must use the fixed path")
    fabric = read_json(args.fabric_lock)
    if read_json(args.provenance_addendum) != fabric.get("provenance"):
        raise PreparationError("fabric provenance differs from the pinned addendum")
    deployment = read_json(args.deployment_lock)
    cmd_verify_deployment(argparse.Namespace(lock=args.deployment_lock))
    policy = fabric.get("production_image_probe", {})
    probe_path = (Path(args.test_input_root) if test_mode and args.test_input_root else Path(INPUT_ROOT)) / policy.get("implementation_path", "")
    if (probe_path.is_symlink() or not probe_path.is_file() or
            file_sha256(probe_path) != policy.get("implementation_sha256") or
            deployment["runtime_image"]["manifest_digest"] != policy.get("runtime_manifest_digest") or
            deployment["runtime_image"]["config_digest"] != policy.get("runtime_config_digest")):
        raise PreparationError("production NCCL probe script or runtime identity differs from the fabric lock")
    if test_mode:
        live = read_json(args.test_consumer_fixture)
        nodes = live["nodes"]
        ssh_trust = live["ssh_trust"]
        df_jumbo = live["df_jumbo"]
        production_probe = live["production_image_probe"]
    else:
        nodes = {role: live_node(role) for role in ("head", "worker")}
        expected = fabric["cluster"]
        for role in ("head", "worker"):
            node = nodes[role]
            node["management_ip"] = expected[role]["management_ip"]
            node["fabric_ips"] = (["192.0.2.33", "192.0.2.35"] if role == "head" else
                                  ["192.0.2.34", "192.0.2.36"])
            if (node["hostname"] != expected[role]["hostname"] or node["user"] != "operator" or
                    node["kernel"] != expected[role]["kernel"] or node["driver"] != expected[role]["nvidia_driver"] or
                    node["netplan_sha256"] != fabric["fabric"]["persistent_netplan"][f"{role}_sha256"]):
                raise PreparationError(f"{role} live identity/version/netplan differs from H3 qualification")
            if role == "worker" and node["http_listeners"]:
                raise PreparationError("worker exposes an HTTP listener before qualification")
        df_jumbo = {}
        peers = {"head": ["192.0.2.34", "192.0.2.36"],
                 "worker": ["192.0.2.33", "192.0.2.35"]}
        for role in ("head", "worker"):
            for interface, peer in zip(FABRIC_INTERFACES, peers[role]):
                argv = ["/usr/bin/ping", "-I", interface, "-M", "do", "-s", "8972", "-c", "20", "-W", "1", peer]
                output = (run_text if role == "head" else remote_text)(argv, timeout=45)
                df_jumbo[f"{role}:{interface}:{peer}"] = {"exit_code": 0, "output_sha256": hashlib.sha256(output.encode()).hexdigest()}
        known_hosts = Path("/home/operator/.ssh/deepseek-v4-flash-worker-known_hosts")
        public_key = Path("/home/operator/.ssh/deepseek-v4-flash-worker.pub")
        if known_hosts.is_symlink() or public_key.is_symlink() or not known_hosts.is_file() or not public_key.is_file():
            raise PreparationError("dedicated worker SSH trust files are absent")
        for address in ("192.0.2.31", "192.0.2.34", "192.0.2.36"):
            run_text(["/usr/bin/ssh-keygen", "-F", address, "-f", str(known_hosts)])
        fingerprint = run_text(["/usr/bin/ssh-keygen", "-lf", str(public_key), "-E", "sha256"]).split()[1]
        remote_hostname = remote_text(["/usr/bin/hostname"]).strip()
        remote_user = remote_text(["/usr/bin/id", "-un"]).strip()
        if (remote_hostname, remote_user) != ("dgx-worker", "operator"):
            raise PreparationError("strict dedicated fabric SSH identity failed")
        ssh_trust = {"known_hosts_sha256": file_sha256(known_hosts),
                     "dedicated_public_key_fingerprint": fingerprint,
                     "pinned_addresses": ["192.0.2.31", "192.0.2.34", "192.0.2.36"]}
        production_probe = run_production_nccl_probe(deployment, probe_path, args.mutation_receipt["transaction_id"])
    validate_consumer_components(fabric, nodes, ssh_trust, df_jumbo, production_probe)
    worker_listeners = len(nodes["worker"].get("http_listeners", []))
    if worker_listeners:
        raise PreparationError("worker HTTP listener count is nonzero")
    mutation = args.mutation_receipt
    receipt = {
        "schema_version": 1, "kind": "deepseek-v4-flash-fabric-consumer-receipt", "terminal": "PASS",
        "transaction_id": mutation["transaction_id"], "lease_generation": mutation["lease_generation"],
        "cluster_id": "dgx-head+dgx-worker", "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "qualification": {key: fabric[key] for key in ("qualification_id", "qualification_path", "producer_receipt",
                                                         "candidate_sha256sums", "score_sha256s", "audit_seal_sha256")},
        "nodes": nodes, "ssh_trust": ssh_trust, "df_jumbo": df_jumbo,
        "historical_release": {"producer_receipt_sha256": fabric["producer_receipt"]["sha256"],
                               "provenance_sha256": file_sha256(args.provenance_addendum),
                               "candidate_sha256sums": fabric["candidate_sha256sums"],
                               "score_sha256s": fabric["score_sha256s"], "audit_seal_sha256": fabric["audit_seal_sha256"]},
        "runtime": {"manifest_digest": deployment["runtime_image"]["manifest_digest"],
                    "config_digest": deployment["runtime_image"]["config_digest"],
                    "rootfs_diff_ids": deployment["runtime_image"]["rootfs_diff_ids"],
                    "platform": deployment["runtime_image"]["platform"], "probe_script_sha256": file_sha256(probe_path)},
        "production_image_probe": production_probe, "worker_http_listener_count": 0,
        "network_or_firewall_rewritten": False,
    }
    body_sha = sha256_json(receipt)
    receipt["replication"] = {
        "head_path": str(args.output),
        "worker_path": "/home/operator/.local/state/deepseek-v4-flash-dspark/artifacts/fabric-consumer.receipt.json",
        "canonical_body_sha256": body_sha,
    }
    atomic_json(args.output, receipt)
    content_sha = file_sha256(args.output)
    if test_mode:
        worker_path = Path(args.test_worker_root) / "home/operator/.local/state/deepseek-v4-flash-dspark/artifacts/fabric-consumer.receipt.json"
        worker_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.output, worker_path)
        readback = file_sha256(worker_path)
    else:
        payload = Path(args.output).read_text(encoding="utf-8")
        remote_python = ("import os,pathlib,sys,tempfile; p=pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True,exist_ok=True); "
                         "fd,t=tempfile.mkstemp(prefix=p.name+'.',dir=p.parent); f=os.fdopen(fd,'w'); f.write(sys.stdin.read()); "
                         "f.flush(); os.fsync(f.fileno()); f.close(); os.replace(t,p)")
        remote_text(["/usr/bin/python3", "-c", remote_python,
                     "/home/operator/.local/state/deepseek-v4-flash-dspark/artifacts/fabric-consumer.receipt.json"],
                    input_text=payload)
        readback = remote_text(["/usr/bin/sha256sum",
                                "/home/operator/.local/state/deepseek-v4-flash-dspark/artifacts/fabric-consumer.receipt.json"]).split()[0]
    if readback != content_sha:
        raise PreparationError("head/worker fabric consumer receipt SHA-256 differs")
    print(f"FABRIC_CONSUMER_QUALIFIED receipt_sha256={content_sha}")


def validate_image_journal_offline(lock, journal):
    runtime, gateway, acquisition = validate_image_lock(lock)
    if journal.get("schema_version") != 1 or not isinstance(journal.get("images"), dict):
        raise PreparationError("image journal is invalid")
    entries = journal["images"]
    for image in (runtime, gateway):
        if not compressed_entry_valid_offline(image, entries.get(image["manifest_digest"])):
            raise PreparationError(f"image journal does not close exact manifest/config/layers: {image['reference']}")
    state = journal.get("acquisition")
    if not acquisition_state_valid(lock, state):
        raise PreparationError("image journal lacks exact VPN/pull/role/transaction acquisition provenance")
    return entries


def validate_archive_receipt_offline(lock, receipt):
    runtime, _, _ = validate_image_lock(lock)
    if (receipt.get("schema_version") != 1 or
            receipt.get("kind") != "deepseek-v4-flash-runtime-archive-receipt" or
            not compressed_entry_valid_offline(runtime, receipt) or
            not isinstance(receipt.get("archive_path"), str) or not receipt["archive_path"] or
            not isinstance(receipt.get("archive_size"), int) or receipt["archive_size"] <= 0 or
            not is_sha256(receipt.get("archive_sha256"))):
        raise PreparationError("runtime archive receipt does not close exact image identity and archive SHA-256")


def validate_node_identity(lock, receipt, role, image_entries, archive_receipt,
                           image_journal_sha256, archive_receipt_sha256):
    runtime, gateway, _ = validate_image_lock(lock)
    expected_names = {"runtime", "gateway"} if role == "head" else {"runtime"}
    images = receipt.get("images")
    if (receipt.get("schema_version") != 1 or
            receipt.get("kind") != "deepseek-v4-flash-node-identity-receipt" or
            receipt.get("role") != role or receipt.get("hostname") != ("dgx-head" if role == "head" else "dgx-worker") or
            receipt.get("kernel") != "6.17.0-1031-nvidia" or receipt.get("driver") != "580.173.02" or
            not isinstance(receipt.get("boot_id"), str) or not receipt["boot_id"] or
            not isinstance(receipt.get("transaction_id"), str) or not receipt["transaction_id"] or
            not isinstance(receipt.get("lease_generation"), str) or len(receipt["lease_generation"]) != 32 or
            receipt.get("model_root_sha256") != lock["model"]["root_sha256"] or
            receipt.get("image_journal_sha256") != image_journal_sha256 or
            receipt.get("runtime_archive_receipt_sha256") != archive_receipt_sha256 or
            not isinstance(images, dict) or set(images) != expected_names):
        raise PreparationError(f"{role} identity receipt does not bind the model and exact image set")
    for name, image in (("runtime", runtime), ("gateway", gateway)):
        if name not in expected_names:
            continue
        identity = images[name]
        expected_rootfs = (archive_receipt if name == "runtime" else image_entries[image["manifest_digest"]])["rootfs_diff_ids"]
        if (identity.get("config_digest") != image["config_digest"] or
                identity.get("os") != "linux" or identity.get("architecture") != "arm64" or
                identity.get("rootfs_diff_ids") != expected_rootfs):
            raise PreparationError(f"{role} {name} config/platform/RootFS identity mismatch")
        if (role == "head" and not
                repo_digest_aliases(image["reference"]).intersection(identity.get("repo_digests", []))):
            raise PreparationError(f"head {name} receipt lacks the pinned RepoDigest")


def evidence_bundle(path, receipt):
    return {"path": str(path), "sha256": file_sha256(path), "receipt": receipt}


def cmd_finalize_deployment(args):
    lock = read_json(args.model_lock)
    if lock.get("finalized") is True or lock.get("model", {}).get("closure_finalized") is not True:
        raise PreparationError("deployment finalization requires a model-only finalized lock")
    bootstrap_manifest = read_json(args.bootstrap_lock)
    validate_bootstrap_manifest(bootstrap_manifest)
    source_receipt = read_json(args.source_receipt)
    license_receipt = read_json(args.license_receipt)
    integration_receipt = read_json(args.integration_receipt)
    image_journal = read_json(args.image_journal)
    archive_receipt = read_json(args.archive_receipt)
    head_receipt = read_json(args.head_identity_receipt)
    worker_receipt = read_json(args.worker_identity_receipt)
    finalized_source = validate_source_receipt(lock, source_receipt, bootstrap_manifest)
    validate_license_receipt(lock, license_receipt)
    finalized_integration, finalized_service_assets = validate_integration_receipt(lock, integration_receipt)
    image_entries = validate_image_journal_offline(lock, image_journal)
    validate_archive_receipt_offline(lock, archive_receipt)
    verify_archive(lock, args.runtime_archive, args.archive_receipt, require_recorded_path=True)
    validate_node_identity(lock, head_receipt, "head", image_entries, archive_receipt,
                           file_sha256(args.image_journal), file_sha256(args.archive_receipt))
    validate_node_identity(lock, worker_receipt, "worker", image_entries, archive_receipt,
                           file_sha256(args.image_journal), file_sha256(args.archive_receipt))

    output = json.loads(json.dumps(lock))
    output["source"] = finalized_source
    output["integration_artifacts"] = finalized_integration
    output["service_assets"] = finalized_service_assets
    output["license_gate"]["operator_acceptance_receipt"] = {
        "operator": license_receipt["operator"],
        "accepted_at": license_receipt["accepted_at"],
        "sha256": file_sha256(args.license_receipt),
    }
    output["runtime_image"]["archive"] = {
        "path": archive_receipt["archive_path"],
        "size": archive_receipt["archive_size"],
        "sha256": archive_receipt["archive_sha256"],
    }
    output["runtime_image"]["rootfs_diff_ids"] = archive_receipt["rootfs_diff_ids"]
    output["runtime_image"]["image_id"] = archive_receipt["config_digest"]
    output["runtime_image"]["node_image_ids"] = {
        "head": head_receipt["images"]["runtime"]["config_digest"],
        "worker": worker_receipt["images"]["runtime"]["config_digest"],
    }
    output["gateway_image"]["image_id"] = head_receipt["images"]["gateway"]["config_digest"]
    output["gateway_image"]["rootfs_diff_ids"] = head_receipt["images"]["gateway"]["rootfs_diff_ids"]
    output["finalization"] = {
        "bootstrap_inputs": evidence_bundle(args.bootstrap_lock, bootstrap_manifest),
        "source": evidence_bundle(args.source_receipt, source_receipt),
        "license": evidence_bundle(args.license_receipt, license_receipt),
        "integration": evidence_bundle(args.integration_receipt, integration_receipt),
        "image_journal": evidence_bundle(args.image_journal, image_journal),
        "runtime_archive": evidence_bundle(args.archive_receipt, archive_receipt),
        "head_identity": evidence_bundle(args.head_identity_receipt, head_receipt),
        "worker_identity": evidence_bundle(args.worker_identity_receipt, worker_receipt),
    }
    output["finalized"] = True
    atomic_json(args.output, output)
    print(f"DEPLOYMENT_LOCK_FINALIZED model_root_sha256={output['model']['root_sha256']}")


def cmd_verify_deployment(args):
    lock = read_json(args.lock)
    finalization = lock.get("finalization")
    if lock.get("finalized") is not True or lock.get("model", {}).get("closure_finalized") is not True or not isinstance(finalization, dict):
        raise PreparationError("deployment lock is not fully finalized")
    required = {"bootstrap_inputs", "source", "license", "integration", "image_journal", "runtime_archive", "head_identity", "worker_identity"}
    if set(finalization) != required or not all(
            isinstance(finalization[name], dict) and is_sha256(finalization[name].get("sha256")) and
            isinstance(finalization[name].get("receipt"), dict) for name in required):
        raise PreparationError("deployment finalization evidence set is incomplete")
    bootstrap_manifest = finalization["bootstrap_inputs"]["receipt"]
    validate_bootstrap_manifest(bootstrap_manifest)
    finalized_source = validate_source_receipt(lock, finalization["source"]["receipt"], bootstrap_manifest)
    if lock["source"] != finalized_source:
        raise PreparationError("deployment source fields differ from embedded source receipt")
    license_receipt = finalization["license"]["receipt"]
    validate_license_receipt(lock, license_receipt)
    if lock["license_gate"].get("operator_acceptance_receipt") != {
        "operator": license_receipt["operator"],
        "accepted_at": license_receipt["accepted_at"],
        "sha256": finalization["license"]["sha256"],
    }:
        raise PreparationError("deployment license fields differ from embedded acceptance receipt")
    integration, service_assets = validate_integration_receipt(lock, finalization["integration"]["receipt"])
    if (lock.get("integration_artifacts") != integration or
            lock.get("service_assets") != service_assets):
        raise PreparationError("deployment integration artifact fields differ from embedded receipt")
    image_entries = validate_image_journal_offline(lock, finalization["image_journal"]["receipt"])
    archive_receipt = finalization["runtime_archive"]["receipt"]
    validate_archive_receipt_offline(lock, archive_receipt)
    head_receipt = finalization["head_identity"]["receipt"]
    worker_receipt = finalization["worker_identity"]["receipt"]
    journal_sha = finalization["image_journal"]["sha256"]
    archive_sha = finalization["runtime_archive"]["sha256"]
    validate_node_identity(lock, head_receipt, "head", image_entries, archive_receipt,
                           journal_sha, archive_sha)
    validate_node_identity(lock, worker_receipt, "worker", image_entries, archive_receipt,
                           journal_sha, archive_sha)
    if (lock["runtime_image"].get("archive") != {
            "path": archive_receipt["archive_path"], "size": archive_receipt["archive_size"],
            "sha256": archive_receipt["archive_sha256"]} or
            lock["runtime_image"].get("image_id") != archive_receipt["config_digest"] or
            lock["runtime_image"].get("rootfs_diff_ids") != archive_receipt["rootfs_diff_ids"] or
            lock["runtime_image"].get("node_image_ids") != {
                "head": head_receipt["images"]["runtime"]["config_digest"],
                "worker": worker_receipt["images"]["runtime"]["config_digest"]} or
            lock["gateway_image"].get("image_id") != head_receipt["images"]["gateway"]["config_digest"] or
            lock["gateway_image"].get("rootfs_diff_ids") != head_receipt["images"]["gateway"]["rootfs_diff_ids"]):
        raise PreparationError("deployment image fields differ from embedded archive/node receipts")
    print("DEPLOYMENT_LOCK_VERIFIED")


def cmd_verify_fabric(args):
    lock = read_json(args.lock)
    evidence = Path(args.evidence_root)
    expected_id = "nccl-evidence-post-upgrade-20260830T112621Z"
    if lock.get("qualification_id") != expected_id or evidence.name != expected_id:
        raise PreparationError("fabric qualification directory is not the exact pinned generation")
    checks = [("fabric-qualification-release-receipt-v1.json", lock["producer_receipt"]["sha256"])]
    for key, directory in (("single_a", "single-a"), ("single_b", "single-b"), ("dual", "dual")):
        checks.append((f"{directory}/SHA256SUMS", lock["candidate_sha256sums"][key]))
        checks.append((f"{directory}-score.json", lock["score_sha256s"][key]))
    checks.append(("post-qualification-audit/SHA256SUMS", lock["audit_seal_sha256"]))
    for relative, expected in checks:
        if file_sha256(evidence / relative) != expected:
            raise PreparationError(f"fabric evidence digest mismatch: {relative}")
    if read_json(args.provenance_addendum) != lock.get("provenance"):
        raise PreparationError("fabric provenance differs from the full pinned H3 addendum")
    limitations = lock["provenance"]["historical_limitations"]
    if limitations.get("lock_level_released_at_retained") is not False or limitations.get("immutable_lock_release_operation_id_retained") is not False:
        raise PreparationError("fabric provenance improperly reconstructs missing H3 lock fields")
    print("FABRIC_QUALIFICATION_LOCK_VERIFIED")


def source_tree_closure(root):
    base = Path(root)
    rows = []
    for path in sorted(base.rglob("*")):
        relative = path.relative_to(base).as_posix()
        if relative == ".deepseek-source-identity.json" or relative.startswith(".git/"):
            continue
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise PreparationError(f"source closure contains unsupported path: {relative}")
        if path.is_file():
            mode = f"{path.stat().st_mode & 0o777:04o}"
            if mode not in {"0644", "0755"}:
                raise PreparationError(f"source file mode is not 0644/0755: {relative}")
            rows.append({"path": relative, "mode": mode, "size": path.stat().st_size,
                         "sha256": file_sha256(path)})
    material = "".join(f"{row['sha256']}  {row['mode']}  {row['size']}  {row['path']}\n" for row in rows).encode()
    return rows, hashlib.sha256(material).hexdigest(), sum(row["size"] for row in rows)


def validate_source_identity(lock, source_root, require_upstream):
    identity_path = Path(source_root) / ".deepseek-source-identity.json"
    if identity_path.is_symlink() or not identity_path.is_file():
        raise PreparationError("source identity is absent or is not a regular non-symlink file")
    identity = read_json(identity_path)
    rows, root_hash, total = source_tree_closure(source_root)
    if (identity.get("schema_version") != 1 or identity.get("repository") != lock["source"]["repository"] or
            identity.get("commit") != lock["source"]["commit"] or identity.get("git_tree") != "a5258a8070b62fe77ed4bdb8887b39ef388e3a24" or
            identity.get("files") != rows or identity.get("root_sha256") != root_hash or
            identity.get("file_count") != len(rows) or identity.get("total_bytes") != total):
        raise PreparationError("source identity/tree/file closure mismatch")
    if require_upstream and identity.get("variant") != "upstream":
        raise PreparationError("source identity is not the upstream variant")
    if (require_upstream and not marked_local_test_seam("DEEPSEEK_ARTIFACT_TEST_SEAM") and
            (len(rows) != 171 or total != 4293967)):
        raise PreparationError("upstream source count/bytes/variant differs from the pinned checkout")
    return identity


def bootstrap_upstream_identity(manifest):
    source = manifest.get("upstream_source", {})
    return {key: source.get(key) for key in (
        "schema_version", "repository", "commit", "git_tree", "variant", "files",
        "file_count", "total_bytes", "root_sha256")}


def validate_bootstrap_manifest(manifest):
    files = manifest.get("files")
    if (manifest.get("schema_version") != 1 or
            manifest.get("kind") != "deepseek-v4-flash-bootstrap-input-lock" or
            manifest.get("finalized") is not True or manifest.get("input_root") != INPUT_ROOT or
            not isinstance(files, list) or [row.get("path") for row in files] != list(HEAD_BOOTSTRAP_FILES)):
        raise PreparationError("bootstrap input lock is not the exact finalized role-scoped manifest")
    file_material = ""
    for row in files:
        expected_roles = ["head", "worker"] if row["path"] in WORKER_BOOTSTRAP_FILES else ["head"]
        if (set(row) != {"path", "mode", "size", "sha256", "roles"} or row.get("roles") != expected_roles or
                row.get("mode") not in {"0644", "0755"} or
                not isinstance(row.get("size"), int) or row["size"] < 0 or not is_sha256(row.get("sha256"))):
            raise PreparationError(f"bootstrap input row is malformed or role-widened: {row.get('path')}")
        file_material += (f"{row['sha256']}  {row['mode']}  {row['size']}  "
                          f"{','.join(row['roles'])}  {row['path']}\n")
    if (manifest.get("file_count") != len(files) or
            manifest.get("total_bytes") != sum(row["size"] for row in files) or
            manifest.get("root_sha256") != hashlib.sha256(file_material.encode()).hexdigest()):
        raise PreparationError("bootstrap input file count/bytes/root closure mismatch")
    upstream = manifest.get("upstream_source", {})
    identity = bootstrap_upstream_identity(manifest)
    if (upstream.get("destination") != BOOTSTRAP_SOURCE_DESTINATION or
            upstream.get("identity_path") != ".deepseek-source-identity.json" or
            identity.get("schema_version") != 1 or identity.get("repository") !=
            "https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark.git" or
            identity.get("commit") != "0107cef1835a56d1a2bcdabf7d9e1a085b70338b" or
            identity.get("git_tree") != "a5258a8070b62fe77ed4bdb8887b39ef388e3a24" or
            identity.get("variant") != "upstream" or identity.get("file_count") != 171 or
            identity.get("total_bytes") != 4293967 or identity.get("root_sha256") != BOOTSTRAP_SOURCE_ROOT or
            not isinstance(identity.get("files"), list) or len(identity["files"]) != 171):
        raise PreparationError("bootstrap upstream source identity is incomplete or differs from the frozen checkout")
    material = ""
    for row in identity["files"]:
        if (set(row) != {"path", "mode", "size", "sha256"} or
                safe_relative_path(row.get("path")) != row["path"] or row.get("mode") not in {"0644", "0755"} or
                not isinstance(row.get("size"), int) or row["size"] < 0 or not is_sha256(row.get("sha256"))):
            raise PreparationError("bootstrap upstream source row is malformed")
        material += f"{row['sha256']}  {row['mode']}  {row['size']}  {row['path']}\n"
    if hashlib.sha256(material.encode()).hexdigest() != BOOTSTRAP_SOURCE_ROOT:
        raise PreparationError("bootstrap upstream source root closure mismatch")
    return identity


def cmd_verify_bootstrap_inputs(args):
    manifest = read_json(args.lock)
    expected_identity = validate_bootstrap_manifest(manifest)
    test_mode = args.test_input_root is not None or args.test_source_root is not None
    if test_mode and (args.test_input_root is None or (args.role == "head" and args.test_source_root is None) or
                      not marked_local_test_seam("DEEPSEEK_ARTIFACT_TEST_SEAM")):
        raise PreparationError("bootstrap verification test roots require the marked local test seam")
    input_root = Path(args.test_input_root) if test_mode else Path(INPUT_ROOT)
    source_root = Path(args.test_source_root) if args.test_source_root else Path(BOOTSTRAP_SOURCE_DESTINATION)
    expected_paths = {row["path"] for row in manifest["files"] if args.role in row["roles"]}
    expected_paths.add(BOOTSTRAP_LOCK_RELATIVE)
    actual_paths = set()
    for path in input_root.rglob("*"):
        relative = path.relative_to(input_root).as_posix()
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise PreparationError(f"bootstrap input tree contains an unsafe path: {relative}")
        if path.is_file():
            actual_paths.add(relative)
    if actual_paths != expected_paths:
        raise PreparationError("bootstrap input role tree contains missing or extra files")
    rows = {row["path"]: row for row in manifest["files"]}
    for relative in expected_paths - {BOOTSTRAP_LOCK_RELATIVE}:
        path = input_root / relative
        row = rows[relative]
        state = path.stat()
        if (path.is_symlink() or not path.is_file() or path.stat().st_size != row["size"] or
                file_sha256(path) != row["sha256"] or f"{state.st_mode & 0o777:04o}" != row["mode"] or
                (not test_mode and (state.st_uid != 0 or state.st_gid != 0))):
            raise PreparationError(f"bootstrap input path/mode/size/SHA/owner mismatch: {relative}")
    if Path(args.lock).resolve() != (input_root / BOOTSTRAP_LOCK_RELATIVE).resolve():
        raise PreparationError("bootstrap verifier did not consume the fixed control lock path")
    if args.role == "head":
        actual_identity = validate_source_identity(
            {"source": {"repository": expected_identity["repository"], "commit": expected_identity["commit"]}},
            source_root, require_upstream=True)
        if actual_identity != expected_identity:
            raise PreparationError("head upstream source differs from the bootstrap input lock")
    elif args.test_source_root is not None:
        raise PreparationError("worker bootstrap verification must not consume an upstream source tree")
    print(f"BOOTSTRAP_INPUTS_VERIFIED role={args.role} files={len(expected_paths)}")


def cmd_verify_source(args):
    lock = read_json(args.lock)
    identity = validate_source_identity(lock, args.source_root, require_upstream=not args.patched)
    if args.patched and identity.get("variant") != "deepseek-local-patched":
        raise PreparationError("source identity is not the patched variant")
    print(f"RUNTIME_SOURCE_VERIFIED commit={lock['source']['commit']}")


def cmd_apply_patch(args):
    lock = read_json(args.lock)
    pre_identity = validate_source_identity(lock, args.source_root, require_upstream=True)
    patch_path = Path(args.patch)
    if patch_path.is_symlink() or not patch_path.is_file():
        raise PreparationError("local runtime patch is absent or unsafe")
    patch_sha256 = file_sha256(patch_path)
    if patch_sha256 != lock["source"]["local_patch"].get("sha256"):
        raise PreparationError("local runtime patch SHA-256 differs from the immutable lock")
    preimages = {row["path"]: row["sha256"] for row in pre_identity["files"]}
    targets = []
    for template in lock["source"]["patch_targets"]:
        relative = safe_relative_path(template["path"])
        if relative not in preimages:
            raise PreparationError(f"runtime patch target absent from source identity: {relative}")
        if preimages[relative] != template.get("preimage_sha256"):
            raise PreparationError(f"runtime patch target preimage differs from the immutable lock: {relative}")
        targets.append({"path": relative, "preimage_sha256": preimages[relative]})
    check = subprocess.run(["/usr/bin/patch", "--batch", "--forward", "--dry-run", "-p1", "-d", str(args.source_root),
                            "-i", str(patch_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check.returncode:
        raise PreparationError("local runtime patch does not apply cleanly to pinned source")
    snapshots = {target["path"]: ((Path(args.source_root) / target["path"]).read_bytes(),
                                  (Path(args.source_root) / target["path"]).stat().st_mode & 0o777)
                 for target in targets}
    try:
        applied = subprocess.run(["/usr/bin/patch", "--batch", "--forward", "-p1", "-d", str(args.source_root),
                                  "-i", str(patch_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if applied.returncode:
            raise PreparationError("local runtime patch failed")
        rows, root_hash, total = source_tree_closure(args.source_root)
        post = {row["path"]: row["sha256"] for row in rows}
        for target in targets:
            target["postimage_sha256"] = post[target["path"]]
            expected_post = next(row["postimage_sha256"] for row in lock["source"]["patch_targets"]
                                 if row["path"] == target["path"])
            if target["postimage_sha256"] != expected_post:
                raise PreparationError(f"runtime patch postimage differs from the immutable lock: {target['path']}")
        post_identity = {"schema_version": 1, "repository": lock["source"]["repository"],
                         "commit": lock["source"]["commit"], "git_tree": pre_identity["git_tree"],
                         "variant": "deepseek-local-patched", "files": rows, "file_count": len(rows),
                         "total_bytes": total, "root_sha256": root_hash}
        atomic_bytes(
            Path(args.source_root) / ".deepseek-source-identity.json",
            (json.dumps(post_identity, indent=2, sort_keys=True) + "\n").encode(),
            0o644,
        )
        atomic_json(args.source_receipt, {
            "schema_version": 1, "commit": lock["source"]["commit"],
            "local_patch": {"path": lock["source"]["local_patch"]["path"], "sha256": patch_sha256},
            "patch_targets": targets, "upstream_identity": pre_identity,
            "head_identity": post_identity, "worker_replica": None,
        })
    except BaseException as original:
        restoration_errors = []
        for relative, (content, mode) in snapshots.items():
            try:
                atomic_bytes(Path(args.source_root) / relative, content, mode)
            except BaseException as exc:
                restoration_errors.append(f"{relative}: {exc}")
        try:
            atomic_bytes(
                Path(args.source_root) / ".deepseek-source-identity.json",
                (json.dumps(pre_identity, indent=2, sort_keys=True) + "\n").encode(),
                0o644,
            )
        except BaseException as exc:
            restoration_errors.append(f"identity: {exc}")
        if restoration_errors:
            raise PreparationError("runtime patch failed and rollback was incomplete: " +
                                   "; ".join(restoration_errors)) from original
        raise
    print("RUNTIME_PATCH_APPLIED_WITH_HASH_GUARDS")


def cmd_verify_patched_source(args):
    lock = read_json(args.lock)
    identity = validate_source_identity(lock, args.source_root, require_upstream=False)
    if identity.get("variant") != "deepseek-local-patched":
        raise PreparationError("source is not the pinned local patched variant")
    receipt = read_json(args.source_receipt)
    if receipt.get("head_identity") != identity:
        raise PreparationError("patched source differs from its head receipt")
    print(f"PATCHED_SOURCE_VERIFIED root_sha256={identity['root_sha256']}")


def cmd_copy_patched_source(args):
    lock = read_json(args.lock)
    receipt = read_json(args.source_receipt)
    identity = validate_source_identity(lock, args.source_root, require_upstream=False)
    if receipt.get("head_identity") != identity or receipt.get("worker_replica") is not None:
        raise PreparationError("source copy requires an unreplicated exact head patched receipt")
    transaction = args.mutation_receipt["transaction_id"]
    if not re.fullmatch(r"[0-9a-f]{32}", transaction) and not marked_local_test_seam("DEEPSEEK_ARTIFACT_TEST_SEAM"):
        raise PreparationError("source copy transaction ID is invalid")
    production_staging = f"/home/operator/dgx-models/deepseek-v4-flash-dspark/source.staging-{transaction}"
    production_destination = "/home/operator/dgx-models/deepseek-v4-flash-dspark/source"
    local_worker_root = Path(args.test_worker_root) if args.test_worker_root else None
    if local_worker_root is not None:
        if not args.test_rsync_bin or not marked_local_test_seam("DEEPSEEK_ARTIFACT_TEST_SEAM"):
            raise PreparationError("worker source promotion test root requires the marked rsync test seam")
        staging = str(local_worker_root / production_staging.lstrip("/"))
        destination = str(local_worker_root / production_destination.lstrip("/"))
    else:
        staging = production_staging
        destination = production_destination
    boundary = str(local_worker_root) if local_worker_root is not None else "/home"
    worker_runner = run_text if local_worker_root is not None else remote_text
    if local_worker_root is not None:
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
    def worker_source_action(operation, hook="none", inode_token="-"):
        return worker_runner(
            ["/usr/bin/python3", "-c", WORKER_SOURCE_PROMOTION_SCRIPT,
             operation, staging if operation != "inspect" else "-", destination,
             hook, inode_token, boundary],
            input_text=json.dumps(identity), timeout=120,
        ).strip()
    rsync = test_seam_command(args.test_rsync_bin, RSYNC_COMMAND)
    preimage = "missing"
    replica = None
    if not args.test_rsync_bin or local_worker_root is not None:
        existing = worker_source_action("inspect")
        if existing == "IDENTICAL":
            replica = identity
            preimage = "identical"
        elif existing != "MISSING":
            raise PreparationError("worker source inspect returned an invalid state")
        staging_path = Path(staging) if local_worker_root is not None else None
        staging_state = (
            "present" if staging_path is not None and (staging_path.exists() or staging_path.is_symlink()) else
            "missing" if staging_path is not None else
            remote_text(["/usr/bin/python3", "-c",
                "import pathlib,sys; p=pathlib.Path(sys.argv[1]); print('present' if p.exists() or p.is_symlink() else 'missing')",
                staging]).strip()
        )
        if staging_state != "missing":
            raise PreparationError("transaction-specific worker source staging path already exists")
    if replica is not None:
        updated = json.loads(json.dumps(receipt))
        updated["worker_replica"] = replica
        updated["replication"] = {
            "transaction_id": transaction,
            "lease_generation": args.mutation_receipt["lease_generation"],
            "destination": production_destination,
            "preimage": preimage,
            "rollback": "not-required-identical-preimage",
        }
        atomic_json(args.source_receipt, updated)
        print(f"PATCHED_SOURCE_COPIED root_sha256={identity['root_sha256']}")
        return
    rsync_destination = (str(Path(staging)) + "/" if local_worker_root is not None else
                         f"operator@192.0.2.34:{production_staging}/")
    command = [rsync, "-a", "--delete", "--exclude=.git/", "-e", WORKER_SSH,
               str(Path(args.source_root)) + "/", rsync_destination]
    promotion_enabled = not args.test_rsync_bin or local_worker_root is not None
    staging_inode = worker_source_action("reserve") if promotion_enabled else None
    try:
        result = subprocess.run(command)
        if result.returncode:
            raise PreparationError(f"patched source rsync failed with exit {result.returncode}")
        if local_worker_root is not None:
            staging_path = Path(staging)
            shutil.copytree(args.source_root, staging_path, copy_function=shutil.copy2,
                            ignore=shutil.ignore_patterns(".git"), dirs_exist_ok=True)
            worker_source_action("promote", args.test_promote_hook or "none", staging_inode)
            replica = identity
        elif args.test_rsync_bin:
            replica = identity
        else:
            remote_text([INSTALL_PATH, "verify-runtime-source", "--lock",
                         "/opt/deepseek-v4-flash-dspark/manifests/deepseek-v4-flash-dspark.lock.json",
                         "--source-root", staging, "--patched"])
            worker_source_action("promote", "none", staging_inode)
            replica = identity
        if replica != identity:
            raise PreparationError("worker patched source identity differs after atomic promotion")
    except BaseException as original:
        # The shared promotion primitive performs inode-limited cleanup for
        # both staging and any target it actually promoted.  Never issue a
        # second path-only deletion here: a competitor may now own either
        # pathname.
        if staging_inode is not None:
            try:
                worker_source_action("cleanup", "none", staging_inode)
            except BaseException as cleanup_error:
                raise PreparationError(
                    f"patched source copy failed and inode-limited staging cleanup failed: {cleanup_error}"
                ) from original
        raise
    updated = json.loads(json.dumps(receipt))
    updated["worker_replica"] = replica
    updated["replication"] = {
        "transaction_id": transaction,
        "lease_generation": args.mutation_receipt["lease_generation"],
        "destination": production_destination,
        "preimage": preimage,
        "rollback": "remove-new-destination-only",
    }
    atomic_json(args.source_receipt, updated)
    print(f"PATCHED_SOURCE_COPIED root_sha256={identity['root_sha256']}")


def parser():
    root = argparse.ArgumentParser(description="Fail-closed DeepSeek V4 Flash artifact preparation")
    commands = root.add_subparsers(dest="command", required=True)
    emit = commands.add_parser("emit-files-from")
    emit.add_argument("--download-manifest", required=True)
    emit.set_defaults(func=cmd_emit_files)
    finalize = commands.add_parser("finalize-model-lock")
    finalize.add_argument("--model-root", required=True)
    finalize.add_argument("--download-manifest", required=True)
    finalize.add_argument("--lock-template", required=True)
    finalize.add_argument("--official-anchor-inventory", required=True)
    finalize.add_argument("--official-metadata", required=True)
    finalize.add_argument("--output", required=True)
    finalize.add_argument("--strict-tree", action="store_true")
    finalize.add_argument("--role", required=True, choices=("head",))
    finalize.set_defaults(func=cmd_finalize)
    verify = commands.add_parser("verify-model-lock")
    verify.add_argument("--model-root", required=True)
    verify.add_argument("--lock", required=True)
    verify.add_argument("--strict-tree", action="store_true")
    verify.set_defaults(func=cmd_verify_model)
    copy = commands.add_parser("copy-model")
    copy.add_argument("--download-manifest", required=True)
    copy.add_argument("--lock", required=True)
    copy.add_argument("--source-root", required=True)
    copy.add_argument("--test-rsync-bin")
    copy.add_argument("--role", required=True, choices=("head",))
    copy.set_defaults(func=cmd_copy_model)
    acquire = commands.add_parser("acquire-images")
    acquire.add_argument("--role", required=True, choices=("head", "worker"))
    acquire.add_argument("--lock", required=True)
    acquire.add_argument("--journal", required=True)
    acquire.add_argument("--rx-counter", required=True)
    acquire.add_argument("--test-docker-command-json")
    acquire.add_argument("--poll-seconds", type=float, default=0.25)
    acquire.set_defaults(func=cmd_acquire_images)
    save = commands.add_parser("save-runtime-archive")
    save.add_argument("--role", required=True, choices=("head", "worker"))
    save.add_argument("--lock", required=True)
    save.add_argument("--journal", required=True)
    save.add_argument("--archive", required=True)
    save.add_argument("--receipt", required=True)
    save.add_argument("--test-docker-command-json")
    save.set_defaults(func=cmd_save_runtime)
    verify_archive_parser = commands.add_parser("verify-runtime-archive")
    verify_archive_parser.add_argument("--lock", required=True)
    verify_archive_parser.add_argument("--archive", required=True)
    verify_archive_parser.add_argument("--receipt", required=True)
    verify_archive_parser.set_defaults(func=cmd_verify_archive)
    copy_archive = commands.add_parser("copy-runtime-archive")
    copy_archive.add_argument("--lock", required=True)
    copy_archive.add_argument("--archive", required=True)
    copy_archive.add_argument("--receipt", required=True)
    copy_archive.add_argument("--journal", required=True)
    copy_archive.add_argument("--test-rsync-bin")
    copy_archive.add_argument("--role", required=True, choices=("head",))
    copy_archive.set_defaults(func=cmd_copy_archive)
    load = commands.add_parser("load-runtime-archive")
    load.add_argument("--role", required=True, choices=("head", "worker"))
    load.add_argument("--lock", required=True)
    load.add_argument("--archive", required=True)
    load.add_argument("--receipt", required=True)
    load.add_argument("--test-docker-command-json")
    load.set_defaults(func=cmd_load_runtime)
    finalize_deployment = commands.add_parser("finalize-deployment-lock")
    finalize_deployment.add_argument("--bootstrap-lock", required=True)
    finalize_deployment.add_argument("--model-lock", required=True)
    finalize_deployment.add_argument("--source-receipt", required=True)
    finalize_deployment.add_argument("--license-receipt", required=True)
    finalize_deployment.add_argument("--integration-receipt", required=True)
    finalize_deployment.add_argument("--image-journal", required=True)
    finalize_deployment.add_argument("--runtime-archive", required=True)
    finalize_deployment.add_argument("--archive-receipt", required=True)
    finalize_deployment.add_argument("--head-identity-receipt", required=True)
    finalize_deployment.add_argument("--worker-identity-receipt", required=True)
    finalize_deployment.add_argument("--output", required=True)
    finalize_deployment.add_argument("--role", required=True, choices=("head",))
    finalize_deployment.set_defaults(func=cmd_finalize_deployment)
    verify_deployment = commands.add_parser("verify-deployment-lock")
    verify_deployment.add_argument("--lock", required=True)
    verify_deployment.set_defaults(func=cmd_verify_deployment)
    fabric = commands.add_parser("verify-fabric-lock")
    fabric.add_argument("--lock", required=True)
    fabric.add_argument("--evidence-root", required=True)
    fabric.add_argument("--provenance-addendum", required=True)
    fabric.set_defaults(func=cmd_verify_fabric)
    bootstrap = commands.add_parser("verify-bootstrap-input-lock")
    bootstrap.add_argument("--role", required=True, choices=("head", "worker"))
    bootstrap.add_argument("--lock", required=True)
    bootstrap.add_argument("--test-input-root")
    bootstrap.add_argument("--test-source-root")
    bootstrap.set_defaults(func=cmd_verify_bootstrap_inputs)
    source = commands.add_parser("verify-runtime-source")
    source.add_argument("--lock", required=True)
    source.add_argument("--source-root", required=True)
    source.add_argument("--patched", action="store_true")
    source.set_defaults(func=cmd_verify_source)
    patch = commands.add_parser("apply-runtime-patch")
    patch.add_argument("--lock", required=True)
    patch.add_argument("--source-root", required=True)
    patch.add_argument("--patch", required=True)
    patch.add_argument("--source-receipt", required=True)
    patch.add_argument("--role", required=True, choices=("head",))
    patch.set_defaults(func=cmd_apply_patch)
    copy_source = commands.add_parser("copy-patched-source")
    copy_source.add_argument("--role", required=True, choices=("head",))
    copy_source.add_argument("--lock", required=True)
    copy_source.add_argument("--source-root", required=True)
    copy_source.add_argument("--source-receipt", required=True)
    copy_source.add_argument("--test-rsync-bin")
    copy_source.add_argument("--test-worker-root")
    copy_source.add_argument("--test-promote-hook", choices=(
        "race-target", "staging-drift", "post-readback-drift"))
    copy_source.set_defaults(func=cmd_copy_patched_source)
    install_integration = commands.add_parser("install-integration-artifacts")
    install_integration.add_argument("--integration-receipt", required=True)
    install_integration.add_argument("--role", required=True, choices=("head",))
    install_integration.add_argument("--test-input-root")
    install_integration.add_argument("--test-install-root")
    install_integration.set_defaults(func=cmd_install_integration)
    install_service = commands.add_parser("install-service-assets")
    install_service.add_argument("--role", required=True, choices=("head", "worker"))
    install_service.add_argument("--final-lock", required=True)
    install_service.add_argument("--source-root", required=True)
    install_service.add_argument("--asset-receipt", required=True)
    install_service.add_argument("--secret-fd")
    install_service.add_argument("--test-input-root")
    install_service.add_argument("--test-install-root")
    install_service.set_defaults(func=cmd_install_service_assets)
    qualify = commands.add_parser("qualify-fabric-consumer")
    qualify.add_argument("--role", required=True, choices=("head",))
    qualify.add_argument("--fabric-lock", required=True)
    qualify.add_argument("--provenance-addendum", required=True)
    qualify.add_argument("--deployment-lock", required=True)
    qualify.add_argument("--output", required=True)
    qualify.add_argument("--test-consumer-fixture")
    qualify.add_argument("--test-worker-root")
    qualify.add_argument("--test-input-root")
    qualify.set_defaults(func=cmd_qualify_fabric_consumer)
    license_parser = commands.add_parser("record-license-acceptance")
    license_parser.add_argument("--role", required=True, choices=("head",))
    license_parser.add_argument("--lock", required=True)
    license_parser.add_argument("--operator", required=True)
    license_parser.add_argument("--approval-basis-id", required=True)
    license_parser.add_argument("--approval-basis", required=True)
    license_parser.add_argument("--authority-commit", required=True)
    license_parser.add_argument("--output", required=True)
    license_parser.set_defaults(func=cmd_record_license)
    node_identity = commands.add_parser("record-node-identity")
    node_identity.add_argument("--role", required=True, choices=("head", "worker"))
    node_identity.add_argument("--lock", required=True)
    node_identity.add_argument("--model-root", required=True)
    node_identity.add_argument("--image-journal", required=True)
    node_identity.add_argument("--archive-receipt", required=True)
    node_identity.add_argument("--output", required=True)
    node_identity.add_argument("--test-docker-command-json")
    node_identity.set_defaults(func=cmd_record_node_identity)
    for mutation in (finalize, copy, acquire, save, copy_archive, load, finalize_deployment, patch,
                     install_integration, install_service, qualify, license_parser, node_identity, copy_source):
        mutation.add_argument("--receipt-file", required=True)
    return root


try:
    arguments = parser().parse_args(CLI_ARGS)
    if arguments.command in MUTATING_COMMANDS:
        require_mutation_receipt(arguments)
    arguments.func(arguments)
except MutationReceiptError as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    raise SystemExit(75)
except PreparationError as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    raise SystemExit(2)
PY
