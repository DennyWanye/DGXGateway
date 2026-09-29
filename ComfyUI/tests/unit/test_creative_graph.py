import importlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'custom_nodes'))


class GraphTransportTests(unittest.TestCase):
    def setUp(self):
        self.graph = importlib.import_module('dgx_remote_h3.creative_graph')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        class Interrupted(Exception): pass
        self.management = SimpleNamespace(throw_exception_if_processing_interrupted=Mock(), InterruptProcessingException=Interrupted)
        import comfy
        self.patchers = [
            patch.dict(sys.modules, {'folder_paths': SimpleNamespace(get_output_directory=lambda:self.tmp.name), 'server': SimpleNamespace(PromptServer=SimpleNamespace(instance=SimpleNamespace(send_sync=Mock()))), 'comfy.model_management':self.management}),
            patch.object(comfy, 'model_management', self.management, create=True),
        ]
        for p in self.patchers: p.start(); self.addCleanup(p.stop)

    def test_native_interrupt_requests_original_job_cancellation(self):
        self.management.throw_exception_if_processing_interrupted.side_effect = [None, self.management.InterruptProcessingException()]
        with patch.object(self.graph, 'call', return_value={'job_id':'a'*32,'state':'RUNNING'}) as call:
            with self.assertRaises(self.management.InterruptProcessingException):
                self.graph.submit_and_wait('edit_image', {'prompt':'x'}, 'comfy')
        self.assertEqual(call.call_count, 2)
        self.assertEqual(call.call_args.args, ('comfy_cancel', {'job_id':'a'*32}))

    def test_saved_result_requires_release_and_no_generation_retry(self):
        with patch.object(self.graph,'call',side_effect=[{'job_id':'b'*32,'state':'QUEUED'}, {'job_id':'b'*32,'state':'SUCCEEDED','memory_released':True}]) as call:
            row=self.graph.submit_and_wait('generate_video',{},'video')
        self.assertEqual(row['state'],'SUCCEEDED')
        self.assertEqual([c.args[0] for c in call.call_args_list], ['generate_video','video_job'])

    def test_ambiguous_submission_retains_exact_request_without_retry(self):
        with patch.object(self.graph,'call',side_effect=TimeoutError()) as call:
            with self.assertRaisesRegex(RuntimeError,'Original request'):
                self.graph.submit_and_wait('edit_image', {'prompt':'original'},'comfy')
        call.assert_called_once()
        files=list(Path(self.tmp.name).glob('dgx-jobs/*.json'))
        record=json.loads(files[0].read_text())
        self.assertEqual(record['arguments']['prompt'],'original')
        self.assertTrue(record['arguments']['idempotency_key'].startswith('comfy-graph-'))

    def test_artifact_rejects_other_host_before_network(self):
        with self.assertRaisesRegex(ValueError,'origin'):
            self.graph.artifact_bytes({'url':'http://example.com/api/v1/artifacts/x'},100)

    def test_successful_resident_job_is_delivered_without_claiming_memory_release(self):
        row = {'job_id':'e'*32,'state':'SUCCEEDED','memory_released':False,
               'resource_settled':True,'resource_status':'resident'}
        with patch.object(self.graph,'call',return_value=row), patch.object(self.graph.time,'sleep'):
            result = self.graph.submit_and_wait('generate_image',{'prompt':'resident'},'comfy',allow_partial=True)
        self.assertFalse(result['memory_released'])
        self.assertTrue(result['resource_settled'])

    def test_unsettled_or_cleanup_job_is_not_delivered(self):
        for extra in ({'resource_settled':False}, {'cleanup_required':True}, {'resource_status':'unknown'}):
            row = {'job_id':'e'*32,'state':'SUCCEEDED','memory_released':False,
                   'resource_settled':True,'resource_status':'resident', **extra}
            with patch.object(self.graph,'call',return_value=row), patch.object(self.graph.time,'sleep'):
                with self.assertRaises(RuntimeError):
                    self.graph.submit_and_wait('generate_image',{'prompt':str(extra)},'comfy',allow_partial=True)

    def test_read_timeout_retries_query_without_resubmitting(self):
        with patch.object(self.graph,'call',side_effect=[{'job_id':'c'*32,'state':'QUEUED'}, TimeoutError(), {'job_id':'c'*32,'state':'SUCCEEDED','memory_released':True}]) as call, patch.object(self.graph.time,'sleep'):
            self.graph.submit_and_wait('edit_image',{},'comfy')
        self.assertEqual([c.args[0] for c in call.call_args_list], ['edit_image','comfy_job','comfy_job'])

    def test_rerun_resumes_known_unfinished_receipt(self):
        root=Path(self.tmp.name)/'dgx-jobs';root.mkdir()
        (root/'previous.json').write_text(json.dumps({'tool':'edit_image','arguments':{'prompt':'same','idempotency_key':'original-key'},'state':'QUEUED','job_id':'d'*32}))
        with patch.object(self.graph,'call',return_value={'job_id':'d'*32,'state':'SUCCEEDED','memory_released':True}) as call:
            self.graph.submit_and_wait('edit_image',{'prompt':'same'},'comfy')
        call.assert_called_once_with('comfy_job',{'job_id':'d'*32})
