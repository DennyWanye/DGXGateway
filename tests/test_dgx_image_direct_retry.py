"""Independent direct image retry identity and artifact-recovery ownership tests."""
from contextlib import nullcontext
import copy
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from DGXGateway.dgx_gateway.creative_jobs import CreativeJobs
from DGXGateway.dgx_gateway.image_batches import manifest
from DGXGateway.dgx_gateway.image_workflows import z_image, qwen_image_edit
from DGXGateway.dgx_gateway import mcp


class DirectRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.app = SimpleNamespace(config=SimpleNamespace(state_dir=Path(self.temp.name), nodes=[SimpleNamespace(address='127.0.0.1')], music3_port=8188),
            _submit_lock=threading.Lock(), store=SimpleNamespace(active_operation=lambda: None, list_operations=lambda **kw: []))
        self.jobs = CreativeJobs(self.app); self.app.creative_jobs = self.jobs
        thread = patch('DGXGateway.dgx_gateway.creative_jobs.threading.Thread'); self.thread = thread.start(); self.addCleanup(thread.stop)
        gate = patch('DGXGateway.dgx_gateway.creative_jobs.creative_admission', return_value=nullcontext()); gate.start(); self.addCleanup(gate.stop)
        self.jobs.assets.get = Mock(side_effect=lambda aid: {'comfy_filename': aid + '.png'})
        self.parent = self.make_parent()

    def make_parent(self, kind='generate_image', jid='a' * 32):
        args = {'prompt': '同一人物在窗边', 'seed': (1 << 64) - 2, 'batch_count': 4, 'parallelism': 2}
        if kind == 'edit_image':
            args.update(asset_ids=['ref1', 'ref2'], steps=27, encoder='huihui-abliterated-bf16', model_precision='bf16')
            graph = qwen_image_edit(args['prompt'], ['ref1.png', 'ref2.png'], args['seed'], args['steps'], args['encoder'], args['model_precision'], 4, 2)
        else:
            args.update(width=512, height=768)
            graph = z_image(args['prompt'], 512, 768, args['seed'], 4, 2)
        row = {'job_id': jid, 'idempotency_key': 'original-key-' + jid, 'state': 'FAILED', 'memory_released': True, 'cleanup_required': False,
               'workflow': graph, 'image_batch': manifest(graph), 'input_assets': args.get('asset_ids', []), 'image_request': {'tool': kind, 'parameters': args}}
        for c in row['image_batch']['candidates']: c['state'] = 'FAILED'
        row['image_batch']['candidates'][0]['state'] = 'SUCCEEDED'
        row['image_batch']['candidates'][1]['state'] = 'UNKNOWN'
        self.jobs.save(row)
        return row

    def test_subset_retry_preserves_original_indices_modulo_seeds_and_parent(self):
        before = (self.jobs.root / (self.parent['job_id'] + '.json')).read_bytes()
        child = self.jobs.retry_images(self.parent['job_id'], [3, 2], 'explicit-child-key')
        self.assertNotEqual(child['job_id'], self.parent['job_id'])
        self.assertEqual(child['retry_of'], self.parent['job_id'])
        self.assertEqual(child['retry_indices'], [2, 3])
        self.assertEqual([(c['index'], c['seed']) for c in child['image_batch']['candidates']], [(2, 0), (3, 1)])
        args = child['image_request']['parameters']
        self.assertEqual((args['seed'], args['width'], args['height'], args['candidate_indices']), ((1 << 64) - 2, 512, 768, [2, 3]))
        self.assertEqual(before, (self.jobs.root / (self.parent['job_id'] + '.json')).read_bytes())
        self.assertEqual(self.thread.call_count, 1)

    def test_same_child_key_returns_original_child_before_busy_or_parent_state_recheck(self):
        child = self.jobs.retry_images(self.parent['job_id'], [2], 'same-child-key')
        self.parent['image_batch']['candidates'][2]['state'] = 'SUCCEEDED'; self.jobs.save(self.parent)
        repeated = self.jobs.retry_images(self.parent['job_id'], [2], 'same-child-key')
        self.assertEqual(repeated['job_id'], child['job_id'])
        self.assertEqual(self.thread.call_count, 1)

    def test_original_parent_key_cannot_be_reused_as_a_new_retry(self):
        with self.assertRaisesRegex(ValueError, 'idempotency key'):
            self.jobs.retry_images(self.parent['job_id'], [2], self.parent['idempotency_key'])
        self.thread.assert_not_called()
        self.assertEqual(len(list(self.jobs.root.glob('*.json'))), 1)

    def test_different_keys_cannot_duplicate_overlapping_candidates(self):
        child = self.jobs.retry_images(self.parent['job_id'], [2], 'first-child-key')
        self.jobs.active = None  # Even after the first child leaves the execution slot.
        with self.assertRaisesRegex(ValueError, child['job_id']):
            self.jobs.retry_images(self.parent['job_id'], [2, 3], 'different-child-key')
        nonoverlap = self.jobs.retry_images(self.parent['job_id'], [3], 'nonoverlap-key')
        self.assertEqual(nonoverlap['retry_indices'], [3])
        self.assertEqual(self.thread.call_count, 2)

    def test_succeeded_unknown_invalid_and_unreleased_candidates_never_retry(self):
        for indices in ([0], [1], [7], [], [2, 2], [-1], [True]):
            with self.subTest(indices=indices), self.assertRaises(ValueError):
                self.jobs.retry_images(self.parent['job_id'], indices, 'invalid-request-key')
        for changes in ({'memory_released': False}, {'cleanup_required': True}, {'state': 'RUNNING'}):
            row = copy.deepcopy(self.parent); row.update(changes); self.jobs.save(row)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.jobs.retry_images(self.parent['job_id'], [2], 'resource-not-released-key')
        self.thread.assert_not_called()

    def test_edit_retry_keeps_references_encoder_precision_steps_prompt_and_seed(self):
        parent = self.make_parent('edit_image', 'b' * 32)
        child = self.jobs.retry_images(parent['job_id'], [2], 'edit-retry-key')
        data = json.loads((self.jobs.root / (child['job_id'] + '.json')).read_text())
        params = data['image_request']['parameters']
        for field in ('asset_ids', 'seed', 'prompt', 'steps', 'encoder', 'model_precision', 'parallelism'):
            self.assertEqual(params[field], parent['image_request']['parameters'][field])
        graph = data['workflow']
        self.assertEqual(data['input_assets'], ['ref1', 'ref2'])
        self.assertEqual(graph['12']['inputs']['steps'], 27)
        self.assertEqual(graph['12']['inputs']['candidate_indices'], '[2]')
        self.assertEqual(graph['20']['inputs']['image'], 'ref1.png')
        self.assertEqual(graph['21']['inputs']['image'], 'ref2.png')
        self.assertIn('bf16', graph['1']['inputs']['unet_name'])
        self.assertIn('huihui', graph['2']['inputs']['clip_name'])

    def test_invalid_parent_identity_and_legacy_parent_fail_before_worker(self):
        with patch.object(Path, 'read_text', side_effect=AssertionError('unsafe path read')):
            with self.assertRaisesRegex(ValueError, 'job_id'):
                self.jobs.retry_images('../arbitrary', [2], 'invalid-parent-key')
        self.parent.pop('image_request'); self.jobs.save(self.parent)
        with self.assertRaisesRegex(ValueError, 'typed image retry'):
            self.jobs.retry_images(self.parent['job_id'], [2], 'legacy-retry-key')
        self.thread.assert_not_called()

    def test_mcp_retry_schema_and_field_wiring_reject_parameter_overrides(self):
        mocked = Mock(return_value={'job_id': 'child'}); self.app.creative_jobs = SimpleNamespace(retry_images=mocked)
        args = {'job_id': 'a' * 32, 'candidate_indices': [2, 3], 'idempotency_key': 'direct-retry-key'}
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'retry_image_candidates', 'arguments': args}}
        response = mcp.dispatch(self.app, request)
        mocked.assert_called_once_with(args['job_id'], [2, 3], args['idempotency_key'])
        self.assertEqual(response['result']['structuredContent'], {'job_id': 'child'})
        for extra in ({'seed': 44}, {'prompt': '替换原提示词'}, {'candidate_indices': [8]}):
            mocked.reset_mock(); request['params']['arguments'] = {**args, **extra}
            self.assertEqual(mcp.dispatch(self.app, request)['error']['code'], -32602)
            mocked.assert_not_called()


