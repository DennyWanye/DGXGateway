import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/minimax-h3-two-node/download-ref2va-artifacts.py"
SPEC = importlib.util.spec_from_file_location("h3_ref2va_download", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class Ref2VADownloadTests(unittest.TestCase):
    def test_locked_manifest_generates_pinned_checksum_plan(self):
        manifest = MODULE.load_manifest(ROOT / "manifests/minimax-h3-ref2va-minimal-lock.json")
        text = MODULE.aria2_input(manifest, Path("/home/operator/dgx-models/video-generation/downloads/ref2va"))
        self.assertEqual(text.count("https://huggingface.co/MiniMaxAI/MiniMax-H3/resolve/42ed227"), 16)
        self.assertEqual(text.count("checksum=sha-256="), 16)
        self.assertIn("out=.model-00001-of-00013.safetensors.part", text)
        self.assertNotIn("token", text.lower())

    def test_manifest_rejects_traversal(self):
        original = json.loads((ROOT / "manifests/minimax-h3-ref2va-minimal-lock.json").read_text())
        original["files"][0]["path"] = "Ref2VA/../escape"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "unsafe.json"
            path.write_text(json.dumps(original))
            with self.assertRaisesRegex(MODULE.DownloadError, "unsafe path"):
                MODULE.load_manifest(path)

    def test_destination_is_fail_closed(self):
        with self.assertRaisesRegex(MODULE.DownloadError, "outside"):
            MODULE.validate_destination(Path("/tmp/ref2va"))
        expected = Path("/home/operator/dgx-models/video-generation/downloads/ref2va")
        self.assertEqual(MODULE.validate_destination(expected), expected)

    def test_concurrency_cap_is_declared(self):
        source = SCRIPT.read_text()
        self.assertIn("arguments.concurrency > 4", source)
        self.assertIn('subprocess.run([str(egress_script), "remove"]', source)
        self.assertIn('parser.add_argument("--signed-input"', source)
        self.assertIn('mount --bind "$1" /etc/hosts', source)


if __name__ == "__main__":
    unittest.main()
