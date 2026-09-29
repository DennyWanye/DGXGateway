"""P7 frozen B08/B10/B16: tiny real ZIP/files with bounded fault injection."""
from concurrent.futures import CancelledError
from pathlib import Path
import struct
import unittest
import zipfile

from tests import test_dgx_drama_backups as fixtures
from DGXGateway.dgx_gateway.drama.backups import pack_project
from DGXGateway.dgx_gateway.drama.restore import inspect_archive, restore_archive
from DGXGateway.dgx_gateway.drama.contracts import DramaError


class P7BoundaryReviewTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.BackupTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)

    def test_b08_path_inode_swap_during_open_fd_read_must_reject_whole_package(self):
        f = self.f
        source = f.s.media_path(f.pid, f.aid)
        saved = source.with_suffix('.original-fixture')
        original = source.read_bytes()
        calls = []
        def replace(**kwargs):
            if not calls:
                source.rename(saved)
                source.write_bytes(b'x' * len(original))
            calls.append(kwargs)
        try:
            # Frozen B08 requires refusal even when the pinned FD still exposes
            # valid old bytes; do not silently substitute a weaker oracle.
            with self.assertRaises((DramaError, OSError)):
                pack_project(f.s, f.pid, f.package, 'a' * 32, progress=replace)
            self.assertTrue(calls)
            self.assertFalse(f.package.exists())
            self.assertFalse(f.package.with_name(f.package.name + '.partial').exists())
        finally:
            if saved.exists():
                source.unlink(missing_ok=True)
                saved.rename(source)

    def test_b10_cancel_during_asset_write_cleans_only_operation_partial(self):
        f = self.f
        source = f.s.media_path(f.pid, f.aid)
        original = source.read_bytes()
        keep = f.root / 'unrelated-user-file'
        keep.write_bytes(b'preserve')
        count = []
        def cancel(**kwargs):
            count.append(kwargs)
            raise CancelledError('deterministic fixture cancellation during write')
        with self.assertRaises(CancelledError):
            pack_project(f.s, f.pid, f.package, 'b' * 32, progress=cancel)
        self.assertEqual(len(count), 1)
        self.assertGreater(count[0]['bytesDone'], 0)
        self.assertFalse(f.package.exists())
        self.assertFalse(f.package.with_name(f.package.name + '.partial').exists())
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(keep.read_bytes(), b'preserve')

    def test_b16_member_count_and_forged_uncompressed_size_rejected_before_extract(self):
        f = self.f
        many = f.root / 'many-tiny-members.zip'
        with zipfile.ZipFile(many, 'w') as archive:
            for index in range(8193):
                archive.writestr('assets/' + f'{index:064x}' + '.png', b'')
        self.assertLess(many.stat().st_size, 2 * 1024 * 1024)
        with self.assertRaisesRegex(DramaError, '数量'):
            inspect_archive(many)
        f.pack()
        raw = bytearray(f.package.read_bytes())
        central = raw.index(b'PK\x01\x02')
        struct.pack_into('<II', raw, central + 20, 512 * 1024 * 1024 + 1, 512 * 1024 * 1024 + 1)
        forged = f.root / 'forged-size.zip'
        forged.write_bytes(raw)
        with self.assertRaisesRegex(DramaError, '大小'):
            inspect_archive(forged)
        self.assertFalse((f.root / 'outside').exists())

    def test_b16_actual_crc_corruption_rejects_without_published_workspace(self):
        f = self.f
        f.pack()
        with zipfile.ZipFile(f.package) as archive:
            member = next(x for x in archive.infolist() if x.filename.startswith('assets/'))
        raw = bytearray(f.package.read_bytes())
        filename_size, extra_size = struct.unpack_from('<HH', raw, member.header_offset + 26)
        offset = member.header_offset + 30 + filename_size + extra_size
        raw[offset] ^= 1
        corrupt = f.root / 'crc-corrupt.zip'; corrupt.write_bytes(raw)
        target = f.root / 'crc-restored'
        with self.assertRaises((DramaError, zipfile.BadZipFile)):
            restore_archive(corrupt, target)
        self.assertFalse(target.exists())
        self.assertFalse((f.root / '.restore-crc-restored.partial').exists())
