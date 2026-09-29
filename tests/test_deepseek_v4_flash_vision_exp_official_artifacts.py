import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/manage-deepseek-v4-flash-vision-exp-official.py"
SPEC = importlib.util.spec_from_file_location("official_artifacts", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


def git_blob(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


class OfficialArtifactTests(unittest.TestCase):
    def test_committed_manifest_is_pinned_and_exact(self):
        manifest = MODULE.load_manifest(
            ROOT / "manifests/deepseek-v4-flash-vision-exp-official-exact.json"
        )
        self.assertEqual(manifest["file_count"], 84)
        self.assertEqual(manifest["total_bytes"], 167831848791)
        self.assertEqual(len(manifest["reuse"]["paths"]), 55)
        self.assertEqual(manifest["declared_license"], "mit")
        self.assertEqual(manifest["pipeline_tag"], "image-text-to-text")
        self.assertEqual(
            len([item for item in manifest["files"] if item["path"].endswith(".safetensors")]),
            48,
        )

    def test_verify_rejects_extra_symlink_and_digest_mismatch(self):
        data = b"native vision closure"
        manifest = {"files": [{"path": "nested/file.txt", "size": len(data),
                               "digest_type": "gitsha1", "digest": git_blob(data)}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "nested").mkdir()
            target = root / "nested/file.txt"
            target.write_bytes(data)
            self.assertEqual(len(MODULE.verify_tree(root, manifest)), 64)
            (root / "extra").write_text("x")
            with self.assertRaises(SystemExit):
                MODULE.verify_tree(root, manifest)
            (root / "extra").unlink()
            target.write_bytes(b"wrong")
            with self.assertRaises(SystemExit):
                MODULE.verify_tree(root, manifest)
            target.unlink()
            target.symlink_to("missing")
            with self.assertRaises(SystemExit):
                MODULE.verify_tree(root, manifest)

    def test_seed_reverifies_source_and_never_hardlinks(self):
        data = b"same object"
        item = {"path": "model.bin", "size": len(data), "digest_type": "sha256",
                "digest": hashlib.sha256(data).hexdigest()}
        manifest = {"files": [item], "reuse": {"paths": ["model.bin"]}}
        original_rename = MODULE.rename_noreplace
        MODULE.rename_noreplace = lambda source, destination: source.rename(destination)
        try:
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                source, staging = base / "source", base / "staging"
                source.mkdir()
                (source / "model.bin").write_bytes(data)
                self.assertEqual(MODULE.seed(manifest, source, staging), 1)
                self.assertEqual((staging / "model.bin").read_bytes(), data)
                self.assertNotEqual((source / "model.bin").stat().st_ino,
                                    (staging / "model.bin").stat().st_ino)
                (source / "model.bin").write_bytes(b"corrupt")
                (staging / "model.bin").unlink()
                with self.assertRaises(SystemExit):
                    MODULE.seed(manifest, source, staging)
        finally:
            MODULE.rename_noreplace = original_rename


if __name__ == "__main__":
    unittest.main()
