#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
runner="$root/scripts/run-q8-200k-comparison.sh"

test -f "$runner"
grep -F 'trap cleanup EXIT INT TERM HUP' "$runner"
grep -F 'systemctl --user stop gemma4-productivity.service' "$runner"
grep -F "pgrep -x llama-server" "$runner"
grep -F 'profiles still active after production stop' "$runner"
grep -F 'for run_index in 1 2 3' "$runner"
grep -F -- '--ctx-size 204800' "$runner"
grep -F -- '--host 127.0.0.1' "$runner"
grep -F -- '--port 18082' "$runner"
grep -F -- '--no-cache-prompt' "$runner"
grep -F -- '--spec-draft-n-max 5' "$runner"
grep -F -- '--spec-draft-p-min 0.4' "$runner"
grep -F 'benchmark-model-matrix.py' "$runner"
grep -F 'systemctl --user start gemma4-productivity.service' "$runner"
grep -F 'mtp_mode=${Q8_200K_MTP_MODE:-embedded}' "$runner"
grep -F 'source_root="$qwen_root/src/llama.cpp-fastmtp"' "$runner"
grep -F 'Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-FastMTP-32K.gguf' "$runner"
grep -F 'draft_args=(--spec-draft-model "$draft" --spec-draft-ngl all)' "$runner"
grep -F 'spec_args=(--spec-type draft-mtp --spec-draft-n-max 3 --spec-draft-p-min 0)' "$runner"
grep -F 'ubatch_size=512' "$runner"
grep -F 'summary_name=q8-200k-summary.json' "$runner"
grep -F 'summary_name=qwen38-q8-fastmtp-200k-summary.json' "$runner"

echo "Q8 clean 200K comparison contract: PASS"
