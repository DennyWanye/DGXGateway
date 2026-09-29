"""Independent P6 review: receipt reads and cross-connection maintenance gates.

Uses temporary SQLite/lease files and fake model IO; never contacts DGX hosts.
Fixture composition deliberately avoids inheriting/counting the implementer's tests.
"""
import json
import threading
from types import SimpleNamespace
import unittest

from tests import test_dgx_gateway_text_jobs as fixture
from DGXGateway.dgx_gateway.creative_jobs import CreativeJobs
from DGXGateway.dgx_gateway.video_jobs import VideoJobs, PROFILE
from DGXGateway.dgx_gateway.store import OperationStore
from DGXGateway.dgx_gateway.text_store import TextPersistence


class IndependentTextReviewTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.TextJobTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.store, self.app = self.case.store, self.case.app

    def fenced(self):
        row = self.store._db.execute("SELECT value FROM metadata WHERE key='mutation_fenced'").fetchone()
        return bool(row and row[0] == '1')

    def test_old_receipts_survive_text_owner_but_changed_or_new_requests_do_not(self):
        op = self.store.create_operation('f'*32, None, fixture.MODEL, 'old-operation-key')
        self.store.set_phase(op.operation_id, 'RELEASED')
        creative = CreativeJobs.__new__(CreativeJobs)
        video = VideoJobs.__new__(VideoJobs)
        assets = SimpleNamespace(get=lambda _: {'content_type': 'image/png'})
        workflow = {'1': {'class_type': 'Fixture', 'inputs': {}}}
        shot = {'name': 'fixture', 'prompt': 'test', 'image_asset_id': 'a'*64, 'audio_asset_id': 'b'*64, 'seed': 1}
        for manager, name in ((creative, 'creative'), (video, 'video')):
            manager.app = self.app; manager.active = None
            manager.root = self.case.root / name; manager.root.mkdir()
            manager.assets = manager.images = manager.audio = assets
        image_row = {'job_id': 'a'*32, 'idempotency_key': 'old-image-key', 'workflow': workflow, 'state': 'SUCCEEDED', 'memory_released': True}
        video_row = {'job_id': 'b'*32, 'idempotency_key': 'old-video-key', 'shots': [shot], 'profile': PROFILE, 'state': 'SUCCEEDED', 'memory_released': True}
        for manager, row in ((creative, image_row), (video, video_row)):
            (manager.root / (row['job_id']+'.json')).write_text(json.dumps(row))
            manager.get = lambda _, row=row: dict(row)
        self.app.creative_jobs, self.app.video_jobs = creative, video
        self.case.submit()
        self.assertEqual(creative.submit(workflow, 'old-image-key')['job_id'], image_row['job_id'])
        self.assertEqual(video.submit([shot], 'old-video-key')['job_id'], video_row['job_id'])
        old, created = self.app.submit_switch(fixture.MODEL, None, 'old-operation-key')
        self.assertEqual(old.operation_id, op.operation_id); self.assertFalse(created)
        self.assertEqual(video.reconcile(video_row['job_id']), video_row)
        for call in (lambda: creative.submit({'2': workflow['1']}, 'old-image-key'),
                     lambda: video.submit([{**shot, 'seed': 2}], 'old-video-key'),
                     lambda: self.app.submit_switch(None, None, 'old-operation-key')):
            with self.assertRaises(ValueError): call()
        for call in (lambda: creative.submit(workflow, 'new-image-key'),
                     lambda: video.submit([shot], 'new-video-key'),
                     lambda: self.app.submit_switch(None, None, 'new-operation-key')):
            with self.assertRaises(BlockingIOError): call()
        self.assertEqual(len(self.store.list_operations()), 1)
        self.assertEqual(self.case.adapter.calls, [])

    def test_parent_between_children_blocks_maintenance_without_installing_fence(self):
        row = self.case.submit()
        with self.assertRaises(BlockingIOError): self.store.set_mutation_fence(True)
        self.assertFalse(self.fenced())
        checked = []
        def during_inference():
            self.assertIsNone(self.store.active_operation())
            with self.assertRaises(BlockingIOError): self.store.set_mutation_fence(True)
            self.assertFalse(self.fenced()); checked.append(True)
        self.case.transport.callback = during_inference
        self.case.jobs.run(row['job_id'])
        self.assertEqual(checked, [True])
        self.assertEqual(self.case.jobs.get(row['job_id'])['state'], 'SUCCEEDED')
        self.assertEqual(self.case.adapter.calls, ['start', 'stop'])
        self.store.set_mutation_fence(True)
        self.assertTrue(self.fenced())

    def test_unknown_owner_blocks_maintenance_until_explicit_cleanup(self):
        self.case.transport.error = fixture.SubmissionUnknown('lost reply')
        row = self.case.run_job()
        self.assertEqual(row['state'], 'SUBMISSION_UNKNOWN')
        with self.assertRaises(BlockingIOError): self.store.set_mutation_fence(True)
        self.assertFalse(self.fenced())
        self.assertEqual(self.case.adapter.calls, ['start'])

    def test_maintenance_blocks_new_text_with_zero_side_effects(self):
        self.store.set_mutation_fence(True)
        with self.assertRaises(BlockingIOError): self.case.submit()
        self.assertEqual(self.case.jobs.list(), [])
        self.assertEqual(self.store.list_operations(), [])
        self.assertEqual(self.case.adapter.calls, [])

    def test_separate_connections_racing_fence_and_parent_create_have_one_winner(self):
        other = OperationStore(self.case.root/'gateway.sqlite3')
        self.addCleanup(other._db.close)
        persistence = TextPersistence(other)
        barrier = threading.Barrier(2)
        outcomes = []
        row = {'job_id': 'c'*32, 'idempotency_key': 'race-review-key', 'input_hash': 'hash',
               'resource_status': 'pending', 'state': 'QUEUED'}
        def compete(name, action):
            barrier.wait(timeout=3)
            try:
                action(); outcomes.append((name, True))
            except (RuntimeError, BlockingIOError): outcomes.append((name, False))
        threads = [threading.Thread(target=compete, args=('fence', lambda: self.store.set_mutation_fence(True))),
                   threading.Thread(target=compete, args=('text', lambda: persistence.create(row)))]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=5); self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(sum(success for _, success in outcomes), 1, outcomes)
        self.assertNotEqual(self.fenced(), bool(persistence.list()), outcomes)

    def test_pre_text_schema_maintenance_compatibility(self):
        legacy = OperationStore(self.case.root/'legacy.sqlite3')
        self.addCleanup(legacy._db.close)
        self.assertIsNone(legacy.set_mutation_fence(True))
        self.assertEqual(legacy._db.execute("SELECT value FROM metadata WHERE key='mutation_fenced'").fetchone()[0], '1')


if __name__ == '__main__':
    unittest.main()
