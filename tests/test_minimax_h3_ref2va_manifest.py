from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "manifests/minimax-h3-ref2va-minimal-lock.json"
HEX64 = re.compile(r"[0-9a-f]{64}")
REVISION = "42ed227ee7df40d41602854ae760620d6eb651fe"


class Ref2VAManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = json.loads(LOCK.read_text(encoding="utf-8"))

    def test_pins_repository_revision_and_metadata_identity(self) -> None:
        self.assertEqual(1, self.document["schema_version"])
        self.assertEqual("MiniMaxAI/MiniMax-H3", self.document["repository"])
        self.assertEqual(REVISION, self.document["revision"])
        source = self.document["source_metadata"]
        self.assertEqual("huggingface-model-api-blobs", source["kind"])
        self.assertEqual(
            "a6c87943e580986a784d15fc538e3bcc76db658e02e1276b5b43de00ab5ba94e",
            source["canonical_sha256"],
        )
        self.assertRegex(source["canonical_sha256"], HEX64)

    def test_minimal_closure_is_exact_and_normalized(self) -> None:
        files = self.document["files"]
        self.assertEqual(16, self.document["file_count"])
        self.assertEqual(16, len(files))
        paths = [entry["path"] for entry in files]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(len(paths), len(set(paths)))
        for path in paths:
            parsed = PurePosixPath(path)
            self.assertFalse(parsed.is_absolute())
            self.assertNotIn("..", parsed.parts)
            self.assertTrue(path.startswith("Ref2VA/"))
            self.assertNotIn("?", path)
            self.assertNotIn("\\", path)
        self.assertEqual(
            {
                "Ref2VA/model_index.json",
                "Ref2VA/transformer/config.json",
                "Ref2VA/transformer/model.safetensors.index.json",
            },
            {path for path in paths if path.endswith(".json")},
        )
        shards = [path for path in paths if path.endswith(".safetensors")]
        self.assertEqual(13, len(shards))
        self.assertEqual(
            [f"Ref2VA/transformer/model-{index:05d}-of-00013.safetensors" for index in range(1, 14)],
            shards,
        )

    def test_every_file_has_exact_size_and_content_sha256(self) -> None:
        files = self.document["files"]
        self.assertEqual(66_280_525_570, self.document["total_size_bytes"])
        self.assertEqual(self.document["total_size_bytes"], sum(entry["size"] for entry in files))
        for entry in files:
            self.assertIsInstance(entry["size"], int)
            self.assertGreater(entry["size"], 0)
            self.assertRegex(entry["sha256"], HEX64)
            self.assertIn(entry["source"]["kind"], {"git-blob-sha1", "lfs-sha256"})
            self.assertEqual(entry["size"], entry["source"]["size"])

    def test_tree_identity_is_canonical_file_tuple_hash(self) -> None:
        rows = [
            (entry["path"], entry["size"], entry["sha256"])
            for entry in self.document["files"]
        ]
        digest = hashlib.sha256(
            json.dumps(rows, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        self.assertEqual(digest, self.document["tree_sha256"])


if __name__ == "__main__":
    unittest.main()
