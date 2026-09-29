from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Callable, Protocol

from .store import OperationRecord, OperationStore
from .authority import ParentLeaseDelegation
from .comfy_identity import canonical_service


class Adapter(Protocol):
    model_id: str

    def observe(self): ...
    def drain(self, context: "OperationContext") -> None: ...
    def stop(self, context: "OperationContext") -> None: ...
    def retire(self, context: "OperationContext") -> None: ...
    def stopped_proof(self, context: "OperationContext") -> dict[str, Any]: ...
    def start(self, context: "OperationContext", profile: str | None = None) -> None: ...
    def health(self, context: "OperationContext") -> dict[str, Any]: ...


@dataclass(frozen=True)
class OperationContext:
    operation_id: str
    token_path: Path
    lease_generation: str
    guard: Callable[[], None]


class OperationController:
    def __init__(self, *, store: OperationStore, adapters: dict[str, Adapter], lease_factory, acceptance_failure_model: str | None = None):
        self.store = store
        self.adapters = adapters
        self.lease_factory = lease_factory
        self.recovery_leases: dict[str, Any] = {}
        self.acceptance_failure_model = acceptance_failure_model

    def adapter_for(self, model_id: str | None) -> Adapter | None:
        if not model_id:
            return None
        adapter = self.adapters.get(model_id)
        if adapter is not None:
            return adapter
        return self.adapters.get(canonical_service(model_id) or "")

    def _prove_others_stopped(
        self,
        operation_id: str,
        context: OperationContext,
        active_model_id: str | None,
        event: str,
    ) -> None:
        for model_id, adapter in self.adapters.items():
            if model_id == active_model_id:
                continue
            proof = adapter.stopped_proof(context)
            self.store.append_event(operation_id, event, proof)

    def _recovery_required(self, operation_id: str, lease: Any, error: BaseException) -> None:
        self.store.set_phase(operation_id, "RECOVERY_REQUIRED", error=str(error))
        current = self.store.get_operation(operation_id)
        if current.outcome is None:
            self.store.freeze_outcome(operation_id, "RECOVERY_REQUIRED")
        self.recovery_leases[operation_id] = lease

    def _defer_worker_cleanup(
        self,
        operation: OperationRecord,
        lease: Any,
        context: OperationContext,
        source: Adapter,
        target: Adapter,
        error: BaseException,
    ) -> OperationRecord:
        fail_close_head = getattr(target, "fail_close_head", None)
        if not callable(fail_close_head):
            raise error
        self.store.set_phase(
            operation.operation_id,
            "TARGET_HEAD_FAIL_CLOSING",
            error=str(error),
        )
        proof = fail_close_head(context)
        if proof.get("head_fail_closed") is not True or proof.get("maintenance_active") is not True:
            raise RuntimeError("target head fail-close proof failed") from error
        self.store.append_event(
            operation.operation_id, "degraded-head-fail-closed", proof
        )
        self.store.set_phase(operation.operation_id, "TARGET_HEAD_FAIL_CLOSED")
        self.store.append_event(
            operation.operation_id,
            "degraded-recovery-obligation",
            {
                "schema": "dgx-gateway-degraded-recovery-v1",
                "lease_generation": context.lease_generation,
                "source_model": source.model_id,
                "source_profile": operation.source_profile,
                "target_model": target.model_id,
                "completed": ["head_target_fail_closed", "maintenance_gateway_active"],
                "remaining": [
                    "retire_target",
                    "prove_target_stopped",
                    "restore_source",
                    "prove_source_healthy",
                    "prove_other_models_stopped",
                    "release_lease",
                ],
            },
        )
        self._recovery_required(operation.operation_id, lease, error)
        return self.store.get_operation(operation.operation_id)

    def _cancel_requested(self, operation_id: str) -> bool:
        return self.store.get_operation(operation_id).cancel_requested

    def _finish_canceled(
        self,
        operation_id: str,
        lease: Any,
        context: OperationContext,
        source: Adapter | None,
        source_profile: str | None,
    ) -> OperationRecord:
        if source is not None:
            self.store.set_phase(operation_id, "CANCEL_ROLLBACK_STARTING")
            source.start(context, source_profile)
            if source.health(context).get("healthy") is not True:
                raise RuntimeError("canceled operation could not restore source")
            self.store.set_phase(operation_id, "CANCEL_SOURCE_RESTORED_PROVEN")
        else:
            self.store.set_phase(operation_id, "CANCEL_SAFE_STOPPED_PROVEN")
        self.store.freeze_outcome(operation_id, "CANCELED")
        self._release(operation_id, lease)
        return self.store.get_operation(operation_id)

    def resume_degraded_recovery(self, operation_id: str, lease: Any) -> OperationRecord:
        """Resume the single durable worker-cleanup obligation from store events."""
        operation = self.store.get_operation(operation_id)
        events = self.store.events(operation_id)
        obligations = [
            event
            for event in events
            if event["kind"] == "degraded-recovery-obligation"
        ]
        if not obligations:
            return self.resume_failed_switch_recovery(operation_id, lease)
        if len(obligations) != 1:
            raise RuntimeError("deferred recovery obligation is missing or ambiguous")
        obligation = obligations[0]["payload"]
        expected = {
            "schema": "dgx-gateway-degraded-recovery-v1",
            "source_model": operation.source_model,
            "source_profile": operation.source_profile,
            "target_model": operation.target_model,
        }
        if any(obligation.get(key) != value for key, value in expected.items()):
            raise RuntimeError("deferred recovery obligation does not match operation")
        resolution = {
            "schema": "dgx-gateway-degraded-recovery-resolution-v1",
            "source_model": operation.source_model,
            "source_profile": operation.source_profile,
            "target_model": operation.target_model,
            "lease_generation": str(obligation["lease_generation"]),
        }
        if operation.phase == "RELEASED" and operation.outcome == "RECOVERY_REQUIRED":
            if not any(event["kind"] == "degraded-recovery-resolved" for event in events):
                actions_complete = any(
                    event["kind"] == "degraded-recovery-actions-complete"
                    and event["payload"] == resolution
                    for event in events
                )
                lease_released = any(event["kind"] == "lease-release" for event in events)
                if not actions_complete or not lease_released:
                    raise RuntimeError("released recovery lacks durable completion proof")
                self.store.append_event(
                    operation_id, "degraded-recovery-resolved", resolution
                )
            return self.store.get_operation(operation_id)
        if operation.phase != "RECOVERY_REQUIRED" or operation.outcome != "RECOVERY_REQUIRED":
            raise RuntimeError("operation has no deferred recovery obligation")
        source = self.adapter_for(operation.source_model)
        target = self.adapter_for(operation.target_model)
        if source is None or target is None:
            raise RuntimeError("deferred recovery adapter is unavailable")
        lease.lease_generation = obligation.get("lease_generation")
        context = OperationContext(
            operation_id=operation_id,
            token_path=lease.token_path,
            lease_generation=str(obligation["lease_generation"]),
            guard=lease.check_supervision,
        )
        try:
            lease.start_heartbeat()
            context.guard()
            target.retire(context)
            target_proof = target.stopped_proof(context)
            self.store.append_event(
                operation_id, "degraded-target-stopped-proof", target_proof
            )
            source.start(context, operation.source_profile)
            source_health = source.health(context)
            if source_health.get("healthy") is not True:
                raise RuntimeError("deferred recovery source health proof failed")
            self.store.append_event(
                operation_id, "degraded-source-health-proof", source_health
            )
            self._prove_others_stopped(
                operation_id,
                context,
                source.model_id,
                "degraded-other-model-stopped-proof",
            )
            self.store.append_event(
                operation_id, "degraded-recovery-actions-complete", resolution
            )
            self._release(operation_id, lease)
            self.store.append_event(
                operation_id,
                "degraded-recovery-resolved",
                resolution,
            )
            return self.store.get_operation(operation_id)
        except BaseException as error:
            self._recovery_required(operation_id, lease, error)
            return self.store.get_operation(operation_id)

    def resume_failed_switch_recovery(self, operation_id: str, lease: Any) -> OperationRecord:
        """Safely roll back a generic failed switch while its exact lease is live."""
        operation = self.store.get_operation(operation_id)
        events = self.store.events(operation_id)
        if operation.phase != "RECOVERY_REQUIRED" or operation.outcome != "RECOVERY_REQUIRED":
            raise RuntimeError("operation has no resumable failed-switch recovery")
        phases = [
            event["payload"].get("phase")
            for event in events
            if event["kind"] == "phase"
        ]
        if phases.count("SOURCE_STOPPED_PROVEN") != 1 or "TARGET_STARTING" not in phases:
            raise RuntimeError("failed-switch recovery history is incomplete")
        source = self.adapter_for(operation.source_model)
        target = self.adapter_for(operation.target_model)
        if source is None or target is None:
            raise RuntimeError("failed-switch recovery adapter is unavailable")
        guarded = lease.guard()
        generation = str(guarded.get("lease_generation", ""))
        if not generation:
            raise RuntimeError("failed-switch recovery lease generation is unavailable")
        lease.lease_generation = generation
        context = OperationContext(
            operation_id=operation_id,
            token_path=lease.token_path,
            lease_generation=generation,
            guard=lease.check_supervision,
        )
        resolution = {
            "schema": "dgx-gateway-failed-switch-recovery-v1",
            "source_model": operation.source_model,
            "source_profile": operation.source_profile,
            "target_model": operation.target_model,
            "lease_generation": generation,
        }
        try:
            lease.start_heartbeat()
            context.guard()
            target.retire(context)
            target_proof = target.stopped_proof(context)
            self.store.append_event(operation_id, "recovery-target-stopped-proof", target_proof)
            source.start(context, operation.source_profile)
            source_health = source.health(context)
            if source_health.get("healthy") is not True:
                raise RuntimeError("failed-switch recovery source health proof failed")
            self.store.append_event(operation_id, "recovery-source-health-proof", source_health)
            self._prove_others_stopped(
                operation_id, context, source.model_id, "recovery-other-model-stopped-proof"
            )
            self.store.append_event(operation_id, "failed-switch-recovery-actions-complete", resolution)
            self._release(operation_id, lease)
            self.store.append_event(operation_id, "failed-switch-recovery-resolved", resolution)
            return self.store.get_operation(operation_id)
        except BaseException as error:
            self._recovery_required(operation_id, lease, error)
            return self.store.get_operation(operation_id)

    def _release(self, operation_id: str, lease: Any) -> None:
        if isinstance(lease, ParentLeaseDelegation):
            lease.check_supervision()
            self.store.append_event(operation_id, "delegated-completion", {
                "parent_job_id": lease.parent_job_id, "lease_epoch": lease.lease_epoch,
                "lease_generation": lease.lease_generation, "resource_status": "held_by_parent",
            })
            self.store.set_phase(operation_id, "DELEGATED_COMPLETED")
            return
        self.store.set_phase(operation_id, "RELEASING")
        lease.check_supervision()
        completion = lease.release()
        public_completion = {
            key: completion[key]
            for key in (
                "state", "release_operation_id", "operation_id",
                "lease_generation", "release_receipt", "release_receipt_sha256",
            )
            if key in completion
        }
        self.store.append_event(operation_id, "lease-release", public_completion)
        self.store.set_phase(operation_id, "RELEASED")

    def _video_start_policy(self, operation: OperationRecord) -> bool:
        """Bind a video start to the job intent durably saved before operation creation."""
        match = re.fullmatch(r"([a-f0-9]{32})-start", operation.idempotency_key)
        if not match:
            return False
        job_id = match.group(1)
        root = self.store.path.parent / "video-jobs"
        path = root / (job_id + ".json")
        if operation.target_model != "minimax-h3" and not path.exists() and not path.is_symlink():
            return False  # Existing image and other lifecycle owners use their own keys.
        if root.is_symlink():
            raise ValueError("Video start admission intent is unavailable")
        from .image_assets import _read_regular
        row = json.loads(_read_regular(path, 2 * 1024 * 1024))
        # The row freezes its backend so a later routing change cannot invalidate
        # an already accepted Ring4, dual-pair, or legacy two-node lifecycle.
        from .video_jobs import _row_runtime
        expected_model, expected_profile, _task = _row_runtime(row)
        if (not isinstance(row, dict) or row.get("job_id") != job_id or operation.target_model != expected_model
                or operation.target_profile != expected_profile or type(row.get("non_preemptive")) is not bool
                or (row["non_preemptive"] and operation.source_model is not None)):
            raise ValueError("Video start admission intent does not match the operation")
        return row["non_preemptive"]

    def _guard_video_stop(self, operation: OperationRecord, context: OperationContext) -> None:
        match = re.fullmatch(r"([a-f0-9]{32})-(stop|cleanup|reconcile_stop|cancel_stop)", operation.idempotency_key)
        if not match:
            return
        job_id = match.group(1)
        root = self.store.path.parent / "video-jobs"
        path = root / (job_id + ".json")
        if operation.source_model != "minimax-h3" and not path.exists() and not path.is_symlink():
            return
        if root.is_symlink():
            raise ValueError("Video stop ownership intent is unavailable")
        from .image_assets import _read_regular
        from .video_jobs import assert_video_stop_ownership, _row_runtime
        row = json.loads(_read_regular(path, 2 * 1024 * 1024))
        if not isinstance(row, dict):
            raise ValueError("Video stop ownership intent is invalid")
        runtime_model, runtime_profile, _ = _row_runtime(row)
        if (not isinstance(row, dict) or row.get("job_id") != job_id or operation.target_model is not None
                or operation.source_model not in {None, runtime_model}
                or (operation.source_model is not None and runtime_profile != operation.source_profile)):
            raise ValueError("Video stop intent does not match the lifecycle operation")
        row["cancel_requested"] = (root / (job_id + ".cancel")).is_file()
        context.guard()
        observed = [adapter.observe() for adapter in self.adapters.values()]
        assert_video_stop_ownership(self.store, row, observed, current_operation_id=operation.operation_id)

    def execute_delegated(self, operation_id: str, delegation: ParentLeaseDelegation) -> OperationRecord:
        if not isinstance(delegation, ParentLeaseDelegation) or delegation.operation_id != operation_id:
            raise ValueError("Invalid internal parent lease delegation")
        delegation.check_supervision()
        controller = OperationController(store=delegation.store, adapters=self.adapters,
                                         lease_factory=self.lease_factory,
                                         acceptance_failure_model=self.acceptance_failure_model)
        return controller._execute(operation_id, delegation)

    def execute(self, operation_id: str) -> OperationRecord:
        return self._execute(operation_id)

    def _execute(self, operation_id: str, delegation: ParentLeaseDelegation | None = None) -> OperationRecord:
        operation = self.store.get_operation(operation_id)
        if operation.phase in {"RELEASED", "DELEGATED_COMPLETED"}:
            return operation
        if operation.phase == "REQUESTED" and operation.cancel_requested:
            self.store.set_phase(operation_id, "CANCELED")
            self.store.freeze_outcome(operation_id, "CANCELED")
            return self.store.get_operation(operation_id)
        source = self.adapter_for(operation.source_model)
        target = self.adapter_for(operation.target_model)
        if operation.target_model and target is None:
            self.store.set_phase(operation_id, "RECOVERY_REQUIRED", error="unknown target adapter")
            self.store.freeze_outcome(operation_id, "RECOVERY_REQUIRED")
            return self.store.get_operation(operation_id)

        self.store.set_phase(operation_id, "PREFLIGHT")
        lease = delegation if delegation is not None else self.lease_factory(operation_id)
        try:
            if delegation is not None:
                acquired = delegation.borrow()
            else:
                acquired = lease.acquire()
                lease.start_heartbeat()
            generation = acquired["lease_generation"]
            context = OperationContext(
                operation_id=operation_id,
                token_path=lease.token_path,
                lease_generation=generation,
                guard=lease.check_supervision,
            )
            self.store.set_phase(operation_id, "LEASE_ACQUIRED")

            # This check runs after acquiring the actual cross-node authority.
            # It is deliberately outside target-start rollback: on rejection no
            # model was ours, so retiring the target could destroy external work.
            try:
                self._guard_video_stop(operation, context)
                non_preemptive = self._video_start_policy(operation)
                if non_preemptive:
                    context.guard()
                    observed = [adapter.observe() for adapter in self.adapters.values()]
                    if not observed or any(item.state != "STOPPED" for item in observed):
                        raise BlockingIOError("Non-preemptive video start found an existing or uncertain model")
            except (OSError, ValueError, TypeError, RuntimeError):
                self.store.append_event(operation_id, "no-resources-acquired", {"reason": "video_lifecycle_admission_rejected"})
                self.store.freeze_outcome(operation_id, "NO_RESOURCES_ACQUIRED")
                self._release(operation_id, lease)
                return self.store.get_operation(operation_id)

            if self._cancel_requested(operation_id):
                self.store.set_phase(operation_id, "CANCEL_SAFE_STOPPED_PROVEN")
                self.store.freeze_outcome(operation_id, "CANCELED")
                self._release(operation_id, lease)
                return self.store.get_operation(operation_id)

            if source is not None:
                self.store.set_phase(operation_id, "SOURCE_DRAINING")
                if delegation is not None:
                    proof = delegation.before_destructive(source.model_id)
                    self.store.append_event(operation_id, "delegated-admission-drained", proof)
                else:
                    source.drain(context)
                context.guard()
                self.store.set_phase(operation_id, "SOURCE_STOPPING")
                source.stop(context)
                context.guard()
                proof = source.stopped_proof(context)
                self.store.append_event(operation_id, "source-stopped-proof", proof)
                self.store.set_phase(operation_id, "SOURCE_STOPPED_PROVEN")

                if self._cancel_requested(operation_id):
                    return self._finish_canceled(
                        operation_id, lease, context, source, operation.source_profile
                    )

            if target is None:
                self._prove_others_stopped(
                    operation_id, context, None, "all-local-models-stopped-proof"
                )
                self.store.freeze_outcome(operation_id, "COMMITTED")
                self._release(operation_id, lease)
                return self.store.get_operation(operation_id)

            try:
                self.store.set_phase(operation_id, "TARGET_STARTING")
                target.start(context, operation.target_profile)
                context.guard()
                if self._cancel_requested(operation_id):
                    raise InterruptedError("operation cancellation requested")
                self.store.set_phase(operation_id, "TARGET_HEALTH")
                health = target.health(context)
                self.store.append_event(operation_id, "target-health-proof", health)
                if target.model_id == self.acceptance_failure_model:
                    self.store.append_event(operation_id, "acceptance-failure-injected", {"model_id": target.model_id})
                    raise RuntimeError("bounded acceptance target health failure")
                if health.get("healthy") is not True:
                    raise RuntimeError("target health proof failed")
                if self._cancel_requested(operation_id):
                    raise InterruptedError("operation cancellation requested after health proof")
                self._prove_others_stopped(
                    operation_id, context, target.model_id, "other-local-model-stopped-proof"
                )
                self.store.set_phase(operation_id, "TARGET_HEALTHY")
                self.store.freeze_outcome(operation_id, "COMMITTED")
            except BaseException as target_error:
                self.store.set_phase(operation_id, "TARGET_RETIRING", error=str(target_error))
                try:
                    if delegation is not None:
                        proof = delegation.before_destructive(target.model_id)
                        self.store.append_event(operation_id, "delegated-admission-drained", proof)
                    target.retire(context)
                    context.guard()
                    proof = target.stopped_proof(context)
                except BaseException as cleanup_error:
                    if source is not None:
                        return self._defer_worker_cleanup(
                            operation,
                            lease,
                            context,
                            source,
                            target,
                            cleanup_error,
                        )
                    raise
                self.store.append_event(operation_id, "target-stopped-proof", proof)
                self.store.set_phase(operation_id, "TARGET_STOPPED_PROVEN")
                if source is None:
                    self.store.freeze_outcome(operation_id, "FAILED_SAFE_STOPPED")
                else:
                    self.store.set_phase(operation_id, "ROLLBACK_STARTING")
                    source.start(context, operation.source_profile)
                    source_health = source.health(context)
                    self.store.append_event(operation_id, "rollback-source-health-proof", source_health)
                    if source_health.get("healthy") is not True:
                        raise RuntimeError("source rollback health proof failed")
                    self._prove_others_stopped(
                        operation_id, context, source.model_id, "rollback-other-model-stopped-proof"
                    )
                    self.store.set_phase(
                        operation_id, "SOURCE_HEALTHY_AND_OTHERS_STOPPED_PROVEN"
                    )
                    self.store.freeze_outcome(operation_id, "ROLLED_BACK")

            self._release(operation_id, lease)
            return self.store.get_operation(operation_id)
        except BaseException as error:
            current = self.store.get_operation(operation_id)
            if current.phase not in {"RELEASED", "DELEGATED_COMPLETED"}:
                self._recovery_required(operation_id, lease, error)
            return self.store.get_operation(operation_id)
