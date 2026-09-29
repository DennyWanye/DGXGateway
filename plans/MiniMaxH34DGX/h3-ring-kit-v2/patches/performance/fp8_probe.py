"""Bounded FP8 GEMM comparison on real DiT operands; returns baseline only."""
from functools import wraps
import json
import math
import time

CANDIDATE = 'triton-m128n256k64-scalealigned-v2'
CONFIG = dict(block_size_m=128, block_size_n=256, block_size_k=64,
              num_warps=8, num_stages=3, use_heuristic=False)


def eligible(a, b, sa, sb, dtype, bias, torch):
    if (a.ndim != 2 or b.ndim != 2 or a.dtype != torch.float8_e4m3fn
            or b.dtype != a.dtype or dtype != torch.bfloat16 or bias is not None):
        return False
    m,k = a.shape; kb,n = b.shape
    return (m == 62496 and k == kb and k in (7168,14336)
            and n == 5376 and k % 128 == n % 128 == 0
            and a.stride(1) == 1 and b.stride(0) == 1
            and sa.numel() == m and sb.numel() == n
            and sa.dtype == sb.dtype == torch.float32
            and a.device.type == 'cuda' and a.device == b.device == sa.device == sb.device)


def compare(a, b, torch):
    """Chunk error scans to avoid materializing several huge FP32 matrices."""
    if a.shape != b.shape or a.dtype != b.dtype:
        raise ValueError('Candidate output shape/dtype changed')
    equal = bool(torch.equal(a,b))
    max_abs = squared_error = squared_ref = 0.0
    different = 0
    finite = True
    for start in range(0,a.shape[0],1024):
        x=a[start:start+1024].float(); y=b[start:start+1024].float()
        finite = finite and bool(torch.isfinite(x).all()) and bool(torch.isfinite(y).all())
        d=x-y
        max_abs=max(max_abs,float(d.abs().max()))
        squared_error+=float(d.square().sum(dtype=torch.float64))
        squared_ref+=float(x.square().sum(dtype=torch.float64))
        different+=int((x!=y).sum())
    return dict(bitwise_equal=equal,finite=finite,max_abs=max_abs,
                relative_l2=math.sqrt(squared_error/max(squared_ref,1e-300)),
                different_elements=different,elements=a.numel())


def install(root):
    import torch
    from pathlib import Path
    from sglang.srt.layers.quantization import fp8_utils
    from sglang.multimodal_gen.runtime.utils.h3_fp8_aligned import triton_scaled_mm
    original=fp8_utils.fp8_scaled_mm
    seen=set()

    @wraps(original)
    def run(mat_a,mat_b,scales_a,scales_b,out_dtype,bias=None):
        baseline=original(mat_a,mat_b,scales_a,scales_b,out_dtype,bias)
        key=(tuple(mat_a.shape),tuple(mat_b.shape),tuple(mat_a.stride()),tuple(mat_b.stride()))
        if len(seen)>=2 or key in seen or not eligible(mat_a,mat_b,scales_a,scales_b,out_dtype,bias,torch):
            return baseline
        seen.add(key)
        row=dict(candidate=CANDIDATE,call=len(seen),wall_time=time.time(),
                 a_shape=list(mat_a.shape),b_shape=list(mat_b.shape),a_stride=list(mat_a.stride()),b_stride=list(mat_b.stride()),
                 scale_a_shape=list(scales_a.shape),scale_b_shape=list(scales_b.shape),config=CONFIG,
                 a_dtype=str(mat_a.dtype),b_dtype=str(mat_b.dtype),out_dtype=str(out_dtype),bias=False)
        try:
            mem=dict(line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines())
            available=int(mem['MemAvailable'].split()[0])*1024
            row['host_available_bytes']=available
            # Largest tested output is <=4GiB; retain room for probe outputs and
            # chunked error scans as well as 12GiB untouched host reserve.
            if available < 24*1024**3 or baseline.numel()*baseline.element_size()>4*1024**3:
                raise RuntimeError('FP8 probe memory budget refused')
            args=(mat_a,mat_b,scales_a,scales_b,out_dtype,bias)
            def candidate():
                return triton_scaled_mm(*args,**CONFIG)
            warm=candidate()
            torch.cuda.synchronize()
            row['comparison']=compare(baseline,warm,torch)
            del warm
            row['timings']=[]
            for name,fn in [('baseline',lambda:original(*args)),('candidate',candidate),
                            ('baseline',lambda:original(*args)),('candidate',candidate)]:
                begin,end=torch.cuda.Event(enable_timing=True),torch.cuda.Event(enable_timing=True)
                begin.record(); value=fn(); end.record(); end.synchronize()
                row['timings'].append(dict(backend=name,ms=begin.elapsed_time(end)))
                del value
        except Exception as exc:
            row['error']=str(exc)[:2000]
            raise
        finally:
            with (root/'fp8-probe.jsonl').open('a') as handle:
                handle.write(json.dumps(row)+'\n')
        return baseline

    fp8_utils.fp8_scaled_mm=run
    def restore():
        fp8_utils.fp8_scaled_mm=original
    return restore
