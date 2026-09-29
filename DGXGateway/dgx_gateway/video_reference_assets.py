"""Content-addressed, local MP4 references for Ring4 Ref2VA."""
from __future__ import annotations

import hashlib
import errno
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .image_assets import _filename, _read_regular, _write_private

MAX_VIDEO_REFERENCE_BYTES = 256 * 1024 * 1024
_ASSET_ID = re.compile(r"[0-9a-f]{64}\Z")


def _probe(path: Path) -> dict:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=format_name,duration:stream=codec_type,codec_name,width,height",
             "-of", "json", str(path)], stdin=subprocess.DEVNULL,
            capture_output=True, timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("video reference probe is unavailable") from exc
    if result.returncode or len(result.stdout) > 256 * 1024:
        raise ValueError("video reference is not a supported MP4")
    try:
        info = json.loads(result.stdout)
        format_info = info["format"]
        duration = float(format_info["duration"])
        videos = [stream for stream in info.get("streams", []) if stream.get("codec_type") == "video"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("video reference metadata is invalid") from exc
    if "mp4" not in format_info.get("format_name", "").split(",") or len(videos) != 1 or not 2 <= duration <= 15:
        raise ValueError("video reference must be one 2–15 second MP4 video stream")
    return {"duration_seconds": duration, "codec": videos[0].get("codec_name", "unknown"),
            "width": videos[0].get("width"), "height": videos[0].get("height")}


class VideoReferenceAssets:
    def __init__(self, root: Path | str):
        self.root = Path(root).absolute()
        if self.root.is_symlink():
            raise ValueError("video asset root must not be a symbolic link")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)

    def register_bytes(self, filename: str, data: bytes) -> dict:
        filename = _filename(filename)
        if not isinstance(data, bytes) or not 0 < len(data) <= MAX_VIDEO_REFERENCE_BYTES:
            raise ValueError("video reference must be a non-empty MP4 no larger than 256 MiB")
        asset_id = hashlib.sha256(data).hexdigest()
        directory = self.root / asset_id
        if directory.exists() or directory.is_symlink():
            return self.get(asset_id)
        temporary = Path(tempfile.mkdtemp(prefix=".register-", dir=self.root))
        try:
            media = temporary / f"dgx-video-ref-{asset_id}.mp4"
            _write_private(media, data)
            probe = _probe(media)
            metadata = {"asset_id": asset_id, "sha256": asset_id, "size_bytes": len(data),
                        "content_type": "video/mp4", "filename": media.name,
                        "original_filename": filename, "validation": "ffprobe_and_sha256", **probe}
            _write_private(temporary / "metadata.json", json.dumps(metadata, ensure_ascii=False).encode())
            try:
                os.rename(temporary, directory)
            except OSError as exc:
                if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return self.get(asset_id)

    def get(self, asset_id: str) -> dict:
        if not isinstance(asset_id, str) or not _ASSET_ID.fullmatch(asset_id):
            raise ValueError("video asset id must be 64 lowercase hex characters")
        directory = self.root / asset_id
        if directory.is_symlink():
            raise ValueError("video asset directory must not be a symbolic link")
        metadata = json.loads(_read_regular(directory / "metadata.json", 8192))
        name = _filename(metadata.get("filename"))
        if name != f"dgx-video-ref-{asset_id}.mp4":
            raise ValueError("invalid stored video reference filename")
        path = directory / name
        data = _read_regular(path, MAX_VIDEO_REFERENCE_BYTES)
        if (hashlib.sha256(data).hexdigest() != asset_id or metadata.get("asset_id") != asset_id
                or metadata.get("sha256") != asset_id or metadata.get("size_bytes") != len(data)
                or metadata.get("content_type") != "video/mp4"
                or not 2 <= float(metadata.get("duration_seconds", 0)) <= 15):
            raise ValueError("video reference integrity check failed")
        return {**metadata, "path": str(path)}
