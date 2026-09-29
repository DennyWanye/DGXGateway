"""Gateway-owned idle model reuse; lifecycle mutations still use the normal lease.

A terminal job may transfer responsibility, never claim physical memory release.
Only an uninterrupted committed start/handoff history can be reused or retired.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid

from .comfy_identity import COMFY_SERVICE, LEGACY_COMFY_SERVICE, canonical_service


class ModelResidency:
    def __init__(self, app, idle_seconds=300):
        self.app = app
        self.idle_seconds = idle_seconds
        self.path = app.config.state_dir / 'model-residency.json'
        self._stop = threading.Event()
        self.row = json.loads(self.path.read_text()) if self.path.exists() else {'state': 'NONE'}
        if self.row.get('model_id') == LEGACY_COMFY_SERVICE:
            self.row['model_id'] = COMFY_SERVICE
            key = self.row.get('key')
            if isinstance(key, list) and key:
                self.row['key'] = [COMFY_SERVICE, *key[1:]]
        if self.row['state'] == 'CLAIMED':
            # A crash before the new start operation cannot have submitted a request.
            owner = self.row['owner_job_id']
            operations = app.store.list_operations(limit=-1)
            if not any(op.idempotency_key == owner + '-start' for op in operations):
                manager = getattr(app, self.row['manager'])
                path = manager.root / (owner + '.json')
                if path.exists():
                    old = json.loads(path.read_text())
                    old.update(state='INTERRUPTED', resource_not_acquired=True, memory_released=True,
                               resource_settled=True, resource_status='released', cleanup_required=False,
                               release_reason='Gateway restart before model handoff operation; no request submitted')
                    manager.save(old)
                if hasattr(manager, '_clear_active'): manager._clear_active(owner)
                elif manager.active == owner: manager.active = None
                self.row.update(owner_job_id=self.row['handoff']['previous_job_id'], manager=self.row['previous_manager'])
        if self.row['state'] != 'NONE':
            self.row.update(state='RECOVERY_REQUIRED', error='Gateway restarted; verify and explicitly release retained resources')
            self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        with tmp.open('w') as stream:
            json.dump(self.row, stream, ensure_ascii=False, indent=2)
            stream.flush(); os.fsync(stream.fileno())
        tmp.replace(self.path)
        fd = os.open(self.path.parent, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)

    @staticmethod
    def key(manager, row):
        if manager == 'video_jobs':
            return ['minimax-h3', row['profile']]
        if row.get('image_request', {}).get('tool') not in {'generate_image', 'edit_image'}:
            return None
        # Loader configuration, including encoder, VAE, quantization and LoRA.
        loaders = sorted(json.dumps(n, sort_keys=True) for n in row['workflow'].values()
                         if 'Loader' in n['class_type'])
        if not loaders: return None
        return [COMFY_SERVICE, hashlib.sha256('\n'.join(loaders).encode()).hexdigest()]

    def view(self):
        # Do not block status polling behind a slow network stop. A busy lock
        # makes the release button unavailable; only writes need the lock.
        acquired = self.app._submit_lock.acquire(blocking=False)
        try:
            row = dict(self.row)
            active = any(getattr(getattr(self.app, n, None), 'active', None) for n in ('video_jobs','creative_jobs'))
            row['can_release'] = acquired and row['state'] in {'IDLE', 'RECOVERY_REQUIRED'} and not active and not self.app.store.active_operation()
            row['idle_seconds'] = self.idle_seconds
            return row
        finally:
            if acquired: self.app._submit_lock.release()

    def _prove(self, *, allow_stopped=False):
        r = self.row
        latest = self.app.store.list_operations(limit=1)
        committed = bool(latest and latest[0].phase == 'RELEASED' and latest[0].outcome == 'COMMITTED')
        same_start = committed and latest[0].operation_id == r.get('start_operation_id')
        own_stop = committed and latest[0].idempotency_key == r['owner_job_id'] + '-stop' and latest[0].target_model is None and canonical_service(latest[0].source_model) == canonical_service(r['model_id'])
        if not same_start and not (allow_stopped and own_stop):
            raise BlockingIOError('Retained model ownership changed; inspect Gateway operations')
        observed = [a.observe() for a in self.app.operations.adapters.values()]
        if own_stop and (not observed or any(m.state != 'STOPPED' for m in observed)):
            raise BlockingIOError('Completed retained stop does not match current resources')
        if allow_stopped and observed and all(m.state == 'STOPPED' for m in observed): return 'STOPPED'
        if not observed or not any(canonical_service(m.model_id) == canonical_service(r['model_id']) and m.state == 'HEALTHY' and
                (r['model_id'] != 'minimax-h3' or m.profile == r.get('profile')) for m in observed):
            raise BlockingIOError('Retained model is not confirmed healthy')
        if any(canonical_service(m.model_id) != canonical_service(r['model_id']) and m.state != 'STOPPED' for m in observed):
            raise BlockingIOError('Another or unknown model prevents retained model mutation')
        if r['manager'] == 'creative_jobs':
            queue = self.app.creative_jobs.request('/queue')
            if queue.get('queue_running') or queue.get('queue_pending'):
                raise BlockingIOError('Comfy has active work; retained model cannot be claimed or stopped')
        return 'OWNED'

    def allow_claim(self, owner, target, profile):
        r = self.row
        return r['state'] == 'CLAIMED' and r.get('owner_job_id') == owner and canonical_service(r['model_id']) == canonical_service(target) and r.get('profile') == profile

    def before_start(self, manager, row):
        """Called in worker, before start. Claims and timer serialize on app lock."""
        key = self.key(manager, row)
        with self.app._submit_lock:
            r = self.row
            if r['state'] == 'NONE': return
            if r['state'] != 'IDLE': raise BlockingIOError('Retained model requires recovery or is busy')
            self._prove()
            if key != r['key'] or time.time() >= r['expires_at']:
                self._release_locked(r['owner_job_id'], incoming_owner=row['job_id'])
                return
            previous = dict(r)
            r['previous_manager'] = r['manager']
            r.update(state='CLAIMED', owner_job_id=row['job_id'], manager=manager)
            r['handoff'] = {'previous_operation_id': previous['start_operation_id'],
                            'previous_job_id': previous['owner_job_id'], 'resident_id': r['resident_id']}
            self.save()
            row.update(resident_id=r['resident_id'], resource_reused=True)

    def claimed(self, manager, row):
        with self.app._submit_lock:
            if self.row['state'] == 'CLAIMED' and self.row.get('owner_job_id') == row['job_id']:
                self.row.update(state='ACTIVE', start_operation_id=row['start_operation_id'])
                self.row.setdefault('jobs', []).append([manager, row['job_id']])
                self.save()

    def retain(self, manager, row):
        key = self.key(manager, row)
        if key is None or self.idle_seconds <= 0: return False
        with self.app._submit_lock:
            jobs = getattr(self.app, manager)
            if jobs.cancel_requested(row['job_id']): return False
            old = self.row
            if old['state'] not in {'NONE','ACTIVE'} or (old['state'] == 'ACTIVE' and old['owner_job_id'] != row['job_id']):
                raise BlockingIOError('Retained resource owner mismatch')
            self.row = {'state': 'IDLE', 'resident_id': row.get('resident_id', uuid.uuid4().hex),
                        'key': key, 'model_id': key[0], 'profile': row.get('profile'), 'manager': manager,
                        'owner_job_id': row['job_id'], 'start_operation_id': row['start_operation_id'],
                        'jobs': old.get('jobs', []) if old['state'] == 'ACTIVE' else [[manager,row['job_id']]],
                        'expires_at': time.time() + self.idle_seconds}
            try:
                self._prove()
                if manager == 'video_jobs':
                    jobs.assert_stop_ownership(row['job_id'], [a.observe() for a in self.app.operations.adapters.values()])
            except Exception:
                self.row = old
                raise
            self.save()  # Persist transfer before allowing next business task.
            row.update(resource_settled=True, resource_status='resident', resident_id=self.row['resident_id'],
                       memory_released=False, cleanup_required=False)
            return True

    def job_finished(self, manager, row):
        with self.app._submit_lock:
            r = self.row
            if r.get('owner_job_id') != row['job_id']: return
            if r['state'] == 'CLAIMED' and not row.get('start_operation_id'):
                # A failed preflight has not transferred the engine. Conversely,
                # a persisted operation with a lost response is still ambiguous.
                if any(op.idempotency_key == row['job_id'] + '-start'
                       for op in self.app.store.list_operations(limit=-1)):
                    row.update(cleanup_required=True, memory_released=False)
                    r.update(state='RECOVERY_REQUIRED', error='Handoff operation exists without its task receipt')
                    self.save()
                    return
                r.update(state='IDLE', owner_job_id=r['handoff']['previous_job_id'], manager=r['previous_manager'])
                self.save()
                row.update(resource_not_acquired=True, memory_released=True,
                           resource_settled=True, resource_status='released', cleanup_required=False,
                           release_reason='Model handoff rejected before an operation or inference request was submitted')
                return
            if row.get('memory_released'):
                for name, job_id in r.get('jobs', []):
                    if job_id == row['job_id']: continue
                    obj = getattr(self.app, name)
                    old = json.loads((obj.root/(job_id+'.json')).read_text())
                    old.update(memory_released=True, resource_settled=True, resource_status='released')
                    obj.save(old)
                self.row = {'state': 'NONE', 'released_at': time.time()}; self.save()
            elif r['state'] != 'IDLE':
                r.update(state='RECOVERY_REQUIRED', error='Task did not safely transfer or release model resources')
                self.save()

    def release_for_other(self):
        """Used by text admission. Never release a running/unknown generation."""
        with self.app._submit_lock:
            if self.row['state'] == 'NONE': return
            if self.row['state'] != 'IDLE': raise BlockingIOError('Retained model needs recovery')
            self._release_locked(self.row['owner_job_id'])

    def _release_locked(self, owner, incoming_owner=None):
        r = self.row
        if owner != r.get('owner_job_id') or r['state'] not in {'IDLE','RECOVERY_REQUIRED'}:
            raise BlockingIOError('Retained model owner changed')
        for name in ('creative_jobs','video_jobs'):
            active = getattr(getattr(self.app, name, None), 'active', None)
            if active and active != incoming_owner:
                raise BlockingIOError('An active task owns the model')
        self.app.admission.assert_allowed()
        if self.app.store.active_operation(): raise BlockingIOError('Lifecycle operation is active')
        try:
            stopped = self._prove(allow_stopped=True) == 'STOPPED'
            r['state'] = 'RELEASING'; self.save()
            jobs = getattr(self.app, r['manager'])
            row = json.loads((jobs.root / (owner+'.json')).read_text())
            if not stopped:
                # Internal release marker gates submit_switch without impersonating incoming task.
                self.releasing_owner = owner
                try: jobs.switch(row, None, 'stop')
                finally: self.releasing_owner = None
            for manager, job_id in r['jobs']:
                manager_obj = getattr(self.app, manager)
                old = json.loads((manager_obj.root/(job_id+'.json')).read_text())
                old.update(memory_released=True, resource_settled=True, resource_status='released')
                manager_obj.save(old)
            self.row = {'state': 'NONE', 'released_at': time.time(), 'last_resident_id': r['resident_id']}
            self.save()
        except Exception as exc:
            r.update(state='RECOVERY_REQUIRED', error=str(exc)); self.row=r; self.save()
            raise

    def release(self, owner):
        with self.app._submit_lock:
            self._release_locked(owner)
            return self.view()

    def start(self):
        def loop():
            while not self._stop.wait(1):
                try:
                    with self.app._submit_lock:
                        r=self.row
                        if r['state'] == 'IDLE' and time.time() >= r['expires_at']:
                            if any(getattr(getattr(self.app,n,None),'active',None) for n in ('video_jobs','creative_jobs')): continue
                            self._release_locked(r['owner_job_id'])
                except Exception: pass  # Persistent RECOVERY_REQUIRED is the visible outcome.
        self.thread=threading.Thread(target=loop, daemon=True, name='gateway-model-residency')
        self.thread.start()

    def close(self): self._stop.set()
