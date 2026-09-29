from __future__ import annotations

import os
import pathlib
import shutil
import stat
import time
import uuid
from collections.abc import Callable
import fcntl


MINIMUM_FREE_BYTES = 512 * 1024 * 1024


class StorageError(RuntimeError):
    """A sanitized local-output error."""


def cleanup_stale_staging(
    output_directory: pathlib.Path | str,
    *,
    now: float | None = None,
    maximum_age_seconds: int = 24 * 60 * 60,
) -> int:
    output = pathlib.Path(output_directory).resolve()
    if not output.is_dir():
        raise StorageError("ComfyUI output directory is unavailable.")
    current_time = time.time() if now is None else now
    lock_path = output / ".dgx-h3-cleanup.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    removed = 0
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        for candidate in output.iterdir():
            name = candidate.name
            if not (name.startswith(".dgx-h3-") and name.endswith(".part")):
                continue
            try:
                metadata = candidate.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(metadata.st_mode):
                continue
            if current_time - metadata.st_mtime <= maximum_age_seconds:
                continue
            try:
                candidate.unlink()
            except FileNotFoundError:
                continue
            removed += 1
        if removed:
            directory_fd = os.open(output, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return removed
    finally:
        os.close(descriptor)


def create_staging(
    output_directory: pathlib.Path | str,
    *,
    minimum_free_bytes: int = MINIMUM_FREE_BYTES,
) -> pathlib.Path:
    output = pathlib.Path(output_directory).resolve()
    if not output.is_dir():
        raise StorageError("ComfyUI output directory is unavailable.")
    if shutil.disk_usage(output).free < minimum_free_bytes:
        raise StorageError("ComfyUI output directory has insufficient free space.")
    path = output / f".dgx-h3-{uuid.uuid4().hex}.part"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    os.close(descriptor)
    return path


def _default_final_name() -> str:
    return f"dgx-h3-{uuid.uuid4().hex}.mp4"


def publish_unique(
    staging_path: pathlib.Path | str,
    output_directory: pathlib.Path | str,
    *,
    name_factory: Callable[[], str] = _default_final_name,
    maximum_attempts: int = 32,
) -> pathlib.Path:
    staging = pathlib.Path(staging_path)
    output = pathlib.Path(output_directory).resolve()
    try:
        staging_parent = staging.resolve(strict=True).parent
        staging_metadata = staging.stat(follow_symlinks=False)
    except (FileNotFoundError, OSError) as exc:
        raise StorageError("Staging video is unavailable.") from exc
    if staging_parent != output or not stat.S_ISREG(staging_metadata.st_mode):
        raise StorageError("Staging video must be a regular file in the output directory.")

    with staging.open("rb") as stream:
        os.fsync(stream.fileno())

    for _ in range(maximum_attempts):
        name = name_factory()
        if pathlib.PurePath(name).name != name or not name.endswith(".mp4"):
            raise StorageError("Generated output filename is invalid.")
        destination = output / name
        try:
            os.link(staging, destination, follow_symlinks=False)
        except FileExistsError:
            continue
        except OSError as exc:
            raise StorageError("Video could not be published safely.") from exc
        try:
            staging.unlink()
            directory_fd = os.open(output, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise StorageError("Video publication could not be finalized.") from exc
        return destination
    raise StorageError("A unique output filename could not be allocated.")
