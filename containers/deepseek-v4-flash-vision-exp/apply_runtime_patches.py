#!/usr/bin/env python3
"""Strictly apply the pinned FlashInfer fix and native trace instrumentation."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import site
from pathlib import Path


FLASHINFER_COMMIT = "7a55473b9e3f81a1ce7c862c3b993f6ef23cab5f"
FLASHINFER_PATCH_SHA256 = "73c32044c5cddcb595782e5d6b4f6622f77ca97d9d3ffad22c9dcd8417031de4"
FLASHINFER_CUDA_PRE_SHA256 = "f7cb21078f1f6d997a3f96f1513eaa8b923ca57b4d17156952ec476e56d60cad"
FLASHINFER_CUDA_POST_SHA256 = "472ff9abd16a1bcb25dc01a6bc693c6d3b9927ff22daf7748fdc906aec05a355"
VLLM_BUILD_COMMIT = "5ab628dd1995cd7cba69c32621a82fc4e8c12b6f"
VLLM_VISION_MERGE_COMMIT = "1356635d837c4ef002ec98c1a0296e7ff60be3c1"
VLLM_VL_PRE_SHA256 = "0b362879887f518da0117479a2165b767dce1ccc12e97fc26e09f889784dc59d"
VLLM_VL_POST_SHA256 = "5029aae571f3f331ab97a7545d57af0f8a0e1372eaa9d56f466b7a635fc8fe3a"


FLASHINFER_OLD = """#define DISPATCH_BY_NH_PBSX(PBSX)"""

FLASHINFER_NEW = """  // DeepSeek V4 Vision widens the primary causal-SWA candidate set from
  // 128 to 512 entries. topk_extra remains runtime, and the extra cache uses
  // both enumerated page layouts in serving.
  if (topk == 512) {
    if (extra_page_block_size == 64) {
      if (num_heads == 32) {
        DISPATCH_DUAL_MG_CM(BF16, 32, 512, 64, 2);
        return true;
      }
      if (num_heads == 64) {
        DISPATCH_DUAL_MG_CM(BF16, 64, 512, 64, 2);
        return true;
      }
    } else if (extra_page_block_size == 2) {
      if (num_heads == 32) {
        DISPATCH_DUAL_MG_CM(BF16, 32, 512, 2, 2);
        return true;
      }
      if (num_heads == 64) {
        DISPATCH_DUAL_MG_CM(BF16, 64, 512, 2, 2);
        return true;
      }
    }
  }

#define DISPATCH_BY_NH_PBSX(PBSX)"""

VLLM_IMPORT_OLD = """import torch
from torch import nn
"""
VLLM_IMPORT_NEW = """import torch
from torch import nn

from deepseek_v4_vision_trace import emit_trace
"""

VLLM_ENCODER_OLD = """            image_embeds = self.aligner(
                self.vision(patches[vit_offset : vit_offset + n_vit], n_vit_h, n_vit_w),
                n_vit_h,
                n_vit_w,
            )
"""
VLLM_ENCODER_NEW = """            vision_input = patches[vit_offset : vit_offset + n_vit]
            emit_trace("vision.enter", tensor=vision_input)
            vision_output = self.vision(vision_input, n_vit_h, n_vit_w)
            emit_trace("vision.exit", tensor=vision_output)
            emit_trace("aligner.enter", tensor=vision_output)
            image_embeds = self.aligner(vision_output, n_vit_h, n_vit_w)
            emit_trace("aligner.exit", tensor=image_embeds)
"""

VLLM_EMBED_OLD = """        return self._process_image_input(
            image_input["patches"],
            image_input["vit_grid"],
            image_input["llm_grid"],
            image_input["perm"],
        )
"""
VLLM_EMBED_NEW = """        emit_trace("multimodal.enter", tensor=image_input["patches"])
        embeddings = self._process_image_input(
            image_input["patches"],
            image_input["vit_grid"],
            image_input["llm_grid"],
            image_input["perm"],
        )
        emit_trace("multimodal.exit", tensor=embeddings)
        return embeddings
"""

VLLM_MERGE_OLD = """        assert is_multimodal is not None
        return _merge_multimodal_embeddings(
            inputs_embeds=inputs_embeds,
            multimodal_embeddings=multimodal_embeddings,
            is_multimodal=is_multimodal,
        )
"""
VLLM_MERGE_NEW = """        assert is_multimodal is not None
        emit_trace(
            "merge.enter",
            inputs_embeds=inputs_embeds,
            multimodal_embeddings=multimodal_embeddings,
            is_multimodal=is_multimodal,
        )
        merged = _merge_multimodal_embeddings(
            inputs_embeds=inputs_embeds,
            multimodal_embeddings=multimodal_embeddings,
            is_multimodal=is_multimodal,
        )
        emit_trace("merge.exit", tensor=merged)
        return merged
"""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"{label} preimage is not unique")
    return text.replace(old, new, 1)


