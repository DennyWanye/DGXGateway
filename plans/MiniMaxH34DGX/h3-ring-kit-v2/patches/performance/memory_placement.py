"""Bounded, opt-in evidence/guard for the Spark memory placement experiment.

No placement mutations or tensor math. The upstream component-scoped residency
implementation owns placement and cleanup. Evidence contains metadata only.
"""
import json
import os
from pathlib import Path
import time

GIB = 1024 ** 3
CANDIDATE = "memory-dit-half-v1"


def inventory(model):
    groups = []
    for manager in model.layerwise_offload_managers or []:
        layers = []
        for index in range(manager.num_layers):
            # These are the buffers prefetch_layer actually copies/allocates,
            # after online FP8 conversion, not the BF16 checkpoint size.
            buffers = list(manager._consolidated_cpu_weights.get(index, {}).values())
            buffers += list(manager._strided_cpu_weights.get(index, {}).values())
            buffers += list(manager._mapped_cpu_weights.get(index, {}).values())
            logical = sum(t.numel() * t.element_size() for t in buffers)
            layers.append({"index": index, "copy_bytes": logical,
                           "resident": index in manager._resident_set,
                           "buffers": len(buffers),
                           "tensor_count": len(manager._weight_metadata.get(index, {}))})
        groups.append({"name": manager.layers_attr_str, "layers": layers,
                       "prefetch_size": manager.prefetch_size,
                       "resident_layers": manager.resident_layers,
                       "residency_policy": manager.residency_policy,
                       "host_copy_bytes": sum(x["copy_bytes"] for x in layers),
                       "selected_device_bytes": sum(x["copy_bytes"] for x in layers if x["resident"])})
    return groups


def budget_check(*, selected_bytes, current_bytes, limit_bytes, host_available_bytes,
                 cuda_allocated_bytes, historical_peak_bytes=58 * GIB,
                 activation_peak_bytes=32 * GIB, reserve_bytes=12 * GIB):
    # Peaks observed from the completed original-input full49 are rounded up.
    # Never add host availability and cgroup headroom: both must pass.
    cgroup_projected = max(current_bytes, historical_peak_bytes) + selected_bytes
    activation_growth = max(0, activation_peak_bytes - cuda_allocated_bytes)
    host_remaining = host_available_bytes - selected_bytes - activation_growth
    return {"ok": cgroup_projected + reserve_bytes <= limit_bytes and host_remaining >= reserve_bytes,
            "selected_bytes": selected_bytes, "cgroup_projected_bytes": cgroup_projected,
            "cgroup_remaining_bytes": limit_bytes - cgroup_projected,
            "host_remaining_after_activation_bytes": host_remaining,
            "activation_growth_bytes": activation_growth, "reserve_bytes": reserve_bytes}


def _record(batch, row):
    root = batch.extra.get("_h3_memory_root")
    if root:
        with (root / "placement.jsonl").open("a") as handle:
            handle.write(json.dumps({"wall_time": time.time(), **row}) + "\n")


