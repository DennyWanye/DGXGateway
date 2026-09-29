#!/usr/bin/env python3
"""Patch the 10Eros image to use ComfyUI's exact ``simple`` schedule."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


PREIMAGE_SHA256 = "9bf880418209cc91e7131bd44595d140db5e9b213f0c3585da4b41e25b4691fc"


def comfy_simple_sigmas(num_steps: int, shift_scale: float) -> list[float]:
    """Return the 1000-bin schedule selected by ComfyUI ``simple``."""
    if num_steps <= 0 or shift_scale <= 0:
        raise ValueError("num_steps and shift_scale must be positive")
    values = []
    for index in range(num_steps):
        timestep = (1000 - int(index * 1000 / num_steps)) / 1000
        values.append(shift_scale * timestep / (1 + (shift_scale - 1) * timestep))
    values.append(0.0)
    return values


def replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise RuntimeError(f"schedule patch preimage count drifted: {source.count(old)}")
    return source.replace(old, new, 1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    raw = args.path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != PREIMAGE_SHA256:
        raise RuntimeError("vLLM MiniMax H3 schedule preimage drifted")
    source = raw.decode("utf-8")
    source = replace_once(
        source,
        """    # The rectified-flow sigma range is fixed at [1.0, 0.0].
    base = torch.linspace(
        1.0,
        0.0,
        int(num_steps),
        device=\"cpu\",
        dtype=torch.float32,
    )
    shifted = float(shift_scale) * base / (1 + (float(shift_scale) - 1) * base)
    shifted, _ = torch.unique_consecutive(shifted, return_counts=True)
    # A one-point request is still exactly one point.  Normal serving uses
    # multiple points, but preserving the requested cardinality keeps
    # ``num_inference_steps`` the sole schedule-size control.
    if num_steps > 1 and shifted[-1].item() > 0.0:
        shifted = torch.cat([shifted, torch.tensor([0.0], dtype=shifted.dtype)])
    return [float(value) for value in shifted.tolist()]
""",
        """    # Match ComfyUI BasicScheduler(simple): select ``num_steps``
    # entries from its 1000-bin shifted table, then append terminal zero.
    indices = torch.arange(int(num_steps), device=\"cpu\", dtype=torch.int64)
    base = (1000 - torch.floor(indices * 1000 / int(num_steps))) / 1000
    base = base.to(torch.float32)
    shifted = float(shift_scale) * base / (1 + (float(shift_scale) - 1) * base)
    shifted = torch.cat([shifted, torch.tensor([0.0], dtype=shifted.dtype)])
    return [float(value) for value in shifted.tolist()]
""",
    )
    args.path.write_text(source, encoding="utf-8")
    print(hashlib.sha256(source.encode()).hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
