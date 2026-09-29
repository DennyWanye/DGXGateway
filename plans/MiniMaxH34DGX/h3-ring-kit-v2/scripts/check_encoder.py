#!/usr/bin/env python3
"""CPU config comparison for H3's 50-layer, 5120-wide Qwen3-VL feature extractor.
Passing this script is not a weight-loader, reference-fidelity or refusal-rate test.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path


def normalized(cfg: dict) -> dict:
    t, v = cfg["text_config"], cfg["vision_config"]
    r = t.get("rope_parameters", t.get("rope_scaling", {}))
    return {
        "model_type": cfg.get("model_type"),
        "architectures": cfg.get("architectures"),
        "text": {k: t.get(k) for k in ("hidden_size", "intermediate_size", "num_hidden_layers", "num_attention_heads", "num_key_value_heads", "head_dim", "vocab_size", "hidden_act", "rms_norm_eps")},
        "rope": {"theta": r.get("rope_theta", t.get("rope_theta")), "type": r.get("rope_type"),
                 "sections": r.get("mrope_section"), "interleaved": r.get("mrope_interleaved")},
        "vision": {k: v.get(k) for k in ("depth", "hidden_size", "intermediate_size", "out_hidden_size", "num_heads", "patch_size", "spatial_merge_size", "temporal_patch_size", "deepstack_visual_indexes")},
        "tokens": {k: cfg.get(k) for k in ("image_token_id", "video_token_id", "vision_start_token_id", "vision_end_token_id")},
    }


def check(official: Path, candidate: Path) -> dict:
    a, b = [json.loads((p / "config.json").read_text()) for p in (official, candidate)]
    for cfg in (a, b):
        if cfg.get("quantization_config") or cfg.get("text_config", {}).get("quantization_config"):
            raise ValueError("BF16 comparison profile must not silently introduce a prequantized encoder")
    na, nb = normalized(a), normalized(b)
    if nb["model_type"] != "qwen3_vl" or nb["text"]["hidden_size"] != 5120 or nb["text"]["num_hidden_layers"] < 50:
        raise ValueError("Not the required H3-compatible Qwen3-VL architecture")
    if na != nb:
        raise ValueError("Architecture/rope/token mapping mismatch: " + json.dumps({"official": na, "candidate": nb}))
    idx = candidate / "model.safetensors.index.json"
    if idx.exists():
        weights = json.loads(idx.read_text())["weight_map"]
        for shard in set(weights.values()):
            p = (candidate / shard).resolve()
            if not p.is_relative_to(candidate.resolve()) or not p.is_file():
                raise ValueError("Missing or unsafe shard in encoder index")
        if not any("visual." in n for n in weights):
            raise ValueError("Encoder index has no visual tower; do not substitute an LM-only export")
    elif not (candidate / "model.safetensors").is_file():
        raise ValueError("Expected complete safetensors checkpoint/index")
    return {"status": "PASS_CONFIG_ONLY", "normalized": nb,
            "tokenizer_policy": "Keep official H3 tokenizer/processor; verify resolved loader paths at runtime",
            "pending": ["GB10 native loader", "tensor key/shape coverage", "selected hidden-state layer", "multimodal encoding", "Ring4 generation and A/B quality"]}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--official", type=Path, required=True)
    p.add_argument("--candidate", type=Path, required=True)
    p.add_argument("--tokenizer", type=Path, help="Optional CPU check inside the installed runtime")
    a=p.parse_args()
    result=check(a.official,a.candidate)
    if a.tokenizer:
        from transformers import AutoTokenizer
        tok=AutoTokenizer.from_pretrained(str(a.tokenizer),local_files_only=True,trust_remote_code=False)
        vocab=tok.get_vocab()
        if "<d>" not in vocab:
            raise ValueError("Official H3 dialogue token <d> is missing")
        max_id=max(vocab.values())
        if max_id >= result["normalized"]["text"]["vocab_size"]:
            raise ValueError("H3 tokenizer contains ids outside candidate embedding vocabulary")
        result["tokenizer_check"]={"dialogue_token_id":vocab["<d>"],"max_token_id":max_id}
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=="__main__":main()
