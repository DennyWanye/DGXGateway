"""P5 predeclared media oracles: actual decoded samples, frames and crop pixels."""
import array
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import wave
from unittest.mock import patch

from DGXGateway.dgx_gateway.drama.post_media import (probe_audio, render_post_timeline,
    render_dialogue_audio, crop_geometry, encode_subtitles, burn_subtitles, qualify_subtitle_font)

FFMPEG = os.environ.get('DGX_TEST_FFMPEG') or shutil.which('ffmpeg') or '/opt/homebrew/bin/ffmpeg'
FFPROBE = os.environ.get('DGX_TEST_FFPROBE') or shutil.which('ffprobe') or '/opt/homebrew/bin/ffprobe'


class SubtitleContractTests(unittest.TestCase):
    def test_text_never_becomes_ass_control_and_srt_is_utf8(self):
        text = '中文“你好” {\\c&HFF0000&}\\N\n第二行 <font>'
        encoded = encode_subtitles([{'startFrame': 24, 'endFrame': 48, 'text': text}], total_frames=96)
        self.assertIn('00:00:01,000 --> 00:00:02,000', encoded['srt'])
        self.assertIn('中文“你好”', encoded['ass'])
        self.assertNotIn('{\\c&', encoded['ass'])
        self.assertIn('\\\u2060N', encoded['ass'])
        self.assertIn('\\N第二行', encoded['ass'])
        self.assertIn('&lt;font&gt;', encoded['srt'])

    def test_empty_subtitles_valid_and_invalid_intervals_rejected(self):
        self.assertEqual(encode_subtitles([], total_frames=96)['srt'], '')
        for rows in ([{'startFrame': 0, 'endFrame': 0, 'text': 'a'}],
                     [{'startFrame': 0, 'endFrame': 97, 'text': 'a'}],
                     [{'startFrame': 0, 'endFrame': 30, 'text': 'a'}, {'startFrame': 29, 'endFrame': 40, 'text': 'b'}]):
            with self.assertRaises(ValueError): encode_subtitles(rows, total_frames=96)
        with self.assertRaises(ValueError): encode_subtitles([], total_frames=96, style={'fontFamily': 'bad,override'})

    def test_cover_requires_explicit_bounded_geometry(self):
        self.assertEqual(crop_geometry(192,96,64,96,{'x':0,'y':.5}), (192,96,0,0))
        self.assertEqual(crop_geometry(192,96,64,96,{'x':1,'y':.5}), (192,96,128,0))
        self.assertEqual(crop_geometry(192,96,32,48,{'x':1,'y':.5}), (96,48,64,0))
        for crop in (None, {'x':0}, {'x':float('nan'),'y':0}, {'x':True,'y':0}, {'x':2,'y':0}):
            with self.assertRaises(ValueError): crop_geometry(192,96,64,96,crop)


