#!/usr/bin/env bash
set -euo pipefail

systemctl --user disable --now gemma4-productivity.service
systemctl --user enable --now qwen38-q8-256k.service

ready=0
for _ in $(seq 1 300); do
  if systemctl --user is-active --quiet qwen38-q8-256k.service &&
    test "$(curl -fsS --max-time 5 http://192.0.2.30:11434/v1/models 2>/dev/null | jq -r '.data[0].id')" = qwen38-q8-256k:latest; then
    ready=1
    break
  fi
  sleep 2
done
[[ $ready == 1 ]]
echo "Q8_PRODUCTION_ROLLBACK_PASS"