def patch_vllm_bytes(raw: bytes) -> bytes:
    if sha256_bytes(raw) != VLLM_VL_PRE_SHA256:
        raise RuntimeError("vLLM DeepSeek V4 VL source preimage hash mismatch")
    text = raw.decode("utf-8")
    for old, new, label in (
        (VLLM_IMPORT_OLD, VLLM_IMPORT_NEW, "trace import"),
        (VLLM_ENCODER_OLD, VLLM_ENCODER_NEW, "vision and aligner trace"),
        (VLLM_EMBED_OLD, VLLM_EMBED_NEW, "multimodal trace"),
        (VLLM_MERGE_OLD, VLLM_MERGE_NEW, "merge trace"),
    ):
        text = _replace_once(text, old, new, label)
    return text.encode("utf-8")


def _module_root(name: str) -> Path:
    spec = importlib.util.find_spec(name)
    if spec is None or spec.origin is None:
        raise RuntimeError(f"installed module is unavailable: {name}")
    return Path(spec.origin).resolve().parent


def patch_vllm() -> dict[str, str]:
    target = _module_root("vllm") / "models/deepseek_v4/nvidia/vl_model.py"
    raw = target.read_bytes()
    digest = sha256_bytes(raw)
    if digest == VLLM_VL_POST_SHA256:
        return {"path": str(target), "state": "already-patched", "sha256": digest}
    patched = patch_vllm_bytes(raw)
    patched_digest = sha256_bytes(patched)
    if patched_digest != VLLM_VL_POST_SHA256:
        raise RuntimeError("vLLM trace postimage hash mismatch")
    target.write_bytes(patched)
    return {"path": str(target), "state": "patched", "sha256": patched_digest}


def patch_flashinfer() -> dict[str, object]:
    package_root = _module_root("flashinfer")
    candidates = sorted(package_root.rglob("sparse_mla_sm120_prefill.cu"))
    matching = [
        item
        for item in candidates
        if sha256_bytes(item.read_bytes())
        in {FLASHINFER_CUDA_PRE_SHA256, FLASHINFER_CUDA_POST_SHA256}
    ]
    if len(matching) != 1:
        raise RuntimeError(
            "expected exactly one hash-pinned FlashInfer sparse MLA CUDA source; "
            f"found {len(matching)}"
        )
    target = matching[0]
    raw = target.read_bytes()
    digest = sha256_bytes(raw)
    state = "already-patched"
    if digest == FLASHINFER_CUDA_PRE_SHA256:
        text = _replace_once(raw.decode("utf-8"), FLASHINFER_OLD, FLASHINFER_NEW, "FlashInfer PR 4850")
        patched = text.encode("utf-8")
        if sha256_bytes(patched) != FLASHINFER_CUDA_POST_SHA256:
            raise RuntimeError("FlashInfer CUDA source postimage hash mismatch")
        target.write_bytes(patched)
        digest = sha256_bytes(patched)
        state = "patched"
    return {"path": str(target), "state": state, "sha256": digest}


def remove_stale_aot() -> list[str]:
    roots = {Path(item).resolve() for item in site.getsitepackages()}
    roots.add(Path.home() / ".cache/flashinfer")
    removed: list[str] = []
    for root in sorted(roots):
        if not root.exists() or root.is_symlink():
            continue
        for target in root.rglob("sparse_mla_sm120.so"):
            if target.is_symlink() or not target.is_file():
                raise RuntimeError(f"unsafe stale AOT candidate: {target}")
            target.unlink()
            removed.append(str(target))
    remaining = [str(item) for root in roots if root.exists() for item in root.rglob("sparse_mla_sm120.so")]
    if remaining:
        raise RuntimeError(f"stale FlashInfer AOT remains: {remaining}")
    return removed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream-patch", type=Path, required=True)
    args = parser.parse_args()
    patch_digest = sha256_bytes(args.upstream_patch.read_bytes())
    if patch_digest != FLASHINFER_PATCH_SHA256:
        raise RuntimeError("vendored FlashInfer PR 4850 patch hash mismatch")
    result = {
        "schema": "deepseek-v4-vision-runtime-patch-v1",
        "flashinfer_commit": FLASHINFER_COMMIT,
        "flashinfer_patch_sha256": patch_digest,
        "flashinfer": patch_flashinfer(),
        "vllm_build_commit": VLLM_BUILD_COMMIT,
        "vllm_vision_merge_commit": VLLM_VISION_MERGE_COMMIT,
        "vllm": patch_vllm(),
        "removed_stale_aot": remove_stale_aot(),
    }
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
