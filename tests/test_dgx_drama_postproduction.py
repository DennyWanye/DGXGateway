"""P5 frozen oracles: versions, exact audio scope, exports and human approval."""
import io
import json
import math
from pathlib import Path
import subprocess
import tempfile
import time
import threading
import shutil
import unittest
from unittest.mock import patch

from tests.test_dgx_drama_service import DramaServiceTests
from DGXGateway.dgx_gateway.drama.contracts import DramaError
from DGXGateway.dgx_gateway.drama.postproduction import mapping_hash


class PostproductionTests(DramaServiceTests):
    # Reuse setup helpers, not duplicated inherited scenario counts.
    def audio(self):
        path = self.root / 'voice.wav'
        if not path.exists():
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=550:sample_rate=48000', '-t', '6', str(path)], check=True)
        data = path.read_bytes()
        return self.service.upload_audio(self.pid, io.BytesIO(data), len(data), 'voice.wav', 'audio')['asset']

    def setup_post(self):
        shot = self.shot(self.asset()['id'])
        clips = [{'clipId': 'a', 'takeId': shot['selectedTakeId'], 'inFrame': 0, 'frames': 96, 'keepSourceAudio': False, 'fit': 'contain'},
                 {'clipId': 'b', 'takeId': shot['selectedTakeId'], 'inFrame': 0, 'frames': 96, 'keepSourceAudio': False, 'fit': 'contain'}]
        voice = {'audioClipId': 'v', 'assetId': self.audio()['id'], 'track': 'voiceover', 'startFrame': 24, 'sourceInFrame': 12, 'frames': 120}
        body = {'clips': clips, 'audioClips': [voice], 'expectedRevision': 0, 'requestKey': 'v2'}
        tl = self.service.save_timeline(self.eid, body)['timeline']
        return tl, body

    def test_p5_audio_idempotence_project_and_bounds(self):
        aid = self.audio()['id']
        self.assertEqual(aid, self.audio()['id'])
        tl, body = self.setup_post()
        self.assertEqual(tl['audioClips'][0]['frames'], 120)
        cases = [{'frames': 180}, {'startFrame': 100}, {'sourceInFrame': 48}, {'gainDb': math.nan}, {'gainDb': 13}, {'fadeInFrames': 100, 'fadeOutFrames': 100}, {'track': 'remote'}]
        for i, patch_value in enumerate(cases):
            with self.subTest(patch_value=patch_value), self.assertRaises(DramaError):
                self.service.save_timeline(self.eid, {**body, 'expectedRevision': 1, 'requestKey': f'bad-{i}', 'audioClips': [{**body['audioClips'][0], **patch_value}]})
        other = self.service.create_project({'title': 'other', 'requestKey': 'other'})['project']['id']
        with self.service.store.connect() as db, self.assertRaises(DramaError):
            self.service.normalize_audio_clips(db, other, body['audioClips'], 192)

    def test_p5_explicit_crop_and_mapping_identity(self):
        tl, body = self.setup_post()
        with self.assertRaises(DramaError):
            self.service.save_timeline(self.eid, {**body, 'expectedRevision': 1, 'requestKey': 'missing-crop', 'clips': [{**c, 'fit': 'cover'} for c in body['clips']]})
        changed = self.service.save_timeline(self.eid, {**body, 'expectedRevision': 1, 'requestKey': 'crop', 'clips': [{**c, 'fit': 'cover', 'crop': {'x': 0, 'y': 1}} for c in body['clips']]})['timeline']
        self.assertEqual(mapping_hash(tl), mapping_hash(changed))
        moved = self.service.save_timeline(self.eid, {**body, 'expectedRevision': 2, 'requestKey': 'move-audio', 'audioClips': [{**body['audioClips'][0], 'startFrame': 0}]})['timeline']
        self.assertNotEqual(mapping_hash(tl), mapping_hash(moved))

    def test_p5_legacy_writer_cannot_drop_v2_audio(self):
        tl, body = self.setup_post()
        with self.assertRaises(DramaError) as error:
            self.service.save_timeline(self.eid, {'clips': body['clips'], 'expectedRevision': 1, 'requestKey': 'legacy'})
        self.assertEqual(error.exception.code, 'timeline_version_required')
        self.assertEqual(self.service.episode(self.eid)['timeline'], tl)

    def test_p5_subtitle_text_only_versions_cas_and_approval(self):
        tl, body = self.setup_post()
        payload = {'segments': [{'startFrame': 24, 'endFrame': 80, 'text': '你好，夜航。'}], 'expectedRevision': 0, 'requestKey': 'sub1'}
        sub = self.service.save_subtitles(tl['id'], payload)['subtitle']
        self.assertFalse(sub['approved'])
        self.assertEqual(sub, self.service.save_subtitles(tl['id'], payload)['subtitle'])
        original = self.service.episode(self.eid)
        with patch('DGXGateway.dgx_gateway.drama.post_media.render_dialogue_audio') as render:
            sub2 = self.service.save_subtitles(tl['id'], {**payload, 'expectedRevision': 1, 'requestKey': 'sub2', 'segments': [{'startFrame': 24, 'endFrame': 80, 'text': '今晚，启程。'}]})['subtitle']
        render.assert_not_called()
        self.assertEqual(sub['audioAssetHash'], sub2['audioAssetHash'])
        self.assertEqual(original, self.service.episode(self.eid))
        with self.assertRaises(DramaError) as conflict:
            self.service.save_subtitles(tl['id'], {**payload, 'requestKey': 'stale'})
        self.assertEqual(conflict.exception.status, 409)
        with self.assertRaises(DramaError):
            self.service.approve_subtitles(sub['id'], {'timelineId': tl['id'], 'requestKey': 'old-approve'})
        self.service.approve_subtitles(sub2['id'], {'timelineId': tl['id'], 'requestKey': 'approve'})
        changed = self.service.save_timeline(self.eid, {**body, 'expectedRevision': 1, 'requestKey': 'audio-move', 'audioClips': [{**body['audioClips'][0], 'startFrame': 0}]})['timeline']
        self.assertTrue(self.service.subtitles(changed['id'])['subtitles'][0]['needsReview'])
        with self.assertRaises(DramaError):
            self.service.create_export(changed['id'], {'requestKey': 'bad-subtitle', 'outputs': ['srt'], 'subtitleRevisionId': sub2['id']})

    def test_p5_multirole_export_real_decode_and_human_state(self):
        tl, _ = self.setup_post()
        sub = self.service.save_subtitles(tl['id'], {'segments': [{'startFrame': 0, 'endFrame': 48, 'text': '中文 {安全} \\ 换行\n第二行'}], 'expectedRevision': 0, 'requestKey': 'sub'})['subtitle']
        with self.assertRaises(DramaError):
            self.service.create_export(tl['id'], {'requestKey': 'unapproved', 'outputs': ['srt'], 'subtitleRevisionId': sub['id']})
        self.service.approve_subtitles(sub['id'], {'timelineId': tl['id'], 'requestKey': 'approve'})
        export = self.service.create_export(tl['id'], {'requestKey': 'out', 'outputs': ['master', 'proxy', 'srt', 'ass', 'report'], 'subtitleRevisionId': sub['id']})['export']
        result = self.wait_export(export['id'])
        self.assertEqual(result['state'], 'SUCCEEDED', result)
        self.assertIsNone(result['review'])
        self.assertEqual({o['role'] for o in result['outputs']}, {'master', 'proxy', 'srt', 'ass', 'report'})
        for item in result['outputs']:
            path = self.service.media_path(self.pid, item['assetId'])
            self.assertTrue(path.is_file())
            if item['role'] in ('master', 'proxy'):
                self.assertEqual(item['metadata']['frames'], 192)
        self.service.approve_export(result['id'], {'requestKey': 'human', 'note': '完整审片通过'})
        self.assertEqual(self.service.export(result['id'])['export']['review']['status'], 'approved')

    def test_p5_asr_missing_explicit_no_process_or_download(self):
        tl, _ = self.setup_post()
        with patch('subprocess.Popen') as process, self.assertRaises(DramaError) as error:
            self.service.create_transcription(tl['id'], {'requestKey': 'asr'})
        self.assertEqual(error.exception.code, 'ASR_WORKER_MISSING')
        process.assert_not_called()

    def test_p5_asr_queued_and_running_never_overwrite_manual_revision(self):
        tl, _ = self.setup_post()
        self.service.asr_worker = shutil.which('python3')
        model = self.root / 'local-model.bin'
        model.write_bytes(b'isolated fake model')
        self.service.asr_model = str(model)
        for mode in ('queued', 'running'):
            with self.subTest(mode=mode):
                entered, proceed = threading.Event(), threading.Event()
                if mode == 'queued':
                    def block():
                        entered.set()
                        proceed.wait(10)
                    self.service._executor.submit(block)
                def recognize(*args, **kwargs):
                    if mode == 'running':
                        entered.set()
                        if not proceed.wait(10):
                            raise RuntimeError('test barrier timed out')
                    return [{'startFrame': 0, 'endFrame': 24, 'text': '识别候选'}], {'fixture': True}
                with patch('DGXGateway.dgx_gateway.drama.asr.transcribe', side_effect=recognize):
                    job = self.service.create_transcription(tl['id'], {'requestKey': 'asr-' + mode})['transcription']
                    self.assertTrue(entered.wait(10))
                    try:
                        previous = self.service.subtitles(tl['id'])['subtitles']
                        manual = self.service.save_subtitles(tl['id'], {'segments': [{'startFrame': 0, 'endFrame': 24, 'text': '人工修正'}], 'expectedRevision': previous[0]['revision'] if previous else 0, 'requestKey': 'manual-' + mode})['subtitle']
                    finally:
                        proceed.set()
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        result = self.service.transcription(job['id'])['transcription']
                        if result['state'] not in ('QUEUED', 'RUNNING'):
                            break
                        time.sleep(.02)
                    self.assertEqual(result['state'], 'NEEDS_REVIEW', result)
                    self.assertEqual(result['result']['segments'][0]['text'], '识别候选')
                    self.assertEqual(self.service.subtitles(tl['id'])['subtitles'][0]['id'], manual['id'])

    def test_p5_retry_uses_original_runtime_and_recipe(self):
        tl, _ = self.setup_post()
        with patch('DGXGateway.dgx_gateway.drama.post_media.render_post_timeline', side_effect=RuntimeError('fixture')):
            old = self.service.create_export(tl['id'], {'outputs': ['proxy', 'report'], 'requestKey': 'fail'})['export']
            failed = self.wait_export(old['id'])
        self.assertEqual(failed['state'], 'FAILED')
        self.service.post_ffmpeg = '/does/not/exist'
        retried = self.service.retry_export(old['id'], {'requestKey': 'retry'})['export']
        done = self.wait_export(retried['id'])
        self.assertEqual(done['state'], 'SUCCEEDED', done)
        self.assertEqual(done['recipe'], failed['recipe'])
        self.assertEqual(self.service.retry_export(old['id'], {'requestKey': 'retry'})['export']['id'], retried['id'])

    def test_p5_invalid_asr_keeps_raw_without_formal_subtitle(self):
        tl, _ = self.setup_post()
        self.service.asr_worker = shutil.which('python3')
        model = self.root / 'model.bin'; model.write_bytes(b'fake')
        self.service.asr_model = str(model)
        error = DramaError('ASR_OUTPUT_INVALID', '末尾越界')
        error.asr_result = {'raw': {'transcription': [{'offsets': {'from': 0, 'to': 8100}, 'text': '待校对'}]},
                            'segments': [{'startFrame': 0, 'endFrame': 194, 'text': '待校对'}], 'requiresCorrection': True}
        with patch('DGXGateway.dgx_gateway.drama.asr.transcribe', side_effect=error):
            job = self.service.create_transcription(tl['id'], {'requestKey': 'invalid-asr'})['transcription']
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                result = self.service.transcription(job['id'])['transcription']
                if result['state'] not in ('QUEUED', 'RUNNING'):
                    break
                time.sleep(.02)
        self.assertEqual(result['state'], 'NEEDS_REVIEW')
        self.assertEqual(result['result'], error.asr_result)
        self.assertEqual(self.service.subtitles(tl['id'])['subtitles'], [])

    def test_p5_explicit_tools_ready_without_default_path(self):
        tl, _ = self.setup_post()
        self.service.post_ffmpeg = shutil.which('ffmpeg')
        self.service.post_ffprobe = shutil.which('ffprobe')
        which = shutil.which
        with patch('shutil.which', side_effect=lambda value: None if value in ('ffmpeg', 'ffprobe') else which(value)):
            self.assertFalse(self.service.capabilities()['mediaReady'])
            self.assertTrue(self.service.capabilities()['postMediaReady'])
            result = self.service.create_export(tl['id'], {'outputs': ['master'], 'requestKey': 'explicit'})['export']
            self.assertEqual(self.wait_export(result['id'])['state'], 'SUCCEEDED')

    def test_p5_malformed_real_worker_always_reaches_reviewable_state(self):
        tl, _ = self.setup_post()
        model = self.root / 'model.bin'; model.write_bytes(b'fake')
        self.service.asr_model = str(model)
        for index, raw in enumerate(('{"transcription":[{"offsets":{"from":0,"to":1e309},"text":"bad"}]}', '{malformed', '{"transcription":[{"offsets":{"from":0,"to":' + '9' * 400 + '},"text":"bad"}]}')):
            worker = self.root / ('worker' + str(index))
            worker.write_text('#!/usr/bin/env python3\nimport sys\nfrom pathlib import Path\nPath(sys.argv[sys.argv.index("-of")+1]+".json").write_text(' + repr(raw) + ')\n')
            worker.chmod(0o700); self.service.asr_worker = str(worker)
            job = self.service.create_transcription(tl['id'], {'requestKey': 'raw-' + str(index)})['transcription']
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                result = self.service.transcription(job['id'])['transcription']
                if result['state'] not in ('QUEUED', 'RUNNING'):
                    break
                time.sleep(.02)
            self.assertEqual(result['state'], 'NEEDS_REVIEW', result)
            self.assertEqual(result['result']['rawText'], raw)
            self.assertIsNone(result['subtitle_id'])

    def test_mv_song_is_clock_and_does_not_write_timeline_on_bind(self):
        self.assertEqual(self.service.project(self.pid)['project']['kind'], 'drama')
        with self.assertRaises(DramaError) as kind:
            self.service.create_project({'title': 'x', 'kind': 'music', 'requestKey': 'bad-kind'})
        self.assertEqual(kind.exception.code, 'invalid_kind')
        with self.assertRaises(DramaError) as drama:
            self.service.bind_song(self.eid, {'songAssetId': 'a' * 64, 'expectedRevision': 1, 'requestKey': 'drama-song'})
        self.assertEqual(drama.exception.code, 'not_mv')
        mv = self.service.create_project({'title': 'MV', 'width': 96, 'height': 64, 'kind': 'mv', 'requestKey': 'mv'})['project']
        ep = self.service.create_episode(mv['id'], {'title': 'e', 'requestKey': 'mve'})['episode']
        video = self.service.upload(mv['id'], io.BytesIO(self.data), len(self.data), '源片.mp4', 'mv-video')['asset']
        shot = self.service.create_shot(ep['id'], {'title': 's', 'assetId': video['id'], 'requestKey': 'mv-shot'})['shot']
        shot = self.service.select_take(shot['id'], {'takeId': shot['takes'][0]['id'], 'expectedRevision': 1, 'requestKey': 'mv-sel'})['shot']
        path = self.root / 'song.wav'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=220:sample_rate=48000', '-t', '4', str(path)], check=True)
        data = path.read_bytes()
        song = self.service.upload_audio(mv['id'], io.BytesIO(data), len(data), 'song.wav', 'mv-song')['asset']
        song_frames = math.floor(song['duration'] * 24)
        current = self.service.episode(ep['id'])['episode']
        bound = self.service.bind_song(ep['id'], {'songAssetId': song['id'], 'expectedRevision': current['revision'], 'requestKey': 'bind'})
        self.assertEqual(bound['episode']['song_asset_id'], song['id'])
        self.assertIsNone(self.service.episode(ep['id'])['timeline'])
        clip = {'clipId': 'a', 'takeId': shot['selectedTakeId'], 'inFrame': 0, 'frames': 108, 'fit': 'contain'}
        with self.assertRaises(DramaError) as mismatch:
            self.service.save_timeline(ep['id'], {'clips': [clip], 'expectedRevision': 0, 'requestKey': 'long'})
        self.assertEqual(mismatch.exception.code, 'timeline_length_mismatch')
        clip = {**clip, 'frames': song_frames}
        tl = self.service.save_timeline(ep['id'], {'clips': [clip], 'expectedRevision': 0, 'requestKey': 'ok'})['timeline']
        self.assertEqual(sum(c['frames'] for c in tl['clips']), song_frames)
        self.assertFalse(tl['clips'][0]['keepSourceAudio'])
        self.assertEqual(tl['audioClips'][0]['audioClipId'], 'song-bgm')
        self.assertEqual(tl['audioClips'][0]['assetId'], song['id'])
        self.assertEqual(tl['audioClips'][0]['frames'], song_frames)
        self.assertEqual(tl['audioClips'][0]['startFrame'], 0)

    def test_import_audio_path_or_hash_xor_and_rejects_video(self):
        path = self.root / 'import.wav'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=330:sample_rate=48000', '-t', '4', str(path)], check=True)
        imported = self.service.import_audio(self.pid, {'path': str(path.resolve()), 'requestKey': 'imp-path'})
        again = self.service.import_audio(self.pid, {'assetId': imported['asset']['id'], 'requestKey': 'imp-hash'})
        self.assertEqual(imported['asset']['id'], again['asset']['id'])
        with self.assertRaises(DramaError):
            self.service.import_audio(self.pid, {'path': str(path), 'assetId': imported['asset']['id'], 'requestKey': 'both'})
        with self.assertRaises(DramaError):
            self.service.import_audio(self.pid, {'path': '/etc/passwd', 'requestKey': 'escape'})
        with self.assertRaises(DramaError) as audio:
            self.service.import_audio(self.pid, {'assetId': self.asset()['id'], 'requestKey': 'vid-as-audio'})
        self.assertEqual(audio.exception.code, 'audio_required')

    def test_import_video_accepts_two_to_fifteen_seconds_and_rejects_short(self):
        motion = self.root / 'motion.mp4'
        shutil.copy(self.source, motion)
        imported = self.service.import_video(self.pid, {'path': str(motion.resolve()), 'requestKey': 'vid-path'})
        again = self.service.import_video(self.pid, {'assetId': imported['asset']['id'], 'requestKey': 'vid-hash'})
        self.assertEqual(imported['asset']['kind'], 'video')
        self.assertGreaterEqual(imported['asset']['duration'], 2)
        self.assertLessEqual(imported['asset']['duration'], 15)
        self.assertEqual(imported['asset']['id'], again['asset']['id'])
        short = self.root / 'short.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=96x64:rate=24',
                        '-t', '1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(short)], check=True)
        with self.assertRaises(DramaError) as too_short:
            self.service.import_video(self.pid, {'path': str(short.resolve()), 'requestKey': 'short'})
        self.assertEqual(too_short.exception.code, 'invalid_video_reference')

    def test_p5_subtitles_reject_overlap_nan_and_oob(self):
        for segments in ([{'startFrame': 0, 'endFrame': 193, 'text': 'a'}],
                         [{'startFrame': 0, 'endFrame': 48, 'text': 'a'}, {'startFrame': 24, 'endFrame': 60, 'text': 'b'}],
                         [{'startFrame': math.nan, 'endFrame': 24, 'text': 'a'}]):
            with self.assertRaises(DramaError):
                self.service.normalize_segments(segments, 192)


# Only newly defined P5 scenarios are discovered here; the baseline suite runs separately.
for _name in list(vars(DramaServiceTests)):
    if _name.startswith('test_'):
        setattr(PostproductionTests, _name, None)
del DramaServiceTests
