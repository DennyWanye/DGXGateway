#!/usr/bin/env bash
set -euo pipefail

systemctl --user stop qwen38-q8-256k.service
systemctl --user disable qwen38-q8-256k.service >/dev/null 2>&1 || true
systemctl --user enable ollama.service
systemctl --user start ollama.service
curl --fail --silent --show-error --max-time 10 \
  http://192.0.2.30:11434/api/version
echo
echo "Rollback to Ollama complete"
