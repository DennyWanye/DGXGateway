"""P4 frozen oracle. Local socket != a real DGX wall-clock/model qualification."""
import copy
import http.client
import json
import socket
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from DGXGateway.dgx_gateway.video_contracts import (CONTRACT_VERSION, LONG_PROFILE, STABLE_PROFILE,
    gateway_generation, normalize_generation, video_capabilities)
from DGXGateway.dgx_gateway.video_jobs import SubmissionUnknown, VideoJobs
from DGXGateway.dgx_gateway.video_transport import LongVideoTransport
from DGXGateway.dgx_gateway.job_cancel import JobCanceled
from DGXGateway.dgx_gateway.drama.contracts import DramaError
from tests import test_dgx_video_safety as safety
from tests import test_dgx_drama_video as business


def generation(seconds=15, profile=LONG_PROFILE):
    return dict(contractVersion=CONTRACT_VERSION, profile=profile, requestDurationSeconds=seconds)


class ContractTests(unittest.TestCase):
    def test_legacy_default_and_separate_verified_sets(self):
        self.assertEqual(normalize_generation(), generation(4, STABLE_PROFILE))
        profiles = {p['id']: p for p in video_capabilities()['profiles']}
        stable, long = profiles[STABLE_PROFILE], profiles[LONG_PROFILE]
        self.assertEqual(stable['verifiedPersistentDurationSeconds'], [4])
        self.assertEqual(long['verifiedPersistentDurationSeconds'], [4])
        self.assertEqual(long['verifiedDirectDurationSeconds'], [4])
        self.assertEqual(long['workbenchDurationSeconds'], list(range(4, 16)))
        self.assertIsNone(long['wait']['totalTimeoutSeconds'])

    def test_gateway_and_workbench_accept_every_integer_from_four_to_fifteen(self):
        for seconds in range(4, 16):
            self.assertEqual(gateway_generation(LONG_PROFILE, {'request_duration_seconds': seconds}), generation(seconds))
            self.assertEqual(normalize_generation(generation(seconds)), generation(seconds))
        for seconds in (3, 16, 4.0, True, '15', None):
            with self.assertRaises(ValueError): gateway_generation(LONG_PROFILE, {'request_duration_seconds': seconds})
        for value in (generation(15, STABLE_PROFILE), generation(4, 'foreign'), {**generation(), 'contractVersion': 'v2'}, {**generation(), 'extra': 1}):
            with self.assertRaises(ValueError): normalize_generation(value)


class SocketServer:
    """Real loopback socket, controlled sync response and independent cancel handler."""
    def __init__(self, response=200, disconnect=False):
        self.started, self.release, self.canceled = threading.Event(), threading.Event(), threading.Event()
        self.calls, self.cancel_ids, self.request_body = [], [], b''
        self.response, self.disconnect = response, disconnect
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b'query-responsive')
            def do_POST(self):
                self.rfile.read(0)
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                request_id = self.headers.get('X-H3-Request-ID')
                if self.path == '/v1/videos/cancel':
                    owner.cancel_ids.append(request_id)
                    self.send_response(200 if request_id in owner.calls else 404); self.end_headers(); self.wfile.write(b'{}')
                    owner.canceled.set(); return
                owner.calls.append(request_id); owner.request_body = body; owner.started.set()
                owner.release.wait(3)
                if owner.disconnect:
                    self.connection.shutdown(socket.SHUT_RDWR); self.connection.close(); return
                self.send_response(owner.response); self.send_header('Content-Type', 'video/mp4'); self.end_headers()
                try:
                    self.wfile.write(b'H3 request canceled at a collective safe boundary' if owner.response == 500 else b'fixture-response')
                except (BrokenPipeError, ConnectionResetError): pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/v1/videos/sync'
    def close(self):
        self.release.set(); self.server.shutdown(); self.server.server_close(); self.thread.join()


