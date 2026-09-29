"""Opt-in H3 measurements. No sampling, weight, or topology changes.

Times are rank-local; overlapping CUDA streams must not be added together.
The Gateway owns termination. This module never returns partial video outputs.
"""
from contextlib import contextmanager
from functools import wraps
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time


def validate_options(options):
    if options is None:
        return None
    if not isinstance(options, dict) or not {"id", "trace_step"} <= set(options) or set(options) - {"id", "trace_step", "attention_probe"}:
        raise ValueError("invalid H3 diagnostic options")
    if not isinstance(options["id"], str) or not re.fullmatch(r"[a-f0-9]{32}", options["id"]):
        raise ValueError("invalid H3 diagnostic id")
    if type(options["trace_step"]) is not int or not -1 <= options["trace_step"] <= 5:
        raise ValueError("trace_step must be -1 (off) or a zero-based step 0..5")
    if "attention_probe" in options and type(options["attention_probe"]) is not bool:
        raise ValueError("attention_probe must be boolean")
    return dict(options)


def _atomic(path, row):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(row))
    temporary.replace(path)


def _sample_resources(root, stop):
    previous = None
    while not stop.is_set():
        now = time.monotonic()
        row = {"wall_time": time.time(), "monotonic": now}
        try:
            fields = "temperature.gpu,clocks.sm,clocks.mem,power.draw,utilization.gpu,clocks_event_reasons.sw_thermal_slowdown,clocks_event_reasons.hw_thermal_slowdown"
            value = subprocess.run(["nvidia-smi", "--query-gpu=" + fields, "--format=csv,noheader,nounits"],
                                   capture_output=True, text=True, timeout=4)
            row["gpu_fields"] = fields.split(",")
            row["gpu_values"] = value.stdout.strip()
            row["gpu_error"] = value.stderr.strip()[:300] if value.returncode else None
            mem = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
            row["memory_kib"] = {key: int(mem[key].split()[0]) for key in ("MemAvailable", "SwapTotal", "SwapFree")}
            vm = dict(line.split() for line in Path("/proc/vmstat").read_text().splitlines())
            counters = (int(vm["pswpin"]), int(vm["pswpout"]))
            row["swap_pages_total"] = counters
            if previous:
                row["swap_pages_per_second"] = [(a-b)/(now-previous[0]) for a,b in zip(counters, previous[1])]
            previous = (now, counters)
            for name in ("memory.current", "memory.peak", "memory.events", "memory.pressure", "memory.max"):
                path = Path("/sys/fs/cgroup") / name
                if path.exists():
                    row[name] = path.read_text().strip()
        except Exception as exc:
            row["error"] = str(exc)[:300]
        with (root / "resources.jsonl").open("a") as handle:
            handle.write(json.dumps(row) + "\n")
        stop.wait(10)


