"""Content-addressed four-second PCM reference audio for H3 Ref2VA."""
from __future__ import annotations

import base64
import binascii
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import wave

from .image_assets import _filename, _read_regular, _write_private

MAX_AUDIO_BYTES = 262144
_MAX_ENCODED = ((MAX_AUDIO_BYTES + 2) // 3) * 4
_ASSET_ID = re.compile(r"[0-9a-f]{64}\Z")


def _validate_wav(data: bytes) -> None:
    if (len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE"
            or int.from_bytes(data[4:8], "little") != len(data) - 8):
        raise ValueError("audio must be a complete WAV file without trailing bytes")
    offset, data_chunks, format_chunks = 12, 0, 0
    while offset < len(data):
        if offset + 8 > len(data):
            raise ValueError("audio contains a truncated WAV chunk")
        kind = data[offset:offset + 4]
        length = int.from_bytes(data[offset + 4:offset + 8], "little")
        end = offset + 8 + length
        if end + (length % 2) > len(data):
            raise ValueError("audio contains truncated PCM samples")
        if kind == b"data":
            data_chunks += 1
        elif kind == b"fmt ":
            format_chunks += 1
            if length < 16 or int.from_bytes(data[offset + 8:offset + 10], "little") != 1:
                raise ValueError("audio must be uncompressed integer PCM WAV")
            fmt = data[offset + 8:end]
            if (int.from_bytes(fmt[2:4], "little"), int.from_bytes(fmt[4:8], "little"),
                    int.from_bytes(fmt[8:12], "little"), int.from_bytes(fmt[12:14], "little"),
                    int.from_bytes(fmt[14:16], "little")) != (1, 24000, 48000, 2, 16):
                raise ValueError("audio must be exactly four-second mono PCM16 24 kHz WAV")
        offset = end + (length % 2)
    if data_chunks != 1 or format_chunks != 1:
        raise ValueError("audio must contain exactly one PCM stream")
    try:
        with wave.open(io.BytesIO(data), "rb") as stream:
            valid = (
                stream.getnchannels() == 1
                and stream.getsampwidth() == 2
                and stream.getframerate() == 24000
                and stream.getnframes() == 96000
                and stream.getcomptype() == "NONE"
            )
            samples = stream.readframes(96001)
            valid = valid and len(samples) == 96000 * 2 and not stream.readframes(1)
    except (EOFError, wave.Error) as exc:
        raise ValueError("audio must be a valid WAV file") from exc
    if not valid:
        raise ValueError("audio must be exactly four-second mono PCM16 24 kHz WAV")


class AudioAssets:
    def __init__(self, root: Path | str):
        self.root = Path(root).absolute()
        if self.root.is_symlink():
            raise ValueError("audio asset root must not be a symbolic link")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)

    def register_base64(self, filename: str, audio_base64: str) -> dict:
        filename = _filename(filename)
        if not isinstance(audio_base64, str) or not audio_base64 or len(audio_base64) > _MAX_ENCODED:
            raise ValueError("audio_base64 must contain at most 256 KiB of audio data")
        try:
            data = base64.b64decode(audio_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("audio_base64 must be strict base64 without a data URI") from exc
        if len(data) > MAX_AUDIO_BYTES or base64.b64encode(data).decode("ascii") != audio_base64:
            raise ValueError("audio exceeds the limit or is not canonical base64")
        _validate_wav(data)
        asset_id = hashlib.sha256(data).hexdigest()
        directory = self.root / asset_id
        if directory.exists() or directory.is_symlink():
            return self.get(asset_id)
        name = f"dgx-audio-{asset_id}.wav"
        metadata = {
            "asset_id": asset_id,
            "sha256": asset_id,
            "size_bytes": len(data),
            "content_type": "audio/wav",
            "filename": name,
            "original_filename": filename,
            "duration_seconds": 4.0,
            "sample_rate": 24000,
            "channels": 1,
            "validation": "wav_contract_and_sha256",
        }
        temporary = Path(tempfile.mkdtemp(prefix=".register-", dir=self.root))
        try:
            _write_private(temporary / name, data)
            _write_private(temporary / "metadata.json", json.dumps(metadata).encode())
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
            raise ValueError("asset_id must be 64 lowercase hex characters")
        directory = self.root / asset_id
        if directory.is_symlink():
            raise ValueError("audio asset directory must not be a symbolic link")
        metadata = json.loads(_read_regular(directory / "metadata.json", 4096))
        name = _filename(metadata.get("filename"))
        if name != f"dgx-audio-{asset_id}.wav":
            raise ValueError("invalid stored audio filename")
        path = directory / name
        data = _read_regular(path, MAX_AUDIO_BYTES)
        _validate_wav(data)
        if (
            hashlib.sha256(data).hexdigest() != asset_id
            or metadata.get("asset_id") != asset_id
            or metadata.get("sha256") != asset_id
            or metadata.get("size_bytes") != len(data)
            or metadata.get("content_type") != "audio/wav"
            or metadata.get("duration_seconds") != 4.0
            or metadata.get("sample_rate") != 24000
            or metadata.get("channels") != 1
        ):
            raise ValueError("audio asset integrity check failed")
        return {**metadata, "path": str(path)}
