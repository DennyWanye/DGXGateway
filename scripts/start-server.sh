#!/usr/bin/env bash
set -euo pipefail

root="${DGX_MODEL_ROOT:-$HOME/dgx-models/qwen38-27b}"
bin="$root/src/llama.cpp/build-cuda/bin/llama-server"
model="$root/models/Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf"
log="$root/logs/llama-server.log"
api_key_file="${DGX_API_KEY_FILE:-$HOME/.config/qwen38/api-keys}"

[[ -x "$bin" ]] || { echo "Missing llama-server: $bin" >&2; exit 1; }
[[ -f "$model" ]] || { echo "Missing model: $model" >&2; exit 1; }
[[ -r "$api_key_file" ]] || { echo "Missing API key file: $api_key_file" >&2; exit 1; }
mkdir -p "$root/logs"

unit="$HOME/.config/systemd/user/qwen38-27b.service"
if [[ -f "$unit" ]]; then
  systemctl --user daemon-reload
  systemctl --user restart qwen38-27b.service
  echo "Restarted persistent qwen38-27b.service; log: $log"
  exit 0
fi

systemctl --user stop qwen38-27b.service >/dev/null 2>&1 || true
systemd-run --user --unit=qwen38-27b --collect \
  --property=Restart=on-failure \
  --property=RestartSec=5 \
  --property="StandardOutput=append:$log" \
  --property="StandardError=append:$log" \
  "$bin" \
    --model "$model" \
    --alias qwen38-27b-aggressive-q8 \
    --ctx-size 262144 \
    --parallel 1 \
    --batch-size 2048 \
    --ubatch-size 512 \
    --n-gpu-layers all \
    --split-mode none \
    --flash-attn on \
    --cache-type-k f16 \
    --cache-type-v f16 \
    --load-mode none \
    --spec-type draft-mtp \
    --spec-draft-n-max 2 \
    --spec-draft-p-min 0 \
    --jinja \
    --reasoning auto \
    --reasoning-format deepseek \
    --api-key-file "$api_key_file" \
    --host 192.0.2.30 \
    --port 8080

echo "Started qwen38-27b.service; log: $log"