class DownloadRecoveryOwnershipTests(unittest.TestCase):
    # Reuse only the fixture, not retry cases as duplicate coverage.
    setUp = DirectRetryTests.setUp
    make_parent = DirectRetryTests.make_parent
    def recovery_row(self):
        row = copy.deepcopy(self.parent)
        row.update(state='RECONCILING', memory_released=False, cleanup_required=True)
        self.jobs.active = row['job_id']; self.jobs.save(row)
        return row

    def test_start_failure_retains_unresolved_owner_without_stop(self):
        row = self.recovery_row()
        self.jobs.switch = Mock(side_effect=TimeoutError('start outcome unknown'))
        self.jobs.request = Mock(); self.jobs.collect = Mock()
        self.jobs._recover_downloads(row)
        self.assertEqual(self.jobs.switch.call_count, 1)
        self.jobs.request.assert_not_called(); self.jobs.collect.assert_not_called()
        self.assertFalse(row['memory_released']); self.assertTrue(row['cleanup_required'])
        self.assertEqual(self.jobs.active, row['job_id'])

    def test_external_queue_does_not_collect_or_stop_others(self):
        row = self.recovery_row()
        self.jobs.switch = Mock(); self.jobs.request = Mock(return_value={'queue_running': ['other-prompt']}); self.jobs.collect = Mock()
        self.jobs._recover_downloads(row)
        self.assertEqual(self.jobs.switch.call_count, 1)
        self.jobs.collect.assert_not_called()
        self.assertTrue(row['cleanup_required']); self.assertEqual(self.jobs.active, row['job_id'])
        self.assertFalse(row['memory_released'])

    def test_stop_failure_retains_owner_after_collection(self):
        row = self.recovery_row()
        self.jobs.switch = Mock(side_effect=[None, RuntimeError('release did not commit')])
        self.jobs.request = Mock(return_value={}); self.jobs.collect = Mock()
        self.jobs._recover_downloads(row)
        self.assertEqual(row['state'], 'INTERRUPTED')
        self.assertTrue(row['cleanup_required']); self.assertFalse(row['memory_released'])
        self.assertEqual(self.jobs.active, row['job_id'])
        self.assertFalse(any(call.args[0] == '/prompt' for call in self.jobs.request.call_args_list))

    def test_collect_failure_still_releases_owned_idle_service_without_new_prompt(self):
        row = self.recovery_row()
        self.jobs.switch = Mock(); self.jobs.request = Mock(return_value={})
        self.jobs.collect = Mock(side_effect=OSError('read failed'))
        self.jobs._recover_downloads(row)
        self.assertEqual(row['state'], 'FAILED')
        self.assertTrue(row['memory_released']); self.assertFalse(row['cleanup_required'])
        self.assertIsNone(self.jobs.active)
        self.assertEqual(self.jobs.switch.call_args_list[1].args[1], None)
        self.assertFalse(any(call.args[0] == '/prompt' for call in self.jobs.request.call_args_list))

    def test_cancel_during_recovery_preserves_downloads_and_finishes_canceled(self):
        row = self.recovery_row()
        self.jobs.switch = Mock(); self.jobs.request = Mock(return_value={})
        def collect(data, **kwargs):
            self.jobs.cancel(data['job_id'])
            for candidate in data['image_batch']['candidates']:
                candidate.update(state='SUCCEEDED', sourceKey='saved-' + str(candidate['index']))
        self.jobs.collect = Mock(side_effect=collect)
        self.jobs._recover_downloads(row)
        saved = self.jobs.get(row['job_id'])
        self.assertEqual(saved['state'], 'CANCELED')
        self.assertTrue(saved['cancel_requested']); self.assertTrue(saved['memory_released'])
        self.assertFalse(saved['cleanup_required']); self.assertIsNone(self.jobs.active)
        self.assertTrue(all(c['state'] == 'SUCCEEDED' and c['sourceKey'] for c in saved['image_batch']['candidates']))
        self.assertEqual(self.jobs.switch.call_count, 2)
        self.assertFalse(any(call.args[0] == '/prompt' for call in self.jobs.request.call_args_list))
