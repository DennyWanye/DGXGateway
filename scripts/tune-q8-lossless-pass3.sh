#!/usr/bin/env bash
set -euo pipefail

root="${DGX_MODEL_ROOT:-$HOME/dgx-models/qwen38-27b}"
unit="qwen38-q8-tuning.service"
run_id="$(date +%Y%m%d-%H%M%S)"
work="$root/evidence/q8-lossless-pass3-work/$run_id"
final="$root/evidence/q8-lossless-pass3.json"
mkdir -p "$work" "$root/logs"

cleanup() {
  systemctl --user stop "$unit" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

production_idle() {
  curl --fail --silent --max-time 3 http://192.0.2.30:11434/slots | \
    python3 -c 'import json,sys; slots=json.load(sys.stdin); raise SystemExit(0 if all(not slot["is_processing"] for slot in slots) else 1)'
}

wait_ready() {
  local state
  for _ in $(seq 1 120); do
    curl --fail --silent http://127.0.0.1:18080/health >/dev/null 2>&1 && return 0
    state="$(systemctl --user is-active "$unit" 2>/dev/null || true)"
    [[ "$state" == failed || "$state" == inactive ]] && return 1
    sleep 1
  done
  return 1
}

run_candidate() {
  local name="$1" spec_default="$2" cache_reuse="$3" ubatch="$4"
  local effective_spec="draft-mtp" log="$root/logs/q8-pass3-$name.log"
  [[ "$spec_default" == 0 ]] || effective_spec="draft-mtp,ngram-mod"
  production_idle || { echo "Production slot is busy; aborting without changing it" >&2; exit 75; }
  cleanup
  systemctl --user reset-failed "$unit" >/dev/null 2>&1 || true
  systemd-run --user --unit=qwen38-q8-tuning --collect \
    --property="StandardOutput=append:$log" \
    --property="StandardError=append:$log" \
    --setenv="SPEC_TYPES=draft-mtp" \
    --setenv="SPEC_DEFAULT=$spec_default" \
    --setenv="MTP_DEPTH=5" \
    --setenv="MTP_P_MIN=0.4" \
    --setenv="UBATCH_SIZE=$ubatch" \
    --setenv="CACHE_REUSE=$cache_reuse" \
    "$root/scripts/run-q8-tuning-candidate.sh" >/dev/null
  wait_ready || { echo "Candidate $name failed to start; see $log" >&2; exit 1; }
  "$root/scripts/benchmark-q8-tuning.py" \
    --candidate "$name" --spec-types "$effective_spec" --depth 5 \
    --p-min 0.4 --ubatch "$ubatch" --cache-reuse "$cache_reuse" \
    --server-log "$log" > "$work/$name.json.tmp"
  production_idle || { rm -f "$work/$name.json.tmp"; echo "Production became busy; discarded $name" >&2; exit 75; }
  mv "$work/$name.json.tmp" "$work/$name.json"
  cleanup
}

matrix=(
  "manual-current:0:0:1024"
  "official-default:1:0:1024"
  "official-default-cache256:1:256:1024"
  "ubatch-2048:0:0:2048"
)
for entry in "${matrix[@]}"; do
  IFS=: read -r name spec_default cache_reuse ubatch <<< "$entry"
  run_candidate "$name" "$spec_default" "$cache_reuse" "$ubatch"
done

python3 - "$work" "$root/scripts" > "$final.tmp" <<'PY'
import importlib.util, json, pathlib, sys
work = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("q8_tuning", pathlib.Path(sys.argv[2]) / "q8_tuning.py")
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
items = [json.loads(path.read_text()) for path in sorted(work.glob("*.json"))]
baseline = next(item for item in items if item["candidate"] == "manual-current")
views = {item["candidate"]: module.selection_aggregate(item) for item in items}
winner = module.choose_winner(
    views["manual-current"],
    [views[item["candidate"]] for item in items if item["candidate"] != "manual-current"],
    minimum_gain=0.02,
)
selected = next(item for item in items if item["candidate"] == winner["name"])
result = {
    "schema_version": 1,
    "baseline": "manual-current",
    "selected": selected["candidate"],
    "selected_settings": selected["settings"],
    "selection_generation_tps": views[selected["candidate"]]["generation_tps"],
    "generation_gain_percent": round((views[selected["candidate"]]["generation_tps"] / views["manual-current"]["generation_tps"] - 1) * 100, 2),
    "candidates": items,
}
print(json.dumps(result, ensure_ascii=False, indent=2))
PY
mv "$final.tmp" "$final"
python3 -m json.tool "$final" >/dev/null
echo "Third-pass evidence: $final"
