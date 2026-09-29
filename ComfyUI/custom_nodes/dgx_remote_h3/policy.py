from __future__ import annotations

import json
from dataclasses import dataclass


MAX_PROMPT_BYTES = 2048
MAX_SEED = 2_147_483_647
RESOLUTIONS = {
    "768x448": (768, 448),
    "1344x768": (1344, 768),
}


class InputError(ValueError):
    """A safe, user-actionable input validation error."""


@dataclass(frozen=True)
class H3Request:
    prompt: str
    width: int
    height: int
    duration: int
    seed: int


def validate_inputs(prompt: str, resolution: str, duration: int, seed: int) -> H3Request:
    if not isinstance(prompt, str) or not prompt.strip():
        raise InputError("Prompt must not be blank.")
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise InputError(f"Prompt must be at most {MAX_PROMPT_BYTES} UTF-8 bytes.")
    if resolution not in RESOLUTIONS:
        raise InputError("Resolution must be 768x448 or 1344x768.")
    if isinstance(duration, bool) or not isinstance(duration, int) or not 2 <= duration <= 4:
        raise InputError("Duration must be an integer from 2 to 4 seconds.")
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= MAX_SEED:
        raise InputError(f"Seed must be an integer from 0 to {MAX_SEED}.")
    width, height = RESOLUTIONS[resolution]
    return H3Request(prompt=prompt, width=width, height=height, duration=duration, seed=seed)


def build_gateway_fields(request: H3Request, *, required_profile: str = "heretic-hmnsfw-v1") -> dict[str, str]:
    return _build_fields(request, task="t2va", required_profile=required_profile)


def validate_fl2va_inputs(prompt: str, resolution: str, duration: int, seed: int) -> H3Request:
    return validate_inputs(prompt, resolution, duration, seed)


def build_fl2va_gateway_fields(
    request: H3Request, *, required_profile: str = "10eros-fl2va-v1"
) -> dict[str, str]:
    return _build_fields(request, task="fl2va", required_profile=required_profile)


def validate_ref2va_inputs(prompt: str, seed: int) -> H3Request:
    return validate_inputs(prompt, "1344x768", 4, seed)


def build_ref2va_gateway_fields(
    request: H3Request, *, required_profile: str = "heretic-ref2va-v1"
) -> dict[str, str]:
    valid_duration = (4 <= request.duration <= 15) if required_profile == "heretic-ref2va-hmnsfw-v1" else request.duration == 4
    if (request.width, request.height) != (1344, 768) or not valid_duration:
        raise InputError("Ref2VA size/duration is outside this profile's supported range.")
    return _build_fields(request, task="ref2va", required_profile=required_profile)


def _build_fields(request: H3Request, *, task: str, required_profile: str) -> dict[str, str]:
    if required_profile not in {
        "heretic-hmnsfw-v1",
        "heretic-ref2va-v1",
        "heretic-ref2va-hmnsfw-v1",
        "10eros-fl2va-v1",
    }:
        raise InputError("Unknown MiniMax H3 profile.")
    inference_steps = 8 if required_profile == "10eros-fl2va-v1" else 20
    return {
        "prompt": request.prompt,
        "width": str(request.width),
        "height": str(request.height),
        "fps": "24",
        "num_inference_steps": str(inference_steps),
        "flow_shift": "12.0",
        "seed": str(request.seed),
        "extra_params": json.dumps(
            {
                "task": task,
                "duration": float(request.duration),
                "audio_flow_shift": 3.0,
                "required_profile": required_profile,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }


def validate_ref2va_duration_inputs(prompt, seed, duration, fps=24):
    from dataclasses import replace
    base = validate_ref2va_inputs(prompt, seed)
    if type(duration) is not int or not 4 <= duration <= 15:
        raise InputError("视频时长需要是4到15之间的整数秒。")
    if type(fps) is not int or fps != 24:
        raise InputError("当前H3原生帧率固定为24 FPS，不支持30/60 FPS。")
    return replace(base, duration=duration)