@unittest.skipUnless(Path(FFMPEG).is_file() and Path(FFPROBE).is_file(), 'FFmpeg required')
class PostMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = tempfile.TemporaryDirectory(); cls.root = Path(cls.workspace.name)
        cls.video = cls.root/'colors.mp4'; cls.audio = cls.root/'voice.wav'; cls.music = cls.root/'music.wav'
        subprocess.run([FFMPEG,'-nostdin','-v','error','-f','lavfi','-i',
            'color=c=red:size=192x96:rate=24:duration=4,drawbox=x=96:y=0:w=96:h=96:color=blue:t=fill',
            '-c:v','libx264','-pix_fmt','yuv420p','-threads','1',str(cls.video)], check=True,capture_output=True,timeout=30)
        # Frequency changes distinguish source time from an incorrect restart at
        # the cut; a plain steady sine would not prove cross-shot continuity.
        for path, music in ((cls.audio,False),(cls.music,True)):
            samples=array.array('h')
            for index in range(48000*8):
                t=index/48000
                frequency=1200 if music else (330 if t<4 else 660)
                samples.append(round(2500*math.sin(2*math.pi*frequency*t)))
            with wave.open(str(path),'wb') as stream:
                stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(48000);stream.writeframes(samples.tobytes())

    @classmethod
    def tearDownClass(cls): cls.workspace.cleanup()
    def setUp(self):
        self.work=tempfile.TemporaryDirectory();self.addCleanup(self.work.cleanup);self.out=Path(self.work.name)
    def clip(self, **patches): return {'path':self.video,'inFrame':0,'frames':96,'fit':'contain','keepSourceAudio':False,**patches}
    def sound(self, **patches): return {'path':self.audio,'track':'voiceover','startFrame':24,'sourceInFrame':12,'frames':144,'gainDb':0,'fadeInFrames':0,'fadeOutFrames':0,**patches}
    def probe(self,path):return probe_audio(path,ffmpeg_bin=FFMPEG,ffprobe_bin=FFPROBE)
    def render(self,clips,audio=None,**kw):return render_post_timeline(clips,self.out/'output.mp4',width=kw.pop('width',192),height=kw.pop('height',96),audio_clips=audio or [],ffmpeg_bin=FFMPEG,ffprobe_bin=FFPROBE,**kw)
    def dialogue(self,clips,audio=None):return render_dialogue_audio(clips,self.out/'dialogue.wav',audio_clips=audio or [],ffmpeg_bin=FFMPEG,ffprobe_bin=FFPROBE)
    def samples(self,path,start,duration):
        raw=subprocess.run([FFMPEG,'-nostdin','-v','error','-i',str(path),'-ss',str(start),'-t',str(duration),'-f','f32le','-ar','48000','-ac','1','pipe:1'],check=True,capture_output=True,timeout=30).stdout
        values=array.array('f');values.frombytes(raw);return values
    def frequency(self,values):return sum(a<=0<b for a,b in zip(values,values[1:]))/(len(values)/48000)

    def test_audio_probe_full_decode_and_source_format_limits(self):
        metadata=self.probe(self.audio)
        self.assertEqual(metadata['decodedSamples48k'],384000);self.assertEqual(metadata['duration'],8)
        self.assertEqual(metadata['role'],'postproduction')
        for ext,codec in [('mp3','libmp3lame'),('m4a','aac'),('flac','flac')]:
            target=self.out/f'audio.{ext}'
            subprocess.run([FFMPEG,'-nostdin','-v','error','-i',str(self.audio),'-c:a',codec,str(target)],check=True,capture_output=True,timeout=30)
            self.assertEqual(self.probe(target)['extension'],ext)
        with self.assertRaises(ValueError):self.probe(self.video)
        broken=self.out/'bad.wav';broken.write_bytes(b'RIFF not audio')
        with self.assertRaises(ValueError):self.probe(broken)
        link=self.out/'link.wav';link.symlink_to(self.audio)
        with self.assertRaises(ValueError):self.probe(link)

    def test_cross_shot_voice_keeps_source_inpoint_and_never_restarts(self):
        result=self.render([self.clip(),self.clip()],[self.sound()])
        self.assertEqual(result['frames'],192);self.assertEqual(result['duration'],8)
        path=self.out/'output.mp4'
        self.assertLess(max(abs(x) for x in self.samples(path,.2,.2)),.0001)
        self.assertAlmostEqual(self.frequency(self.samples(path,3.8,.2)),330,delta=10)
        # At t=5 source time is4.5: this must be660Hz, not restarted330Hz.
        self.assertAlmostEqual(self.frequency(self.samples(path,5,.2)),660,delta=10)
        self.assertLess(max(abs(x) for x in self.samples(path,7.5,.2)),.0001)

    def test_actual_dialogue_excludes_music_but_includes_voice(self):
        info=self.dialogue([self.clip(),self.clip()],[self.sound(),self.sound(path=self.music,track='bgm',startFrame=0,sourceInFrame=0,frames=192)])
        self.assertEqual(info['decodedSamples48k'],384000)
        path=self.out/'dialogue.wav'
        self.assertLess(max(abs(x) for x in self.samples(path,.1,.2)),.0001)
        self.assertAlmostEqual(self.frequency(self.samples(path,5,.2)),660,delta=10)

    def test_invalid_audio_intervals_and_gain_do_not_publish(self):
        for patch in ({'startFrame':80},{'sourceInFrame':150},{'gainDb':float('nan')},{'gainDb':13},{'fadeInFrames':100,'fadeOutFrames':100}):
            with self.subTest(patch=patch),self.assertRaises(ValueError):self.render([self.clip(),self.clip()],[self.sound(**patch)])
            self.assertFalse((self.out/'output.mp4').exists())

    def test_cover_and_proxy_show_same_chosen_region(self):
        for label,x,width,height in [('left',0,64,96),('right',1,64,96),('proxy',1,32,48)]:
            output=self.out/f'{label}.mp4'
            render_post_timeline([self.clip(frames=24,fit='cover',crop={'x':x,'y':.5})],output,width=width,height=height,ffmpeg_bin=FFMPEG,ffprobe_bin=FFPROBE)
            raw=subprocess.run([FFMPEG,'-nostdin','-v','error','-i',str(output),'-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','pipe:1'],check=True,capture_output=True,timeout=30).stdout
            pixel=raw[((height//2)*width+width//2)*3:((height//2)*width+width//2)*3+3]
            self.assertGreater(pixel[0 if x==0 else 2],200)
            self.assertLess(pixel[2 if x==0 else 0],30)

    def test_legacy_delayed_native_audio_timing_is_preserved(self):
        source=self.out/'delayed.mp4'
        subprocess.run([FFMPEG,'-nostdin','-v','error','-i',str(self.video),'-itsoffset','1','-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=1','-c:v','copy','-c:a','aac','-output_ts_offset','2',str(source)],check=True,capture_output=True,timeout=30)
        self.dialogue([self.clip(path=source,keepSourceAudio=True)])
        self.assertLess(max(abs(x) for x in self.samples(self.out/'dialogue.wav',.2,.2)),.0001)
        self.assertGreater(max(abs(x) for x in self.samples(self.out/'dialogue.wav',1.2,.2)),.01)

    def test_existing_output_and_missing_subtitle_renderer_are_explicit(self):
        self.render([self.clip()]);existing=(self.out/'output.mp4').read_bytes()
        with self.assertRaises(FileExistsError):self.render([self.clip()])
        self.assertEqual((self.out/'output.mp4').read_bytes(),existing)
        ass=self.out/'captions.ass';ass.write_text(encode_subtitles([{'startFrame':0,'endFrame':48,'text':'中文字幕'}],total_frames=96)['ass'])
        from DGXGateway.dgx_gateway.drama import post_media
        original_run=post_media._run
        def no_filter(command,*args,**kwargs):return 'Filters: scale\n' if '-filters' in command else original_run(command,*args,**kwargs)
        with patch.object(post_media,'_run',side_effect=no_filter),self.assertRaisesRegex(ValueError,'subtitles/libass'):
            burn_subtitles(self.out/'output.mp4',ass,self.out/'burned.mp4',ffmpeg_bin=FFMPEG,ffprobe_bin=FFPROBE)
        self.assertFalse((self.out/'burned.mp4').exists())


class FontQualificationTests(unittest.TestCase):
    @unittest.skipUnless(Path('/System/Library/Fonts/STHeiti Medium.ttc').is_file() and Path('/opt/homebrew/opt/fontconfig/bin/fc-query').is_file(), 'macOS Chinese font fixture required')
    def test_exact_chinese_family_and_actual_glyph_coverage(self):
        options = dict(font_family='Heiti SC', font_probe_bin='/opt/homebrew/opt/fontconfig/bin/fc-query')
        result = qualify_subtitle_font('/System/Library/Fonts/STHeiti Medium.ttc', required_text='中文字幕你好', **options)
        self.assertEqual(result['verifiedCodepoints'], 6)
        self.assertEqual(len(result['sha256']), 64)
        with self.assertRaisesRegex(ValueError, 'family'):
            qualify_subtitle_font('/System/Library/Fonts/STHeiti Medium.ttc',font_family='Nonexistent Family',font_probe_bin=options['font_probe_bin'])
        with self.assertRaisesRegex(ValueError, 'glyphs'):
            qualify_subtitle_font('/System/Library/Fonts/STHeiti Medium.ttc', required_text=chr(0x10ffff), **options)


@unittest.skipUnless(os.environ.get('DGX_TEST_SUBTITLE_FONT'), 'Explicit qualified subtitle runtime required')
class ChineseSubtitleBurnTests(unittest.TestCase):
    def test_real_chinese_strokes_not_tofu_and_frozen_media_contract(self):
        font=os.environ['DGX_TEST_SUBTITLE_FONT']
        family=os.environ.get('DGX_TEST_SUBTITLE_FAMILY','Heiti SC')
        fontprobe=os.environ.get('DGX_TEST_FONT_PROBE','/opt/homebrew/opt/fontconfig/bin/fc-query')
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);master=root/'black.mp4'
            subprocess.run([FFMPEG,'-nostdin','-v','error','-f','lavfi','-i','color=c=black:size=384x192:rate=24:duration=2', '-f','lavfi','-i','anullsrc=r=48000:cl=stereo','-t','2','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(master)],check=True,capture_output=True,timeout=30)
            frames=[]
            for character in ('中','文'):
                ass=root/(character+'.ass');output=root/(character+'.mp4')
                ass.write_text(encode_subtitles([{'startFrame':12,'endFrame':36,'text':character}],total_frames=48,width=384,height=192,style={'fontFamily':family,'fontSize':64,'marginV':30})['ass'],encoding='utf-8')
                result=burn_subtitles(master,ass,output,ffmpeg_bin=FFMPEG,ffprobe_bin=FFPROBE,font_file=font,font_family=family,font_probe_bin=fontprobe)
                self.assertEqual((result['frames'],result['fps'],result['hasAudio']),(48,24,True))
                def pixels(time):
                    return subprocess.run([FFMPEG,'-nostdin','-v','error','-ss',str(time),'-i',str(output),'-frames:v','1','-f','rawvideo','-pix_fmt','gray','pipe:1'],check=True,capture_output=True,timeout=30).stdout
                before=pixels(.1);active=pixels(.8);after=pixels(1.8)
                self.assertLess(sum(v>180 for v in before),10)
                self.assertLess(sum(v>180 for v in after),10)
                self.assertGreater(sum(v>180 for v in active),300)
                # Distinct known Chinese glyphs must not become identical missing
                # glyph boxes; coverage qualification also rejects missing chars.
                frames.append(active)
            self.assertGreater(sum(abs(a-b)>80 for a,b in zip(*frames)),200)
            with self.assertRaisesRegex(ValueError,'qualified subtitle font'):
                burn_subtitles(master,ass,root/'missing-font.mp4',ffmpeg_bin=FFMPEG,ffprobe_bin=FFPROBE)
            self.assertFalse((root/'missing-font.mp4').exists())
