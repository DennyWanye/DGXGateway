#!/usr/bin/env bash
set -euo pipefail

root=${DGX_MATRIX_ROOT:-/home/operator/dgx-models/model-matrix}
source_root=${DGX_UPSTREAM_ROOT:-/home/operator/dgx-models/qwen38-27b/src/llama.cpp}
binary="$source_root/build-cuda/bin/llama-server"
model="$root/models/Gemma4-26B-A4B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M.gguf"
draft="$root/models/mtp-gemma-4-26B-A4B-it.gguf"
host=${GEMMA_HOST:-192.0.2.30}
port=${GEMMA_PORT:-11434}

[[ $(git -C "$source_root" rev-parse HEAD) == 5ea87ddad22541a37053c7ba92b02ec1923617c6 ]]
[[ -x $binary ]]
[[ $(stat -c %s "$model") == 16796015520 ]]
[[ $(stat -c %s "$draft") == 251937728 ]]

exec "$binary" \
  --model "$model" --spec-draft-model "$draft" --spec-draft-ngl all \
  --alias gemma4-productivity-200k:latest \
  --ctx-size 204800 --parallel 1 --batch-size 2048 --ubatch-size 512 \
  --n-gpu-layers all --split-mode none --flash-attn on \
  --cache-type-k f16 --cache-type-v f16 --load-mode none \
  --spec-type draft-mtp \
  --temp 0.6 --top-k 64 --top-p 0.9 --min-p 0.05 \
  --repeat-penalty 1.1 --reasoning-budget 64 \
  --jinja --host "$host" --port "$port"
