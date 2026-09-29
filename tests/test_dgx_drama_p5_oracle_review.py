"""Independent P5 gap tests: real SQLite/CPU plus explicit failure injection.

Composition deliberately avoids inheriting or recounting existing test methods.
"""
import copy
import io
import tempfile
import threading
import subprocess
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_dgx_drama_postproduction as fixtures
from DGXGateway.dgx_gateway.drama.service import DramaService
from DGXGateway.dgx_gateway.drama.contracts import DramaError
from DGXGateway.dgx_gateway.drama import post_media


class P5OracleReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.PostproductionTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        fixtures.PostproductionTests.tearDownClass()

    def setUp(self):
        self.f = fixtures.PostproductionTests()
        # Match the deployed workspace filesystem, while keeping every row isolated.
        original_temporary = tempfile.TemporaryDirectory
        workspace = Path(__file__).resolve().parents[1] / 'ComfyUI/data'
        with patch.object(tempfile, 'TemporaryDirectory', side_effect=lambda *args, **kwargs: original_temporary(*args, **{'dir': workspace, **kwargs})):
            self.f.setUp()
        self.s = self.f.service

    def tearDown(self):
        self.f.tearDown()

    def approved(self, tl):
        sub = self.s.save_subtitles(tl['id'], {'segments': [{'startFrame': 24, 'endFrame': 72, 'text': '冻结原字幕'}], 'expectedRevision': 0, 'requestKey': 'sub'})['subtitle']
        self.s.approve_subtitles(sub['id'], {'timelineId': tl['id'], 'requestKey': 'approve'})
        return sub

    def test_o02_all_mapping_changes_retain_but_invalidate_old_subtitle(self):
        tl, body = self.f.setup_post()
        sub = self.approved(tl)
        export = self.s.create_export(tl['id'], {'requestKey': 'original', 'outputs': ['srt'], 'subtitleRevisionId': sub['id']})['export']
        original = self.f.wait_export(export['id'])
        self.assertEqual(original['state'], 'SUCCEEDED')
        # A second legitimate audio asset has different bytes and project binding.
        wav = (self.f.root / 'voice.wav').read_bytes()
        alternate = bytearray(wav)
        alternate[-100:] = bytes(100)
        other = self.s.upload_audio(self.f.pid, io.BytesIO(alternate), len(alternate), 'alternate.wav', 'other')['asset']['id']
        variants = []
        for name, clip_patch in [('shorten', {'frames': 95}), ('source-in', {'inFrame': 1}), ('native-audio', {'keepSourceAudio': True})]:
            changed = copy.deepcopy(body)
            changed['clips'][0].update(clip_patch)
            variants.append((name, changed))
        reordered = copy.deepcopy(body)
        reordered['clips'].reverse()
        variants.append(('reorder', reordered))
        for field, value in [('startFrame', 0), ('sourceInFrame', 0), ('assetId', other), ('gainDb', -9), ('fadeInFrames', 12)]:
            changed = copy.deepcopy(body)
            changed['audioClips'][0][field] = value
            variants.append((field, changed))
        for rev, (name, changed) in enumerate(variants, 1):
            with self.subTest(change=name):
                current = self.s.save_timeline(self.f.eid, {**changed, 'expectedRevision': rev, 'requestKey': 'change-' + name})['timeline']
                history = self.s.subtitles(current['id'])['subtitles']
                self.assertEqual(len(history), 1)
                self.assertEqual(history[0]['id'], sub['id'])
                self.assertTrue(history[0]['needsReview'])
                with self.assertRaises(DramaError):
                    self.s.create_export(current['id'], {'requestKey': 'stale-' + name, 'outputs': ['srt'], 'subtitleRevisionId': sub['id']})
                self.assertEqual(self.s.export(export['id'])['export'], original)
        with self.s.store.connect() as db:
            stored = self.s.store.one(db, 'dr_timeline_revisions', tl['id'])
            import json
            self.assertEqual(json.loads(stored['snapshot']), tl)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM dr_subtitle_approvals').fetchone()[0], 1)

    def test_o14_inflight_export_freezes_audio_crop_and_subtitle(self):
        tl, body = self.f.setup_post()
        sub = self.approved(tl)
        entered, proceed = threading.Event(), threading.Event()
        render = post_media.render_post_timeline
        captured = []
        def delayed(clips, destination, **kwargs):
            captured.append((copy.deepcopy(clips), copy.deepcopy(kwargs['audio_clips'])))
            if not entered.is_set():
                entered.set()
                if not proceed.wait(15):
                    raise RuntimeError('test barrier timeout')
            return render(clips, destination, **kwargs)
        with patch.object(post_media, 'render_post_timeline', side_effect=delayed):
            job = self.s.create_export(tl['id'], {'requestKey': 'running', 'outputs': ['master', 'proxy', 'srt', 'ass', 'report'], 'subtitleRevisionId': sub['id']})['export']
            self.assertTrue(entered.wait(10))
            try:
                changed = copy.deepcopy(body)
                changed['clips'][0].update(frames=95, fit='cover', crop={'x': 1, 'y': 0})
                changed['audioClips'][0].update(startFrame=0, gainDb=-12)
                newer = self.s.save_timeline(self.f.eid, {**changed, 'expectedRevision': 1, 'requestKey': 'concurrent'})['timeline']
                fresh = self.s.save_subtitles(newer['id'], {'segments': [{'startFrame': 0, 'endFrame': 48, 'text': '并发新字幕'}], 'expectedRevision': 1, 'requestKey': 'new-sub'})['subtitle']
                self.assertFalse(fresh['approved'])
            finally:
                proceed.set()
            result = self.f.wait_export(job['id'])
        self.assertEqual(result['state'], 'SUCCEEDED', result)
        self.assertEqual(result['recipe']['timelineRevisionId'], tl['id'])
        self.assertEqual(result['recipe']['subtitleRevisionId'], sub['id'])
        self.assertEqual(len(captured), 2)
        for clips, audio in captured:
            self.assertEqual(sum(c['frames'] for c in clips), 192)
            self.assertEqual(clips[0]['fit'], 'contain')
            self.assertEqual(audio[0]['startFrame'], 24)
            self.assertEqual(audio[0]['gainDb'], 0)
        sidecar = next(o for o in result['outputs'] if o['role'] == 'srt')
        text = self.s.media_path(self.f.pid, sidecar['assetId']).read_text()
        self.assertIn('冻结原字幕', text)
        self.assertNotIn('并发新字幕', text)
        self.assertEqual(self.s.episode(self.f.eid)['timeline']['id'], newer['id'])

    def test_o16_proxy_failure_and_partial_publish_disk_failure_retry_original(self):
        tl, _ = self.f.setup_post()
        render = post_media.render_post_timeline
        publish = self.s._publish
        for mode in ('proxy-encoder', 'proxy-disk'):
            with self.subTest(mode=mode):
                def injected_render(clips, destination, **kwargs):
                    if mode == 'proxy-encoder' and destination.name == 'proxy.mp4':
                        raise RuntimeError('isolated encoder fault')
                    return render(clips, destination, **kwargs)
                def injected_publish(path, suffix):
                    if mode == 'proxy-disk' and path.name == 'proxy.mp4':
                        raise OSError(28, 'No space left on device')
                    return publish(path, suffix)
                with patch.object(post_media, 'render_post_timeline', side_effect=injected_render), patch.object(self.s, '_publish', side_effect=injected_publish):
                    job = self.s.create_export(tl['id'], {'requestKey': mode, 'outputs': ['master', 'proxy', 'report']})['export']
                    failed = self.f.wait_export(job['id'])
                self.assertEqual(failed['state'], 'FAILED', failed)
                self.assertIn('proxy导出失败', failed['error'])
                self.assertIsNone(failed['review'])
                self.assertEqual([o['role'] for o in failed['outputs']], ['master'] if mode == 'proxy-disk' else [])
                retry = self.s.retry_export(job['id'], {'requestKey': 'retry-' + mode})['export']
                done = self.f.wait_export(retry['id'])
                self.assertEqual(done['state'], 'SUCCEEDED', done)
                self.assertEqual(done['recipe'], failed['recipe'])
                self.assertEqual({o['role'] for o in done['outputs']}, {'master', 'proxy', 'report'})
                self.assertEqual(self.s.export(job['id'])['export'], failed)
                self.assertEqual(self.s.retry_export(job['id'], {'requestKey': 'retry-' + mode})['export']['id'], retry['id'])

    def test_o16_restart_preserves_queued_and_running_recipe_without_autorun(self):
        tl, _ = self.f.setup_post()
        jobs = []
        # Simulate durable commit followed by process loss before dispatch/finish.
        with patch.object(self.s._executor, 'submit'):
            for state in ('QUEUED', 'RUNNING'):
                job = self.s.create_export(tl['id'], {'requestKey': state, 'outputs': ['proxy', 'report']})['export']
                with self.s.store.connect(write=True) as db:
                    db.execute('UPDATE dr_exports SET state=? WHERE id=?', (state, job['id']))
                jobs.append(job)
        self.s.close()
        with patch.object(post_media, 'render_post_timeline') as render:
            self.s = self.f.service = DramaService(self.f.root, self.f.root)
            for job in jobs:
                stopped = self.s.export(job['id'])['export']
                self.assertEqual(stopped['state'], 'INTERRUPTED')
                self.assertEqual(stopped['recipe'], job['recipe'])
            render.assert_not_called()
        for job in jobs:
            retry = self.s.retry_export(job['id'], {'requestKey': 'restart-retry'})['export']
            done = self.f.wait_export(retry['id'])
            self.assertEqual(done['state'], 'SUCCEEDED', done)
            self.assertEqual(done['recipe'], job['recipe'])

    def test_o19_committed_audio_lost_response_replays_same_binary_key_once(self):
        asset = self.f.audio()
        data = (self.f.root / 'voice.wav').read_bytes()
        before = self.s.project(self.f.pid)['assets']
        # The first response is deliberately discarded after the real SQLite commit.
        replay = self.s.upload_audio(self.f.pid, io.BytesIO(data), len(data), 'voice.wav', 'audio')['asset']
        self.assertEqual(replay, asset)
        self.assertEqual(self.s.project(self.f.pid)['assets'], before)
        altered = bytearray(data)
        altered[-100:] = bytes(100)
        with self.assertRaises(DramaError) as error:
            self.s.upload_audio(self.f.pid, io.BytesIO(altered), len(altered), 'voice.wav', 'audio')
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.s.project(self.f.pid)['assets'], before)


    def qualified_runtime(self):
        root = Path(__file__).resolve().parents[1] / 'ComfyUI/data/post-runtime/bin'
        self.s.post_ffmpeg = str(root / 'ffmpeg')
        self.s.post_ffprobe = str(root / 'ffprobe')
        self.s.post_font_file = '/System/Library/Fonts/STHeiti Medium.ttc'
        self.s.post_font_probe = '/opt/homebrew/opt/fontconfig/bin/fc-query'
        for path in (self.s.post_ffmpeg, self.s.post_ffprobe, self.s.post_font_file, self.s.post_font_probe):
            self.assertTrue(Path(path).is_file(), 'This real CPU qualification requires the recorded local runtime')

    def test_o16_burn_failure_cannot_be_masked_by_master_success(self):
        self.qualified_runtime()
        tl, _ = self.f.setup_post()
        sub = self.approved(tl)
        with patch.object(post_media, 'burn_subtitles', side_effect=RuntimeError('isolated libass failure')):
            job = self.s.create_export(tl['id'], {'requestKey': 'burn-fail', 'outputs': ['master', 'burned', 'srt', 'report'], 'subtitleRevisionId': sub['id']})['export']
            failed = self.f.wait_export(job['id'])
        self.assertEqual(failed['state'], 'FAILED', failed)
        self.assertIn('burned导出失败', failed['error'])
        self.assertEqual(failed['outputs'], [])
        retried = self.s.retry_export(job['id'], {'requestKey': 'burn-retry'})['export']
        done = self.f.wait_export(retried['id'])
        self.assertEqual(done['state'], 'SUCCEEDED', done)
        self.assertEqual(done['recipe'], failed['recipe'])
        self.assertEqual({o['role'] for o in done['outputs']}, {'master', 'burned', 'srt', 'report'})

    def test_o13_portrait_chinese_two_line_glyphs_stay_within_safe_area(self):
        self.qualified_runtime()
        master, ass, output = (self.f.root / name for name in ('portrait.mp4', 'portrait.ass', 'burned.mp4'))
        width, height = 384, 682
        subprocess.run([self.s.post_ffmpeg, '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', f'color=c=black:size={width}x{height}:rate=24:duration=1', '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo', '-t', '1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(master)], check=True, capture_output=True, timeout=30)
        ass.write_text(post_media.encode_subtitles([{'startFrame': 0, 'endFrame': 24, 'text': '首行中文对白\n最后一行字幕'}], total_frames=24, width=width, height=height, style={'fontFamily': 'Heiti SC'})['ass'], encoding='utf-8')
        post_media.burn_subtitles(master, ass, output, ffmpeg_bin=self.s.post_ffmpeg, ffprobe_bin=self.s.post_ffprobe, font_file=self.s.post_font_file, font_family='Heiti SC', font_probe_bin=self.s.post_font_probe)
        raw = subprocess.run([self.s.post_ffmpeg, '-nostdin', '-v', 'error', '-ss', '0.5', '-i', str(output), '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'gray', 'pipe:1'], check=True, capture_output=True, timeout=30).stdout
        lit = [(i % width, i // width) for i, value in enumerate(raw) if value > 180]
        self.assertGreater(len(lit), 500)
        self.assertGreaterEqual(min(x for x, y in lit), 24)
        self.assertLess(max(x for x, y in lit), width - 24)
        self.assertGreater(min(y for x, y in lit), height // 2)
        self.assertLess(max(y for x, y in lit), height - height // 20)

    def test_o04_voiced_then_silent_shot_keep_one_cross_cut_voice_source(self):
        from tests import test_dgx_drama_post_media as media_fixtures
        media_fixtures.PostMediaTests.setUpClass()
        media = media_fixtures.PostMediaTests()
        media.setUp()
        try:
            voiced = self.f.root / 'voiced-shot.mp4'
            subprocess.run([media_fixtures.FFMPEG, '-nostdin', '-v', 'error', '-i', str(media.video), '-f', 'lavfi', '-i', 'sine=frequency=1200:sample_rate=48000:duration=4,volume=0.001', '-map', '0:v', '-map', '1:a', '-c:v', 'copy', '-c:a', 'aac', '-t', '4', str(voiced)], check=True, capture_output=True, timeout=30)
            result = media.render([media.clip(path=voiced, keepSourceAudio=True), media.clip()], [media.sound()])
            self.assertEqual((result['frames'], result['duration']), (192, 8))
            output = media.out / 'output.mp4'
            self.assertAlmostEqual(media.frequency(media.samples(output, 3.8, .2)), 330, delta=10)
            self.assertAlmostEqual(media.frequency(media.samples(output, 5, .2)), 660, delta=10)
            self.assertLess(max(abs(v) for v in media.samples(output, 7.5, .2)), .0001)
        finally:
            media.doCleanups()
            media_fixtures.PostMediaTests.tearDownClass()
