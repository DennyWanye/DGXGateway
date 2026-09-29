import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import stat
import wave

import torch


CUSTOM_NODES = pathlib.Path(__file__).resolve().parents[2] / "custom_nodes"
sys.path.insert(0, str(CUSTOM_NODES))

from dgx_remote_h3.media import (
    MediaError,
    ReferenceAudioError,
    ReferenceImageError,
    stage_reference_audio,
    stage_reference_image,
    validate_h3_mp4,
)


class FakeReferenceImage:
    mode = "RGB"

    def __init__(self, size=(1344, 768), payload=b"\x89PNG\r\n\x1a\nencoded"):
        self.size = size
        self.payload = payload

    def copy(self):
        return self

    def resize(self, size, *, resample):
        self.resize_call = (size, resample)
        self.size = size
        return self

    def save(self, path, **options):
        self.options = options
        pathlib.Path(path).write_bytes(self.payload)


class ReferenceImageTests(unittest.TestCase):
    def test_stages_one_rgb_png_in_private_random_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            image = FakeReferenceImage()
            path = stage_reference_image(image, directory)
            self.assertEqual(path.name, "reference.png")
            self.assertEqual(path.read_bytes(), image.payload)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(image.options["format"], "PNG")

    def test_downscales_large_photos_before_png_encoding(self):
        with tempfile.TemporaryDirectory() as directory:
            image = FakeReferenceImage(size=(6000, 4000))
            stage_reference_image(image, directory)
            width, height = image.size
            self.assertLessEqual(width * height, 4_194_304)
            self.assertAlmostEqual(width / height, 1.5, places=2)
            self.assertTrue(hasattr(image, "resize_call"))

    def test_rejects_extreme_source_pixels_or_encoded_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ReferenceImageError):
                stage_reference_image(FakeReferenceImage(size=(9000, 9000)), directory)
            with self.assertRaises(ReferenceImageError):
                stage_reference_image(FakeReferenceImage(payload=b"x" * 33), directory, max_encoded_bytes=32)


class ReferenceAudioTests(unittest.TestCase):
    def test_stages_exactly_four_seconds_of_private_mono_24khz_wav(self):
        audio = {"waveform": torch.ones(1, 2, 48_000), "sample_rate": 48_000}
        with tempfile.TemporaryDirectory() as directory:
            path = stage_reference_audio(audio, directory)
            self.assertEqual(path.name, "reference.wav")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            with wave.open(str(path), "rb") as stream:
                self.assertEqual(stream.getnchannels(), 1)
                self.assertEqual(stream.getsampwidth(), 2)
                self.assertEqual(stream.getframerate(), 24_000)
                self.assertEqual(stream.getnframes(), 96_000)

    def test_rejects_missing_or_malformed_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            for value in (None, {}, {"waveform": torch.ones(2, 1, 10), "sample_rate": 24_000}):
                with self.subTest(value=type(value).__name__), self.assertRaises(ReferenceAudioError):
                    stage_reference_audio(value, directory)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required for the media oracle test")
class MediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = pathlib.Path(cls.temporary.name)
        cls.valid = cls._make_video("valid.mp4", width=768, height=448, seconds=2, audio=True)
        cls.no_audio = cls._make_video("no-audio.mp4", width=768, height=448, seconds=2, audio=False)
        cls.wrong_size = cls._make_video("wrong-size.mp4", width=640, height=360, seconds=2, audio=True)
        cls.too_short = cls._make_video("too-short.mp4", width=768, height=448, seconds=0.5, audio=True)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    @classmethod
    def _make_video(cls, name, *, width, height, seconds, audio):
        target = cls.root / name
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=blue:s={width}x{height}:r=24:d={seconds}",
        ]
        if audio:
            command += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}"]
        command += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
        if audio:
            command += ["-c:a", "aac", "-shortest"]
        command += ["-y", str(target)]
        subprocess.run(command, check=True)
        return target

    def test_fully_decodes_one_video_and_audio_with_expected_geometry_and_duration(self):
        result = validate_h3_mp4(self.valid, width=768, height=448, requested_duration=2)
        self.assertEqual((result.width, result.height), (768, 448))
        self.assertGreaterEqual(result.video_frames, 47)
        self.assertGreater(result.audio_samples, 0)
        self.assertGreaterEqual(result.video_duration, 2 - 1 / 24)
        self.assertLessEqual(result.video_duration, 3)
        self.assertGreaterEqual(result.audio_duration, 2 - 1 / 24)
        self.assertLessEqual(result.audio_duration, 3)

    def test_rejects_missing_audio_wrong_size_wrong_duration_and_truncation(self):
        cases = [
            (self.no_audio, "audio"),
            (self.wrong_size, "dimensions"),
            (self.too_short, "duration"),
        ]
        truncated = self.root / "truncated.mp4"
        truncated.write_bytes(self.valid.read_bytes()[:512])
        cases.append((truncated, "decode"))
        for path, expected in cases:
            with self.subTest(path=path.name), self.assertRaisesRegex(MediaError, expected):
                validate_h3_mp4(path, width=768, height=448, requested_duration=2)


if __name__ == "__main__":
    unittest.main()