def start(model, batch, server_args):
    if os.environ.get("H3_MEMORY_PLACEMENT") == WHOLE_CANDIDATE:
        return whole_start(model, batch, server_args)
    if os.environ.get("H3_MEMORY_PLACEMENT") != CANDIDATE:
        return
    import torch
    rank = torch.distributed.get_rank()
    options = batch.extra.get("h3_performance_diagnostic")
    run_id = options["id"] if options else "full-" + os.environ["H3_MEMORY_RUN_ID"]
    root = Path("/cache/h3-diagnostics") / run_id / f"rank{rank}"
    root.mkdir(parents=True, exist_ok=True)
    batch.extra["_h3_memory_root"] = root
    batch.extra["_h3_memory_model"] = model
    local = {"rank": rank, "phase": "before_stage"}
    try:
        if server_args.performance_mode != "memory":
            raise ValueError("memory placement experiment requires performance-mode=memory")
        groups = inventory(model)
        if {g["name"]: g["resident_layers"] for g in groups} != {"blocks": 25, "token_refiner.blocks": 1}:
            raise ValueError("unexpected DiT manager/group or resident count")
        selected = sum(g["selected_device_bytes"] for g in groups)
        if selected <= 0:
            raise ValueError("missing materialized host stores")
        cg = Path("/sys/fs/cgroup")
        mem = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        budget = budget_check(selected_bytes=selected,
            current_bytes=int((cg / "memory.current").read_text()),
            limit_bytes=int((cg / "memory.max").read_text()),
            host_available_bytes=int(mem["MemAvailable"].split()[0]) * 1024,
            cuda_allocated_bytes=torch.cuda.memory_allocated())
        local.update(groups=groups, budget=budget, ok=budget["ok"],
                     performance_mode=server_args.performance_mode,
                     component_resident_map=server_args.layerwise_resident_layers)
    except Exception as exc:
        local.update(ok=False, error=str(exc))
    # All ranks fail together before entering the denoise collectives.
    peers = [None] * torch.distributed.get_world_size()
    torch.distributed.all_gather_object(peers, local)
    _record(batch, {**local, "peer_budgets": [{k: p.get(k) for k in ("rank", "ok", "budget", "error")} for p in peers]})
    if not all(p["ok"] for p in peers):
        raise RuntimeError("H3 memory placement budget/configuration rejected; see placement.jsonl")


def state(batch, phase, step_index=None):
    if batch.extra.get("_h3_whole_resident"):
        _record(batch, {"phase": phase, "step_index": step_index,
                        "inventory": resident_inventory(batch.extra["_h3_memory_model"])})
        return
    model = batch.extra.get("_h3_memory_model")
    if model is None:
        return
    _record(batch, {"phase": phase, "step_index": step_index,
                   "groups": [{"name": m.layers_attr_str,
                               "resident_active": m._residency_active,
                               "gpu_layers": sorted(m._gpu_layers),
                               "resident_layers": sorted(m._resident_set)}
                              for m in model.layerwise_offload_managers]})


def verify_samples(batch):
    """Bitwise checks of first/last scalar of every retained tensor, outside timing.

    This detects incorrect bindings/dtypes/copies; it is NOT a full forward
    equivalence proof. No full-sized flatten, temporary weight, or re-forward.
    """
    if batch.extra.get("_h3_whole_resident"):
        return whole_verify(batch)
    model = batch.extra.get("_h3_memory_model")
    if model is None:
        return
    import torch
    try:
        checked = 0
        failures = []
        for manager in model.layerwise_offload_managers:
            for index in sorted(manager._resident_set & manager._gpu_layers):
                for name, meta in manager._weight_metadata.get(index, {}).items():
                    target = manager.get_target_with_name(name).detach()
                    if meta.get("mapped", False):
                        source = manager._mapped_cpu_weights[index][name]
                    elif meta.get("preserve_strides", False):
                        source = manager._strided_cpu_weights[index][name]
                    else:
                        buffer = manager._consolidated_cpu_weights[index][meta["dtype"]]
                        source = buffer[meta["offset"]:meta["offset"] + meta["numel"]].view(meta["shape"])
                    equal = target.dtype == source.dtype and tuple(target.shape) == tuple(source.shape)
                    if equal and target.numel():
                        for position in (tuple(0 for _ in target.shape), tuple(d - 1 for d in target.shape)):
                            a = target[position].cpu().reshape(1).view(torch.uint8)
                            b = source[position].reshape(1).view(torch.uint8)
                            equal = equal and torch.equal(a, b)
                    checked += 1
                    if not equal:
                        failures.append(name)
        local = {"phase": "retained_weight_samples", "checked_tensors": checked, "failures": failures}
    except Exception as exc:
        local = {"phase": "retained_weight_samples", "checked_tensors": 0, "failures": [str(exc)]}
    peers = [None] * torch.distributed.get_world_size()
    torch.distributed.all_gather_object(peers, local)
    _record(batch, local)
    if any(p["failures"] or not p["checked_tensors"] for p in peers):
        raise RuntimeError("H3 resident weight sample verification failed")


