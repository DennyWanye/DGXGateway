#!/usr/bin/env python3
"""Bounded two-rank runtime smoke test; caller must hold the Gateway lease."""
import datetime
import json
import os
import subprocess
import time

import torch
import torch.distributed as dist

rank = int(os.environ["RANK"])
assert rank in (0, 1) and int(os.environ["WORLD_SIZE"]) == 2
assert torch.cuda.device_count() == 1
torch.cuda.set_device(0)
dist.init_process_group("nccl", timeout=datetime.timedelta(seconds=120))
try:
    value = torch.full((4 * 1024 * 1024,), rank + 1.0, device="cuda")
    started = time.monotonic()
    dist.all_reduce(value)
    torch.cuda.synchronize()
    elapsed = time.monotonic() - started
    assert torch.all(value == 3.0).item(), "two-rank reduction mismatch"
    # A small BF16 matrix operation establishes actual GB10 execution.
    x = torch.ones((256, 256), dtype=torch.bfloat16, device="cuda")
    result = x @ x
    assert torch.all(result == 256).item()
    dist.barrier()
    print(json.dumps({"passed": True, "rank": rank, "world_size": dist.get_world_size(),
                      "torch": torch.__version__, "cuda": torch.version.cuda,
                      "nccl": torch.cuda.nccl.version(), "device": torch.cuda.get_device_name(),
                      "reduced_bytes": value.numel() * value.element_size(),
                      "allreduce_seconds": elapsed,
                      "scope": "connectivity/correctness smoke; not bandwidth qualification"}), flush=True)
finally:
    dist.destroy_process_group()

if rank == 0:
    # This CLI resolves its platform even for help; test with actual GPU visibility.
    cli = subprocess.run(["vllm", "serve", "--help=all"], capture_output=True, text=True, timeout=90)
    print("VLLM_HELP_BEGIN\n" + cli.stdout + cli.stderr + "\nVLLM_HELP_END", flush=True)
    assert cli.returncode == 0, "vLLM CLI failed with the GPU visible"
