#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

phase=deploy
config="$ROOT/config/minimax-h3-two-node.env.example"
runtime_lock="$ROOT/manifests/minimax-h3-runtime-lock.json"
fixture=
role=
output=

while (($#)); do
  case "$1" in
    --phase) phase="$2"; shift 2 ;;
    --config) config="$2"; shift 2 ;;
    --runtime-lock) runtime_lock="$2"; shift 2 ;;
    --fixture) fixture="$2"; shift 2 ;;
    --role) role="$2"; shift 2 ;;
    --output) output="$2"; shift 2 ;;
    *) h3_die "unknown argument: $1"; exit 2 ;;
  esac
done
case "$phase" in research|deploy) ;; *) h3_die "phase must be research or deploy"; exit 2 ;; esac

h3_load_config "$config"

load_facts_file() {
  local file="$1" line key value
  [[ -f "$file" ]] || h3_die "facts fixture not found: $file"
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" == H3_FACT_*=* ]] || h3_die "invalid fact line"
    key="${line%%=*}"
    value="${line#*=}"
    [[ "$key" =~ ^H3_FACT_[A-Z0-9_]+$ && "$value" != *[$' \t\n\r`$;&|<>']* ]] || h3_die "unsafe fact"
    printf -v "$key" '%s' "$value"
  done <"$file"
}

capture_local_facts() {
  [[ "$role" == head || "$role" == worker ]] || h3_die "--role is required without --fixture"
  H3_FACT_ROLE="$role"
  H3_FACT_HOSTNAME="$(hostname)"
  H3_FACT_USER="$(whoami)"
  if [[ "$role" == head ]]; then
    H3_FACT_MANAGEMENT_IP="$H3_HEAD_MANAGEMENT_IP"
    H3_FACT_HOSTKEY_SHA256="$(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | awk '{print $2}')"
  else
    H3_FACT_MANAGEMENT_IP="$H3_WORKER_MANAGEMENT_IP"
    H3_FACT_HOSTKEY_SHA256="$(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | awk '{print $2}')"
  fi
  ip -o -4 addr show | awk '{print $4}' | cut -d/ -f1 | grep -Fxq "$H3_FACT_MANAGEMENT_IP" ||
    h3_die "expected management IP is not assigned"
  H3_FACT_ARCH="$(uname -m)"
  if ss -H -ltn "sport = :$H3_EXTERNAL_PORT" | grep -q .; then
    H3_FACT_EXTERNAL_PORT_FREE=0
  else
    H3_FACT_EXTERNAL_PORT_FREE=1
  fi
  if [[ -f "$H3_MODEL_LOCK" ]]; then H3_FACT_MODEL_LOCK_PRESENT=1; else H3_FACT_MODEL_LOCK_PRESENT=0; fi
  H3_FACT_RUNTIME_IMAGE_ID="UNRESOLVED"
  if command -v docker >/dev/null 2>&1; then
    H3_FACT_RUNTIME_IMAGE_ID="$(docker image inspect minimax-h3-two-node:production --format '{{.Id}}' 2>/dev/null || printf UNRESOLVED)"
  fi
}

if [[ -n "$fixture" ]]; then load_facts_file "$fixture"; else capture_local_facts; fi

for name in ROLE HOSTNAME USER MANAGEMENT_IP HOSTKEY_SHA256 ARCH EXTERNAL_PORT_FREE MODEL_LOCK_PRESENT RUNTIME_IMAGE_ID; do
  key="H3_FACT_$name"
  [[ -n "${!key:-}" ]] || h3_die "missing fact: $key"
done
h3_assert_host_role "$H3_FACT_ROLE" "$H3_FACT_HOSTNAME" "$H3_FACT_USER" \
  "$H3_FACT_MANAGEMENT_IP" "$H3_FACT_HOSTKEY_SHA256"
[[ "$H3_FACT_ARCH" == aarch64 ]] || h3_die "architecture must be aarch64"
[[ "$H3_FACT_EXTERNAL_PORT_FREE" == 1 ]] || h3_die "port 18081 is already occupied"

"$SCRIPT_DIR/build-runtime.sh" validate --phase "$phase" --lock "$runtime_lock" >/dev/null
if [[ "$phase" == deploy ]]; then
  [[ "$H3_FACT_MODEL_LOCK_PRESENT" == 1 ]] || h3_die "resolved model lock is required for deploy"
  python3 "$SCRIPT_DIR/lock-model.py" verify --lock "$H3_MODEL_LOCK" --root "$H3_MODEL_ROOT" \
    --expected-node-path "$H3_MODEL_ROOT" >/dev/null
  expected_image="$(python3 - "$runtime_lock" <<'PY'
import json,sys
print(json.load(open(sys.argv[1]))["production"]["final_image_id"])
PY
)"
  [[ "$H3_FACT_RUNTIME_IMAGE_ID" == "$expected_image" ]] || h3_die "runtime image ID mismatch"
fi

report="$(python3 - "$phase" "$H3_FACT_ROLE" "$H3_FACT_HOSTNAME" "$H3_FACT_ARCH" "$H3_FACT_RUNTIME_IMAGE_ID" <<'PY'
import json,sys
print(json.dumps({"passed":True,"phase":sys.argv[1],"role":sys.argv[2],"hostname":sys.argv[3],"architecture":sys.argv[4],"runtime_image_id":sys.argv[5]},sort_keys=True))
PY
)"
if [[ -n "$output" ]]; then
  [[ ! -e "$output" ]] || h3_die "refusing to overwrite preflight evidence: $output"
  umask 077
  printf '%s\n' "$report" >"$output"
fi
printf 'PASS phase=%s role=%s hostname=%s\n' "$phase" "$H3_FACT_ROLE" "$H3_FACT_HOSTNAME"
