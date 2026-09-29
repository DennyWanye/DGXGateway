"""Text-only workbench -> Gateway -> T2VA transport; all model IO mocked."""
import json
import unittest
from unittest.mock import Mock, patch
from tests import test_dgx_drama_video as business
from tests import test_dgx_video_jobs as gateway
from DGXGateway.dgx_gateway.drama.gateway_video_adapter import GatewayVideoAdapter
from DGXGateway.dgx_gateway.video_contracts import TEXT_PROFILE, generation_for_reference, normalize_generation
from DGXGateway.dgx_gateway import mcp


class TextVideoBusinessTests(unittest.TestCase):
    def setUp(self):
        self.f = business.DramaVideoTests(); self.f.setUp(); self.addCleanup(self.f.tearDown)

    def test_no_image_freezes_text_contract_and_dispatches_without_assets(self):
        f = self.f
        shot = f.service.create_shot(f.eid, {'title': '纸船随水漂流', 'requestKey': f.key()})['shot']
        current = f.service.shot_production(shot['id'])
        item = {'shotId': shot['id'], 'shotRevisionId': current['currentRevisionId'], 'audioAssetId': '', 'prompt': 'A paper boat floats on a quiet stream, no speech.', 'seed': 42}
        body = {'shots': [item], 'requestKey': f.key()}
        job = f.service.submit_video_batch(f.eid, body)['job']
        self.assertEqual(job['items'][0]['generation']['profile'], TEXT_PROFILE)
        self.assertEqual(job['items'][0]['imageAssetId'], '')
        self.assertEqual(job['items'][0]['audioAssetId'], '')
        self.assertEqual(f.service.submit_video_batch(f.eid, body)['job']['id'], job['id'])
        def submit(shots, key, images, audios, gate, *, profile):
            self.assertEqual(profile, TEXT_PROFILE); self.assertEqual(images, []); self.assertEqual(audios, [])
            row = business.VideoAdapter.submit(f.adapter, shots, key, images, audios, gate)
            row['profile'] = profile
            f.adapter.record['profile'] = profile
            return row
        f.adapter.submit = Mock(side_effect=submit)
        f.service.dispatch_video_once()
        self.assertEqual(f.service.video_job(job['id'])['job']['state'], 'SUBMITTED')
        f.adapter.submit.assert_called_once()

    def test_text_audio_not_silently_ignored(self):
        f = self.f; shot = f.service.create_shot(f.eid, {'title': '文字镜头', 'requestKey': f.key()})['shot']
        current = f.service.shot_production(shot['id'])
        with self.assertRaisesRegex(Exception, '参考音频'):
            f.service.submit_video_batch(f.eid, {'shots': [{'shotId': shot['id'], 'shotRevisionId': current['currentRevisionId'], 'audioAssetId': f.audio, 'prompt': 'Move slowly', 'seed': 1}], 'requestKey': f.key()})


class TextVideoGatewayTests(unittest.TestCase):
    def setUp(self):
        self.f = gateway.VideoJobTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.shot = {**self.f.shot, 'image_asset_id': '', 'audio_asset_id': ''}

    def test_persistent_text_request_idempotency_and_ref_mode_still_requires_assets(self):
        with patch('threading.Thread'):
            row = self.f.jobs.submit([self.shot], 'text-video-gateway', TEXT_PROFILE)
            again = self.f.jobs.submit([self.shot], 'text-video-gateway', TEXT_PROFILE)
        self.assertEqual(row['job_id'], again['job_id'])
        with self.assertRaises(ValueError): self.f.jobs.submit([self.shot], 'text-video-wrong-mode')
        with self.assertRaises(ValueError): self.f.jobs.submit([self.f.shot], 'text-with-image-no', TEXT_PROFILE)
        with self.assertRaises(ValueError): normalize_generation({**generation_for_reference(None, ''), 'requestDurationSeconds': 15})

    def test_multipart_is_native_t2va_eight_steps_without_fake_image_or_silence(self):
        jobs = self.f.jobs
        with patch('threading.Thread'): row = jobs.submit([self.shot], 'text-video-wire-test', TEXT_PROFILE)
        response = Mock(); response.headers.get_content_type.return_value = 'video/mp4'; response.read.return_value = b'fake-mp4'
        response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
        jobs.http = Mock(); jobs.http.open.return_value = response; jobs._token = Mock(return_value='test-token')
        jobs._publish_result = Mock(return_value={'path': 'test.mp4'})
        jobs._generate(row, row['shots'][0])
        req = jobs.http.open.call_args.args[0]; body = req.data.decode()
        self.assertIn('"task":"t2va"', body); self.assertIn('"required_profile":"10eros-fl2va-v1"', body)
        self.assertIn('name="num_inference_steps"\r\n\r\n8\r\n', body)
        self.assertNotIn('name="reference_image"', body); self.assertNotIn('name="reference_audio"', body)
        self.assertEqual(row['shots'][0]['submission_phase'], 'REQUEST_STARTED')

    def test_business_adapter_accepts_empty_registration_only_when_refs_match(self):
        adapter = GatewayVideoAdapter.__new__(GatewayVideoAdapter)
        adapter._call = Mock(return_value={'job_id': 'b'*32})
        gate = Mock()
        adapter.submit([self.shot], 'text-video-adapter', [], [], gate, profile=TEXT_PROFILE)
        gate.assert_called_once(); adapter._call.assert_called_once()
        self.assertEqual(adapter._call.call_args.args[0], 'video_batch_submit')
        self.assertEqual(adapter._call.call_args.args[1]['profile'], TEXT_PROFILE)
        with self.assertRaises(Exception): adapter.submit([self.f.shot], 'missing-refs', [], [], gate)