class TransportTests(unittest.TestCase):
    def start(self, response=200, disconnect=False):
        server = SocketServer(response, disconnect); self.addCleanup(server.close)
        transport = LongVideoTransport(); transport.connect_timeout = .04; transport.cancel_timeout = .2; transport.cancel_poll_seconds = .005
        canceled, receipts, output = threading.Event(), [], []
        request = urllib.request.Request(server.url, data=b'fixture', headers={'Authorization': 'Bearer fixture-not-a-secret', 'X-H3-Request-ID': 'active-original-id'})
        def run():
            try: output.append(transport.read(server.url, request, 1024, canceled.is_set, receipts.append))
            except Exception as exc: output.append(exc)
        thread = threading.Thread(target=run); thread.start(); self.addCleanup(lambda: thread.join(4))
        self.assertTrue(server.started.wait(1))
        return server, thread, canceled, receipts, output

    def test_real_socket_exceeds_scaled_deadline_and_query_remains_responsive(self):
        server, thread, _, _, output = self.start()
        time.sleep(.15)  # >3x actual connect timeout; response socket must be unlimited.
        conn = http.client.HTTPConnection('127.0.0.1', server.server.server_port, timeout=.2)
        conn.request('GET', '/query'); response = conn.getresponse(); self.assertEqual(response.read(), b'query-responsive'); conn.close()
        self.assertTrue(thread.is_alive()); self.assertEqual(server.calls, ['active-original-id']); self.assertEqual(output, [])
        server.release.set(); thread.join(1)
        self.assertEqual(output, [('video/mp4', b'fixture-response')])

    def test_cancel_ack_is_same_identity_and_does_not_finish_original_socket(self):
        server, thread, cancel, receipts, output = self.start(response=500)
        foreign = http.client.HTTPConnection('127.0.0.1', server.server.server_port, timeout=.2)
        foreign.request('POST', '/v1/videos/cancel', headers={'X-H3-Request-ID': 'unrelated-id'})
        self.assertEqual(foreign.getresponse().status, 404); foreign.close(); server.canceled.clear()
        cancel.set(); self.assertTrue(server.canceled.wait(1)); time.sleep(.04)
        self.assertEqual(receipts, [200]); self.assertEqual(server.cancel_ids, ['unrelated-id', 'active-original-id'])
        self.assertTrue(thread.is_alive()); self.assertEqual(output, [])
        server.release.set(); thread.join(1)
        self.assertIsInstance(output[0], JobCanceled)

    def test_disconnect_is_ambiguous_and_not_replayed(self):
        server, thread, _, _, output = self.start(disconnect=True)
        server.release.set(); thread.join(1)
        self.assertIsInstance(output[0], http.client.RemoteDisconnected)
        self.assertEqual(server.calls, ['active-original-id'])


