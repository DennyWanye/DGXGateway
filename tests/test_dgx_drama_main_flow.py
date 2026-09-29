"""MF-01/03: independent durable queue, no-duplicate and PCM contract oracles."""
import io
import json
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import patch

from tests import test_dgx_drama_video as video_fixtures
from tests.test_dgx_drama_references import FakeGateway
from DGXGateway.dgx_gateway import mcp
from DGXGateway.dgx_gateway.drama.service import DramaService
from DGXGateway.dgx_gateway.drama.gateway_adapter import GatewayBusy, GatewayReferenceAdapter


class MainFlowTests(unittest.TestCase):
    key = video_fixtures.DramaVideoTests.key
    shot = video_fixtures.DramaVideoTests.shot
    submit = video_fixtures.DramaVideoTests.submit
    stop_poller = video_fixtures.DramaVideoTests.stop_poller
    tearDown = video_fixtures.DramaVideoTests.tearDown

    def setUp(self):
        video_fixtures.DramaVideoTests.setUp(self)
        self.service._reference_stop.set()
        self.service._reference_poller.join()
        self.images = FakeGateway()
        self.images.initial_state = 'RUNNING'
        self.images.output_bytes = []
        self.service._reference_adapter = self.images
        self.entity = self.service.create_entity(self.pid, {'kind': 'character', 'name': '演员', 'requestKey': self.key()})['entity']

    def submit_image(self, key=None):
        body = {'tool': 'generate_image', 'parameters': {'prompt': '人物'}, 'requestKey': key or self.key()}
        result = self.service.submit_reference_job('entity', self.entity['currentRevisionId'], body)['job']
        self.service._reference_executor.submit(lambda: None).result(5)
        return self.service.reference_job(result['id'])['job']

    def test_image_video_fifo_waits_for_release_and_preserves_same_key(self):
        first = self.submit_image('same')
        self.assertEqual(self.submit_image('same')['id'], first['id'])
        video = self.submit()
        second = self.submit_image()
        self.service.dispatch_video_once()
        self.assertEqual(len(self.images.submissions), 1)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(second['state'], 'PREPARED')
        record = self.images.records[first['gatewayJobId']]
        record.update(state='CANCELED', memory_released=False)
        self.service.refresh_reference_job(first['id'])
        self.service.dispatch_video_once()
        self.assertEqual(self.adapter.calls, [])
        record['memory_released'] = True
        self.service.refresh_reference_job(first['id'])
        self.service._dispatch_reference(second['id'])
        self.assertEqual(len(self.images.submissions), 1)  # video is earlier
        self.service.dispatch_video_once()
        self.assertEqual(len(self.adapter.calls), 1)
        self.service._dispatch_reference(second['id'])
        self.assertEqual(len(self.images.submissions), 1)
        self.assertEqual(self.service.refresh_video_job(video['id'])['job']['state'], 'RUNNING')

    def test_explicit_busy_requeues_but_unknown_never_reposts(self):
        with patch.object(self.images, 'submit', side_effect=GatewayBusy('busy')):
            job = self.submit_image()
        self.assertEqual(job['state'], 'PREPARED')
        self.images.fail_after_accept = True
        self.service._dispatch_reference(job['id'])
        self.assertEqual(self.service.reference_job(job['id'])['job']['state'], 'SUBMISSION_UNKNOWN')
        for _ in range(3):
            self.service._dispatch_reference(job['id'])
        self.assertEqual(len(self.images.submissions), 1)
        self.service.refresh_reference_job(job['id'])
        self.assertEqual(self.service.reference_job(job['id'])['job']['state'], 'RUNNING')

    def test_video_explicit_busy_retains_reservation_and_dispatch_key(self):
        job = self.submit()
        with patch.object(self.adapter, 'submit', side_effect=GatewayBusy('busy')):
            self.service.dispatch_video_once()
        waiting = self.service.video_job(job['id'])['job']
        self.assertEqual(waiting['state'], 'QUEUED')
        self.assertEqual(self.service.production_state(self.pid)['reservedShots'], 1)
        self.service.dispatch_video_once()
        self.assertEqual(self.adapter.calls[0][1], job['dispatchKey'])

    def test_queued_cancel_never_calls_model_and_next_can_run(self):
        video = self.submit()
        job = self.submit_image()
        self.assertEqual(job['state'], 'PREPARED')
        self.service.cancel_reference_job(job['id'])
        self.service.cancel_video_job(video['id'])
        self.service._dispatch_reference(job['id'])
        self.service.dispatch_video_once()
        self.assertEqual(self.images.submissions, [])
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.images.cancel_count, 0)
        self.assertEqual(self.service.production_state(self.pid)['reservedShots'], 0)

    def test_proven_unsent_image_survives_service_restart(self):
        with patch.object(self.service._reference_executor, 'submit'):
            job = self.service.submit_reference_job('entity', self.entity['currentRevisionId'],
                {'tool': 'generate_image', 'parameters': {'prompt': '等待'}, 'requestKey': self.key()})['job']
        self.service.close()
        self.service = DramaService(self.root, self.root, reference_adapter=self.images)
        self.service._reference_stop.set(); self.service._reference_poller.join()
        self.assertEqual(self.service.reference_job(job['id'])['job']['state'], 'PREPARED')
        self.service._dispatch_reference(job['id'])
        self.service._dispatch_reference(job['id'])
        self.assertEqual(len(self.images.submissions), 1)

    def test_waiting_queue_is_bounded_and_duplicate_scheduling_is_coalesced(self):
        from DGXGateway.dgx_gateway.drama.contracts import DramaError
        self.submit()  # earliest intent holds the image queue without dispatch
        with patch.object(self.service._reference_executor, 'submit') as scheduled:
            first = None
            for i in range(32):
                job = self.service.submit_reference_job('entity', self.entity['currentRevisionId'],
                    {'tool': 'generate_image', 'parameters': {'prompt': '等待'}, 'requestKey': f'queue-{i}'})['job']
                first = first or job
            for _ in range(10):
                self.service._schedule_reference(first['id'])
            self.assertEqual(scheduled.call_count, 32)
            with self.assertRaises(DramaError) as caught:
                self.service.submit_reference_job('entity', self.entity['currentRevisionId'],
                    {'tool': 'generate_image', 'parameters': {'prompt': '等待'}, 'requestKey': 'queue-full'})
            self.assertEqual(caught.exception.code, 'image_queue_full')
        self.assertEqual(self.images.submissions, [])

    def test_index_is_read_only_all_projects_and_real_stages(self):
        video = self.submit()
        image = self.submit_image()
        other = self.service.create_project({'title': '其他项目', 'requestKey': self.key()})['project']['id']
        before = (len(self.images.submissions), len(self.adapter.calls))
        rows = self.service.task_index(self.pid)['tasks']
        self.assertEqual({t['kind'] for t in rows}, {'image', 'video'})
        self.assertEqual({t['stage'] for t in rows}, {'WAITING'})
        self.assertEqual(sorted(t['queuePosition'] for t in rows), [1, 2])
        for task in rows:
            self.assertEqual(task['waitReason'], '任务已保存，等待前序制作完成并交接资源；不会抢占现有任务。')
            self.assertNotIn('完成并释放资源', task['waitReason'])
        self.assertEqual(self.service.task_index(other)['tasks'], [])
        self.assertEqual(len(self.service.task_index()['tasks']), 2)
        self.assertEqual(before, (len(self.images.submissions), len(self.adapter.calls)))
        self.service.cancel_reference_job(image['id'])
        self.service.dispatch_video_once()
        self.adapter.record['state'] = 'LOADING'
        self.service.refresh_video_job(video['id'])
        self.assertEqual(next(t for t in self.service.task_index()['tasks'] if t['kind']=='video')['stage'], 'LOADING')

    def test_default_silence_exact_pcm_idempotent_and_project_bound(self):
        first = self.service.default_reference_audio(self.pid, {'requestKey': 'silence'})['asset']
        again = self.service.default_reference_audio(self.pid, {'requestKey': 'silence'})['asset']
        self.assertEqual(first['id'], again['id'])
        data = self.service.media_path(self.pid, first['id']).read_bytes()
        with wave.open(io.BytesIO(data), 'rb') as wav:
            self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getnframes()), (24000, 1, 2, 96000))
            self.assertEqual(wav.readframes(96000), bytes(192000))
        other = self.service.create_project({'title': '另一项目', 'requestKey': self.key()})['project']['id']
        self.service.default_reference_audio(other, {'requestKey': 'silence'})
        with self.service.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM dr_asset_bindings WHERE asset_id=?', (first['id'],)).fetchone()[0], 2)


class BusyProtocolTests(unittest.TestCase):
    def test_only_explicit_gateway_busy_is_retryable(self):
        request = {'jsonrpc': '2.0', 'id': 'test', 'method': 'tools/call',
                   'params': {'name': 'generate_image', 'arguments': {'prompt': 'test', 'idempotency_key': 'main-flow-protocol'}}}
        with patch.object(mcp, 'call', side_effect=BlockingIOError('occupied')):
            response = mcp.dispatch(None, request)
        adapter = GatewayReferenceAdapter(app=SimpleNamespace(config=SimpleNamespace(state_dir='.')))
        with patch.object(mcp, 'dispatch', return_value=response), self.assertRaises(GatewayBusy):
            adapter._call('generate_image', request['params']['arguments'])
        with patch.object(mcp, 'call', side_effect=RuntimeError('after accept')):
            response = mcp.dispatch(None, request)
        with patch.object(mcp, 'dispatch', return_value=response), self.assertRaises(RuntimeError):
            adapter._call('generate_image', request['params']['arguments'])
