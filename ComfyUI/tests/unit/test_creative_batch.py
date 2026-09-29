"""MF05 graph contracts. Tensor tests use real CPU torch when available; no GPU."""
import importlib
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

# Reuse the existing Comfy API schema stub; media/tensors remain real if installed.
import test_node
from test_creative_graph import GraphTransportTests
if 'comfy' not in sys.modules:
    sys.modules['comfy'] = ModuleType('comfy')
graph = importlib.import_module('dgx_remote_h3.creative_graph')
TENSORS = importlib.util.find_spec('torch') is not None and importlib.util.find_spec('numpy') is not None


class BatchTransportTests(unittest.TestCase):
    setUp = GraphTransportTests.setUp

    def test_partial_terminal_can_return_only_after_resource_release(self):
        result = {'job_id':'a'*32, 'state':'FAILED', 'memory_released':True, 'artifacts':[{'kind':'images'}]}
        with patch.object(graph, 'call', side_effect=[{'job_id':'a'*32,'state':'QUEUED'}, result]):
            row = graph.submit_and_wait('generate_image', {'batch_count':2}, 'comfy', allow_partial=True)
        self.assertEqual(row['state'], 'FAILED')
        self.assertTrue(Path(row['_graph_receipt']).exists())

    def test_partial_unreleased_rerun_queries_original_without_new_generation(self):
        waiting = {'job_id':'b'*32, 'state':'FAILED', 'memory_released':False}
        with patch.object(graph, 'call', side_effect=[{'job_id':'b'*32,'state':'QUEUED'}, waiting]):
            with self.assertRaisesRegex(RuntimeError, 'memory_released=False'):
                graph.submit_and_wait('generate_image', {'batch_count':2}, 'comfy', allow_partial=True)
        with patch.object(graph, 'call', return_value={**waiting, 'memory_released':True}) as call:
            graph.submit_and_wait('generate_image', {'batch_count':2}, 'comfy', allow_partial=True)
        call.assert_called_once_with('comfy_job', {'job_id':'b'*32})

    def test_partial_delivered_receipt_resumes_original_without_repainting_successes(self):
        root=Path(self.tmp.name)/'dgx-jobs';root.mkdir()
        receipt=root/'partial.json'
        receipt.write_text(json.dumps({'tool':'generate_image','arguments':{'prompt':'same','batch_count':2,'idempotency_key':'original-key'},'state':'FAILED','job_id':'p'*32}))
        graph.delivered({'_graph_receipt':str(receipt)}, partial=True)
        self.assertEqual(json.loads(receipt.read_text())['state'],'PARTIAL_DELIVERED')
        with patch.object(graph,'call',return_value={'job_id':'p'*32,'state':'FAILED','memory_released':True}) as call:
            graph.submit_and_wait('generate_image',{'prompt':'same','batch_count':2},'comfy',allow_partial=True)
        call.assert_called_once_with('comfy_job',{'job_id':'p'*32})

    def test_old_single_image_path_still_rejects_partial_failure(self):
        with patch.object(graph, 'call', side_effect=[{'job_id':'c'*32,'state':'QUEUED'}, {'job_id':'c'*32,'state':'FAILED','memory_released':True}]):
            with self.assertRaisesRegex(RuntimeError, 'FAILED'):
                graph.submit_and_wait('edit_image', {}, 'comfy')


