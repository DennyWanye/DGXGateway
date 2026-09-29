from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/minimax-h3-two-node/prepare-ref2va-profile.py"


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class Ref2VAProfileBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fl2va = self.root / "heretic-fl2va"
        self.download = self.root / "download"
        self.destination = self.root / "profile"
        self.lock = self.root / "ref2va-lock.json"

        for component in ("audio_vae", "processor", "text_encoder", "tokenizer", "video_vae"):
            component_root = self.fl2va / component
            component_root.mkdir(parents=True)
            (component_root / "config.bin").write_bytes(f"{component}-heretic".encode())
        (self.fl2va / "transformer").mkdir()
        (self.fl2va / "transformer/ignored.bin").write_bytes(b"fl2va-transformer")
        (self.fl2va / "model_index.json").write_text("{}\n")

        ref_files = {
            "Ref2VA/model_index.json": b'{"_minimax_h3":{"partition":"ref2va"}}\n',
            "Ref2VA/transformer/config.json": b'{"_class_name":"MiniMaxH3DiTModel"}\n',
            "Ref2VA/transformer/model-00001-of-00001.safetensors": b"ref2va-weights",
            "Ref2VA/transformer/model.safetensors.index.json": b'{"weight_map":{}}\n',
        }
        entries = []
        for relative, payload in sorted(ref_files.items()):
            path = self.download / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            entries.append({
                "path": relative,
                "size": len(payload),
                "sha256": digest(payload),
                "source": {"kind": "fixture", "size": len(payload)},
            })
        rows = [(row["path"], row["size"], row["sha256"]) for row in entries]
        self.lock.write_text(json.dumps({
            "schema_version": 1,
            "repository": "MiniMaxAI/MiniMax-H3",
            "revision": "42ed227ee7df40d41602854ae760620d6eb651fe",
            "file_count": len(entries),
            "total_size_bytes": sum(row["size"] for row in entries),
            "tree_sha256": digest(json.dumps(rows, separators=(",", ":")).encode()),
            "files": entries,
        }))

    def run_builder(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "python3", str(SCRIPT),
                "--fl2va-root", str(self.fl2va),
                "--ref2va-download-root", str(self.download),
                "--artifact-lock", str(self.lock),
                "--destination", str(self.destination),
            ],
            capture_output=True,
            text=True,
        )

    def test_builds_atomic_flat_profile_for_the_qualified_runtime(self) -> None:
        result = self.run_builder()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue((self.destination / "profile.json").is_file())
        for component in ("audio_vae", "processor", "text_encoder", "tokenizer", "video_vae"):
            source = self.fl2va / component / "config.bin"
            target = self.destination / "Ref2VA" / component / "config.bin"
            self.assertEqual(source.read_bytes(), target.read_bytes())
            self.assertEqual(source.stat().st_ino, target.stat().st_ino)
        self.assertFalse((self.destination / "FL2VA").exists())
        self.assertEqual(
            (self.download / "Ref2VA/model_index.json").read_bytes(),
            (self.destination / "Ref2VA/model_index.json").read_bytes(),
        )
        profile = json.loads((self.destination / "profile.json").read_text())
        self.assertEqual("heretic-h3", profile["text_conditioning"])
        self.assertEqual("official", profile["ref2va_transformer"])
        self.assertFalse(profile["fl2va_lora_applied"])
        self.assertEqual("ref2va", profile["task_type"])
        self.assertEqual("flat-vllm-omni-0.26", profile["layout"])
        for key in ("shared_tree_sha256", "ref2va_tree_sha256", "profile_tree_sha256"):
            self.assertRegex(profile[key], r"^[0-9a-f]{64}$")

    def test_refuses_existing_destination_without_mutating_it(self) -> None:
        self.destination.mkdir()
        marker = self.destination / "owned.txt"
        marker.write_text("preserve")
        result = self.run_builder()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("destination already exists", result.stderr)
        self.assertEqual("preserve", marker.read_text())

    def test_rejects_symlink_and_unexpected_ref2va_file(self) -> None:
        link = self.fl2va / "processor/link"
        link.symlink_to("config.bin")
        result = self.run_builder()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("symlink", result.stderr)
        link.unlink()

        (self.download / "Ref2VA/extra.bin").write_bytes(b"not locked")
        result = self.run_builder()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("unexpected", result.stderr)

    def test_rejects_locked_byte_mismatch_without_publishing_partial_profile(self) -> None:
        target = self.download / "Ref2VA/transformer/config.json"
        target.write_bytes(b"changed")
        result = self.run_builder()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("mismatch", result.stderr)
        self.assertFalse(self.destination.exists())
        self.assertEqual([], list(self.root.glob(".profile.tmp-*")))


if __name__ == "__main__":
    unittest.main()
