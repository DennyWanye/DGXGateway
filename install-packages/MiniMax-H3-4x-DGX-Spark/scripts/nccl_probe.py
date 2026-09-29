#!/usr/bin/env python3
"""Four-node correctness smoke test; not a standard NCCL bus-bandwidth benchmark."""
import hashlib
import importlib.metadata
import json
import os
import socket
import subprocess
import time
from datetime import timedelta
from pathlib import Path
import torch
import torch.distributed as dist

def main() -> None:
    torch.cuda.set_device(0)
    dist.init_process_group("nccl", timeout=timedelta(minutes=5))
    try:
        rank, size = dist.get_rank(), dist.get_world_size()
        if size != 4:
            raise RuntimeError(f"Expected four ranks; got {size}")
        lock = Path(os.environ.get("H3_ROOT", "/srv/h3")) / "manifests/model-lock.json"
        rev = subprocess.check_output(["git", "-C", "/opt/h3-sglang", "rev-parse", "HEAD"], text=True).strip()
        identity = {"revision": rev, "torch": torch.__version__,
                    "profile": os.environ.get("H3_PROFILE"), "network": os.environ.get("H3_NETWORK_MODE"),
                    "transformers": importlib.metadata.version("transformers"),
                    "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest() if lock.exists() else None}
        identities = [None] * size
        dist.all_gather_object(identities, identity)
        if any(v != identities[0] for v in identities):
            raise RuntimeError(f"Node runtime/model-lock mismatch: {identities}")
        if rank == 0:
            print(json.dumps({"matched_runtime": identity}, indent=2), flush=True)
            if not identity["lock_sha256"]:
                print("Weights have not been staged yet; repeat after model synchronization.", flush=True)
        for mib in (1, 64):
            values = torch.empty(mib * 1024 * 1024 // 4, dtype=torch.float32, device="cuda")
            for _ in range(2):
                values.fill_(rank + 1)
                dist.all_reduce(values)
            torch.cuda.synchronize()
            dist.barrier()
            start = time.perf_counter()
            for _ in range(5):
                values.fill_(rank + 1)
                dist.all_reduce(values)
            torch.cuda.synchronize()
            elapsed = time.perf_counter() - start
            if values.min().item() != 10.0 or values.max().item() != 10.0:
                raise RuntimeError("All-reduce result is incorrect")
            print(json.dumps({"host": socket.gethostname(), "rank": rank,
                              "tensor_MiB": mib, "mean_iteration_s": elapsed / 5,
                              "includes_fill_overhead": True}), flush=True)
        dist.barrier()
        print(f"NCCL_ALLREDUCE_PASS rank={rank}; this is NOT an H3 output test", flush=True)
    finally:
        dist.destroy_process_group()

if __name__ == "__main__":
    main()
