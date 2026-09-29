#!/usr/bin/env bash
set -euo pipefail

root="${DGX_MODEL_ROOT:-$HOME/dgx-models/qwen38-27b}"
unit="qwen38-q8-tuning.service"
run_id="$(date +%Y%m%d-%H%M%S)"
work="$root/evidence/q8-speculation-work/$run_id"
final="$root/evidence/q8-speculation-pass2.json"
mkdir -p "$work" "$root/logs"

cleanup() {
  systemctl --user stop qwen38-q8-tuning.service >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

available_kib="$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)"
(( available_kib >= 40 * 1024 * 1024 )) || { echo "Less than 40 GiB available" >&2; exit 1; }

wait_ready() {
  local state
  for _ in $(seq 1 90); do
    curl --fail --silent http://127.0.0.1:18080/health >/dev/null 2>&1 && return 0
    state="$(systemctl --user is-active "$unit" 2>/dev/null || true)"
    [[ "$state" == failed || "$state" == inactive ]] && return 1
    sleep 1
  done
  return 1
}

production_idle() {
  curl --fail --silent --max-time 3 http://192.0.2.30:11434/slots | \
    python3 -c 'import json,sys; raise SystemExit(0 if not json.load(sys.stdin)[0]["is_processing"] else 1)'
}

record_rejection() {
  local name="$1" spec_types="$2" depth="$3" p_min="$4" reason="$5" log="$6"
  python3 - "$name" "$spec_types" "$depth" "$p_min" "$reason" "$log" > "$work/$name.json" <<'PY'
import json, pathlib, sys
name, spec_types, depth, p_min, reason, log = sys.argv[1:]
path = pathlib.Path(log)
tail = path.read_text(errors="replace").splitlines()[-40:] if path.exists() else []
print(json.dumps({
    "candidate": name,
    "rejected": True,
    "reason": reason,
    "settings": {"model": "qwen38-q8-256k:latest", "context": 262144, "parallel": 1,
                 "cache_k": "f16", "cache_v": "f16", "mtp_depth": int(depth),
                 "mtp_p_min": float(p_min), "ubatch": 1024, "cache_reuse": 0,
                 "spec_types": spec_types},
    "runs": [],
    "aggregate": {"name": name, "valid": False, "valid_runs": 0, "total_runs": 0,
                  "generation_tps": 0.0, "prompt_tps": 0.0,
                  "draft_accepted": 0, "draft_generated": 0},
    "log_tail": tail,
}, ensure_ascii=False, indent=2))
PY
}

run_candidate() {
  local name="$1" spec_types="$2" depth="$3" p_min="$4"
  local log="$root/logs/q8-pass2-$name.log"
  production_idle || { echo "Production slot became busy; aborting cleanly" >&2; exit 75; }
  echo "Running $name spec=$spec_types depth=$depth p_min=$p_min"
  cleanup
  systemctl --user reset-failed "$unit" >/dev/null 2>&1 || true
  systemd-run --user --unit=qwen38-q8-tuning --collect \
    --property="StandardOutput=append:$log" \
    --property="StandardError=append:$log" \
    --setenv="SPEC_TYPES=$spec_types" \
    --setenv="MTP_DEPTH=$depth" \
    --setenv="MTP_P_MIN=$p_min" \
    --setenv="UBATCH_SIZE=1024" \
    --setenv="CACHE_REUSE=0" \
    "$root/scripts/run-q8-tuning-candidate.sh" >/dev/null
  if ! wait_ready; then
    record_rejection "$name" "$spec_types" "$depth" "$p_min" "startup_failed" "$log"
    cleanup
    return 0
  fi
  if ! "$root/scripts/benchmark-q8-tuning.py" \
      --candidate "$name" --spec-types "$spec_types" --depth "$depth" \
      --p-min "$p_min" --ubatch 1024 --cache-reuse 0 \
      --server-log "$log" > "$work/$name.json.tmp"; then
    record_rejection "$name" "$spec_types" "$depth" "$p_min" "benchmark_failed" "$log"
    rm -f "$work/$name.json.tmp"
    cleanup
    return 0
  fi
  production_idle || { echo "Production slot became busy; discarding candidate" >&2; rm -f "$work/$name.json.tmp"; exit 75; }
  mv "$work/$name.json.tmp" "$work/$name.json"
  cleanup
}

matrix=(
  "baseline:draft-mtp:3:0.5"
  "d3-p03:draft-mtp:3:0.3"
  "d3-p04:draft-mtp:3:0.4"
  "d3-p06:draft-mtp:3:0.6"
)
for entry in "${matrix[@]}"; do
  IFS=: read -r name spec_types depth p_min <<< "$entry"
  run_candidate "$name" "$spec_types" "$depth" "$p_min"
done

best_fields="$(python3 - "$work" "$root/scripts" <<'PY'
import importlib.util, json, pathlib, sys
work = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("q8_tuning", pathlib.Path(sys.argv[2]) / "q8_tuning.py")
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
items = [json.loads(path.read_text()) for path in sorted(work.glob("*.json"))]
valid = [item for item in items if item["aggregate"]["valid"]]
best = max(valid, key=lambda item: item["aggregate"]["generation_tps"])
p_min = float(best["settings"]["mtp_p_min"])
neighbor = module.nearest_cutoff(p_min, [0.3, 0.4, 0.5, 0.6])
print(f"{p_min}:{neighbor}")
PY
)"
IFS=: read -r best_p neighbor_p <<< "$best_fields"
for depth in 4 5; do
  run_candidate "d${depth}-best" draft-mtp "$depth" "$best_p"
  run_candidate "d${depth}-neighbor" draft-mtp "$depth" "$neighbor_p"
