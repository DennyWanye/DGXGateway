"""Small, content-addressed reference images; no network or model lifecycle access."""
from __future__ import annotations

import base64
import binascii
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_ENCODED = ((MAX_IMAGE_BYTES + 2) // 3) * 4
_ASSET_ID = re.compile(r"[0-9a-f]{64}\Z")


def _image_type(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    raise ValueError("reference must have PNG, JPEG or WebP image magic")


def _filename(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value.encode("utf-8")) > 255
            or value in {".", ".."} or any(c in value for c in "/\\")
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError("filename must be a plain filename without path components")
    return value


def _read_regular(path: Path, limit: int) -> bytes:
    """Never follow a replaced leaf symlink; bound both stat and actual reads."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError("asset contains a symbolic link") from exc
        raise
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("invalid asset file or size")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("asset exceeds size limit")
    return data


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


class ImageAssets:
    def __init__(self, root: Path | str):
        self.root = Path(root).absolute()
        if self.root.is_symlink():
            raise ValueError("asset root must not be a symbolic link")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)

    def register_base64(self, filename: str, image_base64: str) -> dict:
        filename = _filename(filename)
        if not isinstance(image_base64, str) or not image_base64 or len(image_base64) > _MAX_ENCODED:
            raise ValueError("image_base64 must contain at most 8 MiB of image data")
        try:
            data = base64.b64decode(image_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("image_base64 must be strict base64 without a data URI") from exc
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError("image exceeds 8 MiB")
        if base64.b64encode(data).decode("ascii") != image_base64:
            raise ValueError("image_base64 must use canonical base64 encoding")
        ext, content_type = _image_type(data)
        asset_id = hashlib.sha256(data).hexdigest()
        target = self.root / asset_id
        if target.exists() or target.is_symlink():
            return self.get(asset_id)
        generated = f"dgx-asset-{asset_id}.{ext}"
        metadata = {"asset_id": asset_id, "sha256": asset_id, "size_bytes": len(data),
                    "content_type": content_type, "filename": generated,
                    "comfy_filename": generated, "original_filename": filename,
                    "validation": "magic_and_sha256"}
        temporary = Path(tempfile.mkdtemp(prefix=".register-", dir=self.root))
        try:
            _write_private(temporary / generated, data)
            _write_private(temporary / "metadata.json", json.dumps(metadata, ensure_ascii=False).encode())
            fd = os.open(temporary, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.rename(temporary, target)
            except OSError as exc:
                if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise
                # A concurrent writer won; validate its complete object below.
            fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return self.get(asset_id)

    def get(self, asset_id: str) -> dict:
        if not isinstance(asset_id, str) or not _ASSET_ID.fullmatch(asset_id):
            raise ValueError("asset_id must be 64 lowercase hex characters")
        directory = self.root / asset_id
        if directory.is_symlink():
            raise ValueError("asset directory must not be a symbolic link")
        try:
            metadata = json.loads(_read_regular(directory / "metadata.json", 4096))
            if not isinstance(metadata, dict):
                raise ValueError("invalid asset metadata")
            _filename(metadata.get("original_filename"))
            name = _filename(metadata.get("filename"))
            if name not in {f"dgx-asset-{asset_id}.{ext}" for ext in ("png", "jpg", "webp")}:
                raise ValueError("invalid stored asset filename")
            path = directory / name
            data = _read_regular(path, MAX_IMAGE_BYTES)
            ext, content_type = _image_type(data)
            if (hashlib.sha256(data).hexdigest() != asset_id
                    or metadata.get("asset_id") != asset_id or metadata.get("sha256") != asset_id
                    or metadata.get("size_bytes") != len(data)
                    or metadata.get("content_type") != content_type
                    or metadata.get("comfy_filename") != name
                    or name != f"dgx-asset-{asset_id}.{ext}"):
                raise ValueError("asset integrity check failed")
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid asset metadata") from exc
        return {**metadata, "path": str(path)}
