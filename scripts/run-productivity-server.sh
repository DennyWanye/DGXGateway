#!/usr/bin/env bash
set -euo pipefail

profile=${1:?usage: run-productivity-server.sh PROFILE}
qwen_root=${DGX_MODEL_ROOT:-/home/operator/dgx-models/qwen38-27b}
script_root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
model="$qwen_root/models/Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-Q8_K_P-Ollama-CCSwitch.gguf"

case "$profile" in
  qwen38-q8)
    exec "$qwen_root/src/llama.cpp-q8-256k/build-cuda/bin/llama-server" \
      --model "$model" --alias qwen38-q8-productivity \
      --ctx-size 204800 --parallel 1 --batch-size 2048 --ubatch-size 1024 \
      --n-gpu-layers all --split-mode none --flash-attn on \
      --cache-type-k f16 --cache-type-v f16 --load-mode none --no-cache-prompt \
      --spec-type draft-mtp --spec-draft-n-max 5 --spec-draft-p-min 0.4 \
      --jinja --reasoning auto --reasoning-format deepseek \
      --host 127.0.0.1 --port 18082
    ;;
  qwen38-q8-fastmtp)
    source_root="$qwen_root/src/llama.cpp-fastmtp"
    draft="$qwen_root/models/Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-FastMTP-32K.gguf"
    [[ $(git -C "$source_root" rev-parse HEAD) == 4df29be4f4c3673f428170fda944a5b19f743bb8 ]]
    exec "$source_root/build-cuda/bin/llama-server" \
      --model "$model" --spec-draft-model "$draft" --spec-draft-ngl all \
      --alias qwen38-q8-fastmtp-productivity \
      --ctx-size 204800 --parallel 1 --batch-size 2048 --ubatch-size 512 \
      --n-gpu-layers all --split-mode none --flash-attn on \
      --cache-type-k f16 --cache-type-v f16 --no-mmap --no-cache-prompt \
      --spec-type draft-mtp --spec-draft-n-max 3 --spec-draft-p-min 0 \
      --temp 1.0 --top-k 20 --top-p 0.95 --min-p 0 \
      --presence-penalty 0 --repeat-penalty 1.0 \
      --jinja --reasoning on --reasoning-effort low \
      --reasoning-preserve --reasoning-format deepseek \
      --host 127.0.0.1 --port 18082
    ;;
  qwen38-q6|qwen38-q5|qwen38-q4|qwen36-a3b-q4|gemma4-a4b-q4)
    exec "$script_root/run-model-matrix-server.sh" "$profile"
    ;;
  *) echo "unknown productivity profile: $profile" >&2; exit 2 ;;
esac
