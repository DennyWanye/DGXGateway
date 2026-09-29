import asyncio
import importlib
import json
import math
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock


CUSTOM_NODES = pathlib.Path(__file__).resolve().parents[2] / "custom_nodes"
sys.path.insert(0, str(CUSTOM_NODES))


class FakeInput:
    def __init__(self, identifier, **options):
        self.id = identifier
        self.options = options


class FakeOutput(FakeInput):
    def __init__(self, identifier=None, **options):
        super().__init__(identifier, **options)


class FakeNodeOutput:
    def __init__(self, *args, ui=None):
        self.args = args
        self.ui = ui


class FakeSchema:
    def __init__(self, **values):
        self.__dict__.update(values)


class FakeSavedResult(dict):
    def __init__(self, filename, subfolder, folder_type):
        super().__init__(filename=filename, subfolder=subfolder, type=folder_type)


class FakePreviewVideo:
    def __init__(self, values):
        self.values = values


def install_fake_comfy_api():
    latest = types.ModuleType("comfy_api.latest")
    latest.ComfyExtension = type("ComfyExtension", (), {})
    latest.InputImpl = types.SimpleNamespace(VideoFromFile=lambda path: ("video", path))
    latest.io = types.SimpleNamespace(
        ComfyNode=type("ComfyNode", (), {}),
        Schema=FakeSchema,
        String=types.SimpleNamespace(Input=FakeInput, Output=FakeOutput),
        Combo=types.SimpleNamespace(Input=FakeInput),
        Int=types.SimpleNamespace(Input=FakeInput),
        Image=types.SimpleNamespace(Input=FakeInput, Output=FakeOutput),
        Audio=types.SimpleNamespace(Input=FakeInput),
        Video=types.SimpleNamespace(Output=FakeOutput),
        NodeOutput=FakeNodeOutput,
        FolderType=types.SimpleNamespace(output="output"),
    )
    latest.ui = types.SimpleNamespace(SavedResult=FakeSavedResult, PreviewVideo=FakePreviewVideo)
    comfy_api = types.ModuleType("comfy_api")
    comfy_api.latest = latest
    sys.modules["comfy_api"] = comfy_api
    sys.modules["comfy_api.latest"] = latest

    folder_paths = types.ModuleType("folder_paths")
    folder_paths.get_output_directory = lambda: "/unused-output"
    sys.modules["folder_paths"] = folder_paths

    class FakeRoutes:
        def get(self, _path):
            return lambda function: function

        def post(self, _path):
            return lambda function: function

    aiohttp = types.ModuleType("aiohttp")
    aiohttp.web = types.SimpleNamespace(json_response=lambda value, status=200: (status, value))
    server = types.ModuleType("server")
    server.PromptServer = types.SimpleNamespace(
        instance=types.SimpleNamespace(routes=FakeRoutes())
    )
    sys.modules["aiohttp"] = aiohttp
    sys.modules["server"] = server


install_fake_comfy_api()
for module_name in ("dgx_remote_h3", "dgx_remote_h3.node"):
    sys.modules.pop(module_name, None)
node_package = importlib.import_module("dgx_remote_h3")
node_module = importlib.import_module("dgx_remote_h3.node")
profile_routes = importlib.import_module("dgx_remote_h3.profile_routes")