done

best_mtp="$(python3 - "$work" <<'PY'
import json, pathlib, sys
items = [json.loads(path.read_text()) for path in pathlib.Path(sys.argv[1]).glob("*.json")]
valid = [item for item in items if item["aggregate"]["valid"] and item["settings"]["spec_types"] == "draft-mtp"]
best = max(valid, key=lambda item: item["aggregate"]["generation_tps"])
print(f'{best["settings"]["mtp_depth"]}:{best["settings"]["mtp_p_min"]}')
PY
)"
IFS=: read -r best_depth best_p <<< "$best_mtp"
run_candidate hybrid draft-mtp,ngram-mod "$best_depth" "$best_p"

python3 - "$work" "$root/scripts" > "$final.tmp" <<'PY'
import importlib.util, json, pathlib, sys
work = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("q8_tuning", pathlib.Path(sys.argv[2]) / "q8_tuning.py")
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
items = [json.loads(path.read_text()) for path in sorted(work.glob("*.json"))]
baseline = next(item for item in items if item["candidate"] == "baseline")
views = {item["candidate"]: module.selection_aggregate(item) for item in items}
winner_aggregate = module.choose_winner(
    views["baseline"],
    [views[item["candidate"]] for item in items if item["candidate"] != "baseline"],
    minimum_gain=0.03,
)
selected = next(item for item in items if item["candidate"] == winner_aggregate["name"])
selected_view = views[selected["candidate"]]
result = {
    "schema_version": 1,
    "baseline": "baseline",
    "selected": selected["candidate"],
    "selected_settings": selected["settings"],
    "selection_generation_tps": selected_view["generation_tps"],
    "selection_basis": selected_view["selection_basis"],
    "generation_gain_percent": round((selected_view["generation_tps"] / views["baseline"]["generation_tps"] - 1) * 100, 2),
    "prompt_change_percent": round((selected_view["prompt_tps"] / views["baseline"]["prompt_tps"] - 1) * 100, 2),
    "candidates": items,
}
print(json.dumps(result, ensure_ascii=False, indent=2))
PY
mv "$final.tmp" "$final"
python3 -m json.tool "$final" >/dev/null
echo "Second-pass evidence: $final"
