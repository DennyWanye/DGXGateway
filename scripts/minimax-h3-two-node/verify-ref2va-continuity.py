#!/usr/bin/env python3
"""Validate the frozen MiniMax H3 Ref2VA technical and continuity gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import statistics
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HEX64 = re.compile(r"[0-9a-f]{64}")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
SCORE_KEYS = ("face_identity", "appearance", "wardrobe", "shot_freedom")
CASES = ("fl2va-control", "ref2va-candidate")


class EvidenceError(RuntimeError):
    pass


def _json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise EvidenceError(f"required evidence is missing or unsafe: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"required evidence is invalid: {path}") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"evidence root is not an object: {path}")
    return value


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise EvidenceError(f"missing timestamp: {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError(f"invalid timestamp: {field}") from exc
    if parsed.tzinfo is None:
        raise EvidenceError(f"timestamp lacks timezone: {field}")
    return parsed.astimezone(timezone.utc)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_cluster(root: Path) -> None:
    for filename in ("cluster-before.json", "cluster-after.json"):
        value = _json(root / filename)
        if value.get("exact_two") is not True or value.get("counter_deltas_zero") is not True:
            raise EvidenceError(f"cluster evidence failed: {filename}")
    acquire = _json(root / "lock-acquire-completion.json")
    release = _json(root / "lock-release-completion.json")
    if acquire.get("terminal") != "LOCKED" or release.get("terminal") != "UNLOCKED":
        raise EvidenceError("shared-lock lifecycle is incomplete")
    for value in (acquire, release):
        if not re.fullmatch(r"[0-9a-f]{32,64}", str(value.get("operation_id", ""))):
            raise EvidenceError("shared-lock operation identity is unresolved")


def _validate_identities(root: Path) -> None:
    artifact_ids = []
    for name in ("head-verify.json", "worker-verify-1.json", "worker-verify-2.json"):
        value = _json(root / "artifacts" / name)
        tree = value.get("tree_sha256")
        if value.get("verified") is not True or not isinstance(tree, str) or not HEX64.fullmatch(tree):
            raise EvidenceError(f"artifact identity is unresolved: {name}")
        artifact_ids.append(tree)
    if len(set(artifact_ids)) != 1:
        raise EvidenceError("head/worker artifact identities differ")
    image_ids = []
    for name in ("head-image.json", "worker-image.json"):
        value = _json(root / "runtime" / name)
        image = value.get("image_id")
        if value.get("verified") is not True or not isinstance(image, str) or not IMAGE_ID.fullmatch(image):
            raise EvidenceError(f"runtime identity is unresolved: {name}")
        image_ids.append(image)
    if len(set(image_ids)) != 1:
        raise EvidenceError("head/worker runtime image identities differ")


def _media_duration(probe: dict[str, Any], kind: str) -> float:
    raw_streams = probe.get("streams")
    if not isinstance(raw_streams, list):
        raise EvidenceError("ffprobe streams are missing")
    streams = [item for item in raw_streams if isinstance(item, dict) and item.get("codec_type") == kind]
    if len(streams) != 1:
        raise EvidenceError(f"output must contain exactly one {kind} stream")
    format_value = probe.get("format")
    fallback = format_value.get("duration") if isinstance(format_value, dict) else None
    raw = streams[0].get("duration", fallback)
    try:
        duration = float(raw)
    except (TypeError, ValueError) as exc:
        raise EvidenceError(f"output {kind} duration is invalid") from exc
    if not 0 < duration < 60:
        raise EvidenceError(f"output {kind} duration is invalid")
    return duration


def _validate_requests(root: Path, seeds: list[int]) -> None:
    observed_hashes: set[str] = set()
    requests_root = root / "requests"
    if requests_root.is_symlink() or not requests_root.is_dir():
        raise EvidenceError("requests evidence directory is missing or unsafe")
    actual_cases = {item.name for item in requests_root.iterdir() if item.is_dir()}
    if any(item.is_symlink() for item in requests_root.iterdir()):
        raise EvidenceError("request case path is a symlink")
    if actual_cases != set(CASES):
        raise EvidenceError("request cases are missing, extra, or unbalanced")
    for case in CASES:
        case_root = requests_root / case
        actual_seeds = {item.name for item in case_root.iterdir() if item.is_dir()}
        if any(item.is_symlink() for item in case_root.iterdir()):
            raise EvidenceError("request seed path is a symlink")
        if actual_seeds != {str(seed) for seed in seeds}:
            raise EvidenceError(f"request seeds are unbalanced for {case}")
        for seed in seeds:
            directory = case_root / str(seed)
            request = _json(directory / "request.json")
            response = _json(directory / "response.json")
            probe = _json(directory / "ffprobe.json")
            output = directory / "output.mp4"
            if output.is_symlink() or not output.is_file() or output.stat().st_size < 1:
                raise EvidenceError("output MP4 is missing, empty, or unsafe")
            if request.get("case") != case or request.get("seed") != seed:
                raise EvidenceError("request identity does not match its evidence path")
            try:
                frozen_request = request.get("width") == 1344 and request.get("height") == 768 and float(request.get("duration", 0)) == 4.0
            except (TypeError, ValueError):
                frozen_request = False
            if not frozen_request:
                raise EvidenceError("request geometry/duration differs from the frozen gate")
            if response.get("exit_code") != 0:
                raise EvidenceError("generation exit code is non-zero")
            digest = _sha256(output)
            if response.get("output_sha256") != digest or not HEX64.fullmatch(digest):
                raise EvidenceError("output hash correlation failed")
            if digest in observed_hashes:
                raise EvidenceError("an output hash was reused across A/B evidence")
            observed_hashes.add(digest)
            try:
                mean_luma = float(response["mean_luma"])
                black_ratio = float(response["black_frame_ratio"])
            except (KeyError, TypeError, ValueError) as exc:
                raise EvidenceError("black/empty-frame evidence is missing") from exc
            if mean_luma <= 0.01 or not 0 <= black_ratio < 0.99:
                raise EvidenceError("output is black or visually empty")
            video_duration = _media_duration(probe, "video")
            audio_duration = _media_duration(probe, "audio")
            if not (4.0 - 1 / 24 <= video_duration <= 5.0 and 4.0 - 1 / 24 <= audio_duration <= 5.0):
                raise EvidenceError("output duration differs from the frozen gate")
            video = next(item for item in probe["streams"] if item.get("codec_type") == "video")
            if (video.get("width"), video.get("height")) != (1344, 768):
                raise EvidenceError("output dimensions differ from the frozen gate")


def _score(value: Any, label: str) -> int:
    if not isinstance(value, dict) or set(value) != set(SCORE_KEYS):
        raise EvidenceError(f"rating fields are invalid: {label}")
    scores = list(value.values())
    if any(isinstance(item, bool) or not isinstance(item, int) or item not in (0, 1, 2) for item in scores):
        raise EvidenceError(f"rating score is outside 0..2: {label}")
    return sum(scores)


def _validate_review(root: Path, seeds: list[int]) -> tuple[float, float, int]:
    pairs_doc = _json(root / "review/blind-pairs.json")
    ratings_doc = _json(root / "review/director-ratings.json")
    if pairs_doc.get("rubric_version") != 1 or ratings_doc.get("rubric_version") != 1:
        raise EvidenceError("review rubric version changed")
    pairs = pairs_doc.get("pairs")
    ratings = ratings_doc.get("ratings")
    if not isinstance(pairs, list) or not isinstance(ratings, list) or len(pairs) != len(seeds) or len(ratings) != len(seeds):
        raise EvidenceError("review pairs/ratings are incomplete")
    pair_by_id = {}
    for pair in pairs:
        if not isinstance(pair, dict) or set(pair) != {"pair_id", "seed", "a", "b"}:
            raise EvidenceError("blind pair fields are invalid")
        if pair.get("seed") not in seeds or pair.get("a") != CASES[0] or pair.get("b") != CASES[1]:
            raise EvidenceError("blind pair mapping is invalid")
        pair_id = pair.get("pair_id")
        if not isinstance(pair_id, str) or pair_id in pair_by_id:
            raise EvidenceError("blind pair identity is invalid or duplicate")
        pair_by_id[pair_id] = pair
    control_scores: list[int] = []
    ref_scores: list[int] = []
    improvements = 0
    seen = set()
    for rating in ratings:
        if not isinstance(rating, dict) or set(rating) != {"pair_id", "control", "ref2va"}:
            raise EvidenceError("director rating fields are invalid")
        pair_id = rating.get("pair_id")
        if pair_id not in pair_by_id or pair_id in seen:
            raise EvidenceError("director rating identity is missing or duplicate")
        seen.add(pair_id)
        control = _score(rating["control"], f"{pair_id}/control")
        ref = _score(rating["ref2va"], f"{pair_id}/ref2va")
        if rating["ref2va"]["face_identity"] == 0:
            raise EvidenceError("Ref2VA has a severe identity break")
        if ref < control - 1:
            raise EvidenceError("Ref2VA regressed by more than one point")
        improvements += int(ref > control)
        control_scores.append(control)
        ref_scores.append(ref)
    if seen != set(pair_by_id) or improvements < 2:
        raise EvidenceError("Ref2VA did not improve at least two matched seeds")
    control_median = float(statistics.median(control_scores))
    ref_median = float(statistics.median(ref_scores))
    if ref_median <= control_median:
        raise EvidenceError("Ref2VA median continuity is not strictly higher")
    return control_median, ref_median, improvements


def validate_run(root: Path | str) -> dict[str, Any]:
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise EvidenceError("run directory is missing or unsafe")
    run = _json(root / "run.json")
    if run.get("schema_version") != 1 or run.get("profiles") != list(CASES):
        raise EvidenceError("run contract is invalid")
    seeds = run.get("expected_seeds")
    if not isinstance(seeds, list) or len(seeds) != 3 or len(set(seeds)) != 3 or any(isinstance(x, bool) or not isinstance(x, int) for x in seeds):
        raise EvidenceError("run must declare three unique integer seeds")
    _timestamp(run.get("created_at"), "run.created_at")
    _validate_cluster(root)
    _validate_identities(root)
    _validate_requests(root, seeds)
    control_median, ref_median, improvements = _validate_review(root, seeds)
    ratings = _json(root / "review/director-ratings.json")
    result = _json(root / "continuity-result.json")
    if result.get("rubric_version") != 1 or result.get("status") != "PASS":
        raise EvidenceError("continuity result does not record the frozen PASS gate")
    if _timestamp(result.get("created_at"), "continuity-result.created_at") <= _timestamp(ratings.get("created_at"), "director-ratings.created_at"):
        raise EvidenceError("continuity result was written before director ratings")
    return {
        "schema_version": 1,
        "status": "PASS",
        "control_median": control_median,
        "ref2va_median": ref_median,
        "improved_pairs": improvements,
        "seeds": seeds,
    }


def _write_fixture_json(root: Path, relative: str, value: dict[str, Any]) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _build_self_test_run(root: Path) -> None:
    _write_fixture_json(root, "run.json", {"schema_version": 1, "run_id": "self-test", "created_at": "2026-09-01T00:00:00Z", "expected_seeds": [11, 22, 33], "profiles": list(CASES)})
    for name in ("cluster-before.json", "cluster-after.json"):
        _write_fixture_json(root, name, {"exact_two": True, "counter_deltas_zero": True})
    _write_fixture_json(root, "lock-acquire-completion.json", {"terminal": "LOCKED", "operation_id": "a" * 32})
    _write_fixture_json(root, "lock-release-completion.json", {"terminal": "UNLOCKED", "operation_id": "b" * 32})
    for name in ("head-verify.json", "worker-verify-1.json", "worker-verify-2.json"):
        _write_fixture_json(root, f"artifacts/{name}", {"verified": True, "tree_sha256": "a" * 64})
    for name in ("head-image.json", "worker-image.json"):
        _write_fixture_json(root, f"runtime/{name}", {"verified": True, "image_id": "sha256:" + "b" * 64})
    for case in CASES:
        for seed in (11, 22, 33):
            relative = f"requests/{case}/{seed}"
            directory = root / relative
            directory.mkdir(parents=True, exist_ok=True)
            output = directory / "output.mp4"
            output.write_bytes(f"self-test-{case}-{seed}".encode())
            _write_fixture_json(root, f"{relative}/request.json", {"case": case, "seed": seed, "width": 1344, "height": 768, "duration": 4.0})
            _write_fixture_json(root, f"{relative}/response.json", {"exit_code": 0, "output_sha256": _sha256(output), "mean_luma": 0.4, "black_frame_ratio": 0.0})
            _write_fixture_json(root, f"{relative}/ffprobe.json", {"format": {"duration": "4.0"}, "streams": [{"codec_type": "video", "width": 1344, "height": 768, "duration": "4.0"}, {"codec_type": "audio", "duration": "4.0"}]})
    pairs = [{"pair_id": f"pair-{seed}", "seed": seed, "a": CASES[0], "b": CASES[1]} for seed in (11, 22, 33)]
    ratings = [{"pair_id": f"pair-{seed}", "control": {key: 1 for key in SCORE_KEYS}, "ref2va": {key: 2 for key in SCORE_KEYS}} for seed in (11, 22, 33)]
    _write_fixture_json(root, "review/blind-pairs.json", {"rubric_version": 1, "pairs": pairs})
    _write_fixture_json(root, "review/director-ratings.json", {"rubric_version": 1, "created_at": "2026-09-01T01:00:00Z", "ratings": ratings})
    _write_fixture_json(root, "continuity-result.json", {"rubric_version": 1, "created_at": "2026-09-01T01:00:01Z", "status": "PASS"})


def _run_self_tests(path: Path) -> None:
    scenarios = _json(path / "scenarios.json")
    expected = {"valid": "PASS", "corrupt": "FAIL", "missing-audio": "FAIL", "severe-break": "FAIL", "unbalanced": "FAIL"}
    if scenarios.get("schema_version") != 1 or scenarios.get("scenarios") != expected:
        raise EvidenceError("self-test scenario manifest is invalid")
    for name, outcome in expected.items():
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _build_self_test_run(root)
            if name == "corrupt":
                (root / "requests/ref2va-candidate/11/output.mp4").write_bytes(b"corrupt")
            elif name == "missing-audio":
                path = root / "requests/ref2va-candidate/11/ffprobe.json"
                value = _json(path); value["streams"] = value["streams"][:1]; _write_fixture_json(root, str(path.relative_to(root)), value)
            elif name == "severe-break":
                path = root / "review/director-ratings.json"
                value = _json(path); value["ratings"][0]["ref2va"]["face_identity"] = 0; _write_fixture_json(root, str(path.relative_to(root)), value)
            elif name == "unbalanced":
                shutil.rmtree(root / "requests/ref2va-candidate/33")
            try:
                validate_run(root)
                observed = "PASS"
            except EvidenceError:
                observed = "FAIL"
            if observed != outcome:
                raise EvidenceError(f"self-test scenario {name} expected {outcome}, observed {observed}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_directory", nargs="?", type=Path)
    parser.add_argument("--self-test-fixtures", type=Path)
    args = parser.parse_args()
    try:
        if args.self_test_fixtures:
            _run_self_tests(args.self_test_fixtures)
            print("PASS valid; FAIL corrupt, missing-audio, severe-break, unbalanced")
            return 0
        if args.run_directory is None:
            parser.error("run_directory or --self-test-fixtures is required")
        print(json.dumps(validate_run(args.run_directory), sort_keys=True))
        return 0
    except EvidenceError as exc:
        print(f"FAIL: {exc}", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
