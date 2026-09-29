"""Independent MF01 regression oracles from queue review."""
import unittest
from unittest.mock import patch

from tests import test_dgx_drama_main_flow as fixtures
from DGXGateway.dgx_gateway.drama.contracts import DramaError


class QueueReviewTests(unittest.TestCase):
    setUp = fixtures.MainFlowTests.setUp
    tearDown = fixtures.MainFlowTests.tearDown
    key = fixtures.MainFlowTests.key
    shot = fixtures.MainFlowTests.shot
    submit = fixtures.MainFlowTests.submit
    stop_poller = fixtures.MainFlowTests.stop_poller

    def test_saved_image_queue_retains_existing_32_task_admission_bound(self):
        with patch.object(self.service._reference_executor, 'submit'):
            for _ in range(32):
                self.service.submit_reference_job('entity', self.entity['currentRevisionId'],
                    {'tool': 'generate_image', 'parameters': {'prompt': '有界队列'}, 'requestKey': self.key()})
            with self.assertRaises(DramaError) as rejected:
                self.service.submit_reference_job('entity', self.entity['currentRevisionId'],
                    {'tool': 'generate_image', 'parameters': {'prompt': '第33个'}, 'requestKey': self.key()})
        self.assertEqual(rejected.exception.code, 'image_queue_full')

    def test_same_timestamp_video_jobs_use_the_same_tiebreaker_as_shared_queue(self):
        with patch('DGXGateway.dgx_gateway.drama.video.new_id', side_effect=['f' * 32, 'd' * 32, 'a' * 32, 'b' * 32]):
            self.submit()
            self.submit()
        with self.service.store.connect(write=True) as db:
            # Make insertion order conflict with ID order deterministically.
            db.execute('UPDATE dr_video_jobs SET created_at=1')
            rows = db.execute('SELECT id FROM dr_video_jobs ORDER BY created_at').fetchall()
            initial = rows[0]['id']
            other = rows[1]['id']
            # The following ORDER BY must match the shared FIFO even if SQLite
            # chooses any equal-time row based on its query plan.
            expected = min(initial, other)
        self.service.dispatch_video_once()
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.adapter.calls[0][1], 'drama-video-' + expected)
        self.assertEqual(self.service.video_job(expected)['job']['state'], 'SUBMITTED')
