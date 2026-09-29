#!/usr/bin/env python3
"""Small real GB10 kernels + import probe. No model weights, no fallback mutation."""
import argparse
import importlib
import importlib.metadata
import json
from pathlib import Path
import platform
import sys
import time
import traceback


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--kernels-only", action="store_true", help="Skip Pipeline import; does not satisfy the full runtime gate")
    args = parser.parse_args()
    report = {"status": "FAIL", "seed": 1949, "tests": [], "started_epoch": time.time(), "platform": platform.platform(), "scope": "kernels_only" if args.kernels_only else "kernels_and_pipeline", "pipeline_import_tested": False}
    try:
        import torch
        import torch.nn.functional as F
        from torch.nn.attention import sdpa_kernel, SDPBackend
        from flash_attn import flash_attn_func, flash_attn_varlen_func
        report["torch"] = torch.__version__
        report["cuda"] = torch.version.cuda
        report["flash_attn"] = importlib.metadata.version("flash-attn")
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA unavailable")
        report["device"] = torch.cuda.get_device_name(0)
        report["capability"] = list(torch.cuda.get_device_capability(0))
        if report["capability"] != [12, 1] or "GB10" not in report["device"]:
            raise RuntimeError("Expected NVIDIA GB10 capability 12.1")
        torch.manual_seed(report["seed"])
        torch.cuda.manual_seed_all(report["seed"])
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False

        def check(name, actual, reference, max_abs=0.03, rel_rms=0.015):
            torch.cuda.synchronize()
            finite = bool(torch.isfinite(actual).all() and torch.isfinite(reference).all())
            diff = actual.float() - reference.float()
            max_error = diff.abs().max().item()
            rms_error = diff.square().mean().sqrt().item()
            ref_rms = reference.float().square().mean().sqrt().item()
            relative = rms_error / max(ref_rms, 1e-8)
            passed = finite and max_error <= max_abs and relative <= rel_rms
            report["tests"].append({"name": name, "shape": list(actual.shape), "finite": finite,
                 "max_abs_error": max_error, "relative_rms_error": relative,
                 "reference_rms": ref_rms, "max_abs_limit": max_abs, "relative_rms_limit": rel_rms, "pass": passed})
            if not passed:
                raise RuntimeError(f"Numerical gate failed: {name}")

        def tensor(*shape):
            return torch.randn(*shape, device="cuda", dtype=torch.bfloat16)

        # Scale matrices to keep the reference output near unit variance.
        a, b = tensor(128, 256) / 16, tensor(256, 128)
        check("bf16_matmul", a @ b, a.float() @ b.float())
        def reference(q, k, v):
            with sdpa_kernel(SDPBackend.MATH):
                return F.scaled_dot_product_attention(q.float().transpose(1, 2), k.float().transpose(1, 2), v.float().transpose(1, 2), dropout_p=0).transpose(1, 2)

        with torch.inference_mode():
            for qlen, klen in [(128, 128), (93, 157)]:
                q, k, v = tensor(2, qlen, 4, 128), tensor(2, klen, 4, 128), tensor(2, klen, 4, 128)
                actual = flash_attn_func(q, k, v, dropout_p=0.0, causal=False)
                check(f"fa_dense_q{qlen}_k{klen}", actual, reference(q, k, v))
            qlens, klens = [93, 71], [57, 121]
            qs = [tensor(1, length, 4, 128) for length in qlens]
            ks = [tensor(1, length, 4, 128) for length in klens]
            vs = [tensor(1, length, 4, 128) for length in klens]
            q, k, v = [torch.cat([x.squeeze(0) for x in seq], dim=0) for seq in [qs, ks, vs]]
            cq = torch.tensor([0, qlens[0], sum(qlens)], device="cuda", dtype=torch.int32)
            ck = torch.tensor([0, klens[0], sum(klens)], device="cuda", dtype=torch.int32)
            actual = flash_attn_varlen_func(q, k, v, cq, ck, max(qlens), max(klens), dropout_p=0.0, causal=False)
            expected = torch.cat([reference(qi, ki, vi).squeeze(0) for qi, ki, vi in zip(qs, ks, vs)])
            check("fa_varlen_unequal_lengths", actual, expected)
        if not args.kernels_only:
            sys.path.insert(0, str(args.source_dir.resolve()))
            pipeline = importlib.import_module("longcat_video.pipeline_longcat_video_avatar")
            if not hasattr(pipeline, "LongCatVideoAvatarPipeline"):
                raise RuntimeError("Missing expected Pipeline class")
            report["tests"].append({"name": "pipeline_import", "pass": True})
            report["pipeline_import_tested"] = True
        report["status"] = "PASS"
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()
    finally:
        report["finished_epoch"] = time.time()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
