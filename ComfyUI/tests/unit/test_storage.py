import pathlib
import os
import stat
import sys
import tempfile
import unittest


CUSTOM_NODES = pathlib.Path(__file__).resolve().parents[2] / "custom_nodes"
sys.path.insert(0, str(CUSTOM_NODES))

from dgx_remote_h3.storage import StorageError, cleanup_stale_staging, create_staging, publish_unique


class StorageTests(unittest.TestCase):
    def test_creates_exclusive_private_staging_beneath_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory).resolve()
            staging = create_staging(output, minimum_free_bytes=0)
            self.assertEqual(staging.parent, output)
            self.assertTrue(staging.name.startswith(".dgx-h3-"))
            self.assertTrue(staging.name.endswith(".part"))
            self.assertEqual(stat.S_IMODE(staging.stat().st_mode), 0o600)

    def test_fails_before_creation_when_free_space_is_too_low(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory)
            with self.assertRaisesRegex(StorageError, "free space"):
                create_staging(output, minimum_free_bytes=2**63)
            self.assertEqual(list(output.iterdir()), [])

    def test_publishes_without_overwriting_and_retries_unique_name(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory).resolve()
            staging = create_staging(output, minimum_free_bytes=0)
            staging.write_bytes(b"new-video")
            existing = output / "existing.mp4"
            existing.write_bytes(b"old-video")
            names = iter(["existing.mp4", "generated.mp4"])

            published = publish_unique(staging, output, name_factory=names.__next__)

            self.assertEqual(published, output / "generated.mp4")
            self.assertEqual(existing.read_bytes(), b"old-video")
            self.assertEqual(published.read_bytes(), b"new-video")
            self.assertFalse(staging.exists())

    def test_rejects_staging_outside_exact_output_directory(self):
        with tempfile.TemporaryDirectory() as output_dir, tempfile.TemporaryDirectory() as other_dir:
            staging = pathlib.Path(other_dir) / ".dgx-h3-other.part"
            staging.write_bytes(b"data")
            with self.assertRaisesRegex(StorageError, "output directory"):
                publish_unique(staging, pathlib.Path(output_dir))

    def test_cleanup_removes_only_old_exact_staging_files(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory).resolve()
            old_part = output / ".dgx-h3-old.part"
            recent_part = output / ".dgx-h3-recent.part"
            completed = output / "dgx-h3-complete.mp4"
            unrelated = output / ".other.part"
            for path in (old_part, recent_part, completed, unrelated):
                path.write_bytes(b"data")
            os.utime(old_part, (100.0, 100.0))

            removed = cleanup_stale_staging(output, now=100.0 + 86_401)

            self.assertEqual(removed, 1)
            self.assertFalse(old_part.exists())
            self.assertTrue(recent_part.exists())
            self.assertTrue(completed.exists())
            self.assertTrue(unrelated.exists())


if __name__ == "__main__":
    unittest.main()
