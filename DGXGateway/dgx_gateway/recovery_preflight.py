"""Prove an abandoned pre-lease operation safe without changing model runtimes."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import json

from .adapters.catalog import build_adapters
from .admission import MutationAdmission
from .authority import GatewayLease
from .config import load_config
from .operations import OperationContext, OperationController
from .store import OperationStore


def recover_preflight_stop(config_path: Path, operation_id: str):
    config = load_config(config_path)
    MutationAdmission(config).assert_allowed()
    store = OperationStore(config.state_dir / 'gateway.sqlite3')
    lease = GatewayLease(lock_cli=config.lock_cli, lock_root=config.lock_root,
                         token_dir=config.token_dir, operation_id=operation_id,
                         purpose='explicit orphan preflight stopped-proof recovery')
    acquired = False
    controller = None
    try:
        operation = store.get_operation(operation_id)
        events = store.events(operation_id)
        phases = [event['payload'].get('phase') for event in events if event['kind'] == 'phase']
        acquire_rejected = (
            operation.phase == 'RECOVERY_REQUIRED' and operation.outcome == 'RECOVERY_REQUIRED'
            and phases == ['PREFLIGHT', 'RECOVERY_REQUIRED']
            and all(event['kind'] in {'created', 'request-origin', 'phase', 'outcome'} for event in events)
            and str(operation.error or '').startswith('deployment-lock: ACTIVE lock already owned by ')
        )
        if not acquire_rejected and (operation.phase not in {'REQUESTED', 'PREFLIGHT'} or operation.outcome is not None):
            raise ValueError('operation is not an unfinished pre-lease request')
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(operation.updated_at.replace('Z', '+00:00'))).total_seconds()
        if age < 30:
            raise ValueError('preflight must be unchanged for at least 30 seconds')
        if lease.token_path.exists():
            raise ValueError('operation already has a lease token; use normal Gateway recovery')
        # Existing live/external cluster ownership rejects this normal acquire.
        lease.acquire()
        acquired = True
        lease.start_heartbeat()
        current = store.get_operation(operation_id)
        if current.phase != operation.phase or current.updated_at != operation.updated_at:
            raise RuntimeError('operation changed during recovery admission')
        adapters = build_adapters(config)
        controller = OperationController(store=store, adapters=adapters, lease_factory=lambda _: lease)
        context = OperationContext(operation_id, lease.token_path, lease.lease_generation, lease.check_supervision)
        controller._prove_others_stopped(operation_id, context, None, 'orphan-preflight-stopped-proof')
        current = store.get_operation(operation_id)
        if current.phase != operation.phase or current.updated_at != operation.updated_at:
            raise RuntimeError('operation changed while stopped proofs were collected')
        store.append_event(operation_id, 'orphan-preflight-safe-stop', {'runtime_mutations': False, 'lease_generation': lease.lease_generation})
        if operation.outcome is None:
            store.freeze_outcome(operation_id, 'FAILED_SAFE_STOPPED')
        controller._release(operation_id, lease)
        acquired = False
        return store.get_operation(operation_id).to_dict()
    except BaseException as exc:
        if acquired:
            if controller is not None:
                # Preserve the acquired lease for ordinary Gateway recovery if
                # stopped proof or release is uncertain. Never claim success.
                controller._recovery_required(operation_id, lease, exc)
                lease.stop_heartbeat()
            else:
                lease.release()
        raise
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--operation-id', required=True)
    args = parser.parse_args()
    print(json.dumps(recover_preflight_stop(args.config, args.operation_id), sort_keys=True, indent=2))


if __name__ == '__main__':
    main()
