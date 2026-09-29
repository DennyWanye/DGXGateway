#!/usr/bin/env bash
set -euo pipefail

root="${DGX_MODEL_ROOT:-$HOME/dgx-models/qwen38-27b}"
unit="qwen38-q8-tuning.service"
run_id="$(date +%Y%m%d-%H%M%S)"
work="$root/evidence/q8-tuning-work/$run_id"
final="$root/evidence/q8-lossless-tuning.json"
mkdir -p "$work" "$root/logs"

cleanup() {
  systemctl --user stop qwen38-q8-tuning.service >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

available_kib="$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)"
(( available_kib >= 40 * 1024 * 1024 )) || { echo "Less than 40 GiB available" >&2; exit 1; }

wait_ready() {
  for _ in $(seq 1 90); do
    curl --fail --silent http://127.0.0.1:18080/health >/dev/null 2>&1 && return 0
    sleep 1
  done
  journalctl --user -u "$unit" -n 80 --no-pager >&2
  return 1
}

run_candidate() {
  local name="$1" depth="$2" p_min="$3" ubatch="$4" cache_reuse="$5"
  echo "Running $name depth=$depth p_min=$p_min ubatch=$ubatch cache_reuse=$cache_reuse"
  cleanup
  systemctl --user reset-failed "$unit" >/dev/null 2>&1 || true
  systemd-run --user --unit=qwen38-q8-tuning --collect \
    --property="StandardOutput=append:$root/logs/q8-tuning-$name.log" \
    --property="StandardError=append:$root/logs/q8-tuning-$name.log" \
    --setenv="MTP_DEPTH=$depth" \
    --setenv="MTP_P_MIN=$p_min" \
    --setenv="UBATCH_SIZE=$ubatch" \
    --setenv="CACHE_REUSE=$cache_reuse" \
    "$root/scripts/run-q8-tuning-candidate.sh" >/dev/null
  wait_ready
  "$root/scripts/benchmark-q8-tuning.py" \
    --candidate "$name" --depth "$depth" --p-min "$p_min" \
    --ubatch "$ubatch" --cache-reuse "$cache_reuse" \
    --server-log "$root/logs/q8-tuning-$name.log" > "$work/$name.json.tmp"
  mv "$work/$name.json.tmp" "$work/$name.json"
  cleanup
}

matrix=(
  "baseline:2:0:512:0"
  "mtp-d2-p05:2:0.5:512:0"
  "mtp-d2-p08:2:0.8:512:0"
  "mtp-d3-p05:3:0.5:512:0"
  "mtp-d3-p08:3:0.8:512:0"
)
for entry in "${matrix[@]}"; do
  IFS=: read -r name depth p_min ubatch cache_reuse <<< "$entry"
  run_candidate "$name" "$depth" "$p_min" "$ubatch" "$cache_reuse"
done

best_fields="$(python3 - "$work" "$root/scripts" <<'PY'
import importlib.util, json, pathlib, sys
work = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("q8_tuning", pathlib.Path(sys.argv[2]) / "q8_tuning.py")
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
items = [json.loads(path.read_text()) for path in sorted(work.glob("*.json"))]
baseline = next(item["aggregate"] for item in items if item["candidate"] == "baseline")
winner = module.choose_winner(baseline, [item["aggregate"] for item in items if item["candidate"] != "baseline"])
selected = next(item for item in items if item["candidate"] == winner["name"])
s = selected["settings"]
print(f'{s["mtp_depth"]}:{s["mtp_p_min"]}')
PY
)"
IFS=: read -r best_depth best_p_min <<< "$best_fields"
run_candidate "followup-ub1024" "$best_depth" "$best_p_min" 1024 0
run_candidate "followup-cache256" "$best_depth" "$best_p_min" 1024 256

python3 - "$work" "$root/scripts" > "$final.tmp" <<'PY'
import importlib.util, json, pathlib, sys
work = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("q8_tuning", pathlib.Path(sys.argv[2]) / "q8_tuning.py")
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
items = [json.loads(path.read_text()) for path in sorted(work.glob("*.json"))]
baseline_item = next(item for item in items if item["candidate"] == "baseline")
winner = module.choose_winner(baseline_item["aggregate"], [item["aggregate"] for item in items if item["candidate"] != "baseline"])
selected = next(item for item in items if item["candidate"] == winner["name"])
result = {
    "schema_version": 1,
    "baseline": baseline_item["candidate"],
    "selected": selected["candidate"],
    "selected_settings": selected["settings"],
    "generation_gain_percent": round((selected["aggregate"]["generation_tps"] / baseline_item["aggregate"]["generation_tps"] - 1) * 100, 2),
    "prompt_change_percent": round((selected["aggregate"]["prompt_tps"] / baseline_item["aggregate"]["prompt_tps"] - 1) * 100, 2),
    "candidates": items,
}
print(json.dumps(result, ensure_ascii=False, indent=2))
PY
mv "$final.tmp" "$final"
python3 -m json.tool "$final" >/dev/null
echo "Tuning evidence: $final"
