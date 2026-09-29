"""Admin recovery: real isolated leases/SQLite, fake runtime, never SSH."""
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from tests import test_dgx_gateway_text_jobs as fixture
from DGXGateway.dgx_gateway.text_admin_recovery import TextAdminRecovery
from DGXGateway.dgx_gateway.text_jobs import SubmissionUnknown
from DGXGateway.dgx_gateway.text_store import TextFenced
from DGXGateway.dgx_gateway.authority import LeaseError


class AdminTests(unittest.TestCase):
    setUp=fixture.TextJobTests.setUp;lease=fixture.TextJobTests.lease;submit=fixture.TextJobTests.submit;run_job=fixture.TextJobTests.run_job
    def prepare(self):
        self.app.config.authority_credential=self.root/'authority-key'
        self.app.config.authority_credential.write_text('local-fixture-'+('x'*40));self.app.config.authority_credential.chmod(0o600)
        self.app.admission.assert_allowed=lambda:{'allowed':True,'authority_id':'test-local','epoch':1}
        self.transport.error=SubmissionUnknown('unknown');row=self.run_job()
        self.expire()
        self.observer=SimpleNamespace(inspect=lambda guard:self.observation(guard))
        self.admin=TextAdminRecovery(self.jobs,observer=self.observer)
        return row
    def expire(self):
        path=self.app.config.lock_root/'owner.json';data=json.loads(path.read_text());data['heartbeat_at']='2000-01-01T00:00:00Z';path.write_text(json.dumps(data))
    def observation(self,guard):
        guard();return {'state':'ALL_STOPPED' if self.adapter.state=='STOPPED' else 'ACTIVE_EXCLUSIVE',
                        'runtime_identity':{'runtime_id':'qa-only-runtime'},'models':[{'model_id':self.adapter.model_id,'state':self.adapter.state}],
                        'remote_attestation_absent':True}
    def recover(self,row,key='admin-recovery-key'):
        return self.admin.run(row['job_id'],row['lease_epoch'],key)
    def test_expired_unknown_cleanup_keeps_original_attempt_and_old_new_receipts(self):
        row=self.prepare();result=self.recover(row);job=result['job'];record=result['recovery']
        self.assertEqual(job['dispatch_state'],'SUBMISSION_UNKNOWN');self.assertEqual(job['attempt_id'],row['attempt_id'])
        self.assertTrue(job['memory_released']);self.assertEqual(record['phase'],'DONE')
        self.assertNotEqual(record['old_generation'],record['new_generation'])
        self.assertEqual(job['resource_operation_id'],record['new_resource_operation_id'])
        self.assertEqual(self.transport.calls,1);self.assertEqual(self.adapter.calls,['start','stop'])
        self.assertEqual(self.recover(row)['recovery']['recovery_id'],record['recovery_id'])
    def test_missing_original_identity_refuses_active_runtime(self):
        row=self.prepare();stored=self.jobs.persistence.get(row['job_id']);stored['runtime_identity']=None
        with self.jobs.persistence.transaction() as db:self.jobs.persistence._save(db,stored)
        with self.assertRaisesRegex(TextFenced,'identity'):self.recover(row)
        self.assertEqual(self.adapter.calls,['start']);self.assertEqual(self.transport.calls,1)
        self.assertTrue(self.app.config.lock_root.exists())
    def test_changed_identity_refuses_stop_and_lease_retirement(self):
        row=self.prepare();self.observer.inspect=lambda guard:dict(self.observation(guard),runtime_identity={'runtime_id':'different'})
        with self.assertRaises(TextFenced):self.recover(row)
        self.assertEqual(self.adapter.calls,['start']);self.assertTrue(self.app.config.lock_root.exists())
    def test_all_stopped_branch_needs_no_fence_or_inference(self):
        row=self.prepare();self.adapter.state='STOPPED';self.fence.qualified=False
        result=self.recover(row);self.assertTrue(result['job']['memory_released']);self.assertEqual(self.transport.calls,1);self.assertEqual(self.fence.calls,0)
    def test_gap_competitor_is_not_released_or_stopped(self):
        row=self.prepare();original=self.admin._save;competitor=self.lease('admin-gap-competitor')
        def save(record,phase=None,**changes):
            original(record,phase,**changes)
            if phase=='OLD_RETIRED':competitor.acquire()
        with patch.object(self.admin,'_save',side_effect=save),self.assertRaises(LeaseError):self.recover(row)
        competitor.guard();self.assertEqual(self.adapter.calls,['start']);self.assertFalse(self.jobs.get(row['job_id'])['memory_released']);competitor.release()
    def test_new_acquire_before_database_commit_resumes_original_intent(self):
        row=self.prepare();original=self.admin._guard;failed=[False]
        def fail(record):
            new=self.admin._lease(record['new_resource_operation_id'])
            if record['phase']=='NEW_ACQUIRE_INTENT' and new.token_path.exists() and not failed[0]:
                failed[0]=True;raise OSError('fixture lost DB acknowledgement')
            return original(record)
        with patch.object(self.admin,'_guard',side_effect=fail),self.assertRaises(OSError):self.recover(row)
        pending=self.admin._get('admin-recovery-key');new=self.admin._lease(pending['new_resource_operation_id']);generation=new.guard()['lease_generation']
        result=self.recover(row);self.assertEqual(result['recovery']['new_generation'],generation);self.assertEqual(self.transport.calls,1)
    def test_release_receipt_before_database_completion_is_reconciled(self):
        row=self.prepare();original=self.jobs._finish_receipt
        with patch.object(self.jobs,'_finish_receipt',side_effect=OSError('lost SQLite completion')),self.assertRaises(OSError):self.recover(row)
        result=self.recover(row);self.assertTrue(result['job']['memory_released']);self.assertEqual(self.transport.calls,1)
    def test_authority_epoch_change_refuses_admin_takeover(self):
        row=self.prepare();original=self.admin._proof
        def changed(record,observation):
            result=original(record,observation);self.app.admission.assert_allowed=lambda:{'allowed':True,'authority_id':'test-local','epoch':2};return result
        with patch.object(self.admin,'_proof',side_effect=changed),self.assertRaises(TextFenced):self.recover(row)
        self.assertEqual(self.adapter.calls,['start']);self.assertTrue(self.app.config.lock_root.exists())
    def test_second_recovery_lease_expiry_uses_new_exact_journal(self):
        row=self.prepare();self.fence.qualified=False
        with self.assertRaises(TextFenced):self.recover(row)
        first=self.admin._get('admin-recovery-key');self.expire();self.fence.qualified=True
        current=self.jobs.get(row['job_id']);result=self.recover(current,key='second-admin-recovery')
        self.assertEqual(result['recovery']['old_generation'],first['new_generation'])
        self.assertEqual(result['recovery']['old_resource_operation_id'],first['new_resource_operation_id'])
        self.assertEqual(self.transport.calls,1);self.assertTrue(result['job']['memory_released'])

    def test_admission_fenced_partial_requires_full_original_identity_and_receipt(self):
        from DGXGateway.dgx_gateway.text_admin_recovery import RecoveryObservation
        row=self.prepare();self.adapter.state='PARTIAL'
        identity={'nodes':{'head':{'bootId':'h','containers':[{'id':'a'*64}]},'worker':{'bootId':'w','containers':[{'id':'b'*64}]}},'gateway_container_id':'c'*64}
        stored=self.jobs.persistence.get(row['job_id']);stored['runtime_identity']=identity
        with self.jobs.persistence.transaction() as db:self.jobs.persistence._save(db,stored)
        self.app.config.nodes=[SimpleNamespace(role='head',hostname='dgx-head',user='operator'),SimpleNamespace(role='worker',hostname='dgx-worker',user='operator')]
        self.adapter._text_runtime_identity=lambda **kw:dict(identity,gateway_container_id=None)
        self.admin.observer=RecoveryObservation(self.app)
        def ssh(node,*args,**kwargs):return SimpleNamespace(returncode=0,stdout=node.hostname+'\noperator\n')
        # Strong adapter rejects incomplete worker inventory before proof signing.
        with patch('DGXGateway.dgx_gateway.text_admin_recovery.FixedSSH.run',side_effect=ssh):
            with patch.object(self.adapter,'_text_runtime_identity',side_effect=RuntimeError('missing worker')):
                with self.assertRaisesRegex(RuntimeError,'missing worker'):self.recover(row)
            with patch.object(self.adapter,'_text_runtime_identity',return_value={'nodes':{},'gateway_container_id':'d'*64}):
                with self.assertRaises(TextFenced):self.recover(row)
            with patch.object(self.adapter,'_text_runtime_identity',return_value=identity):
                observed=self.admin.observer.inspect(lambda:None)
                self.assertFalse(observed['admission_observed_closed'])
            # Complete identity alone never substitutes for the helper receipt.
            with patch.object(self.fence,'drain',side_effect=TextFenced('missing fence receipt')):
                with self.assertRaises(TextFenced):self.recover(row)
            self.assertEqual(self.adapter.calls,['start']);self.assertEqual(self.transport.calls,1)
            result=self.recover(row)
            self.assertTrue(result['job']['memory_released']);self.assertEqual(self.adapter.calls,['start','stop'])
            proof_path=Path(result['recovery']['proof_path']);proof=json.loads(proof_path.read_text());evidence=json.loads(Path(proof['evidence_path']).read_text())
            self.assertTrue(evidence['admission_observed_closed']);self.assertIsNotNone(evidence['observed_runtime_identity_sha256'])

    def test_stale_new_acquire_without_db_ack_can_be_referenced_by_next_journal(self):
        row=self.prepare();original=self.admin._guard;failed=[False]
        def fail(record):
            if record['phase']=='NEW_ACQUIRE_INTENT' and self.admin._lease(record['new_resource_operation_id']).token_path.exists() and not failed[0]:
                failed[0]=True;raise OSError('crash before adoption')
            return original(record)
        with patch.object(self.admin,'_guard',side_effect=fail),self.assertRaises(OSError):self.recover(row)
        first=self.admin._get('admin-recovery-key');new=self.admin._lease(first['new_resource_operation_id']);new_generation=new.guard()['lease_generation']
        self.expire();current=self.jobs.get(row['job_id'])
        result=self.recover(current,key='next-after-stale-intent')
        self.assertEqual(result['recovery']['old_generation'],new_generation)
        self.assertEqual(result['recovery']['old_resource_operation_id'],first['new_resource_operation_id'])
        self.assertTrue(self.admin._get('admin-recovery-key')['acquire_ack_recovered'])
        self.assertEqual(self.transport.calls,1);self.assertTrue(result['job']['memory_released'])

    def test_own_abandoned_loading_child_does_not_block_admin_recovery(self):
        row=self.prepare();child=self.store.list_operations()[0]
        self.store.set_phase(child.operation_id,'LOADING')
        result=self.recover(row)
        self.assertTrue(result['job']['memory_released']);self.assertEqual(self.transport.calls,1)
        self.assertEqual(self.store.get_operation(child.operation_id).phase,'RECOVERY_REQUIRED')


