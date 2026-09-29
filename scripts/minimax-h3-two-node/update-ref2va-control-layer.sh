#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

[[ $# == 3 ]] || { echo "usage: $0 head|worker TRANSACTION STAGE" >&2; exit 64; }
role=$1
tx=$2
stage=$3
case "$role" in
  head) expected=dgx-head ;;
  worker) expected=dgx-worker ;;
  *) echo "invalid role" >&2; exit 64 ;;
esac
[[ $(hostname -s) == "$expected" ]] || { echo "role mismatch" >&2; exit 1; }
[[ $tx =~ ^[A-Za-z0-9_-]+$ ]] || { echo "invalid transaction" >&2; exit 1; }
[[ -d "$stage" && -f "$stage/stage.sha256" ]] || { echo "stage missing" >&2; exit 1; }

state="/var/lib/minimax-h3/ref2va-updates/$tx"
[[ ! -e "$state" ]] || { echo "transaction exists" >&2; exit 1; }
install -d -o root -g root -m 0700 "$state" "$state/preimage"
targets="$state/targets.tsv"
: >"$targets"

record_target() {
  local destination=$1 copy="$state/preimage$destination"
  if [[ -e "$destination" ]]; then
    [[ -f "$destination" && ! -L "$destination" ]] || { echo "unsafe target: $destination" >&2; return 1; }
    install -d -m 0700 "$(dirname "$copy")"
    cp -a -- "$destination" "$copy"
    printf 'present\t%s\n' "$destination" >>"$targets"
  else
    printf 'missing\t%s\n' "$destination" >>"$targets"
  fi
}

rollback_update() {
  set +e
  tac "$targets" | while IFS=$'\t' read -r status destination; do
    if [[ $status == present ]]; then
      cp -a -- "$state/preimage$destination" "$destination"
    else
      rm -f -- "$destination"
    fi
  done
  systemctl daemon-reload
  printf 'ROLLED_BACK\n' >"$state/state"
}
trap rollback_update ERR

install_target() {
  local source=$1 destination=$2 mode=$3 temporary
  [[ -f "$source" && ! -L "$source" ]] || { echo "unsafe source: $source" >&2; return 1; }
  record_target "$destination"
  install -d -m 0755 "$(dirname "$destination")"
  temporary="$(dirname "$destination")/.$(basename "$destination").$tx"
  install -o root -g root -m "$mode" -- "$source" "$temporary"
  mv -f -- "$temporary" "$destination"
}

config_new="$state/minimax-h3.env.new"
cp -a -- /etc/minimax-h3/minimax-h3.env "$config_new"
chmod 0600 "$config_new"
set_key() {
  local key=$1 value=$2
  if grep -q "^${key}=" "$config_new"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$config_new"
  else
    printf '%s=%s\n' "$key" "$value" >>"$config_new"
  fi
}
fl2va=/home/operator/dgx-models/video-generation/snapshots/minimax-h3-heretic-hmnsfw-v1-h3j-20260901-001/FL2VA
set_key H3_MODEL_ROOT "$fl2va"
set_key H3_MODEL_PATH "$fl2va"
set_key H3_MODEL_LOCK /etc/minimax-h3/minimax-h3-fl2va-lock.json
set_key H3_PROFILES_PATH /etc/minimax-h3/profiles-v1.json
set_key H3_SELECTED_PROFILE heretic-hmnsfw-v1
set_key H3_RUNTIME_LOCK /etc/minimax-h3/minimax-h3-runtime-lock.json
set_key H3_RUNTIME_IMAGE minimax-h3-two-node:production
set_key H3_RUNTIME_OVERLAY /etc/minimax-h3/runtime-overlays/worker.py
set_key H3_RESTORE_LLM_ON_STOP 0
set_key H3_MAC_LOCK_ATTESTATION_PATH /run/minimax-h3-two-node/mac-lock-attestation.json

bash -n \
  "$stage/scripts/minimax-h3-two-node/minimax-h3-runtime-action" \
  "$stage/scripts/minimax-h3-two-node/minimax-h3-worker-action" \
  "$stage/scripts/minimax-h3-two-node/common.sh"
python3 -m py_compile \
  "$stage/scripts/minimax-h3-two-node/minimax-h3-control" \
  "$stage/scripts/minimax-h3-two-node/gateway.py"
