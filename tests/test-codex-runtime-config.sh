#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
test_patch="$root/patches/llama-codex-namespace-test.patch"
implementation_patch="$root/patches/llama-codex-namespace-tools.patch"
history_test_patch="$root/patches/llama-codex-history-items-test.patch"
history_implementation_patch="$root/patches/llama-codex-history-items.patch"
builder="$root/scripts/build-codex-compatible-runtimes.sh"
runner="$root/scripts/run-codex-compatible-server.sh"
matrix="$root/scripts/run-codex-agentic-matrix.sh"
production_unit="$root/systemd/dgx-codex-production.service"
selected_runner="$root/scripts/run-selected-codex-production.sh"
cutover="$root/scripts/cutover-codex-production.sh"
rollback="$root/scripts/rollback-codex-production.sh"

test -f "$test_patch"
grep -F '"name": "read_file"' "$test_patch"
grep -F '"function": {' "$test_patch"
grep -F 'mcp__filesystem__read_file' "$test_patch"
grep -F 'mcp__filesystem__write_file' "$test_patch"

test -f "$implementation_patch"
grep -F 'type == "namespace"' "$implementation_patch"
grep -F 'resp_tool.at("tools").is_array()' "$implementation_patch"
grep -F 'function["name"] = ns + name' "$implementation_patch"
grep -F 'SRV_WRN("%s", "malformed Responses namespace tool skipped\n")' "$implementation_patch"
if grep -F 'type == "web_search"' "$implementation_patch"; then
  echo "web_search must not be converted to a local function" >&2
  exit 1
fi

test -f "$history_test_patch"
grep -F 'custom_tool_call' "$history_test_patch"
grep -F 'custom_tool_call_output' "$history_test_patch"
grep -F 'summary_text' "$history_test_patch"
test -f "$history_implementation_patch"
grep -F 'item.at("type") == "custom_tool_call"' "$history_implementation_patch"
grep -F 'item.at("type") == "custom_tool_call_output"' "$history_implementation_patch"
grep -F 'reasoning_text.empty()' "$history_implementation_patch"

test -f "$builder"
grep -F '5ea87ddad22541a37053c7ba92b02ec1923617c6' "$builder"
grep -F '4df29be4f4c3673f428170fda944a5b19f743bb8' "$builder"
grep -F '981285400b59dc45cf99936b6ff66d4b3aa0f1b532f85fa51418cb407e51d615' "$builder"
grep -F -- '-DCMAKE_CUDA_ARCHITECTURES=121' "$builder"
grep -F -- '-DCMAKE_CUDA_COMPILER=/usr/local/cuda-13.0/bin/nvcc' "$builder"
grep -F -- '--target test-chat' "$builder"
grep -F -- '--target test-chat llama-server' "$builder"
grep -F 'namespace RED test unexpectedly passed' "$builder"
grep -F 'history_test_patch' "$builder"
grep -F 'history_implementation_patch' "$builder"

test -f "$runner"
grep -F 'gemma4)' "$runner"
grep -F 'qwen38-q8-fastmtp)' "$runner"
grep -F 'llama.cpp-codex-gemma/build-codex/bin/llama-server' "$runner"
grep -F 'llama.cpp-codex-fastmtp/build-codex/bin/llama-server' "$runner"
grep -F 'host=${CODEX_HOST:-127.0.0.1}' "$runner"
grep -F 'port=${CODEX_PORT:-18084}' "$runner"
grep -F 'context_size=${CODEX_CONTEXT_SIZE:-262144}' "$runner"
grep -F -- '--ctx-size "$context_size" --parallel 1 --batch-size 2048' "$runner"
grep -F -- '--reasoning on --reasoning-effort low --reasoning-preserve' "$runner"
grep -F 'gemma4-codex-200k:candidate' "$runner"
grep -F 'qwen38-q8-fastmtp-codex-200k:candidate' "$runner"
grep -F 'gemma_temp=${CODEX_GEMMA_TEMP:-0.2}' "$runner"
grep -F -- '--temp "$gemma_temp"' "$runner"
if grep -Eq -- '(^|[[:space:]])--seed([=[:space:]]|$)' "$runner"; then
  echo "production Codex runner must use a per-request random seed" >&2
  exit 1
fi
if grep -F -- '--reasoning-budget 64' "$runner"; then
  echo "candidate runner must not impose a 64-token reasoning cap" >&2
  exit 1
fi

test -f "$matrix"
grep -F 'profiles=(gemma4 qwen38-q8-fastmtp)' "$matrix"
grep -F 'aliases=(gemma4-codex-200k:candidate qwen38-q8-fastmtp-codex-200k:candidate)' "$matrix"
grep -F -- '-L 18084:127.0.0.1:18084' "$matrix"
grep -F 'gemma4-productivity.service' "$matrix"
grep -F 'dgx-codex-recovery' "$matrix"
grep -F 'scripts/codex_compatibility_probe.py' "$matrix"
grep -F 'scripts/create-codex-agent-fixture.py' "$matrix"
grep -F -- '--ephemeral --ignore-user-config --ignore-rules' "$matrix"
grep -F -- '--dangerously-bypass-approvals-and-sandbox --json' "$matrix"
grep -F 'model_context_window=204800' "$matrix"
grep -F 'model_reasoning_effort="low"' "$matrix"
grep -F 'wire_api="responses"' "$matrix"
grep -F 'python3 -m unittest discover -s tests -v' "$matrix"
grep -F '2>"$output_root/stderr-$profile-$fixture_name.log"' "$matrix"
grep -F "final_message=\$(jq -s '" "$matrix"
if grep -F "final_message=\$(jq -rs" "$matrix"; then
  echo "final message extraction must preserve JSON string encoding" >&2
  exit 1
fi

test -f "$production_unit"
grep -F 'Environment=CODEX_HOST=192.0.2.30' "$production_unit"
grep -F 'Environment=CODEX_PORT=11434' "$production_unit"
grep -F 'Environment=CODEX_ALIAS=dgx-codex-200k:latest' "$production_unit"
grep -F 'Environment=CODEX_CONTEXT_SIZE=262144' "$production_unit"
grep -F 'ExecStart=%h/dgx-models/model-matrix/scripts/run-selected-codex-production.sh' "$production_unit"
grep -F 'Restart=on-failure' "$production_unit"

test -f "$selected_runner"
grep -F 'evidence/codex-agentic-20260827/summary.json' "$selected_runner"
grep -F 'gemma4|qwen38-q8-fastmtp)' "$selected_runner"
grep -F 'run-codex-compatible-server.sh' "$selected_runner"

test -f "$cutover"
grep -F 'trap rollback_on_error ERR' "$cutover"
grep -F 'gemma4-productivity.service' "$cutover"
grep -F 'dgx-codex-production.service' "$cutover"
grep -F 'scripts/codex_compatibility_probe.py' "$cutover"
grep -F 'scripts/create-codex-agent-fixture.py' "$cutover"
grep -F 'DGX_CODEX_PRODUCTION_PASS' "$cutover"

test -f "$rollback"
grep -F 'systemctl --user disable --now dgx-codex-production.service' "$rollback"
grep -F 'systemctl --user enable --now gemma4-productivity.service' "$rollback"
grep -F 'gemma4-productivity-200k:latest' "$rollback"

echo "Codex runtime configuration contract: PASS"
