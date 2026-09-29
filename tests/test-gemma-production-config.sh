#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
runner="$root/scripts/run-gemma4-production.sh"
unit="$root/systemd/gemma4-productivity.service"
cutover="$root/scripts/cutover-gemma4-productivity.sh"
rollback="$root/scripts/rollback-gemma4-productivity.sh"

test -f "$runner" && test -f "$unit" && test -f "$cutover" && test -f "$rollback"
grep -F 'Gemma4-26B-A4B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M.gguf' "$runner"
grep -F 'mtp-gemma-4-26B-A4B-it.gguf' "$runner"
grep -F -- '--alias gemma4-productivity-200k:latest' "$runner"
grep -F -- '--ctx-size 204800' "$runner"
grep -F -- '--spec-draft-model' "$runner"
grep -F -- '--spec-draft-ngl all' "$runner"
grep -F -- '--reasoning-budget 64' "$runner"
grep -F 'GEMMA_HOST' "$runner"
grep -F 'GEMMA_PORT' "$runner"
grep -F 'Restart=on-failure' "$unit"
grep -F 'systemctl --user stop qwen38-q8-256k.service' "$cutover"
grep -F 'systemctl --user enable --now gemma4-productivity.service' "$cutover"
grep -F 'systemctl --user disable --now gemma4-productivity.service' "$rollback"
grep -F 'systemctl --user enable --now qwen38-q8-256k.service' "$rollback"

echo "Gemma4 production configuration contract: PASS"
