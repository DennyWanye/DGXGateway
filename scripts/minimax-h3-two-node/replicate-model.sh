#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

command_name="${1:-}"
case "$command_name" in plan|execute) ;; *) h3_die "usage: replicate-model.sh plan|execute"; exit 2 ;; esac
shift

config="$ROOT/config/minimax-h3-two-node.env.example"
lock="$ROOT/manifests/minimax-h3-fl2va-lock.json"
source_root=
run_id=

while (($#)); do
  case "$1" in
    --config) config="$2"; shift 2 ;;
    --lock) lock="$2"; shift 2 ;;
    --source) source_root="$2"; shift 2 ;;
    --run-id) run_id="$2"; shift 2 ;;
    *) h3_die "unknown argument: $1"; exit 2 ;;
  esac
done

h3_load_config "$config"
status="$(python3 - "$lock" <<'PY'
import json,sys
try: print(json.load(open(sys.argv[1])).get("status","UNRESOLVED"))
except Exception: print("UNRESOLVED")
PY
)"
if [[ "$status" != resolved ]]; then
  h3_die "REFUSE unresolved model lock"
  exit 2
fi

if [[ "$command_name" == plan ]]; then
  printf 'PLAN verify-existing=%s; otherwise rsync over fabric to named staging and atomically rename; no model tree is deleted\n' "$H3_MODEL_ROOT"
  exit 0
fi

[[ "${H3_ALLOW_MODEL_REPLICATION:-0}" == 1 ]] || { h3_die "execute requires H3_ALLOW_MODEL_REPLICATION=1"; exit 2; }
[[ -n "$source_root" && -d "$source_root" ]] || { h3_die "--source must be an existing directory"; exit 2; }
[[ -n "$run_id" && "$run_id" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || { h3_die "--run-id must be an immutable UTC ID"; exit 2; }
[[ "$source_root" == "$H3_MODEL_ROOT" ]] || { h3_die "source path must equal locked head model path"; exit 2; }
[[ -f "$H3_WORKER_SSH_IDENTITY" && -f "$H3_WORKER_SSH_KNOWN_HOSTS" ]] || { h3_die "worker SSH trust files are missing"; exit 2; }

python3 "$SCRIPT_DIR/lock-model.py" verify --lock "$lock" --root "$source_root" \
  --expected-node-path "$H3_MODEL_ROOT" >/dev/null

worker_target="$H3_EXPECTED_USER@$H3_WORKER_FABRIC_A"
ssh_options=(
  -i "$H3_WORKER_SSH_IDENTITY"
  -o BatchMode=yes
  -o ConnectTimeout=8
  -o IdentitiesOnly=yes
  -o StrictHostKeyChecking=yes
  -o "UserKnownHostsFile=$H3_WORKER_SSH_KNOWN_HOSTS"
)

observed_key="$(ssh-keygen -F "$H3_WORKER_FABRIC_A" -f "$H3_WORKER_SSH_KNOWN_HOSTS" 2>/dev/null |
  sed '/^#/d' | awk 'NR==1 {print $2, $3}' | ssh-keygen -lf - | awk '{print $2}')"
identity="$(ssh "${ssh_options[@]}" "$worker_target" status)"
observed_hostname="$(sed -n 's/.*host=\([^ ]*\).*/\1/p' <<<"$identity")"
observed_user="$(sed -n 's/.*user=\([^ ]*\).*/\1/p' <<<"$identity")"
observed_management_ip="$(sed -n 's/.*management_ip=\([^ ]*\).*/\1/p' <<<"$identity")"
h3_assert_host_role worker "$observed_hostname" "$observed_user" "$observed_management_ip" "$observed_key"

remote_root="$H3_MODEL_ROOT"
staging="${remote_root}.staging-${run_id}"

remote_verify() {
  local slot="$1"
  # slot is either the literal final or staging plus a validated UTC run ID.
  # shellcheck disable=SC2029
  ssh "${ssh_options[@]}" "$worker_target" "verify-model $slot"
}

tree_status="$(ssh "${ssh_options[@]}" "$worker_target" model-tree-status)"
if [[ "$tree_status" == PRESENT ]]; then
  if remote_verify final >/dev/null; then
    printf 'PASS verify-existing worker model tree; replication not needed\n'
    exit 0
  fi
  h3_die "existing worker model tree failed verification; refusing to overwrite"
  exit 2
fi
[[ "$tree_status" == ABSENT ]] || { h3_die "unexpected worker model status: $tree_status"; exit 2; }

# shellcheck disable=SC2029
ssh "${ssh_options[@]}" "$worker_target" "prepare-model-staging $run_id"
rsync --archive --numeric-ids --partial --append-verify \
  -e "ssh -i $H3_WORKER_SSH_IDENTITY -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$H3_WORKER_SSH_KNOWN_HOSTS" \
  "$source_root/" "$worker_target:$staging/"
if ! remote_verify "staging-$run_id" >/dev/null; then
  # --append-verify can resume an interrupted short file efficiently, but a
  # pre-existing same-size corrupt file needs a full checksum comparison.
  # Keep the failed staging tree, repair only differing blocks/files, then
  # require the immutable model verifier to pass before publication.
  rsync --archive --numeric-ids --partial --checksum \
    -e "ssh -i $H3_WORKER_SSH_IDENTITY -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$H3_WORKER_SSH_KNOWN_HOSTS" \
    "$source_root/" "$worker_target:$staging/"
  remote_verify "staging-$run_id" >/dev/null
fi
# shellcheck disable=SC2029
ssh "${ssh_options[@]}" "$worker_target" "finalize-model $run_id"
remote_verify final >/dev/null
printf 'PASS replicated and verified worker model tree\n'
