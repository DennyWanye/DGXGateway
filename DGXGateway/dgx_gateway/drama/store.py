"""Local SQLite truth; no model or media work happens inside a write transaction."""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .contracts import DramaError, text


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def new_id() -> str:
    return uuid.uuid4().hex


class DramaStore:
    SCHEMA_VERSION = 9

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            current = db.execute("PRAGMA user_version").fetchone()[0]
            if current < 0 or current > self.SCHEMA_VERSION:
                raise RuntimeError("Unsupported drama database version")
            if 0 < current < self.SCHEMA_VERSION:
                backup = path.with_name(f"{path.name}.before-v{current + 1}.bak")
                if not backup.exists():
                    with closing(sqlite3.connect(backup)) as target:
                        db.backup(target)
            names = {1: "001_initial.sql", 2: "002_production_revisions.sql", 3: "003_reference_jobs.sql", 4: "004_video_jobs.sql", 5: "005_postproduction.sql", 6: "006_scripts.sql", 7: "007_model_residency.sql", 8: "008_reference_candidate_reviews.sql", 9: "009_project_kind_and_song.sql"}
            for version in range(current + 1, self.SCHEMA_VERSION + 1):
                db.executescript((Path(__file__).parent / "migrations" / names[version]).read_text())

    @contextmanager
    def connect(self, write: bool = False):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def mutate(self, scope: str, key, payload, operation):
        key = text(key, "requestKey", 128)
        try:
            digest = hashlib.sha256(encode(payload).encode()).hexdigest()
        except (ValueError, TypeError) as exc:
            raise DramaError("invalid_input", "请求必须包含有限数值和有效JSON内容") from exc
        with self.connect(write=True) as db:
            old = db.execute("SELECT * FROM dr_requests WHERE scope=? AND request_key=?", (scope, key)).fetchone()
            if old:
                if old["input_hash"] != digest:
                    raise DramaError("request_conflict", "同一请求编号不能用于不同内容", 409)
                return json.loads(old["response"])
            result = operation(db)
            db.execute("INSERT INTO dr_requests VALUES (?,?,?,?,?)", (scope, key, digest, encode(result), time.time()))
            from .business_mcp import CALL_SOURCE
            db.execute("INSERT INTO dr_events VALUES (?,?,?,?,?)", (new_id(), scope, "mutation", encode({"requestKey": key, "actor": CALL_SOURCE.get()}), time.time()))
            return result

    @staticmethod
    def one(db, table: str, object_id: str):
        if table not in {"dr_projects", "dr_episodes", "dr_assets", "dr_shots", "dr_takes", "dr_timeline_revisions", "dr_exports",
                         "dr_entities", "dr_entity_revisions", "dr_shot_revisions", "dr_invalidations", "dr_reference_jobs", "dr_reference_candidates", "dr_video_jobs", "dr_video_items", "dr_subtitle_revisions", "dr_transcriptions"}:
            raise ValueError("invalid table")
        row = db.execute(f"SELECT * FROM {table} WHERE id=?", (object_id,)).fetchone()
        if row is None:
            raise DramaError("not_found", "找不到指定对象", 404)
        return dict(row)
