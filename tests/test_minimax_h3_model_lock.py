from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts/minimax-h3-two-node/lock-model.py"
SPEC = importlib.util.spec_from_file_location("minimax_h3_lock_model", MODULE_PATH)
assert SPEC and SPEC.loader
lock_model = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lock_model)


def git_blob_sha1(payload: bytes) -> str:
    header = f"blob {len(payload)}\0".encode()
    return hashlib.sha1(header + payload).hexdigest()


class ModelLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "snapshot"
        self.root.mkdir()
        (self.root / "FL2VA").mkdir()
        self.small = b'{"model":"h3"}\n'
        self.large = b"weights-not-a-pointer"
        (self.root / "FL2VA/config.json").write_bytes(self.small)
        (self.root / "FL2VA/model.safetensors").write_bytes(self.large)
        self.queue = [
            {
                "repo": "MiniMaxAI/MiniMax-H3",
                "revision": "42ed227ee7df40d41602854ae760620d6eb651fe",
                "path": "FL2VA/config.json",
            },
            {
                "repo": "MiniMaxAI/MiniMax-H3",
                "revision": "42ed227ee7df40d41602854ae760620d6eb651fe",
                "path": "FL2VA/model.safetensors",
            },
            {"repo": "someone/else", "revision": "main", "path": "ignored.bin"},
        ]
        self.metadata = {
            "id": "MiniMaxAI/MiniMax-H3",
            "sha": "42ed227ee7df40d41602854ae760620d6eb651fe",
            "siblings": [
                {
                    "rfilename": "FL2VA/config.json",
                    "size": len(self.small),
                    "blobId": git_blob_sha1(self.small),
                },
                {
                    "rfilename": "FL2VA/model.safetensors",
                    "size": len(self.large),
                    "blobId": "pointer-blob-id-is-not-content-identity",
                    "lfs": {"size": len(self.large), "sha256": hashlib.sha256(self.large).hexdigest()},
                },
            ],
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_inventory_rejects_partial_symlink_and_extra_file(self) -> None:
        expected = {"FL2VA/config.json", "FL2VA/model.safetensors"}
        self.assertEqual(sorted(expected), sorted(lock_model.inventory(self.root, expected)))

        partial = self.root / "FL2VA/model.safetensors.part"
        partial.write_bytes(b"partial")
        with self.assertRaisesRegex(lock_model.LockError, "partial"):
            lock_model.inventory(self.root, expected)
        partial.unlink()

        link = self.root / "FL2VA/link"
        link.symlink_to("config.json")
        with self.assertRaisesRegex(lock_model.LockError, "symlink"):
            lock_model.inventory(self.root, expected | {"FL2VA/link"})
        link.unlink()

        extra = self.root / "FL2VA/extra.safetensors"
        extra.write_bytes(b"extra")
        with self.assertRaisesRegex(lock_model.LockError, "unexpected"):
            lock_model.inventory(self.root, expected)

    def test_generate_and_verify_lock_uses_source_identities(self) -> None:
        license_file = Path(self.temp.name) / "license-ack.txt"
        license_file.write_text("user accepted MiniMax H3 Community License\n")
        output = Path(self.temp.name) / "model-lock.json"

        lock_model.generate_lock(
            root=self.root,
            queue=self.queue,
            metadata=self.metadata,
            output=output,
            repo="MiniMaxAI/MiniMax-H3",
            revision="42ed227ee7df40d41602854ae760620d6eb651fe",
            head_path="/models/minimax-h3-official",
            worker_path="/models/minimax-h3-official",
            license_evidence=license_file,
            generated_at="2026-08-30T00:00:00Z",
        )

        document = json.loads(output.read_text())
        self.assertEqual("resolved", document["status"])
        self.assertEqual(2, document["file_count"])
        self.assertEqual("git-blob-sha1", document["files"][0]["source"]["kind"])
        self.assertEqual("lfs-sha256", document["files"][1]["source"]["kind"])
        report = lock_model.verify_lock(output, self.root, "/models/minimax-h3-official")
        self.assertTrue(report["passed"])
        self.assertEqual(2, report["verified_files"])

    def test_generate_fails_when_metadata_or_bytes_do_not_match(self) -> None:
        license_file = Path(self.temp.name) / "license-ack.txt"
        license_file.write_text("accepted\n")
        self.metadata["siblings"][0]["blobId"] = "0" * 40
        with self.assertRaisesRegex(lock_model.LockError, "source identity"):
            lock_model.generate_lock(
                root=self.root,
                queue=self.queue,
                metadata=self.metadata,
                output=Path(self.temp.name) / "lock.json",
                repo="MiniMaxAI/MiniMax-H3",
                revision="42ed227ee7df40d41602854ae760620d6eb651fe",
                head_path="/models/h3",
                worker_path="/models/h3",
                license_evidence=license_file,
                generated_at="2026-08-30T00:00:00Z",
            )

    def test_verify_rejects_changed_bytes_and_wrong_absolute_path(self) -> None:
        license_file = Path(self.temp.name) / "license-ack.txt"
        license_file.write_text("accepted\n")
        output = Path(self.temp.name) / "lock.json"
        lock_model.generate_lock(
            root=self.root,
            queue=self.queue,
            metadata=self.metadata,
            output=output,
            repo="MiniMaxAI/MiniMax-H3",
            revision="42ed227ee7df40d41602854ae760620d6eb651fe",
            head_path="/models/h3",
            worker_path="/models/h3",
            license_evidence=license_file,
            generated_at="2026-08-30T00:00:00Z",
        )
        (self.root / "FL2VA/config.json").write_bytes(b"changed")
        with self.assertRaisesRegex(lock_model.LockError, "mismatch"):
            lock_model.verify_lock(output, self.root, "/models/h3")
        with self.assertRaisesRegex(lock_model.LockError, "absolute path"):
            lock_model.verify_lock(output, self.root, "/wrong/path")


if __name__ == "__main__":
    unittest.main()
