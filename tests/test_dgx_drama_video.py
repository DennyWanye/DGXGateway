"""P3 business oracles: durable intent, ambiguity, independent archival and pause."""
import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

from PIL import Image
from DGXGateway.dgx_gateway.drama.contracts import DramaError
from DGXGateway.dgx_gateway.drama.gateway_adapter import GatewayRejected
from DGXGateway.dgx_gateway.drama.gateway_video_adapter import GatewayVideoAdapter
from DGXGateway.dgx_gateway.drama.service import DramaService


class VideoAdapter:
    def __init__(self):
        self.calls, self.lookups, self.cancels, self.registered = [], [], [], []
        self.record, self.payloads, self.failure = None, [], None
        self.before_gate = lambda: None

    def submit(self, shots, key, images, audios, before_submit, videos=None, profile=None):
        self.before_gate()
        before_submit()
        if self.failure == 'reject':
            raise GatewayRejected('DGX occupied')
        self.calls.append((copy.deepcopy(shots), key))
        self.registered.append({"images": images, "videos": videos or [], "audios": audios, "profile": profile})
        self.record = {'job_id': 'a' * 32, 'idempotency_key': key, 'non_preemptive': True,
                       'profile': profile or 'heretic-ref2va-v1',
                       'shots': [{**shot, 'state': 'QUEUED', 'submission_phase': 'NOT_SUBMITTED'} for shot in shots],
                       'state': 'RUNNING', 'memory_released': False, 'cleanup_required': False}
        if self.failure == 'lost':
            raise TimeoutError('accepted but response lost')
        if self.failure == 'malformed':
            return {}
        return copy.deepcopy(self.record)

    def get(self, jid):
        return copy.deepcopy(self.record)

    def find(self, key):
        self.lookups.append(key)
        return copy.deepcopy(self.record)

    def outputs(self, record):
        return copy.deepcopy(self.payloads)

    def cancel(self, jid):
        self.cancels.append(jid)
        self.record['cancel_requested'] = True
        return self.get(jid)

    def reconcile(self, jid):
        self.record.update(memory_released=True, cleanup_required=False)
        return self.get(jid)


class DramaVideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media_temp = tempfile.TemporaryDirectory()
        path = Path(cls.media_temp.name) / 'four-seconds.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=1344x768:r=24:d=4.458333333',
                        '-f', 'lavfi', '-i', 'anullsrc=r=24000:cl=mono', '-t', '4.458333333', '-c:v', 'libx264',
                        '-preset', 'ultrafast', '-threads', '2', '-c:a', 'aac', '-pix_fmt', 'yuv420p', str(path)], check=True)
        cls.mp4 = path.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.media_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.adapter = VideoAdapter()
        self.serial = 0
        self.service = DramaService(self.root, self.root, video_adapter=self.adapter)
        self.stop_poller()
        self.pid = self.service.create_project({'title': '视频测试', 'requestKey': self.key()})['project']['id']
        self.eid = self.service.create_episode(self.pid, {'title': '第一集', 'requestKey': self.key()})['episode']['id']
        buffer = io.BytesIO()
        Image.new('RGB', (64, 64), 'blue').save(buffer, format='PNG')
        data = buffer.getvalue()
        self.image = self.service.upload_image(self.pid, io.BytesIO(data), len(data), 'approved.png', self.key())['asset']['id']
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(24000)
            output.writeframes(bytes(192000))
        self.wav = buffer.getvalue()
        self.audio = self.service.upload_reference_audio(self.pid, io.BytesIO(self.wav), len(self.wav), 'reference.wav', self.key())['asset']['id']

    def stop_poller(self):
        self.service._video_stop.set()
        self.service._video_thread.join()

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def key(self):
        self.serial += 1
        return 'video-test-' + str(self.serial)

    def shot(self):
        shot = self.service.create_shot(self.eid, {'title': '镜头', 'requestKey': self.key()})['shot']
        self.service.select_reference(shot['id'], {'expectedRevision': shot['revision'], 'assetId': self.image, 'requestKey': self.key()})
        current = self.service.shot_production(shot['id'])
        return {'shotId': shot['id'], 'shotRevisionId': current['currentRevisionId'], 'audioAssetId': self.audio, 'prompt': '慢慢向前走', 'seed': 42}

    def submit(self, count=1):
        return self.service.submit_video_batch(self.eid, {'shots': [self.shot() for _ in range(count)], 'requestKey': self.key()})['job']

    def finish(self, index=0):
        self.adapter.record['shots'][index].update(state='SUCCEEDED', submission_phase='RECEIVED')
        self.adapter.payloads.append({'index': index, 'sourceKey': f'{index}-clip.mp4', 'filename': f'{index}-clip.mp4', 'data': self.mp4})

    def test_multiple_candidates_freeze_distinct_seeds_and_reservations(self):
        shot = {**self.shot(), 'candidateCount': 4, 'seed': 2147483646}
        body = {'shots': [shot], 'requestKey': self.key()}
        job = self.service.submit_video_batch(self.eid, body)['job']
        self.assertEqual([i['seed'] for i in job['items']], [2147483646, 2147483647, 0, 1])
        self.assertEqual([i['candidateIndex'] for i in job['items']], [1, 2, 3, 4])
        self.assertEqual(len({i['shotId'] for i in job['items']}), 1)
        self.assertEqual(self.service.production_state(self.pid)['reservedShots'], 4)
        self.assertEqual(self.service.submit_video_batch(self.eid, body)['job']['id'], job['id'])
        self.service.dispatch_video_once()
        self.assertEqual(len(self.adapter.calls[0][0]), 4)
        self.assertEqual(len({i['name'] for i in self.adapter.calls[0][0]}), 4)

    def test_multiple_candidates_bounds_and_continuation_do_not_expand_twice(self):
        for count in (0, 5, True, 1.5):
            with self.assertRaises(DramaError):
                self.service.submit_video_batch(self.eid, {'shots': [{**self.shot(), 'candidateCount': count}], 'requestKey': self.key()})
        job = self.service.submit_video_batch(self.eid, {'shots': [{**self.shot(), 'candidateCount': 3}], 'requestKey': self.key()})['job']
        self.service.dispatch_video_once()
        self.adapter.record.update(state='INTERRUPTED', memory_released=True, cleanup_required=False)
        self.service.refresh_video_job(job['id'])
        continued = self.service.continue_video_job(job['id'], {'requestKey': self.key()})['job']
        self.assertEqual(len(continued['items']), 3)
        self.assertEqual([i['seed'] for i in continued['items']], [42, 43, 44])

    def test_queue_freezes_inputs_and_same_key_returns_same_batch(self):
        body = {'shots': [self.shot()], 'requestKey': self.key()}
        first = self.service.submit_video_batch(self.eid, body)
        self.assertEqual(self.service.submit_video_batch(self.eid, body), first)
        self.assertEqual(len(self.adapter.calls), 0)
        self.assertEqual(self.service.production_state(self.pid)['reservedSeconds'], 4)
        self.service.dispatch_video_once()
        self.service.dispatch_video_once()
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.adapter.calls[0][0][0]['image_asset_id'], self.image)

    def test_cross_project_audio_and_unapproved_reference_rejected(self):
        item = self.shot()
        other = self.service.create_project({'title': '其他项目', 'requestKey': self.key()})['project']['id']
        with self.service.store.connect(write=True) as db:
            db.execute('DELETE FROM dr_asset_bindings WHERE project_id=? AND asset_id=?', (self.pid, self.audio))
        with self.assertRaises(DramaError):
            self.service.submit_video_batch(self.eid, {'shots': [item], 'requestKey': self.key()})
        self.assertEqual(self.service.video_jobs(self.pid)['jobs'], [])

    def test_current_draft_reference_requires_explicit_approval_before_video(self):
        item = self.shot()
        current = self.service.shot_production(item['shotId'])
        revised = self.service.revise_shot(item['shotId'], {
            'baseRevisionId': current['currentRevisionId'], 'expectedRevision': current['revision'],
            'snapshot': {**current['current']['snapshot'], 'prompt': '尚待批准的新版画面'},
            'requestKey': self.key()})
        item['shotRevisionId'] = revised['currentRevisionId']
        context = self.service.get_revision_context('shot', item['shotRevisionId'])
        self.assertEqual(context['currentRevisionId'], item['shotRevisionId'])
        self.assertEqual(context['snapshot']['approvedReferenceAssetId'], self.image)
        self.assertIsNone(context['approvedReferenceAssetId'])
        with self.assertRaises(DramaError) as exc:
            self.service.submit_video_batch(self.eid, {'shots': [item], 'requestKey': self.key()})
        self.assertEqual(exc.exception.code, 'reference_required')
        self.assertEqual(self.service.video_jobs(self.pid)['jobs'], [])
        self.assertEqual(self.service.production_state(self.pid)['reservedShots'], 0)
        self.service.dispatch_video_once()
        self.assertEqual(self.adapter.calls, [])

    def test_same_request_key_changed_seed_conflicts_without_job_or_post(self):
        body = {'shots': [self.shot()], 'requestKey': self.key()}
        original = self.service.submit_video_batch(self.eid, body)['job']
        self.service.dispatch_video_once()
        before_jobs = self.service.video_jobs(self.pid)['jobs']
        before_calls = copy.deepcopy(self.adapter.calls)
        self.assertEqual(len(before_jobs), 1)
        self.assertEqual(len(before_calls), 1)
        changed = copy.deepcopy(body)
        changed['shots'][0]['seed'] += 1
        with self.assertRaises(DramaError) as exc:
            self.service.submit_video_batch(self.eid, changed)
        self.assertEqual((exc.exception.code, exc.exception.status), ('request_conflict', 409))
        self.service.dispatch_video_once()
        self.assertEqual(self.service.video_jobs(self.pid)['jobs'], before_jobs)
        self.assertEqual(self.adapter.calls, before_calls)
        self.assertEqual(self.service.video_job(original['id'])['job']['items'][0]['seed'], body['shots'][0]['seed'])

    def test_same_request_key_changed_approved_shot_revision_conflicts_without_job_or_post(self):
        body = {'shots': [self.shot()], 'requestKey': self.key()}
        original = self.service.submit_video_batch(self.eid, body)['job']
        self.service.dispatch_video_once()
        sid = body['shots'][0]['shotId']
        current = self.service.shot_production(sid)
        revised = self.service.revise_shot(sid, {
            'baseRevisionId': current['currentRevisionId'], 'expectedRevision': current['revision'],
            'snapshot': {**current['current']['snapshot'], 'prompt': '已另行批准的新镜头内容'},
            'requestKey': self.key()})
        self.service.select_reference(sid, {'expectedRevision': revised['revision'], 'assetId': self.image,
                                            'requestKey': self.key()})
        new_revision = self.service.shot_production(sid)['currentRevisionId']
        self.assertNotEqual(new_revision, body['shots'][0]['shotRevisionId'])
        self.assertEqual(self.service.get_revision_context('shot', new_revision)['approvedReferenceAssetId'], self.image)
        changed = copy.deepcopy(body)
        changed['shots'][0]['shotRevisionId'] = new_revision
        before_jobs = self.service.video_jobs(self.pid)['jobs']
        before_calls = copy.deepcopy(self.adapter.calls)
        self.assertEqual(len(before_jobs), 1)
        self.assertEqual(len(before_calls), 1)
        with self.assertRaises(DramaError) as exc:
            self.service.submit_video_batch(self.eid, changed)
        self.assertEqual((exc.exception.code, exc.exception.status), ('request_conflict', 409))
        self.service.dispatch_video_once()
        self.assertEqual(self.service.video_jobs(self.pid)['jobs'], before_jobs)
        self.assertEqual(self.adapter.calls, before_calls)
        self.assertEqual(self.service.video_job(original['id'])['job']['items'][0]['shotRevisionId'], body['shots'][0]['shotRevisionId'])

    def test_two_concurrent_submissions_compete_for_last_local_reservation(self):
        initial = self.submit()
        bodies = [{'shots': [self.shot()], 'requestKey': self.key()} for _ in range(2)]
        barrier = threading.Barrier(3)
        successes, errors = [], []
        result_lock = threading.Lock()
        def submit_at_barrier(body):
            try:
                barrier.wait(timeout=5)
                result = self.service.submit_video_batch(self.eid, body)['job']
                with result_lock:
                    successes.append(result)
            except Exception as exc:
                with result_lock:
                    errors.append(exc)
        with patch('DGXGateway.dgx_gateway.drama.video.MAX_RESERVED', 2):
            self.assertEqual(self.service.production_state(self.pid)['reservedShots'], 1)
            threads = [threading.Thread(target=submit_at_barrier, args=(body,)) for body in bodies]
            for thread in threads:
                thread.start()
            try:
                barrier.wait(timeout=5)
            finally:
                for thread in threads:
                    thread.join(timeout=10)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(len(successes), 1)
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], DramaError)
            self.assertEqual((errors[0].code, errors[0].status), ('queue_limit', 409))
            state = self.service.production_state(self.pid)
            self.assertEqual(state['reservedShots'], 2)
            self.assertEqual(state['reservedSeconds'], 8)
            self.assertEqual(state['maxReservedShots'], 2)
            jobs = self.service.video_jobs(self.pid)['jobs']
            self.assertEqual({job['id'] for job in jobs}, {initial['id'], successes[0]['id']})
            self.assertEqual(sum(len(job['items']) for job in jobs), 2)
            self.assertEqual(self.adapter.calls, [])

    def test_utf8_budget_is_bytes(self):
        item = self.shot(); item['prompt'] = '中' * 5462
        with self.assertRaises(DramaError):
            self.service.submit_video_batch(self.eid, {'shots': [item], 'requestKey': self.key()})

    def test_lost_receipt_finds_original_never_resubmits(self):
        job = self.submit(); self.adapter.failure = 'lost'
        self.service.dispatch_video_once()
        self.assertEqual(self.service.video_job(job['id'])['job']['state'], 'SUBMISSION_UNKNOWN')
        current = self.service.refresh_video_job(job['id'])['job']
        self.assertEqual(current['gatewayJobId'], 'a' * 32)
        self.service.dispatch_video_once()
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.adapter.lookups, [job['dispatchKey']])

    def test_missing_receipt_lookup_retains_unknown_and_reservation(self):
        job = self.submit(); self.adapter.failure = 'lost'
        self.service.dispatch_video_once(); self.adapter.record = None
        for _ in range(3):
            self.service.refresh_video_job(job['id']); self.service.dispatch_video_once()
        self.assertEqual(self.service.video_job(job['id'])['job']['state'], 'SUBMISSION_UNKNOWN')
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.service.production_state(self.pid)['reservedShots'], 1)

    def test_malformed_receipt_is_unknown_not_definite_blocked(self):
        job = self.submit(); self.adapter.failure = 'malformed'
        self.service.dispatch_video_once()
        self.assertEqual(self.service.video_job(job['id'])['job']['state'], 'SUBMISSION_UNKNOWN')

    def test_pause_between_registration_and_post_sends_zero_batches(self):
        job = self.submit()
        self.adapter.before_gate = lambda: self.service.set_production_state(self.pid, {'paused': True, 'expectedRevision': 1, 'requestKey': self.key()})
        self.service.dispatch_video_once()
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.service.video_job(job['id'])['job']['state'], 'BLOCKED')

    def test_pause_does_not_cancel_inflight_and_archives_completed(self):
        job = self.submit(); self.service.dispatch_video_once()
        self.service.set_production_state(self.pid, {'paused': True, 'expectedRevision': 1, 'requestKey': self.key()})
        self.finish()
        self.adapter.record.update(state='SUCCEEDED', memory_released=True)
        result = self.service.refresh_video_job(job['id'])['job']
        self.assertTrue(result['items'][0]['takeId'])
        self.assertTrue(result['memoryReleased'])
        self.assertEqual(self.adapter.cancels, [])

    def test_partial_batch_unknown_does_not_lose_success_and_only_unsent_continues_once(self):
        job = self.submit(3); self.service.dispatch_video_once(); self.finish(0)
        self.adapter.record['shots'][1].update(state='SUBMISSION_UNKNOWN', submission_phase='UNKNOWN')
        self.adapter.record.update(state='FAILED', memory_released=True)
        first = self.service.refresh_video_job(job['id'])['job']
        again = self.service.refresh_video_job(job['id'])['job']
        self.assertEqual(first['items'][0]['takeId'], again['items'][0]['takeId'])
        self.assertIsNone(first['items'][1]['takeId'])
        continued = self.service.continue_video_job(job['id'], {'requestKey': self.key()})['job']
        repeated = self.service.continue_video_job(job['id'], {'requestKey': self.key()})['job']
        self.assertEqual(continued['id'], repeated['id'])
        self.assertEqual(len(continued['items']), 1)
        self.assertEqual(continued['items'][0]['shotId'], first['items'][2]['shotId'])

    def test_technical_check_then_review_then_explicit_select(self):
        job = self.submit(); self.service.dispatch_video_once(); self.finish()
        self.adapter.record.update(state='SUCCEEDED', memory_released=True)
        result = self.service.refresh_video_job(job['id'])['job']
        item = result['items'][0]
        shot = self.service.episode(self.eid)['shots'][0]
        self.assertIsNone(shot['selectedTakeId'])
        body = {'takeId': item['takeId'], 'expectedRevision': shot['revision'], 'requestKey': self.key()}
        with self.assertRaises(DramaError): self.service.select_take(shot['id'], body)
        self.service.review_take(item['takeId'], {'decision': 'approved', 'notes': '运动及声音通过', 'requestKey': self.key()})
        selected = self.service.select_take(shot['id'], body)['shot']
        self.assertEqual(selected['selectedTakeId'], item['takeId'])
        self.service.review_take(item['takeId'], {'decision': 'rejected', 'notes': '复核发现不连贯', 'requestKey': self.key()})
        self.assertEqual(self.service.episode(self.eid)['shots'][0]['selectedTakeId'], item['takeId'])

    def test_take_review_accepts_omitted_or_empty_notes_but_bounds_optional_text(self):
        job = self.submit(); self.service.dispatch_video_once(); self.finish()
        self.adapter.record.update(state='SUCCEEDED', memory_released=True)
        item = self.service.refresh_video_job(job['id'])['job']['items'][0]
        take_id = item['takeId']
        approval_body = {'decision': 'approved', 'requestKey': self.key()}
        approved = self.service.review_take(take_id, approval_body)
        self.assertEqual(approved['reviewStatus'], 'approved')
        self.assertEqual(approved, self.service.review_take(take_id, approval_body))
        rejected = self.service.review_take(take_id, {'decision': 'rejected', 'notes': '', 'requestKey': self.key()})
        self.assertEqual(rejected['reviewStatus'], 'rejected')
        with self.service.store.connect() as db:
            self.assertEqual(db.execute('SELECT notes FROM dr_take_reviews WHERE take_id=?', (take_id,)).fetchone()[0], '')
        with self.assertRaises(DramaError):
            self.service.review_take(take_id, {'decision': 'approved', 'notes': 'x' * 2001, 'requestKey': self.key()})
        self.service.review_take(take_id, {'decision': 'approved', 'notes': '', 'requestKey': self.key()})
        shot = self.service.episode(self.eid)['shots'][0]
        selected = self.service.select_take(shot['id'], {'takeId': take_id, 'expectedRevision': shot['revision'], 'requestKey': self.key()})['shot']
        self.assertEqual(selected['selectedTakeId'], take_id)

    def test_bad_video_never_creates_take(self):
        job = self.submit(); self.service.dispatch_video_once(); self.finish()
        self.adapter.payloads[0]['data'] = b'not an mp4'
        self.adapter.record.update(state='SUCCEEDED', memory_released=True)
        result = self.service.refresh_video_job(job['id'])['job']
        self.assertEqual(result['state'], 'FAILED')
        self.assertIsNone(result['items'][0]['takeId'])
        self.assertEqual(self.service.episode(self.eid)['shots'][0]['takes'], [])

    def test_pending_cancel_never_calls_gateway_and_releases_reservation(self):
        job = self.submit(); self.service.cancel_video_job(job['id']); self.service.cancel_video_job(job['id'])
        self.service.dispatch_video_once()
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.service.production_state(self.pid)['reservedShots'], 0)
        self.assertFalse(self.service.video_job(job['id'])['job']['memoryReleased'])

    def test_interrupted_and_unsent_unknown_cancel_releases_reservation(self):
        first = self.submit()
        with self.service.store.connect(write=True) as db:
            db.execute("UPDATE dr_video_jobs SET state='INTERRUPTED' WHERE id=?", (first['id'],))
        self.service.cancel_video_job(first['id'])
        self.assertEqual(self.service.video_job(first['id'])['job']['state'], 'CANCELED')
        self.assertEqual(self.service.production_state(self.pid)['reservedShots'], 0)
        second = self.submit()
        with self.service.store.connect(write=True) as db:
            db.execute("UPDATE dr_video_jobs SET state='SUBMISSION_UNKNOWN',gateway_job_id=NULL WHERE id=?", (second['id'],))
        self.service.cancel_video_job(second['id'])
        self.assertEqual(self.service.video_job(second['id'])['job']['state'], 'CANCELED')
        self.assertEqual(self.adapter.cancels, [])
        third = self.submit()
        self.service.dispatch_video_once()
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.adapter.calls[0][1], third['dispatchKey'])

    def test_inflight_cancel_intent_survives_restart_and_keeps_completed_clip(self):
        job = self.submit(2); self.service.dispatch_video_once(); self.service.cancel_video_job(job['id'])
        self.assertEqual(self.service.video_job(job['id'])['job']['state'], 'CANCEL_REQUESTED')
        self.service.close()
        self.service = DramaService(self.root, self.root, video_adapter=self.adapter); self.stop_poller()
        self.finish(0)
        self.adapter.record['shots'][1]['state'] = 'CANCELED'
        self.adapter.record.update(state='CANCELED', memory_released=False, cleanup_required=True)
        result = self.service.refresh_video_job(job['id'])['job']
        self.assertTrue(result['cancelRequested']); self.assertTrue(result['cleanupRequired'])
        self.assertTrue(result['items'][0]['takeId']); self.assertFalse(result['memoryReleased'])

    def test_dispatch_restart_only_looks_up_original_key(self):
        job = self.submit()
        with self.service.store.connect(write=True) as db:
            db.execute("UPDATE dr_video_jobs SET state='DISPATCHING' WHERE id=?", (job['id'],))
        self.service.close()
        self.service = DramaService(self.root, self.root, video_adapter=self.adapter); self.stop_poller()
        self.service.refresh_video_job(job['id']); self.service.dispatch_video_once()
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.adapter.lookups, [job['dispatchKey']])
        self.assertEqual(self.service.video_job(job['id'])['job']['items'][0]['submissionPhase'], 'UNKNOWN')

    def test_concurrent_claim_only_dispatches_once(self):
        self.submit()
        threads = [threading.Thread(target=self.service.dispatch_video_once) for _ in range(5)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(len(self.adapter.calls), 1)

    def test_busy_rejection_is_blocked_no_resubmission(self):
        job = self.submit(); self.adapter.failure = 'reject'
        self.service.dispatch_video_once(); self.service.dispatch_video_once()
        self.assertEqual(self.service.video_job(job['id'])['job']['state'], 'BLOCKED')
        self.assertEqual(self.adapter.calls, [])

    def test_changed_reference_after_queue_blocks_dispatch(self):
        job = self.submit()
        sid = job['items'][0]['shotId']; current = self.service.shot_production(sid)
        self.service.revise_shot(sid, {'baseRevisionId': current['currentRevisionId'], 'expectedRevision': current['revision'],
                                    'snapshot': {**current['current']['snapshot'], 'prompt': 'new'}, 'requestKey': self.key()})
        self.service.dispatch_video_once()
        self.assertEqual(self.service.video_job(job['id'])['job']['state'], 'BLOCKED')
        self.assertEqual(self.adapter.calls, [])

    def test_source_hash_tampering_does_not_submit(self):
        job = self.submit()
        self.service.media_path(self.pid, self.audio).write_bytes(b'changed')
        self.service.dispatch_video_once()
        self.assertEqual(self.service.video_job(job['id'])['job']['state'], 'BLOCKED')
        self.assertEqual(self.adapter.calls, [])

    def test_oversized_local_input_is_definitely_unsent(self):
        job = self.submit()
        self.service.media_path(self.pid, self.audio).write_bytes(bytes(256 * 1024 + 1))
        self.service.dispatch_video_once()
        self.assertEqual(self.service.video_job(job['id'])['job']['state'], 'BLOCKED')
        self.assertEqual(self.service.production_state(self.pid)['reservedShots'], 0)
        self.assertEqual(self.adapter.calls, [])

    def test_second_service_has_no_recovery_side_effects(self):
        job = self.submit()
        with self.service.store.connect(write=True) as db:
            db.execute("UPDATE dr_video_jobs SET state='DISPATCHING' WHERE id=?", (job['id'],))
        before = self.service.store.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'already has a scheduler'):
            DramaService(self.root, self.root, video_adapter=self.adapter)
        self.assertEqual(self.service.video_job(job['id'])['job']['state'], 'DISPATCHING')
        self.assertEqual(self.service.store.path.read_bytes(), before)

    def test_truncated_pcm_is_not_accepted_by_header_only(self):
        with self.assertRaises(DramaError):
            self.service.upload_reference_audio(self.pid, io.BytesIO(self.wav[:-100]), len(self.wav)-100, 'broken.wav', self.key())

    def test_current_selected_candidate_survives_bad_new_result(self):
        first = self.submit(); self.service.dispatch_video_once(); self.finish()
        self.adapter.record.update(state='SUCCEEDED', memory_released=True)
        item = self.service.refresh_video_job(first['id'])['job']['items'][0]
        self.service.review_take(item['takeId'], {'decision': 'approved', 'notes': '可采用', 'requestKey': self.key()})
        shot = self.service.episode(self.eid)['shots'][0]
        self.service.select_take(shot['id'], {'takeId': item['takeId'], 'expectedRevision': shot['revision'], 'requestKey': self.key()})
        body = {key: item[key] for key in ('shotId','shotRevisionId','audioAssetId','prompt','seed')}
        second = self.service.submit_video_batch(self.eid, {'shots': [body], 'requestKey': self.key()})['job']
        self.adapter.record = None; self.adapter.payloads = []
        self.service.dispatch_video_once()
        self.adapter.record['job_id'] = 'b' * 32
        # Simulate another confirmed task identity from the initial acceptance.
        with self.service.store.connect(write=True) as db:
            db.execute('UPDATE dr_video_jobs SET gateway_job_id=? WHERE id=?', ('b'*32,second['id']))
        self.finish(); self.adapter.payloads[0]['data'] = b'bad mp4'
        self.adapter.record.update(state='FAILED',memory_released=True)
        self.service.refresh_video_job(second['id'])
        self.assertEqual(self.service.episode(self.eid)['shots'][0]['selectedTakeId'], item['takeId'])

    def test_accepted_then_busy_has_no_resource_and_allows_explicit_continue(self):
        job = self.submit(); self.service.dispatch_video_once()
        self.adapter.record.update(state='BLOCKED', memory_released=True, resource_not_acquired=True)
        result = self.service.refresh_video_job(job['id'])['job']
        self.assertEqual(result['state'], 'BLOCKED'); self.assertTrue(result['canContinue'])
        self.assertEqual(self.service.production_state(self.pid)['reservedShots'], 0)
        new = self.service.continue_video_job(job['id'], {'requestKey': self.key()})['job']
        self.assertEqual(new['state'], 'QUEUED')

    def test_normal_inflight_with_real_output_reader_has_no_archive_error(self):
        job = self.submit(); self.service.dispatch_video_once()
        self.adapter.record['shots'][0].update(index=0, state='RUNNING', submission_phase='REQUEST_STARTED')
        self.adapter.outputs = GatewayVideoAdapter(state_dir=self.root).outputs
        result = self.service.refresh_video_job(job['id'])['job']
        self.assertEqual(result['state'], 'RUNNING')
        self.assertEqual(result['items'][0]['state'], 'RUNNING')
        self.assertIsNone(result['items'][0]['error'])
        self.assertIsNone(result['items'][0]['takeId'])
        self.assertEqual(len(self.adapter.calls), 1)

    def test_confirmed_inflight_clears_only_temporary_missing_output_error(self):
        job = self.submit(); self.service.dispatch_video_once()
        self.adapter.record.update(state='SUBMISSION_UNKNOWN')
        self.adapter.record['shots'][0].update(index=0, state='SUBMISSION_UNKNOWN', submission_phase='UNKNOWN')
        self.adapter.outputs = GatewayVideoAdapter(state_dir=self.root).outputs
        missing = self.service.refresh_video_job(job['id'])['job']
        self.assertEqual(missing['items'][0]['state'], 'OUTPUT_UNAVAILABLE')
        self.assertTrue(missing['items'][0]['error'])
        self.adapter.record.update(state='RUNNING')
        self.adapter.record['shots'][0].update(state='RUNNING', submission_phase='REQUEST_STARTED')
        restored = self.service.refresh_video_job(job['id'])['job']
        self.assertEqual(restored['items'][0]['state'], 'RUNNING')
        self.assertIsNone(restored['items'][0]['error'])
        self.assertEqual(restored['items'][0]['submissionPhase'], 'REQUEST_STARTED')
        self.assertEqual(self.service.production_state(self.pid)['reservedShots'], 1)
        self.assertEqual(len(self.adapter.calls), 1)

    def test_recovered_ring_receipt_clears_temporary_missing_output_error(self):
        job = self.submit(); self.service.dispatch_video_once()
        self.adapter.record.update(state='SUBMISSION_UNKNOWN')
        self.adapter.record['shots'][0].update(index=0, state='SUBMISSION_UNKNOWN', submission_phase='UNKNOWN')
        self.adapter.outputs = GatewayVideoAdapter(state_dir=self.root).outputs
        missing = self.service.refresh_video_job(job['id'])['job']
        self.assertEqual(missing['items'][0]['state'], 'OUTPUT_UNAVAILABLE')
        self.assertTrue(missing['items'][0]['error'])
        self.adapter.record.update(state='RUNNING')
        self.adapter.record['shots'][0].update(state='RUNNING', submission_phase='RECEIVED')
        restored = self.service.refresh_video_job(job['id'])['job']
        self.assertEqual(restored['items'][0]['state'], 'RUNNING')
        self.assertIsNone(restored['items'][0]['error'])
        self.assertEqual(restored['items'][0]['submissionPhase'], 'RECEIVED')
        self.assertEqual(self.service.production_state(self.pid)['reservedShots'], 1)
        self.assertEqual(len(self.adapter.calls), 1)

    def test_media_validation_error_survives_later_missing_file_and_inflight_status(self):
        job = self.submit(); self.service.dispatch_video_once(); self.finish()
        self.adapter.payloads[0]['data'] = b'not-valid-video'
        self.adapter.record.update(state='SUCCEEDED', memory_released=True)
        invalid = self.service.refresh_video_job(job['id'])['job']['items'][0]
        self.assertEqual(invalid['state'], 'INVALID')
        self.assertTrue(invalid['error'])
        self.adapter.record.update(state='SUBMISSION_UNKNOWN')
        self.adapter.record['shots'][0].update(state='SUBMISSION_UNKNOWN', submission_phase='UNKNOWN')
        self.adapter.payloads = [{'index': 0, 'sourceKey': '0-clip.mp4', 'filename': '0-clip.mp4', 'error': 'artifact_unavailable'}]
        missing = self.service.refresh_video_job(job['id'])['job']['items'][0]
        self.assertEqual((missing['state'], missing['error']), ('INVALID', invalid['error']))
        self.adapter.record.update(state='RUNNING')
        self.adapter.record['shots'][0].update(state='RUNNING', submission_phase='REQUEST_STARTED')
        self.adapter.payloads = []
        inflight = self.service.refresh_video_job(job['id'])['job']['items'][0]
        self.assertEqual((inflight['state'], inflight['error']), ('INVALID', invalid['error']))
        self.assertIsNone(inflight['takeId'])
        self.assertEqual(len(self.adapter.calls), 1)

    def test_missing_archive_recovery_keeps_successful_sibling(self):
        job = self.submit(2); self.service.dispatch_video_once(); self.finish(0); self.finish(1)
        self.adapter.record.update(state='SUCCEEDED',memory_released=True)
        saved = self.adapter.payloads[1].pop('data')
        self.adapter.payloads[1]['error'] = 'artifact_unavailable'
        partial = self.service.refresh_video_job(job['id'])['job']
        self.assertTrue(partial['items'][0]['takeId'])
        self.assertEqual(partial['items'][1]['state'], 'OUTPUT_UNAVAILABLE')
        self.adapter.payloads[1].pop('error'); self.adapter.payloads[1]['data'] = saved
        complete = self.service.refresh_video_job(job['id'])['job']
        self.assertEqual(complete['items'][0]['takeId'], partial['items'][0]['takeId'])
        self.assertTrue(complete['items'][1]['takeId'])
        self.assertEqual(complete['state'], 'SUCCEEDED')

    def test_local_reservation_bound_is_transactional(self):
        with patch('DGXGateway.dgx_gateway.drama.video.MAX_RESERVED', 2):
            self.submit(2)
            with self.assertRaises(DramaError) as exc: self.submit(1)
            self.assertEqual(exc.exception.code, 'queue_limit')
            self.assertEqual(len(self.service.video_jobs(self.pid)['jobs']), 1)

    def test_publish_before_database_failure_recovers_one_candidate(self):
        job = self.submit(); self.service.dispatch_video_once(); self.finish()
        self.adapter.record.update(state='SUCCEEDED',memory_released=True)
        original = self.service._bind_asset
        with patch.object(self.service, '_bind_asset', side_effect=RuntimeError('crash after published file')):
            failed = self.service.refresh_video_job(job['id'])['job']
        self.assertIsNone(failed['items'][0]['takeId'])
        restored = self.service.refresh_video_job(job['id'])['job']
        self.assertTrue(restored['items'][0]['takeId'])
        self.assertEqual(len(self.service.episode(self.eid)['shots'][0]['takes']), 1)

    def _webp(self, color):
        buffer = io.BytesIO()
        Image.new('RGB', (8, 8), color).save(buffer, format='WEBP', lossless=True)
        data = buffer.getvalue()
        return self.service.upload_image(self.pid, io.BytesIO(data), len(data), color + '.webp', self.key())['asset']['id']

    def test_reference_nodes_send_saved_prompt_and_webp_as_png(self):
        red, blue = self._webp('red'), self._webp('blue')
        created = self.service.create_shot(self.eid, {'title': '节点镜头', 'requestKey': self.key()})['shot']
        current = self.service.shot_production(created['id'])
        official = "\n\n".join([
            "subject_definitions:\n<Subject 1> is the person in <Picture 1>.",
            "summary:\nOne shot.",
            "retention_analysis:\nRetain <Picture 1> and <Picture 2>.",
            "detailed_description:\n[Shot 1] Turn once. <Picture 2> is the location.",
            "overall_soundscape:\nNo dialogue.",
            "non_diegetic_music:\nNo added score.",
        ])
        revised = self.service.revise_shot(created['id'], {
            'baseRevisionId': current['currentRevisionId'], 'expectedRevision': current['revision'],
            'snapshot': {**current['current']['snapshot'], 'prompt': official, 'approvedReferenceAssetId': None,
                        'referenceNodes': [
                            {'id': 'a', 'media': 'image', 'role': 'character', 'assetId': red, 'description': '脸'},
                            {'id': 'b', 'media': 'image', 'role': 'scene', 'assetId': blue, 'description': '台'},
                        ]},
            'requestKey': self.key()})
        job = self.service.submit_video_batch(self.eid, {'shots': [{
            'shotId': created['id'], 'shotRevisionId': revised['currentRevisionId'],
            'audioAssetId': '', 'prompt': '旧草稿，不要用这篇', 'seed': 7,
        }], 'requestKey': self.key()})['job']
        self.service.dispatch_video_once()
        sent = self.adapter.calls[0][0][0]
        self.assertEqual(sent['prompt'], official)
        # Node references are their own ordered inputs, not an implicit rewrite
        # of the empty approved-main-image slot (which the receipt audits).
        self.assertEqual(sent['image_asset_id'], '')
        self.assertEqual(self.service.video_job(job['id'])['job']['state'], 'SUBMITTED')
        self.assertEqual(len(sent['reference_image_ids']), 2)
        self.assertNotIn(red, sent['reference_image_ids'])
        self.assertNotIn(blue, sent['reference_image_ids'])
        registered = {row['assetId']: row['data'] for row in self.adapter.registered[-1]['images']}
        first = Image.open(io.BytesIO(registered[sent['reference_image_ids'][0]]))
        second = Image.open(io.BytesIO(registered[sent['reference_image_ids'][1]]))
        self.assertEqual(first.format, 'PNG')
        self.assertEqual(first.getpixel((0, 0)), (255, 0, 0))
        self.assertEqual(second.getpixel((0, 0)), (0, 0, 255))
        with self.service.store.connect() as db:
            frozen = json.loads(db.execute("SELECT input_snapshot FROM dr_video_items WHERE job_id=?", (job['id'],)).fetchone()[0])
        self.assertEqual([node['assetId'] for node in frozen['referenceNodes']], [red, blue])
        self.assertEqual(frozen['prompt'], official)
