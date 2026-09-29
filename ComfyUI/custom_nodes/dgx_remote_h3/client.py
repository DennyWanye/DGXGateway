from __future__ import annotations

import http.client
import os
import pathlib
import socket
import stat
import time
import threading
import uuid
from collections.abc import Mapping

from .contract import RuntimeContract
from .identity import decode_and_validate


FIELD_ORDER = (
    "prompt",
    "width",
    "height",
    "fps",
    "num_inference_steps",
    "flow_shift",
    "seed",
    "extra_params",
)


class GatewayError(RuntimeError):
    """A sanitized H3 request/response error."""


def _encode_multipart(
    fields: Mapping[str, str],
    *,
    reference_path: pathlib.Path | str | None = None,
    reference_mime: str | None = None,
    reference_audio_path: pathlib.Path | str | None = None,
    reference_audio_mime: str | None = None,
) -> tuple[bytes, str]:
    if tuple(fields) != FIELD_ORDER or any(not isinstance(value, str) for value in fields.values()):
        raise GatewayError("H3 request fields do not match the pinned policy.")
    boundary = f"dgx-h3-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name in FIELD_ORDER:
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{name}"\r\n'.encode("ascii"),
                b"Content-Type: text/plain\r\n\r\n",
                fields[name].encode("utf-8"),
                b"\r\n",
            ]
        )
    if reference_path is not None:
        path = pathlib.Path(reference_path)
        if reference_mime not in {"image/png", "image/jpeg"}:
            raise GatewayError("H3 reference image staging is invalid.")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            try:
                metadata = os.fstat(descriptor)
                size = metadata.st_size
                if not stat.S_ISREG(metadata.st_mode) or not (1 <= size <= 31_457_280):
                    raise GatewayError("H3 reference image size is invalid.")
                image_chunks = []
                remaining = size
                while remaining:
                    chunk = os.read(descriptor, min(1024 * 1024, remaining))
                    if not chunk:
                        raise GatewayError("H3 reference image staging changed during encoding.")
                    image_chunks.append(chunk)
                    remaining -= len(chunk)
                image = b"".join(image_chunks)
            finally:
                os.close(descriptor)
        except GatewayError:
            raise
        except OSError as exc:
            raise GatewayError("H3 reference image staging is invalid.") from exc
        filename = "reference.png" if reference_mime == "image/png" else "reference.jpg"
        chunks.extend([
            f"--{boundary}\r\n".encode("ascii"),
            f'Content-Disposition: form-data; name="reference_image"; filename="{filename}"\r\n'.encode("ascii"),
            f"Content-Type: {reference_mime}\r\n\r\n".encode("ascii"),
            image,
            b"\r\n",
        ])
    elif reference_mime is not None:
        raise GatewayError("H3 reference image staging is invalid.")
    if reference_audio_path is not None:
        path = pathlib.Path(reference_audio_path)
        if reference_audio_mime != "audio/wav":
            raise GatewayError("H3 reference audio staging is invalid.")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            try:
                metadata = os.fstat(descriptor)
                size = metadata.st_size
                if not stat.S_ISREG(metadata.st_mode) or not (44 <= size <= 262_144):
                    raise GatewayError("H3 reference audio size is invalid.")
                audio = os.read(descriptor, size)
                if len(audio) != size:
                    raise GatewayError("H3 reference audio staging changed during encoding.")
            finally:
                os.close(descriptor)
        except GatewayError:
            raise
        except OSError as exc:
            raise GatewayError("H3 reference audio staging is invalid.") from exc
        chunks.extend([
            f"--{boundary}\r\n".encode("ascii"),
            b'Content-Disposition: form-data; name="reference_audio"; filename="reference.wav"\r\n',
            b"Content-Type: audio/wav\r\n\r\n",
            audio,
            b"\r\n",
        ])
    elif reference_audio_mime is not None:
        raise GatewayError("H3 reference audio staging is invalid.")
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), boundary


