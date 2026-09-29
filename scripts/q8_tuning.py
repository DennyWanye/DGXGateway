#!/usr/bin/env python3
"""Pure helpers for reproducible Q8 inference tuning."""

from statistics import median
from typing import Any


MODEL = "qwen38-q8-256k:latest"


def build_request() -> dict[str, Any]:
    prefix = "Python performance reference: " + " filler" * 10_000
    task = (
        "\nWrite a complete Python implementation of an asynchronous bounded "
        "worker pool with type hints, cancellation, retries, docstrings, and "
        "an executable example. Output code only and continue until the output "
        "budget is exhausted."
    )
    return {
        "model": MODEL,
        "input": prefix + task,
        "reasoning": {"effort": "none"},
        "temperature": 0,
        "max_output_tokens": 256,
    }


def _output_text(response: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in response.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                parts.append(content.get("text", ""))
    return "".join(parts)


def normalize_run(
    response: dict[str, Any],
    elapsed_seconds: float,
    timings_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    usage = response.get("usage", {})
    timings = timings_override if timings_override is not None else response.get("timings", {})
    input_tokens = int(usage.get("input_tokens", 0))
    output_tokens = int(usage.get("output_tokens", 0))
    prompt_tps = float(timings.get("prompt_per_second", 0.0))
    generation_tps = float(timings.get("predicted_per_second", 0.0))
    text = _output_text(response)
    valid = (
        response.get("status") == "completed"
        and bool(text.strip())
        and input_tokens > 9_000
        and output_tokens >= 240
        and prompt_tps > 0
        and generation_tps > 0
    )
    return {
        "valid": valid,
        "status": response.get("status"),
        "elapsed_seconds": round(elapsed_seconds, 3),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "prompt_eval_tokens": int(timings.get("prompt_n", input_tokens)),
        "prompt_tps": round(prompt_tps, 3),
        "generation_tps": round(generation_tps, 3),
        "draft_generated": int(timings.get("draft_n", 0)),
        "draft_accepted": int(timings.get("draft_n_accepted", 0)),
        "output_characters": len(text),
    }


def aggregate_runs(name: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
    valid_runs = [run for run in runs if run.get("valid")]
    measured = valid_runs or runs
    full_prompt_runs = [
        run for run in valid_runs if int(run.get("prompt_eval_tokens", 0)) > 9_000
    ]
    prompt_runs = full_prompt_runs or measured
    result = {
        "name": name,
        "valid": len(valid_runs) == len(runs) and len(runs) >= 3,
        "valid_runs": len(valid_runs),
        "total_runs": len(runs),
        "generation_tps": round(median(float(run["generation_tps"]) for run in measured), 3),
        "prompt_tps": round(median(float(run["prompt_tps"]) for run in prompt_runs), 3),
        "draft_accepted": sum(int(run.get("draft_accepted", 0)) for run in valid_runs),
        "draft_generated": sum(int(run.get("draft_generated", 0)) for run in valid_runs),
    }
    return result


def choose_winner(
    baseline: dict[str, Any],
    candidates: list[dict[str, Any]],
    minimum_gain: float = 0.05,
    maximum_prompt_regression: float = 0.10,
) -> dict[str, Any]:
    minimum_generation = float(baseline["generation_tps"]) * (1 + minimum_gain)
    minimum_prompt = float(baseline["prompt_tps"]) * (1 - maximum_prompt_regression)
    qualifying = [
        candidate
        for candidate in candidates
        if candidate.get("valid")
        and float(candidate["generation_tps"]) >= minimum_generation
        and float(candidate["prompt_tps"]) >= minimum_prompt
    ]
    return max(qualifying, key=lambda item: float(item["generation_tps"]), default=baseline)


def nearest_cutoff(value: float, choices: list[float]) -> float:
    alternatives = [choice for choice in choices if choice != value]
    if not alternatives:
        raise ValueError("at least two distinct cutoff choices are required")
    return min(alternatives, key=lambda choice: (round(abs(choice - value), 10), choice))


def selection_aggregate(candidate: dict[str, Any]) -> dict[str, Any]:
    view = dict(candidate["aggregate"])
    if "ngram" in str(candidate.get("settings", {}).get("spec_types", "")):
        view["generation_tps"] = float(candidate["runs"][0]["generation_tps"])
        view["selection_basis"] = "cold_first_run"
    else:
        view["selection_basis"] = "median_three_runs"
    return view
