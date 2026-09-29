#!/usr/bin/env python3
from __future__ import annotations

import base64
import argparse
import hashlib
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from DGXGateway.dgx_gateway.authority import GatewayLease


LOCK_ROOT = Path("/Users/operator/.codex/locks/dgx-spark-two-node")
TOKEN_DIR = Path("/Users/operator/.codex/locks/dgx-cluster-gateway-tokens")
LOCK_CLI = REPO_ROOT / "scripts/dgx-spark-deployment-lock.py"
JOURNAL = Path("/Users/operator/.codex/state/deepseek-v4-flash-dspark/journal.jsonl")
HOSTS = {
    "head": ("192.0.2.30", "dgx-head"),
    "worker": ("192.0.2.31", "dgx-worker"),
}
H3_FILES = {
    "head": {
        "/usr/local/libexec/recover-deepseek-v4-flash": REPO_ROOT
        / "scripts/recover-deepseek-v4-flash.py",
        "/usr/local/sbin/minimax-h3-control": REPO_ROOT
        / "scripts/minimax-h3-two-node/minimax-h3-control",
        "/usr/local/libexec/minimax-h3-runtime-action": REPO_ROOT
        / "scripts/minimax-h3-two-node/minimax-h3-runtime-action",
    },
    "worker": {
        "/usr/local/libexec/minimax-h3-worker-action": REPO_ROOT
        / "scripts/minimax-h3-two-node/minimax-h3-worker-action",
    },
}


REMOTE_INSTALL = r'''
import base64, hashlib, json, os, pathlib, pwd, shutil, socket, sys
expected_host, operation_id = sys.argv[1:]
if socket.gethostname() != expected_host or pwd.getpwuid(os.getuid()).pw_name != "root":
    raise SystemExit(75)
payload = json.load(sys.stdin)
if payload.get("schema_version") != 1 or payload.get("operation_id") != operation_id:
    raise SystemExit(64)
state = pathlib.Path("/var/lib/dgx-cluster-gateway/control-seams") / operation_id
if state.exists():
    raise SystemExit("transaction already exists")
(state / "preimage").mkdir(parents=True, mode=0o700)
records = []
try:
    for row in payload.get("files", []):
        if set(row) != {"destination", "sha256", "content_base64", "mode"}:
            raise RuntimeError("invalid file row")
        destination = pathlib.Path(row["destination"])
        allowed = {
            "/usr/local/sbin/minimax-h3-control",
            "/usr/local/libexec/minimax-h3-runtime-action",
            "/usr/local/libexec/minimax-h3-worker-action",
            "/usr/local/libexec/recover-deepseek-v4-flash",
        }
        if str(destination) not in allowed or row["mode"] != "0755":
            raise RuntimeError("destination refused")
        if destination.is_symlink() or (destination.exists() and not destination.is_file()):
            raise RuntimeError("unsafe destination")
        backup = state / "preimage" / destination.relative_to("/")
        existed = destination.exists()
        if existed:
            backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(destination, backup)
        data = base64.b64decode(row["content_base64"], validate=True)
        if hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise RuntimeError("source digest mismatch")
        temporary = destination.with_name(f".{destination.name}.{operation_id}")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o755)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chown(temporary, 0, 0)
        os.chmod(temporary, 0o755)
        os.replace(temporary, destination)
        records.append({"destination":str(destination),"existed":existed,"backup":str(backup)})
    installed = {}
    for row in payload["files"]:
        path = pathlib.Path(row["destination"])
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != row["sha256"]:
            raise RuntimeError("postimage digest mismatch")
        installed[str(path)] = digest
    receipt = {"schema_version":1,"operation_id":operation_id,"hostname":socket.gethostname(),"installed":installed}
    target = state / "completion.json"
    target.write_text(json.dumps(receipt,sort_keys=True)+"\n")
    os.chmod(target,0o600)
    print(json.dumps(receipt,sort_keys=True))
except BaseException:
    for record in reversed(records):
        destination = pathlib.Path(record["destination"])
        if record["existed"]:
            shutil.copy2(record["backup"], destination)
        else:
            destination.unlink(missing_ok=True)
    raise
'''


def load_controller():
    path = REPO_ROOT / "scripts/control-deepseek-v4-flash-dspark.py"
    spec = importlib.util.spec_from_file_location("gateway_deepseek_installer", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load DeepSeek controller")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def install_control_files(role: str, operation_id: str, lease: GatewayLease) -> dict:
    address, expected = HOSTS[role]
    rows = []
    for destination, source in H3_FILES[role].items():
        data = source.read_bytes()
        rows.append(
            {
                "destination": destination,
                "sha256": hashlib.sha256(data).hexdigest(),
                "content_base64": base64.b64encode(data).decode("ascii"),
                "mode": "0755",
            }
        )
    payload = json.dumps(
        {"schema_version": 1, "operation_id": operation_id, "files": rows},
        sort_keys=True,
        separators=(",", ":"),
    )
    lease.check_supervision()
    result = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            "-o",
            "StrictHostKeyChecking=yes",
            f"operator@{address}",
            shlex.join(
                [
                    "sudo",
                    "-n",
                    "python3",
                    "-c",
                    REMOTE_INSTALL,
                    expected,
                    operation_id,
                ]
            ),
        ],
        input=payload,
        text=True,
        capture_output=True,
        timeout=120,
    )
    if result.returncode:
        raise RuntimeError(f"{role} control seam install failed: {result.stderr.strip()}")
    lease.check_supervision()
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume-operation")
    args = parser.parse_args()
    operation_id = args.resume_operation or f"gateway-seams-{uuid.uuid4().hex}"
    lease = GatewayLease(
        lock_cli=LOCK_CLI,
        lock_root=LOCK_ROOT,
        token_dir=TOKEN_DIR,
        operation_id=operation_id,
        purpose="install borrowed-lock control seams for DGX Gateway",
        lease_seconds=1800,
    )
    if args.resume_operation:
        acquired = lease.guard()
    else:
        acquired = lease.acquire()
    lease.start_heartbeat()
    result = {"operation_id": operation_id, "lease_generation": acquired["lease_generation"]}
    try:
        module = load_controller()
        runner = module.SubprocessRunner()
        authority = module.LockAuthority(
            runner=runner,
            token_file=lease.token_path,
            owner="dgx-cluster-gateway",
            lock_root=LOCK_ROOT,
            lock_script=LOCK_CLI,
            lock_history=LOCK_ROOT.parent / f"{LOCK_ROOT.name}-history",
        )
        controller = module.ClusterController(
            authority=authority,
            runner=runner,
            journal=module.Journal(JOURNAL),
            task_id=operation_id,
        )
        result["deepseek_bootstrap_executor"] = controller.bootstrap_executor(
            _under_active=True
        )
        result["deepseek_bootstrap_inputs"] = controller.bootstrap_input_bundle(
            _under_active=True
        )
        result["worker_control_files"] = install_control_files(
            "worker", operation_id, lease
        )
        result["head_control_files"] = install_control_files(
            "head", operation_id, lease
        )
        released = lease.release()
        result["release"] = {
            "state": released.get("state"),
            "release_operation_id": released.get("release_operation_id"),
            "release_receipt_sha256": released.get("release_receipt_sha256"),
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except BaseException:
        lease.stop_heartbeat()
        raise


if __name__ == "__main__":
    raise SystemExit(main())