class H3GatewayClient:
    def __init__(self, contract: RuntimeContract, interrupt_check=None):
        self._contract = contract
        self._interrupt_check = interrupt_check

    def generate(
        self,
        fields: Mapping[str, str],
        token: str,
        staging_path: pathlib.Path | str,
        *,
        reference_path: pathlib.Path | str | None = None,
        reference_mime: str | None = None,
        reference_audio_path: pathlib.Path | str | None = None,
        reference_audio_mime: str | None = None,
    ) -> int:
        if not isinstance(token, str) or not token:
            raise GatewayError("H3 gateway token is unavailable.")
        body, boundary = _encode_multipart(
            fields,
            reference_path=reference_path,
            reference_mime=reference_mime,
            reference_audio_path=reference_audio_path,
            reference_audio_mime=reference_audio_mime,
        )
        try:
            required_profile = __import__("json").loads(fields["extra_params"])["required_profile"]
        except Exception as exc:
            raise GatewayError("H3 request profile identity is missing.") from exc
        deadline = None if self._contract.total_timeout_seconds is None else time.monotonic() + self._contract.total_timeout_seconds
        request_id = uuid.uuid4().hex
        finished = threading.Event()
        interrupted = []
        def watch_interrupt():
            while not finished.wait(0.5):
                if not interrupted:
                    try:
                        self._interrupt_check()
                    except BaseException as exc:
                        interrupted.append(exc)
                if interrupted:
                    cancel = http.client.HTTPConnection(self._contract.endpoint_host, self._contract.endpoint_port, timeout=5)
                    try:
                        cancel.request("POST", "/v1/videos/cancel", body=b"", headers={"Authorization": f"Bearer {token}", "X-H3-Request-ID": request_id})
                        if cancel.getresponse().status == 200:
                            return
                    except (OSError, http.client.HTTPException):
                        pass
                    finally:
                        cancel.close()
        watcher = None
        connection = http.client.HTTPConnection(
            self._contract.endpoint_host,
            self._contract.endpoint_port,
            timeout=self._contract.connect_timeout_seconds,
        )
        try:
            connection.request(
                "POST",
                self._contract.endpoint_path,
                body=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Content-Length": str(len(body)),
                    "Accept": "video/mp4",
                    "User-Agent": "dgx-remote-h3-comfyui/1",
                    "X-H3-Request-ID": request_id,
                },
            )
            self._set_remaining_timeout(connection, deadline)
            if self._interrupt_check is not None:
                watcher = threading.Thread(target=watch_interrupt, daemon=True)
                watcher.start()
            response = connection.getresponse()
            if interrupted:
                raise interrupted[0]
            self._validate_response_headers(response, required_profile=required_profile)
            total = self._stream_response(response, staging_path, connection, deadline)
            if interrupted:
                raise interrupted[0]
            return total
        except GatewayError:
            raise
        except (socket.timeout, TimeoutError) as exc:
            raise GatewayError("H3 request timed out.") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise GatewayError("H3 gateway is unavailable.") from exc
        finally:
            finished.set()
            if watcher is not None:
                watcher.join(timeout=6)
            connection.close()

    @staticmethod
    def _set_remaining_timeout(connection: http.client.HTTPConnection, deadline: float | None) -> None:
        if deadline is None:
            if connection.sock is not None:
                connection.sock.settimeout(None)
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GatewayError("H3 request timed out.")
        if connection.sock is not None:
            connection.sock.settimeout(remaining)

    def _validate_response_headers(self, response: http.client.HTTPResponse, *, required_profile: str) -> None:
        if response.status in (401, 403):
            raise GatewayError("H3 authentication failed.")
        if response.status == 503:
            raise GatewayError("H3 is busy or not ready.")
        if response.status != 200:
            raise GatewayError(f"H3 gateway returned HTTP {response.status}.")
        content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "video/mp4":
            raise GatewayError("H3 gateway did not return an MP4.")
        try:
            decode_and_validate(response.getheader("X-H3-Identity", ""), required_profile=required_profile)
        except ValueError as exc:
            raise GatewayError("H3 gateway returned a mismatched model identity.") from exc
        content_length = response.getheader("Content-Length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError as exc:
                raise GatewayError("H3 gateway returned invalid response metadata.") from exc
            if declared < 1:
                raise GatewayError("H3 gateway returned an empty MP4.")
            if declared > self._contract.max_response_bytes:
                raise GatewayError("H3 gateway response is too large.")

    def _stream_response(
        self,
        response: http.client.HTTPResponse,
        staging_path: pathlib.Path | str,
        connection: http.client.HTTPConnection,
        deadline: float | None,
    ) -> int:
        flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(staging_path, flags)
        except OSError as exc:
            raise GatewayError("Local video staging file is unavailable.") from exc
        total = 0
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise GatewayError("Local video staging file is invalid.")
            while True:
                self._set_remaining_timeout(connection, deadline)
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > self._contract.max_response_bytes:
                    raise GatewayError("H3 gateway response is too large.")
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise GatewayError("Local video staging write failed.")
                    view = view[written:]
            if total < 1:
                raise GatewayError("H3 gateway returned an empty MP4.")
            os.fsync(descriptor)
            return total
        finally:
            os.close(descriptor)
