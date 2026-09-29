"""Independent MF06 recovery fault tests; no remote services are contacted."""
import unittest
import threading
import time
from unittest.mock import patch

from tests import test_model_residency as residency_fixtures
from tests import test_dgx_drama_video as drama_fixtures
from DGXGateway.dgx_gateway.residency import ModelResidency
from DGXGateway.dgx_gateway.video_jobs import VideoJobs


class ResidencyRecoveryReviewTests(unittest.TestCase):
    setUp = residency_fixtures.ResidencyTests.setUp
    row = residency_fixtures.ResidencyTests.row
    start = residency_fixtures.ResidencyTests.start
    finish = residency_fixtures.ResidencyTests.finish

    def test_committed_stop_bookkeeping_failure_can_be_reconciled(self):
        a = self.row('a')
        self.start(a)
        self.finish(a)
        original_save = self.app.video_jobs.save

        def fail_released_snapshot(row):
            if row.get('memory_released'):
                raise OSError('injected disk failure after committed stop')
            return original_save(row)

        with patch.object(self.app.video_jobs, 'save', side_effect=fail_released_snapshot):
            with self.assertRaises(OSError):
                self.r.release(a['job_id'])
        self.assertEqual(self.h3.state, 'STOPPED')
        self.assertEqual(self.r.view()['state'], 'RECOVERY_REQUIRED')
        # A subsequent explicit release must accept its own committed stop proof.
        self.r.release(a['job_id'])
        self.assertEqual(self.r.view()['state'], 'NONE')

    def test_video_reconcile_settles_failed_claimed_residency(self):
        a = self.row('a')
        self.start(a)
        self.finish(a)
        b = self.row('b')
        self.start(b)
        b.update(state='SUBMISSION_UNKNOWN', cleanup_required=True)
        self.app.video_jobs.save(b)
        self.r.job_finished('video_jobs', b)
        self.assertEqual(self.r.view()['state'], 'RECOVERY_REQUIRED')
        result = self.app.video_jobs.reconcile(b['job_id'])
        self.assertTrue(result['memory_released'])
        self.assertEqual(self.h3.state, 'STOPPED')
        # No stale resident obligation may strand all future submissions.
        self.assertEqual(self.r.view()['state'], 'NONE')

    def test_typed_image_busy_rejection_does_not_strand_business_queue(self):
        self.h3.state = 'HEALTHY'  # External model, no Gateway resident ownership.
        row = {'job_id': 'c' * 32, 'state': 'QUEUED', 'memory_released': False,
               'workflow': {'1': {'class_type': 'UNETLoader', 'inputs': {'unet_name': 'test'}}},
               'image_request': {'tool': 'generate_image'}}
        jobs = self.app.creative_jobs
        jobs.save(row)
        jobs.active = row['job_id']
        jobs.run(row)
        result = jobs.get(row['job_id'])
        self.assertEqual(self.h3.state, 'HEALTHY')
        self.assertEqual(self.store.list_operations(), [])
        self.assertFalse(result.get('cleanup_required'))
        # generation_turn treats memoryReleased=False without resourceSettled as busy.
        self.assertTrue(result.get('memory_released') or result.get('resource_settled'))

    def test_restart_before_handoff_operation_can_release_original_owner(self):
        a = self.row('a')
        self.start(a)
        self.finish(a)
        b = self.row('b')
        b.update(state='LOADING', shots=[{'state': 'QUEUED', 'submission_phase': 'NOT_SUBMITTED'}])
        self.app.video_jobs.save(b)
        self.app.video_jobs.active = b['job_id']
        self.r.before_start('video_jobs', b)
        self.assertEqual(self.r.row['state'], 'CLAIMED')
        self.assertFalse(any(op.idempotency_key == b['job_id']+'-start' for op in self.store.list_operations()))
        # Restart at the actual application initialization order.
        self.app.video_jobs = VideoJobs(self.app)
        self.app.residency = self.r = ModelResidency(self.app)
        self.assertEqual(self.r.row['state'], 'RECOVERY_REQUIRED')
        self.r.release(self.r.row['owner_job_id'])
        self.assertEqual(self.h3.state, 'STOPPED')
        self.assertEqual(self.r.row['state'], 'NONE')

    def test_claim_wins_idle_expiry_and_old_owner_cannot_release(self):
        a = self.row('a')
        self.start(a)
        self.finish(a)
        b = self.row('b')
        self.app.video_jobs.active = b['job_id']
        self.r.before_start('video_jobs', b)
        self.r.row['expires_at'] = time.time() - 1
        self.r.start()
        self.addCleanup(self.r.close)
        time.sleep(1.1)  # Allow a real timer cycle after the former idle deadline.
        self.assertEqual(self.r.row['state'], 'CLAIMED')
        self.assertEqual(self.events.count('minimax-h3.stop'), 0)
        with self.assertRaises(BlockingIOError):
            self.r.release(a['job_id'])
        self.app.video_jobs.switch(b, 'minimax-h3', 'start')
        self.r.claimed('video_jobs', b)
        self.assertEqual(self.events.count('minimax-h3.start'), 1)

    def test_release_wins_admission_race_then_new_job_cold_starts(self):
        a = self.row('a')
        self.start(a)
        self.finish(a)
        b = self.row('b')
        stopping, allow_stop = threading.Event(), threading.Event()
        original_stop = self.h3.stop
        errors = []

        def paused_stop(context):
            stopping.set()
            if not allow_stop.wait(5):
                raise AssertionError('test never released stop barrier')
            return original_stop(context)

        def release():
            try:
                self.r.release(a['job_id'])
            except Exception as exc:
                errors.append(exc)

        def admit():
            try:
                # Mirrors submit's serialization before publishing active ownership.
                with self.app._submit_lock:
                    self.start(b)
            except Exception as exc:
                errors.append(exc)

        with patch.object(self.h3, 'stop', side_effect=paused_stop):
            release_thread = threading.Thread(target=release)
            claim_thread = threading.Thread(target=admit)
            release_thread.start()
            try:
                self.assertTrue(stopping.wait(5))
                claim_thread.start()
            finally:
                allow_stop.set()
            release_thread.join(5)
            claim_thread.join(5)
        self.assertFalse(release_thread.is_alive())
        self.assertFalse(claim_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.events.count('minimax-h3.stop'), 1)
        self.assertEqual(self.events.count('minimax-h3.start'), 2)
        self.assertEqual(self.h3.state, 'HEALTHY')

    def test_real_creative_worker_reuses_typed_loaders_but_not_generic_workflow(self):
        jobs = self.app.creative_jobs

        def transport(path, body=None):
            if path == '/queue':
                return {'queue_running': [], 'queue_pending': []}
            if path == '/prompt':
                return {'prompt_id': jobs.active}
            if path.startswith('/history/'):
                return {jobs.active: {'status': {'status_str': 'success'}, 'outputs': {'out': {'images': []}}}}
            raise AssertionError(path)

        jobs.request = transport
        jobs.collect = lambda row, **kw: None  # Inference/download fake, worker and lifecycle are real.
        for identifier, typed in [('c', True), ('d', True), ('e', False)]:
            row = {'job_id': identifier * 32, 'state': 'QUEUED', 'memory_released': False,
                   'workflow': {'1': {'class_type': 'UNETLoader', 'inputs': {'unet_name': 'same-loader'}}}}
            if typed:
                row['image_request'] = {'tool': 'generate_image'}
            jobs.save(row)
            jobs.active = row['job_id']
            jobs.run(row)
            result = jobs.get(row['job_id'])
            self.assertEqual(result['state'], 'SUCCEEDED', result)
            if typed:
                self.assertFalse(result['memory_released'])
                self.assertTrue(result['resource_settled'])
                self.assertEqual(self.events.count('comfyui.start'), 1)
            else:
                self.assertTrue(result['memory_released'])
                self.assertEqual(self.r.row['state'], 'NONE')
        self.assertEqual(self.events.count('comfyui.start'), 2)


