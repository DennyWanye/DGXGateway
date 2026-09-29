#!/usr/bin/env python3
"""Small, dependency-free oracle for MiniMax H3 pruned-curve checkpoints."""

from __future__ import annotations

from collections.abc import Iterable, Sequence


REQUIRED_CURVE_KEYS = {"adaln_t_table", "adaln_basis", "adaln_mean"}
DENSE_TIME_MARKERS = ("time_embedder.", "time_embedding.")


def interpolate_curve(table: Sequence[Sequence[float]], timestep: float) -> list[float]:
    if len(table) < 2 or not table[0]:
        raise ValueError("curve table must contain at least two non-empty rows")
    width = len(table[0])
    if any(len(row) != width for row in table):
        raise ValueError("curve table rows have inconsistent widths")
    normalized = min(1.0, max(0.0, float(timestep)))
    scaled = normalized * (len(table) - 1)
    lower = min(int(scaled), len(table) - 2)
    fraction = scaled - lower
    return [
        float(table[lower][column]) * (1.0 - fraction)
        + float(table[lower + 1][column]) * fraction
        for column in range(width)
    ]


def validate_curve_keys(keys: Iterable[str], *, projection_count: int) -> None:
    names = set(keys)
    missing = REQUIRED_CURVE_KEYS - names
    if missing:
        raise ValueError(f"curve keys are missing: {','.join(sorted(missing))}")
    dense = sorted(name for name in names if any(marker in name for marker in DENSE_TIME_MARKERS))
    if dense:
        raise ValueError("curve checkpoint contains dense time weights")
    if projection_count != 51:
        raise ValueError("curve checkpoint must contain 51 AdaLN projections")
