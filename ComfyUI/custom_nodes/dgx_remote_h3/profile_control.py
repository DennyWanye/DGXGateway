from __future__ import annotations

import http.client
import json
import os
import pathlib
import stat


CONTROL_HOST = "host.docker.internal"
CONTROL_PORT = 19082
CONTROL_TOKEN_PATH = pathlib.Path("/run/dgx-h3-secret/control-token")
PUBLIC_PROFILES = (
    "heretic-ref2va-v1",
    "heretic-ref2va-hmnsfw-v1",
    "10eros-fl2va-v1",
)


class ProfileControlError(RuntimeError):
    pass


def load_control_token(path: pathlib.Path | str = CONTROL_TOKEN_PATH) -> str:
    path = pathlib.Path(path)
    if path.is_symlink():
        raise ProfileControlError("H3 profile control token is unsafe.")
    try:
        metadata = path.stat()
        raw = path.read_bytes()
    except OSError as exc:
        raise ProfileControlError("H3 profile control is unavailable.") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}:
        raise ProfileControlError("H3 profile control token is unsafe.")
    value = raw.rstrip(b"\r\n")
    if len(value) < 16 or b"\n" in value or b"\r" in value:
        raise ProfileControlError("H3 profile control token is invalid.")
    return value.decode("utf-8", "strict")


def profile_action(action: str, profile: str) -> dict:
    if action not in {"status", "switch", "stop"} or profile not in PUBLIC_PROFILES:
        raise ProfileControlError("Unsupported H3 profile operation.")
    token = load_control_token()
    connection = http.client.HTTPConnection(CONTROL_HOST, CONTROL_PORT, timeout=1800)
    try:
        if action == "status":
            connection.request("GET", "/status", headers={"Authorization": f"Bearer {token}"})
        else:
            body = json.dumps({"profile": profile}, separators=(",", ":")).encode()
            connection.request(
                "POST",
                f"/{action}",
                body=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                },
            )
        response = connection.getresponse()
        payload = response.read(64 * 1024)
        if response.status == 409:
            raise ProfileControlError("H3 is BUSY; wait for the current generation and click again.")
        if response.status != 200:
            raise ProfileControlError(f"H3 profile operation failed (HTTP {response.status}).")
        document = json.loads(payload)
        if not isinstance(document, dict):
            raise ValueError
        return document
    except ProfileControlError:
        raise
    except Exception as exc:
        raise ProfileControlError("H3 profile controller is unavailable.") from exc
    finally:
        connection.close()
