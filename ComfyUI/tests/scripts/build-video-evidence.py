#!/usr/bin/env python3
"""Build a fail-closed, hash-bound semantic review package for one H3 MP4."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


FRACTIONS = (0.1, 0.3, 0.5, 0.7, 0.9)
PROMPTS = {
    "S-CUI-1": "暖色工作灯下，微距镜头拍摄一名工程师稳定地焊接电路板，房间里有轻微环境声。",
    "S-CUI-2": "A continuous wide shot of ocean waves reaching a quiet beach at sunrise, with wind moving the grass and natural surf audio.",
    "S-CUI-3": "Three colorful paper boats move side by side through a shallow stream while the camera follows them, with gentle water sounds.",
}

COMMON_CRITERIA = [
    ("common-full-playback", "Play the entire MP4 once at 1.0x with audio unmuted in official ComfyUI PreviewVideo."),
    ("common-no-corruption", "Observe no freeze, unexplained cut, corruption, or obvious audio/video mismatch during full playback."),
    ("common-preview-identity", "Confirm the preview/download file SHA-256 matches the reviewed video SHA-256."),
]
SCENARIO_CRITERIA = {
    "S-CUI-1": [
        ("s1-circuit-board", "Circuit board is visible in at least four of five samples."),
        ("s1-engineer-soldering", "Engineer and soldering tool or operating pose are visible in at least three of five samples."),
        ("s1-warm-light", "Warm work-light appearance is visible in at least four of five samples."),
        ("s1-sustained-motion", "Hand/tool position changes across at least three non-adjacent samples while the board persists; playback shows sustained soldering, not a still."),
        ("s1-audio", "Light room ambience is audible and not obviously mismatched."),
    ],
    "S-CUI-2": [
        ("s2-ocean-beach-sunrise", "Ocean, beach, and sunrise are each visible in at least four of five samples."),
        ("s2-wavefront-motion", "Wavefront position changes across at least three non-adjacent samples."),
        ("s2-grass-motion", "Grass is visible in at least three samples and changes displacement/orientation in at least two sample-pair comparisons."),
        ("s2-continuity", "Full playback preserves a continuous wide composition without an unexplained cut."),
        ("s2-audio", "Surf or wind is audible, scene-appropriate, and not obviously mismatched."),
    ],
    "S-CUI-3": [
        ("s3-three-boats", "Three distinct colorful boats are visible in at least three of five samples."),
        ("s3-side-by-side", "The side-by-side spatial relationship is visible in at least three of five samples."),
        ("s3-shallow-stream", "A shallow stream is visible in at least four of five samples."),
        ("s3-motion", "Boat positions and water/background texture change across at least three non-adjacent samples."),
        ("s3-camera-follow", "Boats remain in the central image region in at least three samples while background/water changes, and full playback feels like a following camera."),
        ("s3-audio", "Gentle water sound is audible and not obviously mismatched."),
    ],
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bytes_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def command(argv: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"command failed ({argv[0]}): {result.stderr.strip()[:500]}")
    return result


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def volume_metrics(video: Path) -> tuple[float, float]:
    result = command(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(video), "-vn", "-af", "volumedetect", "-f", "null", "-"])
    mean = re.search(r"mean_volume:\s+(-?inf|[-0-9.]+) dB", result.stderr)
    peak = re.search(r"max_volume:\s+(-?inf|[-0-9.]+) dB", result.stderr)
    if not mean or not peak:
        raise RuntimeError("audio volume metrics were not produced")
    convert = lambda value: float("-inf") if value == "-inf" else float(value)
    return convert(mean.group(1)), convert(peak.group(1))


def build_contact_sheet(samples: list[dict[str, object]], sample_paths: list[Path], destination: Path, scenario_id: str) -> None:
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise RuntimeError("Pillow is required to create the labeled contact sheet") from exc

    opened = [Image.open(path).convert("RGB") for path in sample_paths]
    try:
        width = sum(image.width for image in opened)
        height = max(image.height for image in opened)
        sheet = Image.new("RGB", (width, height), "black")
        x = 0
        for image, sample in zip(opened, samples, strict=True):
            sheet.paste(image, (x, 0))
            label = f"{scenario_id} n={sample['frame_index']} pts={sample['pts_seconds']:.3f}"
            draw = ImageDraw.Draw(sheet)
            box = draw.textbbox((0, 0), label)
            text_height = box[3] - box[1]
            text_width = box[2] - box[0]
            y = image.height - text_height - 12
            draw.rectangle((x + 4, y - 4, x + text_width + 12, image.height - 4), fill=(0, 0, 0))
            draw.text((x + 8, y), label, fill=(255, 255, 255))
            x += image.width
        sheet.save(destination, format="PNG")
    finally:
        for image in opened:
            image.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario-id", choices=sorted(PROMPTS), required=True)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--resolution", choices=("768x448", "1344x768"), required=True)
    parser.add_argument("--duration", type=int, choices=(2, 3, 4), required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--workflow", type=Path, required=True)
    parser.add_argument("--queue-history", type=Path, required=True)
    parser.add_argument("--prompt-id", required=True)
    parser.add_argument("--reviewer-kind", choices=("human-user", "browser-agent"), required=True)
    parser.add_argument("--reviewer-id", required=True)
    parser.add_argument("--reviewer-confirmation", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.scenario_id == "S-CUI-1" and args.reviewer_kind != "human-user":
        raise ValueError("S-CUI-1 is reserved for reviewer-kind human-user")
    if args.scenario_id == "S-CUI-1":
        if args.reviewer_confirmation is None:
            raise ValueError("S-CUI-1 requires a reviewer confirmation artifact")
        if args.reviewer_confirmation.is_symlink() or not args.reviewer_confirmation.is_file():
            raise ValueError("reviewer confirmation must be a regular file")
    if args.prompt != PROMPTS[args.scenario_id]:
        raise ValueError(f"prompt does not exactly match {args.scenario_id}")
    if not 0 <= args.seed <= 2147483647:
        raise ValueError("seed is outside the H3 gateway range")
    for path in (args.video, args.workflow, args.queue_history):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"required evidence input is not a regular file: {path}")
    if args.prompt_id not in args.queue_history.read_text(encoding="utf-8"):
        raise ValueError("prompt-id is not present in queue history evidence")
    if args.output.exists():
        raise ValueError("output evidence directory already exists")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{args.output.name}.tmp-", dir=args.output.parent))
    try:
        copied_video = stage / "video.mp4"
        copied_workflow = stage / "workflow.json"
        copied_history = stage / "queue-history.json"
        shutil.copyfile(args.video, copied_video)
        shutil.copyfile(args.workflow, copied_workflow)
        shutil.copyfile(args.queue_history, copied_history)
        copied_confirmation = None
        if args.reviewer_confirmation is not None:
            copied_confirmation = stage / "reviewer-confirmation.txt"
            shutil.copyfile(args.reviewer_confirmation, copied_confirmation)
        video_hash = sha256(copied_video)

        command(["ffmpeg", "-v", "error", "-nostdin", "-i", str(copied_video), "-f", "null", "-"])
        probe = json.loads(command(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(copied_video)]).stdout)
        frames = json.loads(
            command(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_frames",
                    "-show_entries",
                    "frame=best_effort_timestamp_time",
                    "-of",
                    "json",
                    str(copied_video),
                ]
            ).stdout
        )["frames"]
        if len(frames) < 5:
            raise ValueError("video has fewer than five decoded frames")
        video_streams = [stream for stream in probe["streams"] if stream.get("codec_type") == "video"]
        audio_streams = [stream for stream in probe["streams"] if stream.get("codec_type") == "audio"]
        if len(video_streams) != 1 or len(audio_streams) != 1:
            raise ValueError("video must have exactly one video and one audio stream")
        expected_width, expected_height = (int(value) for value in args.resolution.split("x"))
        if (video_streams[0].get("width"), video_streams[0].get("height")) != (expected_width, expected_height):
            raise ValueError("decoded dimensions do not match requested resolution")
        video_duration = float(video_streams[0].get("duration", probe["format"]["duration"]))
        audio_duration = float(audio_streams[0].get("duration", probe["format"]["duration"]))
        if not args.duration - 1 / 24 <= video_duration <= args.duration + 1.0:
            raise ValueError("decoded video duration is outside the H3 acceptance window")
        duration_delta = abs(video_duration - audio_duration)
        mean_dbfs, peak_dbfs = volume_metrics(copied_video)

        sample_dir = stage / "samples"
        sample_dir.mkdir()
        sample_rows: list[dict[str, object]] = []
        sample_paths: list[Path] = []
        for ordinal, fraction in enumerate(FRACTIONS, start=1):
            frame_index = math.floor(fraction * (len(frames) - 1))
            pts = float(frames[frame_index]["best_effort_timestamp_time"])
            sample_path = sample_dir / f"frame-{ordinal:02d}.png"
            command(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-nostdin",
                    "-i",
                    str(copied_video),
                    "-vf",
                    f"select=eq(n\\,{frame_index})",
                    "-frames:v",
                    "1",
                    str(sample_path),
                ]
            )
            sample_rows.append(
                {
                    "fraction": fraction,
                    "frame_index": frame_index,
                    "pts_seconds": pts,
                    "png": str(sample_path.relative_to(stage)),
                    "png_sha256": sha256(sample_path),
                    "source_video_sha256": video_hash,
                }
            )
            sample_paths.append(sample_path)
        if len({row["frame_index"] for row in sample_rows}) != 5:
            raise ValueError("sampling did not yield five unique frames")

        write_json(stage / "samples.json", {"schema_version": 1, "video_sha256": video_hash, "fractions": list(FRACTIONS), "samples": sample_rows})
        build_contact_sheet(sample_rows, sample_paths, stage / "contact-sheet.png", args.scenario_id)
        write_json(stage / "ffprobe.json", probe)
        media = {
            "schema_version": 1,
            "video_sha256": video_hash,
            "full_decode": True,
            "measurements": {
                "video_streams": 1,
                "audio_streams": 1,
                "video_codec": video_streams[0].get("codec_name"),
                "audio_codec": audio_streams[0].get("codec_name"),
                "width": expected_width,
                "height": expected_height,
                "decoded_frames": len(frames),
                "video_duration_seconds": video_duration,
                "audio_duration_seconds": audio_duration,
                "absolute_duration_delta_seconds": duration_delta,
                "audio_mean_dbfs": mean_dbfs,
                "audio_peak_dbfs": peak_dbfs,
            },
            "machine_gates": {
                "av_duration_delta_lte_0_05": duration_delta <= 0.05,
                "audio_peak_gt_minus_60_dbfs": peak_dbfs > -60,
            },
        }
        machine_pass = all(media["machine_gates"].values())
        write_json(stage / "media.json", media)
        now = datetime.now(timezone.utc).isoformat()
        scenario = {
            "schema_version": 1,
            "scenario_id": args.scenario_id,
            "exact_prompt": args.prompt,
            "prompt_utf8_sha256": bytes_sha256(args.prompt),
            "resolution": args.resolution,
            "requested_duration_seconds": args.duration,
            "seed": args.seed,
            "prompt_id": args.prompt_id,
            "workflow_sha256": sha256(copied_workflow),
            "queue_history_sha256": sha256(copied_history),
            "video_sha256": video_hash,
            "evidence_built_at_utc": now,
        }
        write_json(stage / "scenario.json", scenario)
        criteria = [
            {"criterion_id": criterion_id, "required": True, "requirement": requirement, "verdict": "PENDING", "observation": "", "evidence_refs": []}
            for criterion_id, requirement in COMMON_CRITERIA + SCENARIO_CRITERIA[args.scenario_id]
        ]
        review = {
            "schema_version": 1,
            "scenario_id": args.scenario_id,
            "source_video_sha256": video_hash,
            "reviewer": {
                "kind": args.reviewer_kind,
                "id": args.reviewer_id,
                "reviewed_at_utc": None,
                "method": None,
                "confirmation_sha256": sha256(copied_confirmation) if copied_confirmation else None,
            },
            "bindings": {
                "video_sha256": video_hash,
                "workflow_sha256": sha256(copied_workflow),
                "queue_history_sha256": sha256(copied_history),
                "scenario_sha256": sha256(stage / "scenario.json"),
                "media_sha256": sha256(stage / "media.json"),
                "samples_sha256": sha256(stage / "samples.json"),
                "contact_sheet_sha256": sha256(stage / "contact-sheet.png"),
            },
            "playback": {"started_at_utc": None, "ended_at_utc": None, "played_from_start_to_end": False, "playback_rate": 1.0, "audio_unmuted": False},
            "browser_artifact_sha256": [],
            "criteria": criteria,
            "overall_verdict": "PENDING" if machine_pass else "BLOCKED_MEDIA",
            "derivation_rule": "PASS only when every required criterion is PASS; missing, PENDING, N/A, ambiguous, or unreferenced observations are FAIL.",
        }
        write_json(stage / "review.template.json", review)

        artifacts: dict[str, str] = {}
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                artifacts[str(path.relative_to(stage))] = sha256(path)
        manifest = {
            "schema_version": 1,
            "scenario_id": args.scenario_id,
            "video_sha256": video_hash,
            "artifacts": artifacts,
            "verdict": "PENDING_REVIEW" if machine_pass else "FAIL_MEDIA",
        }
        write_json(stage / "manifest.json", manifest)
        copied_video.chmod(0o444)
        stage.rename(args.output)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    if not machine_pass:
        print(f"ERROR: media quality gates failed; evidence retained at {args.output}", file=sys.stderr)
        return 2
    print(f"Evidence package created: {args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
