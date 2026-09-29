"""Pure Python regression tests; no CUDA/import of upstream model packages."""
import ast
import hashlib
from pathlib import Path
import tarfile
import tempfile
import types
import unittest

from prepare_runtime import generate, SOURCE_HASHES, ENTRY, OUTPUT
from runtime_guards import require_clean_audio, verify_audio_unchanged, require_dmd, verify_file, load_required_dmd

ROOT = Path(__file__).resolve().parents[3]
ARCHIVE = ROOT / "plans/longcat-avatar15/evidence/install/longcat-source.tar.gz"


class RuntimeTests(unittest.TestCase):
    def test_patch_identity_idempotence_and_no_original_mutation(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as work:
            directory = Path(work)
            with tarfile.open(ARCHIVE) as archive:
                for member in archive.getmembers():
                    rel = member.name.split('/', 1)[-1]
                    if rel in SOURCE_HASHES:
                        path = directory / rel
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(archive.extractfile(member).read())
            first = generate(directory)
            mtimes = {p: p.stat().st_mtime_ns for p in directory.rglob('*') if p.is_file()}
            self.assertEqual(first, generate(directory))
            self.assertEqual(mtimes, {p: p.stat().st_mtime_ns for p in directory.rglob('*') if p.is_file()})
            for rel, expected in SOURCE_HASHES.items():
                self.assertEqual(hashlib.sha256((directory / rel).read_bytes()).hexdigest(), expected)
            generated = (directory / OUTPUT).read_text()
            ast.parse(generated)
            self.assertNotIn('Separator', generated)
            self.assertNotIn('os.remove(', generated)
            self.assertNotIn('temp_vocal_path', generated)
            self.assertIn('seconds=600', generated)
            (directory / ENTRY).write_text('# changed')
            with self.assertRaises(ValueError):
                generate(directory)

    def test_clean_audio_original_survives(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as work:
            source = Path(work) / 'original.wav'
            source.write_bytes(b'original audio')
            path, digest = require_clean_audio(source)
            verify_audio_unchanged(path, digest)
            self.assertEqual(source.read_bytes(), b'original audio')
            source.write_bytes(b'changed')
            with self.assertRaises(RuntimeError):
                verify_audio_unchanged(path, digest)

    def test_dmd_missing_size_hash_and_registration_gates(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as work:
            with self.assertRaises(FileNotFoundError):
                require_dmd(work)
            path = Path(work) / 'artifact'
            path.write_bytes(b'abc')
            digest = hashlib.sha256(b'abc').hexdigest()
            self.assertEqual(verify_file(path, 3, digest), str(path))
            with self.assertRaises(ValueError):
                verify_file(path, 4, digest)
            with self.assertRaises(ValueError):
                verify_file(path, 3, '0' * 64)
            dummy = types.SimpleNamespace(load_lora=lambda *a, **k: None, lora_dict={}, active_loras=[])
            with self.assertRaises(RuntimeError):
                load_required_dmd(dummy, path)
            dummy.lora_dict = {'dmd': types.SimpleNamespace(loras=[object()])}
            dummy.enable_loras = lambda keys: None
            with self.assertRaises(RuntimeError):
                load_required_dmd(dummy, path)
            dummy.enable_loras = lambda keys: dummy.active_loras.extend(keys)
            load_required_dmd(dummy, path)
            self.assertEqual(dummy.active_loras, ['dmd'])


if __name__ == '__main__':
    unittest.main()
