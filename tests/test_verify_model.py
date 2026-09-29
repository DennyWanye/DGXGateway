import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
VERIFIER = REPO_ROOT / "scripts" / "verify-model.py"
DOWNLOAD_UNIT = REPO_ROOT / "systemd" / "qwen38-q4-download.service"
PREFIX = "Qwen3.8-27B-Uncensored-HauhauCS-Aggressive"


class VerifyModelTests(unittest.TestCase):
    def run_verifier(self, model_root, *args):
        return subprocess.run(
            [sys.executable, str(VERIFIER), "--root", str(model_root), *args],
            check=False,
            capture_output=True,
            text=True,
        )

    def prepare(self, model_root, quant, payload, expected_payload=None):
        name = f"{PREFIX}-{quant}.gguf"
        (model_root / name).write_bytes(payload)
        expected_payload = payload if expected_payload is None else expected_payload
        manifest = {
            "artifacts": {
                name: {
                    "bytes": len(expected_payload),
                    "sha256": hashlib.sha256(expected_payload).hexdigest(),
                }
            }
        }
        (model_root / "HauhauCS-RELEASE-MANIFEST.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        return name

    def test_correct_q4(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            name = self.prepare(root, "Q4_K_P", b"verified q4")
            result = self.run_verifier(root, "--quant", "Q4_K_P")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"verified {name}", result.stdout)

    def test_wrong_size(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self.prepare(root, "Q4_K_P", b"short", b"expected payload")
            result = self.run_verifier(root, "--quant", "Q4_K_P")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("size mismatch", result.stderr)

    def test_wrong_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self.prepare(root, "Q4_K_P", b"wrong!", b"right!")
            result = self.run_verifier(root, "--quant", "Q4_K_P")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("sha256 mismatch", result.stderr)

    def test_unknown_quant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            result = self.run_verifier(root, "--quant", "Q3_UNKNOWN")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid choice", result.stderr)

    def test_q8_is_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            name = self.prepare(root, "Q8_K_P", b"verified q8")
            result = self.run_verifier(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"verified {name}", result.stdout)

    def test_download_unit_is_manual_and_retries_only_failures(self):
        unit = DOWNLOAD_UNIT.read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertNotIn("WantedBy=", unit)


if __name__ == "__main__":
    unittest.main()