class Diagnostic:
    def __init__(self, options):
        import torch
        self.options = validate_options(options)
        self.rank = torch.distributed.get_rank() if torch.distributed.is_initialized() else 0
        self.root = Path("/cache/h3-diagnostics") / options["id"] / f"rank{self.rank}"
        self.root.mkdir(parents=True, exist_ok=True)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=_sample_resources, args=(self.root, self.stop_event), daemon=True)
        self.thread.start()
        self.attention_restore = None
        self.fp8_restore = None
        if os.environ.get("H3_FP8_PROBE") == "triton-m128n256k64-scalealigned-v2":
            if options.get("attention_probe") or options["trace_step"] != -1:
                raise ValueError("FP8 probe requires a dedicated trace-free diagnostic")
            from sglang.multimodal_gen.runtime.utils import h3_fp8_probe
            self.fp8_restore = h3_fp8_probe.install(self.root)
        if options.get("attention_probe"):
            if os.environ.get("H3_ATTENTION_PROBE") == "m256n64-t256-v1":
                self.attention_restore = install_next_attention_probe(self.root)
            else:
                self.attention_restore = install_attention_probe(self.root)

    @contextmanager
    def step(self, index):
        import torch
        # Only step boundaries synchronize. No layer/collective synchronization.
        torch.cuda.synchronize()
        trace = None
        if index == self.options["trace_step"]:
            trace = torch.profiler.profile(activities=[torch.profiler.ProfilerActivity.CPU,
                                                      torch.profiler.ProfilerActivity.CUDA],
                                           record_shapes=False, with_stack=False, profile_memory=False)
            trace.start()
        start_event, end_event = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
        start_event.record()
        started = time.perf_counter()
        completed = False
        try:
            yield
            completed = True
        finally:
            end_event.record()
            torch.cuda.synchronize()
            ended = time.perf_counter()
            row = {"rank": self.rank, "step_index": index, "completed_steps": index + int(completed),
                   "step_succeeded": completed,
                   "wall_time": time.time(), "rank_local_seconds": ended-started,
                   "current_stream_ms": start_event.elapsed_time(end_event), "trace": trace is not None,
                   "allocated_bytes": torch.cuda.memory_allocated(),
                   "reserved_bytes": torch.cuda.memory_reserved(),
                   "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
                   "peak_reserved_bytes": torch.cuda.max_memory_reserved()}
            if trace:
                trace.stop()
                trace.export_chrome_trace(str(self.root / f"step-{index}.json.gz"))
                row["trace_export_seconds"] = time.perf_counter()-ended
            with (self.root / "steps.jsonl").open("a") as handle:
                handle.write(json.dumps(row) + "\n")
            _atomic(self.root / "progress.json", row)

    def close(self):
        self.stop_event.set()
        if self.fp8_restore:
            self.fp8_restore()
        if self.attention_restore:
            self.attention_restore()


def install_attention_probe(root):
    """Compare two tiles on the first layer's four real Ring KV chunks.

    Always return the unmodified baseline result. Extra GPU calls are bounded,
    recorded as diagnostic overhead and must never count as speedup evidence.
    """
    import inspect
    import torch
    from sglang.kernels.ops.attention.flash_attn.cute import interface
    from sglang.kernels.ops.attention.fa4_sm120.flash_fwd import FlashAttentionForwardSm120 as Kernel
    original = interface._flash_attn_fwd
    signature = inspect.signature(original)
    original_threads = Kernel.get_fwd_num_threads
    calls = 0

    @wraps(original)
    def run(*args, **kwargs):
        nonlocal calls
        arguments = dict(signature.bind_partial(*args, **kwargs).arguments)
        q, k = arguments.get("q"), arguments.get("k")
        if (calls >= 4 or q is None or k is None or q.dtype != torch.bfloat16
                or q.shape[-1] != 128 or q.numel() < 1000000
                or arguments.get("causal", False)):
            return original(*args, **kwargs)
        calls += 1
        baseline = original(*args, **kwargs)
        # Fresh output allocations preserve any caller-owned output buffers.
        clean = {**arguments, "out": None, "lse": None}
        row = {"call": calls, "q_shape": list(q.shape), "k_shape": list(k.shape),
               "q_stride": list(q.stride()), "return_lse": arguments.get("return_lse"),
               "candidates": []}
        try:
            for threads in (128, 256):
                def selected(hd, hdv, tm, tn, paged_kv=False):
                    if (hd, hdv, tm, tn, paged_kv) == (128, 128, 128, 64, False):
                        return threads
                    return original_threads(hd, hdv, tm, tn, paged_kv=paged_kv)
                Kernel.get_fwd_num_threads = staticmethod(selected)
                candidate_args = {**clean, "tile_mn": (128, 64)}
                # Compile/warm separately, then interleave A/B timings.
                warm = original(**candidate_args)
                torch.cuda.synchronize()
                equal = [a is None and b is None or a is not None and b is not None and torch.equal(a,b)
                         for a,b in zip(baseline,warm)]
                max_abs = [float((a.float()-b.float()).abs().max()) if a is not None and b is not None else None
                           for a,b in zip(baseline,warm)]
                del warm
                timings = []
                for tile in ((64,64), (128,64), (64,64), (128,64)):
                    begin, end = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
                    begin.record()
                    value = original(**{**clean, "tile_mn": tile})
                    end.record(); end.synchronize()
                    timings.append({"tile": tile, "ms": begin.elapsed_time(end)})
                    del value
                row["candidates"].append({"tile": [128,64], "threads": threads,
                                           "bitwise_equal": equal, "max_abs": max_abs,
                                           "timings": timings})
        except Exception as exc:
            row["error"] = str(exc)[:1000]
            raise
        finally:
            Kernel.get_fwd_num_threads = staticmethod(original_threads)
            with (root / "attention-probe.jsonl").open("a") as handle:
                handle.write(json.dumps(row) + "\n")
        return baseline

    interface._flash_attn_fwd = run
    def restore():
        interface._flash_attn_fwd = original
        Kernel.get_fwd_num_threads = staticmethod(original_threads)
    return restore


