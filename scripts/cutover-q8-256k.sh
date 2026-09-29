#!/usr/bin/env bash
set -euo pipefail

root="${DGX_MODEL_ROOT:-$HOME/dgx-models/qwen38-27b}"
evidence="${Q8_ACCEPTANCE_EVIDENCE:-$root/evidence/q8-256k-acceptance.json}"
python3 - "$evidence" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
assert data["status"] == "completed", data
assert data["sentinel_recalled"] is True, data
assert data["raw_prompt_tokens"] >= 245000, data
PY

# Invoked indirectly by the ERR trap.
# shellcheck disable=SC2329
rollback() {
  systemctl --user stop qwen38-q8-256k.service >/dev/null 2>&1 || true
  systemctl --user start ollama.service
}
trap rollback ERR

systemctl --user stop qwen38-q8-256k-stage.service 2>/dev/null || true
systemctl --user stop ollama.service
systemctl --user enable qwen38-q8-256k.service
systemctl --user start qwen38-q8-256k.service
for _ in $(seq 1 180); do
  if curl --fail --silent http://192.0.2.30:11434/health >/dev/null; then
    "$root/scripts/test-q8-responses.sh" http://192.0.2.30:11434/v1
    trap - ERR
    echo "Q8 256K cutover complete"
    exit 0
  fi
  sleep 1
done
echo "Q8 production service did not become ready" >&2
exit 1