class BatchInputTests(unittest.TestCase):
    def test_qwen_schema_appends_parallelism_without_reordering_old_widgets(self):
        schema = graph.DGXQwenImageEditBatch.define_schema()
        self.assertEqual([v.id for v in schema.inputs], ['reference_image','prompt','model_precision','encoder','batch_count','seed','steps','parallelism'])
        self.assertEqual(schema.inputs[-1].options, {'default':2,'min':1,'max':2,'optional':True})
        self.assertEqual([v.id for v in schema.outputs], ['image','job_json'])
        self.assertEqual([v.id for v in graph.DGXQwenImageEdit.define_schema().inputs], ['reference_image','prompt','seed','steps'])
        self.assertEqual(len(graph.DGXQwenImageEditHuihui.define_schema().outputs), 1)

    def test_all_batch_controls_are_validated_before_register_or_submit(self):
        with patch.object(graph, 'register_image') as register, patch.object(graph, 'submit_and_wait') as submit:
            for count, parallel in [(0,2),(9,2),(2,0),(2,3),(True,2),(2,False)]:
                with self.subTest(count=count,parallel=parallel), self.assertRaises(ValueError):
                    graph.DGXQwenImageEditBatch.execute([object()], 'prompt', batch_count=count, parallelism=parallel)
            with self.assertRaises(ValueError):
                graph.DGXQwenImageEditBatch.execute([object(),object()], 'prompt', batch_count=2)
            register.assert_not_called(); submit.assert_not_called()

    def test_qwen_batch_passes_parallelism_and_complete_task_to_decoder(self):
        row = {'job_id':'job'}
        with patch.object(graph, 'register_image', return_value='reference'), patch.object(graph, 'submit_and_wait', return_value=row) as submit, patch.object(graph, 'image_batch_result', return_value='outputs') as decode:
            self.assertEqual(graph.DGXQwenImageEditBatch.execute([object()], 'prompt', batch_count=4, seed=99, parallelism=1), 'outputs')
        submit.assert_called_once_with('edit_image', {'asset_ids':['reference'],'prompt':'prompt','seed':99,'steps':20,'model_precision':'fp8','encoder':'standard','batch_count':4,'parallelism':1}, 'comfy', allow_partial=True)
        decode.assert_called_once_with(row, 4)

    def test_zimage_batch_defaults_and_output_contract(self):
        with patch.object(graph, 'submit_and_wait', return_value={'job_id':'z'}) as submit, patch.object(graph, 'image_batch_result', return_value='outputs'):
            self.assertEqual(graph.DGXZImageBatch.execute('prompt', batch_count=2), 'outputs')
        self.assertEqual(submit.call_args.args, ('generate_image', {'prompt':'prompt','width':1024,'height':1024,'seed':42,'batch_count':2,'parallelism':2}, 'comfy'))
        self.assertEqual([v.id for v in graph.DGXZImageBatch.define_schema().outputs], ['image','job_json'])


