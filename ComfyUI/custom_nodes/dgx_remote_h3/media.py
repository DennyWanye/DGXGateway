from __future__ import annotations

import pathlib
import math
import os
import shutil
import tempfile
import wave
from dataclasses import dataclass


class MediaError(RuntimeError):
    """A sanitized MP4 validation error."""


class ReferenceImageError(RuntimeError):
    """A sanitized reference-image staging error."""


class ReferenceAudioError(RuntimeError):
    """A sanitized reference-audio staging error."""


def stage_reference_image(
    image: object,
    output_directory: pathlib.Path | str,
    *,
    max_pixels: int = 4_194_304,
    max_source_pixels: int = 67_108_864,
    max_encoded_bytes: int = 31_457_280,
) -> pathlib.Path:
    root = pathlib.Path(output_directory).resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise ReferenceImageError("Reference-image staging directory is invalid.")
    temporary = pathlib.Path(tempfile.mkdtemp(prefix=".dgx-h3-reference-", dir=root))
    os.chmod(temporary, 0o700)
    target = temporary / "reference.png"
    try:
        if all(hasattr(image, name) for name in ("mode", "size", "save", "copy")):
            prepared = image.copy()
            if getattr(prepared, "mode") != "RGB":
                if not hasattr(prepared, "convert"):
                    raise ReferenceImageError("Reference image must be RGB.")
                prepared = prepared.convert("RGB")
        else:
            try:
                shape = tuple(image.shape)  # type: ignore[attr-defined]
                if len(shape) != 4 or shape[0] != 1 or shape[-1] != 3:
                    raise ReferenceImageError("Reference image must contain exactly one RGB frame.")
                source_height, source_width = shape[1], shape[2]
                if (
                    not isinstance(source_width, int)
                    or not isinstance(source_height, int)
                    or source_width < 1
                    or source_height < 1
                    or source_width * source_height > max_source_pixels
                ):
                    raise ReferenceImageError("Reference image has too many source pixels.")
                array = image.detach().cpu().clamp(0, 1).mul(255).byte().numpy()[0]  # type: ignore[attr-defined]
                from PIL import Image
                prepared = Image.fromarray(array, mode="RGB")
            except ReferenceImageError:
                raise
            except Exception as exc:
                raise ReferenceImageError("Reference image could not be converted to RGB.") from exc
        width, height = prepared.size
        if (
            not isinstance(width, int)
            or not isinstance(height, int)
            or width < 1
            or height < 1
            or width * height > max_source_pixels
        ):
            raise ReferenceImageError("Reference image has too many source pixels.")
        if max_pixels < 1:
            raise ReferenceImageError("Reference-image pixel limit is invalid.")
        if width * height > max_pixels:
            from PIL import Image

            scale = math.sqrt(max_pixels / (width * height))
            resized = (max(1, int(width * scale)), max(1, int(height * scale)))
            if not hasattr(prepared, "resize"):
                raise ReferenceImageError("Reference image could not be resized safely.")
            prepared = prepared.resize(resized, resample=Image.Resampling.LANCZOS)
        prepared.save(target, format="PNG", optimize=False, compress_level=6)
        if target.is_symlink() or not target.is_file():
            raise ReferenceImageError("Reference image staging did not create a regular file.")
        os.chmod(target, 0o600)
        size = target.stat().st_size
        if not (1 <= size <= max_encoded_bytes):
            raise ReferenceImageError("Encoded reference image is too large.")
        with target.open("rb") as stream:
            if stream.read(8) != b"\x89PNG\r\n\x1a\n":
                raise ReferenceImageError("Encoded reference image is not a PNG.")
            os.fsync(stream.fileno())
        return target
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def stage_reference_audio(
    audio: object,
    output_directory: pathlib.Path | str,
    *,
    sample_rate: int = 24_000,
    duration_seconds: int = 4,
    max_encoded_bytes: int = 262_144,
) -> pathlib.Path:
    root = pathlib.Path(output_directory).resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise ReferenceAudioError("Reference-audio staging directory is invalid.")
    temporary = pathlib.Path(tempfile.mkdtemp(prefix=".dgx-h3-reference-audio-", dir=root))
    os.chmod(temporary, 0o700)
    target = temporary / "reference.wav"
    try:
        if not isinstance(audio, dict) or set(audio) < {"waveform", "sample_rate"}:
            raise ReferenceAudioError("Reference audio is unavailable.")
        waveform = audio["waveform"]
        source_rate = audio["sample_rate"]
        shape = tuple(waveform.shape)
        if (
            len(shape) != 3
            or shape[0] != 1
            or shape[1] < 1
            or shape[2] < 1
            or isinstance(source_rate, bool)
            or not isinstance(source_rate, int)
            or source_rate < 1
        ):
            raise ReferenceAudioError("Reference audio must contain exactly one waveform batch.")
        import torch
        import torch.nn.functional as functional

        samples = waveform.detach().float().cpu()[0].mean(dim=0, keepdim=True).unsqueeze(0)
        if source_rate != sample_rate:
            resampled = max(1, round(samples.shape[-1] * sample_rate / source_rate))
            samples = functional.interpolate(samples, size=resampled, mode="linear", align_corners=False)
        required = sample_rate * duration_seconds
        samples = functional.pad(samples, (0, max(0, required - samples.shape[-1])))[:, :, :required]
        pcm = samples.squeeze(0).squeeze(0).clamp(-1, 1).mul(32767).round().to(torch.int16).numpy().tobytes()
        with wave.open(str(target), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            output.writeframes(pcm)
        os.chmod(target, 0o600)
        size = target.stat().st_size
        if not (44 <= size <= max_encoded_bytes):
            raise ReferenceAudioError("Encoded reference audio is too large.")
        with target.open("rb") as stream:
            if stream.read(4) != b"RIFF" or stream.read(4) != (size - 8).to_bytes(4, "little") or stream.read(4) != b"WAVE":
                raise ReferenceAudioError("Encoded reference audio is not a WAV.")
            os.fsync(stream.fileno())
        return target
    except ReferenceAudioError:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        raise ReferenceAudioError("Reference audio could not be converted to WAV.") from exc


@dataclass(frozen=True)
class MediaInfo:
    width: int
    height: int
    video_frames: int
    audio_samples: int
    video_duration: float
    audio_duration: float


def validate_h3_mp4(
    path: pathlib.Path | str,
    *,
    width: int,
    height: int,
    requested_duration: int,
) -> MediaInfo:
    try:
        import av
    except ImportError as exc:
        raise MediaError("PyAV is unavailable for MP4 validation.") from exc

    try:
        with av.open(str(path), mode="r") as container:
            format_names = set(container.format.name.split(","))
            if "mp4" not in format_names:
                raise MediaError("H3 media is not an MP4 container.")
            video_streams = list(container.streams.video)
            audio_streams = list(container.streams.audio)
            if len(video_streams) != 1:
                raise MediaError("H3 MP4 must contain exactly one video stream.")
            if len(audio_streams) != 1:
                raise MediaError("H3 MP4 must contain exactly one audio stream.")
            video_stream = video_streams[0]
            audio_stream = audio_streams[0]
            if (video_stream.width, video_stream.height) != (width, height):
                raise MediaError("H3 MP4 dimensions do not match the request.")

            video_frames = 0
            audio_samples = 0
            video_start: float | None = None
            video_end: float | None = None
            video_frame_seconds = (
                1.0 / float(video_stream.average_rate)
                if video_stream.average_rate
                else 1.0 / 24.0
            )
            for frame in container.decode(video_stream, audio_stream):
                if isinstance(frame, av.VideoFrame):
                    video_frames += 1
                    if frame.time is not None:
                        start = float(frame.time)
                        frame_duration = (
                            float(frame.duration * frame.time_base)
                            if frame.duration is not None and frame.time_base is not None
                            else video_frame_seconds
                        )
                        video_start = start if video_start is None else min(video_start, start)
                        end = start + frame_duration
                        video_end = end if video_end is None else max(video_end, end)
                elif isinstance(frame, av.AudioFrame):
                    audio_samples += int(frame.samples)

            if video_frames < 1 or video_start is None or video_end is None:
                raise MediaError("H3 MP4 video decode produced no frames.")
            if audio_samples < 1 or not audio_stream.rate:
                raise MediaError("H3 MP4 audio decode produced no samples.")
            video_duration = video_end - video_start
            audio_duration = audio_samples / float(audio_stream.rate)
            minimum = requested_duration - 1.0 / 24.0
            maximum = requested_duration + 1.0
            if not minimum <= video_duration <= maximum:
                raise MediaError("H3 MP4 video duration does not match the request.")
            if not minimum <= audio_duration <= maximum:
                raise MediaError("H3 MP4 audio duration does not match the request.")
            return MediaInfo(
                width=width,
                height=height,
                video_frames=video_frames,
                audio_samples=audio_samples,
                video_duration=video_duration,
                audio_duration=audio_duration,
            )
    except MediaError:
        raise
    except Exception as exc:
        raise MediaError("H3 MP4 could not be fully decoded.") from exc
