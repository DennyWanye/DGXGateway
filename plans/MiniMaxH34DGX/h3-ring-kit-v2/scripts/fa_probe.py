#!/usr/bin/env python3
"""Exercise H3-shaped packed attention and LSE without loading model weights."""
import json
import math
import platform


def main() -> None:
    import torch
    from sglang.kernels.ops.attention.flash_attention import flash_attn_varlen_func
    from sglang.multimodal_gen.configs.models.dits.minimax_h3 import MiniMaxH3DiTArchConfig

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable; do not download/load H3 yet")
    torch.cuda.set_device(0)
    arch = MiniMaxH3DiTArchConfig()
    heads, dim = arch.num_attention_heads, arch.attention_head_dim
    lengths = [64, 96]
    cu = torch.tensor([0, 64, 160], dtype=torch.int32, device="cuda")
    report = {"machine": platform.machine(), "torch": torch.__version__,
              "cuda": torch.version.cuda, "gpu": torch.cuda.get_device_name(0),
              "capability": torch.cuda.get_device_capability(0),
              "heads": heads, "head_dim": dim, "tests": []}
    with torch.inference_mode():
        for dtype in (torch.float16, torch.bfloat16):
            torch.manual_seed(17)
            q, k, v = [torch.randn(sum(lengths), heads, dim, device="cuda", dtype=dtype) * 0.25
                       for _ in range(3)]
            result = flash_attn_varlen_func(
                q, k, v, cu_seqlens_q=cu, cu_seqlens_k=cu,
                max_seqlen_q=max(lengths), max_seqlen_k=max(lengths),
                softmax_scale=1.0 / math.sqrt(dim), causal=False,
                return_softmax_lse=True, ver=4,
            )
            if not isinstance(result, (tuple, list)) or len(result) != 2:
                raise RuntimeError("Expected (output, LSE); API/runtime does not match this probe")
            out, lse = result
            if out.shape != q.shape or lse.shape != (heads, sum(lengths)):
                raise RuntimeError(f"Unexpected output/LSE shapes: {out.shape}, {lse.shape}")
            refs, lses, start = [], [], 0
            for length in lengths:
                end = start + length
                qs, ks, vs = [x[start:end].float().transpose(0, 1) for x in (q, k, v)]
                scores = qs @ ks.transpose(-1, -2) / math.sqrt(dim)
                refs.append((scores.softmax(-1) @ vs).transpose(0, 1))
                lses.append(scores.logsumexp(-1))
                start = end
            reference = torch.cat(refs, dim=0)
            reference_lse = torch.cat(lses, dim=1)
            torch.cuda.synchronize()
            if not (torch.isfinite(out).all() and torch.isfinite(lse).all()):
                raise RuntimeError("Non-finite attention output or LSE")
            out_error = (out.float() - reference).abs().max().item()
            lse_error = (lse.float() - reference_lse).abs().max().item()
            # Smoke-test thresholds, not a video-quality guarantee.
            if out_error > 0.03 or lse_error > 0.03:
                raise RuntimeError(f"Attention numerical check failed: out={out_error}, LSE={lse_error}")
            report["tests"].append({"dtype": str(dtype), "max_abs_error": out_error,
                                     "max_lse_error": lse_error})
    report["status"] = "PASS_KERNEL_ONLY"
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