class GatewayTests(unittest.TestCase):
    def setUp(self):
        safety.VideoSafetyTests.setUp(self)
        # Preserve coverage for the retired two-node synchronous adapter. The
        # published LONG_PROFILE now routes to Ring4 and is tested separately.
        self._legacy_runtime = patch(
            'DGXGateway.dgx_gateway.video_jobs.runtime_target',
            side_effect=lambda profile: ('minimax-h3', profile, 'ref2va'),
        )
        self._legacy_runtime.start()
        self.addCleanup(self._legacy_runtime.stop)
    def submit(self, seconds=15, profile=LONG_PROFILE, key='p4-original-key'):
        with patch('DGXGateway.dgx_gateway.video_jobs.threading.Thread'):
            row = self.jobs.submit([{**self.shot, 'request_duration_seconds': seconds}], key, profile, non_preemptive=True)
        return row

    def test_contract_idempotency_and_legacy_missing_fields(self):
        row = self.submit()
        self.assertEqual(self.submit()['job_id'], row['job_id'])
        for seconds, profile in ((4, LONG_PROFILE), (4, STABLE_PROFILE)):
            with self.assertRaises(ValueError): self.submit(seconds, profile)
        self.jobs.active = None
        stable = self.submit(4, STABLE_PROFILE, 'legacy-key')
        stable['shots'][0].pop('request_duration_seconds'); stable['shots'][0].pop('contract_version'); self.jobs.save(stable)
        self.assertEqual(self.submit(4, STABLE_PROFILE, 'legacy-key')['job_id'], stable['job_id'])

    def test_invalid_contract_never_creates_durable_job(self):
        for seconds in (3, 16, True, 4.1):
            with self.assertRaises(ValueError): self.submit(seconds)
        self.assertEqual(list(self.jobs.root.glob('*.json')), [])

    def test_request_fsync_identity_duration_and_virtual_old_deadline(self):
        row = self.submit(); row['state'] = 'RUNNING'; self.jobs.save(row)
        self.jobs._token = lambda: 'fixture-not-a-secret'
        def read(endpoint, request, maximum, canceled, receipt):
            saved = json.loads((self.jobs.root / (row['job_id'] + '.json')).read_text())
            self.assertEqual(saved['shots'][0]['submission_phase'], 'REQUEST_STARTED')
            self.assertEqual(request.get_header('X-h3-request-id'), saved['shots'][0]['request_id'])
            self.assertIn(b'"duration":15.0', request.data); self.assertIn(LONG_PROFILE.encode(), request.data)
            for logical_time in (3701, 4400):
                with patch('DGXGateway.dgx_gateway.video_jobs.time.monotonic', return_value=logical_time):
                    queried = self.jobs.get(row['job_id'])
                    self.assertEqual(queried['state'], 'RUNNING'); self.assertFalse(queried['memory_released'])
                    self.assertEqual(queried['shots'][0]['request_id'], saved['shots'][0]['request_id'])
            raise ConnectionResetError('received then disconnected')
        self.jobs.long_transport.read = Mock(side_effect=read)
        with self.assertRaises(SubmissionUnknown): self.jobs._generate(row, row['shots'][0])
        self.assertEqual(row['shots'][0]['submission_phase'], 'UNKNOWN')
        with self.assertRaises(SubmissionUnknown): self.jobs._generate(row, row['shots'][0])
        self.jobs.long_transport.read.assert_called_once()
        recovered = VideoJobs(self.app).find('p4-original-key')
        self.assertEqual(recovered['job_id'], row['job_id']); self.assertEqual(recovered['shots'][0]['request_id'], row['shots'][0]['request_id'])

    def test_stable_keeps_original_timeout_and_no_redirects(self):
        row = self.submit(4, STABLE_PROFILE); self.jobs._token = lambda: 'fixture-not-a-secret'
        self.jobs.http.open = Mock(side_effect=TimeoutError('stable cutoff'))
        self.jobs._wait_original_result = Mock(return_value=None)
        with self.assertRaises(SubmissionUnknown): self.jobs._generate(row, row['shots'][0])
        self.assertEqual(self.jobs.http.open.call_args.kwargs['timeout'], 30)
        self.jobs.long_transport.read = Mock(); self.jobs.long_transport.read.assert_not_called()
        from DGXGateway.dgx_gateway.video_jobs import NoVideoRedirect
        with self.assertRaises(urllib.error.HTTPError): NoVideoRedirect().redirect_request(urllib.request.Request('http://127.0.0.1/'), None, 302, 'redirect', {}, 'http://foreign/')

    def test_cancel_receipt_not_terminal_no_next_clip_until_safe_response(self):
        row = self.submit(); row['shots'].append({**row['shots'][0], 'name': 'later', 'index': 1, 'request_id': 'f'*32})
        self.jobs.save(row); self.jobs._token = lambda: 'fixture-not-a-secret'
        def read(endpoint, request, maximum, canceled, receipt):
            self.jobs.cancel(row['job_id']); self.jobs.cancel(row['job_id'])
            self.assertTrue(canceled()); self.assertTrue((self.jobs.root / (row['job_id'] + '.cancel')).exists())
            receipt(200)
            current = self.jobs.get(row['job_id'])
            self.assertEqual(current['state'], 'RUNNING'); self.assertFalse(current['memory_released'])
            self.assertTrue(current['cooperative_cancel']['acknowledgement_only'])
            self.assertEqual(current['cooperative_cancel']['request_id'], row['shots'][0]['request_id'])
            raise JobCanceled('collective boundary confirmed')
        self.jobs.long_transport.read = Mock(side_effect=read)
        self.jobs.switch = Mock()
        self.jobs.run(row)
        result = self.jobs.get(row['job_id'])
        self.assertEqual(result['state'], 'CANCELED'); self.assertTrue(result['memory_released'])
        self.assertEqual(result['shots'][1]['submission_phase'], 'NOT_SUBMITTED')
        self.jobs.long_transport.read.assert_called_once()
        self.assertEqual([call.args[2] for call in self.jobs.switch.call_args_list], ['start', 'cleanup'])

    def test_unknown_cancel_retains_ownership_and_never_stops(self):
        row = self.submit(); self.jobs._token = lambda: 'fixture-not-a-secret'
        def read(*args):
            self.jobs.cancel(row['job_id']); raise urllib.error.HTTPError('http://fixture', 503, 'uncertain', {}, None)
        self.jobs.long_transport.read = Mock(side_effect=read); self.jobs.switch = Mock()
        self.jobs.run(row); result = self.jobs.get(row['job_id'])
        self.assertEqual(result['state'], 'SUBMISSION_UNKNOWN'); self.assertFalse(result['memory_released']); self.assertTrue(result['cleanup_required'])
        self.assertEqual([call.args[2] for call in self.jobs.switch.call_args_list], ['start'])
        self.assertEqual(self.jobs.active, row['job_id'])

    def test_encoding_health_jitter_real_socket_has_no_ownership_side_effects(self):
        from DGXGateway.dgx_gateway.domain import ObservedModel
        server = SocketServer(); self.addCleanup(server.close)
        row = self.submit(); self.jobs.endpoint = server.url; self.jobs._token = lambda: 'fixture-not-a-secret'
        self.jobs.long_transport.connect_timeout = .04
        self.jobs.switch = Mock()
        thread = threading.Thread(target=self.jobs.run, args=(row,)); thread.start()
        try:
            self.assertTrue(server.started.wait(1)); time.sleep(.12)
            for state in ('PARTIAL', 'HEALTHY'):
                self.h3.observe.return_value = ObservedModel('minimax-h3', state, LONG_PROFILE)
                current = self.jobs.get(row['job_id'])
                self.assertEqual(current['state'], 'RUNNING'); self.assertFalse(current['memory_released'])
                self.assertEqual(current['shots'][0]['submission_phase'], 'REQUEST_STARTED')
                self.assertEqual(server.calls, [row['shots'][0]['request_id']])
                self.assertEqual([c.args[2] for c in self.jobs.switch.call_args_list], ['start'])
        finally:
            server.release.set(); thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_long_cancel_during_load_sends_no_generation_or_cooperative_call(self):
        row = self.submit()
        self.jobs.long_transport.read = Mock()
        def switch(value, target, suffix):
            if suffix == 'start': self.jobs.cancel(row['job_id'])
        self.jobs.switch = Mock(side_effect=switch); self.jobs.run(row)
        result = self.jobs.get(row['job_id'])
        self.assertEqual(result['state'], 'CANCELED'); self.assertTrue(result['memory_released'])
        self.assertEqual(result['shots'][0]['submission_phase'], 'NOT_SUBMITTED')
        self.jobs.long_transport.read.assert_not_called()

    def test_confirmed_cancel_with_release_failure_is_interrupted_not_released(self):
        row = self.submit(); self.jobs._token = lambda: 'fixture-not-a-secret'
        def read(*args):
            self.jobs.cancel(row['job_id']); raise JobCanceled('safe boundary')
        def switch(value, target, suffix):
            if suffix == 'cleanup': raise RuntimeError('release unavailable')
        self.jobs.long_transport.read = Mock(side_effect=read); self.jobs.switch = Mock(side_effect=switch)
        self.jobs.run(row); result = self.jobs.get(row['job_id'])
        self.assertEqual(result['state'], 'INTERRUPTED'); self.assertFalse(result['memory_released']); self.assertTrue(result['cleanup_required'])
        self.assertEqual(self.jobs.active, row['job_id'])