def finish(batch):
    state(batch, "after_stage_release")
    batch.extra.pop("_h3_memory_model", None)
    batch.extra.pop("_h3_memory_root", None)
    batch.extra.pop("_h3_whole_resident", None)


WHOLE_CANDIDATE = "memory-dit-resident-v1"
_WHOLE_PARAMETER_SAMPLE = None


def resident_inventory(model):
    """Metadata only: deduplicate actual parameter/buffer storages, not numel."""
    seen = set()
    bytes_by_device = {}
    dtype_parameters = {}
    cpu_parameters = []
    parameters = list(model.named_parameters())
    for name, tensor in parameters:
        dtype_parameters[str(tensor.dtype)] = dtype_parameters.get(str(tensor.dtype), 0) + 1
        if tensor.device.type != "cuda":
            cpu_parameters.append(name)
    for name, tensor in parameters + list(model.named_buffers()):
        if tensor.device.type == "meta":
            raise ValueError("Unmaterialized DiT tensor: " + name)
        storage = tensor.untyped_storage()
        key = (str(tensor.device), storage.data_ptr(), storage.nbytes())
        if key in seen:
            continue
        seen.add(key)
        device = tensor.device.type
        bytes_by_device[device] = bytes_by_device.get(device, 0) + storage.nbytes()
    managers = getattr(model, "layerwise_offload_managers", None) or []
    stores = sum(len(getattr(m, attr, {}) or {}) for m in model.modules()
                 for attr in ("_consolidated_cpu_weights", "_strided_cpu_weights", "_mapped_cpu_weights"))
    return {"unique_storage_bytes": bytes_by_device, "storage_count": len(seen),
            "parameter_count": len(parameters), "parameter_dtypes": dtype_parameters,
            "non_cuda_parameters": cpu_parameters,
            "layerwise_managers": len(managers), "host_store_entries": stores}


def resident_budget(*, inventory, host_available_bytes, current_bytes, limit_bytes, cuda_allocated_bytes):
    device_bytes = inventory["unique_storage_bytes"].get("cuda", 0)
    # Existing full49 CUDA peak rounded to 32 GiB, reserved here as ADDITIONAL
    # non-weight working memory. A separate 16 GiB host/cgroup uncertainty pad.
    growth = max(0, device_bytes + 32 * GIB - cuda_allocated_bytes)
    remaining = host_available_bytes - growth
    projected_cgroup = max(current_bytes, 58 * GIB) + 16 * GIB
    placement_ok = (not inventory["non_cuda_parameters"]
                    and not inventory["layerwise_managers"]
                    and not inventory["host_store_entries"]
                    and inventory["unique_storage_bytes"].get("cpu", 0) < GIB
                    and 28 * GIB <= device_bytes <= 34 * GIB
                    and any("float8" in dtype for dtype in inventory["parameter_dtypes"]))
    return {"ok": placement_ok and remaining >= 28 * GIB and projected_cgroup + 12 * GIB <= limit_bytes,
            "placement_ok": placement_ok, "activation_growth_bytes": growth,
            "host_remaining_after_growth_bytes": remaining,
            "host_required_reserve_bytes": 28 * GIB,
            "cgroup_projected_bytes": projected_cgroup, "limit_bytes": limit_bytes}


def parameter_sample(model):
    """Endpoint digest only; neither full-weight hashing nor forward equivalence."""
    import hashlib
    import torch
    digest = hashlib.sha256()
    count = 0
    for name, tensor in model.named_parameters():
        tensor = tensor.detach()
        digest.update(repr((name, str(tensor.dtype), tuple(tensor.shape))).encode())
        if tensor.numel():
            for pos in (tuple(0 for _ in tensor.shape), tuple(n-1 for n in tensor.shape)):
                digest.update(tensor[pos].cpu().reshape(1).view(torch.uint8).numpy().tobytes())
        count += 1
    return {"sha256": digest.hexdigest(), "checked_parameters": count}


