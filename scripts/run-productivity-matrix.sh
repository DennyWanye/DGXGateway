#!/usr/bin/env bash
set -euo pipefail

script_root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
matrix_root=${DGX_MATRIX_ROOT:-/home/operator/dgx-models/model-matrix}
output_root=${PRODUCTIVITY_EVIDENCE_ROOT:-$matrix_root/evidence/productivity-$(date -u +%Y%m%dT%H%M%SZ)}
profiles=(qwen38-q8 qwen38-q8-fastmtp qwen38-q6 qwen38-q5 qwen38-q4 qwen36-a3b-q4 gemma4-a4b-q4)
server_pid=
recovery_unit=qwen38-productivity-recovery

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

for profile in "${profiles[@]}"; do
  log="$output_root/logs/$profile.log"
  "$script_root/run-productivity-server.sh" "$profile" >"$log" 2>&1 &
  server_pid=$!
  ready=0
  for _ in $(seq 1 300); do
    if ! kill -0 "$server_pid" 2>/dev/null; then break; fi
    if models=$(curl --fail --silent --max-time 3 http://127.0.0.1:18082/v1/models); then
      ready=1
      break
    fi
    sleep 2
  done
  [[ $ready == 1 ]] || { tail -100 "$log" >&2; exit 1; }
  alias=$(jq -r '.data[0].id' <<<"$models")
  mapfile -t active_servers < <(pgrep -x llama-server)
  [[ ${#active_servers[@]} == 1 && ${active_servers[0]} == "$server_pid" ]] || {
    echo "profiles still active during productivity test" >&2
    pgrep -af llama-server >&2 || true
    exit 1
  }
  ! pgrep -x ollama >/dev/null
  ss -lntp | grep -F '127.0.0.1:18082'
  ! ss -lntp | grep -E '0\.0\.0\.0:18082|192\.168\.10\.27:18082'

  python3 "$script_root/productivity_benchmark.py" \
    --profile "$profile" --alias "$alias" \
    --output "$output_root/raw/$profile.json"

  kill "$server_pid"
  wait "$server_pid" || true
  server_pid=
  sleep 3
done

python3 - "$output_root" "${profiles[@]}" <<'PY'
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
profiles = sys.argv[2:]
results = [json.loads((root / "raw" / f"{profile}.json").read_text()) for profile in profiles]
ranking = sorted(
    [item["summary"] for item in results],
    key=lambda row: (-row["passed_tasks"], row["total_seconds"]),
)
payload = {"results": results, "ranking": ranking, "winner": ranking[0]["profile"]}
(root / "productivity-summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({"winner": payload["winner"], "ranking": ranking}, ensure_ascii=False))
PY

echo "PRODUCTIVITY_MATRIX_PASS output=$output_root"
cleanup
trap - EXIT INT TERM HUP
production_health
