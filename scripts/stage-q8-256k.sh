#!/usr/bin/env bash
set -euo pipefail

root="${DGX_MODEL_ROOT:-$HOME/dgx-models/qwen38-27b}"
systemctl --user stop qwen38-q8-256k-stage.service 2>/dev/null || true
systemd-run --user --unit=qwen38-q8-256k-stage \
  --property=Restart=no \
  --setenv=DGX_MODEL_ROOT="$root" \
  --setenv=Q8_HOST=127.0.0.1 \
  --setenv=Q8_PORT=18080 \
  "$root/scripts/run-q8-256k-server.sh"
for _ in $(seq 1 180); do
  if curl --fail --silent http://127.0.0.1:18080/health >/dev/null; then
    echo "Q8 staging service ready on 127.0.0.1:18080"
    exit 0
  fi
  sleep 1
done
echo "Q8 staging service did not become ready" >&2
exit 1
