"""Offline whisper.cpp adapter with explicit local model and bounded output."""
import hashlib
import json
import math
from pathlib import Path
import subprocess
import tempfile
import time

from .contracts import DramaError


def transcribe(audio, *, worker, model, total_frames, fps=24, timeout=1800):
    model, audio = Path(model), Path(audio)
    if not model.is_file() or model.is_symlink():
        raise DramaError("ASR_MODEL_MISSING", "未配置可用的本地ASR模型", 503)
    with tempfile.TemporaryDirectory(prefix="drama-asr-") as directory:
        output = Path(directory) / "result"
        # No prompt/script is supplied. Recognition consumes the rendered audio only.
        args = [worker, "-m", str(model.resolve()), "-f", str(audio.resolve()), "-l", "auto", "-ng", "-t", "4", "-oj", "-of", str(output), "-np"]
        try:
            child = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise DramaError("ASR_WORKER_MISSING", "本地转写程序不可执行", 503) from exc
        result_path = output.with_suffix(".json")
        deadline = time.monotonic() + timeout
        try:
            while child.poll() is None:
                if time.monotonic() >= deadline:
                    raise DramaError("ASR_TIMEOUT", "本地转写超时，原音轨已保留")
                if result_path.exists() and result_path.stat().st_size > 4 * 1024 * 1024:
                    raise DramaError("ASR_OUTPUT_LIMIT", "转写结果超过大小上限")
                time.sleep(.05)
            if child.returncode != 0:
                raise DramaError("ASR_FAILED", "本地转写失败，请检查模型与音轨")
            if not result_path.is_file() or result_path.is_symlink() or result_path.stat().st_size > 4 * 1024 * 1024:
                raise DramaError("ASR_OUTPUT_INVALID", "转写程序未返回受支持的结果")
            result = None
            raw_bytes = result_path.read_bytes()
            raw_text = raw_bytes.decode("utf-8", errors="replace")
            def finite_float(value):
                parsed = float(value)
                if not math.isfinite(parsed):
                    raise ValueError("nonfinite JSON number")
                return parsed
            try:
                result = json.loads(raw_bytes.decode("utf-8"), parse_float=finite_float,
                                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                rows = result["transcription"]
                if not isinstance(rows, list) or len(rows) > 2000:
                    raise ValueError()
                segments, previous, previous_ms = [], 0, 0
                for row in rows:
                    start, end = row["offsets"]["from"], row["offsets"]["to"]
                    if type(start) not in (int, float) or type(end) not in (int, float) or not all(math.isfinite(v) for v in (start, end)) or start < 0 or end <= start:
                        raise ValueError()
                    if start < previous_ms:
                        raise ValueError()
                    if end > total_frames / fps * 1000 + 1e-6:
                        raise ValueError()
                    # Both sides of a shared boundary use the same nearest-frame rule.
                    first, last = math.floor(start * fps / 1000 + .5), math.floor(end * fps / 1000 + .5)
                    if end <= total_frames / fps * 1000 + 1e-6:
                        last = min(total_frames, last)
                    if first < previous or last > total_frames or last <= first:
                        raise ValueError()
                    value = row["text"]
                    if not isinstance(value, str) or not value.strip() or len(value) > 1000 or '\x00' in value:
                        raise ValueError()
                    segments.append({"startFrame": first, "endFrame": last, "text": value.strip()})
                    previous = last
                    previous_ms = end
            except (ValueError, TypeError, KeyError, OSError, OverflowError) as exc:
                error = DramaError("ASR_OUTPUT_INVALID", "转写结果时间轴或结构无效，原始结果已保留供校对")
                proposed = []
                if isinstance(result, dict) and isinstance(result.get("transcription"), list):
                    for row in result["transcription"][:2000]:
                        try:
                            start, end, value = row["offsets"]["from"], row["offsets"]["to"], row["text"]
                            if (type(start) not in (int, float) or type(end) not in (int, float)
                                    or not all(math.isfinite(v) for v in (start, end)) or not isinstance(value, str)):
                                continue
                            proposed.append({"startFrame": math.floor(start * fps / 1000 + .5),
                                             "endFrame": math.floor(end * fps / 1000 + .5), "text": value[:1000]})
                        except (TypeError, KeyError, OverflowError, ValueError):
                            continue
                error.asr_result = {"raw": result, "rawText": raw_text, "rawSha256": hashlib.sha256(raw_bytes).hexdigest(),
                                    "segments": proposed, "validationError": error.message,
                                    "requiresCorrection": True}
                raise error from exc
            with model.open("rb") as stream:
                model_hash = hashlib.file_digest(stream, "sha256").hexdigest()
            return segments, {"adapter": "whisper.cpp-v1", "modelSha256": model_hash, "language": result.get("result", {}).get("language"), "rawSegments": rows}
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()
