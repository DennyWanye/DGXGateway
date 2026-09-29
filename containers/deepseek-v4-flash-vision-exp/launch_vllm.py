#!/usr/bin/env python3
"""Fail-closed launcher for the DeepSeek V4 Vision vLLM server."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence


REQUIRED_MIDDLEWARE = "vision_media_guard.VisionMediaGuard"
REQUIRED_MEDIA_DOMAIN = "deepseek-v4-vision.invalid"
REQUIRED_MASTER_ADDR = "192.0.2.33"
REQUIRED_MASTER_PORT = "25001"
REQUIRED_HOST_IP = {"head": "192.0.2.33", "worker": "192.0.2.34"}


class LaunchContractError(ValueError):
    """The controller did not supply the required media-boundary contract."""


def _option_values(argv: Sequence[str], option: str, *, multi: bool = False) -> list[list[str]]:
    """Return every occurrence while preserving an argparse ``nargs='+'`` value."""

    occurrences: list[list[str]] = []
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == option:
            index += 1
            values: list[str] = []
            while index < len(argv) and not argv[index].startswith("--"):
                values.append(argv[index])
                index += 1
                if not multi:
                    break
            occurrences.append(values)
            continue
        if argument.startswith(option + "="):
            occurrences.append([argument.split("=", 1)[1]])
        index += 1
    return occurrences


def validate_args(argv: Sequence[str], environ: dict[str, str] | os._Environ[str]) -> None:
    """Require the controller to express the complete fail-closed media policy."""

    middleware = _option_values(argv, "--middleware")
    if middleware != [[REQUIRED_MIDDLEWARE]]:
        raise LaunchContractError(
            "exactly one --middleware vision_media_guard.VisionMediaGuard is required"
        )

    media_domains = _option_values(argv, "--allowed-media-domains", multi=True)
    if media_domains != [[REQUIRED_MEDIA_DOMAIN]]:
        raise LaunchContractError(
            "--allowed-media-domains must contain only the reserved .invalid domain"
        )

    if _option_values(argv, "--allowed-local-media-path", multi=True):
        raise LaunchContractError("--allowed-local-media-path is forbidden")

    if environ.get("VLLM_MEDIA_URL_ALLOW_REDIRECTS") != "0":
        raise LaunchContractError("VLLM_MEDIA_URL_ALLOW_REDIRECTS must equal 0")

    role = environ.get("VISION_ROLE")
    if role not in REQUIRED_HOST_IP:
        raise LaunchContractError("VISION_ROLE must be head or worker")
    if environ.get("VLLM_HOST_IP") != REQUIRED_HOST_IP[role]:
        raise LaunchContractError("VLLM_HOST_IP must use the role-specific fabric address")

    expected_options = {
        "--distributed-executor-backend": "mp",
        "--nnodes": "2",
        "--node-rank": "0" if role == "head" else "1",
        "--master-addr": REQUIRED_MASTER_ADDR,
        "--master-port": REQUIRED_MASTER_PORT,
        "--tensor-parallel-size": "2",
        "--pipeline-parallel-size": "1",
        "--gpu-memory-utilization": "0.85",
    }
    for option, expected in expected_options.items():
        if _option_values(argv, option) != [[expected]]:
            raise LaunchContractError(f"{option} must equal {expected}")

    is_headless = _option_values(argv, "--headless") == [[]]
    if is_headless != (role == "worker"):
        raise LaunchContractError("only the worker rank must use --headless")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    validate_args(arguments, os.environ)
    os.execvp("vllm", ["vllm", "serve", *arguments])
    raise AssertionError("os.execvp unexpectedly returned")


if __name__ == "__main__":
    raise SystemExit(main())
