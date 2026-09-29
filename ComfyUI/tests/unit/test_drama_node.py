from importlib.machinery import PathFinder
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "custom_nodes"))
from dgx_remote_h3.drama_client import build_canvas_draft


class DramaDraftContractTests(unittest.TestCase):
    def arguments(self):
        return {"shotId": "shot-a", "expectedRevision": 7, "baseRevisionId": "revision-original",
                "templateVersion": "dgx-drama-shot-v1", "title": "门口回头", "action": "停步回头",
                "framing": "medium", "camera": "locked", "prompt": "雨夜门口", "targetEditFrames": 72,
                "snapshot_json": json.dumps({"title": "旧名", "characterRevisionIds": ["character-r2"],
                    "locationRevisionId": "location-r4", "approvedReferenceAssetId": "asset-original",
                    "propRevisionIds": ["prop-r1"], "costumeRevisionIds": []})}

    def test_edit_preserves_exact_dependency_and_base_identity(self):
        args = self.arguments()
        old_snapshot = args["snapshot_json"]
        result = build_canvas_draft(**args)
        self.assertEqual(result["expectedRevision"], 7)
        self.assertEqual(result["baseRevisionId"], "revision-original")
        self.assertEqual(result["snapshot"]["characterRevisionIds"], ["character-r2"])
        self.assertEqual(result["snapshot"]["locationRevisionId"], "location-r4")
        self.assertEqual(result["snapshot"]["approvedReferenceAssetId"], "asset-original")
        self.assertEqual(result["snapshot"]["title"], "门口回头")
        self.assertEqual(result["snapshot"]["targetEditFrames"], 72)
        self.assertEqual(args["snapshot_json"], old_snapshot)

    def test_invalid_identity_does_not_make_portable_unbound_draft(self):
        for key, value in [("shotId", ""), ("baseRevisionId", ""), ("templateVersion", "other-template"),
                           ("expectedRevision", True), ("expectedRevision", 0), ("targetEditFrames", 1.5)]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                build_canvas_draft(**{**self.arguments(), key: value})

    def test_invalid_snapshot_cannot_silently_drop_dependencies(self):
        for snapshot in ("[]", "null", "not json"):
            with self.subTest(snapshot=snapshot), self.assertRaises(ValueError):
                build_canvas_draft(**{**self.arguments(), "snapshot_json": snapshot})

    def test_blank_title_is_rejected(self):
        with self.assertRaises(ValueError):
            build_canvas_draft(**{**self.arguments(), "title": " "})


@unittest.skipUnless(PathFinder.find_spec("comfy_api", sys.path), "Requires the installed ComfyUI runtime")
class DramaNodeRuntimeTests(unittest.TestCase):
    arguments = DramaDraftContractTests.arguments
    def test_execution_has_no_network_side_effect_and_emits_draft(self):
        # Other unit modules install fake comfy_api modules while being collected.
        # A clean interpreter must exercise the real NodeOutput, regardless of order.
        script = """
import json
import sys
from unittest.mock import patch
request = json.load(sys.stdin)
sys.path[:] = request["paths"]
from comfy_api.latest import io
from dgx_remote_h3.drama_node import DGXDramaShotDraft
with patch("urllib.request.urlopen", side_effect=AssertionError("Draft node must never perform HTTP")):
    output = DGXDramaShotDraft.execute(**request["arguments"])
assert isinstance(output, io.NodeOutput)
assert io.NodeOutput.__name__ != "FakeNodeOutput"
print(json.dumps(json.loads(output.result[0]), ensure_ascii=False))
"""
        result = subprocess.run([sys.executable, "-c", script],
                                input=json.dumps({"paths": sys.path, "arguments": self.arguments()}),
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(parsed["baseRevisionId"], "revision-original")
        self.assertNotIn("requestKey", parsed)
        self.assertEqual(parsed["snapshot"]["characterRevisionIds"], ["character-r2"])


if __name__ == "__main__":
    unittest.main()
