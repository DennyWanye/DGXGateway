#!/usr/bin/env python3
"""Local media checks + review sheets; never claims visual or lip-sync review."""
import argparse
import array
import csv
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import time


def sha_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def run(argv, timeout=300):
    return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def pcm16(ffmpeg, path, seconds):
    cmd = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-i', str(path), '-map', '0:a:0',
           '-t', str(seconds), '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', '-f', 's16le', '-']
    result = run(cmd)
    if result.returncode:
        raise RuntimeError('PCM decode failed: ' + result.stderr.decode(errors='replace'))
    values = array.array('h')
    values.frombytes(result.stdout)
    if sys.byteorder != 'little':
        values.byteswap()
    return values


def compare_audio(actual, reference):
    count = min(len(actual), len(reference))
    if not count:
        raise ValueError('No audio samples to compare')
    mean_a = sum(actual[:count]) / count
    mean_b = sum(reference[:count]) / count
    covariance = variance_a = variance_b = 0.0
    for a, b in zip(actual[:count], reference[:count]):
        da, db = a - mean_a, b - mean_b
        covariance += da * db
        variance_a += da * da
        variance_b += db * db
    denominator = math.sqrt(variance_a * variance_b)
    return {'method': 'Pearson correlation at zero lag, decoded mono PCM16 16000Hz; no shift search',
            'reference_samples': len(reference), 'output_samples': len(actual), 'compared_samples': count,
            'compared_seconds': count / 16000, 'correlation': covariance / denominator if denominator else None,
            'output_rms_pcm': math.sqrt(sum(v*v for v in actual[:count]) / count),
            'reference_rms_pcm': math.sqrt(sum(v*v for v in reference[:count]) / count),
            'interpretation': 'Audio prefix similarity only; NOT lip-sync, dialogue correctness or visual review',
            'gate': 'INFORMATION_ONLY_NO_LIPSYNC_INFERENCE'}


