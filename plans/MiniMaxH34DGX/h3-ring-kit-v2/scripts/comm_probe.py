#!/usr/bin/env python3
"""One GPU per node. Test neighbor P2P, collectives, or optional all-pairs P2P."""
import argparse
import datetime
import json
import os
import time


def main() -> None:
    import torch
    import torch.distributed as dist

    p = argparse.ArgumentParser()
    p.add_argument("--suite", choices=["ring", "collectives", "allpairs"], required=True)
    p.add_argument("--timeout", type=int, default=180)
    a = p.parse_args()
    rank, world = int(os.environ["RANK"]), int(os.environ["WORLD_SIZE"])
    if world not in (2, 4):
        raise ValueError("This probe is designed for two or four one-GPU nodes")
    torch.cuda.set_device(0)
    dist.init_process_group("nccl", timeout=datetime.timedelta(seconds=a.timeout))
    try:
        # All ranks enter the first communicator operation.
        dist.barrier(device_ids=[0])
        for nbytes in (4096, 1048576, 16777216, 67108864):
            count = nbytes // 4
            tx = torch.full((count,), rank + 1, dtype=torch.float32, device="cuda")
            rx = torch.zeros_like(tx)
            dist.barrier(device_ids=[0])
            torch.cuda.synchronize()
            t0 = time.perf_counter()
            if a.suite == "ring":
                for direction in (1, -1):
                    send_to = (rank + direction) % world
                    recv_from = (rank - direction) % world
                    rx.zero_()
                    reqs = dist.batch_isend_irecv([
                        dist.P2POp(dist.isend, tx, send_to),
                        dist.P2POp(dist.irecv, rx, recv_from),
                    ])
                    for req in reqs:
                        req.wait()
                    torch.cuda.synchronize()
                    if not torch.all(rx == recv_from + 1).item():
                        raise RuntimeError(f"Neighbor data mismatch at rank {rank}")
            elif a.suite == "collectives":
                reduced = tx.clone()
                dist.all_reduce(reduced)
                expected = world * (world + 1) / 2
                if not torch.all(reduced == expected).item():
                    raise RuntimeError("all_reduce data mismatch")
                gathered = [torch.empty_like(tx) for _ in range(world)]
                dist.all_gather(gathered, tx)
                for source, tensor in enumerate(gathered):
                    if not torch.all(tensor == source + 1).item():
                        raise RuntimeError("all_gather data mismatch")
                # Every rank is a broadcast source at least once.
                for source in range(world):
                    rx.fill_(source + 1 if rank == source else 0)
                    dist.broadcast(rx, src=source)
                    if not torch.all(rx == source + 1).item():
                        raise RuntimeError("broadcast data mismatch")
                del reduced, gathered
            else:
                # offset=2 in world=4 specifically checks the opposite ranks.
                for offset in range(1, world):
                    send_to, recv_from = (rank + offset) % world, (rank - offset) % world
                    rx.zero_()
                    reqs = dist.batch_isend_irecv([
                        dist.P2POp(dist.isend, tx, send_to),
                        dist.P2POp(dist.irecv, rx, recv_from),
                    ])
                    for req in reqs:
                        req.wait()
                    torch.cuda.synchronize()
                    if not torch.all(rx == recv_from + 1).item():
                        raise RuntimeError("all-pairs data mismatch")
            torch.cuda.synchronize()
            print(json.dumps({"rank": rank, "world": world, "suite": a.suite,
                              "bytes_per_tensor": nbytes,
                              "seconds": round(time.perf_counter() - t0, 6),
                              "status": "PASS"}), flush=True)
            del tx, rx
        dist.barrier(device_ids=[0])
    finally:
        dist.destroy_process_group()


if __name__ == "__main__":
    main()
