#!/usr/bin/env bash
set -euo pipefail

root="${DGX_MODEL_ROOT:-$HOME/dgx-models/qwen38-27b}"
binary="$root/src/llama.cpp-q8-256k/build-cuda/bin/llama-server"
model="$root/models/Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-Q8_K_P-Ollama-CCSwitch.gguf"
depth="${MTP_DEPTH:-3}"
p_min="${MTP_P_MIN:-0.5}"
ubatch="${UBATCH_SIZE:-1024}"
cache_reuse="${CACHE_REUSE:-0}"
spec_types="${SPEC_TYPES:-draft-mtp}"
spec_default="${SPEC_DEFAULT:-0}"
port="${Q8_TUNING_PORT:-18080}"

case "$depth" in 3|4|5) ;; *) echo "Invalid MTP_DEPTH: $depth" >&2; exit 2 ;; esac
case "$p_min" in 0.3|0.4|0.5|0.6) ;; *) echo "Invalid MTP_P_MIN: $p_min" >&2; exit 2 ;; esac
case "$ubatch" in 512|1024|2048) ;; *) echo "Invalid UBATCH_SIZE: $ubatch" >&2; exit 2 ;; esac
case "$cache_reuse" in 0|256) ;; *) echo "Invalid CACHE_REUSE: $cache_reuse" >&2; exit 2 ;; esac
case "$spec_types" in draft-mtp|draft-mtp,ngram-mod) ;; *) echo "Invalid SPEC_TYPES: $spec_types" >&2; exit 2 ;; esac
case "$spec_default" in 0|1) ;; *) echo "Invalid SPEC_DEFAULT: $spec_default" >&2; exit 2 ;; esac
[[ "$port" =~ ^[0-9]+$ ]] || { echo "Invalid Q8_TUNING_PORT: $port" >&2; exit 2; }
[[ -x "$binary" ]] || { echo "Missing llama-server: $binary" >&2; exit 1; }
[[ -f "$model" ]] || { echo "Missing Q8 model: $model" >&2; exit 1; }

default_args=()
if [[ "$spec_default" == 1 ]]; then
  default_args+=(--spec-default --reasoning-preserve)
fi

exec "$binary" \
  --model "$model" \
  --alias qwen38-q8-256k:latest \
  --ctx-size 262144 \
  --parallel 1 \
  --batch-size 2048 \
  --ubatch-size "$ubatch" \
  --n-gpu-layers all \
  --split-mode none \
  --flash-attn on \
  --cache-type-k f16 \
  --cache-type-v f16 \
  --load-mode none \
  --spec-type "$spec_types" \
  --spec-draft-n-max "$depth" \
  --spec-draft-p-min "$p_min" \
  "${default_args[@]}" \
  --cache-prompt \
  --cache-reuse "$cache_reuse" \
  --jinja \
  --reasoning auto \
  --reasoning-format deepseek \
  --host 127.0.0.1 \
  --port "$port"
