import unittest
from DGXGateway.dgx_gateway.image_workflows import generate_image, z_image
from DGXGateway.dgx_gateway.image_batches import manifest, attribution
from DGXGateway.dgx_gateway.runtime_view import job_runtime_view
from DGXGateway.dgx_gateway.residency import ModelResidency


class NewImageModels(unittest.TestCase):
    def test_legacy_graph_unchanged(self):
        self.assertEqual(generate_image('rain', model='z-image-turbo'), z_image('rain'))
        self.assertEqual(generate_image('rain'), generate_image('rain', model='flux2-dev'))

    def test_new_model_groups_and_retry_seeds(self):
        for model in ['flux2-dev', 'flux2-dev-abliterated', 'llada-image-turbo']:
            g = generate_image('rain', model=model, batch_count=3, seed=2**64-1, candidate_indices=[0, 3, 7])
            b = manifest(g)
            self.assertEqual([x['batchSize'] for x in b['groups']], [2, 1])
            self.assertEqual([x['seed'] for x in b['candidates']], [2**64-1, 2, 6])
            self.assertEqual(b['observedParallelism'], 0)
            first, last = b['groups']
            self.assertEqual(g[last['samplerNode']]['inputs']['after_batch'], [first['saveNode'], 0])
            self.assertEqual(attribution(b, last['saveNode'], 0)['candidate_index'], 7)

    def test_flux_uses_resolution_schedule_and_fixed_encoder(self):
        g = generate_image('rain', width=1344, height=768, model='flux2-dev-abliterated', batch_count=4)
        self.assertEqual(g['11']['inputs'], {'steps': 20, 'width': 1344, 'height': 768})
        self.assertEqual(g['2']['inputs']['clip_name'], 'mistral_small_3_abliterated_v2_fp8mixed.safetensors')
        for group in manifest(g)['groups']:
            self.assertEqual(g[group['samplerNode']]['inputs']['sigmas'], ['11', 0])

    def test_flux2_edit_chains_reference_latents_and_sizes_from_image_one(self):
        from DGXGateway.dgx_gateway.image_workflows import edit_image
        from DGXGateway.dgx_gateway.drama.gateway_adapter import validate_parameters
        p = dict(model='flux2-dev', prompt='keep the dress', asset_ids=['a', 'b'], seed=7, batch_count=2)
        validate_parameters('edit_image', p)
        g = edit_image(p, ['ref-a.png', 'ref-b.png'])
        self.assertEqual(g['20']['inputs']['image'], 'ref-a.png')
        self.assertEqual(g['21']['inputs']['image'], 'ref-b.png')
        self.assertEqual(g['60']['inputs']['conditioning'], ['7', 0])
        self.assertEqual(g['61']['inputs']['conditioning'], ['60', 0])
        self.assertEqual(g['61']['inputs']['latent'], ['51', 0])
        self.assertEqual(g['8']['inputs']['positive'], ['61', 0])
        self.assertEqual(g['6']['inputs']['width'], ['68', 0])
        self.assertEqual(g['11']['inputs']['height'], ['68', 1])
        self.assertEqual(g['68']['inputs']['image'], ['40', 0])

    def test_flux2_generate_keeps_requested_canvas_when_references_are_attached(self):
        from DGXGateway.dgx_gateway.image_workflows import generate_image
        from DGXGateway.dgx_gateway.drama.gateway_adapter import validate_parameters
        p = dict(model='flux2-dev', prompt='combine them', asset_ids=['a', 'b', 'c'], width=768, height=1344)
        validate_parameters('generate_image', p)
        g = generate_image(p['prompt'], p['width'], p['height'], model='flux2-dev', image_filenames=['1.png', '2.png', '3.png'])
        self.assertEqual(g['6']['inputs']['width'], 768)
        self.assertEqual(g['6']['inputs']['height'], 1344)
        self.assertNotIn('68', g)
        self.assertEqual(g['8']['inputs']['positive'], ['62', 0])
        self.assertEqual(g['62']['inputs']['latent'], ['52', 0])
        with self.assertRaises(ValueError):
            validate_parameters('generate_image', dict(prompt='x', model='z-image-turbo', preview=True, asset_ids=['a']))
        with self.assertRaises(ValueError):
            validate_parameters('edit_image', dict(model='flux2-dev', prompt='x', asset_ids=['a'], steps=28))
        with self.assertRaises(ValueError):
            validate_parameters('edit_image', dict(prompt='x', asset_ids=['a', 'b', 'c', 'd']))


    def test_model_identities_remain_distinct(self):
        keys = []
        for model in ['flux2-dev', 'flux2-dev-abliterated', 'llada-image-turbo']:
            row = {'workflow': generate_image('rain', model=model), 'state': 'RUNNING',
                   'image_request': {'tool': 'generate_image', 'parameters': {'model': model}}}
            keys.append(str(ModelResidency.key('creative_jobs', row)))
            self.assertIsNotNone(job_runtime_view(row, 'creative')['model_name'])
        self.assertEqual(len(set(keys)), 3)

    def test_invalid_inputs_rejected(self):
        for kwargs in [{'model': 'llada-base'}, {'model': 'flux2-dev', 'width': 528},
                       {'model': 'llada-image-turbo', 'parallelism': 0}]:
            with self.assertRaises(ValueError):
                generate_image('rain', **kwargs)


class ReferenceEditing(unittest.TestCase):
    def test_qwen_default_keeps_exact_legacy_graph(self):
        from DGXGateway.dgx_gateway.image_workflows import edit_image, qwen_image_edit
        p = dict(prompt='rain', asset_ids=['a', 'b'], batch_count=2)
        self.assertEqual(edit_image(p, ['a.png', 'b.png']), qwen_image_edit('rain', ['a.png', 'b.png'], batch_count=2))

    def test_llada_edit_references_survive_groups_retry_and_residency(self):
        from DGXGateway.dgx_gateway.image_workflows import edit_image
        p = dict(model='llada-image-turbo', prompt='turn teapot green', asset_ids=['a'],
                 batch_count=3, candidate_indices=[1, 3, 7], seed=42, width=512, height=512)
        g = edit_image(p, ['reference.png'])
        b = manifest(g)
        self.assertEqual(g['2'], {'class_type': 'LoadImage', 'inputs': {'image': 'reference.png'}})
        self.assertEqual([c['seed'] for c in b['candidates']], [43, 45, 49])
        for group in b['groups']:
            self.assertEqual(g[group['samplerNode']]['inputs']['source_image'], ['2', 0])
        row = {'workflow': g, 'state': 'RUNNING', 'image_request': {'tool': 'edit_image', 'parameters': p}}
        self.assertIn('LLaDA', job_runtime_view(row, 'creative')['model_name'])
        self.assertIsNotNone(ModelResidency.key('creative_jobs', row))

    def test_model_specific_rejections_before_task_persistence(self):
        from DGXGateway.dgx_gateway.drama.gateway_adapter import validate_parameters
        p = dict(model='llada-image-turbo', prompt='green', asset_ids=['a'])
        with self.assertRaises(ValueError):
            validate_parameters('edit_image', p)
        for overrides in [dict(asset_ids=['a', 'b']), dict(steps=28), dict(encoder='standard'),
                          dict(model_precision='fp8'), dict(width=528), dict(width=2048)]:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                validate_parameters('edit_image', {**p, **overrides})
        for overrides in [dict(steps=4), dict(width=512)]:
            with self.assertRaises(ValueError):
                validate_parameters('edit_image', dict(prompt='green', asset_ids=['a'], **overrides))
