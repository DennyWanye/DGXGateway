"""Isolated node/protocol oracles: no CUDA, model weights or live Comfy server."""
import importlib.util
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

try:
    import torch as real_torch
except ImportError:
    real_torch = None

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


class Tensor:
    def __init__(self, shape, values=None, device='cpu'):
        self.shape = tuple(shape)
        self.ndim = len(shape)
        self.values = values or []
        self.device = device
        self.dtype = 'torch.float32'
    def repeat(self, counts):
        return Tensor([d * n for d, n in zip(self.shape, counts)], self.values * counts[0], self.device)
    def __len__(self):
        return self.shape[0]


class BatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.graph = {str(i): {'class_type': 'DGXSeedBatchKSampler' if i % 2 else 'DGXBatchSaveImage'} for i in range(1, 9)}
        self.running = [[0, 'prompt1', self.graph, {'client_id': 'owner'}]]
        self.pending = []
        queue = SimpleNamespace(mutex=threading.RLock(), get_current_queue_volatile=lambda: (list(self.running), list(self.pending)))
        self.server = SimpleNamespace(routes=web.RouteTableDef(), prompt_queue=queue)
        self.noise_calls, self.captured, self.existing_calls = [], [], []
        self.failure = None
        self.split = False
        self.cpu_forward = False
        self.hooks = []
        self.internal_split = False
        self.diffusion_kwargs = False
        self.save_failure = False
        self.cuda = SimpleNamespace(synchronize=Mock(), reset_peak_memory_stats=Mock(),
                                    max_memory_allocated=lambda _: 1234, max_memory_reserved=lambda _: 5678)
        def noise(latent, seed):
            self.noise_calls.append((latent.shape, seed))
            return Tensor(latent.shape, [seed])
        def cat(tensors, dim):
            assert dim == 0
            return Tensor((sum(t.shape[0] for t in tensors), *tensors[0].shape[1:]), sum([t.values for t in tensors], []))
        def sample(model, noise, steps, cfg, sampler, scheduler, positive, negative, latent, **kwargs):
            self.captured.append({'noise': noise.values, 'latent_shape': latent.shape, 'kwargs': kwargs})
            if self.failure:
                raise self.failure
            count = len(noise.values)
            device = 'cpu' if self.cpu_forward else 'cuda:0'
            args = {'input': Tensor((count * 2, 16, 4, 4), device=device), 'timestep': 1, 'c': {}, 'cond_or_uncond': [0, 1]}
            for step in range(steps):
                def apply(x, t, **kw):
                    if self.internal_split:
                        x = Tensor((2, *x.shape[1:]), device=x.device)
                    for hook in list(self.hooks):
                        hook(None, () if self.diffusion_kwargs else (x,), {'x': x} if self.diffusion_kwargs else {})
                    return x
                model.model_options['model_function_wrapper'](apply, args)
                kwargs['callback'](step, None, None, steps)
            return latent
        self.model = SimpleNamespace(model_options={}, load_device='cuda:0')
        def existing(apply, args):
            self.existing_calls.append(True)
            if self.split:
                return apply(Tensor((2, 16, 4, 4), device='cuda:0'), args['timestep'])
            return apply(args['input'], args['timestep'], **args['c'])
        self.model.model_options['model_function_wrapper'] = existing
        def register_hook(hook, with_kwargs):
            assert with_kwargs
            self.hooks.append(hook)
            return SimpleNamespace(remove=lambda: self.hooks.remove(hook))
        self.diffusion = SimpleNamespace(register_forward_pre_hook=register_hook)
        def clone():
            value = SimpleNamespace(model_options=self.model.model_options.copy(), load_device='cuda:0')
            value.get_model_object = lambda name: self.diffusion if name == 'diffusion_model' else None
            value.set_model_unet_function_wrapper = lambda fn: value.model_options.__setitem__('model_function_wrapper', fn)
            return value
        self.model.clone = clone
        def save(images, prefix, prompt, png):
            if self.save_failure:
                raise OSError('save failed')
            return {'ui': {'images': [{'filename': f'{prefix}-{i}.png', 'subfolder': '', 'type': 'output'} for i in range(len(images))]}}
        self.sample_module = SimpleNamespace(prepare_noise=noise, fix_empty_latent_channels=lambda m, x, *args: x, sample=sample)
        utils = SimpleNamespace(PROGRESS_BAR_ENABLED=True)
        self.modules = patch.dict(sys.modules, {
            'server': SimpleNamespace(PromptServer=SimpleNamespace(instance=self.server)),
            'torch': SimpleNamespace(cat=cat, cuda=self.cuda),
            'comfy': SimpleNamespace(sample=self.sample_module, utils=utils),
            'comfy.sample': self.sample_module, 'comfy.utils': utils,
            'latent_preview': SimpleNamespace(prepare_callback=lambda *args: None),
            'nodes': SimpleNamespace(SaveImage=lambda: SimpleNamespace(save_images=save),
                                     KSampler=SimpleNamespace(INPUT_TYPES=lambda: {'required': {'model': ('MODEL',)}})),
        })
        self.modules.start()
        spec = importlib.util.spec_from_file_location('dgx_batch_node_test', Path(__file__).with_name('__init__.py'))
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.progress = self.server._dgx_batch_progress
        app = web.Application(); app.add_routes(self.server.routes)
        self.client = TestClient(TestServer(app)); await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.modules.stop()

    def run_group(self, node='1', indices='[0,1]', seed=42, after=None, **kwargs):
        return self.module.DGXSeedBatchKSampler().sample(self.model, seed, 3, 1, 'euler', 'simple', [], [],
            {'samples': Tensor((1, 16, 4, 4))}, candidate_indices=indices, unique_id=node, after_batch=after, **kwargs)

    def save_group(self, info, node='2'):
        return self.module.DGXBatchSaveImage().save_images(Tensor((len(info['indices']), 32, 32, 3)), info, 'candidate', unique_id=node)

    async def test_custom_parallelism_and_more_than_eight_groups(self):
        _, info = self.run_group(indices=json.dumps(list(range(20))))
        self.assertEqual(self.captured[0]['latent_shape'][0], 20)
        self.assertEqual(info['indices'], list(range(20)))
        self.assertEqual(len(self.noise_calls), 20)
        for i in range(10):
            self.progress.begin('more-groups', 'owner', str(i), [i], [42+i], 3)
        self.assertEqual(len(self.progress.jobs['more-groups']['groups']), 10)

    async def test_seed_modulo_and_original_subset_indices_use_independent_single_noise(self):
        _, info = self.run_group(indices='[1,7]', seed=(1 << 64) - 2)
        self.assertEqual(info['seeds'], [(1 << 64) - 1, 5])
        self.assertEqual(self.noise_calls, [((1, 16, 4, 4), (1 << 64) - 1), ((1, 16, 4, 4), 5)])
        self.assertEqual(self.captured[0]['noise'], info['seeds'])
        self.assertEqual(self.captured[0]['latent_shape'], (2, 16, 4, 4))
        self.assertEqual(len(self.captured), 1)
        self.assertEqual(len(self.existing_calls), 3)
        self.assertEqual(self.hooks, [])
        self.assertEqual(self.model.model_options.keys(), {'model_function_wrapper'})

    async def test_cfg_shape_is_normalized_and_metrics_are_bounded(self):
        _, info = self.run_group()
        g = self.progress.jobs['prompt1']['groups']['1']
        self.assertEqual(g['actual_candidate_parallelism'], 2)
        self.assertEqual(g['trace'][0]['input_shape'][0], 4)
        self.assertEqual(g['trace'][0]['source'], 'diffusion_forward')
        self.assertEqual(g['apply_trace'][0]['source'], 'apply_model_input')
        self.assertEqual(g['trace'][0]['cond_or_uncond'], [0, 1])
        self.assertEqual((g['step'], g['total_steps']), (3, 3))
        self.assertEqual(g['peak_memory_allocated'], 1234)
        self.assertEqual(g['peak_memory_reserved'], 5678)
        for _ in range(100):
            self.progress.forward(info, Tensor((4, 16, 4, 4), device='cuda:0'), [0, 1])
        self.assertEqual(len(g['trace']), 32)
        self.assertEqual(g['forward_count'], 103)

    async def test_existing_wrapper_that_splits_batch_cannot_claim_parallelism(self):
        self.split = True
        self.run_group()
        self.assertEqual(self.progress.jobs['prompt1']['groups']['1']['actual_candidate_parallelism'], 0)

    async def test_existing_wrapper_model_lifecycle_is_preserved(self):
        calls = []
        class OwnedWrapper:
            def __call__(self, apply, args):
                calls.append('call')
                return apply(args['input'], args['timestep'], **args['c'])
            def models(self):
                return ['owned-model']
            def to(self, device):
                calls.append(device)
                return self
        self.model.model_options['model_function_wrapper'] = OwnedWrapper()
        original_sample = self.sample_module.sample
        def sample(model, *args, **kwargs):
            wrapper = model.model_options['model_function_wrapper']
            self.assertEqual(wrapper.models(), ['owned-model'])
            self.assertIs(wrapper.to('cuda:0'), wrapper)
            return original_sample(model, *args, **kwargs)
        with patch.object(self.sample_module, 'sample', sample):
            self.run_group()
        self.assertEqual(calls, ['cuda:0', 'call', 'call', 'call'])
        self.assertEqual(self.progress.jobs['prompt1']['groups']['1']['actual_candidate_parallelism'], 2)
        self.assertEqual(self.hooks, [])

    async def test_internal_model_split_cannot_claim_apply_entry_batch_as_actual(self):
        self.internal_split = True
        self.run_group()
        group = self.progress.jobs['prompt1']['groups']['1']
        self.assertEqual(group['apply_trace'][0]['candidate_batch'], 2)
        self.assertEqual(group['trace'][0]['candidate_batch'], 1)
        self.assertEqual(group['actual_candidate_parallelism'], 0)
        self.assertEqual(self.hooks, [])

    async def test_diffusion_kwargs_input_is_observed_and_hook_removed_after_interrupt(self):
        self.diffusion_kwargs = True
        self.run_group()
        self.assertEqual(self.progress.jobs['prompt1']['groups']['1']['actual_candidate_parallelism'], 2)
        self.assertEqual(self.hooks, [])
        self.failure = RuntimeError('interrupted')
        with self.assertRaises(RuntimeError): self.run_group(node='3', indices='[2]')
        self.assertEqual(self.hooks, [])

    async def test_cpu_forward_does_not_claim_gpu_parallelism(self):
        self.cpu_forward = True
        self.run_group()
        self.assertEqual(self.progress.jobs['prompt1']['groups']['1']['actual_candidate_parallelism'], 0)

    async def test_saved_group_is_published_before_next_group_and_preserved_on_failure(self):
        _, info = self.run_group()
        saved = self.save_group(info)
        self.assertEqual(saved['result'][0]['saved'], True)
        response = await self.client.get('/dgx/batch-progress/prompt1?client_id=owner')
        first = await response.json()
        self.assertEqual(first['groups']['1']['state'], 'SAVED')
        self.assertEqual(len(first['outputs']['2']['images']), 2)
        self.failure = RuntimeError('OOM')
        with self.assertRaises(RuntimeError):
            self.run_group(node='3', indices='[2,3]', after=saved['result'][0])
        result = await (await self.client.get('/dgx/batch-progress/prompt1?client_id=owner')).json()
        self.assertEqual(result['outputs'], first['outputs'])
        self.assertEqual(result['groups']['3']['state'], 'FAILED')
        self.assertEqual(result['groups']['3']['error_type'], 'RuntimeError')
        self.assertEqual(len(self.captured), 2)

    async def test_queue_binding_is_unique_and_no_pending_or_last_id_fallback(self):
        self.running.clear(); self.pending.append([0, 'prompt1', self.graph, {'client_id': 'owner'}])
        with self.assertRaises(RuntimeError): self.run_group()
        self.running.extend([[0, 'a', self.graph, {'client_id': 'a'}], [0, 'b', self.graph, {'client_id': 'b'}]])
        with self.assertRaises(RuntimeError): self.run_group()
        self.assertEqual(self.progress.jobs, {})

    async def test_progress_route_rejects_other_owner_unknown_and_duplicate_identity(self):
        self.run_group()
        self.assertEqual((await self.client.get('/dgx/batch-progress/prompt1?client_id=other')).status, 403)
        self.assertEqual((await self.client.get('/dgx/batch-progress/missing?client_id=owner')).status, 404)
        self.assertEqual((await self.client.get('/dgx/batch-progress/prompt1?client_id=owner&client_id=owner')).status, 400)
        self.running.clear()  # Completed prompt remains readable to its recorded owner.
        self.assertEqual((await self.client.get('/dgx/batch-progress/prompt1?client_id=owner')).status, 200)

    async def test_limits_reject_invalid_groups_duplicate_execution_and_unsaved_dependency(self):
        for value in ('[]', '[0,0]', '[0.5]', '[-1]', '[true]', 'bad'):
            with self.assertRaises(ValueError): self.run_group(indices=value)
        _, info = self.run_group()
        with self.assertRaises(RuntimeError): self.run_group()
        with self.assertRaises(ValueError): self.run_group(node='3', indices='[2]', after=info)
        self.assertEqual(len(self.captured), 1)

    async def test_save_failure_and_comfy_interrupt_are_rethrown_without_retry(self):
        _, info = self.run_group()
        self.save_failure = True
        with self.assertRaises(OSError): self.save_group(info)
        self.assertEqual(self.progress.jobs['prompt1']['groups']['1']['state'], 'FAILED')
        class InterruptProcessingException(Exception): pass
        self.failure = InterruptProcessingException()
        with self.assertRaises(InterruptProcessingException): self.run_group(node='3', indices='[2]')
        self.assertEqual(len(self.captured), 2)
        self.assertEqual(self.progress.jobs['prompt1']['groups']['3']['error_type'], 'InterruptProcessingException')

    @unittest.skipIf(real_torch is None, 'real torch CPU package unavailable')
    async def test_real_cpu_noise_is_stable_across_two_plus_two_and_subset(self):
        torch = real_torch
        source = torch.zeros((1, 16, 4, 4), dtype=torch.float32)
        batches = []
        def real_noise(latent, seed):
            generator = torch.manual_seed(seed)
            return torch.randn(latent.size(), dtype=torch.float32, generator=generator, device='cpu').to(dtype=latent.dtype)
        def capture(model, noise, steps, cfg, sampler, scheduler, positive, negative, latent, **kwargs):
            batches.append(noise.clone())
            return latent
        # The sampler is stubbed; only numeric noise creation/cat and output shape are real torch CPU.
        with patch.dict(sys.modules, {'torch': torch}), patch.object(self.sample_module, 'prepare_noise', real_noise), patch.object(self.sample_module, 'sample', capture):
            original_clone = self.model.clone
            def cpu_clone():
                model = original_clone(); model.load_device = 'cpu'; return model
            self.model.clone = cpu_clone
            for pid, indices in [('group01', '[0,1]'), ('group23', '[2,3]'), ('subset2', '[2]')]:
                self.running[:] = [[0, pid, self.graph, {'client_id': 'owner'}]]
                output, info = self.module.DGXSeedBatchKSampler().sample(self.model, 99, 3, 1, 'euler', 'simple', [], [], {'samples': source}, candidate_indices=indices, unique_id='1')
                self.assertEqual(output['samples'].shape[0], len(info['indices']))
            expected_last = real_noise(source, 102)[0]
        self.assertTrue(torch.equal(batches[1][0], batches[2][0]))
        self.assertFalse(torch.equal(batches[0][0], batches[0][1]))
        self.assertTrue(torch.equal(batches[1][1], expected_last))

    async def test_registry_evicts_to_64_jobs_and_schema_exports_two_nodes(self):
        for i in range(65): self.progress.begin(f'p{i}', 'owner', '1', [0], [42], 3)
        self.assertEqual(len(self.progress.jobs), 64)
        self.assertNotIn('p0', self.progress.jobs)
        schema = self.module.DGXSeedBatchKSampler.INPUT_TYPES()
        self.assertEqual(schema['optional']['after_batch'], ('DGX_BATCH_INFO',))
        self.assertEqual(schema['hidden']['unique_id'], 'UNIQUE_ID')
        self.assertEqual(set(self.module.NODE_CLASS_MAPPINGS), {'DGXSeedBatchKSampler', 'DGXBatchSaveImage'})


if __name__ == '__main__':
    unittest.main()