class DramaResidencyBridgeReviewTests(unittest.TestCase):
    setUpClass = classmethod(drama_fixtures.DramaVideoTests.setUpClass.__func__)
    tearDownClass = classmethod(drama_fixtures.DramaVideoTests.tearDownClass.__func__)
    setUp = drama_fixtures.DramaVideoTests.setUp
    tearDown = drama_fixtures.DramaVideoTests.tearDown
    key = drama_fixtures.DramaVideoTests.key
    stop_poller = drama_fixtures.DramaVideoTests.stop_poller
    shot = drama_fixtures.DramaVideoTests.shot
    submit = drama_fixtures.DramaVideoTests.submit
    finish = drama_fixtures.DramaVideoTests.finish

    def test_completed_resident_video_persists_settled_and_dispatches_next_batch(self):
        first = self.submit()
        second = self.submit()
        self.service.dispatch_video_once()
        self.finish()
        self.adapter.record.update(state='SUCCEEDED', resource_settled=True, resource_status='resident', memory_released=False)
        completed = self.service.refresh_video_job(first['id'])['job']
        self.assertEqual(completed['state'], 'SUCCEEDED')
        self.assertFalse(completed['memoryReleased'])
        self.assertTrue(completed['resourceSettled'])
        with self.service.store.connect() as db:
            row = db.execute('SELECT resource_settled, memory_released FROM dr_video_jobs WHERE id=?', (first['id'],)).fetchone()
            self.assertEqual(tuple(row), (1, 0))
        self.service.dispatch_video_once()
        self.assertEqual(len(self.adapter.calls), 2)
