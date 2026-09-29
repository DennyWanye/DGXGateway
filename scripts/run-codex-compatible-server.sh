#!/usr/bin/env bash
set -euo pipefail

profile=${1:?usage: run-codex-compatible-server.sh gemma4|qwen38-q8-fastmtp}
qwen_root=${DGX_MODEL_ROOT:-/home/operator/dgx-models/qwen38-27b}
matrix_root=${DGX_MATRIX_ROOT:-/home/operator/dgx-models/model-matrix}
host=${CODEX_HOST:-127.0.0.1}
port=${CODEX_PORT:-18084}
gemma_temp=${CODEX_GEMMA_TEMP:-0.2}
context_size=${CODEX_CONTEXT_SIZE:-262144}

case "$profile" in
  gemma4)
    binary="$qwen_root/src/llama.cpp-codex-gemma/build-codex/bin/llama-server"
    model="$matrix_root/models/Gemma4-26B-A4B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M.gguf"
    draft="$matrix_root/models/mtp-gemma-4-26B-A4B-it.gguf"
    alias=${CODEX_ALIAS:-gemma4-codex-200k:candidate}
    profile_args=(
      --spec-draft-model "$draft" --spec-draft-ngl all
      --cache-type-k f16 --cache-type-v f16 --load-mode none
      --spec-type draft-mtp
      --temp "$gemma_temp" --top-k 64 --top-p 0.9 --min-p 0.05
      --repeat-penalty 1.1
    )
    ;;
  qwen38-q8-fastmtp)
    binary="$qwen_root/src/llama.cpp-codex-fastmtp/build-codex/bin/llama-server"
    model="$qwen_root/models/Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-Q8_K_P-Ollama-CCSwitch.gguf"
    draft="$qwen_root/models/Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-FastMTP-32K.gguf"
    alias=${CODEX_ALIAS:-qwen38-q8-fastmtp-codex-200k:candidate}
    profile_args=(
      --spec-draft-model "$draft" --spec-draft-ngl all
      --cache-type-k f16 --cache-type-v f16 --no-mmap
      --spec-type draft-mtp --spec-draft-n-max 3 --spec-draft-p-min 0
      --temp 1.0 --top-k 20 --top-p 0.95 --min-p 0
      --presence-penalty 0 --repeat-penalty 1.0
    )
    ;;
  *)
    echo "unknown Codex candidate profile: $profile" >&2
    exit 2
    ;;
esac

[[ -x $binary && -f $model && -f $draft ]]

exec "$binary" \
  --model "$model" --alias "$alias" \
  --ctx-size "$context_size" --parallel 1 --batch-size 2048 --ubatch-size 512 \
  --n-gpu-layers all --split-mode none --flash-attn on \
  "${profile_args[@]}" \
  --jinja --reasoning on --reasoning-effort low --reasoning-preserve \
  --reasoning-format deepseek \
  --host "$host" --port "$port"
