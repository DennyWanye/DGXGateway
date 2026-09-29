"""Local administrator recovery of an existing text resource obligation.

Uses the original signed stale-retirement primitive and ordinary GatewayLease.
No inference, remote URL selection, force unlock, or alternative authority.
"""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from .admission import MutationAdmission
from .authority import GatewayLease, LeaseError
from .recovery import _atomic_private_json
from .ssh import FixedSSH
from .text_jobs import MODEL, TextJobError, canonical
from .text_store import TextFenced


class RecoveryObservation:
    """Read-only exact runtime inventory; never uses an expired mutation context."""
    def __init__(self, app): self.app = app
    def inspect(self, guard):
        observed = []
        for adapter in self.app.operations.adapters.values():
            guard(); observed.append(adapter.observe())
        guard()
        active = [x for x in observed if x.state != 'STOPPED']
        if not observed or (active and (len(active) != 1 or active[0].model_id != MODEL or active[0].state not in ('HEALTHY','PARTIAL'))):
            raise TextFenced('Recovery requires all stopped or exclusively the recorded DeepSeek runtime')
        identity = self.app.operations.adapters[MODEL]._text_runtime_identity(admission_open=None) if active else None
        for node in self.app.config.nodes:
            guard()
            response = FixedSSH().run(node, ['sh','-c','hostname; whoami; test ! -e /run/minimax-h3-two-node/mac-lock-attestation.json'], timeout=10)
            if response.returncode or response.stdout.splitlines() != [node.hostname,node.user]:
                raise TextFenced('Remote runtime attestation absence is not proven')
        guard()
        return {'state':'ACTIVE_EXCLUSIVE' if active else 'ALL_STOPPED','runtime_identity':identity,
                'models':[{'model_id':x.model_id,'state':x.state} for x in observed], 'remote_attestation_absent':True,
                'admission_observed_closed':bool(active and identity.get('gateway_container_id') is None),
                'runtime_identity_sha256':hashlib.sha256(canonical(identity).encode()).hexdigest() if identity else None}


