"""Local tests of our scripts. These do not validate CUDA, model quality, or TP4."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

client = load("client", "04-generate.py")
downloader = load("downloader", "01-download-models.py")

class PlanTests(unittest.TestCase):
    def test_original_and_turbo_schedule(self):
        for profile, steps in [("q", 50), ("u-q", 50), ("p", 9), ("u-p", 9)]:
            p = client.make_payload(profile, "quiet scene", 5, 768, 1)
            self.assertEqual(p["num_inference_steps"], steps)
            self.assertEqual(p["model"], "h3-fl2va-" + profile)
            self.assertEqual(p["quality"], "lossless")
            self.assertEqual(p["task"], "t2va")
    def test_first_last_conditions(self):
        p = client.make_payload("q", "move naturally", 5, 768, 1,
                                "/srv/h3/input/first.png", "/srv/h3/input/last.png")
        self.assertEqual([c["frame_index"] for c in p["conditions"]], [0, -1])
        self.assertEqual(p["task"], "fl2va")
        self.assertEqual(p["target"]["aspect_ratio"], "auto")
    def test_relative_condition_path_rejected(self):
        with self.assertRaises(ValueError):
            client.make_payload("q", "test", 5, 768, 1, "first.png")
    def test_invalid_duration_rejected(self):
        with self.assertRaises(ValueError):
            client.make_payload("q", "test", 3, 768, 1)
    def test_do_not_download_ref2va_or_whole_turbo_repo(self):
        self.assertTrue(downloader.wanted("base", "FL2VA/text_encoder/model-00001.safetensors"))
        self.assertFalse(downloader.wanted("base", "Ref2VA/transformer/model.safetensors"))
        self.assertFalse(downloader.wanted("base", "transformer/diffusion_pytorch_model.safetensors"))
        self.assertTrue(downloader.wanted("turbo", "minimax_h3_turbo_v4_step600_ema.safetensors"))
        self.assertFalse(downloader.wanted("turbo", "checkpoint_123/optimizer.bin"))
        self.assertFalse(downloader.wanted("heretic", "checkpoint-100/model.safetensors"))
    def test_path_traversal_rejected(self):
        with self.assertRaises(ValueError):
            downloader.wanted("base", "../outside.safetensors")
    def test_failed_job_records_id_without_downloading(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "failed.mp4"
            payload = client.make_payload("q", "test", 5, 768, 1)
            with patch.object(client.shutil, "which", return_value="/usr/bin/stub"), \
                 patch.object(client, "json_request", side_effect=[{"id": "job-7"},
                     {"id": "job-7", "status": "failed", "error": "test error"}]):
                with self.assertRaises(RuntimeError):
                    client.run_job("http://localhost:30010", payload, out, poll_s=0.01)
            self.assertFalse(out.exists())
            saved = json.loads(out.with_suffix(".job.json").read_text())
            self.assertEqual(saved["id"], "job-7")
            self.assertIn("client_error", saved)
    def test_existing_output_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "already.mp4"
            out.write_bytes(b"keep")
            with patch.object(client.shutil, "which", return_value="/usr/bin/stub"):
                with self.assertRaises(FileExistsError):
                    client.run_job("http://localhost", {}, out)
            self.assertEqual(out.read_bytes(), b"keep")

if __name__ == "__main__":
    unittest.main()
