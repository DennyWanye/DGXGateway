"""Independent missing archive view/recovery tests; no real service operations."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

from tests import test_dgx_image_direct_retry as fixtures
from DGXGateway.dgx_gateway.image_batches import attribution


class ImageArchiveViewTests(unittest.TestCase):
    make_parent = fixtures.DirectRetryTests.make_parent

    def setUp(self):
        from PIL import Image
        fixtures.DirectRetryTests.setUp(self)
        self.app.config.bind = '127.0.0.1'; self.app.config.port = 9999
        self.row = copy.deepcopy(self.parent)
        self.row.update(state='SUCCEEDED', artifacts=[], outputs={})
        root = self.app.config.state_dir / 'creative-artifacts' / self.row['job_id']
        root.mkdir(parents=True)
        for group in self.row['image_batch']['groups']:
            node = group['saveNode']
            items = []
            for index, original in enumerate(group['indices']):
                item = {'filename': f'candidate-{original}.png', 'subfolder': '', 'type': 'output'}
                identity = json.dumps([node, 'images', index, item['filename'], '', 'output'], separators=(',', ':'))
                key = hashlib.sha256(identity.encode()).hexdigest()[:20]
                path = root / (key + '-' + item['filename'])
                buffer = io.BytesIO(); Image.new('RGB', (2, 2), (original, 64, 128)).save(buffer, format='PNG')
                path.write_bytes(buffer.getvalue())
                info = path.stat()
                self.row['artifacts'].append({'path': str(path), 'output_key': key, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                    'size_bytes': info.st_size, 'mtime_ns': info.st_mtime_ns, 'node': node, 'kind': 'images', **attribution(self.row['image_batch'], node, index)})
                candidate = self.row['image_batch']['candidates'][original]
                candidate.update(state='SUCCEEDED', sourceKey=path.name)
                items.append(item)
            self.row['outputs'][node] = {'images': items}
        self.jobs.save(self.row)

    def assert_broken_view(self):
        persisted = (self.jobs.root / (self.row['job_id'] + '.json')).read_bytes()
        view = self.jobs.get(self.row['job_id'])
        self.assertEqual(view['state'], 'FAILED')
        self.assertEqual(view['image_batch']['candidates'][0]['state'], 'UNKNOWN')
        self.assertEqual(view['image_batch']['candidates'][0]['error'], 'Saved image download requires recovery')
        self.assertTrue(all(c['state'] == 'SUCCEEDED' for c in view['image_batch']['candidates'][1:]))
        self.assertEqual(persisted, (self.jobs.root / (self.row['job_id'] + '.json')).read_bytes())
        return view

    def test_missing_file_derives_failure_without_mutating_persisted_identity(self):
        Path(self.row['artifacts'][0]['path']).unlink()
        view = self.assert_broken_view()
        self.assertEqual([(c['index'], c['seed']) for c in view['image_batch']['candidates']],
                         [(c['index'], c['seed']) for c in self.row['image_batch']['candidates']])
        with self.assertRaises(ValueError):
            self.jobs.retry_images(self.row['job_id'], [0], 'never-regenerate-missing')

    def test_symlink_directory_fifo_and_empty_are_not_usable_archives(self):
        target = Path(self.row['artifacts'][0]['path'])
        sibling = Path(self.row['artifacts'][1]['path'])
        for kind in ('symlink', 'directory', 'fifo', 'empty'):
            with self.subTest(kind=kind):
                target.unlink()
                if kind == 'symlink': target.symlink_to(sibling)
                elif kind == 'directory': target.mkdir()
                elif kind == 'fifo': os.mkfifo(target)
                else: target.touch()
                self.assert_broken_view()
                if kind == 'directory': target.rmdir()
                else: target.unlink()
                target.write_bytes(b'placeholder')

    def test_size_or_mtime_change_invalidates_lightweight_view_without_reading_bytes(self):
        target = Path(self.row['artifacts'][0]['path'])
        original = target.read_bytes(); before = target.stat()
        target.write_bytes(original + b'changed-size')
        self.assert_broken_view()
        target.write_bytes(original)
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns + 10_000_000))
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('get must not hash every artifact')):
            view = self.jobs.get(self.row['job_id'])
        self.assertEqual(view['image_batch']['candidates'][0]['state'], 'UNKNOWN')

    def test_legacy_missing_metadata_checks_regular_nonempty_without_rejecting_healthy(self):
        for artifact in self.row['artifacts']:
            artifact.pop('size_bytes'); artifact.pop('mtime_ns')
        self.jobs.save(self.row)
        self.assertEqual(self.jobs.get(self.row['job_id'])['state'], 'SUCCEEDED')
        Path(self.row['artifacts'][0]['path']).unlink()
        self.assert_broken_view()

    def test_reconcile_merges_derived_failure_and_enters_only_archive_recovery(self):
        Path(self.row['artifacts'][0]['path']).unlink()
        result = self.jobs.reconcile(self.row['job_id'])
        self.assertEqual(result['state'], 'RECONCILING')
        self.assertEqual(self.thread.call_args.kwargs['target'], self.jobs._recover_downloads)
        worker_row = self.thread.call_args.kwargs['args'][0]
        self.assertEqual(worker_row['image_batch']['candidates'][0]['error'], 'Saved image download requires recovery')
        self.assertFalse(result['memory_released']); self.assertTrue(result['cleanup_required'])
        self.assertEqual(result['job_id'], self.row['job_id'])

    def test_recovery_hash_match_revalidates_mtime_and_candidate_without_download(self):
        target = Path(self.row['artifacts'][0]['path']); before = target.stat()
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns + 10_000_000))
        view = self.assert_broken_view()
        view['artifacts'] = copy.deepcopy(self.row['artifacts'])  # reconcile retains the durable hash identity.
        self.jobs.http.open = Mock(side_effect=AssertionError('existing matching hash needs no network'))
        self.jobs.collect(view, require_complete=False)
        self.assertEqual(view['image_batch']['candidates'][0]['state'], 'SUCCEEDED')
        self.assertNotIn('error', view['image_batch']['candidates'][0])
        self.assertEqual(view['artifacts'][0]['mtime_ns'], target.stat().st_mtime_ns)
        self.assertEqual(view['artifacts'][0]['size_bytes'], target.stat().st_size)
        self.jobs.http.open.assert_not_called()

    def test_changed_remote_bytes_cannot_replace_original_saved_identity(self):
        from PIL import Image
        stream = io.BytesIO(); Image.new('RGB', (2, 2), 'red').save(stream, format='PNG')
        target = Path(self.row['artifacts'][0]['path']); target.unlink()
        expected = copy.deepcopy(self.row['artifacts'][0])
        self.jobs.reconcile(self.row['job_id'])
        worker_row = self.thread.call_args.kwargs['args'][0]
        self.jobs.http.open = Mock(side_effect=lambda *args, **kwargs: io.BytesIO(stream.getvalue()))
        self.jobs.switch = Mock(); self.jobs.request = Mock(return_value={})
        self.jobs._recover_downloads(worker_row)
        persisted = json.loads((self.jobs.root / (self.row['job_id'] + '.json')).read_text())
        old = next(a for a in persisted['artifacts'] if a['candidate_index'] == 0)
        self.assertEqual(old, expected)
        self.assertFalse(target.exists()); self.assertFalse(target.with_suffix('.png.part').exists())
        self.assertEqual(persisted['image_batch']['candidates'][0]['state'], 'UNKNOWN')
        self.assertEqual(persisted['image_batch']['candidates'][0]['error'], 'Saved image download requires recovery')
        self.assertTrue(persisted['memory_released']); self.assertIsNone(self.jobs.active)
        self.assertEqual(self.jobs.http.open.call_count, 1)

    def test_matching_remote_archive_restores_same_identity_and_file_metadata(self):
        target = Path(self.row['artifacts'][0]['path']); data = target.read_bytes(); target.unlink()
        self.jobs.reconcile(self.row['job_id'])
        worker_row = self.thread.call_args.kwargs['args'][0]
        self.jobs.http.open = Mock(side_effect=lambda *args, **kwargs: io.BytesIO(data))
        self.jobs.switch = Mock(); self.jobs.request = Mock(return_value={})
        self.jobs._recover_downloads(worker_row)
        result = self.jobs.get(self.row['job_id'])
        self.assertEqual(result['state'], 'SUCCEEDED')
        restored = next(a for a in result['artifacts'] if a['candidate_index'] == 0)
        for field in ('sha256', 'output_key', 'path', 'candidate_index', 'seed'):
            self.assertEqual(restored[field], self.row['artifacts'][0][field])
        self.assertEqual(restored['size_bytes'], target.stat().st_size)
        self.assertEqual(restored['mtime_ns'], target.stat().st_mtime_ns)
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(self.jobs.http.open.call_count, 1)
        self.assertFalse(any(call.args[0] == '/prompt' for call in self.jobs.request.call_args_list))
