from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class OperationRecord:
    operation_id: str
    source_model: str | None
    source_profile: str | None
    target_model: str | None
    target_profile: str | None
    idempotency_key: str
    phase: str
    outcome: str | None
    cancel_requested: bool
    created_at: str
    updated_at: str
    error: str | None
    parent_job_id: str | None = None
    parent_lease_epoch: int | None = None
    parent_lease_generation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if not key.startswith("parent_") or value is not None}


class OperationStore:
    SCHEMA_VERSION = 1

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=DELETE")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        with self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    source_model TEXT,
                    source_profile TEXT,
                    target_model TEXT,
                    target_profile TEXT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    phase TEXT NOT NULL,
                    outcome TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS operation_outcome_immutable
                BEFORE UPDATE OF outcome ON operations
                WHEN OLD.outcome IS NOT NULL AND NEW.outcome IS NOT OLD.outcome
                BEGIN
                    SELECT RAISE(ABORT, 'operation outcome is immutable');
                END;
                """
            )
            columns = {
                row[1] for row in self._db.execute("PRAGMA table_info(operations)")
            }
            if "source_profile" not in columns:
                self._db.execute("ALTER TABLE operations ADD COLUMN source_profile TEXT")
            for column, kind in (("parent_job_id", "TEXT"), ("parent_lease_epoch", "INTEGER"), ("parent_lease_generation", "TEXT")):
                if column not in columns:
                    self._db.execute(f"ALTER TABLE operations ADD COLUMN {column} {kind}")
            self._db.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES('schema_version', ?)",
                (str(self.SCHEMA_VERSION),),
            )

    @staticmethod
    def _record(row: sqlite3.Row) -> OperationRecord:
        return OperationRecord(
            operation_id=row["operation_id"],
            source_model=row["source_model"],
            source_profile=row["source_profile"],
            target_model=row["target_model"],
            target_profile=row["target_profile"],
            idempotency_key=row["idempotency_key"],
            phase=row["phase"],
            outcome=row["outcome"],
            cancel_requested=bool(row["cancel_requested"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            error=row["error"],
            parent_job_id=row["parent_job_id"], parent_lease_epoch=row["parent_lease_epoch"],
            parent_lease_generation=row["parent_lease_generation"],
        )

    def create_operation(
        self,
        operation_id: str,
        source_model: str | None,
        target_model: str | None,
        idempotency_key: str,
        target_profile: str | None = None,
        source_profile: str | None = None,
    ) -> OperationRecord:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM operations WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if row:
                return self._record(row)
            now = utc_now()
            try:
                self._db.execute("BEGIN IMMEDIATE")
                fenced = self._db.execute(
                    "SELECT value FROM metadata WHERE key='mutation_fenced'"
                ).fetchone()
                if fenced is not None and fenced[0] == "1":
                    raise RuntimeError("Gateway mutation admission is quiesced")
                self._db.execute(
                        """INSERT INTO operations(
                            operation_id, source_model, source_profile, target_model, target_profile,
                            idempotency_key, phase, created_at, updated_at
                        ) VALUES(?, ?, ?, ?, ?, ?, 'REQUESTED', ?, ?)""",
                        (
                            operation_id,
                            source_model,
                            source_profile,
                            target_model,
                            target_profile,
                            idempotency_key,
                            now,
                            now,
                        ),
                    )
                self._append_event(operation_id, "created", {"phase": "REQUESTED"}, now)
                self._db.commit()
            except sqlite3.IntegrityError:
                self._db.rollback()
                row = self._db.execute(
                    "SELECT * FROM operations WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
                if row:
                    return self._record(row)
                raise
            except BaseException:
                self._db.rollback()
                raise
            return self.get_operation(operation_id)

    def set_mutation_fence(self, enabled: bool) -> OperationRecord | None:
        """Atomically fence new operations and report any operation already active."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                # The parent text owner remains active between delegated children.
                # Refuse before installing a fence that would obstruct its cleanup.
                if enabled and self._db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='text_jobs'"
                ).fetchone():
                    text_owner = self._db.execute("""SELECT job_id FROM text_jobs
                        WHERE COALESCE(json_extract(payload,'$.resource_status'),'unknown') NOT IN ('released','not_acquired')
                           OR COALESCE(json_extract(payload,'$.state'),'UNKNOWN') NOT IN ('SUCCEEDED','FAILED','CANCELED')
                        LIMIT 1""").fetchone()
                    if text_owner:
                        raise BlockingIOError(f"text job {text_owner[0]} owns DGX or requires recovery")
                self._db.execute(
                    "INSERT INTO metadata(key,value) VALUES('mutation_fenced',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    ("1" if enabled else "0",),
                )
                row = self._db.execute(
                    """SELECT * FROM operations
                       WHERE phase NOT IN ('RELEASED', 'DELEGATED_COMPLETED', 'RECOVERY_REQUIRED', 'CANCELED')
                       ORDER BY created_at DESC LIMIT 1"""
                ).fetchone()
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise
        return self._record(row) if row else None

    def _append_event(self, operation_id: str, kind: str, payload: dict, now: str | None = None) -> None:
        self._db.execute(
            "INSERT INTO events(operation_id, kind, payload_json, created_at) VALUES(?, ?, ?, ?)",
            (operation_id, kind, json.dumps(payload, sort_keys=True), now or utc_now()),
        )

    def set_phase(self, operation_id: str, phase: str, *, error: str | None = None) -> OperationRecord:
        with self._lock, self._db:
            now = utc_now()
            changed = self._db.execute(
                "UPDATE operations SET phase=?, error=?, updated_at=? WHERE operation_id=?",
                (phase, error, now, operation_id),
            ).rowcount
            if not changed:
                raise KeyError(operation_id)
            self._append_event(operation_id, "phase", {"phase": phase, "error": error}, now)
        return self.get_operation(operation_id)

    def freeze_outcome(self, operation_id: str, outcome: str) -> OperationRecord:
        with self._lock:
            current = self.get_operation(operation_id)
            if current.outcome is not None:
                if current.outcome == outcome:
                    return current
                raise ValueError("operation outcome is immutable")
            now = utc_now()
            with self._db:
                self._db.execute(
                    "UPDATE operations SET outcome=?, updated_at=? WHERE operation_id=? AND outcome IS NULL",
                    (outcome, now, operation_id),
                )
                self._append_event(operation_id, "outcome", {"outcome": outcome}, now)
        return self.get_operation(operation_id)

    def request_cancel(self, operation_id: str) -> OperationRecord:
        with self._lock:
            current = self.get_operation(operation_id)
            cancelable = {
                "REQUESTED",
                "PREFLIGHT",
                "LEASE_ACQUIRED",
                "SOURCE_DRAINING",
                "SOURCE_STOPPING",
                "SOURCE_STOPPED_PROVEN",
                "TARGET_STARTING",
                "TARGET_HEALTH",
            }
            if current.outcome is not None or current.phase not in cancelable:
                raise ValueError("operation is not cancelable in its current phase")
            with self._db:
                now = utc_now()
                changed = self._db.execute(
                    "UPDATE operations SET cancel_requested=1, updated_at=? WHERE operation_id=?",
                    (now, operation_id),
                ).rowcount
                if not changed:
                    raise KeyError(operation_id)
                self._append_event(operation_id, "cancel-requested", {}, now)
        return self.get_operation(operation_id)

    def get_operation(self, operation_id: str) -> OperationRecord:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if not row:
                raise KeyError(operation_id)
            return self._record(row)

    def active_operation(self) -> OperationRecord | None:
        with self._lock:
            row = self._db.execute(
                """SELECT * FROM operations
                   WHERE phase NOT IN ('RELEASED', 'DELEGATED_COMPLETED', 'RECOVERY_REQUIRED', 'CANCELED')
                   ORDER BY created_at DESC LIMIT 1"""
            ).fetchone()
            return self._record(row) if row else None

    def list_operations(self, limit: int = 50) -> list[OperationRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM operations ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._record(row) for row in rows]

    def last_committed_profile(self, model_id: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT target_profile FROM operations WHERE target_model=? "
                "AND outcome='COMMITTED' AND target_profile IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 1", (model_id,),
            ).fetchone()
            return row[0] if row else None

    def events(self, operation_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM events WHERE operation_id=? ORDER BY event_id", (operation_id,)
            ).fetchall()
            return [
                {
                    "event_id": row["event_id"],
                    "kind": row["kind"],
                    "payload": json.loads(row["payload_json"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]

    def append_event(self, operation_id: str, kind: str, payload: dict[str, Any]) -> None:
        with self._lock, self._db:
            if not self._db.execute(
                "SELECT 1 FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone():
                raise KeyError(operation_id)
            self._append_event(operation_id, kind, payload)

    def close(self) -> None:
        with self._lock:
            self._db.close()
