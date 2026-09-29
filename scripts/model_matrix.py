#!/usr/bin/env python3
"""Frozen model/artifact manifest and pure benchmark result helpers."""

import argparse
from dataclasses import asdict, dataclass
import json
from statistics import median
from typing import Optional


QWEN38_REPO = "HauhauCS/Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-MTP-GGUF"
QWEN38_REV = "993a5971fda8f30dd1b7eb2654792ba4415c7460"
QWEN36_REPO = "HauhauCS/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive"
QWEN36_REV = "f12a584fecbeb5f20001130d8ecd66c9327ae685"
GEMMA4_REPO = "HauhauCS/Gemma4-26B-A4B-QAT-Uncensored-HauhauCS-Balanced-MTP"
GEMMA4_REV = "f9093662a2e7ae0503f637088bc96f77a1a70c83"


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    repo: str
    revision: str
    filename: str
    size: int
    sha256: str
    location: str
    preexisting: bool = False

    @property
    def origin_url(self) -> str:
        return f"https://huggingface.co/{self.repo}/resolve/{self.revision}/{self.filename}"


@dataclass(frozen=True)
class ModelProfile:
    profile_id: str
    artifact_id: str
    alias: str
    runtime: str
    draft_id: Optional[str]
    server_args: tuple[str, ...]


def artifacts() -> dict[str, Artifact]:
    rows = [
        Artifact("qwen38-q6", QWEN38_REPO, QWEN38_REV,
                 "Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-Q6_K_P.gguf",
                 25_924_151_424, "70c8139f4dcd05d3818bc9843cde7301faa44f66321e5966965929baa9c24a6a", "matrix"),
        Artifact("qwen38-q5", QWEN38_REPO, QWEN38_REV,
                 "Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-Q5_K_P.gguf",
                 20_218_177_664, "a21e22af885bd2f7c430a72b550a78f5445966e6a72d3143b0950baa0bd6d411", "matrix"),
        Artifact("qwen38-q4", QWEN38_REPO, QWEN38_REV,
                 "Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-Q4_K_P.gguf",
                 17_923_393_664, "ba36dc3c2b2ff5e0aa5d71092a8894546996a6a119ae391803dda07cdc08516d", "qwen38", True),
        Artifact("qwen38-fastmtp", QWEN38_REPO, QWEN38_REV,
                 "Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-FastMTP-32K.gguf",
                 903_453_952, "115e618e1f73cb50817ed5856f0551c6bf9c3d94df96f440eaca78dc63b8968b", "qwen38", True),
        Artifact("qwen36-a3b-q4", QWEN36_REPO, QWEN36_REV,
                 "Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-Q4_K_P.gguf",
                 23_424_536_704, "8d344a4336d8ea7da0cbfc12792d1471e568be7abe8930c52260698bfd01d731", "matrix"),
        Artifact("gemma4-a4b-q4", GEMMA4_REPO, GEMMA4_REV,
                 "Gemma4-26B-A4B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M.gguf",
                 16_796_015_520, "3c13133469e431312fffb8b1d9c85ae42199e6bb5746ea1da84e8ddf2097d73c", "matrix"),
        Artifact("gemma4-mtp", GEMMA4_REPO, GEMMA4_REV,
                 "mtp-gemma-4-26B-A4B-it.gguf", 251_937_728,
                 "62bd3af7f66c9308de9a5454233852f8c7324c93767e8dfb824ed45b9179864a", "matrix"),
    ]
    return {row.artifact_id: row for row in rows}


def _common(alias: str) -> tuple[str, ...]:
    return (
        "--alias", alias, "--ctx-size", "204800", "--parallel", "1",
        "--batch-size", "2048", "--n-gpu-layers", "all", "--split-mode", "none",
        "--flash-attn", "on", "--no-mmap", "--no-cache-prompt", "--jinja",
        "--host", "127.0.0.1", "--port", "18082",
    )


