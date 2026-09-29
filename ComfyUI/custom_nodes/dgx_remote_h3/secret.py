from __future__ import annotations

import os
import pathlib
import stat


SECRET_PATH = pathlib.Path("/run/dgx-h3-secret/token")
MAX_TOKEN_BYTES = 4096


class SecretError(RuntimeError):
    """A sanitized gateway-token loading error."""


def read_gateway_token(path: pathlib.Path | str = SECRET_PATH) -> str:
    secret_path = pathlib.Path(path)
    try:
        metadata = secret_path.lstat()
    except FileNotFoundError as exc:
        raise SecretError("H3 gateway token is missing.") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SecretError("H3 gateway token must be a regular file.")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(secret_path, flags)
    except OSError as exc:
        raise SecretError("H3 gateway token could not be opened safely.") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise SecretError("H3 gateway token must be a regular file.")
        if stat.S_IMODE(opened.st_mode) != 0o400:
            raise SecretError("H3 gateway token must have mode 0400.")
        if opened.st_uid != os.geteuid():
            raise SecretError("H3 gateway token must be owned by the runtime user.")
        value = os.read(descriptor, MAX_TOKEN_BYTES + 1)
    finally:
        os.close(descriptor)

    if not value or len(value) > MAX_TOKEN_BYTES:
        raise SecretError("H3 gateway token has an invalid length.")
    if b"\x00" in value:
        raise SecretError("H3 gateway token has an invalid format.")
    if value.endswith(b"\n"):
        value = value[:-1]
    if not value or b"\n" in value or b"\r" in value:
        raise SecretError("H3 gateway token has an invalid format.")
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecretError("H3 gateway token has an invalid encoding.") from exc
