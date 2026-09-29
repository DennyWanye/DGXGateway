"""PDD Ref2VA adapter for native H3 TP1/Ring4; no model lifecycle operations.

PDD math follows Alibaba PAI / VideoX-Fun (f3d54914598c70cb2dae36b5b1a4cde54162a3f3).
The base FP8 weights stay intact; BF16 low-rank updates are added at each linear
output, with native QKV and SwiGLU layout conversion. No silent partial loading.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time

import torch
import torch.nn.functional as F

LOG = logging.getLogger(__name__)
WEIGHT_SHA256 = "111c82e669f6e20e628228172edf39395f1a9fc3ad049793895e542c0f55b18c"
NFE = 8
GRID = 32


def pdd_path():
    return os.environ.get("H3_PDD_PATH", "")


def time_grid(shift, count=GRID):
    sigma = torch.linspace(1, 0, count + 1, dtype=torch.float64)
    return 1 - shift * sigma / (1 + (shift - 1) * sigma)


def sampling_plan(shift, step):
    if not 0 <= step < NFE:
        raise ValueError("PDD step outside 8-evaluation plan")
    sizes = time_grid(shift).diff()
    start, end = step * 4, (step + 1) * 4
    plan = torch.zeros(GRID, dtype=torch.float32)
    plan[start:end] = (sizes[start:end] / sizes[start:end].sum()).float()
    return plan


def native_target(name):
    """Return native module, QKV slice number, and SwiGLU row swap."""
    m = re.fullmatch(r"transformer_blocks\.(\d+)\.(.*)", name)
    if m:
        prefix, suffix = "blocks." + m[1], m[2]
    else:
        m = re.fullmatch(r"token_refiner\.refiner_blocks\.(\d+)\.(.*)", name)
        if not m:
            raise ValueError(f"Unsupported PDD module {name}")
        prefix, suffix = "token_refiner.blocks." + m[1], m[2]
    for i, qkv in enumerate(("q", "k", "v")):
        if suffix == f"attn.to_{qkv}":
            return prefix + ".attn.qkv_proj", i, False
    mapping = {"attn.to_out.0": "attn.out_proj", "ff.net.0.proj": "mlp.fc1",
               "ff.net.2": "mlp.fc2", "adaln_proj.linear": "adaln_proj.linear"}
    if suffix not in mapping:
        raise ValueError(f"Unsupported PDD module {name}")
    return prefix + "." + mapping[suffix], None, suffix == "ff.net.0.proj"


def add_delta(output, x, adapters):
    if not isinstance(x, torch.Tensor):
        raise ValueError("PDD requires unquantized activation input to native linears")
    # Chunk activation work to avoid materializing a full packed-sequence delta.
    out = output[0] if isinstance(output, tuple) else output
    flat_x, flat_out = x.reshape(-1, x.shape[-1]), out.view(-1, out.shape[-1])
    for down, up, slot, scale in adapters:
        down, up = down.to(x.device, x.dtype), up.to(x.device, x.dtype)
        width = up.shape[0]
        start = 0 if slot is None else slot * width
        for row in range(0, flat_x.shape[0], 2048):
            end = row + 2048
            delta = F.linear(F.linear(flat_x[row:end], down), up)
            flat_out[row:end, start:start + width].add_(delta.to(out.dtype), alpha=scale)
    return output


class PDDController:
    def __init__(self, model, path):
        from safetensors import safe_open
        from sglang.multimodal_gen.runtime.distributed.parallel_state import get_tp_world_size
        if get_tp_world_size() != 1:
            raise ValueError("PDD adapter qualified only for TP1; keep Ring4/SP4")
        if getattr(model, "adaln_cache", None) is not None:
            raise ValueError("PDD AdaLN deltas cannot use the base AdaLN sidecar")
        start = time.monotonic()
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8 << 20), b""):
                digest.update(chunk)
        if digest.hexdigest() != WEIGHT_SHA256:
            raise ValueError("PDD weight SHA256 mismatch")
        with safe_open(path, framework="pt", device="cpu") as f:
            meta = f.metadata()
            if (int(meta["pdd_num_steps"]), int(meta["pdd_block_size"]), int(meta["lora_rank"])) != (32, 4, 64):
                raise ValueError("Unexpected PDD grid/block/rank")
            scale = float(meta["lora_alpha"]) / int(meta["lora_rank"])
            tensors = {k: f.get_tensor(k) for k in f.keys()}
        expected = set()
        grouped = {}
        for key in sorted(tensors):
            if not key.endswith(".lora_down"):
                continue
            source = key.removesuffix(".lora_down")
            other = source + ".lora_up"
            down, up = tensors[key], tensors[other]
            target, slot, swap = native_target(source)
            module = model.get_submodule(target)
            if swap:
                value, gate = up.chunk(2, dim=0)
                up = torch.cat((gate, value))
            if down.shape != (64, module.input_size):
                raise ValueError(f"PDD input dimension mismatch: {source}")
            width = module.output_size // 3 if slot is not None else module.output_size
            if up.shape != (width, 64):
                raise ValueError(f"PDD output dimension mismatch: {source}")
            grouped.setdefault(target, []).append((down, up, slot, scale))
            expected.update((key, other))
        self.heads = {}
        self.fused = {}
        for source, target, width in (("proj_out", "video_out", 96), ("audio_proj_out", "audio_out", 32)):
            weight, bias = tensors[source + ".weight"], tensors[source + ".bias"]
            if weight.shape != (GRID, width, 5376) or bias.shape != (GRID, width):
                raise ValueError(f"PDD head shape mismatch: {source}")
            self.heads[target] = (weight.float(), bias.float())
            expected.update((source + ".weight", source + ".bias"))
        if expected != set(tensors) or len(expected) != 728 or len(grouped) != 258:
            raise ValueError(f"Incomplete PDD load: consumed {len(expected)}/728, modules {len(grouped)}/258")
        # All validation precedes the first mutation. Hooks retain CPU tensors,
        # outside layerwise offload's base-checkpoint parameter inventory.
        self.handles = []
        for target, adapters in grouped.items():
            module = model.get_submodule(target)
            def hook(_module, args, output, adapters=adapters):
                return add_delta(output, args[0], adapters)
            self.handles.append(module.register_forward_hook(hook))
        for name in self.heads:
            module = model.get_submodule("final_layer." + name)
            def head_hook(_module, args, output, name=name):
                if name not in self.fused:
                    raise ValueError("PDD output head used without a sampling plan")
                x = args[0]
                w, b = self.fused[name]
                result = F.linear(x, w.to(x.device, x.dtype), b.to(x.device, x.dtype))
                return (result, None)
            self.handles.append(module.register_forward_hook(head_hook))
        LOG.warning("H3_PDD loaded sha256=%s tensors=728 modules=258 heads=2 seconds=%.3f", WEIGHT_SHA256, time.monotonic() - start)

    def arm(self, step):
        for name, shift in (("video_out", 12.0), ("audio_out", 3.0)):
            weight, bias = self.heads[name]
            plan = sampling_plan(shift, step)
            self.fused[name] = (torch.einsum("n,noi->oi", plan, weight), torch.einsum("n,no->o", plan, bias))


def attach_pdd(model):
    path = pdd_path()
    if not path:
        return
    if not hasattr(model, "_h3_pdd"):
        model._h3_pdd = PDDController(model, path)


def prepare_pdd_schedule(batch, scales):
    if not pdd_path():
        return None
    if scales != {"video": 12.0, "audio": 3.0}:
        raise ValueError("Released PDD requires video/audio shifts 12/3")
    batch.num_inference_steps = NFE + 1  # Native H3 counts sigma points.
    batch.sampling_params.num_inference_steps = NFE + 1
    result = {k: (1 - time_grid(v)[::4]).float().tolist() for k, v in scales.items()}
    LOG.warning("H3_PDD schedule nfe=8 grid=32 block=4 sigmas=%s", json.dumps(result))
    return result
