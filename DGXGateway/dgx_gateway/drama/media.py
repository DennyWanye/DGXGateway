"""Bounded local MP4 validation and frame-based P1 timeline rendering.

Inputs belong to the immutable drama archive. This module never invokes model
services. P1 normalizes constant-rate sources to the project frame rate before
trimming project-frame intervals and supports only contain fitting.
"""

from __future__ import annotations

import json
import math
import os
import stat
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path


MAX_FILE_BYTES = 2 * 1024**3
MAX_DURATION_SECONDS = 3600
MAX_DIMENSION = 4096
MAX_PIXELS = 4096 * 2160
MAX_FPS = 120
MAX_CLIPS = 64
PROCESS_TIMEOUT_SECONDS = 1800
MAX_PROCESS_OUTPUT = 256 * 1024


def _run(command: list[str], timeout: int = PROCESS_TIMEOUT_SECONDS) -> str:
    """Keep subprocess output off the heap and return bounded diagnostic data."""
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=stdout,
                                    stderr=stderr, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("Media processing exceeded its time limit") from exc
        except OSError as exc:
            raise ValueError("Media processing tool is unavailable") from exc
        if stdout.tell() > MAX_PROCESS_OUTPUT or stderr.tell() > MAX_PROCESS_OUTPUT:
            raise ValueError("Media tool produced excessive diagnostic output")
        stdout.seek(0)
        stderr.seek(0)
        output = stdout.read().decode("utf-8", errors="replace")
        errors = stderr.read().decode("utf-8", errors="replace")
        # Commands run at error log level. Some decoders report damage but exit 0.
        if result.returncode or errors.strip():
            raise ValueError("Media processing failed: invalid or unsupported media")
        return output


def _regular_file(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError("Media file is unavailable") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Media input must be a regular file, not a symlink")
    if info.st_size <= 0 or info.st_size > MAX_FILE_BYTES:
        raise ValueError("Media file size is outside the supported range")
    return info


def _same_file(before: os.stat_result, after: os.stat_result) -> bool:
    return (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)


def _positive_integer(value: object, name: str, maximum: int) -> int:
    if type(value) is not int or not 0 < value <= maximum:
        raise ValueError(f"{name} must be a positive integer no larger than {maximum}")
    return value


def _dimensions(width: int, height: int) -> None:
    _positive_integer(width, "width", MAX_DIMENSION)
    _positive_integer(height, "height", MAX_DIMENSION)
    if width * height > MAX_PIXELS or width % 2 or height % 2:
        raise ValueError("Output dimensions must be even and within the pixel limit")


def _rate(value: object) -> float:
    try:
        rate = float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError, OverflowError) as exc:
        raise ValueError("Video frame rate is invalid") from exc
    if not math.isfinite(rate) or not 0 < rate <= MAX_FPS:
        raise ValueError("Video frame rate is outside the supported range")
    return rate


def _probe_json(path: Path, ffprobe: str, *, count_frames: bool) -> dict:
    command = [ffprobe, "-v", "error", "-max_alloc", "268435456",
               "-protocol_whitelist", "file,pipe", "-threads", "2"]
    if count_frames:
        command += ["-count_frames"]
    command += ["-show_entries",
                "format=format_name,duration:stream=index,codec_type,codec_name,width,height,"
                "avg_frame_rate,r_frame_rate,nb_read_frames,duration,start_time,pix_fmt,sample_rate,channels",
                "-of", "json", str(path.absolute())]
    try:
        return json.loads(_run(command))
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("Media probe returned invalid metadata") from exc