class NodeTests(unittest.TestCase):
    def test_defines_exact_modern_node_schema(self):
        schema = node_module.DGXRemoteMiniMaxH3T2VA.define_schema()
        self.assertEqual(schema.node_id, "DGXRemoteMiniMaxH3T2VA")
        self.assertEqual(schema.display_name, "DGX Remote MiniMax H3 T2VA")
        self.assertTrue(schema.is_output_node)
        self.assertEqual([item.id for item in schema.inputs], ["prompt", "resolution", "duration", "seed"])
        self.assertEqual(schema.inputs[0].options["multiline"], True)
        self.assertEqual(schema.inputs[1].options["options"], ["768x448", "1344x768"])
        self.assertEqual(schema.inputs[2].options["min"], 2)
        self.assertEqual(schema.inputs[2].options["max"], 4)
        self.assertEqual(schema.inputs[3].options["min"], 0)
        self.assertEqual(schema.inputs[3].options["max"], 2_147_483_647)
        self.assertEqual(len(schema.outputs), 1)

    def test_always_invalidates_executor_cache(self):
        fingerprint = node_module.DGXRemoteMiniMaxH3T2VA.fingerprint_inputs(
            prompt="same", resolution="768x448", duration=2, seed=1
        )
        self.assertTrue(math.isnan(fingerprint))

    def test_returns_official_video_and_preview_for_published_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory)
            published = output / "generated.mp4"
            published.write_bytes(b"video")
            node_module.folder_paths.get_output_directory = lambda: str(output)
            with (
                mock.patch.object(node_module, "load_runtime_contract", return_value="contract") as load,
                mock.patch.object(node_module, "execute_request", return_value=published) as execute,
            ):
                result = node_module.DGXRemoteMiniMaxH3T2VA.execute(
                    "prompt", "768x448", 2, 9
                )
            load.assert_called_once_with(expected_profile="production")
            self.assertEqual(execute.call_args.kwargs["output_directory"], str(output))
            self.assertEqual(result.args, (("video", str(published.resolve())),))
            self.assertEqual(result.ui.values, [{"filename": "generated.mp4", "subfolder": "", "type": "output"}])

    def test_registers_creative_and_remote_nodes_through_comfy_entrypoint(self):
        extension = asyncio.run(node_package.comfy_entrypoint())
        self.assertEqual(
            asyncio.run(extension.get_node_list()),
            [
                node_module.DGXZImageBatch,
                node_module.DGXDramaShotDraft,
                node_module.DGXRemoteMiniMaxH3HMNSFWControls,
                node_module.DGXQwenImageEditBatch,
                node_module.DGXQwenImageEditHuihui,
                node_module.DGXQwenImageEdit,
                node_module.DGXH3Video,
                node_module.DGXCreativeTool,
                node_module.DGXRemoteMiniMaxH3ProfileManager,
                node_module.DGXRemoteMiniMaxH3T2VA,
                node_module.DGXRemoteMiniMaxH3FL2VA,
                node_module.DGXRemoteMiniMaxH3Ref2VA,
            ],
        )

    def test_profile_manager_has_three_public_profiles_and_explicit_action(self):
        schema = node_module.DGXRemoteMiniMaxH3ProfileManager.define_schema()
        self.assertEqual(schema.node_id, "DGXRemoteMiniMaxH3ProfileManager")
        self.assertEqual([item.id for item in schema.inputs], ["profile", "action"])
        self.assertEqual(
            schema.inputs[0].options["options"],
            ["heretic-ref2va-v1", "heretic-ref2va-hmnsfw-v1", "10eros-fl2va-v1"],
        )
        self.assertEqual(schema.inputs[1].options["options"], ["status", "switch", "stop"])

    def test_profile_routes_require_explicit_header_and_sanitize_status(self):
        request = types.SimpleNamespace(
            headers={"X-DGX-H3-Action": "explicit-user-action"},
            content_length=64,
            json=mock.AsyncMock(return_value={"action": "switch", "profile": "heretic-ref2va-v1"}),
        )
        raw = {
            "state": "READY",
            "profile": "heretic-ref2va-v1",
            "healthy": True,
            "profile_identity": {"name": "heretic-ref2va-v1"},
            "secret_path": "/forbidden",
        }
        with mock.patch.object(profile_routes, "profile_action", return_value=raw):
            status, payload = asyncio.run(profile_routes.post_profile_action(request))
        self.assertEqual(status, 200)
        self.assertNotIn("secret_path", payload)

        request.headers = {}
        status, payload = asyncio.run(profile_routes.post_profile_action(request))
        self.assertEqual((status, payload), (403, {"error": "action_header_required"}))

    def test_fl2va_node_has_editable_prompt_and_connectable_first_frame(self):
        schema = node_module.DGXRemoteMiniMaxH3FL2VA.define_schema()
        self.assertEqual(schema.node_id, "DGXRemoteMiniMaxH3FL2VA")
        self.assertEqual([item.id for item in schema.inputs], ["first_frame", "prompt", "resolution", "duration", "seed"])
        self.assertEqual(schema.inputs[0].options, {})
        self.assertTrue(schema.inputs[1].options["multiline"])

    def test_ref2va_node_has_connectable_image_editable_prompt_and_fixed_video_contract(self):
        schema = node_module.DGXRemoteMiniMaxH3Ref2VA.define_schema()
        self.assertEqual(schema.node_id, "DGXRemoteMiniMaxH3Ref2VA")
        self.assertEqual([item.id for item in schema.inputs], ["reference_image", "reference_audio", "profile", "prompt", "seed"])
        self.assertEqual(schema.inputs[0].options, {})
        self.assertEqual(schema.inputs[1].options, {})
        self.assertEqual(schema.inputs[2].options["options"], ["heretic-ref2va-v1", "heretic-ref2va-hmnsfw-v1"])
        self.assertTrue(schema.inputs[2].options["optional"])
        self.assertTrue(schema.inputs[3].options["multiline"])
        self.assertFalse(schema.inputs[3].options["dynamic_prompts"])
        self.assertEqual(schema.inputs[4].options["min"], 0)
        self.assertEqual(schema.inputs[4].options["max"], 2_147_483_647)
        self.assertEqual(len(schema.outputs), 1)

    def test_ref2va_static_validation_accepts_an_unevaluated_linked_image(self):
        self.assertIs(
            node_module.DGXRemoteMiniMaxH3Ref2VA.validate_inputs(
                reference_image=None,
                reference_audio=None,
                profile="heretic-ref2va-v1",
                prompt="A valid editable movie prompt.",
                seed=7,
            ),
            True,
        )

    def test_ref2va_legacy_workflow_defaults_missing_profile_to_stable(self):
        self.assertIs(
            node_module.DGXRemoteMiniMaxH3Ref2VA.validate_inputs(
                reference_image=None,
                reference_audio=None,
                prompt="A valid prompt from a workflow saved before profile selection existed.",
                seed=7,
            ),
            True,
        )

    def test_ref2va_node_stages_image_and_returns_official_video_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory)
            published = output / "ref-generated.mp4"
            published.write_bytes(b"video")
            node_module.folder_paths.get_output_directory = lambda: str(output)
            with (
                mock.patch.object(node_module, "load_runtime_contract", return_value="contract"),
                mock.patch.object(node_module, "execute_ref2va_request", return_value=published) as execute,
            ):
                result = node_module.DGXRemoteMiniMaxH3Ref2VA.execute(
                    "image-tensor", "audio-tensor", "heretic-ref2va-v1", "editable prompt", 7
                )
            self.assertEqual(execute.call_args.args[1], "image-tensor")
            self.assertEqual(execute.call_args.args[2], "audio-tensor")
            self.assertEqual(execute.call_args.kwargs["output_directory"], str(output))
            self.assertEqual(execute.call_args.kwargs["required_profile"], "heretic-ref2va-v1")
            self.assertEqual(result.args, (("video", str(published.resolve())),))

    def test_profile_is_selected_by_an_immutable_build_module(self):
        self.assertEqual(node_module.EXPECTED_PROFILE, "production")

    def test_ref2va_workflow_connects_load_image_and_serializes_no_secret(self):
        workflow_path = pathlib.Path(__file__).resolve().parents[2] / "workflows/dgx-remote-h3-ref2va.json"
        workflow = json.loads(workflow_path.read_text())
        self.assertEqual(
            workflow["links"],
            [[1, 1, 0, 3, 0, "IMAGE"], [2, 2, 0, 3, 1, "AUDIO"]],
        )
        ref = next(node for node in workflow["nodes"] if node["type"] == "DGXRemoteMiniMaxH3Ref2VA")
        self.assertEqual(ref["inputs"][0]["name"], "reference_image")
        self.assertEqual(ref["inputs"][1]["name"], "reference_audio")
        load_image = next(node for node in workflow["nodes"] if node["type"] == "LoadImage")
        self.assertEqual(
            load_image["widgets_values"][0],
            "ref2va-character-yellow-raincoat.png",
        )
        self.assertEqual(ref["widgets_values"][3], "heretic-ref2va-v1")
        self.assertIsInstance(ref["widgets_values"][0], str)
        self.assertGreater(len(ref["widgets_values"][0]), 20)
        self.assertIsInstance(ref["widgets_values"][1], int)
        self.assertEqual(ref["widgets_values"][2], "fixed")
        serialized = json.dumps(workflow).lower()
        for forbidden in ("token", "password", "ssh", "lock_attestation"):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