class LongAdapter(business.VideoAdapter):
    def submit(self, shots, key, images, audios, before_submit, profile=STABLE_PROFILE):
        try:
            super().submit(shots, key, images, audios, before_submit)
            return {**copy.deepcopy(self.record), 'profile': profile}
        finally:
            if self.record: self.record['profile'] = profile


class BusinessTests(unittest.TestCase):
    setUp = business.DramaVideoTests.setUp
    tearDown = business.DramaVideoTests.tearDown
    key = business.DramaVideoTests.key
    stop_poller = business.DramaVideoTests.stop_poller
    shot = business.DramaVideoTests.shot

    def long_shot(self, seconds=15):
        item = self.shot(); current = self.service.shot_production(item['shotId'])
        self.service.revise_shot(item['shotId'], {'baseRevisionId': current['currentRevisionId'], 'expectedRevision': current['revision'],
            'snapshot': {**current['current']['snapshot'], 'generation': generation(seconds)}, 'requestKey': self.key()})
        current = self.service.shot_production(item['shotId'])
        self.service.select_reference(item['shotId'], {'expectedRevision': current['revision'], 'assetId': self.image, 'requestKey': self.key()})
        current = self.service.shot_production(item['shotId']); item['shotRevisionId'] = current['currentRevisionId']; item['generation'] = generation(seconds)
        return item

    def use_long_adapter(self):
        self.adapter = LongAdapter(); self.service.video_adapter = self.adapter

    def test_mixed_profile_and_forged_generation_rejected_before_queue(self):
        long = self.long_shot()
        with self.assertRaises(DramaError): self.service.submit_video_batch(self.eid, {'shots': [self.shot(), long], 'requestKey': self.key()})
        with self.assertRaises(DramaError): self.service.submit_video_batch(self.eid, {'shots': [{**long, 'generation': generation(4)}], 'requestKey': self.key()})
        self.assertEqual(self.service.video_jobs(self.pid)['jobs'], [])

    def test_frozen_reservation_19_and_changed_key_contract_rejected(self):
        body = {'shots': [self.long_shot(4), self.long_shot(15)], 'requestKey': self.key()}
        first = self.service.submit_video_batch(self.eid, body)
        self.assertEqual(self.service.production_state(self.pid)['reservedSeconds'], 19)
        self.assertEqual(self.service.submit_video_batch(self.eid, body), first)
        changed = copy.deepcopy(body); changed['shots'][0]['generation'] = generation(15)
        with self.assertRaises(DramaError): self.service.submit_video_batch(self.eid, changed)
        self.assertEqual(self.service.production_state(self.pid)['reservedSeconds'], 19)

    def test_lost_persistent_receipt_restart_recovers_original_profile_id(self):
        self.use_long_adapter(); self.adapter.failure = 'lost'
        job = self.service.submit_video_batch(self.eid, {'shots': [self.long_shot()], 'requestKey': self.key()})['job']
        self.service.dispatch_video_once(); self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.adapter.calls[0][0][0]['request_duration_seconds'], 15)
        self.service.close()
        from DGXGateway.dgx_gateway.drama.service import DramaService
        self.service = DramaService(self.root, self.root, video_adapter=self.adapter); self.stop_poller()
        self.service.dispatch_video_once()
        recovered = self.service.refresh_video_job(job['id'])['job']
        self.assertEqual(len(self.adapter.calls), 1); self.assertEqual(recovered['gatewayJobId'], 'a'*32)
        self.assertEqual(recovered['items'][0]['generation'], generation())
        self.assertTrue(self.adapter.lookups)

    def test_forged_gateway_profile_and_duration_cannot_bind(self):
        self.use_long_adapter()
        job = self.service.submit_video_batch(self.eid, {'shots': [self.long_shot()], 'requestKey': self.key()})['job']
        self.service.dispatch_video_once()
        for change in ('profile', 'duration', 'version'):
            record = copy.deepcopy(self.adapter.record)
            if change == 'profile': record['profile'] = STABLE_PROFILE
            elif change == 'duration': record['shots'][0]['request_duration_seconds'] = 4
            else: record['shots'][0]['contract_version'] = 'unknown'
            with self.assertRaises(ValueError): self.service._bind_video_record(job['id'], record)
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.service.video_job(job['id'])['job']['items'][0]['generation'], generation())

    def test_long_media_full_decode_partial_unknown_and_continuation(self):
        import subprocess
        self.use_long_adapter()
        job = self.service.submit_video_batch(self.eid, {'shots': [self.long_shot(4), self.long_shot(15), self.long_shot(15)], 'requestKey': self.key()})['job']
        self.service.dispatch_video_once()
        path = self.root / 'fixture-long.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=1344x768:r=24:d=4.458333333',
                        '-f', 'lavfi', '-i', 'anullsrc=r=24000:cl=mono', '-t', '4.458333333', '-c:v', 'libx264',
                        '-preset', 'ultrafast', '-threads', '2', '-c:a', 'aac', '-pix_fmt', 'yuv420p', str(path)], check=True)
        self.adapter.payloads = [{'index': 0, 'sourceKey': '0.mp4', 'filename': '0.mp4', 'data': path.read_bytes()}]
        self.adapter.record['shots'][0].update(state='SUCCEEDED', submission_phase='RECEIVED')
        self.adapter.record['shots'][1].update(state='SUBMISSION_UNKNOWN', submission_phase='UNKNOWN')
        self.adapter.record.update(state='INTERRUPTED', memory_released=False, cleanup_required=True)
        first = self.service.refresh_video_job(job['id'])['job']; take = first['items'][0]['takeId']
        self.assertTrue(take); self.assertEqual(self.service.production_state(self.pid)['reservedSeconds'], 15)
        self.assertIsNone(first['items'][1]['takeId']); self.assertTrue(first['canContinue'])
        again = self.service.refresh_video_job(job['id'])['job']; self.assertEqual(again['items'][0]['takeId'], take)
        continuation = self.service.continue_video_job(job['id'], {'requestKey': self.key()})['job']
        self.assertEqual(len(continuation['items']), 1); self.assertEqual(continuation['items'][0]['generation'], generation())
        self.assertEqual(continuation['items'][0]['shotId'], first['items'][2]['shotId'])
        self.service.dispatch_video_once(); self.assertEqual(len(self.adapter.calls), 1)  # unknown owns reservation/engine

    def test_fifteen_second_actual_frames_accepted_truncated_rejected(self):
        import subprocess
        self.use_long_adapter()
        job = self.service.submit_video_batch(self.eid, {'shots': [self.long_shot(), self.long_shot()], 'requestKey': self.key()})['job']
        self.service.dispatch_video_once()
        path = self.root / 'fixture-15.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=1344x768:r=24:d=15.083333333',
                        '-f', 'lavfi', '-i', 'anullsrc=r=24000:cl=mono', '-t', '15.083333333', '-c:v', 'libx264',
                        '-preset', 'ultrafast', '-threads', '2', '-c:a', 'aac', '-pix_fmt', 'yuv420p', str(path)], check=True)
        data = path.read_bytes()
        self.adapter.payloads = [{'index': i, 'sourceKey': f'{i}.mp4', 'filename': f'{i}.mp4', 'data': data if i == 0 else data[:len(data)//2]} for i in (0, 1)]
        for shot in self.adapter.record['shots']: shot.update(state='SUCCEEDED', submission_phase='RECEIVED')
        self.adapter.record.update(state='SUCCEEDED', memory_released=True)
        result = self.service.refresh_video_job(job['id'])['job']
        self.assertEqual(result['state'], 'FAILED')
        self.assertTrue(result['items'][0]['takeId']); self.assertEqual(result['items'][1]['state'], 'INVALID'); self.assertIsNone(result['items'][1]['takeId'])
        from DGXGateway.dgx_gateway.drama.media import probe_media
        media = probe_media(path)
        self.assertEqual(media['frames'], 362); self.assertGreater(media['duration'], 15)
        self.assertEqual(self.service.production_state(self.pid)['reservedSeconds'], 0)


from tests import test_dgx_gateway_video_adapter as adapter_tests

class AdapterTests(unittest.TestCase):
    setUp = adapter_tests.VideoGatewayAdapterTests.setUp
    def test_mcp_registration_preserves_long_contract_and_bounded_submit_rpc(self):
        import hashlib, base64
        calls = []
        def rpc(name, arguments):
            calls.append((name, arguments))
            if name in {'comfy_register_image', 'video_register_audio'}:
                field = 'image_base64' if name == 'comfy_register_image' else 'audio_base64'
                return {'asset_id': hashlib.sha256(base64.b64decode(arguments[field])).hexdigest()}
            return {'job_id': 'b'*32}
        shots = [{**self.shots[0], 'request_duration_seconds': 15, 'contract_version': CONTRACT_VERSION}]
        with patch.object(self.adapter, '_call', side_effect=rpc):
            self.adapter.submit(shots, 'long-original-key', self.images, self.audios, lambda: None, profile=LONG_PROFILE)
        self.assertEqual(calls[-1], ('video_batch_submit', {'shots': shots, 'idempotency_key': 'long-original-key', 'profile': LONG_PROFILE, 'non_preemptive': True}))
        with patch.object(self.adapter.http, 'open', side_effect=TimeoutError('bounded submit RPC')) as opened:
            with self.assertRaises(TimeoutError): self.adapter._call('video_job', {'job_id': 'b'*32})
        self.assertEqual(opened.call_args.kwargs['timeout'], 30)

    def test_invalid_stable_duration_is_rejected_before_any_registration(self):
        with patch.object(self.adapter, '_call') as call:
            with self.assertRaises(adapter_tests.GatewayRejected):
                self.adapter.submit([{**self.shots[0], 'request_duration_seconds': 15}], 'invalid-duration', self.images, self.audios, lambda: None)
        call.assert_not_called()
