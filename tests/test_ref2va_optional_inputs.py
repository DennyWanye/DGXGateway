"""Optional-input contract; isolated business data and mocked GPU transport."""
import itertools
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from DGXGateway.dgx_gateway import mcp
from DGXGateway.dgx_gateway.drama.business_mcp import call
from DGXGateway.dgx_gateway.video_contracts import LONG_PROFILE, TEXT_PROFILE, generation_for_references
from DGXGateway.dgx_gateway.video_jobs import _row_runtime
from tests import test_dgx_video_jobs as gateway
from tests import test_dgx_drama_video as business

class Ref2VAOptionalGatewayTests(unittest.TestCase):
    def setUp(self):
        self.f = gateway.VideoJobTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_sixteen_input_combinations_freeze_ref2va_and_keep_supplied_assets(self):
        self.f.jobs.video_refs.get = Mock(return_value={})
        shots = []
        for i, (text, image, video, audio) in enumerate(itertools.product((False, True), repeat=4)):
            shots.append(dict(name=f'optional-{i}', seed=i, prompt='exact text' if text else '',
                image_asset_id=self.f.image['asset_id'] if image else '',
                video_asset_id='b'*64 if video else '',
                audio_asset_id=self.f.audio['asset_id'] if audio else '', request_duration_seconds=7))
        schema = next(t['inputSchema'] for t in mcp.TOOLS if t['name'] == 'video_batch_submit')
        params = dict(shots=shots, profile=LONG_PROFILE, idempotency_key='optional-matrix')
        mcp._validate(schema, params)
        with patch('threading.Thread'):
            row = self.f.jobs.submit(shots, 'optional-matrix', LONG_PROFILE)
        self.assertEqual(row['runtime_task'], 'ref2va')
        for original, frozen in zip(shots, row['shots']):
            for key, value in original.items():
                self.assertEqual(frozen[key], value)

    def test_omitted_inputs_and_idempotency(self):
        with patch('threading.Thread'):
            first = self.f.jobs.submit([{'name': 'empty', 'seed': 42}], 'optional-empty')
            again = self.f.jobs.submit([{'name': 'empty', 'seed': 42}], 'optional-empty')
        self.assertEqual(first['job_id'], again['job_id'])
        for key in ('prompt', 'image_asset_id', 'audio_asset_id', 'video_asset_id'):
            self.assertEqual(first['shots'][0][key], '')
        self.assertEqual(first['runtime_task'], 'ref2va')

    def test_incompatible_live_partition_rejected_before_post(self):
        ring = SimpleNamespace(observe=lambda: SimpleNamespace(state='HEALTHY', profile='ref2va-official'))
        self.f.app.operations = SimpleNamespace(adapters={'h3-ring4': ring})
        with self.assertRaisesRegex(BlockingIOError, 'does not serve t2va'):
            self.f.jobs.submit([{'name': 'legacy', 'seed': 42, 'prompt': 'text'}], 'legacy-wrong-partition', TEXT_PROFILE)
        self.assertEqual(list(self.f.jobs.root.glob('*.json')), [])

    def test_historical_frozen_t2va_identity_is_not_rewritten(self):
        row = dict(runtime_model='h3-ring4', runtime_profile='fl2va-official', runtime_task='t2va')
        self.assertEqual(_row_runtime(row), ('h3-ring4', 'fl2va-official', 't2va'))
        self.assertEqual(generation_for_references(None, False)['profile'], LONG_PROFILE)
        self.assertEqual(generation_for_references(None, True)['profile'], LONG_PROFILE)

class Ref2VAOptionalBusinessTests(unittest.TestCase):
    def setUp(self):
        self.f = business.DramaVideoTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)

    def test_business_mcp_omitted_text_and_audio_are_audited_then_dispatched_empty(self):
        f = self.f
        shot = f.service.create_shot(f.eid, {'title': 'optional input', 'requestKey': f.key()})['shot']
        current = f.service.shot_production(shot['id'])
        body = {'episodeId': f.eid, 'shots': [{'shotId': shot['id'], 'shotRevisionId': current['currentRevisionId'], 'seed': 42}], 'requestKey': f.key()}
        row = call(f.service, 'drama_request_video_take', {'projectId': f.pid, 'arguments': body})['job']
        self.assertEqual(row['items'][0]['generation']['profile'], LONG_PROFILE)
        self.assertEqual(row['items'][0]['prompt'], '')
        f.service.dispatch_video_once()
        self.assertEqual(f.service.video_job(row['id'])['job']['state'], 'SUBMITTED')
        self.assertEqual(f.adapter.calls[0][0][0]['prompt'], '')
        self.assertEqual(f.adapter.registered[0]['images'], [])
        self.assertEqual(f.adapter.registered[0]['audios'], [])

    def test_saved_legacy_text_profile_can_submit_without_revising_creative_content(self):
        f = self.f
        shot = f.service.create_shot(f.eid, {'title': 'text only', 'requestKey': f.key()})['shot']
        current = f.service.shot_production(shot['id'])
        # Legacy profile is an input alias; matching duration remains required.
        body = {'shots': [{'shotId': shot['id'], 'shotRevisionId': current['currentRevisionId'], 'prompt': 'unchanged', 'seed': 42,
                          'generation': {'contractVersion': 'h3-ref2va-v1', 'profile': TEXT_PROFILE, 'requestDurationSeconds': 4}}], 'requestKey': f.key()}
        row = f.service.submit_video_batch(f.eid, body)['job']
        self.assertEqual(row['items'][0]['prompt'], 'unchanged')
        self.assertEqual(row['items'][0]['generation']['profile'], LONG_PROFILE)
        self.assertEqual(f.service.shot_production(shot['id'])['currentRevisionId'], current['currentRevisionId'])

if __name__ == '__main__': unittest.main()
