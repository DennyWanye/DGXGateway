from __future__ import annotations

import pathlib
import shutil
from collections.abc import Callable
from typing import Any

from .client import H3GatewayClient
from .contract import RuntimeContract
from .media import stage_reference_audio, stage_reference_image, validate_h3_mp4
from .policy import H3Request, build_fl2va_gateway_fields, build_gateway_fields, build_ref2va_gateway_fields
from .secret import read_gateway_token
from .storage import MINIMUM_FREE_BYTES, create_staging, publish_unique


def execute_request(
    request: H3Request,
    *,
    output_directory: pathlib.Path | str,
    contract: RuntimeContract,
    token_reader: Callable[[pathlib.Path | str], str] = read_gateway_token,
    client_factory: Callable[[RuntimeContract], Any] = H3GatewayClient,
    media_validator: Callable[..., Any] = validate_h3_mp4,
    minimum_free_bytes: int = MINIMUM_FREE_BYTES,
    required_profile: str = "heretic-hmnsfw-v1",
) -> pathlib.Path:
    token = token_reader(contract.secret_path)
    staging = create_staging(output_directory, minimum_free_bytes=minimum_free_bytes)
    try:
        client_factory(contract).generate(
            build_gateway_fields(request, required_profile=required_profile), token, staging
        )
        media_validator(
            staging,
            width=request.width,
            height=request.height,
            requested_duration=request.duration,
        )
        return publish_unique(staging, output_directory)
    except BaseException:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def execute_ref2va_request(
    request: H3Request,
    reference_image: object,
    reference_audio: object,
    *,
    output_directory: pathlib.Path | str,
    contract: RuntimeContract,
    token_reader: Callable[[pathlib.Path | str], str] = read_gateway_token,
    client_factory: Callable[[RuntimeContract], Any] = H3GatewayClient,
    media_validator: Callable[..., Any] = validate_h3_mp4,
    image_stager: Callable[..., pathlib.Path] = stage_reference_image,
    audio_stager: Callable[..., pathlib.Path] = stage_reference_audio,
    minimum_free_bytes: int = MINIMUM_FREE_BYTES,
    required_profile: str = "heretic-ref2va-v1",
) -> pathlib.Path:
    token = token_reader(contract.secret_path)
    staging = create_staging(output_directory, minimum_free_bytes=minimum_free_bytes)
    reference_path: pathlib.Path | None = None
    reference_audio_path: pathlib.Path | None = None
    try:
        reference_path = image_stager(reference_image, output_directory)
        reference_audio_path = audio_stager(reference_audio, output_directory)
        client_factory(contract).generate(
            build_ref2va_gateway_fields(request, required_profile=required_profile),
            token,
            staging,
            reference_path=reference_path,
            reference_mime="image/png",
            reference_audio_path=reference_audio_path,
            reference_audio_mime="audio/wav",
        )
        media_validator(
            staging,
            width=request.width,
            height=request.height,
            requested_duration=request.duration,
        )
        return publish_unique(staging, output_directory)
    except BaseException:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        if reference_path is not None:
            shutil.rmtree(reference_path.parent, ignore_errors=True)
        if reference_audio_path is not None:
            shutil.rmtree(reference_audio_path.parent, ignore_errors=True)


def execute_fl2va_request(
    request: H3Request,
    first_frame: object,
    *,
    output_directory: pathlib.Path | str,
    contract: RuntimeContract,
    required_profile: str = "10eros-fl2va-v1",
    token_reader: Callable[[pathlib.Path | str], str] = read_gateway_token,
    client_factory: Callable[[RuntimeContract], Any] = H3GatewayClient,
    media_validator: Callable[..., Any] = validate_h3_mp4,
    image_stager: Callable[..., pathlib.Path] = stage_reference_image,
    minimum_free_bytes: int = MINIMUM_FREE_BYTES,
) -> pathlib.Path:
    token = token_reader(contract.secret_path)
    staging = create_staging(output_directory, minimum_free_bytes=minimum_free_bytes)
    reference_path: pathlib.Path | None = None
    try:
        reference_path = image_stager(first_frame, output_directory)
        client_factory(contract).generate(
            build_fl2va_gateway_fields(request, required_profile=required_profile),
            token,
            staging,
            reference_path=reference_path,
            reference_mime="image/png",
        )
        media_validator(
            staging,
            width=request.width,
            height=request.height,
            requested_duration=request.duration,
        )
        return publish_unique(staging, output_directory)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
    finally:
        if reference_path is not None:
            shutil.rmtree(reference_path.parent, ignore_errors=True)
