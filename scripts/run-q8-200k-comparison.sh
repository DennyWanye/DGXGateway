#!/usr/bin/env bash
set -euo pipefail

qwen_root=${DGX_MODEL_ROOT:-/home/operator/dgx-models/qwen38-27b}
script_root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
output_root=${Q8_200K_EVIDENCE_ROOT:-$qwen_root/evidence/q8-200k-$(date -u +%Y%m%dT%H%M%SZ)}
model="$qwen_root/models/Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-Q8_K_P-Ollama-CCSwitch.gguf"
mtp_mode=${Q8_200K_MTP_MODE:-embedded}
server_pid=
recovery_unit=qwen38-q8-200k-recovery

case "$mtp_mode" in
  embedded)
    binary="$qwen_root/src/llama.cpp-q8-256k/build-cuda/bin/llama-server"
    profile=qwen38-q8
    alias=qwen38-q8-200k
    summary_name=q8-200k-summary.json
    ubatch_size=1024
    draft_args=()
    spec_args=(--spec-type draft-mtp --spec-draft-n-max 5 --spec-draft-p-min 0.4)
    load_args=(--load-mode none)
    ;;
  fastmtp)
    source_root="$qwen_root/src/llama.cpp-fastmtp"
    binary="$source_root/build-cuda/bin/llama-server"
    draft="$qwen_root/models/Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-FastMTP-32K.gguf"
    profile=qwen38-q8-fastmtp
    alias=qwen38-q8-fastmtp-200k
    summary_name=qwen38-q8-fastmtp-200k-summary.json
    ubatch_size=512
    draft_args=(--spec-draft-model "$draft" --spec-draft-ngl all)
    spec_args=(--spec-type draft-mtp --spec-draft-n-max 3 --spec-draft-p-min 0)
    load_args=(--no-mmap)
    [[ -f $draft ]]
    [[ $(git -C "$source_root" rev-parse HEAD) == 4df29be4f4c3673f428170fda944a5b19f743bb8 ]]
    ;;
  *) echo "unknown Q8_200K_MTP_MODE: $mtp_mode" >&2; exit 2 ;;
esac

production_health() {
  systemctl --user is-active --quiet gemma4-productivity.service &&
    test "$(curl --fail --silent --max-time 10 http://192.0.2.30:11434/v1/models | jq -r '.data[0].id')" = gemma4-productivity-200k:latest
}

cleanup() {
  set +e
  if [[ -n ${server_pid:-} ]]; then
    kill "$server_pid" 2>/dev/null
    wait "$server_pid" 2>/dev/null
  fi
  pkill -f 'llama-server.*--port 18082' 2>/dev/null || true
  systemctl --user start gemma4-productivity.service
  restored=0
  for _ in $(seq 1 180); do
    if production_health; then restored=1; break; fi
    sleep 2
  done
  systemctl --user stop "$recovery_unit.timer" "$recovery_unit.service" 2>/dev/null || true
  [[ $restored == 1 ]] || { echo "production recovery health check failed" >&2; return 1; }
}
trap cleanup EXIT INT TERM HUP

[[ -x $binary && -f $model ]]
production_health || { echo "production is not healthy before benchmark" >&2; exit 1; }
mkdir -p "$output_root/raw" "$output_root/logs"
systemd-run --user --unit="$recovery_unit" --on-active=4h /bin/bash -lc \
  'systemctl --user start gemma4-productivity.service' >/dev/null
systemctl --user stop gemma4-productivity.service

if pgrep -x llama-server >/dev/null || pgrep -x ollama >/dev/null; then
  echo "profiles still active after production stop" >&2
  pgrep -af 'llama-server|ollama' >&2 || true
  exit 1
fi

for run_index in 1 2 3; do
  log="$output_root/logs/$profile-run$run_index.log"
  "$binary" \
    --model "$model" "${draft_args[@]}" --alias "$alias" \
    --ctx-size 204800 --parallel 1 --batch-size 2048 --ubatch-size "$ubatch_size" \
    --n-gpu-layers all --split-mode none --flash-attn on \
    --cache-type-k f16 --cache-type-v f16 "${load_args[@]}" --no-cache-prompt \
    "${spec_args[@]}" \
    --temp 1.0 --top-k 20 --top-p 0.95 --min-p 0 \
    --presence-penalty 0 --repeat-penalty 1.0 \
    --jinja --reasoning auto --reasoning-format deepseek \
    --host 127.0.0.1 --port 18082 >"$log" 2>&1 &
  server_pid=$!

  ready=0
  for _ in $(seq 1 300); do
    if ! kill -0 "$server_pid" 2>/dev/null; then break; fi
    if curl --fail --silent --max-time 3 http://127.0.0.1:18082/v1/models \
      >"$output_root/raw/$profile-run$run_index-models.json"; then
      ready=1
      break
    fi
    sleep 2
  done
  [[ $ready == 1 ]] || { tail -100 "$log" >&2; exit 1; }

  mapfile -t active_servers < <(pgrep -x llama-server)
  [[ ${#active_servers[@]} == 1 && ${active_servers[0]} == "$server_pid" ]] || {
    echo "profiles still active after isolated server start" >&2
    pgrep -af llama-server >&2 || true
    exit 1
  }
  ! pgrep -x ollama >/dev/null
  ss -lntp | grep -F '127.0.0.1:18082'
  ! ss -lntp | grep -E '0\.0\.0\.0:18082|192\.168\.10\.27:18082'

  python3 "$script_root/benchmark-model-matrix.py" \
    --profile "$profile" --alias "$alias" --run-index "$run_index" \
    --server-log "$log" --server-pid "$server_pid" \
    --output "$output_root/raw/$profile-run$run_index.json"

  kill "$server_pid"
  wait "$server_pid" || true
  server_pid=
  sleep 3
done

python3 - "$output_root" "$profile" "$mtp_mode" "$summary_name" <<'PY'
import json
from pathlib import Path
from statistics import median
import sys

root = Path(sys.argv[1])
profile = sys.argv[2]
mtp_mode = sys.argv[3]
summary_name = sys.argv[4]
runs = [json.loads((root / "raw" / f"{profile}-run{i}.json").read_text()) for i in (1, 2, 3)]
assert all(
    row["status"] == "pass"
    and 200_000 <= row["actual_input_tokens"] <= 204_544
    and row["output_tokens"] >= 256
    and row["sentinel_recalled"]
    and row["full_prompt_evaluated"]
    and row["speculative_loaded"]
    for row in runs
)
summary = {
    "profile": profile,
    "mtp_mode": mtp_mode,
    "environment": "isolated-cold-loopback-no-prompt-cache",
    "runs": runs,
    "summary": {
        "valid_runs": 3,
        "prompt_tps_median": median(row["prompt_tps"] for row in runs),
        "generation_tps_median": median(row["generation_tps"] for row in runs),
        "ttft_seconds_median": median(row["ttft_seconds"] for row in runs),
        "draft_acceptance_weighted": round(
            sum(row["draft_accepted"] for row in runs) /
            sum(row["draft_generated"] for row in runs), 6
        ),
    },
}
(root / summary_name).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(summary["summary"], ensure_ascii=False))
PY

echo "Q8_200K_COMPARISON_PASS output=$output_root"
cleanup
trap - EXIT INT TERM HUP
production_health