def probe_media(path: Path, *, ffprobe: str = "ffprobe", ffmpeg: str = "ffmpeg",
                full_decode: bool = True) -> dict:
    """Validate an MP4 and count actual decoded video frames, including audio QA.

    ``full_decode=False`` still decodes video to count frames; it only skips the
    additional strict FFmpeg pass over both video and audio.
    """
    path = Path(path)
    original = _regular_file(path)
    metadata = _probe_json(path, ffprobe, count_frames=False)
    format_info = metadata.get("format", {})
    if "mp4" not in format_info.get("format_name", "").split(","):
        raise ValueError("P1 media inputs must use an MP4-compatible container")
    videos = [s for s in metadata.get("streams", []) if s.get("codec_type") == "video"]
    if len(videos) != 1:
        raise ValueError("Media must contain exactly one video stream")
    video = videos[0]
    width, height = video.get("width"), video.get("height")
    _positive_integer(width, "video width", MAX_DIMENSION)
    _positive_integer(height, "video height", MAX_DIMENSION)
    if width * height > MAX_PIXELS:
        raise ValueError("Video dimensions exceed the pixel limit")
    fps = _rate(video.get("avg_frame_rate"))
    if abs(_rate(video.get("r_frame_rate")) - fps) > 0.001:
        raise ValueError("Variable frame rate media requires normalization before import")
    try:
        duration = float(video.get("duration", format_info.get("duration", "nan")))
    except (TypeError, ValueError) as exc:
        raise ValueError("Video duration is invalid") from exc
    if not math.isfinite(duration) or not 0 < duration <= MAX_DURATION_SECONDS:
        raise ValueError("Video duration is outside the supported range")

    counted = _probe_json(path, ffprobe, count_frames=True)
    counted_video = next(s for s in counted["streams"] if s.get("codec_type") == "video")
    try:
        frames = int(counted_video["nb_read_frames"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Could not count decoded video frames") from exc
    if not 0 < frames <= MAX_DURATION_SECONDS * MAX_FPS:
        raise ValueError("Decoded frame count is outside the supported range")
    if abs(frames / fps - duration) > max(1 / fps, 0.01):
        raise ValueError("Video timestamps and decoded frame count are inconsistent")
    if full_decode:
        _run([ffmpeg, "-nostdin", "-v", "error", "-xerror", "-max_alloc", "268435456",
              "-protocol_whitelist", "file,pipe", "-err_detect", "explode", "-threads", "2",
              "-i", str(path.absolute()), "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"])
    if not _same_file(original, _regular_file(path)):
        raise ValueError("Media file changed during validation")
    return {"kind": "video", "width": width, "height": height, "fps": fps,
            "frames": frames, "duration": frames / fps,
            "hasAudio": any(s.get("codec_type") == "audio" for s in metadata["streams"]),
            "codec": video.get("codec_name", "unknown"), "contentType": "video/mp4"}


def _video_origin(path: Path, ffprobe: str) -> float:
    """Read the common source origin without exposing timing internals in the API."""
    metadata = _probe_json(path, ffprobe, count_frames=False)
    video = next(stream for stream in metadata["streams"] if stream.get("codec_type") == "video")
    try:
        origin = float(video["start_time"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("Source video requires a valid timestamp origin") from exc
    if not math.isfinite(origin) or abs(origin) > MAX_DURATION_SECONDS:
        raise ValueError("Source timestamp origin is outside the supported range")
    return origin


def render_timeline(clips: list[dict], output: Path, *, width: int, height: int,
                    fps: int = 24, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> dict:
    """Render a frozen sequence, strictly trim frames, and publish without overwrite."""
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise FileExistsError("Export target already exists")
    if not output.parent.is_dir():
        raise ValueError("Export directory must already exist")
    _dimensions(width, height)
    _positive_integer(fps, "fps", MAX_FPS)
    if not isinstance(clips, list) or not 0 < len(clips) <= MAX_CLIPS:
        raise ValueError(f"Timeline must contain between 1 and {MAX_CLIPS} clips")

    inputs: list[str] = []
    filters: list[str] = []
    sources: dict[Path, tuple[dict, os.stat_result, float]] = {}
    total_frames = 0
    for index, clip in enumerate(clips):
        if not isinstance(clip, dict):
            raise ValueError("Timeline clip must be an object")
        try:
            path = Path(clip["path"])
        except (KeyError, TypeError) as exc:
            raise ValueError("Timeline clip requires a media path") from exc
        start = clip.get("inFrame")
        if type(start) is not int or start < 0:
            raise ValueError("inFrame must be a nonnegative integer")
        count = _positive_integer(clip.get("frames"), "frames", MAX_DURATION_SECONDS * fps)
        if type(clip.get("keepSourceAudio")) is not bool:
            raise ValueError("keepSourceAudio must be a boolean")
        if clip.get("fit", "contain") != "contain":
            raise ValueError("P1 supports contain fitting only; crop requires an explicit later workflow")
        if path not in sources:
            info = probe_media(path, ffprobe=ffprobe, ffmpeg=ffmpeg)
            sources[path] = info, _regular_file(path), _video_origin(path, ffprobe)
        info = sources[path][0]
        available_project_frames = math.floor(info["frames"] / info["fps"] * fps + 1e-6)
        if start + count > available_project_frames:
            raise ValueError("Clip interval exceeds decoded source frames")
        total_frames += count
        if total_frames > MAX_DURATION_SECONDS * fps:
            raise ValueError("Timeline exceeds the maximum duration")
        inputs += ["-protocol_whitelist", "file,pipe", "-err_detect", "explode", "-threads", "2",
                   "-i", str(path.absolute())]
        filters.append(
            f"[{index}:v:0]setpts=PTS-STARTPTS,fps={fps},"
            f"trim=start_frame={start}:end_frame={start + count},"
            f"setpts=N/({fps}*TB),scale={width}:{height}:force_original_aspect_ratio=decrease:"
            f"force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,format=yuv420p,fps={fps}[v{index}]")
        seconds = count / fps
        if info["hasAudio"] and clip["keepSourceAudio"]:
            # Both streams retain source timestamps (-copyts). Rebase audio to
            # the VIDEO origin, then fill timestamp gaps before trimming. Using
            # audio STARTPTS would incorrectly pull a delayed voice to frame 0.
            origin = sources[path][2]
            filters.append(
                f"[{index}:a:0]asetpts=PTS-({origin:.12f})/TB,"
                f"aresample=48000:async=1:first_pts=0,"
                f"aformat=sample_fmts=fltp:channel_layouts=stereo,apad,"
                f"atrim=start={start / fps:.12f}:end={(start + count) / fps:.12f},"
                f"asetpts=PTS-STARTPTS[a{index}]")
        else:
            filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={seconds:.12f},"
                           f"asetpts=PTS-STARTPTS[a{index}]")
    labels = "".join(f"[v{i}][a{i}]" for i in range(len(clips)))
    filters.append(f"{labels}concat=n={len(clips)}:v=1:a=1[v][a]")
    with tempfile.TemporaryDirectory(prefix=".drama-render-", dir=output.parent) as work:
        temporary = Path(work) / "result.mp4"
        command = [ffmpeg, "-nostdin", "-v", "error", "-xerror", "-copyts", "-max_alloc", "268435456",
                   "-filter_complex_threads", "2", *inputs, "-filter_complex", ";".join(filters),
                   "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast",
                   "-crf", "18", "-pix_fmt", "yuv420p", "-threads", "2", "-r", str(fps),
                   "-fps_mode", "cfr", "-c:a", "aac", "-ar", "48000", "-ac", "2",
                   "-movflags", "+faststart", "-n", str(temporary)]
        _run(command)
        result = probe_media(temporary, ffmpeg=ffmpeg, ffprobe=ffprobe)
        if result["frames"] != total_frames or abs(result["fps"] - fps) > 0.001:
            raise ValueError("Rendered video did not match the frozen frame count")
        if (result["width"], result["height"]) != (width, height) or not result["hasAudio"]:
            raise ValueError("Rendered video did not match the output media contract")
        for path, (_, original, _) in sources.items():
            if not _same_file(original, _regular_file(path)):
                raise ValueError("Source media changed during rendering")
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        # A hard link publishes atomically and fails if the target appeared while
        # rendering. os.replace would silently overwrite user-owned state.
        os.link(temporary, output)
        return result