@contextmanager
def diagnostic_step(batch, index):
    options = batch.extra.get("h3_performance_diagnostic")
    if os.environ.get("H3_ATTENTION_CANDIDATE") == "m128n64-t256-15s":
        if (options and options.get("attention_probe")
                and os.environ.get("H3_ATTENTION_PROBE") != "m256n64-t256-v1"):
            raise ValueError("Disable the attention candidate before running a baseline probe")
        if "_h3_attention_restore" not in batch.extra:
            batch.extra["_h3_attention_restore"] = install_attention_candidate()
    if not options:
        yield
        return
    diagnostic = batch.extra.get("_h3_diagnostic_instance")
    if diagnostic is None:
        diagnostic = Diagnostic(options)
        batch.extra["_h3_diagnostic_instance"] = diagnostic
    with diagnostic.step(index):
        yield
    # Correctness/placement evidence runs after the timed region. No extra
    # forward or weight probe contributes to the reported step duration.
    if batch.extra.get("_h3_memory_model") is not None and index < 2:
        from sglang.multimodal_gen.runtime.utils import h3_memory_placement
        h3_memory_placement.state(batch, "after_step", index)
        if index == 0:
            h3_memory_placement.verify_samples(batch)


def close_diagnostic(batch):
    diagnostic = batch.extra.pop("_h3_diagnostic_instance", None)
    if diagnostic:
        diagnostic.close()
    restore = batch.extra.pop("_h3_attention_restore", None)
    if restore:
        restore()


def candidate_matches(arguments, bf16):
    """Only the measured 15-second H3 Ring4 shape; all others keep defaults."""
    q, k, v = (arguments.get(name) for name in ("q", "k", "v"))
    return (q is not None and k is not None and v is not None
            and q.dtype == k.dtype == v.dtype == bf16
            and tuple(q.shape) == (62496, 56, 128)
            and tuple(k.shape) in ((62496, 56, 128), (62475, 56, 128))
            and tuple(v.shape) == tuple(k.shape)
            and tuple(q.stride()) == (21504, 128, 1)
            and not arguments.get("causal", False)
            and arguments.get("return_lse") is True
            and arguments.get("tile_mn") is None)


def install_attention_candidate():
    """Apply the measured tile only during this H3 request, without extra work."""
    import inspect
    import torch
    from sglang.kernels.ops.attention.flash_attn.cute import interface
    from sglang.kernels.ops.attention.fa4_sm120.flash_fwd import FlashAttentionForwardSm120 as Kernel
    original = interface._flash_attn_fwd
    signature = inspect.signature(original)
    original_threads = Kernel.get_fwd_num_threads
    matched = 0
    fallback = 0

    def selected(hd, hdv, tm, tn, paged_kv=False):
        if (hd, hdv, tm, tn, paged_kv) == (128, 128, 128, 64, False):
            return 256
        return original_threads(hd, hdv, tm, tn, paged_kv=paged_kv)

    @wraps(original)
    def run(*args, **kwargs):
        nonlocal matched, fallback
        arguments = dict(signature.bind_partial(*args, **kwargs).arguments)
        if not candidate_matches(arguments, torch.bfloat16):
            fallback += 1
            return original(*args, **kwargs)
        matched += 1
        Kernel.get_fwd_num_threads = staticmethod(selected)
        try:
            return original(**{**arguments, "tile_mn": (128, 64)})
        finally:
            Kernel.get_fwd_num_threads = staticmethod(original_threads)

    interface._flash_attn_fwd = run
    print("H3 attention candidate enabled: m128n64-t256-15s", flush=True)
    def restore():
        interface._flash_attn_fwd = original
        Kernel.get_fwd_num_threads = staticmethod(original_threads)
        print(f"H3 attention candidate restored: matched={matched} fallback={fallback}", flush=True)
    return restore


