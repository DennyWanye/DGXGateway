#!/usr/bin/env python3
"""Verify the small runtime build context without Docker, network, or a GPU."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTEXT = ROOT / "containers/deepseek-v4-flash-vision-exp"
BASE_DIGEST = "sha256:8568b4bbc821903d93a0a9c17dd80382fdc0ba78eaa128e3eb5cb71c3bf06b79"
BASE_CONFIG = "sha256:b12d77aecd6ca64639f55ab946f90342ba108ae1c32e4c545242722eff3b04d1"
BASE_LOCAL_REF = "vllm/vllm-openai:vision-base-8568b4bbc821"
PATCH_SHA256 = "73c32044c5cddcb595782e5d6b4f6622f77ca97d9d3ffad22c9dcd8417031de4"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_patch_module():
    module_path = CONTEXT / "apply_runtime_patches.py"
    spec = importlib.util.spec_from_file_location("dsv4_runtime_patch", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load runtime patch module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    required = (
        "Containerfile",
        "apply_runtime_patches.py",
        "deepseek_v4_vision_trace.py",
        "vision_media_guard.py",
        "launch_vllm.py",
        "verify_runtime_install.py",
        "flashinfer-7a55473b9e3f81a1ce7c862c3b993f6ef23cab5f.patch",
    )
    missing = [name for name in required if not (CONTEXT / name).is_file()]
    if missing:
        raise RuntimeError(f"runtime context files missing: {missing}")
    for name in required:
        if name.endswith(".py"):
            ast.parse((CONTEXT / name).read_text(encoding="utf-8"), filename=name)

    containerfile = (CONTEXT / "Containerfile").read_text(encoding="utf-8")
    first_instruction = next(
        line.strip() for line in containerfile.splitlines() if line.strip()
    )
    if first_instruction != f"FROM {BASE_LOCAL_REF}":
        raise RuntimeError("Containerfile does not use the content-addressed local base tag")
    if "--mount=type=secret" in containerfile or "curl " in containerfile or "apt-get" in containerfile:
        raise RuntimeError("runtime build context attempts network/package acquisition")
    if f'org.opencontainers.image.base.digest="{BASE_DIGEST}"' not in containerfile:
        raise RuntimeError("base digest provenance label missing")
    if 'io.dgxspark.deepseek.model-repository="drowzeys/keys-DeepSeekV4Flash-Vision-EXP-ablit"' not in containerfile:
        raise RuntimeError("selected ablit repository provenance label missing")
    if 'io.dgxspark.deepseek.model-revision="48095b3452a17f3e3ae8f77892399389c45de9e1"' not in containerfile:
        raise RuntimeError("selected ablit revision provenance label missing")
    expected_entrypoint = (
        'ENTRYPOINT ["python3", '
        '"/opt/deepseek-v4-flash-vision-exp/launch_vllm.py"]'
    )
    if expected_entrypoint not in containerfile:
        raise RuntimeError("fail-closed vLLM launcher is not the image entrypoint")

    patch_path = CONTEXT / required[-1]
    if digest(patch_path) != PATCH_SHA256:
        raise RuntimeError("FlashInfer upstream patch hash mismatch")
    patch_text = patch_path.read_text(encoding="utf-8")
    if patch_text.count("diff --git ") != 2:
        raise RuntimeError("FlashInfer PR patch is not the exact two-file change")
    for geometry in (
        "(32, 512, 128, 2)",
        "(64, 512, 128, 2)",
        "DISPATCH_DUAL_MG_CM(BF16, 32, 512, 64, 2)",
        "DISPATCH_DUAL_MG_CM(BF16, 32, 512, 2, 2)",
    ):
        if geometry not in patch_text:
            raise RuntimeError(f"FlashInfer geometry missing: {geometry}")

    patch_module = load_patch_module()
    expected = {
        "FLASHINFER_PATCH_SHA256": PATCH_SHA256,
        "VLLM_BUILD_COMMIT": "5ab628dd1995cd7cba69c32621a82fc4e8c12b6f",
        "VLLM_VISION_MERGE_COMMIT": "1356635d837c4ef002ec98c1a0296e7ff60be3c1",
    }
    for name, value in expected.items():
        if getattr(patch_module, name) != value:
            raise RuntimeError(f"runtime pin mismatch: {name}")

    trace_text = (CONTEXT / "deepseek_v4_vision_trace.py").read_text(encoding="utf-8")
    if ".item(" in trace_text or "tolist(" in trace_text or "numpy(" in trace_text:
        raise RuntimeError("native trace attempts to read tensor values")
    guard_text = (CONTEXT / "vision_media_guard.py").read_text(encoding="utf-8")
    for forbidden in ("http://", "https://", "file://", "ftp://", "gopher://"):
        if forbidden in guard_text:
            raise RuntimeError("media guard contains a remote/local URL allow path")

    launcher_path = CONTEXT / "launch_vllm.py"
    launcher_spec = importlib.util.spec_from_file_location("dsv4_launch_vllm", launcher_path)
    if launcher_spec is None or launcher_spec.loader is None:
        raise RuntimeError("cannot load fail-closed vLLM launcher")
    launcher = importlib.util.module_from_spec(launcher_spec)
    launcher_spec.loader.exec_module(launcher)
    if launcher.REQUIRED_MIDDLEWARE != "vision_media_guard.VisionMediaGuard":
        raise RuntimeError("vLLM middleware import path is not pinned")
    if launcher.REQUIRED_MEDIA_DOMAIN != "deepseek-v4-vision.invalid":
        raise RuntimeError("reserved remote-media domain is not pinned")
    launcher.validate_args(
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
                "schema": "deepseek-v4-vision-runtime-context-v1",
                "base_manifest_digest": BASE_DIGEST,
                "base_config_digest": BASE_CONFIG,
                "flashinfer_patch_sha256": digest(patch_path),
                "files": {name: digest(CONTEXT / name) for name in required},
                "status": "PASS",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
