import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from DGXGateway.dgx_gateway.mcp import dispatch

class MCPTests(unittest.TestCase):
    def setUp(self):
        self.app = SimpleNamespace(snapshot=SimpleNamespace(build=lambda: {"active_connection": {"api_key":"secret"}, "models": []}))
    def request(self, method, **params):
        return dispatch(self.app, {"jsonrpc":"2.0", "id":1,"method":method,"params":params})
    def test_handshake_and_catalog(self):
        self.assertIn('tools',self.request('initialize',protocolVersion='2025-11-25')['result']['capabilities'])
        self.assertIn('dgx_stop',[t['name'] for t in self.request('tools/list')['result']['tools']])
    def test_status_omits_credential(self):
        r=self.request('tools/call',name='dgx_status',arguments={})
        self.assertNotIn('secret',str(r)); self.assertFalse(r['result']['isError'])
    def test_invalid_cannot_invoke_mutation(self):
        self.app.submit_switch=Mock()
        for args in ({}, {'idempotency_key':'x'}, {'idempotency_key':'valid-key','ssh':'rm'}):
            self.assertEqual(self.request('tools/call',name='dgx_stop',arguments=args)['error']['code'],-32602)
        self.app.submit_switch.assert_not_called()
    def test_unknown_tool_and_notifications(self):
        self.assertEqual(self.request('tools/call',name='exec',arguments={})['error']['code'],-32602)
        self.assertIsNone(dispatch(self.app,{'jsonrpc':'2.0','method':'notifications/initialized'}))
    def test_video_batch_nested_schema_rejects_extra_fields_before_mutation(self):
        self.app.video_jobs=SimpleNamespace(submit=Mock())
        shot={"name":"WH-01","prompt":"move","image_asset_id":"a"*64,"audio_asset_id":"b"*64,"seed":1,"ssh":"forbidden"}
        response=self.request('tools/call',name='video_batch_submit',arguments={"shots":[shot],"idempotency_key":"video-key"})
        self.assertEqual(response['error']['code'],-32602)
        self.app.video_jobs.submit.assert_not_called()

    def test_quality_image_policy_is_the_only_generation_path(self):
        from DGXGateway.dgx_gateway.mcp import TOOLS, call
        from DGXGateway.dgx_gateway.drama.gateway_adapter import validate_parameters
        generate = next(t for t in TOOLS if t['name'] == 'generate_image')
        edit = next(t for t in TOOLS if t['name'] == 'edit_image')
        submit = next(t for t in TOOLS if t['name'] == 'comfy_submit')
        self.assertIn('flux2-dev', generate['description'])
        self.assertIn('preview', generate['inputSchema']['properties'])
        self.assertIn('qwen-image-edit-2511', edit['description'])
        self.assertIn('generate_image or edit_image', submit['description'])
        self.assertIn('generate_image or edit_image', self.request('initialize', protocolVersion='2025-11-25')['result']['instructions'])
        app = SimpleNamespace(creative_jobs=SimpleNamespace(submit=Mock(return_value={'job_id': 'j'})))
        call(app, 'generate_image', {'prompt': 'rain', 'idempotency_key': 'quality-default'})
        saved = app.creative_jobs.submit.call_args.kwargs['image_request']['parameters']
        self.assertEqual(saved['model'], 'flux2-dev')
        self.assertIn('EmptyFlux2LatentImage', str(app.creative_jobs.submit.call_args.args[0]))
        for arguments in (
            {'prompt': 'rain', 'model': 'z-image-turbo', 'idempotency_key': 'preview-missing'},
            {'prompt': 'rain', 'model': 'flux2-dev-abliterated', 'idempotency_key': 'not-quality'},
            {'prompt': 'rain', 'model': 'llada-image-turbo', 'idempotency_key': 'not-quality-llada'},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                call(app, 'generate_image', arguments)
        call(app, 'generate_image', {'prompt': 'rain', 'model': 'z-image-turbo', 'preview': True, 'idempotency_key': 'look-only'})
        self.assertEqual(app.creative_jobs.submit.call_args.kwargs['image_request']['parameters']['model'], 'z-image-turbo')
        with self.assertRaises(ValueError):
            call(app, 'comfy_submit', {'workflow': {'1': {'class_type': 'UNETLoader', 'inputs': {'unet_name': 'z_image_turbo_bf16.safetensors'}}}, 'idempotency_key': 'raw-image'})
        call(app, 'comfy_submit', {'workflow': {'1': {'class_type': 'UNETLoader', 'inputs': {'unet_name': 'minimax_music3_dit_fp16.safetensors'}}}, 'idempotency_key': 'music-ok'})
        with self.assertRaises(ValueError):
            validate_parameters('generate_image', {'prompt': 'rain', 'width': 528, 'height': 768})
        validate_parameters('generate_image', {'prompt': 'rain', 'model': 'z-image-turbo', 'preview': True, 'width': 528, 'height': 768})

if __name__=='__main__': unittest.main()