def profiles() -> dict[str, ModelProfile]:
    qwen38_sampler = (
        "--ubatch-size", "512", "--spec-type", "draft-mtp",
        "--spec-draft-n-max", "3", "--spec-draft-p-min", "0",
        "--temp", "1.0", "--top-k", "20", "--top-p", "0.95", "--min-p", "0",
        "--presence-penalty", "0", "--repeat-penalty", "1.0",
        "--reasoning", "on", "--reasoning-effort", "low",
        "--reasoning-preserve", "--reasoning-format", "deepseek",
    )
    qwen36_sampler = (
        "--ubatch-size", "512", "--temp", "0.6", "--top-k", "20",
        "--top-p", "0.95", "--min-p", "0", "--presence-penalty", "0",
        "--repeat-penalty", "1.0", "--reasoning", "on", "--reasoning-effort", "low",
        "--reasoning-budget", "64",
        "--reasoning-preserve", "--reasoning-format", "deepseek",
    )
    gemma_sampler = (
        "--ubatch-size", "512", "--spec-type", "draft-mtp",
        "--temp", "0.6", "--top-k", "64", "--top-p", "0.9", "--min-p", "0.05",
        "--repeat-penalty", "1.1", "--reasoning-budget", "64",
    )
    rows = []
    for artifact_id in ("qwen38-q6", "qwen38-q5", "qwen38-q4"):
        rows.append(ModelProfile(artifact_id, artifact_id, f"{artifact_id}-200k", "fastmtp",
                                 "qwen38-fastmtp", _common(f"{artifact_id}-200k") + qwen38_sampler))
    rows.extend([
        ModelProfile("qwen36-a3b-q4", "qwen36-a3b-q4", "qwen36-a3b-q4-200k", "upstream",
                     None, _common("qwen36-a3b-q4-200k") + qwen36_sampler),
        ModelProfile("gemma4-a4b-q4", "gemma4-a4b-q4", "gemma4-a4b-q4-200k", "upstream",
                     "gemma4-mtp", _common("gemma4-a4b-q4-200k") + gemma_sampler),
    ])
    return {row.profile_id: row for row in rows}


def missing_payload_bytes() -> int:
    return sum(item.size for item in artifacts().values() if not item.preexisting)


def summarize_runs(runs: list[dict]) -> dict:
    valid = [
        row for row in runs
        if row.get("status") == "pass"
        and int(row.get("actual_input_tokens", 0)) >= 200_000
        and int(row.get("output_tokens", 0)) >= 256
        and row.get("sentinel_recalled") is True
        and row.get("full_prompt_evaluated") is True
    ]
    if len(valid) != 3:
        return {"status": "fail", "valid_runs": len(valid), "total_runs": len(runs)}
    return {
        "status": "pass",
        "valid_runs": 3,
        "prompt_tps_median": round(median(float(row["prompt_tps"]) for row in valid), 3),
        "generation_tps_median": round(median(float(row["generation_tps"]) for row in valid), 3),
        "ttft_seconds_median": round(median(float(row.get("ttft_seconds", 0)) for row in valid), 3),
        "peak_rss_bytes": max(int(row.get("peak_rss_bytes", 0)) for row in valid),
    }


def manifest_dict() -> dict:
    return {
        "artifacts": {key: asdict(value) | {"origin_url": value.origin_url}
                      for key, value in artifacts().items()},
        "profiles": {key: asdict(value) for key, value in profiles().items()},
        "missing_payload_bytes": missing_payload_bytes(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("json")
    artifact_parser = sub.add_parser("artifact")
    artifact_parser.add_argument("artifact_id")
    artifact_parser.add_argument("--field")
    profile_parser = sub.add_parser("profile")
    profile_parser.add_argument("profile_id")
    args = parser.parse_args()
    if args.command == "json":
        payload = manifest_dict()
    elif args.command == "artifact":
        item = artifacts()[args.artifact_id]
        payload = asdict(item) | {"origin_url": item.origin_url}
        if args.field:
            value = payload[args.field]
            print(str(value).lower() if isinstance(value, bool) else value)
            return 0
    else:
        payload = asdict(profiles()[args.profile_id])
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
