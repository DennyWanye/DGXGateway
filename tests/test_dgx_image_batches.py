"""MF05 acceptance: identities, incremental preservation and explicit failed-subset retry."""
import copy
import io
import json
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from DGXGateway.dgx_gateway.image_batches import manifest, update_progress, finish_candidates, attribution
from DGXGateway.dgx_gateway.image_workflows import z_image, qwen_image_edit
from DGXGateway.dgx_gateway.creative_jobs import CreativeJobs
from tests import test_dgx_drama_references as _reference_fixtures
image_bytes = _reference_fixtures.image_bytes
from DGXGateway.dgx_gateway.drama.contracts import DramaError

class BatchIdentityTests(unittest.TestCase):
    def test_two_and_four_candidates_are_bounded_tensor_groups(self):
        for make in (lambda n:z_image('rain',batch_count=n),lambda n:qwen_image_edit('rain',['ref.png'],batch_count=n)):
            for n in (1,2,3,4,8):
                graph=make(n); batch=manifest(graph)
                self.assertEqual([c['seed'] for c in batch['candidates']],list(range(42,42+n)))
                self.assertEqual([g['batchSize'] for g in batch['groups']], [2]*(n//2)+([1] if n%2 else []))
                self.assertEqual(batch['observedParallelism'],0)
                for first, second in zip(batch['groups'],batch['groups'][1:]):
                    self.assertEqual(graph[second['samplerNode']]['inputs']['after_batch'],[first['saveNode'],0])
    def test_user_counts_groups_and_high_retry_indices(self):
        from DGXGateway.dgx_gateway.image_workflows import generate_image
        from DGXGateway.dgx_gateway.drama.gateway_adapter import validate_parameters
        from DGXGateway.dgx_gateway.mcp import TOOLS, _validate
        for model in ('z-image-turbo','flux2-dev','flux2-dev-abliterated','llada-image-turbo'):
            b = manifest(generate_image('rain', model=model, batch_count=25, parallelism=4))
            self.assertEqual([g['batchSize'] for g in b['groups']], [4]*6+[1])
            self.assertEqual([c['index'] for c in b['candidates']], list(range(25)))
        b = manifest(qwen_image_edit('rain',['ref.png'],batch_count=25,parallelism=25))
        self.assertEqual(b['parallelLimit'], 25)
        retry = manifest(z_image('rain',batch_count=2,parallelism=4,candidate_indices=[8,24]))
        self.assertEqual([c['seed'] for c in retry['candidates']], [50,66])
        # More than the old implicit 16-item JSON-schema cap.
        parameters = {'prompt':'rain','batch_count':25,'parallelism':25,'candidate_indices':list(range(25))}
        validate_parameters('generate_image',parameters)
        _validate(next(t['inputSchema'] for t in TOOLS if t['name']=='retry_image_candidates'),
                  {'job_id':'example','candidate_indices':list(range(25)), 'idempotency_key':'retry-example'})
        self.assertEqual(len(manifest(z_image('rain',batch_count=70,parallelism=1))['groups']),70)

    def test_subset_and_uint64_wrap_preserve_original_identity(self):
        batch=manifest(z_image('rain',seed=2**64-2,batch_count=2,candidate_indices=[1,7]))
        self.assertEqual([(c['index'],c['seed']) for c in batch['candidates']],[(1,2**64-1),(7,5)])
    def test_bad_counts_indices_and_parallelism_rejected(self):
        for kwargs in ({'batch_count':True},{'batch_count':0},{'parallelism':0},{'parallelism':False},{'batch_count':2,'candidate_indices':[1,1]}, {'candidate_indices':[-1]}):
            with self.assertRaises(ValueError):z_image('rain',**kwargs)
    def test_observation_can_degrade_and_identity_cannot_change(self):
        batch=manifest(z_image('rain',batch_count=2)); g=batch['groups'][0]
        live={'groups':{g['samplerNode']:{'indices':[0,1],'seeds':[42,43],'state':'SAMPLING','actual_candidate_parallelism':2}}}
        update_progress(batch,live);self.assertEqual(batch['observedParallelism'],2)
        live['groups'][g['samplerNode']]['actual_candidate_parallelism']=0
        update_progress(batch,live);self.assertEqual(batch['observedParallelism'],0)
        live['groups'][g['samplerNode']]['seeds']=[43,44]
        with self.assertRaises(ValueError):update_progress(batch,live)
    def test_sampled_but_unsaved_group_can_be_canceled_without_download_unknown(self):
        batch=manifest(z_image('rain',batch_count=2));group=batch['groups'][0]
        update_progress(batch, {'groups':{group['samplerNode']:{'indices':[0,1],'seeds':[42,43],'state':'SAMPLED','actual_candidate_parallelism':2}}})
        finish_candidates(batch,'CANCELED',True)
        self.assertEqual([c['state'] for c in batch['candidates']],['CANCELED','CANCELED'])

    def test_business_rejects_invalid_group_contract_before_dispatch(self):
        from DGXGateway.dgx_gateway.drama.gateway_adapter import validate_parameters
        for params in ({'prompt':'x','batch_count':2,'candidate_indices':[0]}, {'prompt':'x','batch_count':2,'candidate_indices':[0,0]}, {'prompt':'x','seed':-1}):
            with self.assertRaises(ValueError): validate_parameters('generate_image', params)

    def test_success_and_download_unknown_are_never_regenerated(self):
        batch=manifest(z_image('rain',batch_count=4))
        batch['candidates'][0]['state']='SUCCEEDED';batch['candidates'][1]['state']='COLLECTING'
        finish_candidates(batch,'CANCELED',True)
        self.assertEqual([c['state'] for c in batch['candidates']],['SUCCEEDED','UNKNOWN','CANCELED','CANCELED'])
        with self.assertRaises(ValueError):attribution(batch,'10',-1)

class BatchArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.app=SimpleNamespace(config=SimpleNamespace(state_dir=Path(self.tmp.name),nodes=[SimpleNamespace(address='127.0.0.1')],music3_port=8188,bind='localhost',port=18082),_submit_lock=threading.Lock(),store=SimpleNamespace(active_operation=lambda:None,list_operations=lambda **kw:[]))
        self.jobs=CreativeJobs(self.app);self.app.creative_jobs=self.jobs
        graph=z_image('rain',batch_count=4)
        self.d={'job_id':'b'*32,'idempotency_key':'batch-test','state':'RUNNING','workflow':graph,'image_batch':manifest(graph),'memory_released':False,'outputs':{}}
        self.jobs.active=self.d['job_id'];self.jobs.save(self.d)
    def outputs(self,group):
        g=self.d['image_batch']['groups'][group]
        return {g['saveNode']:{'images':[{'filename':f"{i}.png",'subfolder':'','type':'output'} for i in g['indices']]}}
    def test_partial_polling_is_incremental_and_stable_across_restart(self):
        self.jobs.http=SimpleNamespace(open=Mock(side_effect=lambda *a,**k:io.BytesIO(image_bytes())))
        self.d['outputs'].update(self.outputs(0));self.jobs.collect(self.d,False)
        original=copy.deepcopy(self.d['artifacts']);self.assertEqual(len(original),2)
        self.jobs.collect(self.d,False);self.assertEqual(self.jobs.http.open.call_count,2)
        restored=CreativeJobs(self.app); row=json.loads((restored.root/(self.d['job_id']+'.json')).read_text())
        restored.http=self.jobs.http;row['outputs'].update(self.outputs(1));restored.collect(row)
        self.assertEqual(row['artifacts'][:2],original);self.assertEqual(len(row['artifacts']),4)
        self.assertEqual([a['seed'] for a in row['artifacts']],[42,43,44,45])
    def test_failed_download_keeps_siblings_and_retries_only_download(self):
        self.d['outputs'].update(self.outputs(0));self.jobs.http=SimpleNamespace(open=Mock(side_effect=[io.BytesIO(image_bytes()),urllib.error.URLError('offline')]))
        self.jobs.collect(self.d,False)
        self.assertEqual(len(self.d['artifacts']),1);self.assertEqual(self.d['image_batch']['candidates'][1]['state'],'COLLECTING')
        self.assertFalse(list((Path(self.tmp.name)/'creative-artifacts').rglob('*.part')))
        self.jobs.http.open=Mock(return_value=io.BytesIO(image_bytes("blue")));self.jobs.collect(self.d,False)
        self.jobs.http.open.assert_called_once();self.assertEqual(len(self.d['artifacts']),2)
    def test_execution_error_collects_success_before_cleanup(self):
        self.d['outputs']=self.outputs(0);self.jobs.http=SimpleNamespace(open=Mock(side_effect=lambda *a,**k:io.BytesIO(image_bytes())))
        self.jobs.switch=Mock();self.jobs.batch_progress=Mock()
        self.jobs.request=Mock(side_effect=[{}, {'prompt_id':'p'}, {'p':{'status':{'status_str':'error'},'outputs':self.outputs(0)}},{}])
        self.jobs.run(self.d)
        self.assertEqual(len(self.d['artifacts']),2);self.assertTrue(self.d['memory_released'])
        self.assertEqual([c['state'] for c in self.d['image_batch']['candidates']],['SUCCEEDED','SUCCEEDED','FAILED','FAILED'])
    def test_ambiguous_post_marks_unknown_and_keeps_ownership(self):
        self.jobs.switch=Mock();self.jobs.request=Mock(side_effect=[{},TimeoutError('lost')]);self.jobs.run(self.d)
        self.assertTrue(self.d['cleanup_required']);self.assertEqual(self.jobs.request.call_count,2)
        self.assertEqual({c['state'] for c in self.d['image_batch']['candidates']},{'UNKNOWN'})

class FailedSubsetTests(_reference_fixtures.DramaReferenceTests):
    # Inherit fixtures only, not the old test suite in discovery.
    def parent(self):
        job=self.submit(parameters={'prompt':'same rain','batch_count':4,'parallelism':2});self.drain()
        batch=manifest(z_image('same rain',batch_count=4))
        for c in batch['candidates']:c['state']='FAILED'
        batch['candidates'][0].update(state='SUCCEEDED',sourceKey='0')
        batch['candidates'][1]['state']='UNKNOWN'
        record={'job_id':self.service.reference_job(job['id'])['job']['gatewayJobId'],'state':'FAILED','memory_released':True,'image_batch':batch,'outputs':[]}
        self.service._accept_reference_record(job['id'],record)
        return job['id'],record
    def test_retry_subset_freezes_indices_and_is_idempotent(self):
        jid,_=self.parent()
        with self.hold_submission_executor():
            body={'candidateIndices':[3],'requestKey':'retry-one'}
            child=self.service.retry_reference_job(jid,body)['job']
            repeat=self.service.retry_reference_job(jid,body)['job']
            self.assertEqual(child['id'],repeat['id']);self.assertEqual(child['retryOf'],jid)
            self.assertEqual(child['parameters']['candidate_indices'],[3]);self.assertEqual(child['parameters']['batch_count'],1)
            self.assertEqual(child['parameters']['prompt'],'same rain')
            with self.assertRaises(DramaError):self.service.retry_reference_job(jid,{**body,'requestKey':'duplicate-intent'})
            sibling=self.service.retry_reference_job(jid,{'candidateIndices':[2],'requestKey':'retry-two'})['job']
            self.assertNotEqual(child['id'],sibling['id'])
    def test_retry_rejects_success_unknown_and_unreleased(self):
        jid,record=self.parent()
        for index in (0,1):
            with self.assertRaises(DramaError):self.service.retry_reference_job(jid,{'candidateIndices':[index],'requestKey':f'bad-{index}'})
        record['memory_released']=False;self.service._accept_reference_record(jid,record)
        with self.assertRaises(DramaError):self.service.retry_reference_job(jid,{'candidateIndices':[2],'requestKey':'busy'})

# Avoid recounting inherited baseline scenarios as new MF05 coverage.
for _name in list(_reference_fixtures.DramaReferenceTests.__dict__):
    if _name.startswith('test_') and _name not in FailedSubsetTests.__dict__:
        setattr(FailedSubsetTests,_name,None)
