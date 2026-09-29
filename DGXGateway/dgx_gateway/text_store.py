"""Text ownership records in the existing OperationStore SQLite transaction domain."""
from __future__ import annotations
import json
import time
from contextlib import contextmanager
from .store import utc_now


class TextFenced(RuntimeError):
    pass


class TextPersistence:
    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock
        with store._lock:
            store._db.executescript('''
            CREATE TABLE IF NOT EXISTS text_jobs(
              job_id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
              input_hash TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS text_attempts(
              attempt_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES text_jobs(job_id),
              epoch INTEGER NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS text_children(
              operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id),
              job_id TEXT NOT NULL REFERENCES text_jobs(job_id), epoch INTEGER NOT NULL,
              action TEXT NOT NULL, model TEXT NOT NULL, profile TEXT);
            CREATE TABLE IF NOT EXISTS text_events(
              id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES text_jobs(job_id),
              kind TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS text_quarantine(
              id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES text_jobs(job_id),
              epoch INTEGER NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL);
            ''')

    @contextmanager
    def transaction(self):
        with self.store._lock:
            db = self.store._db
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback(); raise

    def _get(self, db, job_id):
        row = db.execute('SELECT payload FROM text_jobs WHERE job_id=?', (job_id,)).fetchone()
        if row is None: raise KeyError(job_id)
        return json.loads(row[0])

    def get(self, job_id):
        with self.store._lock: return self._get(self.store._db, job_id)

    def find(self, key):
        with self.store._lock:
            row = self.store._db.execute('SELECT payload FROM text_jobs WHERE request_key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def list(self, limit=50):
        with self.store._lock:
            return [json.loads(x[0]) for x in self.store._db.execute('SELECT payload FROM text_jobs ORDER BY rowid DESC LIMIT ?', (limit,))]

    def active(self):
        with self.store._lock:
            row = self.store._db.execute('''SELECT job_id FROM text_jobs
              WHERE json_extract(payload,'$.resource_status') NOT IN ('released','not_acquired')
              OR json_extract(payload,'$.state') NOT IN ('SUCCEEDED','FAILED','CANCELED')
              ORDER BY rowid LIMIT 1''').fetchone()
            return row[0] if row else None

    def _save(self, db, row):
        row['updated_at'] = self.clock()
        db.execute('UPDATE text_jobs SET payload=? WHERE job_id=?',
                   (json.dumps(row, ensure_ascii=False, separators=(',', ':'), allow_nan=False), row['job_id']))

    def _event(self, db, job_id, kind, payload):
        db.execute('INSERT INTO text_events(job_id,kind,payload,created_at) VALUES(?,?,?,?)',
                   (job_id, kind, json.dumps(payload, ensure_ascii=False, allow_nan=False), self.clock()))

    @staticmethod
    def _new_work_allowed(db):
        fence = db.execute("SELECT value FROM metadata WHERE key='mutation_fenced'").fetchone()
        if fence is not None and fence[0] == '1': raise BlockingIOError('Gateway maintenance fenced new text work')

    def create(self, row):
        with self.transaction() as db:
            existing = db.execute('SELECT payload FROM text_jobs WHERE request_key=?', (row['idempotency_key'],)).fetchone()
            if existing: return json.loads(existing[0]), False
            self._new_work_allowed(db)
            if self.active(): raise BlockingIOError('Text resource owner exists')
            db.execute('INSERT INTO text_jobs VALUES(?,?,?,?)', (row['job_id'], row['idempotency_key'], row['input_hash'], json.dumps(row, ensure_ascii=False, allow_nan=False)))
            self._event(db, row['job_id'], 'created', {'input_hash': row['input_hash']})
            return row, True

    def claim(self, job_id, owner_id, seconds):
        with self.transaction() as db:
            self._new_work_allowed(db)
            row = self._get(db, job_id)
            if row['state'] != 'QUEUED' or row.get('owner_id') is not None: raise TextFenced('Job cannot be claimed')
            row.update(owner_id=owner_id, lease_epoch=row['lease_epoch'] + 1, lease_until=self.clock() + seconds)
            self._save(db, row); self._event(db, job_id, 'owner-claimed', {'epoch': row['lease_epoch']})
            return row

    def _guard(self, db, job_id, owner, epoch, *, recovery=False):
        row = self._get(db, job_id)
        if (row.get('owner_id'), row['lease_epoch']) != (owner, epoch) or row.get('lease_until', 0) <= self.clock():
            raise TextFenced('Text owner epoch expired or changed')
        if row['resource_status'] == 'recovery_required' and not recovery: raise TextFenced('Explicit resource reconciliation required')
        return row

    def guard(self, job_id, owner, epoch, *, recovery=False):
        with self.store._lock: return self._guard(self.store._db, job_id, owner, epoch, recovery=recovery)

    def update(self, job_id, owner, epoch, changes, event, *, recovery=False):
        with self.transaction() as db:
            row = self._guard(db, job_id, owner, epoch, recovery=recovery)
            row.update(changes); self._save(db, row)
            self._event(db, job_id, event, {'epoch': epoch})
            return row

    def cancel(self, job_id):
        with self.transaction() as db:
            row = self._get(db, job_id)
            if row['state'] in ('SUCCEEDED', 'FAILED', 'CANCELED') and row['resource_status'] in ('released', 'not_acquired'): return row
            row['cancel_requested'] = True
            if row['state'] == 'QUEUED' and row.get('owner_id') is None:
                row.update(state='CANCELED', resource_status='not_acquired')
            self._save(db, row); self._event(db, job_id, 'cancel-requested', {})
            return row

    def recover(self, job_id, *, expected_epoch=None, reason='Gateway restarted', owner_id=None, seconds=60):
        """Fence old workers; recovery ownership never authorizes inference replay."""
        with self.transaction() as db:
            row = self._get(db, job_id)
            if expected_epoch is not None and row['lease_epoch'] != expected_epoch: raise TextFenced('Epoch already replaced')
            if row['state'] in ('SUCCEEDED', 'FAILED', 'CANCELED') and row['resource_status'] in ('released', 'not_acquired'): raise TextFenced('Released job cannot be fenced back into recovery')
            row.update(lease_epoch=row['lease_epoch'] + 1, owner_id=owner_id,
                       lease_until=self.clock() + seconds if owner_id else 0,
                       state='RECOVERY_REQUIRED', resource_status='recovery_required', cleanup_required=True,
                       memory_released=False, error=reason)
            # Parent epoch revocation and abandoned child visibility are atomic.
            # No old child is declared complete/released; its outcome stays frozen.
            abandoned = db.execute("SELECT operation_id FROM text_children WHERE job_id=? AND epoch<?", (job_id, row['lease_epoch'])).fetchall()
            for child in abandoned:
                changed = db.execute("UPDATE operations SET phase='RECOVERY_REQUIRED',error=?,updated_at=? WHERE operation_id=? AND phase NOT IN ('RELEASED','DELEGATED_COMPLETED','RECOVERY_REQUIRED','CANCELED')", (reason, utc_now(), child['operation_id'])).rowcount
                if changed: self.store._append_event(child['operation_id'], 'parent-epoch-revoked', {'parent_job_id': job_id, 'new_epoch': row['lease_epoch'], 'resource_status': 'held_by_parent'})
            self._save(db, row); self._event(db, job_id, 'recovery-obligation', {'epoch': row['lease_epoch'], 'reason': reason})
            return row

    def prepare_attempt(self, job_id, owner, epoch, attempt_id, payload_hash):
        with self.transaction() as db:
            self._new_work_allowed(db)
            row = self._guard(db, job_id, owner, epoch)
            if row['attempt_id'] is not None: raise TextFenced('An attempt already exists; replay forbidden')
            if row['cancel_requested']: return False
            db.execute('INSERT INTO text_attempts VALUES(?,?,?,?,?)', (attempt_id, job_id, epoch, 'INTENT_DURABLE', json.dumps({'payload_hash': payload_hash})))
            row.update(attempt_id=attempt_id, dispatch_state='INTENT_DURABLE', state='SUBMITTING')
            self._save(db, row); self._event(db, job_id, 'dispatch-intent', {'attempt_id': attempt_id, 'epoch': epoch})
            return True

    def mark_sent(self, job_id, owner, epoch, attempt_id):
        with self.transaction() as db:
            self._new_work_allowed(db)
            row = self._guard(db, job_id, owner, epoch)
            if row['cancel_requested']: return False
            changed = db.execute("UPDATE text_attempts SET state='SENT' WHERE attempt_id=? AND job_id=? AND epoch=? AND state='INTENT_DURABLE'", (attempt_id, job_id, epoch)).rowcount
            if not changed: raise TextFenced('Attempt cannot be dispatched twice')
            row.update(dispatch_state='SENT', state='RUNNING'); self._save(db, row)
            self._event(db, job_id, 'dispatch-sent', {'attempt_id': attempt_id})
            return True

    def archive(self, job_id, owner, epoch, attempt_id, changes):
        with self.transaction() as db:
            row = self._guard(db, job_id, owner, epoch)
            changed = db.execute("UPDATE text_attempts SET state='RESULT_ARCHIVED',payload=? WHERE attempt_id=? AND job_id=? AND epoch=? AND state='SENT'", (json.dumps({'output_sha256': changes['output_sha256']}), attempt_id, job_id, epoch)).rowcount
            if not changed: raise TextFenced('Result does not belong to the live attempt')
            row.update(changes, dispatch_state='RESULT_ARCHIVED', state='VALIDATING')
            self._save(db, row); self._event(db, job_id, 'result-archived', {'attempt_id': attempt_id, 'sha256': changes['output_sha256']})
            return row

    def quarantine(self, job_id, epoch, payload):
        # Evidence only: cannot alter jobs, attempts, child operations or approval.
        with self.transaction() as db:
            db.execute('INSERT INTO text_quarantine(job_id,epoch,payload,created_at) VALUES(?,?,?,?)',
                       (job_id, epoch, json.dumps(payload, ensure_ascii=False), self.clock()))

    def create_child(self, job_id, owner, epoch, operation_id, action, model):
        with self.transaction() as db:
            row = self._guard(db, job_id, owner, epoch, recovery=True)
            if action not in ('load', 'stop'): raise ValueError('Invalid delegation action')
            if action == 'load': self._new_work_allowed(db)
            if row['resource_status'] == 'recovery_required' and action != 'stop': raise TextFenced('Recovery cannot load')
            now = utc_now()
            db.execute('''INSERT INTO operations(operation_id,source_model,target_model,idempotency_key,phase,created_at,updated_at)
                          VALUES(?,?,?,?, 'REQUESTED',?,?)''',
                       (operation_id, model if action == 'stop' else None, model if action == 'load' else None,
                        job_id + '-' + action + '-' + operation_id, now, now))
            db.execute('INSERT INTO text_children VALUES(?,?,?,?,?,NULL)', (operation_id, job_id, epoch, action, model))
            db.execute('UPDATE operations SET parent_job_id=?,parent_lease_epoch=?,parent_lease_generation=? WHERE operation_id=?',
                       (job_id, epoch, row['lease_generation'], operation_id))
            self.store._append_event(operation_id, 'parent-delegation', {'parent_job_id': job_id, 'lease_epoch': epoch, 'resource_status': 'held_by_parent'})
            row['operation_ids'].append(operation_id); self._save(db, row)
            return self.store.get_operation(operation_id)

    def child_binding(self, operation_id):
        with self.store._lock:
            row = self.store._db.execute('SELECT * FROM text_children WHERE operation_id=?', (operation_id,)).fetchone()
            return dict(row) if row else None

    def child_write(self, job_id, owner, epoch, operation_id, kind, value, *, error=None):
        with self.transaction() as db:
            self._guard(db, job_id, owner, epoch, recovery=True)
            binding = self.child_binding(operation_id)
            if not binding or (binding['job_id'], binding['epoch']) != (job_id, epoch): raise TextFenced('Child binding mismatch')
            if kind == 'phase':
                db.execute('UPDATE operations SET phase=?,error=?,updated_at=? WHERE operation_id=?', (value, error, utc_now(), operation_id))
                self.store._append_event(operation_id, 'phase', {'phase': value, 'error': error})
            elif kind == 'outcome':
                current = self.store.get_operation(operation_id)
                if current.outcome not in (None, value): raise ValueError('Outcome is immutable')
                db.execute('UPDATE operations SET outcome=?,updated_at=? WHERE operation_id=?', (value, utc_now(), operation_id))
                self.store._append_event(operation_id, 'outcome', {'outcome': value})
            else: self.store._append_event(operation_id, kind, value)
            return self.store.get_operation(operation_id)


class FencedOperationStore:
    """Existing lifecycle engine writes through epoch-bound SQLite transactions."""
    def __init__(self, persistence, job_id, owner, epoch):
        self.persistence, self.job_id, self.owner, self.epoch = persistence, job_id, owner, epoch
        self.path = persistence.store.path
    def __getattr__(self, name): return getattr(self.persistence.store, name)
    def set_phase(self, operation_id, phase, *, error=None):
        return self.persistence.child_write(self.job_id, self.owner, self.epoch, operation_id, 'phase', phase, error=error)
    def freeze_outcome(self, operation_id, outcome):
        return self.persistence.child_write(self.job_id, self.owner, self.epoch, operation_id, 'outcome', outcome)
    def append_event(self, operation_id, kind, payload):
        return self.persistence.child_write(self.job_id, self.owner, self.epoch, operation_id, kind, payload)