class QualificationTests(unittest.TestCase):
    setUp=fixture.TextJobTests.setUp;lease=fixture.TextJobTests.lease
    def config(self):
        from tests.test_dgx_gateway_text_jobs import REPO
        c=self.app.config;c.repo_root=REPO;c.authority_id='test-local';c.authority_credential=self.root/'key'
        c.authority_credential.write_text('local-qualification-'+('x'*40));c.authority_credential.chmod(0o600)
        c.text_enabled=False;c.text_qualification_file=self.root/'qualification.json';return c
    def record(self,c,**changes):
        from DGXGateway.dgx_gateway.text_qualification import source_hashes,REMOTE_BY_ROLE,_signed
        from DGXGateway.dgx_gateway.text_jobs import MODEL,CONTRACT,KINDS
        sources=source_hashes(c);runtime={r:{p:sources[s] for p,s in REMOTE_BY_ROLE[r].items()} for r in ('head','worker')}
        record={'schema':'dgx-text-qualification-v1','qualification_id':'fixture','model':MODEL,'contract':CONTRACT,'simulation':False,
                'authority_id':c.authority_id,'gates':{'http':'PASS','fence':'PASS','release':'PASS','external_concurrent':'PASS'},'sources':sources,'runtime_helpers':runtime,
                'jobs':[]}
        for i,k in enumerate(KINDS):
            job_id=f'{i:032x}';output='{}';output_sha=hashlib.sha256(output.encode()).hexdigest();fence={'admission_closed':True};release={'sha256':'f'*64}
            row={'job_id':job_id,'state':'SUCCEEDED','resource_status':'released','memory_released':True,'output_status':'valid_draft','task_kind':k,
                 'input_hash':'i'*64,'output_sha256':output_sha,'output_text':output,'fence_proof':fence,'release_receipt':release}
            if i==0:
                from tests.test_dgx_text_concurrent_qualification import complete_fixture_proof
                from DGXGateway.dgx_gateway.text_concurrent_qualification import digest
                row['external_concurrent_proof']=complete_fixture_proof(row)
                record['external_concurrent']={'job_id':job_id,'sha256':digest(row['external_concurrent_proof'])}
            self.store._db.execute('INSERT OR REPLACE INTO text_jobs VALUES(?,?,?,?)',(job_id,'fixture-'+k,'i'*64,json.dumps(row)))
            if i==0:self.store._db.execute('INSERT OR REPLACE INTO text_attempts VALUES(?,?,?,?,?)',(row['attempt_id'],job_id,row['lease_epoch'],'RESULT_ARCHIVED',json.dumps({'output_sha256':output_sha})))
            from DGXGateway.dgx_gateway.text_jobs import canonical
            record['jobs'].append({'job_id':job_id,'task_kind':k,'input_hash':'i'*64,'output_sha256':output_sha,
                                   'fence_proof_sha256':hashlib.sha256(canonical(fence).encode()).hexdigest(),'release_receipt_sha256':'f'*64})
        record.update(changes);record['hmac_sha256']=_signed(record,c)
        c.text_qualification_file.write_text(json.dumps(record));c.text_qualification_file.chmod(0o600)
        c.text_qualification_sha256=hashlib.sha256(c.text_qualification_file.read_bytes()).hexdigest();return record
    def test_default_qualification_does_not_enable_or_query_network(self):
        from DGXGateway.dgx_gateway.text_qualification import apply_qualification
        c=self.config();c.text_qualification_file=None;apply_qualification(self.jobs);self.assertFalse(self.jobs.capabilities()['generation_qualified'])
    def test_simulation_runner_rejected_before_any_model_call(self):
        from DGXGateway.dgx_gateway.text_qualification import qualify
        self.config()
        with self.assertRaisesRegex(RuntimeError,'Simulation'):qualify(self.jobs,self.root/'result.json')
        self.assertEqual(self.transport.calls,0);self.assertEqual(self.adapter.calls,[])
    def test_signed_record_source_and_runtime_hashes_validate(self):
        from DGXGateway.dgx_gateway.text_qualification import load_qualification
        c=self.config();self.record(c);self.assertEqual(load_qualification(c)['qualification_id'],'fixture')
    def test_simulation_or_version_drift_never_qualifies(self):
        from DGXGateway.dgx_gateway.text_qualification import load_qualification
        c=self.config()
        for changes in ({'simulation':True},{'sources':{}},{'runtime_helpers':{}}):
            self.record(c,**changes)
            with self.subTest(changes=changes),self.assertRaises(RuntimeError):load_qualification(c)
    def test_artifact_tamper_rejected(self):
        from DGXGateway.dgx_gateway.text_qualification import load_qualification
        c=self.config();self.record(c);c.text_qualification_file.write_text('{}')
        with self.assertRaises(RuntimeError):load_qualification(c)

    def test_qualification_rejects_missing_or_changed_archived_job(self):
        from DGXGateway.dgx_gateway.text_qualification import load_qualification
        c=self.config();self.record(c);self.store._db.execute('DELETE FROM text_attempts WHERE job_id=?',('0'*32,));self.store._db.execute('DELETE FROM text_jobs WHERE job_id=?',('0'*32,))
        with self.assertRaisesRegex(RuntimeError,'missing'):load_qualification(c)

    def test_config_rejects_nonboolean_enable_and_missing_qualification(self):
        from DGXGateway.dgx_gateway.config import load_config
        source=(fixture.REPO/'DGXGateway/config/gateway.example.toml').read_text()
        path=self.root/'config.toml'
        for text in ('[text]\nenabled="true"\n','[text]\nenabled=true\n'):
            path.write_text(source+'\n'+text)
            with self.assertRaises(ValueError):load_config(path)
        path.write_text(source+'\n[text]\nenabled=false\n');self.assertFalse(load_config(path).text_enabled)

    def test_generation_disable_keeps_qualified_cleanup_available(self):
        from DGXGateway.dgx_gateway.text_qualification import apply_qualification
        from DGXGateway.dgx_gateway.text_jobs import FixedTextHTTP
        c=self.config();self.record(c);c.nodes=[SimpleNamespace(role='head',address='192.0.2.1')];c.deepseek_gateway_secret=c.authority_credential
        self.adapter.config=c;self.jobs.transport=FixedTextHTTP(c)
        apply_qualification(self.jobs)
        self.assertFalse(self.jobs.capabilities()['generation_qualified']);self.assertTrue(self.jobs.fence.qualified)
        c.text_enabled=True;apply_qualification(self.jobs);self.assertTrue(self.jobs.capabilities()['generation_qualified'])

    def test_bad_qualification_disables_generation_without_breaking_application(self):
        from DGXGateway.dgx_gateway.text_qualification import apply_qualification
        c=self.config();c.text_enabled=True;c.text_qualification_sha256='0'*64
        self.assertFalse(apply_qualification(self.jobs))
        self.assertFalse(self.jobs.capabilities()['generation_qualified'])
        self.assertIn('资格不可用',self.jobs.capabilities()['reason'])
        self.assertEqual(self.jobs.list(),[]);self.assertFalse(self.adapter.text_fence_qualified)

    def test_role_specific_runtime_hashes_and_business_exclusion(self):
        from DGXGateway.dgx_gateway.text_qualification import source_hashes,REMOTE_BY_ROLE,runtime_hashes,validate_runtime
        c=self.config();sources=source_hashes(c)
        self.assertFalse(any('/drama/' in name for name in sources))
        self.assertEqual(len(REMOTE_BY_ROLE['head']),2);self.assertEqual(len(REMOTE_BY_ROLE['worker']),1)
        c.nodes=[SimpleNamespace(role=r,hostname='spark-'+r,user='operator') for r in ('head','worker')]
        def probe(node,args,**kwargs):
            return SimpleNamespace(returncode=0,stdout=node.hostname+'\noperator\n'+'\n'.join(sources[s]+'  '+p for p,s in REMOTE_BY_ROLE[node.role].items())+'\n')
        with patch('DGXGateway.dgx_gateway.text_qualification.FixedSSH.run',side_effect=probe):actual=runtime_hashes(c)
        validate_runtime(c,sources,actual)

    def test_qa_inputs_have_four_bounded_url_free_kinds(self):
        from DGXGateway.dgx_gateway.text_qualification import validate_qa_inputs
        from DGXGateway.dgx_gateway.text_jobs import KINDS
        inputs={k:{'source':'fixture'} for k in KINDS};self.assertEqual(validate_qa_inputs(inputs),inputs)
        for invalid in ({},dict(inputs,draft_script={'url':'https://example.invalid'}),dict(inputs,draft_script={'text':'x'*32001})):
            with self.assertRaises(RuntimeError):validate_qa_inputs(invalid)

    def test_external_concurrency_not_run_cannot_enable_production(self):
        from DGXGateway.dgx_gateway.text_qualification import load_qualification
        c=self.config();self.record(c,gates={'http':'PASS','fence':'PASS','release':'PASS','external_concurrent':'NOT_RUN'})
        with self.assertRaises(RuntimeError):load_qualification(c)