def whole_check(model, server_args, phase):
    import torch
    rank = torch.distributed.get_rank()
    row = {"phase": phase, "rank": rank}
    try:
        if server_args.performance_mode != "memory" or server_args.residency_mode("transformer") != "resident":
            raise ValueError("Unexpected whole DiT configuration")
        for name in ("text_encoder", "video_vae"):
            if server_args.residency_mode(name) != "layerwise-offload":
                raise ValueError("Auxiliary placement changed: " + name)
        inv = resident_inventory(model)
        cg = Path("/sys/fs/cgroup")
        mem = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        budget = resident_budget(inventory=inv,
            host_available_bytes=int(mem["MemAvailable"].split()[0])*1024,
            current_bytes=int((cg/"memory.current").read_text()),
            limit_bytes=int((cg/"memory.max").read_text()),
            cuda_allocated_bytes=torch.cuda.memory_allocated())
        row.update(inventory=inv, budget=budget, ok=budget["ok"],
                   cuda_allocated_bytes=torch.cuda.memory_allocated())
    except Exception as exc:
        row.update(ok=False, error=str(exc))
    peers = [None] * torch.distributed.get_world_size()
    torch.distributed.all_gather_object(peers, row)
    row["peer_ok"] = [p["ok"] for p in peers]
    return row


def initialize(modules, server_args):
    """Called after normal loader and offload setup; no placement mutations."""
    if os.environ.get("H3_MEMORY_PLACEMENT") != WHOLE_CANDIDATE:
        return
    global _WHOLE_PARAMETER_SAMPLE
    import torch
    rank = torch.distributed.get_rank()
    root = Path("/cache/h3-diagnostics") / ("load-" + os.environ["H3_MEMORY_RUN_ID"]) / f"rank{rank}"
    root.mkdir(parents=True, exist_ok=True)
    model = modules["transformer"]
    row = whole_check(model, server_args, "after_load")
    (root / "resident-load.json").write_text(json.dumps(row, indent=2))
    if not all(row["peer_ok"]):
        raise RuntimeError("Whole DiT placement/budget rejected at load; see resident-load.json")
    try:
        sample = {"ok": True, **parameter_sample(model)}
    except Exception as exc:
        sample = {"ok": False, "error": str(exc)}
    peers = [None] * torch.distributed.get_world_size()
    torch.distributed.all_gather_object(peers, sample)
    (root / "parameter-sample.json").write_text(json.dumps({"local": sample, "peers": peers}, indent=2))
    if not all(p.get("ok") and p == sample for p in peers):
        raise RuntimeError("Whole DiT replicated parameter samples differ across ranks")
    _WHOLE_PARAMETER_SAMPLE = {k: v for k, v in sample.items() if k != "ok"}


def whole_start(model, batch, server_args):
    import torch
    rank = torch.distributed.get_rank()
    options = batch.extra.get("h3_performance_diagnostic")
    run_id = options["id"] if options else "full-" + os.environ["H3_MEMORY_RUN_ID"]
    root = Path("/cache/h3-diagnostics") / run_id / f"rank{rank}"
    root.mkdir(parents=True, exist_ok=True)
    batch.extra.update(_h3_memory_root=root, _h3_memory_model=model, _h3_whole_resident=True)
    row = whole_check(model, server_args, "before_stage")
    _record(batch, row)
    if not all(row["peer_ok"]) or _WHOLE_PARAMETER_SAMPLE is None:
        raise RuntimeError("Whole DiT placement/budget rejected before denoise")
    whole_verify(batch)


def whole_verify(batch):
    import torch
    try:
        sample = parameter_sample(batch.extra["_h3_memory_model"])
        local = {"phase": "resident_parameter_samples", **sample,
                 "ok": sample == _WHOLE_PARAMETER_SAMPLE}
    except Exception as exc:
        local = {"phase": "resident_parameter_samples", "ok": False, "error": str(exc)}
    peers = [None] * torch.distributed.get_world_size()
    torch.distributed.all_gather_object(peers, local)
    _record(batch, local)
    if not all(p["ok"] for p in peers):
        raise RuntimeError("Whole DiT parameter endpoint sample changed")