@unittest.skipUnless(TENSORS, 'real CPU numpy/torch required; use existing Comfy container')
class BatchDecodeTests(unittest.TestCase):
    def png(self, color, size=(8,6)):
        from PIL import Image
        output = io.BytesIO(); Image.new('RGB', size, color).save(output, 'PNG'); return output.getvalue()

    def row(self):
        return {'job_id':'job', 'state':'SUCCEEDED','memory_released':True,
                'image_batch':{'version':1,'candidateCount':2,'candidates':[{'index':0,'seed':42,'state':'SUCCEEDED'},{'index':1,'seed':43,'state':'SUCCEEDED'}]},
                'artifacts':[{'node':'10','kind':'images','candidate_index':1,'seed':43,'url':'blue'},{'node':'10','kind':'images','candidate_index':0,'seed':42,'url':'red'}]}

    def test_same_save_node_outputs_keep_candidate_order_and_real_image_batch(self):
        row = self.row()
        with patch.object(graph,'artifact_bytes',side_effect=lambda a,n:self.png(a['url'])), patch.object(graph,'delivered') as delivered:
            result = graph.image_batch_result(row,2)
        self.assertEqual(tuple(result.args[0].shape),(2,6,8,3))
        self.assertEqual(result.args[0][0,0,0].tolist(), [1.,0.,0.])
        self.assertEqual(result.args[0][1,0,0].tolist(), [0.,0.,1.])
        summary = json.loads(result.args[1]);self.assertTrue(summary['delivery']['complete'])
        self.assertEqual([i['seed'] for i in summary['delivery']['images']],[42,43]);delivered.assert_called_once_with(row)

    def test_download_failure_preserves_success_tensor_and_explicit_error(self):
        row=self.row();row['state']='FAILED'
        def payload(a,n):
            if a['url']=='blue':raise ConnectionError('download interrupted')
            return self.png('red')
        with patch.object(graph,'artifact_bytes',side_effect=payload),patch.object(graph,'delivered') as delivered:
            result=graph.image_batch_result(row,2)
        delivered.assert_called_once_with(row, partial=True)
        self.assertEqual(tuple(result.args[0].shape),(1,6,8,3))
        report=json.loads(result.args[1]);self.assertFalse(report['delivery']['complete'])
        self.assertEqual(report['job']['state'],'FAILED');self.assertEqual(report['delivery']['errors'][0]['candidateIndex'],1)

    def test_wrong_seed_is_reported_but_other_success_survives(self):
        row=self.row();row['artifacts'][0]['seed']=999
        with patch.object(graph,'artifact_bytes',side_effect=lambda a,n:self.png('red')),patch.object(graph,'delivered'):
            result=graph.image_batch_result(row,2)
        self.assertEqual(len(result.args[0]),1);self.assertIn('seed',json.loads(result.args[1])['delivery']['errors'][0]['error'])

    def test_zero_valid_outputs_keeps_original_receipt_for_recovery(self):
        with patch.object(graph,'artifact_bytes',side_effect=ConnectionError('no bytes')),patch.object(graph,'delivered') as delivered:
            with self.assertRaisesRegex(RuntimeError,'原任务回执仍保留'):graph.image_batch_result(self.row(),2)
        delivered.assert_not_called()

    def test_legacy_results_do_not_invent_seeds_or_discard_extra_images(self):
        row={'job_id':'legacy','state':'SUCCEEDED','artifacts':[{'kind':'images','node':'8','url':'red'},{'kind':'images','node':'9','url':'blue'}]}
        with patch.object(graph,'artifact_bytes',side_effect=lambda a,n:self.png(a['url'])),patch.object(graph,'delivered'):
            result=graph.image_batch_result(row,2)
        report=json.loads(result.args[1]);self.assertEqual(len(result.args[0]),2)
        self.assertEqual(report['delivery']['identitySource'],'legacy_artifact_order');self.assertTrue(all(c['seed'] is None for c in report['delivery']['images']))


class BatchWorkflowTests(unittest.TestCase):
    def test_official_recommended_templates_wire_all_images_and_preserve_json_output(self):
        root=Path(__file__).resolve().parents[2]/'workflows'
        for filename, node_type in [('DGX-Qwen-版本选择与批量.json','DGXQwenImageEditBatch'),('Qwen-2511-版本选择与批量（推荐）.json','DGXQwenImageEditBatch'),('ZImage-文字生图与多候选（推荐）.json','DGXZImageBatch')]:
            with self.subTest(filename=filename):
                value=json.loads((root/filename).read_text());node=next(n for n in value['nodes'] if n['type']==node_type)
                self.assertEqual(node['outputs'][0]['type'],'IMAGE');self.assertEqual(node['outputs'][1]['name'],'job_json')
                self.assertEqual(node['widgets_values'][-1],2)
                link=next(l for l in value['links'] if l[1]==node['id'] and l[2]==0)
                self.assertEqual(next(n for n in value['nodes'] if n['id']==link[3])['type'],'SaveImage')

    def test_all_legacy_single_templates_keep_single_nodes_and_explicit_batch_migration(self):
        root=Path(__file__).resolve().parents[2]/'workflows'
        for name in ['DGX-Qwen-图片编辑.json','Qwen-2511-FP8-标准编码器-单图.json','DGX-Qwen-去限制编码器.json','Qwen-2511-FP8-Huihui编码器-单图.json']:
            value=json.loads((root/name).read_text());nodes=[n for n in value['nodes'] if n['type'] in ['DGXQwenImageEdit','DGXQwenImageEditHuihui']]
            self.assertEqual(len(nodes),1);self.assertEqual(len(nodes[0]['outputs']),1)
            self.assertIn('Qwen-2511-版本选择与批量（推荐）.json',json.dumps(value,ensure_ascii=False))