H3_TESTING=1 bash -c 'source "$1"; h3_load_config "$2"' _ \
  "$stage/scripts/minimax-h3-two-node/common.sh" "$config_new"

install_target "$stage/scripts/minimax-h3-two-node/minimax-h3-control" /usr/local/sbin/minimax-h3-control 0755
install_target "$stage/scripts/minimax-h3-two-node/gateway.py" /usr/local/libexec/minimax-h3-gateway 0755
install_target "$stage/scripts/minimax-h3-two-node/worker-dispatch" /usr/local/libexec/minimax-h3-worker-dispatch 0755
install_target "$stage/scripts/minimax-h3-two-node/worker-model-dispatch" /usr/local/libexec/minimax-h3-worker-model-dispatch 0755
install_target "$stage/scripts/minimax-h3-two-node/minimax-h3-action" /usr/local/libexec/minimax-h3-action 0755
install_target "$stage/scripts/minimax-h3-two-node/minimax-h3-runtime-action" /usr/local/libexec/minimax-h3-runtime-action 0755
install_target "$stage/scripts/minimax-h3-two-node/minimax-h3-worker-action" /usr/local/libexec/minimax-h3-worker-action 0755
install_target "$stage/scripts/minimax-h3-two-node/common.sh" /usr/local/libexec/common.sh 0644
install_target "$stage/scripts/minimax-h3-two-node/minimax-h3-llm-oracle" /usr/local/libexec/minimax-h3-llm-oracle 0755
install_target "$stage/manifests/minimax-h3-request-policy-v1.json" /etc/minimax-h3/request-policy-v1.json 0644
install_target "$stage/manifests/minimax-h3-request-policy-v2.json" /etc/minimax-h3/request-policy-v2.json 0644
install_target "$stage/manifests/minimax-h3-coexistence-budget.json" /etc/minimax-h3/coexistence-budget.json 0600
install_target "$stage/manifests/minimax-h3-profiles-v1.json" /etc/minimax-h3/profiles-v1.json 0600
install_target "$stage/manifests/minimax-h3-fl2va-lock.json" /etc/minimax-h3/minimax-h3-fl2va-lock.json 0644
install_target "$stage/manifests/minimax-h3-runtime-lock.json" /etc/minimax-h3/minimax-h3-runtime-lock.json 0644
install_target "$stage/manifests/minimax-h3-ref2va-minimal-lock.json" /etc/minimax-h3/minimax-h3-ref2va-minimal-lock.json 0600
install_target "$stage/manifests/minimax-h3-ref2va-runtime-lock.json" /etc/minimax-h3/minimax-h3-ref2va-runtime-sealed.json 0600
install_target "$stage/overlays/minimax-h3-two-node/worker.py" /etc/minimax-h3/runtime-overlays/worker.py 0644
install_target "$stage/systemd/minimax-h3-two-node.service" /etc/systemd/system/minimax-h3-two-node.service 0644
install_target "$stage/systemd/minimax-h3-gateway.service" /etc/systemd/system/minimax-h3-gateway.service 0644
install_target "$config_new" /etc/minimax-h3/minimax-h3.env 0600

H3_TESTING=1 bash -c 'source /usr/local/libexec/common.sh; h3_load_config /etc/minimax-h3/minimax-h3.env'
systemctl daemon-reload
{
  printf 'transaction=%s\nrole=%s\nhostname=%s\ncommitted_at=%s\n' "$tx" "$role" "$(hostname -s)" "$(date -u +%FT%TZ)"
  sha256sum \
    /usr/local/sbin/minimax-h3-control \
    /usr/local/libexec/minimax-h3-gateway \
    /usr/local/libexec/minimax-h3-runtime-action \
    /usr/local/libexec/minimax-h3-worker-action \
    /usr/local/libexec/common.sh \
    /etc/minimax-h3/profiles-v1.json \
    /etc/minimax-h3/minimax-h3-ref2va-minimal-lock.json \
    /etc/minimax-h3/minimax-h3-ref2va-runtime-sealed.json \
    /etc/minimax-h3/minimax-h3.env
} >"$state/installed.sha256.txt"
chmod 0600 "$state/installed.sha256.txt"
printf 'COMMITTED\n' >"$state/state"
trap - ERR
echo "COMMITTED role=$role tx=$tx"
