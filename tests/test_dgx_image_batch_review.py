"""MF05 independent recovery/retry oracles; isolated files/SQLite, no models."""
import copy
from contextlib import nullcontext
import io
import json
from pathlib import Path
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from PIL import Image
from DGXGateway.dgx_gateway.creative_jobs import CreativeJobs
from DGXGateway.dgx_gateway.image_batches import manifest, finish_candidates
from DGXGateway.dgx_gateway.drama.service import DramaService
from DGXGateway.dgx_gateway.drama.contracts import DramaError
from tests.test_dgx_drama_references import FakeGateway


def workflow():
    return {'8': {'class_type':'DGXSeedBatchKSampler','inputs':{'seed':42,'candidate_indices':'[0,1]'}},
            '10': {'class_type':'DGXBatchSaveImage','inputs':{'batch_info':['8',1]}}}


def png(color):
    out=io.BytesIO();Image.new('RGB',(16,16),color).save(out,'PNG');return out.getvalue()


class IncrementalArchiveReview(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.app=SimpleNamespace(config=SimpleNamespace(state_dir=Path(self.temp.name),nodes=[SimpleNamespace(address='127.0.0.1')],music3_port=8188,bind='127.0.0.1',port=18082),
            _submit_lock=threading.Lock(),store=SimpleNamespace(active_operation=lambda:None,list_operations=lambda **kw:[]),
            operations=SimpleNamespace(adapters={'comfy':SimpleNamespace(observe=lambda:SimpleNamespace(state='STOPPED'))}))
        self.jobs=CreativeJobs(self.app);self.app.creative_jobs=self.jobs
        self.row={'job_id':'a'*32,'state':'RUNNING','workflow':workflow(),'image_batch':manifest(workflow()),'memory_released':False,
            'outputs':{'10':{'images':[{'filename':'first.png','type':'output'},{'filename':'second.png','type':'output'}]}}}
        self.jobs.active=self.row['job_id'];self.jobs.save(self.row)

    def transport(self, fail_second=False):
        def fetch(url, **kwargs):
            name=parse_qs(urlsplit(url).query)['filename'][0]
            if name=='second.png' and fail_second:raise URLError('download interrupted')
            return io.BytesIO(png('red' if name=='first.png' else 'blue'))
        self.jobs.http.open=Mock(side_effect=fetch)
        return self.jobs.http.open

    def test_incremental_collection_retries_only_missing_output_with_stable_identity(self):
        download=self.transport(fail_second=True)
        self.jobs.collect(self.row,require_complete=False)
        first=self.row['artifacts'][0].copy()
        self.assertEqual(first['candidate_index'],0);self.assertEqual(first['seed'],42)
        self.assertEqual(self.row['image_batch']['candidates'][1]['state'],'COLLECTING')
        download=self.transport()
        self.jobs.collect(self.row)
        self.assertEqual(download.call_count,1)
        self.assertEqual(self.row['artifacts'][0],first)
        self.assertEqual({a['candidate_index'] for a in self.row['artifacts']},{0,1})
        self.jobs.collect(self.row)
        self.assertEqual(download.call_count,1,'already verified successful artifact must not be redownloaded')
        self.assertEqual(len(self.row['artifacts']),2)

    def test_download_unknown_never_becomes_retryable_by_repeated_finalization(self):
        self.transport(fail_second=True);self.jobs.collect(self.row,require_complete=False)
        finish_candidates(self.row['image_batch'],'FAILED',True)
        self.assertEqual(self.row['image_batch']['candidates'][1]['state'],'UNKNOWN')
        finish_candidates(self.row['image_batch'],'FAILED',True)
        self.assertEqual(self.row['image_batch']['candidates'][1]['state'],'UNKNOWN',
            'a second reconcile must not turn a saved-but-unavailable output into a sampling failure')

    def test_nonempty_truncated_png_is_not_a_successful_candidate(self):
        self.jobs.http.open=Mock(side_effect=lambda url,**kw:io.BytesIO(png('red')[:33] if 'second.png' in url else png('red')))
        self.jobs.collect(self.row,require_complete=False)
        finish_candidates(self.row['image_batch'],'FAILED',True)
        self.assertEqual(len(self.row['artifacts']),1,'incomplete PNG must not be advertised as a successful artifact')
        self.assertEqual(self.row['image_batch']['candidates'][0]['state'],'SUCCEEDED')
        self.assertEqual(self.row['image_batch']['candidates'][1]['state'],'UNKNOWN')
        self.assertIn('recovery',self.row['image_batch']['candidates'][1].get('error',''))

    def test_progress_refuses_wrong_prompt_or_seed_before_archiving(self):
        self.row['prompt_id']='owned'
        self.jobs.request=Mock(return_value={'prompt_id':'other','groups':{},'outputs':{}})
        with patch.object(self.jobs,'collect') as collect:
            with self.assertRaisesRegex(ValueError,'prompt identity'):self.jobs.batch_progress(self.row)
            collect.assert_not_called()
        self.jobs.request=Mock(return_value={'prompt_id':'owned','groups':{'8':{'indices':[0,1],'seeds':[100,101],'state':'SAVED'}},'outputs':{}})
        with self.assertRaisesRegex(ValueError,'candidate identity'):self.jobs.batch_progress(self.row)

    def test_released_download_failure_has_explicit_recovery_without_another_prompt(self):
        self.transport(fail_second=True);self.jobs.collect(self.row,require_complete=False)
        finish_candidates(self.row['image_batch'],'FAILED',True)
        self.row.update(state='FAILED',memory_released=True,cleanup_required=False,prompt_id='owned',comfy_status={'status_str':'success'})
        self.jobs.save(self.row);self.jobs.active=None
        self.transport();self.jobs.switch=Mock()
        self.jobs.request=Mock(return_value={})
        with patch('DGXGateway.dgx_gateway.creative_jobs.threading.Thread') as thread, patch('DGXGateway.dgx_gateway.creative_jobs.creative_admission', return_value=nullcontext()):
            self.jobs.reconcile(self.row['job_id'])
            self.assertEqual(thread.call_count,1)
            call=thread.call_args
        call.kwargs['target'](*call.kwargs.get('args',()))
        result=self.jobs.get(self.row['job_id'])
        self.assertEqual(result['state'],'SUCCEEDED',result)
        self.assertTrue(result['memory_released']);self.assertFalse(result.get('cleanup_required'))
        self.assertEqual(len(result['artifacts']),2)
        self.assertTrue(all(c['state']=='SUCCEEDED' for c in result['image_batch']['candidates']))
        self.assertFalse(any(c.args[0]=='/prompt' for c in self.jobs.request.call_args_list),'archive recovery must never resample')

    def test_artifact_recovery_preserves_other_groups_definite_sampling_failure(self):
        self.transport(fail_second=True);self.jobs.collect(self.row,require_complete=False)
        finish_candidates(self.row['image_batch'],'FAILED',True)
        self.row['image_batch']['candidates'].append({'index':2,'seed':44,'state':'FAILED'})
        self.row['image_batch']['candidateCount']=3
        self.row['image_batch']['groups'].append({'samplerNode':'11','saveNode':'13','indices':[2],'seeds':[44],'batchSize':1,'state':'FAILED'})
        self.row.update(state='RECONCILING',cleanup_required=True)
        self.jobs.switch=Mock();self.jobs.request=Mock(return_value={});self.transport()
        self.jobs._recover_downloads(self.row)
        self.assertEqual(self.row['image_batch']['candidates'][1]['state'],'SUCCEEDED')
        self.assertEqual(self.row['image_batch']['candidates'][2]['state'],'FAILED',
            'recovering one downloaded file must not make another proven sampling failure unknown')

    def test_reconcile_rejects_path_shaped_job_id_before_reading_any_file(self):
        with patch.object(Path,'read_text',side_effect=AssertionError('read before validating identity')):
            with self.assertRaisesRegex(ValueError,'job_id'):
                self.jobs.reconcile('../unrelated')


class FrozenRetryReview(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.service=DramaService(Path(self.temp.name),Path(self.temp.name));self.addCleanup(self.service.close)
        self.service._reference_stop.set();self.service._reference_poller.join()
        self.gateway=FakeGateway();self.service._reference_adapter=self.gateway
        scheduled=patch.object(self.service,'_schedule_reference');scheduled.start();self.addCleanup(scheduled.stop)
        self.pid=self.service.create_project({'title':'isolated retry review','requestKey':'project'})['project']['id']
        entity=self.service.create_entity(self.pid,{'kind':'character','name':'actor','requestKey':'actor'})['entity']
        self.parent=self.service.submit_reference_job('entity',entity['currentRevisionId'],{'tool':'generate_image','parameters':{'prompt':'frozen original','seed':42,'batch_count':4,'parallelism':2},'requestKey':'initial'})['job']
        record={'job_id':'f'*32,'state':'FAILED','memory_released':True,'outputs':[],
            'image_batch':{'version':1,'candidateCount':4,'parallelLimit':2,'candidates':[{'index':i,'seed':42+i,'state':state} for i,state in enumerate(['SUCCEEDED','FAILED','UNKNOWN','CANCELED'])]}}
        self.service._accept_reference_record(self.parent['id'],record)

    def retry(self, indices, key):
        return self.service.retry_reference_job(self.parent['id'],{'candidateIndices':indices,'requestKey':key})['job']

    def test_same_key_lost_reply_restores_child_and_original_seed_indices(self):
        one=self.retry([3,1],'same-key');two=self.retry([3,1],'same-key')
        self.assertEqual(one['id'],two['id'])
        self.assertEqual(one['retryOf'],self.parent['id'])
        self.assertEqual(one['parameters'],{'prompt':'frozen original','seed':42,'batch_count':2,'parallelism':2,'candidate_indices':[1,3]})
        self.assertEqual(len(self.service.reference_jobs(self.pid)['jobs']),2)
        self.assertEqual(self.gateway.submissions,[])

    def test_success_unknown_and_unreleased_candidates_cannot_retry(self):
        for indices in [[0],[2]]:
            with self.subTest(indices=indices),self.assertRaises(DramaError) as error:self.retry(indices,str(indices))
            self.assertEqual(error.exception.code,'retry_not_safe')
        with self.service.store.connect(write=True) as db:
            db.execute("UPDATE dr_reference_jobs SET input_snapshot=json_set(input_snapshot,'$.gatewayStatus.memoryReleased',json('false')) WHERE id=?",(self.parent['id'],))
        with self.assertRaises(DramaError) as error:self.retry([1],'not-released')
        self.assertEqual(error.exception.code,'retry_not_safe')

    def test_concurrent_different_keys_cannot_create_overlapping_children(self):
        def attempt(key):
            try:return self.retry([1],key)
            except DramaError as error:return error
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,['one','two']))
        self.assertEqual(sum(isinstance(r,dict) for r in results),1)
        self.assertEqual([r.code for r in results if isinstance(r,DramaError)],['retry_already_exists'])
        remaining=self.retry([3],'remaining')
        self.assertEqual(remaining['parameters']['candidate_indices'],[3])
        self.assertEqual(len(self.service.reference_jobs(self.pid)['jobs']),3)
