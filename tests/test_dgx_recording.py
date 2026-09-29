import gzip
from datetime import date, datetime, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from DGXGateway.dgx_gateway.recording import RecordingService

spec = importlib.util.spec_from_file_location('nas_receiver', Path(__file__).parents[1] / 'DGXGateway/deploy/agentos/receive-dgx-recording.py')
receiver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receiver)


class RecordingTests(unittest.TestCase):
    def service(self, path, options=None):
        service = RecordingService(SimpleNamespace(state_dir=Path(path), nodes=()),
            SimpleNamespace(build=lambda: {'sampled_at':'2026-09-05T00:00:00Z', 'nodes': [],
                'active_connection': {'api_key':'DO-NOT-RECORD'},
                'current_operation': {'operation_id':'a','phase':'RUNNING','token':'DO-NOT-RECORD'}}), options)
        service.root.mkdir()
        return service

    def test_batch_write_and_projection(self):
        with tempfile.TemporaryDirectory() as d:
            s = self.service(d)
            row = s.collect()
            self.assertNotIn('DO-NOT-RECORD', json.dumps(row))
            for _ in range(20):
                s.append(row)
            self.assertEqual(list(s.root.glob('*.jsonl')), [])
            s.flush()
            self.assertEqual(len(next(s.root.glob('*.jsonl')).read_text().splitlines()), 20)
            s.flush()
            self.assertEqual(len(next(s.root.glob('*.jsonl')).read_text().splitlines()), 20)

    def test_restart_recovers_persisted_history(self):
        with tempfile.TemporaryDirectory() as d:
            s = self.service(d)
            s.append({'sampled_at':'persisted','nodes':[]})
            s.flush()
            restored = RecordingService(s.config, s.snapshot)
            restored._record_loop = lambda: None
            restored._backup_loop = lambda: None
            restored.start()
            restored.stop()
            self.assertEqual(restored.history()[0]['sampled_at'], 'persisted')

    def test_low_disk_keeps_buffer(self):
        with tempfile.TemporaryDirectory() as d:
            s = self.service(d)
            s.append({'sampled_at':'x'})
            with patch('DGXGateway.dgx_gateway.recording.shutil.disk_usage', return_value=SimpleNamespace(free=1)):
                with self.assertRaises(OSError): s.flush()
            self.assertEqual(len(s.buffer), 1)

    def test_archive_roundtrip_and_failed_backup_keeps_source(self):
        with tempfile.TemporaryDirectory() as d:
            s = self.service(d, {'nas_host':'test','nas_receiver':'receiver'})
            source = s.root/'2026-09-01.jsonl'
            source.write_text('{"sample":1}\n')
            a = s.seal('2026-09-01')
            self.assertEqual(gzip.decompress(a.read_bytes()), source.read_bytes())
            with patch('DGXGateway.dgx_gateway.recording.subprocess.run', return_value=SimpleNamespace(returncode=1)):
                with self.assertRaises(RuntimeError): s.backup_once()
            self.assertTrue(source.exists())
            self.assertFalse(list(s.root.glob('*.receipt.json')))

    def test_verified_receipt_idempotency(self):
        with tempfile.TemporaryDirectory() as d:
            s = self.service(d, {'nas_host':'test','nas_receiver':'receiver'})
            (s.root/'2026-09-01.jsonl').write_text('{"sample":1}\n')
            a=s.seal('2026-09-01')
            reply=json.dumps({'sha256':hashlib.sha256(a.read_bytes()).hexdigest(),'bytes':a.stat().st_size}).encode()
            with patch('DGXGateway.dgx_gateway.recording.subprocess.run', return_value=SimpleNamespace(returncode=0,stdout=reply)) as run:
                s.backup_once(); s.backup_once()
                self.assertEqual(run.call_count,1)
            self.assertTrue((s.root/'2026-09-01.receipt.json').exists())

    def test_receiver_checksum_retry_and_truncation(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); data=gzip.compress(b'hello'); sha=hashlib.sha256(data).hexdigest();name='dgx-2026-09-01.jsonl.gz'
            with patch.object(receiver.shutil,'disk_usage',return_value=SimpleNamespace(free=10**12)):
                with self.assertRaises(ValueError): receiver.receive(name,sha,len(data),io.BytesIO(b'x'),root)
                self.assertFalse((root/name).exists())
                result=receiver.receive(name,sha,len(data),io.BytesIO(data),root)
                self.assertEqual(result['sha256'],sha)
                self.assertTrue(receiver.receive(name,sha,len(data),io.BytesIO(data),root)['already_present'])
                with self.assertRaises(RuntimeError): receiver.receive(name,'a'*64,len(data),io.BytesIO(data),root)
                with self.assertRaises(ValueError): receiver.receive('../bad',sha,len(data),io.BytesIO(data),root)

class RetentionTests(unittest.TestCase):
    def test_calendar_boundaries_and_scoped_deletion(self):
        for today, old, keep in [(date(2026,9,1),'2026-07-31','2026-08-01'),
                                  (date(2027,1,1),'2026-11-30','2026-12-01'),
                                  (date(2024,4,1),'2024-02-29','2024-03-01')]:
            with self.subTest(today=today), tempfile.TemporaryDirectory() as d:
                root=Path(d)
                expired=root/f'dgx-{old}.jsonl.gz'; expired.write_bytes(b'old')
                retained=root/f'dgx-{keep}.jsonl.gz'; retained.write_bytes(b'keep')
                unrelated=root/'family-document.txt'; unrelated.write_text('keep')
                invalid=root/'dgx-2020-99-99.jsonl.gz';invalid.write_text('keep')
                folder=root/'dgx-2020-01-01.jsonl.gz';folder.mkdir()
                link=root/'dgx-2020-01-02.jsonl.gz';link.symlink_to(unrelated)
                preview=receiver.prune(root,as_of=today,dry_run=True)
                self.assertTrue(expired.exists())
                self.assertEqual(len(preview['files']),1)
                result=receiver.prune(root,as_of=today)
                self.assertEqual(result['deleted_count'],1)
                self.assertFalse(expired.exists())
                for p in (retained,unrelated,invalid,folder,link): self.assertTrue(p.exists())
                self.assertEqual(receiver.prune(root,as_of=today)['deleted_count'],0)
                self.assertIn('completed',next(root.glob('retention-*.jsonl')).read_text())

    def test_month_receipt_prevents_repeat_and_retry_on_failure(self):
        with tempfile.TemporaryDirectory() as d:
            service=RecordingService(SimpleNamespace(state_dir=Path(d)),None,
                {'nas_host':'test','nas_receiver':'test','nas_monthly_retention':True})
            service.root.mkdir()
            with patch('DGXGateway.dgx_gateway.recording.subprocess.run',return_value=SimpleNamespace(returncode=1)):
                with self.assertRaises(RuntimeError):service.prune_nas_monthly()
            self.assertFalse((service.root/'nas-retention.json').exists())
            reply={'month':datetime.now(timezone.utc).strftime('%Y-%m'),'cutoff':'2026-08-01','deleted_count':0,'dry_run':False}
            with patch('DGXGateway.dgx_gateway.recording.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(reply))) as run:
                service.prune_nas_monthly();service.prune_nas_monthly()
                self.assertEqual(run.call_count,1)

if __name__ == '__main__':
    unittest.main()
