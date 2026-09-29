from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .adapters.catalog import build_adapters
from .admission import MutationAdmission
from .authority import GatewayLease
from .config import load_config
from .domain import classify_local_models
from .operations import OperationController
from .ssh import FixedSSH
from .store import OperationStore


def _atomic_private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _verified_safe_proof(config, operation_id: str, status: dict, request_path: Path) -> Path:
    if request_path.is_symlink() or not request_path.is_file() or request_path.stat().st_mode & 0o077:
        raise RuntimeError("recovery request is missing or unsafe")
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if request != {"schema": "dgx-gateway-recovery-request-v1", "operation_id": operation_id}:
        raise RuntimeError("recovery request does not bind the operation")
    adapters = tuple(build_adapters(config).values())
    with ThreadPoolExecutor(max_workers=len(adapters)) as executor:
        observed = list(executor.map(lambda adapter: adapter.observe(), adapters))
    classification = classify_local_models(observed)
    if classification.state not in {"ACTIVE", "NONE"}:
        raise RuntimeError("live local-model state is not safe for recovery")
    ssh = FixedSSH()
    for node in config.nodes:
        result = ssh.run(
            node,
            ["sh", "-c", "hostname; whoami; test ! -e /run/minimax-h3-two-node/mac-lock-attestation.json"],
            timeout=10,
        )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if result.returncode or lines != [node.hostname, node.user]:
            raise RuntimeError(f"remote attestation absence is unproven on {node.role}")
    evidence = {
        "schema": "dgx-stale-retirement-evidence-v1",
        "cluster_id": "dgx-head+dgx-worker",
        "owner": status["owner"],
        "lease_generation": status["lease_generation"],
        "observed_local_model_state": "ACTIVE_EXCLUSIVE" if classification.state == "ACTIVE" else "ALL_STOPPED",
        "observed_models": [
            {"model_id": item.model_id, "state": item.state, "profile": item.profile}
            for item in observed
        ],
        "remote_attestation_absent": True,
        "sampled_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    evidence_path = config.state_dir / "recovery-evidence" / f"{operation_id}-live-state.json"
    _atomic_private_json(evidence_path, evidence)
    evidence_sha = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    verifier_key = MutationAdmission._read_secret(config.authority_credential)
    signature_payload = f"{evidence_sha}:{status['owner']}:{status['lease_generation']}".encode()
    proof = {
        "schema": "dgx-stale-retirement-proof-v1",
        "cluster_id": "dgx-head+dgx-worker",
        "owner": status["owner"],
        "lease_generation": status["lease_generation"],
        "evidence_path": str(evidence_path),
        "evidence_sha256": evidence_sha,
        "verifier_hmac_sha256": hmac.new(
            verifier_key.encode(), signature_payload, hashlib.sha256
        ).hexdigest(),
    }
    proof_path = config.state_dir / "recovery-evidence" / f"{operation_id}-proof.json"
    _atomic_private_json(proof_path, proof)
    return proof_path


def recover_release(config_path: Path, operation_id: str, safe_proof: Path) -> dict:
    config = load_config(config_path)
    store = OperationStore(config.state_dir / "gateway.sqlite3")
    operation = store.get_operation(operation_id)
    if operation.phase != "RECOVERY_REQUIRED":
        raise RuntimeError("operation is not awaiting recovery")
    token_path = config.token_dir / f"{operation_id}.token"
    status_result = subprocess.run(
        [str(config.lock_cli), "--root", str(config.lock_root), "status"],
        text=True, capture_output=True, timeout=10,
    )
    if status_result.returncode:
        raise RuntimeError("lock status is unavailable")
    status = json.loads(status_result.stdout)
    if status.get("state") != "STALE":
        raise RuntimeError("manual recovery release requires an expired STALE lease and fresh safe-state proof")
    verified_proof = _verified_safe_proof(config, operation_id, status, safe_proof)
    command = [
        str(config.lock_cli), "--root", str(config.lock_root), "release",
        "--owner", "dgx-cluster-gateway", "--token-file", str(token_path),
        "--operation-id", operation_id,
    ]
    command.extend([
        "--stale-retirement-proof", str(verified_proof),
        "--stale-retirement-verifier-key", str(config.authority_credential),
    ])
    result = subprocess.run(command, text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError("matching lock release recovery failed")
    receipt = json.loads(result.stdout)
    public = {
        key: receipt[key]
        for key in ("state", "release_operation_id", "release_receipt", "release_receipt_sha256")
        if key in receipt
    }
    store.append_event(operation_id, "manual-release-recovery", public)
    store.set_phase(operation_id, "RELEASED")
    store.close()
    return {"operation_id": operation_id, **public}


def resume_degraded_recovery(config_path: Path, operation_id: str) -> dict:
    """Continue a recorded head-fail-closed recovery under its live lease."""
    config = load_config(config_path)
    store = OperationStore(config.state_dir / "gateway.sqlite3")
    adapters = build_adapters(config)
    lease = GatewayLease(
        lock_cli=config.lock_cli,
        lock_root=config.lock_root,
        token_dir=config.token_dir,
        operation_id=operation_id,
        purpose=f"resume degraded local model switch {operation_id}",
    )
    controller = OperationController(
        store=store,
        adapters=adapters,
        lease_factory=lambda _operation_id: lease,
        acceptance_failure_model=config.acceptance_failure_model,
    )
    try:
        operation = controller.resume_degraded_recovery(operation_id, lease)
        return {
            "operation_id": operation.operation_id,
            "phase": operation.phase,
            "outcome": operation.outcome,
        }
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Explicit DGX Gateway release recovery")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--safe-proof", type=Path)
    parser.add_argument("--resume-degraded", action="store_true")
    args = parser.parse_args()
    if args.resume_degraded:
        if args.safe_proof is not None:
            parser.error("--safe-proof cannot be combined with --resume-degraded")
        result = resume_degraded_recovery(args.config, args.operation_id)
    else:
        if args.safe_proof is None:
            parser.error("--safe-proof is required unless --resume-degraded is used")
        result = recover_release(args.config, args.operation_id, args.safe_proof)
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
