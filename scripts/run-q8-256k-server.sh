#!/usr/bin/env bash
set -euo pipefail

root="${DGX_MODEL_ROOT:-$HOME/dgx-models/qwen38-27b}"
binary="$root/src/llama.cpp-q8-256k/build-cuda/bin/llama-server"
model="$root/models/Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-Q8_K_P-Ollama-CCSwitch.gguf"
host="${Q8_HOST:-192.0.2.30}"
port="${Q8_PORT:-11434}"

[[ -x "$binary" ]] || { echo "Missing llama-server: $binary" >&2; exit 1; }
[[ -f "$model" ]] || { echo "Missing Q8 model: $model" >&2; exit 1; }

exec "$binary" \
  --model "$model" \
  --alias qwen38-q8-256k:latest \
  --ctx-size 262144 \
  --parallel 1 \
  --batch-size 2048 \
  --ubatch-size 1024 \
  --n-gpu-layers all \
  --split-mode none \
  --flash-attn on \
  --cache-type-k f16 \
  --cache-type-v f16 \
  --load-mode none \
  --spec-type draft-mtp \
  --spec-draft-n-max 5 \
  --spec-draft-p-min 0.4 \
  --jinja \
  --reasoning auto \
  --reasoning-format deepseek \
  --host "$host" \
  --port "$port"
