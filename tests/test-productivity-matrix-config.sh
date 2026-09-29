#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
server="$root/scripts/run-productivity-server.sh"
runner="$root/scripts/run-productivity-matrix.sh"

test -f "$server"
test -f "$runner"
grep -F 'qwen38-q8-fastmtp' "$server"
grep -F 'llama.cpp-fastmtp' "$server"
grep -F -- '--spec-draft-model' "$server"
grep -F -- '--spec-draft-n-max 3' "$server"
grep -F 'qwen38-q8)' "$server"
grep -F -- '--spec-draft-n-max 5' "$server"
grep -F -- '--no-cache-prompt' "$server"
grep -F -- '--host 127.0.0.1' "$server"
grep -F -- '--port 18082' "$server"
grep -F 'run-model-matrix-server.sh' "$server"

grep -F 'trap cleanup EXIT INT TERM HUP' "$runner"
grep -F 'systemctl --user stop gemma4-productivity.service' "$runner"
grep -F 'profiles still active after production stop' "$runner"
grep -F 'qwen38-q8 qwen38-q8-fastmtp qwen38-q6 qwen38-q5 qwen38-q4 qwen36-a3b-q4 gemma4-a4b-q4' "$runner"
grep -F 'productivity_benchmark.py' "$runner"
grep -F 'systemctl --user start gemma4-productivity.service' "$runner"

echo "Productivity matrix configuration contract: PASS"
