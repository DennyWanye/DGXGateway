"""P1 media oracle: real decoded frames and audio, never header-only fixtures."""

import array
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from DGXGateway.dgx_gateway.drama.media import probe_media, render_timeline


FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"


@unittest.skipUnless(Path(FFMPEG).is_file() and Path(FFPROBE).is_file(), "FFmpeg required")
class DramaMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.silent = cls.root / "silent.mp4"
        cls.sound = cls.root / "sound.mp4"
        for target, audio in ((cls.silent, False), (cls.sound, True)):
            command = [FFMPEG, "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                       "testsrc2=size=96x64:rate=24:duration=4"]
            if audio:
                command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4"]
            command += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-threads", "1"]
            if audio:
                command += ["-c:a", "aac", "-ac", "2"]
            subprocess.run(command + [str(target)], check=True, capture_output=True, timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        self.output_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.output_dir.cleanup)
        self.output = Path(self.output_dir.name) / "render.mp4"

    def probe(self, path):
        return probe_media(path, ffmpeg=FFMPEG, ffprobe=FFPROBE)

    def render(self, clips, **kwargs):
        return render_timeline(clips, self.output, width=96, height=64, fps=24,
                               ffmpeg=FFMPEG, ffprobe=FFPROBE, **kwargs)

    def clip(self, path, in_frame=0, frames=96, sound=True):
        return {"path": path, "inFrame": in_frame, "frames": frames,
                "keepSourceAudio": sound, "fit": "contain"}

    def samples(self, start, duration):
        result = subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-i", str(self.output),
                                 "-ss", str(start), "-t", str(duration), "-vn", "-f", "f32le",
                                 "-ac", "1", "-ar", "48000", "pipe:1"],
                                capture_output=True, check=True, timeout=30)
        samples = array.array("f")
        samples.frombytes(result.stdout)
        self.assertGreater(len(samples), 1000)
        return max(abs(value) for value in samples)

    def test_probe_counts_real_frames_and_audio(self):
        silent, sound = self.probe(self.silent), self.probe(self.sound)
        self.assertEqual(silent["frames"], 96)
        self.assertEqual(silent["fps"], 24)
        self.assertEqual((silent["width"], silent["height"]), (96, 64))
        self.assertFalse(silent["hasAudio"])
        self.assertTrue(sound["hasAudio"])
        self.assertEqual(sound["contentType"], "video/mp4")

    def test_two_shots_are_exactly_192_frames_and_eight_seconds(self):
        result = self.render([self.clip(self.sound), self.clip(self.silent)])
        self.assertEqual(result["frames"], 192)
        self.assertAlmostEqual(result["duration"], 8.0, places=5)
        self.assertEqual(result["codec"], "h264")
        self.assertTrue(result["hasAudio"])
        self.assertGreater(self.samples(1, 1), 0.01)
        self.assertLess(self.samples(5, 1), 0.0001)
        # Independent oracle: count actual RGB frames, not our probe's return.
        decoded = subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-i", str(self.output),
                                  "-map", "0:v:0", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                                 capture_output=True, check=True, timeout=30)
        self.assertEqual(len(decoded.stdout), 192 * 96 * 64 * 3)

    def test_same_asset_two_intervals_and_muting(self):
        result = self.render([self.clip(self.sound, 48, 24, False),
                              self.clip(self.sound, 0, 48, True)])
        self.assertEqual(result["frames"], 72)
        self.assertAlmostEqual(result["duration"], 3.0, places=5)
        self.assertLess(self.samples(0.1, 0.5), 0.0001)
        self.assertGreater(self.samples(1.5, 0.5), 0.01)

    def test_invalid_intervals_never_publish(self):
        for start, count in ((0, 97), (96, 1), (-1, 1), (0, 0), (True, 1), (0, 1.5)):
            with self.subTest(start=start, count=count), self.assertRaises(ValueError):
                self.render([self.clip(self.silent, start, count)])
            self.assertFalse(self.output.exists())
        self.assertEqual(list(self.output.parent.iterdir()), [])

    def test_invalid_and_symlink_inputs_rejected(self):
        broken = self.output.parent / "broken.mp4"
        broken.write_bytes(b"not a media file")
        link = self.output.parent / "link.mp4"
        link.symlink_to(self.silent)
        for path in (broken, link, self.output.parent):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.probe(path)

    def test_truncated_file_is_rejected(self):
        truncated = self.output.parent / "truncated.mp4"
        original = self.sound.read_bytes()
        truncated.write_bytes(original[:len(original) // 2])
        with self.assertRaises(ValueError):
            self.probe(truncated)

    def test_existing_target_is_never_overwritten(self):
        self.output.write_bytes(b"owned state")
        with self.assertRaises(FileExistsError):
            self.render([self.clip(self.silent)])
        self.assertEqual(self.output.read_bytes(), b"owned state")

    def test_bad_audio_flag_and_unsupported_fit_rejected(self):
        for field, value in (("keepSourceAudio", "false"), ("fit", "cover")):
            clip = self.clip(self.silent)
            clip[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.render([clip])

    def test_25fps_source_normalizes_to_project_frames_before_trim(self):
        source = self.output.parent / "25fps.mp4"
        subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=96x64:rate=25:duration=4", "-c:v", "libx264",
                        "-threads", "1", str(source)], check=True, capture_output=True, timeout=30)
        self.assertEqual(self.probe(source)["frames"], 100)
        result = self.render([self.clip(source, 0, 96)])
        self.assertEqual(result["frames"], 96)
        self.assertEqual(result["fps"], 24)
        self.output.unlink()
        with self.assertRaises(ValueError):
            self.render([self.clip(source, 0, 97)])

    def test_failed_render_cleans_temporary_output(self):
        from DGXGateway.dgx_gateway.drama import media
        real_run = media._run

        def fail_render(command, *args, **kwargs):
            if "-filter_complex" in command:
                Path(command[-1]).write_bytes(b"incomplete")
                raise ValueError("injected encoder failure")
            return real_run(command, *args, **kwargs)

        with patch.object(media, "_run", side_effect=fail_render), self.assertRaises(ValueError):
            self.render([self.clip(self.silent)])
        self.assertEqual(list(self.output.parent.iterdir()), [])

    def test_audio_only_is_rejected(self):
        source = self.output.parent / "audio.mp4"
        subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=0.1", "-c:a", "aac", str(source)],
                       check=True, capture_output=True, timeout=30)
        with self.assertRaises(ValueError):
            self.probe(source)

    def test_contain_preserves_image_with_black_padding(self):
        render_timeline([self.clip(self.silent, 0, 24)], self.output, width=64, height=96,
                        ffmpeg=FFMPEG, ffprobe=FFPROBE)
        frame = subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-i", str(self.output),
                                "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                               capture_output=True, check=True, timeout=30).stdout
        self.assertEqual(len(frame), 64 * 96 * 3)
        self.assertLess(max(frame[:64 * 10 * 3]), 5)
        self.assertGreater(max(frame[64 * 30 * 3:64 * 60 * 3]), 100)

    def test_delayed_audio_keeps_source_timing_and_trimmed_silence(self):
        for timestamp_offset in (0, 2):
            self.output.unlink(missing_ok=True)
            source = self.output.parent / f"delayed-{timestamp_offset}.mp4"
            subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                            "testsrc2=size=96x64:rate=24:duration=4", "-itsoffset", "1",
                            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
                            "-c:v", "libx264", "-threads", "1", "-c:a", "aac", "-ac", "2",
                            "-output_ts_offset", str(timestamp_offset), str(source)],
                           check=True, capture_output=True, timeout=30)
            with self.subTest(timestamp_offset=timestamp_offset):
                self.render([self.clip(source)])
                self.assertLess(self.samples(0.2, 0.2), 0.0001)
                self.assertGreater(self.samples(1.2, 0.2), 0.01)
                self.assertLess(self.samples(2.2, 0.2), 0.0001)
                self.output.unlink()
                # A half-second cut must move the delayed tone by half a second,
                # while a clip entirely after that tone must remain silent.
                result = self.render([self.clip(source, 12, 48), self.clip(source, 60, 24)])
                self.assertEqual(result["frames"], 72)
                self.assertLess(self.samples(0.1, 0.2), 0.0001)
                self.assertGreater(self.samples(0.7, 0.2), 0.01)
                self.assertLess(self.samples(2.2, 0.2), 0.0001)
                self.output.unlink()


if __name__ == "__main__":
    unittest.main()
