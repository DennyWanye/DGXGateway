#!/usr/bin/env python3
"""Non-GPU verification for the derived DeepSeek V4 Vision runtime image."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import site
from pathlib import Path

from apply_runtime_patches import (
    FLASHINFER_COMMIT,
    FLASHINFER_CUDA_POST_SHA256,
    VLLM_BUILD_COMMIT,
    VLLM_VISION_MERGE_COMMIT,
    VLLM_VL_POST_SHA256,
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def module_root(name: str) -> Path:
    spec = importlib.util.find_spec(name)
    if spec is None or spec.origin is None:
        raise RuntimeError(f"module unavailable: {name}")
    return Path(spec.origin).resolve().parent


def main() -> int:
    vllm_version = importlib.metadata.version("vllm")
    if "g5ab628dd1" not in vllm_version:
        raise RuntimeError(f"unexpected vLLM build version: {vllm_version}")
    vllm_source = module_root("vllm") / "models/deepseek_v4/nvidia/vl_model.py"
    if digest(vllm_source) != VLLM_VL_POST_SHA256:
        raise RuntimeError("instrumented vLLM VL source hash mismatch")
    vllm_text = vllm_source.read_text(encoding="utf-8")
    native_model_anchors = (
        "class DeepseekV4ForConditionalGeneration(",
        "self.vision = DeepseekV4ViT(",
        "self.aligner = DeepseekV4Aligner(",
        "vision_output = self.vision(",
        "image_embeds = self.aligner(",
        "_merge_multimodal_embeddings(",
    )
    if not all(item in vllm_text for item in native_model_anchors):
        raise RuntimeError("pinned vLLM native DeepSeek V4 Vision implementation is incomplete")
    required_events = (
        'emit_trace("vision.enter"',
        'emit_trace("vision.exit"',
        'emit_trace("aligner.enter"',
        'emit_trace("aligner.exit"',
        'emit_trace("multimodal.enter"',
        'emit_trace("multimodal.exit"',
        '"merge.enter"',
        'emit_trace("merge.exit"',
    )
    if not all(item in vllm_text for item in required_events):
        raise RuntimeError("native vision trace boundaries are incomplete")

    flash_root = module_root("flashinfer")
    flash_sources = [
        item
        for item in flash_root.rglob("sparse_mla_sm120_prefill.cu")
        if digest(item) == FLASHINFER_CUDA_POST_SHA256
    ]
    if len(flash_sources) != 1:
        raise RuntimeError("patched FlashInfer CUDA source is not unique")
    flash_text = flash_sources[0].read_text(encoding="utf-8")
    for arm in (
        "DISPATCH_DUAL_MG_CM(BF16, 32, 512, 64, 2)",
        "DISPATCH_DUAL_MG_CM(BF16, 64, 512, 64, 2)",
        "DISPATCH_DUAL_MG_CM(BF16, 32, 512, 2, 2)",
        "DISPATCH_DUAL_MG_CM(BF16, 64, 512, 2, 2)",
    ):
        if arm not in flash_text:
            raise RuntimeError(f"FlashInfer dispatch arm missing: {arm}")

    stale = []
    for root in [Path(item).resolve() for item in site.getsitepackages()] + [
        Path.home() / ".cache/flashinfer"
    ]:
        if root.exists():
            stale.extend(str(item) for item in root.rglob("sparse_mla_sm120.so"))
    if stale:
        raise RuntimeError(f"stale FlashInfer AOT present: {stale}")

    receipt_path = Path("/opt/deepseek-v4-flash-vision-exp/patch-receipt.json")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("flashinfer_commit") != FLASHINFER_COMMIT:
        raise RuntimeError("FlashInfer patch receipt commit mismatch")
    if receipt.get("vllm_build_commit") != VLLM_BUILD_COMMIT:
        raise RuntimeError("vLLM build receipt commit mismatch")
    if receipt.get("vllm_vision_merge_commit") != VLLM_VISION_MERGE_COMMIT:
        raise RuntimeError("vLLM vision merge receipt mismatch")

    import deepseek_v4_vision_trace  # noqa: F401
    import launch_vllm
    import vision_media_guard  # noqa: F401

    launch_vllm.validate_args(
        [
            "/models/deepseek-v4-flash-vision-exp",
            "--distributed-executor-backend", "mp",
            "--nnodes", "2",
            "--node-rank", "0",
            "--master-addr", "192.0.2.33",
            "--master-port", "25001",
            "--tensor-parallel-size", "2",
            "--pipeline-parallel-size", "1",
            "--gpu-memory-utilization", "0.85",
            "--middleware",
            "vision_media_guard.VisionMediaGuard",
            "--allowed-media-domains",
            "deepseek-v4-vision.invalid",
        ],
        {
            "VLLM_MEDIA_URL_ALLOW_REDIRECTS": "0",
            "VISION_ROLE": "head",
            "VLLM_HOST_IP": "192.0.2.33",
        },
    )

    print(
        json.dumps(
            {
                "schema": "deepseek-v4-vision-runtime-static-verification-v1",
                "vllm_version": vllm_version,
                "vllm_source_sha256": digest(vllm_source),
                "flashinfer_source": str(flash_sources[0]),
                "flashinfer_source_sha256": digest(flash_sources[0]),
                "stale_aot_count": 0,
                "status": "PASS",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
