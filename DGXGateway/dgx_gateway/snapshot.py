from __future__ import annotations

import json
import copy
import threading
import time
import subprocess
from concurrent.futures import Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone

from .config import GatewayConfig
from .connection_view import active_connection_view
from .domain import classify_local_models
from .domain import ObservedModel
from .operation_view import operation_view
from .store import OperationStore
from .telemetry import TelemetryCollector


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class SnapshotService:
    def __init__(self, config: GatewayConfig, store: OperationStore, adapters, telemetry=None, providers=()):
        self.config = config
        self.store = store
        self.adapters = adapters
        self.telemetry = telemetry or TelemetryCollector(config)
        self.providers = tuple(providers)
        self._build_lock = threading.Lock()
        self._flight = None
        self._cached = None
        self._cached_at = 0.0
        self._cache_ttl = 2.0

    def lock_status(self) -> dict:
        result = subprocess.run(
            [str(self.config.lock_cli), "--root", str(self.config.lock_root), "status"],
            text=True,
            capture_output=True,
            timeout=5,
        )
        if result.returncode:
            return {"state": "UNKNOWN", "error": "lock_status_failed"}
        value = json.loads(result.stdout)
        # Deliberately omit token hashes and controller process details.
        return {
            key: value[key]
            for key in ("state", "cluster_id", "owner", "task_id", "purpose", "lease_generation", "expires_at")
            if key in value
        }

    def build(self) -> dict:
        # HTTP callers may disconnect during slow SSH probes. They share one
        # flight rather than spawning another set of probes on each retry.
        with self._build_lock:
            if self._cached is not None and time.monotonic() - self._cached_at <= self._cache_ttl:
                return copy.deepcopy(self._cached)
            flight = self._flight
            leader = flight is None
            if leader:
                flight = Future()
                self._flight = flight
        if leader:
            try:
                value = self._build_uncached()
                with self._build_lock:
                    self._cached = copy.deepcopy(value)
                    self._cached_at = time.monotonic()
                flight.set_result(value)
            except BaseException as exc:
                flight.set_exception(exc)
            finally:
                with self._build_lock:
                    self._flight = None
        # Handlers add session data or omit connection details; never let a
        # caller mutate the cache or another caller's result.
        return copy.deepcopy(flight.result())

    def _build_uncached(self) -> dict:
        adapter_items = tuple(self.adapters.items())
        executor = ThreadPoolExecutor(max_workers=len(adapter_items) + 2)
        model_futures = {
            executor.submit(adapter.observe): model_id
            for model_id, adapter in adapter_items
        }
        telemetry_future = executor.submit(self.telemetry.collect)
        lock_future = executor.submit(self.lock_status)
        all_futures = set(model_futures) | {telemetry_future, lock_future}
        done, pending = wait(all_futures, timeout=9.0)
        for future in pending:
            future.cancel()
        observed = []
        for future, model_id in model_futures.items():
            if future not in done:
                observed.append(ObservedModel(model_id, "UNKNOWN"))
                continue
            try:
                observed.append(future.result())
            except Exception:
                observed.append(ObservedModel(model_id, "UNKNOWN"))
        if telemetry_future in done:
            try:
                nodes = telemetry_future.result()
            except Exception:
                nodes = []
        else:
            nodes = [{
                "role": node.role, "hostname": node.hostname, "address": node.address,
                "online": False, "sampled_at": utc_now(), "stale": True,
                "error": "snapshot_deadline_exceeded",
            } for node in self.config.nodes]
        if lock_future in done:
            try:
                lock = lock_future.result()
            except Exception:
                lock = {"state": "UNKNOWN", "error": "lock_status_failed"}
        else:
            lock = {"state": "UNKNOWN", "error": "snapshot_deadline_exceeded"}
        # Keep this flight active until already running probes finish. Canceling
        # a future cannot cancel its SSH subprocess; releasing the flight early
        # would accumulate detached executors on every cache expiration.
        executor.shutdown(wait=True, cancel_futures=True)
        active = classify_local_models(observed)
        observed_active = next((item for item in observed if item.model_id == active.model_id), None)
        active_connection = active_connection_view(
            self.config,
            active,
            observed_state=observed_active.state if observed_active else "UNKNOWN",
        )
        current = self.store.active_operation()
        recent = self.store.list_operations(limit=1)
        return {
            "schema_version": 1,
            "sampled_at": utc_now(),
            "nodes": nodes,
            "lock": lock,
            "active_local_model": {
                "state": active.state,
                "model_id": active.model_id,
                "profile": active.profile,
            },
            "active_connection": active_connection,
            "models": [{
                "model_id": item.model_id,
                "state": item.state,
                "profile": item.profile,
                "profiles": list(getattr(self.adapters[item.model_id], "profiles", ())),
                "default_profile": getattr(self.adapters[item.model_id], "default_profile", None),
                "saved_profile": self.store.last_committed_profile(item.model_id),
                "mutable": item.state != "UNAVAILABLE",
            } for item in observed],
            "providers": [provider.capability().to_dict() for provider in self.providers],
            "current_operation": operation_view(current) if current else None,
            "latest_operation": operation_view(recent[0]) if recent else None,
        }
