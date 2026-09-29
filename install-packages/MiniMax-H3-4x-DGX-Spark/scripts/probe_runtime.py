#!/usr/bin/env python3
"""Fail early on a mismatched runtime. This is not an H3 inference test."""
import importlib.metadata
import json
import platform
import subprocess
import sys
from pathlib import Path

def main() -> None:
    import torch
    import torchaudio  # Check shared-library compatibility before downloading weights.
    import sglang
    if platform.machine() not in {"aarch64", "arm64"}:
        raise RuntimeError("Expected Linux ARM64, not an emulated amd64 image")
    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError("Each node must expose exactly one working CUDA GPU")
    cap = torch.cuda.get_device_capability(0)
    if cap != (12, 1):
        raise RuntimeError(f"Expected GB10 SM121; observed {cap}. Reassess this recipe.")
    x = torch.randn((128, 128), device="cuda", dtype=torch.bfloat16)
    y = x @ x.T
    torch.cuda.synchronize()
    if not torch.isfinite(y).all().item():
        raise RuntimeError("BF16 smoke test failed")
    result = subprocess.run(["sglang", "serve", "--help"], text=True,
                            capture_output=True, timeout=180)
    text = result.stdout + result.stderr
    if result.returncode:
        raise RuntimeError("sglang serve --help failed:\n" + text[-6000:])
    flags = ["--num-gpus", "--nnodes", "--node-rank", "--dist-init-addr",
             "--tp-size", "--sp-degree", "--model-variant", "--encoder-parallel",
             "--warmup-mode", "--enable-torch-compile", "--served-model-name",
             "--attention-backend", "--lora-weight-name", "--lora-merge-mode"]
    missing = [f for f in flags if f not in text]
    if missing:
        raise RuntimeError(f"Required CLI flags absent: {missing}")
    versions = {}
    for name in ("torch", "torchaudio", "transformers", "sglang", "huggingface-hub"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "unknown"
    print(json.dumps({"python": sys.version, "arch": platform.machine(),
                      "gpu": torch.cuda.get_device_name(0), "capability": cap,
                      "cuda": torch.version.cuda, "packages": versions,
                      "sglang_import": str(Path(sglang.__file__).resolve()),
                      "runtime_probe": "PASS (not H3 generation)"}, indent=2))

if __name__ == "__main__":
    main()
