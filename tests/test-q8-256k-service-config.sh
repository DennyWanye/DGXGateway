#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
build="$root/scripts/build-q8-llama.sh"
runner="$root/scripts/run-q8-256k-server.sh"
unit="$root/systemd/qwen38-q8-256k.service"
responses="$root/scripts/test-q8-responses.sh"
stage="$root/scripts/stage-q8-256k.sh"
install_script="$root/scripts/install-q8-256k-service.sh"
cutover="$root/scripts/cutover-q8-256k.sh"
rollback="$root/scripts/rollback-q8-256k.sh"
long_context="$root/scripts/test-q8-256k.py"

grep -F 'commit="3737e41370da1830a44c663f9929a0f27591ffa6"' "$build"
grep -F 'https://gitcode.com/gh_mirrors/ll/llama.cpp.git' "$build"
grep -F -- '-DLLAMA_BUILD_UI=OFF' "$build"
grep -F -- '-DLLAMA_USE_PREBUILT_UI=OFF' "$build"
grep -F 'Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-Q8_K_P-Ollama-CCSwitch.gguf' "$runner"
grep -F -- '--alias qwen38-q8-256k:latest' "$runner"
grep -F -- '--ctx-size 262144' "$runner"
grep -F -- '--parallel 1' "$runner"
grep -F -- '--n-gpu-layers all' "$runner"
grep -F -- '--flash-attn on' "$runner"
grep -F -- '--cache-type-k f16' "$runner"
grep -F -- '--cache-type-v f16' "$runner"
grep -F -- '--spec-type draft-mtp' "$runner"
grep -F -- '--ubatch-size 1024' "$runner"
grep -F -- '--spec-draft-n-max 5' "$runner"
grep -F -- '--spec-draft-p-min 0.4' "$runner"
if grep -F -- '--cache-reuse' "$runner"; then
  echo "Measured winner uses default prompt cache without cache-reuse shifting" >&2
  exit 1
fi
grep -F 'Environment="Q8_HOST=192.0.2.30"' "$unit"
grep -F 'Environment="Q8_PORT=11434"' "$unit"
grep -F 'Restart=on-failure' "$unit"
if grep -F 'Restart=always' "$unit"; then
  echo "Q8 service must not loop forever on configuration failures" >&2
  exit 1
fi

grep -F 'http://127.0.0.1:18080/v1' "$responses"
grep -F "\$base_url/responses" "$responses"
grep -F 'qwen38-q8-256k:latest' "$responses"
grep -F 'response.completed' "$responses"
grep -F 'function_call' "$responses"
grep -F '\"reasoning\":{\"effort\":\"none\"}' "$responses"
grep -F '"reasoning": {"effort": "none"}' "$long_context"
grep -F 'Q8_HOST=127.0.0.1' "$stage"
grep -F 'Q8_PORT=18080' "$stage"
grep -F 'qwen38-q8-256k.service' "$install_script"
grep -F 'systemctl --user stop ollama.service' "$cutover"
grep -F 'systemctl --user start qwen38-q8-256k.service' "$cutover"
grep -F 'systemctl --user stop qwen38-q8-256k.service' "$rollback"
grep -F 'systemctl --user start ollama.service' "$rollback"

shellcheck "$build" "$runner" "$responses" "$stage" "$install_script" "$cutover" "$rollback" "$root/tests/test-q8-256k-service-config.sh"
echo "Q8 256K service configuration contract: PASS"
