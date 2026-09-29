#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runner="$root/scripts/run-q8-tuning-candidate.sh"
orchestrator="$root/scripts/tune-q8-256k.sh"
pass2="$root/scripts/tune-q8-speculation-pass2.sh"

[[ -f "$runner" ]]
grep -F 'llama.cpp-q8-256k/build-cuda/bin/llama-server' "$runner"
grep -F 'Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-Q8_K_P-Ollama-CCSwitch.gguf' "$runner"
grep -F -- '--alias qwen38-q8-256k:latest' "$runner"
grep -F -- '--ctx-size 262144' "$runner"
grep -F -- '--parallel 1' "$runner"
grep -F -- '--n-gpu-layers all' "$runner"
grep -F -- '--flash-attn on' "$runner"
grep -F -- '--cache-type-k f16' "$runner"
grep -F -- '--cache-type-v f16' "$runner"
grep -F '127.0.0.1' "$runner"
grep -F '3|4|5)' "$runner"
grep -F '0.3|0.4|0.5|0.6)' "$runner"
grep -F 'draft-mtp|draft-mtp,ngram-mod)' "$runner"
grep -F -- "--spec-type \"\$spec_types\"" "$runner"
grep -F '512|1024|2048)' "$runner"
grep -F '0|256)' "$runner"
grep -F '0|1)' "$runner"
grep -F -- '--spec-default' "$runner"
grep -F -- '--reasoning-preserve' "$runner"

[[ -f "$orchestrator" ]]
grep -F 'baseline:2:0:512:0' "$orchestrator"
grep -F 'mtp-d2-p05:2:0.5:512:0' "$orchestrator"
grep -F 'mtp-d2-p08:2:0.8:512:0' "$orchestrator"
grep -F 'mtp-d3-p05:3:0.5:512:0' "$orchestrator"
grep -F 'mtp-d3-p08:3:0.8:512:0' "$orchestrator"
grep -F 'systemctl --user stop qwen38-q8-tuning.service' "$orchestrator"

[[ -f "$pass2" ]]
grep -F 'baseline:draft-mtp:3:0.5' "$pass2"
grep -F 'd3-p03:draft-mtp:3:0.3' "$pass2"
grep -F 'd3-p04:draft-mtp:3:0.4' "$pass2"
grep -F 'd3-p06:draft-mtp:3:0.6' "$pass2"
grep -F 'nearest_cutoff' "$pass2"
grep -F 'draft-mtp,ngram-mod' "$pass2"
grep -F 'minimum_gain=0.03' "$pass2"
grep -F 'trap cleanup EXIT INT TERM' "$pass2"
grep -F 'http://192.0.2.30:11434/slots' "$pass2"
grep -F 'Production slot became busy' "$pass2"

pass3="$root/scripts/tune-q8-lossless-pass3.sh"
[[ -f "$pass3" ]]
grep -F 'manual-current:0:0' "$pass3"
grep -F 'official-default:1:0' "$pass3"
grep -F 'official-default-cache256:1:256' "$pass3"
grep -F 'ubatch-2048:0:0:2048' "$pass3"
grep -F 'minimum_gain=0.02' "$pass3"
grep -F 'http://192.0.2.30:11434/slots' "$pass3"

echo "Q8 tuning configuration contract: PASS"