def inspect(args):
    args.video = args.video.resolve(strict=True)
    args.reference_audio = args.reference_audio.resolve(strict=True)
    if not 1 <= args.segments <= 19:
        raise ValueError('segments must be 1..19')
    if not args.video.is_file() or not args.reference_audio.is_file():
        raise ValueError('video and reference-audio must be regular files')
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        raise RuntimeError('Existing ffmpeg and ffprobe are required; no package installation performed')
    output = args.output_dir.absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError('output-dir must not exist; refusing overwrite')
    output.mkdir(parents=False, exist_ok=False)
    started = time.monotonic()
    expected_frames = 93 + 80 * (args.segments - 1)
    expected_seconds = expected_frames / 25
    report = {'status': 'TECHNICAL_FAIL', 'visual_review': 'NOT_REVIEWED', 'lipsync_review': 'NOT_REVIEWED',
              'segments': args.segments, 'expected_frames': expected_frames, 'expected_seconds': expected_seconds,
              'video': str(args.video), 'video_sha256': sha_file(args.video),
              'reference_audio': str(args.reference_audio), 'reference_audio_sha256': sha_file(args.reference_audio),
              'checks': {}, 'commands': [], 'review_sheets': []}
    def checked(argv, label):
        report['commands'].append(argv)
        result = run(argv)
        (output / (label + '.stderr.txt')).write_bytes(result.stderr)
        if result.returncode:
            raise RuntimeError(f'{label} exited {result.returncode}')
        return result
    try:
        probe_cmd = [ffprobe, '-v', 'error', '-count_frames', '-show_streams', '-show_format', '-of', 'json', str(args.video)]
        result = checked(probe_cmd, 'ffprobe')
        (output / 'ffprobe.json').write_bytes(result.stdout)
        info = json.loads(result.stdout)
        video = next(s for s in info['streams'] if s['codec_type'] == 'video')
        audio = next(s for s in info['streams'] if s['codec_type'] == 'audio')
        rate = float(Fraction(video['avg_frame_rate']))
        actual_frames = int(video['nb_read_frames'])
        video_seconds = float(video['duration'])
        audio_seconds = float(audio['duration'])
        video_start, audio_start = float(video['start_time']), float(audio['start_time'])
        report['media'] = {'fps': rate, 'frames': actual_frames, 'video_duration_seconds': video_seconds,
             'width': video['width'], 'height': video['height'], 'video_codec': video['codec_name'],
             'audio_codec': audio['codec_name'], 'audio_sample_rate': int(audio['sample_rate']), 'audio_channels': audio['channels'],
             'video_start_pts': video.get('start_pts'), 'video_time_base': video.get('time_base'),
             'audio_start_pts': audio.get('start_pts'), 'audio_time_base': audio.get('time_base'),
             'video_start_seconds': video_start, 'audio_start_seconds': audio_start,
             'audio_duration_seconds': audio_seconds, 'audio_minus_video_start_seconds': audio_start-video_start}
        report['checks'].update(frame_count_matches=actual_frames == expected_frames, fps_25=abs(rate-25) < 1e-6,
             positive_resolution=video['width'] > 0 and video['height'] > 0,
             video_duration_matches=abs(video_seconds-expected_seconds) <= 0.04,
             audio_video_duration_within_100ms=abs(audio_seconds-video_seconds) <= 0.1,
             audio_video_start_within_100ms=abs(audio_start-video_start) <= 0.1)
        report['tolerances'] = {'video_duration_seconds': 0.04, 'audio_video_duration_seconds': 0.1,
                                'audio_video_start_seconds': 0.1, 'fps_absolute': 1e-6}
        decode_cmd = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-i', str(args.video), '-f', 'null', '-']
        report['commands'].append(decode_cmd)
        decode = run(decode_cmd)
        (output / 'full-decode.stderr.txt').write_bytes(decode.stderr)
        report['full_decode_exit_code'] = decode.returncode
        report['checks']['full_decode_ok'] = decode.returncode == 0 and not decode.stderr.strip()
        reference_pcm = pcm16(ffmpeg, args.reference_audio, expected_seconds)
        output_pcm = pcm16(ffmpeg, args.video, expected_seconds)
        report['audio_comparison'] = compare_audio(output_pcm, reference_pcm)
        report['checks']['reference_prefix_long_enough'] = len(reference_pcm) >= round(expected_seconds * 16000)
        report['checks']['output_audio_nonzero'] = report['audio_comparison']['output_rms_pcm'] > 0

        # Obtain decoded frame timestamps, not packet timestamps or assumed frame rounding.
        timestamp_cmd = [ffprobe, '-v', 'error', '-select_streams', 'v:0', '-show_frames',
                         '-show_entries', 'frame=best_effort_timestamp_time', '-of', 'json', str(args.video)]
        result = checked(timestamp_cmd, 'frame-timestamps')
        (output / 'frame-timestamps.json').write_bytes(result.stdout)
        timestamps = [float(frame['best_effort_timestamp_time']) for frame in json.loads(result.stdout)['frames']]
        report['checks']['decoded_timestamp_count_matches'] = len(timestamps) == expected_frames
        report['checks']['timestamps_25fps'] = all(abs((right-left)-0.04) <= 1e-5 for left, right in zip(timestamps, timestamps[1:]))
        if actual_frames != expected_frames or len(timestamps) != expected_frames:
            raise ValueError('Frame mismatch: refusing to produce misleading expected-boundary sheets')
        rows = []
        if args.segments == 1:
            rows.append({'kind': 'single_segment_overview', 'boundary_index': None, 'boundary_frame': None,
                         'indices': [0, 30, 61, expected_frames-1]})
        else:
            for boundary in range(args.segments-1):
                index = 93 + 80 * boundary
                rows.append({'kind': 'continuation_boundary', 'boundary_index': boundary+1, 'boundary_frame': index,
                             'indices': [index-2, index-1, index, index+1]})
        frame_dir = output / 'frames'
        frame_dir.mkdir()
        indices = [index for row in rows for index in row['indices']]
        selection = '+'.join(f'eq(n\\,{index})' for index in indices)
        extract_cmd = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n', '-i', str(args.video),
                       '-map', '0:v:0', '-vf', 'select=' + selection, '-fps_mode', 'vfr', str(frame_dir / 'frame-%05d.png')]
        checked(extract_cmd, 'extract-frames')
        frames = sorted(frame_dir.glob('frame-*.png'))
        if len(frames) != len(indices):
            raise RuntimeError('Extracted frame count mismatch; no placeholder frames are allowed')
        frame_manifest = []
        for position, (index, path) in enumerate(zip(indices, frames)):
            row = rows[position // 4]
            frame_manifest.append({'image': str(path.relative_to(output)), 'frame_index_zero_based': index,
                    'decoded_timestamp_seconds': timestamps[index], 'relative_time_seconds': timestamps[index] - video_start,
                    'kind': row['kind'], 'boundary_index': row['boundary_index'], 'boundary_frame_zero_based': row['boundary_frame'],
                    'sheet': position // 24 + 1, 'row': (position // 4) % 6 + 1, 'column': position % 4 + 1,
                    'sha256': sha_file(path)})
        (output / 'review-frames.json').write_text(json.dumps(frame_manifest, indent=2)+'\n')
        with (output / 'review-frames.csv').open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(frame_manifest[0]))
            writer.writeheader()
            writer.writerows(frame_manifest)
        for sheet_start in range(0, len(rows), 6):
            sheet_rows = min(6, len(rows)-sheet_start)
            sheet = output / f'review-sheet-{sheet_start//6+1:02d}.png'
            sheet_cmd = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n', '-framerate', '1',
                '-start_number', str(sheet_start*4+1), '-i', str(frame_dir/'frame-%05d.png'),
                '-vf', f'scale=320:-2,tile=4x{sheet_rows}:nb_frames={4*sheet_rows}', '-frames:v', '1', '-update', '1', str(sheet)]
            checked(sheet_cmd, sheet.stem)
            report['review_sheets'].append({'file': sheet.name, 'rows': sheet_rows, 'columns': 4,
                'kind': 'single_segment_overview' if args.segments == 1 else 'continuation_boundaries',
                'first_boundary': None if args.segments == 1 else sheet_start+1,
                'last_boundary': None if args.segments == 1 else sheet_start+sheet_rows,
                'sha256': sha_file(sheet), 'placeholder_frames': 0})
        report['checks']['review_sheets_extracted'] = True
        report['status'] = 'TECHNICAL_PASS' if all(report['checks'].values()) else 'TECHNICAL_FAIL'
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        report['elapsed_seconds'] = time.monotonic()-started
        report['video_unchanged'] = sha_file(args.video) == report['video_sha256']
        report['reference_audio_unchanged'] = sha_file(args.reference_audio) == report['reference_audio_sha256']
        if not report['video_unchanged'] or not report['reference_audio_unchanged']:
            report['status'] = 'TECHNICAL_FAIL'
        (output / 'report.json').write_text(json.dumps(report, indent=2)+'\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--video', required=True, type=Path)
    parser.add_argument('--segments', required=True, type=int)
    parser.add_argument('--reference-audio', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()
    report = inspect(args)
    print(json.dumps(report, indent=2))
    return 0 if report['status'] == 'TECHNICAL_PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