class TextAdminRecovery:
    def __init__(self, jobs, *, observer=None):
        self.jobs, self.app, self.persistence = jobs, jobs.app, jobs.persistence
        self.observer = observer or RecoveryObservation(self.app)
        with self.persistence.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS text_admin_recoveries(recovery_id TEXT PRIMARY KEY,recovery_key TEXT UNIQUE NOT NULL,job_id TEXT NOT NULL,payload TEXT NOT NULL)')

    def _get(self, key):
        with self.app.store._lock:
            row = self.app.store._db.execute('SELECT payload FROM text_admin_recoveries WHERE recovery_key=?',(key,)).fetchone()
            return json.loads(row[0]) if row else None

    def _authority(self, expected=None):
        current = self.app.admission.assert_allowed()
        if not isinstance(current,dict) or current.get('allowed') is not True or type(current.get('epoch')) is not int:
            raise TextFenced('Administrator authority is unavailable')
        result = {'authority_id':current['authority_id'],'epoch':current['epoch']}
        if expected is not None and result != expected: raise TextFenced('Administrator authority epoch changed')
        return result

    def _save(self, record, phase=None, **updates):
        with self.persistence.transaction() as db:
            row = self.persistence._guard(db, record['job_id'], record['owner_id'], record['job_epoch'], recovery=True)
            current = self._get(record['recovery_key'])
            if current['owner_id'] != record['owner_id'] or current['job_epoch'] != record['job_epoch']:
                raise TextFenced('Administrator recovery owner changed')
            current.update(updates)
            if phase: current['phase'] = phase
            current['updated_at'] = time.time()
            row['lease_until'] = self.jobs.clock()+60
            if current['phase']=='DONE':
                row.update(admin_recovery_pending=None,error_code=None,recovery_action=None,error=current.get('original_error'))
            self.persistence._save(db,row)
            db.execute('UPDATE text_admin_recoveries SET payload=? WHERE recovery_id=?',(canonical(current),record['recovery_id']))
            self.persistence._event(db,record['job_id'],'admin-recovery-phase',{'recovery_id':record['recovery_id'],'phase':current['phase'],'epoch':record['job_epoch']})
            record.update(current)

    def _guard(self, record):
        self._authority(record['authority'])
        self.persistence.update(record['job_id'],record['owner_id'],record['job_epoch'],{'lease_until':self.jobs.clock()+60},'admin-owner-renewed',recovery=True)

    def _lease(self, operation_id):
        return GatewayLease(lock_cli=self.app.config.lock_cli,lock_root=self.app.config.lock_root,token_dir=self.app.config.token_dir,
                            operation_id=operation_id,purpose='explicit text administrator cleanup')

    def _status(self, lease): return lease._run('status')

    @staticmethod
    def _matching_identity(expected, actual):
        if not isinstance(expected,dict) or not expected: return False
        if 'nodes' in expected:
            return actual and actual.get('nodes') == expected['nodes'] and actual.get('gateway_container_id') in (None,expected.get('gateway_container_id'))
        return actual == expected  # deterministic isolated test adapter

    def _inspect(self, record):
        observation = self.observer.inspect(lambda:self._guard(record))
        if observation.get('remote_attestation_absent') is not True: raise TextFenced('Missing remote attestation absence')
        if observation['state'] == 'ACTIVE_EXCLUSIVE':
            if not self._matching_identity(record.get('runtime_identity'),observation.get('runtime_identity')):
                raise TextFenced('Original persisted runtime identity is missing or changed; no inferred ownership')
        elif observation['state'] != 'ALL_STOPPED': raise TextFenced('Ambiguous resource state')
        return observation

    def _bind_stale_acquire(self, record, current):
        """Recover acquisition evidence, not a new lease, after lost DB acknowledgement."""
        if record.get('new_generation') or record['phase'] != 'NEW_ACQUIRE_INTENT': return record, current
        self._authority(record['authority'])
        lease = self._lease(record['new_resource_operation_id'])
        path = lease.token_path
        if not path.exists(): return record, current
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077: raise TextFenced('Recovery intent token is unsafe')
        status = self._status(lease)
        if status.get('state') != 'STALE': return record, current
        digest = hashlib.sha256(path.read_text().strip().encode()).hexdigest()
        if (status.get('task_id') != lease.operation_id or status.get('owner') != GatewayLease.OWNER
                or status.get('token_sha256') != digest): raise TextFenced('Stale acquisition does not match persisted recovery intent')
        with self.persistence.transaction() as db:
            row = self.persistence._get(db,current['job_id']); saved = self._get(record['recovery_key'])
            if (row['lease_epoch'] != current['lease_epoch'] or row['lease_generation'] != record['old_generation']
                    or saved['phase'] != 'NEW_ACQUIRE_INTENT' or saved.get('new_generation')):
                raise TextFenced('Stale acquisition binding CAS changed')
            row.update(resource_operation_id=lease.operation_id,lease_generation=status['lease_generation'],stop_proof=None,release_receipt=None)
            saved.update(phase='NEW_LEASE_HELD',new_generation=status['lease_generation'],acquire_ack_recovered=True)
            self.persistence._save(db,row)
            db.execute('UPDATE text_admin_recoveries SET payload=? WHERE recovery_id=?',(canonical(saved),saved['recovery_id']))
            self.persistence._event(db,current['job_id'],'stale-admin-acquire-bound',{'recovery_id':saved['recovery_id'],'generation':status['lease_generation']})
            return saved,row

    def _reserve(self, job_id, expected_epoch, key, authority):
        old = self._get(key)
        if old:
            if old['job_id'] != job_id or old['expected_epoch'] != expected_epoch: raise TextJobError('TEXT_KEY_CONFLICT','恢复编号与原任务/epoch不匹配',409)
            if old['phase'] == 'DONE': return old
        current = self.persistence.get(job_id)
        if old: old,current = self._bind_stale_acquire(old,current)
        if job_id in self.jobs._running or self.jobs._other_busy(allow_job=job_id): raise TextJobError('TEXT_BUSY','其他执行或恢复仍在进行',409)
        if not old:
            if current['lease_epoch'] != expected_epoch or current['resource_status'] in ('released','not_acquired'):
                raise TextFenced('Expected unreleased text epoch is not current')
            if current.get('admin_recovery_pending'):
                previous = self._get(current['admin_recovery_pending'])
                if previous: previous,current = self._bind_stale_acquire(previous,current)
                # Only a new expired *actual* recovery lease permits a new journal.
                if not previous or not previous.get('new_generation') or current.get('lease_generation') != previous['new_generation']:
                    raise TextFenced('Continue the original administrator recovery key')
            operation_id = current.get('resource_operation_id') or 'text-'+job_id
            status = self._status(self._lease(operation_id))
            if status.get('state') != 'STALE' or status.get('task_id') != operation_id or status.get('lease_generation') != current['lease_generation'] or status.get('owner') != GatewayLease.OWNER:
                raise TextFenced('Only the exact original STALE capability may be retired')
        with self.persistence.transaction() as db:
            row = self.persistence._get(db,job_id)
            if old:
                if row.get('admin_recovery_pending') != key or row.get('lease_generation') not in (old['old_generation'],old.get('new_generation')):
                    raise TextFenced('Recovery journal no longer owns this text resource')
            elif row['lease_epoch'] != expected_epoch:
                raise TextFenced('Recovery expected epoch changed')
            epoch = row['lease_epoch']+1; owner = 'text-admin-'+uuid.uuid4().hex
            record = old or {'recovery_id':uuid.uuid4().hex,'recovery_key':key,'job_id':job_id,'expected_epoch':expected_epoch,
                'old_generation':row['lease_generation'],'old_resource_operation_id':row.get('resource_operation_id') or 'text-'+job_id,
                'new_resource_operation_id':'text-recovery-'+uuid.uuid4().hex,'new_generation':None,'phase':'RESERVED',
                'authority':authority,'input_hash':row['input_hash'],'attempt_id':row['attempt_id'],
                'runtime_identity':row.get('runtime_identity'),'original_error':row.get('error'),'original_state':row['state'],'created_at':time.time()}
            if record['authority'] != authority: raise TextFenced('Original administrator authority epoch changed')
            record.update(owner_id=owner,job_epoch=epoch)
            row.update(owner_id=owner,lease_epoch=epoch,lease_until=self.jobs.clock()+60,state='RECOVERY_REQUIRED',resource_status='recovery_required',
                       cleanup_required=True,memory_released=False,admin_recovery_pending=key)
            self.persistence._save(db,row)
            db.execute('INSERT INTO text_admin_recoveries VALUES(?,?,?,?) ON CONFLICT(recovery_key) DO UPDATE SET payload=excluded.payload',
                       (record['recovery_id'],key,job_id,canonical(record)))
            # Any abandoned child keeps its outcome, but cannot block safe cleanup.
            db.execute("UPDATE operations SET phase='RECOVERY_REQUIRED' WHERE parent_job_id=? AND phase NOT IN ('RELEASED','DELEGATED_COMPLETED','RECOVERY_REQUIRED','CANCELED')",(job_id,))
            self.persistence._event(db,job_id,'admin-recovery-reserved',{'recovery_id':record['recovery_id'],'epoch':epoch})
            return record

    def _proof(self, record, observation):
        evidence = {'schema':'dgx-stale-retirement-evidence-v1','cluster_id':'dgx-head+dgx-worker','owner':GatewayLease.OWNER,
                    'lease_generation':record['old_generation'],'observed_local_model_state':observation['state'],
                    'remote_attestation_absent':True,'observed_models':observation['models'],
                    'admission_observed_closed':observation.get('admission_observed_closed',False),'observed_runtime_identity_sha256':observation.get('runtime_identity_sha256'),
                    'sampled_at':datetime.now(timezone.utc).isoformat(),'text_recovery':{k:record[k] for k in
                    ('recovery_id','job_id','expected_epoch','job_epoch','old_generation','old_resource_operation_id','new_resource_operation_id','authority','input_hash','attempt_id','runtime_identity')}}
        directory = self.app.config.state_dir/'text-recovery-evidence'/record['recovery_id']
        path = directory/'evidence.json'; _atomic_private_json(path,evidence)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        secret = MutationAdmission._read_secret(self.app.config.authority_credential)
        proof = {'schema':'dgx-stale-retirement-proof-v1','cluster_id':'dgx-head+dgx-worker','owner':GatewayLease.OWNER,
                 'lease_generation':record['old_generation'],'evidence_path':str(path),'evidence_sha256':digest,
                 'verifier_hmac_sha256':hmac.new(secret.encode(),f'{digest}:{GatewayLease.OWNER}:{record["old_generation"]}'.encode(),hashlib.sha256).hexdigest()}
        proof_path = directory/'proof.json'; _atomic_private_json(proof_path,proof)
        self._save(record,'OLD_RETIRE_INTENT',proof_path=str(proof_path),proof_sha256=hashlib.sha256(proof_path.read_bytes()).hexdigest())
        return proof_path

    def _verify_receipt(self, receipt, operation_id, generation):
        path = Path(receipt['release_receipt'])
        if path.is_symlink() or not path.is_file(): raise TextFenced('Release receipt missing')
        data = path.read_bytes(); result = json.loads(data)
        if hashlib.sha256(data).hexdigest() != receipt['release_receipt_sha256'] or result.get('task_id') != operation_id or result.get('lease_generation') != generation:
            raise TextFenced('Retirement receipt does not match original capability')
        return {'path':str(path),'sha256':receipt['release_receipt_sha256'],'generation':generation,'operation_id':operation_id}

    def run(self, job_id, expected_epoch, recovery_key):
        self.jobs.get(job_id)
        if type(expected_epoch) is not int or expected_epoch < 0 or not isinstance(recovery_key,str) or not 8<=len(recovery_key)<=128:
            raise TextJobError('TEXT_INPUT_INVALID','管理员恢复输入无效')
        directory = self.app.config.state_dir/'text-recovery-locks';directory.mkdir(exist_ok=True,mode=0o700)
        fd = os.open(directory/(job_id+'.lock'),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        record = None; heartbeat = None; lease = None
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            authority = self._authority()
            record = self._reserve(job_id,expected_epoch,recovery_key,authority)
            if record['phase']=='DONE': return {'recovery':record,'job':self.jobs.get(job_id)}
            self.jobs._deadlines[job_id]=time.monotonic()+1800
            if record['phase'] in ('RESERVED','OLD_RETIRE_INTENT'):
                original = self._lease(record['old_resource_operation_id']);original.lease_generation=record['old_generation']
                if original.token_path.exists():
                    status=self._status(original)
                    if status.get('state')!='STALE' or status.get('task_id')!=original.operation_id or status.get('lease_generation')!=record['old_generation']:
                        raise TextFenced('Original stale owner changed before retirement')
                    observation=self._inspect(record);proof=self._proof(record,observation);self._guard(record)
                    receipt=original._run('release','--owner',GatewayLease.OWNER,'--token-file',str(original.token_path),'--operation-id',original.operation_id,
                                         '--stale-retirement-proof',str(proof),'--stale-retirement-verifier-key',str(self.app.config.authority_credential))
                else:
                    self._guard(record);receipt=original.release()
                bound=self._verify_receipt(receipt,original.operation_id,record['old_generation'])
                self._save(record,'OLD_RETIRED',old_receipt=bound)
            lease=self._lease(record['new_resource_operation_id'])
            if record['phase'] in ('OLD_RETIRED','NEW_ACQUIRE_INTENT'):
                self._save(record,'NEW_ACQUIRE_INTENT');self._guard(record)
                if lease.token_path.exists():acquired=lease.guard()
                else:acquired=lease.acquire()
                self._guard(record)
                with self.persistence.transaction() as db:
                    row=self.persistence._guard(db,job_id,record['owner_id'],record['job_epoch'],recovery=True)
                    row.update(resource_operation_id=lease.operation_id,lease_generation=acquired['lease_generation'],stop_proof=None,release_receipt=None)
                    self.persistence._save(db,row)
                    record.update(phase='NEW_LEASE_HELD',new_generation=acquired['lease_generation'])
                    db.execute('UPDATE text_admin_recoveries SET payload=? WHERE recovery_id=?',(canonical(record),record['recovery_id']))
            lease.lease_generation=record['new_generation']
            current=self.persistence.get(job_id)
            if record['phase']=='RELEASE_INTENT' and current.get('stop_proof') and not lease.token_path.exists():
                self._guard(record);self.jobs._finish_receipt(current,lease.release())
                self._save(record,'DONE')
                return {'recovery':record,'job':self.jobs.get(job_id)}
            lease.guard();self._guard(record)
            row=self.persistence.get(job_id)
            heartbeat=self.jobs._supervise(job_id,row,lease)
            context=self.jobs._context(job_id,row,lease,recovery=True)
            observation=self._inspect(record)
            if observation['state']=='ACTIVE_EXCLUSIVE':
                if not getattr(self.jobs.fence,'qualified',False):raise TextFenced('External fence not qualified; preserve cleanup owner')
                self._save(record,'CLEANING');self.jobs._child(row,lease,'stop',recovery=True)
            self._save(record,'RELEASE_INTENT')
            self.jobs._release(row,lease,context)
            self._save(record,'DONE')
            return {'recovery':record,'job':self.jobs.get(job_id)}
        except Exception as exc:
            if record and record.get('phase')!='DONE':
                try:
                    self._save(record,error_code='TEXT_ADMIN_RECOVERY_REQUIRED',error=str(exc)[:500] if isinstance(exc,(TextFenced,LeaseError)) else '管理员收尾失败，检查本地证据与持久存储后继续原编号')
                    row=self.persistence.get(job_id)
                    self.jobs._update(row,{'error_code':'TEXT_ADMIN_RECOVERY_REQUIRED','error':record.get('error','管理员收尾未完成，原请求与资源义务保留'),'recovery_action':'authority_admin'},'admin-recovery-failed',recovery=True)
                except (TextFenced,KeyError):pass
            raise
        finally:
            if heartbeat:heartbeat[0].set();heartbeat[1].join(timeout=5)
            if lease:lease.stop_heartbeat()
            os.close(fd)


def build_admin_jobs(config_path, *, apply=True):
    """Read-only assembly: never run service startup recovery from an admin CLI."""
    from .config import load_config
    from .store import OperationStore
    from .operations import OperationController
    from .server import GatewayApplication
    from .adapters.catalog import build_adapters
    from .maintenance import _active_creative_jobs
    from .text_jobs import TextJobs
    config=load_config(config_path);store=OperationStore(config.state_dir/'gateway.sqlite3')
    adapters=build_adapters(config)
    operations=OperationController(store=store,adapters=adapters,lease_factory=lambda op:GatewayLease(lock_cli=config.lock_cli,lock_root=config.lock_root,token_dir=config.token_dir,operation_id=op,purpose='text administrator'))
    app=GatewayApplication(config=config,store=store,operations=operations,snapshot=None,static_dir=config.state_dir,admission=MutationAdmission(config))
    active=_active_creative_jobs(config);app.creative_jobs=SimpleNamespace(active=active[0] if active else None);app.video_jobs=SimpleNamespace(active=None)
    jobs=TextJobs(app,autostart=False,recover_on_startup=False);app.text_jobs=jobs
    if apply:
        from .text_qualification import apply_qualification
        apply_qualification(jobs)
    return jobs


def main():
    parser=argparse.ArgumentParser(description='Explicit administrator recovery of one original expired text lease')
    parser.add_argument('--config',type=Path,required=True);parser.add_argument('--text-job-id',required=True)
    parser.add_argument('--expected-epoch',type=int,required=True);parser.add_argument('--recovery-key',required=True)
    args=parser.parse_args();jobs=build_admin_jobs(args.config)
    try:
        result=TextAdminRecovery(jobs).run(args.text_job_id,args.expected_epoch,args.recovery_key)
        print(json.dumps({'job':result['job'],'recovery_id':result['recovery']['recovery_id'],'phase':result['recovery']['phase']},ensure_ascii=False))
    finally:jobs.app.store.close()

if __name__=='__main__':main()
