#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
modelfile="$root/ollama/Modelfile.q4-256k"
unit="$root/systemd/ollama.service"

grep -F 'FROM /home/operator/dgx-models/qwen38-27b/models/Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-Q4_K_P-Ollama-CCSwitch.gguf' "$modelfile"
grep -F 'PARAMETER num_ctx 262144' "$modelfile"
grep -F 'Environment="OLLAMA_HOST=192.0.2.30:11434"' "$unit"
grep -F 'Environment="OLLAMA_NUM_PARALLEL=1"' "$unit"
grep -F 'Environment="OLLAMA_MAX_LOADED_MODELS=1"' "$unit"
grep -F 'Environment="OLLAMA_KEEP_ALIVE=30m"' "$unit"

if grep -F 'OLLAMA_HOST=0.0.0.0' "$unit"; then
  echo "Ollama must remain bound to the DGX LAN address" >&2
  exit 1
fi

echo "Q4 Ollama configuration contract: PASS"
