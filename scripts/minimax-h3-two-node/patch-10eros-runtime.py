#!/usr/bin/env python3
"""Apply the pinned, minimal 10Eros pruned-curve adaptation to vLLM-Omni."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


PREIMAGE_SHA256 = "5cdc9078215f1d07fe617d8dba6309c64f1987e8f83c82b0e22c50ac062f3826"


def replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise RuntimeError(f"runtime patch preimage count drifted: {source.count(old)}")
    return source.replace(old, new, 1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    raw = args.path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != PREIMAGE_SHA256:
        raise RuntimeError("vLLM MiniMax H3 source preimage drifted")
    source = raw.decode("utf-8")
    source = replace_once(
        source,
        "    final_norm_eps: float = 1e-5\n",
        "    final_norm_eps: float = 1e-5\n    use_adaln_curve: bool = False\n    adaln_curve_grid: int = 0\n    adaln_curve_dim: int = 8\n",
    )
    source = replace_once(
        source,
        "    if name.endswith(\".attn.qkv_proj.weight\"):\n        return _reorder_grouped_qkv_to_qkv(\n",
        "    if name.endswith(\".attn.qkv_proj.weight\"):\n        # 10Eros is published for ComfyUI with contiguous Q | K | V rows.\n        # Reordering it as the legacy grouped SGLang layout corrupts attention.\n        if arch.use_adaln_curve:\n            return weight\n        return _reorder_grouped_qkv_to_qkv(\n",
    )
    source = replace_once(
        source,
        "        self.hidden_size = arch.hidden_size\n        self.linear = ColumnParallelLinear(\n            arch.time_embed_dim,\n            out_features,\n            bias=True,\n            gather_output=True,\n            params_dtype=_BF16_DTYPE,\n            quant_config=quant_config,\n        )\n",
        "        self.hidden_size = arch.hidden_size\n        self.apply_silu = not arch.use_adaln_curve\n        self.compute_dtype = _FP32_DTYPE if arch.use_adaln_curve else _BF16_DTYPE\n        self.linear = ColumnParallelLinear(\n            arch.adaln_curve_dim if arch.use_adaln_curve else arch.time_embed_dim,\n            out_features,\n            bias=True,\n            gather_output=True,\n            params_dtype=self.compute_dtype,\n            quant_config=None if arch.use_adaln_curve else quant_config,\n        )\n",
    )
    source = replace_once(
        source,
        "        x = nn.functional.silu(t_emb)\n        # Online FP8 replaces ``weight`` with an FP8 parameter after loading;\n        # activations must remain BF16 and be quantized by the linear kernel.\n        x, _ = self.linear(x.to(_BF16_DTYPE))\n        m = x.shape[0]\n        x = x.view(m * self.modality_num, self.expand_ratio * self.hidden_size)\n        return tuple(x.chunk(self.expand_ratio, dim=-1))\n",
        "        x = nn.functional.silu(t_emb) if self.apply_silu else t_emb\n        # The curve reference projects its eight coordinates in FP32, then\n        # casts modulation rows back to BF16 before applying them.\n        x, _ = self.linear(x.to(self.compute_dtype))\n        m = x.shape[0]\n        x = x.view(m * self.modality_num, self.expand_ratio * self.hidden_size)\n        if self.compute_dtype == _FP32_DTYPE:\n            return tuple(item.to(_BF16_DTYPE) for item in x.chunk(self.expand_ratio, dim=-1))\n        return tuple(x.chunk(self.expand_ratio, dim=-1))\n",
    )
    source = replace_once(
        source,
        "        self.time_embedder = MiniMaxH3TimeEmbedder(arch, quant_config)\n        self.rope = MiniMaxH3Rope(arch.rope_inv_freq_len)\n",
        "        if arch.use_adaln_curve:\n            if arch.adaln_curve_grid < 2 or arch.adaln_curve_dim != 8:\n                raise ValueError(\"invalid 10Eros AdaLN curve contract\")\n            self.register_buffer(\"adaln_t_table\", torch.empty(arch.adaln_curve_grid, arch.adaln_curve_dim, dtype=_FP32_DTYPE))\n        else:\n            self.time_embedder = MiniMaxH3TimeEmbedder(arch, quant_config)\n        self.rope = MiniMaxH3Rope(arch.rope_inv_freq_len)\n",
    )
    source = replace_once(
        source,
        "            param = params.get(name)\n            if param is None:\n                logger.warning(\"Skipping MiniMax H3 weight not present in model: %s\", name)\n                continue\n",
        "            param = params.get(name)\n            if param is None:\n                if self.arch.use_adaln_curve and name in {\"adaln_basis\", \"adaln_mean\"}:\n                    loaded.add(name)\n                    continue\n                logger.warning(\"Skipping MiniMax H3 weight not present in model: %s\", name)\n                continue\n",
    )
    source = replace_once(
        source,
        "        t_emb = self.time_embedder(unique_timesteps)\n        return embeddings, t_emb\n",
        "        if self.arch.use_adaln_curve:\n            table = self.adaln_t_table.to(device=device, dtype=_FP32_DTYPE)\n            positions = unique_timesteps.to(device=device, dtype=_FP32_DTYPE).clamp(0.0, 1.0) * (table.shape[0] - 1)\n            lower = positions.floor().long().clamp(max=table.shape[0] - 2)\n            fraction = (positions - lower).unsqueeze(1)\n            t_emb = torch.lerp(table[lower], table[lower + 1], fraction)\n        else:\n            t_emb = self.time_embedder(unique_timesteps)\n        return embeddings, t_emb\n",
    )
    args.path.write_text(source, encoding="utf-8")
    print(hashlib.sha256(source.encode()).hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
