"""P5 CPU media primitives. Frozen inputs only; no model calls or downloads.

The caller binds asset IDs to checked private paths. All encoders run without a
shell, with bounded diagnostics, and publish new files without overwriting.
"""
from __future__ import annotations

import html
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .media import (_dimensions, _probe_json, _regular_file, _run, _same_file,
                    _video_origin, probe_media)

MAX_AUDIO_BYTES = 256 * 1024 * 1024
SAMPLE_RATE = 48000
MAX_SECONDS = 3600
MAX_AUDIO_CLIPS = 32


def _int(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}")
    return value


def _number(value, name, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be finite and between {low} and {high}")
    return float(value)


def _new_output(output):
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise FileExistsError("Output already exists; a new task filename is required")
    if not output.parent.is_dir() or output.parent.is_symlink():
        raise ValueError("Output directory must be a regular existing directory")
    return output


def _publish(temporary, output):
    with Path(temporary).open('rb') as stream:
        os.fsync(stream.fileno())
    os.link(temporary, output)  # Atomic no-overwrite publication, including races.


def probe_audio(path, *, ffmpeg_bin='ffmpeg', ffprobe_bin='ffprobe'):
    """Probe AND fully decode one audio-only WAV/MP3/M4A/FLAC (<=256MiB/1h).

    The decoded 48k sample count is authoritative for source interval checks;
    compressed container duration alone is not a trustworthy editing boundary.
    """
    path = Path(path)
    original = _regular_file(path)
    if original.st_size > MAX_AUDIO_BYTES:
        raise ValueError('Postproduction audio exceeds 256 MiB')
    info = _probe_json(path, ffprobe_bin, count_frames=False)
    streams = info.get('streams', [])
    if len(streams) != 1 or streams[0].get('codec_type') != 'audio':
        raise ValueError('Postproduction upload must contain one audio stream and no video')
    stream = streams[0]
    formats = set(info.get('format', {}).get('format_name', '').split(','))
    extension = next((ext for ext in ('wav', 'mp3', 'flac') if ext in formats), None)
    if extension is None and 'mp4' in formats and stream.get('codec_name') in ('aac', 'alac'):
        extension = 'm4a'
    if extension is None:
        raise ValueError('Postproduction audio must be WAV, MP3, M4A, or FLAC')
    try:
        duration = float(stream.get('duration', info.get('format', {}).get('duration')))
        rate = int(stream['sample_rate'])
        channels = int(stream['channels'])
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError('Audio metadata is incomplete') from exc
    if not math.isfinite(duration) or not 0 < duration <= MAX_SECONDS or not 8000 <= rate <= 192000 or not 1 <= channels <= 8:
        raise ValueError('Audio duration, sample rate, or channel count is outside limits')
    with tempfile.TemporaryDirectory(prefix='drama-audio-probe-') as work:
        decoded = Path(work) / 'samples.s16'
        _run([ffmpeg_bin, '-nostdin', '-v', 'error', '-xerror', '-max_alloc', '268435456',
              '-protocol_whitelist', 'file,pipe', '-err_detect', 'explode', '-threads', '2',
              '-i', str(path.absolute()), '-map', '0:a:0', '-ac', '1', '-ar', str(SAMPLE_RATE),
              '-t', str(MAX_SECONDS + 1), '-f', 's16le', '-n', str(decoded)])
        size = decoded.stat().st_size
        if size % 2 or not 0 < size // 2 <= MAX_SECONDS * SAMPLE_RATE:
            raise ValueError('Decoded audio duration exceeds one hour or contains no samples')
        samples = size // 2
    if not _same_file(original, _regular_file(path)):
        raise ValueError('Audio changed during validation')
    return {'kind': 'audio', 'extension': extension,
            'contentType': {'wav': 'audio/wav', 'mp3': 'audio/mpeg', 'm4a': 'audio/mp4', 'flac': 'audio/flac'}[extension],
            'duration': samples / SAMPLE_RATE, 'sampleRate': rate, 'channels': channels,
            'decodedSamples48k': samples, 'role': 'postproduction'}


def crop_geometry(source_width, source_height, width, height, crop):
    """Explicit normalized overflow offsets; same geometry contract for proxies."""
    _dimensions(width, height)
    if not isinstance(crop, dict) or set(crop) != {'x', 'y'}:
        raise ValueError('Cover fitting requires explicit crop x and y')
    x, y = (_number(crop[k], f'crop {k}', 0, 1) for k in ('x', 'y'))
    factor = max(width / source_width, height / source_height)
    scaled_w = math.ceil(source_width * factor / 2) * 2
    scaled_h = math.ceil(source_height * factor / 2) * 2
    return scaled_w, scaled_h, math.floor((scaled_w - width) * x / 2) * 2, math.floor((scaled_h - height) * y / 2) * 2


def _compile(clips, audio_clips, *, width, height, fps, dialogue, ffmpeg_bin, ffprobe_bin):
    _int(fps, 'Project FPS', 24, 24)
    if not isinstance(clips, list) or not 1 <= len(clips) <= 64:
        raise ValueError('Timeline requires 1–64 video clips')
    if not isinstance(audio_clips, list) or len(audio_clips) > MAX_AUDIO_CLIPS:
        raise ValueError('Timeline permits at most 32 independent audio clips')
    inputs, filters, sources = [], [], {}
    total = 0
    for index, clip in enumerate(clips):
        if not isinstance(clip, dict):
            raise ValueError('Video clip must be an object')
        path = Path(clip.get('path', ''))
        start = _int(clip.get('inFrame'), 'Source in-frame', 0, MAX_SECONDS * fps)
        count = _int(clip.get('frames'), 'Clip frames', 1, MAX_SECONDS * fps)
        keep = clip.get('keepSourceAudio')
        if type(keep) is not bool:
            raise ValueError('keepSourceAudio must be a boolean')
        fit = clip.get('fit', 'contain')
        if fit not in ('contain', 'cover'):
            raise ValueError('Unknown video fitting mode')
        if path not in sources:
            info = probe_media(path, ffmpeg=ffmpeg_bin, ffprobe=ffprobe_bin)
            sources[path] = (info, _regular_file(path), _video_origin(path, ffprobe_bin))
        info, _, origin = sources[path]
        available = math.floor(info['frames'] / info['fps'] * fps + 1e-6)
        if start + count > available:
            raise ValueError('Clip interval exceeds decoded source frames')
        total += count
        if total > MAX_SECONDS * fps:
            raise ValueError('Timeline exceeds one hour')
        inputs += ['-protocol_whitelist', 'file,pipe', '-err_detect', 'explode', '-threads', '2', '-i', str(path.absolute())]
        # Validate geometry even when rendering audio, so no malformed snapshot
        # becomes an apparently valid subtitle source.
        if fit == 'cover':
            sw, sh, x, y = crop_geometry(info['width'], info['height'], width, height, clip.get('crop'))
            geometry = f'scale={sw}:{sh},crop={width}:{height}:{x}:{y}'
        else:
            geometry = f'scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black'
        if not dialogue:
            filters.append(f'[{index}:v:0]setpts=PTS-STARTPTS,fps={fps},trim=start_frame={start}:end_frame={start+count},setpts=N/({fps}*TB),{geometry},setsar=1,format=yuv420p,fps={fps}[v{index}]')
        if keep and info['hasAudio']:
            # Keep audio relative to video origin (including late audio tracks).
            filters.append(f'[{index}:a:0]asetpts=PTS-({origin:.12f})/TB,aresample=48000:async=1:first_pts=0,aformat=sample_fmts=fltp:channel_layouts=stereo,apad,atrim=start_sample={start*2000}:end_sample={(start+count)*2000},asetpts=PTS-STARTPTS[a{index}]')
        else:
            filters.append(f'anullsrc=r=48000:cl=stereo,atrim=end_sample={count*2000},asetpts=PTS-STARTPTS[a{index}]')
    labels = ''.join(f'[a{i}]' if dialogue else f'[v{i}][a{i}]' for i in range(len(clips)))
    filters.append(f'{labels}concat=n={len(clips)}:v={0 if dialogue else 1}:a=1' + ('[native]' if dialogue else '[v][native]'))
    mix_labels = ['[native]']
    for ordinal, audio in enumerate(audio_clips):
        if not isinstance(audio, dict):
            raise ValueError('Audio clip must be an object')
        track = audio.get('track')
        if track not in ('voiceover', 'ambience', 'bgm'):
            raise ValueError('Unknown independent audio track')
        start = _int(audio.get('startFrame'), 'Audio timeline start', 0, total)
        source = _int(audio.get('sourceInFrame'), 'Audio source start', 0, MAX_SECONDS * fps)
        frames = _int(audio.get('frames'), 'Audio frames', 1, MAX_SECONDS * fps)
        gain = _number(audio.get('gainDb', 0), 'Audio gain dB', -60, 12)
        fade_in = _int(audio.get('fadeInFrames', 0), 'Fade in frames', 0, frames)
        fade_out = _int(audio.get('fadeOutFrames', 0), 'Fade out frames', 0, frames)
        if fade_in + fade_out > frames or start + frames > total:
            raise ValueError('Audio or fades exceed the frozen timeline interval')
        path = Path(audio.get('path', ''))
        if path not in sources:
            info = probe_audio(path, ffmpeg_bin=ffmpeg_bin, ffprobe_bin=ffprobe_bin)
            sources[path] = (info, _regular_file(path), 0)
        info = sources[path][0]
        if info.get('kind') != 'audio' or (source + frames) * 2000 > info['decodedSamples48k']:
            raise ValueError('Audio source interval exceeds decoded samples')
        if dialogue and track != 'voiceover':
            continue
        index = len(clips) + len(mix_labels) - 1
        inputs += ['-protocol_whitelist', 'file,pipe', '-err_detect', 'explode', '-threads', '2', '-i', str(path.absolute())]
        chain = f'[{index}:a:0]asetpts=PTS-STARTPTS,aresample=48000:async=1:first_pts=0,aformat=sample_fmts=fltp:channel_layouts=stereo,atrim=start_sample={source*2000}:end_sample={(source+frames)*2000},asetpts=PTS-STARTPTS,volume={gain:.9f}dB'
        if fade_in:
            chain += f',afade=t=in:ss=0:ns={fade_in*2000}'
        if fade_out:
            chain += f',afade=t=out:ss={(frames-fade_out)*2000}:ns={fade_out*2000}'
        chain += f',adelay={start*2000}S:all=1[audio{ordinal}]'
        filters.append(chain)
        mix_labels.append(f'[audio{ordinal}]')
    filters.append(''.join(mix_labels) + f'amix=inputs={len(mix_labels)}:duration=longest:dropout_transition=0:normalize=0,alimiter=limit=0.891250938:level=0:latency=1,apad,atrim=end_sample={total*2000},asetpts=PTS-STARTPTS[a]')
    return inputs, filters, sources, total


def _source_check(sources):
    for path, (_, original, _) in sources.items():
        if not _same_file(original, _regular_file(path)):
            raise ValueError('Frozen source changed during rendering')


def render_post_timeline(clips, output, *, width, height, fps=24, audio_clips=None,
                         ffmpeg_bin='ffmpeg', ffprobe_bin='ffprobe'):
    output = _new_output(output)
    _dimensions(width, height)
    inputs, filters, sources, total = _compile(clips, audio_clips or [], width=width, height=height, fps=fps, dialogue=False, ffmpeg_bin=ffmpeg_bin, ffprobe_bin=ffprobe_bin)
    with tempfile.TemporaryDirectory(prefix='.drama-post-', dir=output.parent) as work:
        temporary = Path(work) / 'master.mp4'
        _run([ffmpeg_bin, '-nostdin', '-v', 'error', '-xerror', '-copyts', '-max_alloc', '268435456', '-filter_complex_threads', '2', *inputs,
              '-filter_complex', ';'.join(filters), '-map', '[v]', '-map', '[a]', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-pix_fmt', 'yuv420p',
              '-threads', '2', '-r', str(fps), '-fps_mode', 'cfr', '-c:a', 'aac', '-ar', '48000', '-ac', '2', '-movflags', '+faststart', '-n', str(temporary)])
        metadata = probe_media(temporary, ffmpeg=ffmpeg_bin, ffprobe=ffprobe_bin)
        if metadata['frames'] != total or metadata['fps'] != fps or (metadata['width'], metadata['height']) != (width, height) or not metadata['hasAudio']:
            raise ValueError('Postproduction output violates the frozen media contract')
        _source_check(sources)
        _publish(temporary, output)
        return metadata


def render_dialogue_audio(clips, output, *, fps=24, audio_clips=None, width=1344, height=768,
                          ffmpeg_bin='ffmpeg', ffprobe_bin='ffprobe'):
    """Render actual native+voiceover audio; exclude independent music/ambience."""
    output = _new_output(output)
    inputs, filters, sources, total = _compile(clips, audio_clips or [], width=width, height=height, fps=fps, dialogue=True, ffmpeg_bin=ffmpeg_bin, ffprobe_bin=ffprobe_bin)
    with tempfile.TemporaryDirectory(prefix='.drama-dialogue-', dir=output.parent) as work:
        temporary = Path(work) / 'dialogue.wav'
        _run([ffmpeg_bin, '-nostdin', '-v', 'error', '-xerror', '-copyts', '-filter_complex_threads', '2', *inputs,
              '-filter_complex', ';'.join(filters), '-map', '[a]', '-c:a', 'pcm_s16le', '-ar', '48000', '-ac', '2', '-n', str(temporary)])
        # This derived file may exceed the ORIGINAL upload cap for long timelines.
        import wave
        with wave.open(str(temporary), 'rb') as stream:
            if (stream.getnframes(), stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) != (total * 2000, 48000, 2, 2):
                raise ValueError('Dialogue audio violates the frozen sample mapping')
        _run([ffmpeg_bin, '-nostdin', '-v', 'error', '-xerror', '-i', str(temporary), '-f', 'null', '-'])
        _source_check(sources)
        _publish(temporary, output)
        return {'kind': 'audio', 'extension': 'wav', 'contentType': 'audio/wav', 'duration': total / fps,
                'sampleRate': 48000, 'channels': 2, 'decodedSamples48k': total * 2000, 'role': 'dialogue_check'}


def _timestamp(frame, fps, decimals, separator):
    scale = 10 ** decimals
    value = (frame * scale + fps // 2) // fps
    seconds, part = divmod(value, scale)
    minutes, second = divmod(seconds, 60)
    hour, minute = divmod(minutes, 60)
    return f'{hour:02d}:{minute:02d}:{second:02d}{separator}{part:0{decimals}d}'


def encode_subtitles(segments, *, fps=24, total_frames, width=1344, height=768, style=None):
    """Safe text encodings; ASS text never becomes an override/control sequence."""
    _int(fps, 'FPS', 24, 24); _int(total_frames, 'Timeline frames', 1, MAX_SECONDS * fps)
    _dimensions(width, height)
    if not isinstance(segments, list) or len(segments) > 2000:
        raise ValueError('Subtitles permit at most 2000 segments')
    style = style or {}
    if not isinstance(style, dict) or set(style) - {'fontFamily', 'fontSize', 'outline', 'marginV', 'maxLines'}:
        raise ValueError('Unsupported subtitle style')
    font = style.get('fontFamily', 'Noto Sans CJK SC')
    if not isinstance(font, str) or not re.fullmatch(r'[\w .\-]{1,100}', font):
        raise ValueError('Subtitle font family is invalid')
    size = _int(style.get('fontSize', 44), 'Font size', 12, 160)
    outline = _number(style.get('outline', 2), 'Outline', 0, 8)
    margin = _int(style.get('marginV', max(20, height // 20)), 'Bottom margin', 0, height // 2)
    max_lines = _int(style.get('maxLines', 2), 'Subtitle lines', 1, 2)
    header = f'[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: Default,{font},{size},&H00FFFFFF,&H000000FF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,{outline},0,2,24,24,{margin},1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
    srt, ass, previous = [], [], 0
    for index, item in enumerate(segments):
        if not isinstance(item, dict):
            raise ValueError('Subtitle segment must be an object')
        start = _int(item.get('startFrame'), 'Subtitle start', 0, total_frames)
        end = _int(item.get('endFrame'), 'Subtitle end', 1, total_frames)
        text = item.get('text')
        if start >= end or start < previous:
            raise ValueError('Subtitles must be ordered, non-overlapping, and within the timeline')
        if not isinstance(text, str) or not text.strip() or len(text) > 2000 or any(ord(c) < 32 and c not in '\n\t' for c in text):
            raise ValueError('Subtitle text is empty, oversized, or contains control characters')
        if len(text.split('\n')) > max_lines:
            raise ValueError('Subtitle exceeds the configured line limit')
        previous = end
        # Word-joiner guards are zero-width; they preserve literal glyphs while
        # preventing libass from interpreting user \N / {\...} as instructions.
        escaped = text.replace('\\', '\\\u2060').replace('{', '{\u2060').replace('}', '\u2060}').replace('\n', '\\N')
        ass.append(f'Dialogue: 0,{_timestamp(start,fps,2,".")},{_timestamp(end,fps,2,".")},Default,,0,0,0,,{escaped}')
        srt.append(f'{index+1}\n{_timestamp(start,fps,3,",")} --> {_timestamp(end,fps,3,",")}\n{html.escape(text, quote=False)}\n')
    return {'srt': '\n'.join(srt), 'ass': header + '\n'.join(ass) + '\n'}


def qualify_subtitle_font(font_path, *, font_family, required_text='', font_probe_bin='fc-query'):
    """Require an exact font face and glyph coverage; never accept OS fallback."""
    font_path = Path(font_path)
    original = _regular_file(font_path)
    if original.st_size > 128 * 1024 * 1024 or font_path.suffix.lower() not in ('.ttf', '.ttc', '.otf'):
        raise ValueError('Configured subtitle font must be a TTF, TTC, or OTF below 128 MiB')
    if not isinstance(font_family, str) or not font_family.strip():
        raise ValueError('An explicit subtitle font family is required')
    try:
        faces = _run([font_probe_bin, '--format', '%{family}\t%{charset}\n', str(font_path.absolute())], timeout=30)
    except ValueError as exc:
        raise ValueError('Subtitle font qualification tool is unavailable or failed (fc-query)') from exc
    required = {ord(c) for c in required_text if not c.isspace() and c != '\u2060'}
    matched = False
    for face in faces.splitlines():
        fields = face.split('\t')
        if len(fields) != 2 or font_family.casefold() not in {f.strip().casefold() for f in fields[0].split(',')}:
            continue
        matched = True
        try:
            ranges = [tuple(int(n, 16) for n in token.split('-')) for token in fields[1].split()]
            missing = [c for c in required if not any(r[0] <= c <= r[-1] for r in ranges)]
        except ValueError as exc:
            raise ValueError('Subtitle font character map is invalid') from exc
        if not missing:
            digest = hashlib.sha256(font_path.read_bytes()).hexdigest()
            if not _same_file(original, _regular_file(font_path)):
                raise ValueError('Subtitle font changed during qualification')
            return {'fontFamily': font_family, 'fontPath': str(font_path.absolute()), 'sha256': digest,
                    'verifiedCodepoints': len(required)}
    if not matched:
        raise ValueError('Configured font does not contain the requested subtitle font family')
    raise ValueError('Configured subtitle font lacks required glyphs; choose a font covering the actual subtitle text')


def _ass_font_request(ass_path):
    text = ass_path.read_text(encoding='utf-8', errors='strict')
    styles = [line.split(',')[1] for line in text.splitlines() if line.startswith('Style: Default,')]
    if len(styles) != 1:
        raise ValueError('Subtitle burning requires one generated Default font style')
    dialogue = []
    for line in text.splitlines():
        if line.startswith('Dialogue: '):
            fields = line.split(',', 9)
            if len(fields) != 10 or fields[3] != 'Default' or '{\\' in fields[9]:
                raise ValueError('Subtitle file contains an unsupported dialogue style')
            dialogue.append(fields[9].replace('\\N', '\n').replace('\u2060', ''))
    return styles[0], '\n'.join(dialogue)


def burn_subtitles(master_path, ass_path, output, *, ffmpeg_bin='ffmpeg', ffprobe_bin='ffprobe', fonts_dir=None,
                   font_file=None, font_family=None, font_probe_bin='fc-query'):
    """Burn an already generated ASS using fixed safe local names, not user text."""
    output = _new_output(output)
    master_path, ass_path = Path(master_path), Path(ass_path)
    master = probe_media(master_path, ffmpeg=ffmpeg_bin, ffprobe=ffprobe_bin)
    original, subtitles = _regular_file(master_path), _regular_file(ass_path)
    if subtitles.st_size > 8 * 1024 * 1024:
        raise ValueError('ASS file exceeds the subtitle size limit')
    filters = _run([ffmpeg_bin, '-v', 'error', '-filters'])
    if not re.search(r'\bsubtitles\s', filters):
        raise ValueError('Configured FFmpeg lacks subtitles/libass; subtitle burning is unavailable')
    if fonts_dir is not None and (not Path(fonts_dir).is_dir() or Path(fonts_dir).is_symlink()):
        raise ValueError('Configured subtitle fonts directory is unavailable')
    requested_family, required_text = _ass_font_request(ass_path)
    if font_family is not None and font_family != requested_family:
        raise ValueError('Configured subtitle font family differs from the frozen ASS style')
    candidates = [Path(font_file)] if font_file is not None else (list(Path(fonts_dir).iterdir()) if fonts_dir else [])
    if not candidates or len(candidates) > 64:
        raise ValueError('An explicit qualified subtitle font file or bounded fonts directory is required')
    selected = None
    for candidate in candidates:
        try:
            qualify_subtitle_font(candidate, font_family=requested_family, required_text=required_text, font_probe_bin=font_probe_bin)
            selected = candidate
            break
        except ValueError:
            if font_file is not None:
                raise
    if selected is None:
        raise ValueError('No configured subtitle font covers the exact family and actual text')
    with tempfile.TemporaryDirectory(prefix='.drama-burn-', dir=output.parent) as work:
        work_path = Path(work)
        safe_ass = work_path / 'captions.ass'
        shutil.copyfile(ass_path, safe_ass)
        (work_path / 'fonts').mkdir()
        safe_font = work_path / 'fonts' / ('qualified' + selected.suffix.lower())
        shutil.copyfile(selected, safe_font)
        qualification = qualify_subtitle_font(safe_font, font_family=requested_family, required_text=required_text, font_probe_bin=font_probe_bin)
        subtitle_filter = 'subtitles=filename=captions.ass:fontsdir=fonts'
        temporary = work_path / 'burned.mp4'
        command = [ffmpeg_bin, '-nostdin', '-v', 'error', '-xerror', '-protocol_whitelist', 'file,pipe', '-threads', '2', '-i', str(master_path.absolute()),
                   '-vf', subtitle_filter, '-map', '0:v:0', '-map', '0:a?', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-pix_fmt', 'yuv420p', '-c:a', 'copy', '-movflags', '+faststart', '-n', str(temporary.absolute())]
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                result = subprocess.run(command, cwd=work_path, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, timeout=1800, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ValueError('Subtitle encoder failed or timed out') from exc
            if result.returncode or stderr.tell() or stdout.tell() > 256 * 1024:
                raise ValueError('Subtitle rendering failed; verify libass and configured fonts')
        metadata = probe_media(temporary, ffmpeg=ffmpeg_bin, ffprobe=ffprobe_bin)
        if any(metadata[k] != master[k] for k in ('width', 'height', 'fps', 'frames', 'hasAudio')):
            raise ValueError('Burned video changed the frozen media contract')
        if not _same_file(original, _regular_file(master_path)) or not _same_file(subtitles, _regular_file(ass_path)):
            raise ValueError('Subtitle or master changed during rendering')
        _publish(temporary, output)
        return {**metadata, 'subtitleFontFamily': requested_family, 'subtitleFontSha256': qualification['sha256']}
