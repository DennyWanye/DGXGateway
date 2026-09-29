#!/usr/bin/env bash
set -euo pipefail

root="${DGX_MODEL_ROOT:-$HOME/dgx-models/qwen38-27b}"
install -d "$HOME/.config/systemd/user" "$root/logs"
install -m 0644 \
  "$root/systemd/qwen38-q8-256k.service" \
  "$HOME/.config/systemd/user/qwen38-q8-256k.service"
systemctl --user daemon-reload
echo "Installed qwen38-q8-256k.service without starting it"
