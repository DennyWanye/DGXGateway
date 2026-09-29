"""Ring4 ComfyUI nodes: contract shape, profile routing, and no-bypass guarantees."""
import importlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'custom_nodes'))


class Ring4NodeTests(unittest.TestCase):
    """Self-contained: installs the Comfy stubs, then exercises the Ring4 nodes."""

    def setUp(self):
        import types
        import tempfile

        class FakeSchema:
            def __init__(self, **values): self.__dict__.update(values)

        class FakeIO:
            def __init__(self, identifier=None, **options): self.id = identifier; self.options = options
            Input = Output = None

        def make(kind):
            return types.SimpleNamespace(Input=lambda *a, **k: FakeIO(k.get("id", a[0] if a else None), **k),
                                         Output=lambda *a, **k: FakeIO(k.get("id", a[0] if a else None), **k))
        class FakeNodeOutput:
            def __init__(self, *args, ui=None, **kw): self.args = args; self.ui = ui

        latest = types.ModuleType("comfy_api.latest")
        class FakeComfyNode:
            @classmethod
            def define_schema(cls): raise NotImplementedError
            @classmethod
            def execute(cls, **kwargs): raise NotImplementedError
        latest.io = types.SimpleNamespace(
            ComfyNode=FakeComfyNode,
            Schema=FakeSchema, NodeOutput=FakeNodeOutput,
            String=make("String"), Combo=make("Combo"), Int=make("Int"),
            Image=make("Image"), Audio=make("Audio"), Video=make("Video"),
            FolderType=types.SimpleNamespace(output="output"))
        latest.ui = types.SimpleNamespace(
            SavedResult=lambda *a, **k: {"filename": a[0] if a else "", "subfolder": "", "type": "output"},
            PreviewVideo=lambda items: items)
        latest.InputImpl = types.SimpleNamespace(VideoFromFile=lambda path: path)
        comfy_api = types.ModuleType("comfy_api"); comfy_api.latest = latest
        sys.modules["comfy_api"] = comfy_api; sys.modules["comfy_api.latest"] = latest

        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        folder_paths = types.ModuleType("folder_paths")
        folder_paths.get_output_directory = lambda: self.tmp.name
        folder_paths.get_temp_directory = lambda: self.tmp.name
        sys.modules["folder_paths"] = folder_paths
        comfy = types.ModuleType("comfy")
        comfy.model_management = types.SimpleNamespace(
            throw_exception_if_processing_interrupted=lambda: None)
        sys.modules["comfy"] = comfy
        server = types.ModuleType("server")
        server.PromptServer = types.SimpleNamespace(
            instance=types.SimpleNamespace(send_sync=lambda *a, **k: None))
        sys.modules["server"] = server

        for name in ("dgx_remote_h3", "dgx_remote_h3.creative_graph", "dgx_remote_h3.node"):
            sys.modules.pop(name, None)
        self.graph = importlib.import_module('dgx_remote_h3.creative_graph')

    # --- schema / registration -------------------------------------------
    def test_nodes_are_registered_in_extension_list(self):
        node_path = Path(__file__).resolve().parents[2] / 'custom_nodes/dgx_remote_h3/node.py'
        source = node_path.read_text(encoding='utf-8')
        listing = source[source.index('async def get_node_list'):]
        for node_id in ('DGXH3Ring4Ref2VA', 'DGXH3Ring4T2VA', 'DGXH3Ring4Status'):
            self.assertIn(node_id, listing, f"{node_id} 未注册到扩展节点列表")
            self.assertIn(node_id, source[:source.index('class ') if 'class ' in source else len(source)] or source,
                          f"{node_id} 未导入")

    # --- validation -------------------------------------------------------
    def test_ref2va_profile_allowlist_rejects_runtime_profile_name(self):
        with self.assertRaisesRegex(ValueError, '参考生视频'):
            self.graph.DGXH3Ring4Ref2VA.execute(
                reference_image=Mock(), reference_audio=Mock(), prompt='x', seed=1,
                profile='ref2va-official')

    def test_empty_prompt_is_rejected_before_any_network_call(self):
        with patch.object(self.graph, 'call') as call:
            with self.assertRaisesRegex(ValueError, '提示词'):
                self.graph.DGXH3Ring4T2VA.execute(prompt='   ', seed=1)
            call.assert_not_called()

    # --- routing ----------------------------------------------------------
    def test_text_mode_uses_fl2va_profile_and_empty_assets(self):
        row = {'job_id': 'a'*32, 'state': 'SUCCEEDED', 'memory_released': True,
               'shots': [{'name': 'ring4-shot-01', 'state': 'SUCCEEDED',
                          'artifact': {'url': 'http://192.0.2.37:18082/x'}}]}
        with patch.object(self.graph, 'submit_and_wait', return_value=row) as submit, \
             patch.object(self.graph, '_ring4_video_output', return_value='out'):
            self.graph.DGXH3Ring4T2VA.execute(prompt='A dancer turns.', seed=7)
        self.assertEqual(submit.call_args.args[0], 'video_batch_submit')
        args = submit.call_args.args[1]
        self.assertEqual(args['profile'], '10eros-fl2va-v1')
        shot = args['shots'][0]
        self.assertEqual(shot['image_asset_id'], '')
        self.assertEqual(shot['audio_asset_id'], '')
        self.assertEqual(shot['request_duration_seconds'], 4)
        self.assertEqual(shot['contract_version'], 'h3-ref2va-v1')

    def test_reference_mode_sends_image_and_audio_assets(self):
        row = {'job_id': 'b'*32, 'state': 'SUCCEEDED', 'memory_released': True,
               'shots': [{'name': 'ring4-shot-01', 'state': 'SUCCEEDED',
                          'artifact': {'url': 'http://192.0.2.37:18082/x'}}]}
        audio_file = Path(self.tmp.name) / 'ref.wav'
        audio_file.write_bytes(b'RIFF')
        Path(self.tmp.name).mkdir(parents=True, exist_ok=True)
        with patch.object(self.graph, 'register_image', return_value='c'*64), \
             patch.object(self.graph, 'stage_reference_audio', return_value=audio_file), \
             patch.object(self.graph, 'call', return_value={'asset_id': 'd'*64}), \
             patch.object(self.graph, 'submit_and_wait', return_value=row) as submit, \
             patch.object(self.graph, '_ring4_video_output', return_value='out') as out:
            self.graph.DGXH3Ring4Ref2VA.execute(
                reference_image=Mock(), reference_audio=Mock(), prompt='p', seed=3,
                profile='heretic-ref2va-v1')
        out.assert_called_once()
        args = submit.call_args.args[1]
        self.assertEqual(args['profile'], 'heretic-ref2va-v1')
        self.assertEqual(args['shots'][0]['image_asset_id'], 'c'*64)
        self.assertEqual(args['shots'][0]['audio_asset_id'], 'd'*64)

    def test_never_bypasses_gateway_with_direct_endpoint(self):
        source = Path(self.graph.__file__).read_text(encoding='utf-8')
        section = source[source.index('RING4_PROFILES'):]
        for forbidden in ('30021', '127.0.0.1:8188', 'videos/sync', 'ssh ', 'docker '):
            self.assertNotIn(forbidden, section, f"Ring4 节点不应直接使用 {forbidden}")

    # --- status -----------------------------------------------------------
    def test_status_node_is_read_only(self):
        payload = {'models': [{'model_id': 'h3-ring4', 'state': 'HEALTHY', 'profile': 'ref2va-official'}],
                   'active_local_model': {'model_id': 'h3-ring4', 'state': 'ACTIVE'},
                   'lock': {'state': 'UNLOCKED'}, 'current_operation': None,
                   'nodes': [{'hostname': 'dgx-head', 'unified_memory': {'available_bytes': 76291436544}}]}
        with patch.object(self.graph, 'call', return_value=payload) as call:
            out = self.graph.DGXH3Ring4Status.execute()
        call.assert_called_once_with('dgx_status', {})
        parsed = json.loads(out.args[0])
        self.assertEqual(parsed['h3_ring4_state'], 'HEALTHY')
        self.assertEqual(parsed['lock'], 'UNLOCKED')
        self.assertNotIn('switch', out.args[0])