NEXT_ATTENTION_TILE = (256, 64)


def install_next_attention_probe(root):
    """One bounded next candidate; always return the current production output.

    K reduction length remains 64. Larger M may improve KV reuse, but register
    pressure can negate that. Only measured bitwise-equal results may win.
    """
    import inspect
    import torch
    from sglang.kernels.ops.attention.flash_attn.cute import interface
    from sglang.kernels.ops.attention.fa4_sm120.flash_fwd import FlashAttentionForwardSm120 as Kernel
    original = interface._flash_attn_fwd  # wraps the retained 128x64 candidate
    signature = inspect.signature(original)
    original_threads = Kernel.get_fwd_num_threads
    calls = 0
    def selected(hd, hdv, tm, tn, paged_kv=False):
        if hd == hdv == 128 and tm in (128, 256) and tn == 64 and not paged_kv:
            return 256
        return original_threads(hd, hdv, tm, tn, paged_kv=paged_kv)

    @wraps(original)
    def run(*args, **kwargs):
        nonlocal calls
        arguments = dict(signature.bind_partial(*args, **kwargs).arguments)
        if calls >= 4 or not candidate_matches(arguments, torch.bfloat16):
            return original(*args, **kwargs)
        calls += 1
        baseline = original(*args, **kwargs)
        clean = {**arguments, "out": None, "lse": None}
        row = {"call": calls, "baseline_tile": [128, 64], "candidate_tile": list(NEXT_ATTENTION_TILE),
               "threads": 256, "q_shape": list(arguments["q"].shape),
               "k_shape": list(arguments["k"].shape), "q_stride": list(arguments["q"].stride())}
        try:
            Kernel.get_fwd_num_threads = staticmethod(selected)
            warm = original(**{**clean, "tile_mn": NEXT_ATTENTION_TILE})
            torch.cuda.synchronize()
            row["bitwise_equal"] = [a is None and b is None or a is not None and b is not None and torch.equal(a,b)
                                    for a,b in zip(baseline,warm)]
            row["max_abs"] = [float((a.float()-b.float()).abs().max()) if a is not None and b is not None else None
                              for a,b in zip(baseline,warm)]
            del warm
            timings=[]
            for tile in ((128,64), NEXT_ATTENTION_TILE, (128,64), NEXT_ATTENTION_TILE):
                begin,end=torch.cuda.Event(enable_timing=True),torch.cuda.Event(enable_timing=True)
                begin.record()
                value=original(**{**clean,"tile_mn":tile})
                end.record();end.synchronize()
                timings.append({"tile":list(tile),"ms":begin.elapsed_time(end)})
                del value
            row["timings"]=timings
        except Exception as exc:
            row["error"]=str(exc)[:1500]
            # A compile/resource rejection is evidence against this candidate.
            # Do not silently select it or change the original trajectory.
            raise
        finally:
            Kernel.get_fwd_num_threads=staticmethod(original_threads)
            with (root/"attention-next-probe.jsonl").open("a") as handle:
                handle.write(json.dumps(row)+"\n")
        return baseline

    interface._flash_attn_fwd=run
    def restore():
        interface._flash_attn_fwd=original
        Kernel.get_fwd_num_threads=staticmethod(original_threads)
    return restore
