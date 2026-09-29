#!/usr/bin/env python3
"""Bounded two-rank CUDA/NCCL compatibility probe for the pinned runtime image."""

import json
import os
import time

import torch
import torch.distributed as dist


def main() -> None:
    if not torch.cuda.is_available() or not dist.is_available() or not dist.is_nccl_available():
        raise SystemExit("CUDA/NCCL torch.distributed is unavailable")
    rank = int(os.environ["RANK"])
    world = int(os.environ["WORLD_SIZE"])
    if world != 2 or rank not in (0, 1):
        raise SystemExit("probe requires exactly two ranks")
    torch.cuda.set_device(0)
    device_name = torch.cuda.get_device_name(0)
    capability = list(torch.cuda.get_device_capability(0))
    if capability != [12, 1] and "GB10" not in device_name.upper():
        raise SystemExit(f"unexpected CUDA device: {device_name} capability={capability}")
    dist.init_process_group(backend="nccl")
    wrong = 0
    iterations = 32
    elements = 1 << 24
    tensor = torch.full((elements,), float(rank + 1), dtype=torch.float32, device="cuda")
    try:
        for _ in range(4):
            tensor.fill_(float(rank + 1))
            dist.all_reduce(tensor)
            torch.cuda.synchronize()
        started = time.monotonic()
        for _ in range(iterations):
            tensor.fill_(float(rank + 1))
            dist.all_reduce(tensor)
            torch.cuda.synchronize()
            if not bool(torch.all(tensor == 3.0).item()):
                wrong += 1
        duration = time.monotonic() - started
        result = {
            "schema_version": 1,
            "rank": rank,
            "world_size": world,
            "backend": dist.get_backend(),
            "device_name": device_name,
            "device_capability": capability,
            "torch_version": torch.__version__,
            "cuda_version": torch.version.cuda,
            "nccl_version": list(torch.cuda.nccl.version()),
            "iterations": iterations,
            "elements": elements,
            "bytes_per_tensor": tensor.numel() * tensor.element_size(),
            "duration_seconds": duration,
            "wrong": wrong,
        }
        print(json.dumps(result, sort_keys=True), flush=True)
        if wrong:
            raise SystemExit("all_reduce data validation failed")
    finally:
        dist.destroy_process_group()


if __name__ == "__main__":
    main()
