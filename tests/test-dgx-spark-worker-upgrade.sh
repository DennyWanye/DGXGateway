#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
controller=$root/scripts/upgrade-dgx-spark-worker-7.5.sh
remote_payload=$root/scripts/dgx-spark-worker-7.5-remote.sh
pins=$root/plans/minimax-h3-two-node/worker-upgrade/99-dgx-worker-7.5-pins.pref
grub_recovery=$root/plans/minimax-h3-two-node/worker-upgrade/zz-dgx-worker-upgrade-recovery.cfg

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

expect_failure() {
  local expected=$1
  shift
  local output status
  set +e
  output=$("$@" 2>&1)
  status=$?
  set -e
  (( status != 0 )) || fail "command unexpectedly succeeded: $*"
  grep -Fq -- "$expected" <<<"$output" || fail "missing failure marker '$expected': $output"
}

[[ -x $controller ]] || fail "controller missing: $controller"
[[ -f $remote_payload ]] || fail "remote payload missing: $remote_payload"
[[ -f $pins ]] || fail "pin file missing: $pins"
[[ -f $grub_recovery ]] || fail "GRUB recovery file missing: $grub_recovery"

bash -n "$controller"
bash -n "$remote_payload"

contract=$($controller contract)
grep -Fq '"worker_host":"operator@192.0.2.31"' <<<"$contract" || fail "worker host not locked"
grep -Fq '"worker_hostname":"dgx-worker"' <<<"$contract" || fail "worker hostname not locked"
grep -Fq '"head_host":"operator@192.0.2.30"' <<<"$contract" || fail "head exclusion not documented"
grep -Fq '"target_dgx_release":"7.5.0"' <<<"$contract" || fail "DGX target not locked"
grep -Fq '"target_kernel":"6.17.0-1031-nvidia"' <<<"$contract" || fail "kernel target not locked"
grep -Fq '"target_driver":"580.173.02"' <<<"$contract" || fail "driver target not locked"
grep -Fq '"firmware_in_scope":false' <<<"$contract" || fail "firmware must be excluded"

expect_failure "refusing non-worker host" env DGX_UPGRADE_HOST=operator@192.0.2.30 "$controller" contract
expect_failure "refusing non-worker host" env DGX_UPGRADE_HOST=operator@192.0.2.32 "$controller" contract
expect_failure "--execute is required" "$controller" backup
expect_failure "--execute is required" "$controller" repair-workbench-key
expect_failure "--execute is required" "$controller" restage-payload
expect_failure "--execute is required" "$controller" install
expect_failure "--execute is required" "$controller" reboot
expect_failure "--execute is required" "$controller" finish-first-boot

grep -Fq 'Pin: version 7.5.0' "$pins" || fail "DGX release pin missing"
grep -Fq 'Pin: version 6.17.0-1031.31' "$pins" || fail "kernel pin missing"
grep -Fq 'Pin: version 580.173.02-0ubuntu0.24.04.1' "$pins" || fail "driver pin missing"
grep -Fq 'GRUB_TIMEOUT_STYLE=menu' "$grub_recovery" || fail "GRUB menu not enabled"
grep -Fq 'GRUB_TIMEOUT=10' "$grub_recovery" || fail "GRUB timeout not locked"

grep -Fq 'nvidia-spark-mlnx-firmware-manager.service' "$remote_payload" || \
  fail "Mellanox firmware service is not controlled"
grep -Eq 'systemctl[[:space:]]+mask.*nvidia-spark-mlnx-firmware-manager\.service' "$remote_payload" || \
  fail "Mellanox firmware service is not masked before reboot"
if grep -Eq 'fwupdmgr[[:space:]]+(upgrade|update)' "$controller" "$remote_payload"; then
  fail "firmware updater is in scope"
fi
grep -Fq 'linux-modules-nvidia-580-open-nvidia-hwe-24.04' "$remote_payload" || \
  fail "one-time boot APT guard package is not validated"
grep -Fq 'nvidia-spark-run-apt-upgrade-once' "$controller" || \
  fail "one-time boot APT service is not verified"
grep -Fq 'ibv_devinfo' "$controller" || \
  fail "HCA evidence has no ibstat fallback"
grep -Fq '52ECDF6597CFBD86D5BF4DB3CD63F8B21266DE3C' "$controller" || \
  fail "Workbench replacement key fingerprint is not locked"
grep -Fq 'cbb93f24f19c004850aef4319721a2dd2571ea72d737bb0ad5c7892c6004fea4' "$controller" || \
  fail "Workbench replacement key payload hash is not locked"
grep -Fq 'LC_ALL=C.UTF-8' "$remote_payload" || \
  fail "remote APT parser locale is not fixed"
grep -Fq "\${db:Status-Abbrev}" "$controller" || \
  fail "first-boot package state check is not dpkg-status aware"
if grep -Eq 'ibdev2netdev[[:space:]]*\|[[:space:]]*grep[^\n]*-Fq' "$controller"; then
  fail "HCA verifier uses grep -q under pipefail"
fi

if grep -Eq '192\.168\.10\.27.*(apt-get|reboot|systemctl|install)' "$controller"; then
  fail "controller contains a mutating head command"
fi

echo "PASS: DGX worker upgrade controller contract"
