#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
test_script="$root/scripts/test-codex-compatibility.sh"

test -f "$test_script"
grep -F 'model=${MODEL:?set MODEL}' "$test_script"
grep -F '"$base_url/responses"' "$test_script"
grep -F 'response.completed' "$test_script"
grep -E 'tool_choice.*required' "$test_script"
grep -F 'function_call' "$test_script"

echo "Generic Codex compatibility configuration: PASS"
