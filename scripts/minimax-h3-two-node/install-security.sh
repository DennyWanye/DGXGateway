#!/bin/bash
set -Eeuo pipefail

umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd -P)
INSTALL_ROOT=${H3_INSTALL_ROOT:-/}
STATE_DIR="$INSTALL_ROOT/var/lib/minimax-h3/security"
TX_ROOT="$STATE_DIR/firewall-transactions"
CURRENT_TX="$STATE_DIR/current-firewall-transaction"
ETC_DIR="$INSTALL_ROOT/etc/minimax-h3"
LIBEXEC_DIR="$INSTALL_ROOT/usr/local/libexec"
SBIN_DIR="$INSTALL_ROOT/usr/local/sbin"
UNIT_DIR="$INSTALL_ROOT/etc/systemd/system"
TOKEN_CREATED_MARKER="$STATE_DIR/gateway-token.created-by-installer"
RAY_TOKEN_CREATED_MARKER="$STATE_DIR/ray-token.created-by-installer"
HASH_RECEIPT="$STATE_DIR/installed.sha256"
RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)-$$

UFW_COMMENT_SSH=minimax-h3-management-ssh-v1
UFW_COMMENT_GATEWAY=minimax-h3-gateway-18081-v1
UFW_COMMENT_FABRIC=minimax-h3-fabric-internal-v1
UFW_COMMENT_DENY=minimax-h3-deny-management-internal-v1
OWNED_COMMENTS="$UFW_COMMENT_SSH|$UFW_COMMENT_GATEWAY|$UFW_COMMENT_FABRIC|$UFW_COMMENT_DENY"
RULES_APPLYING=0
ACTIVE_TX_DIR=

die() {
  echo "FAIL: $*" >&2
  exit 1
}

require_role() {
  local role=$1 expected actual
  case "$role" in
    head) expected=dgx-head ;;
    worker) expected=dgx-worker ;;
    *) die "role must be head or worker" ;;
  esac
  if [[ "$INSTALL_ROOT" == / ]]; then
    [[ ${EUID} -eq 0 ]] || die "installation requires root"
    actual=$(hostname -s)
    [[ "$actual" == "$expected" ]] || die "role mismatch: expected $expected, got $actual"
  fi
}

install_identical_or_new() {
  local source=$1 destination=$2 mode=$3
  if [[ -e "$destination" ]]; then
    [[ -f "$destination" && ! -L "$destination" ]] || die "refusing non-regular destination: $destination"
    cmp -s -- "$source" "$destination" || die "refusing to overwrite different file: $destination"
    chmod "$mode" "$destination"
    return
  fi
  install -m "$mode" -- "$source" "$destination"
}

install_files() {
  install -d -m 0700 "$STATE_DIR" "$TX_ROOT"
  install -d -m 0755 "$ETC_DIR" "$ETC_DIR/runtime-overlays" "$LIBEXEC_DIR" "$SBIN_DIR" "$UNIT_DIR"
  install_identical_or_new "$SCRIPT_DIR/minimax-h3-control" "$SBIN_DIR/minimax-h3-control" 0755
  install_identical_or_new "$SCRIPT_DIR/gateway.py" "$LIBEXEC_DIR/minimax-h3-gateway" 0755
  install_identical_or_new "$SCRIPT_DIR/worker-dispatch" "$LIBEXEC_DIR/minimax-h3-worker-dispatch" 0755
  install_identical_or_new "$SCRIPT_DIR/worker-model-dispatch" "$LIBEXEC_DIR/minimax-h3-worker-model-dispatch" 0755
  install_identical_or_new "$SCRIPT_DIR/minimax-h3-action" "$LIBEXEC_DIR/minimax-h3-action" 0755
  install_identical_or_new "$SCRIPT_DIR/minimax-h3-runtime-action" "$LIBEXEC_DIR/minimax-h3-runtime-action" 0755
  install_identical_or_new "$SCRIPT_DIR/minimax-h3-worker-action" "$LIBEXEC_DIR/minimax-h3-worker-action" 0755
  install_identical_or_new "$SCRIPT_DIR/common.sh" "$LIBEXEC_DIR/common.sh" 0644
  install_identical_or_new "$SCRIPT_DIR/minimax-h3-llm-oracle" "$LIBEXEC_DIR/minimax-h3-llm-oracle" 0755
  install_identical_or_new "$SCRIPT_DIR/ufw-rollback-guard.sh" "$LIBEXEC_DIR/minimax-h3-ufw-rollback-guard" 0700
  install_identical_or_new "$REPO_ROOT/manifests/minimax-h3-request-policy-v1.json" "$ETC_DIR/request-policy-v1.json" 0644
  install_identical_or_new "$REPO_ROOT/manifests/minimax-h3-request-policy-v2.json" "$ETC_DIR/request-policy-v2.json" 0644
  install_identical_or_new "$REPO_ROOT/manifests/minimax-h3-coexistence-budget.json" "$ETC_DIR/coexistence-budget.json" 0600
  install_identical_or_new "$REPO_ROOT/manifests/minimax-h3-profiles-v1.json" "$ETC_DIR/profiles-v1.json" 0600
  install_identical_or_new "$REPO_ROOT/overlays/minimax-h3-two-node/worker.py" "$ETC_DIR/runtime-overlays/worker.py" 0644
  install_identical_or_new "$REPO_ROOT/systemd/minimax-h3-two-node.service" "$UNIT_DIR/minimax-h3-two-node.service" 0644
  install_identical_or_new "$REPO_ROOT/systemd/minimax-h3-gateway.service" "$UNIT_DIR/minimax-h3-gateway.service" 0644

  if [[ ! -e "$ETC_DIR/gateway.token" ]]; then
    python3 - "$ETC_DIR/gateway.token" <<'PY'
import os
import secrets
import sys

descriptor = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w", encoding="ascii") as handle:
    handle.write(secrets.token_urlsafe(48) + "\n")
PY
    install -m 0600 /dev/null "$TOKEN_CREATED_MARKER"
  else
    [[ -f "$ETC_DIR/gateway.token" && ! -L "$ETC_DIR/gateway.token" ]] || die "unsafe gateway token path"
    python3 - "$ETC_DIR/gateway.token" <<'PY' || die "gateway token must be mode 0600"
import os
import stat
import sys

raise SystemExit(0 if stat.S_IMODE(os.stat(sys.argv[1]).st_mode) == 0o600 else 1)
PY
  fi

  if [[ ! -e "$ETC_DIR/ray.token" ]]; then
    python3 - "$ETC_DIR/ray.token" <<'PY'
import os
import secrets
import sys

descriptor = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w", encoding="ascii") as handle:
    handle.write(secrets.token_urlsafe(48) + "\n")
PY
    install -m 0600 /dev/null "$RAY_TOKEN_CREATED_MARKER"
  else
    [[ -f "$ETC_DIR/ray.token" && ! -L "$ETC_DIR/ray.token" ]] || die "unsafe Ray token path"
    python3 - "$ETC_DIR/ray.token" <<'PY' || die "Ray token must be mode 0600"
import os
import stat
import sys

raise SystemExit(0 if stat.S_IMODE(os.stat(sys.argv[1]).st_mode) == 0o600 else 1)
PY
  fi

  sha256sum \
    "$SBIN_DIR/minimax-h3-control" \
    "$LIBEXEC_DIR/minimax-h3-gateway" \
    "$LIBEXEC_DIR/minimax-h3-worker-dispatch" \
    "$LIBEXEC_DIR/minimax-h3-worker-model-dispatch" \
    "$LIBEXEC_DIR/minimax-h3-action" \
    "$LIBEXEC_DIR/minimax-h3-runtime-action" \
    "$LIBEXEC_DIR/minimax-h3-worker-action" \
    "$LIBEXEC_DIR/common.sh" \
    "$LIBEXEC_DIR/minimax-h3-llm-oracle" \
    "$LIBEXEC_DIR/minimax-h3-ufw-rollback-guard" \
    "$ETC_DIR/request-policy-v1.json" \
    "$ETC_DIR/request-policy-v2.json" \
    "$ETC_DIR/profiles-v1.json" \
    "$ETC_DIR/runtime-overlays/worker.py" \
    "$UNIT_DIR/minimax-h3-two-node.service" \
    "$UNIT_DIR/minimax-h3-gateway.service" >"$HASH_RECEIPT"
  chmod 0600 "$HASH_RECEIPT"
  if [[ "$INSTALL_ROOT" == / ]]; then
    systemctl daemon-reload
  fi
}

set_role_network() {
  local role=$1
  MANAGEMENT_IF=wlP9s9
  FABRIC_A_IF=enp1s0f0np0
  FABRIC_B_IF=enP2p1s0f0np0
  if [[ "$role" == head ]]; then
    MANAGEMENT_IP=192.0.2.30
    FABRIC_A_IP=192.0.2.33
    FABRIC_A_PEER=192.0.2.34
    FABRIC_B_IP=192.0.2.35
    FABRIC_B_PEER=192.0.2.36
  else
    MANAGEMENT_IP=192.0.2.31
    FABRIC_A_IP=192.0.2.34
    FABRIC_A_PEER=192.0.2.33
    FABRIC_B_IP=192.0.2.36
    FABRIC_B_PEER=192.0.2.35
  fi
}

require_command() {
  command -v "$1" >/dev/null || die "required command missing: $1"
}

require_safe_firewall_preimage() {
  local role=$1 first_line backend path
  [[ "$INSTALL_ROOT" == / ]] || die "firewall transactions require the real root filesystem"
  for command in ufw systemd-run systemctl ip ss nft iptables-save ip6tables-save sha256sum flock sysctl; do
    require_command "$command"
  done
  for path in /etc/ufw/user.rules /etc/ufw/user6.rules /etc/ufw/ufw.conf /etc/default/ufw; do
    [[ -f "$path" && ! -L "$path" ]] || die "ambiguous UFW preimage: $path"
    [[ $(stat -c '%u' "$path") == 0 ]] || die "UFW preimage is not root-owned: $path"
  done
  first_line=$(ufw status | sed -n '1p')
  case "$first_line" in
    "Status: active") PREVIOUS_ACTIVE=active ;;
    "Status: inactive") PREVIOUS_ACTIVE=inactive ;;
    *) die "ambiguous UFW state: $first_line" ;;
  esac
  ufw status numbered | grep -Eq "$OWNED_COMMENTS" && die "stale H3-owned UFW rule or unfinished transaction exists"
  [[ ! -e "$CURRENT_TX" ]] || die "firewall transaction pointer already exists"
  backend=$(readlink -f /etc/alternatives/iptables)
  case "$(basename "$backend")" in
    iptables-nft|iptables-legacy|xtables-nft-multi|xtables-legacy-multi) ;;
    *) die "unsupported or ambiguous iptables backend: $backend" ;;
  esac

  set_role_network "$role"
  for path in "$MANAGEMENT_IF" "$FABRIC_A_IF" "$FABRIC_B_IF"; do
    ip link show dev "$path" >/dev/null || die "required interface missing: $path"
  done
  ip -o -4 addr show dev "$MANAGEMENT_IF" | grep -Fq " $MANAGEMENT_IP/" || die "management address mismatch"
  ip -o -4 addr show dev "$FABRIC_A_IF" | grep -Fq " $FABRIC_A_IP/" || die "fabric A address mismatch"
  ip -o -4 addr show dev "$FABRIC_B_IF" | grep -Fq " $FABRIC_B_IP/" || die "fabric B address mismatch"
  ss -H -lnt | grep -Eq "($MANAGEMENT_IP|0\.0\.0\.0):22[[:space:]]" || die "management SSH listener is not present"
  if [[ "$role" == head ]]; then
    ss -H -lnt | grep -Eq "($MANAGEMENT_IP|0\.0\.0\.0):11434[[:space:]]" || die "head 11434 listener is not present"
  fi
  ss -H -lnt | grep -Eq "($MANAGEMENT_IP|0\.0\.0\.0):(6379|8000|807[6-9]|8265|100[0-2][0-9]|1003[01]|29500)[[:space:]]" \
    && die "internal Ray/runtime listener already exposed on management"
  return 0
}

capture_firewall_preimage() {
  local role=$1 tx_dir=$2 preimage
  preimage="$tx_dir/preimage"
  install -d -m 0700 "$tx_dir" "$preimage"
  cp -a -- /etc/ufw/user.rules "$preimage/user.rules"
  cp -a -- /etc/ufw/user6.rules "$preimage/user6.rules"
  cp -a -- /etc/ufw/ufw.conf "$preimage/ufw.conf"
  cp -a -- /etc/default/ufw "$preimage/default.ufw"
  ufw status verbose >"$preimage/ufw-status-verbose.txt"
  ufw status numbered >"$preimage/ufw-status-numbered.txt"
  ufw show added >"$preimage/ufw-show-added.txt"
  iptables-save >"$preimage/iptables-save.txt"
  ip6tables-save >"$preimage/ip6tables-save.txt"
  nft list ruleset >"$preimage/nft-list-ruleset.txt"
  if nft list table inet mihomo >/dev/null 2>&1; then
    nft list table inet mihomo \
      | sed -E 's/counter packets [0-9]+ bytes [0-9]+/counter/g' \
      >"$preimage/nft-mihomo-normalized.txt"
  fi
  ss -H -lntup >"$preimage/listeners.txt"
  sysctl -n net.ipv4.ip_forward >"$preimage/ipv4-forward.txt"
  readlink -f /etc/alternatives/iptables >"$preimage/iptables-backend.txt"
  printf '%s\n' "$role" >"$tx_dir/role"
  printf '%s\n' "$PREVIOUS_ACTIVE" >"$tx_dir/previous-active"
  install -m 0700 "$SCRIPT_DIR/ufw-rollback-guard.sh" "$tx_dir/rollback-guard.sh"
  {
    printf 'TX_DIR=%q\n' "$tx_dir"
    printf 'UFW_COMMAND=%q\n' "$(command -v ufw)"
    printf 'IPTABLES_SAVE_COMMAND=%q\n' "$(command -v iptables-save)"
    printf 'IP6TABLES_SAVE_COMMAND=%q\n' "$(command -v ip6tables-save)"
    printf 'USER_RULES=%q\n' /etc/ufw/user.rules
    printf 'USER6_RULES=%q\n' /etc/ufw/user6.rules
    printf 'UFW_CONF=%q\n' /etc/ufw/ufw.conf
    printf 'DEFAULT_UFW=%q\n' /etc/default/ufw
    printf 'PREVIOUS_ACTIVE=%q\n' "$PREVIOUS_ACTIVE"
  } >"$tx_dir/transaction.env"
  chmod 0600 "$tx_dir/transaction.env"
  sha256sum "$preimage"/* "$tx_dir/transaction.env" "$tx_dir/rollback-guard.sh" >"$tx_dir/preimage.sha256"
  chmod 0600 "$tx_dir/preimage.sha256"
  printf '%s\n' PREIMAGE_CAPTURED >"$tx_dir/state"
}

guard_unit_for() {
  printf 'minimax-h3-ufw-rollback-%s' "$(basename "$1" | tr -cd 'A-Za-z0-9_-')"
}

arm_rollback_guard() {
  local tx_dir=$1 unit
  unit=$(guard_unit_for "$tx_dir")
  printf '%s\n' "$unit" >"$tx_dir/guard-unit"
  systemd-run --unit="$unit" --on-active=120s --collect --property=Type=oneshot \
    /bin/bash "$tx_dir/rollback-guard.sh" "$tx_dir" >/dev/null
  systemctl is-active --quiet "$unit.timer" || die "timed firewall rollback guard did not arm"
  printf '%s\n' GUARD_ARMED >"$tx_dir/state"
}

cancel_guard_after_verification() {
  local tx_dir=$1 unit
  unit=$(cat "$tx_dir/guard-unit")
  systemctl is-active --quiet "$unit.service" && die "rollback service is already running"
  systemctl stop "$unit.timer"
  systemctl is-active --quiet "$unit.timer" && die "rollback timer is still active"
  systemctl reset-failed "$unit.timer" "$unit.service" >/dev/null 2>&1 || true
}

management_ssh_rule() {
  ufw allow in on "$MANAGEMENT_IF" from 192.0.2.27/24 to "$MANAGEMENT_IP" port 22 proto tcp comment "$UFW_COMMENT_SSH"
}

gateway_rule() {
  ufw allow in on "$MANAGEMENT_IF" from 192.0.2.27/24 to 192.0.2.30 port 18081 proto tcp comment "$UFW_COMMENT_GATEWAY"
}

fabric_rules() {
  # Ray pins its control ports, but NCCL selects peer-listener TCP ports
  # dynamically when a communicator is created for the first real request.
  # Scope those dynamic listeners to the exact point-to-point interface,
  # source, and destination instead of exposing them on the management LAN.
  ufw allow in on "$FABRIC_A_IF" from "$FABRIC_A_PEER" to "$FABRIC_A_IP" proto tcp comment "$UFW_COMMENT_FABRIC"
  ufw allow in on "$FABRIC_B_IF" from "$FABRIC_B_PEER" to "$FABRIC_B_IP" proto tcp comment "$UFW_COMMENT_FABRIC"
  if ! existing_roce_allow "$FABRIC_A_IF" "$FABRIC_A_PEER" "$FABRIC_A_IP"; then
    ufw allow in on "$FABRIC_A_IF" from "$FABRIC_A_PEER" to "$FABRIC_A_IP" port 4791 proto udp comment "$UFW_COMMENT_FABRIC"
  fi
  if ! existing_roce_allow "$FABRIC_B_IF" "$FABRIC_B_PEER" "$FABRIC_B_IP"; then
    ufw allow in on "$FABRIC_B_IF" from "$FABRIC_B_PEER" to "$FABRIC_B_IP" port 4791 proto udp comment "$UFW_COMMENT_FABRIC"
  fi
}

existing_roce_allow() {
  local interface=$1 peer=$2 local_ip=$3
  ufw status numbered \
    | grep -F "$local_ip 4791/udp on $interface" \
    | grep -F "$peer" \
    | grep -Fq "ALLOW IN"
}

management_denies() {
  local port
  for port in 6379 8000 8076:8079 8265 10000:10031 29500; do
    ufw deny in on "$MANAGEMENT_IF" from 192.0.2.27/24 to "$MANAGEMENT_IP" port "$port" proto tcp comment "$UFW_COMMENT_DENY"
  done
}

normalize_non_h3_status() {
  # Compare only semantic numbered rules. This intentionally ignores the
  # headers that appear when an inactive worker is enabled for the first time.
  sed -n '/^\[/p' "$1" \
    | awk -v owned="$OWNED_COMMENTS" '$0 !~ owned' \
    | sed -E 's/^\[[[:space:]]*[0-9]+\]/[]/' >"$2"
}

verify_firewall() {
  local role=$1 tx_dir=$2 phase=$3 post protected_port
  [[ "$phase" == prepare || "$phase" == finalize ]] || die "invalid firewall verification phase"
  post="$tx_dir/post-$phase"
  [[ ! -e "$post" ]] || die "refusing to overwrite firewall verification evidence: $post"
  install -d -m 0700 "$post"
  ufw status verbose >"$post/ufw-status-verbose.txt"
  ufw status numbered >"$post/ufw-status-numbered.txt"
  ufw show added >"$post/ufw-show-added.txt"
  iptables-save >"$post/iptables-save.txt"
  ip6tables-save >"$post/ip6tables-save.txt"
  nft list ruleset >"$post/nft-list-ruleset.txt"
  if [[ -f "$tx_dir/preimage/nft-mihomo-normalized.txt" ]]; then
    nft list table inet mihomo \
      | sed -E 's/counter packets [0-9]+ bytes [0-9]+/counter/g' \
      >"$post/nft-mihomo-normalized.txt"
    cmp -s "$tx_dir/preimage/nft-mihomo-normalized.txt" "$post/nft-mihomo-normalized.txt" \
      || die "unrelated inet mihomo table changed"
  fi
  ss -H -lntup >"$post/listeners.txt"
  sysctl -n net.ipv4.ip_forward >"$post/ipv4-forward.txt"

  [[ $(sed -n '1p' "$post/ufw-status-numbered.txt") == "Status: active" ]] || die "UFW enforcement is not active"
  grep -Fq "$UFW_COMMENT_SSH" "$post/ufw-status-numbered.txt" || die "management SSH allow is missing"
  grep -Fq "$UFW_COMMENT_FABRIC" "$post/ufw-status-numbered.txt" || die "fabric peer allows are missing"
  grep -Fq "$UFW_COMMENT_DENY" "$post/ufw-status-numbered.txt" || die "management internal-port denies are missing"
  grep -F "$FABRIC_A_IP/tcp on $FABRIC_A_IF" "$post/ufw-status-numbered.txt" \
    | grep -F "$FABRIC_A_PEER" | grep -Fq "ALLOW IN" || die "fabric A dynamic NCCL TCP allow is missing"
  grep -F "$FABRIC_B_IP/tcp on $FABRIC_B_IF" "$post/ufw-status-numbered.txt" \
    | grep -F "$FABRIC_B_PEER" | grep -Fq "ALLOW IN" || die "fabric B dynamic NCCL TCP allow is missing"
  if [[ "$role" == head ]]; then
    grep -Fq "$UFW_COMMENT_GATEWAY" "$post/ufw-status-numbered.txt" || die "gateway allow is missing"
  fi
  grep -Fq "Default: deny (incoming)" "$post/ufw-status-verbose.txt" || die "incoming default policy is not deny"
  cmp -s "$tx_dir/preimage/ipv4-forward.txt" "$post/ipv4-forward.txt" || die "IPv4 forwarding changed"

  normalize_non_h3_status "$tx_dir/preimage/ufw-status-numbered.txt" "$tx_dir/preimage/non-h3-normalized.txt"
  normalize_non_h3_status "$post/ufw-status-numbered.txt" "$post/non-h3-normalized.txt"
  cmp -s "$tx_dir/preimage/non-h3-normalized.txt" "$post/non-h3-normalized.txt" \
    || die "non-H3 UFW rules changed during transaction"
  if [[ "$role" == head ]]; then
    for protected_port in 11434 8188; do
      grep -F "$protected_port" "$tx_dir/preimage/non-h3-normalized.txt" >"$tx_dir/preimage/port-$protected_port.txt" || true
      grep -F "$protected_port" "$post/non-h3-normalized.txt" >"$post/port-$protected_port.txt" || true
      cmp -s "$tx_dir/preimage/port-$protected_port.txt" "$post/port-$protected_port.txt" \
        || die "protected head rule changed for port $protected_port"
    done
  fi
}

rollback_partial_rules() {
  local exit_code=$?
  trap - ERR
  if [[ "$RULES_APPLYING" == 1 && -n "$ACTIVE_TX_DIR" ]]; then
    /bin/bash "$ACTIVE_TX_DIR/rollback-guard.sh" "$ACTIVE_TX_DIR" || true
  fi
  exit "$exit_code"
}

prepare_firewall() {
  local role=$1 tx_dir pointer_tmp
  tx_dir="$TX_ROOT/$RUN_ID-$role"
  require_safe_firewall_preimage "$role"
  capture_firewall_preimage "$role" "$tx_dir"
  arm_rollback_guard "$tx_dir"
  RULES_APPLYING=1
  ACTIVE_TX_DIR=$tx_dir
  trap rollback_partial_rules ERR
  printf '%s\n' APPLYING >"$tx_dir/state"
  set_role_network "$role"
  management_ssh_rule
  if [[ "$role" == head ]]; then gateway_rule; fi
  fabric_rules
  management_denies
  ufw default deny incoming
  # The production enable step is deliberately explicit: ufw --force enable.
  ufw --force enable
  [[ ${H3_FIREWALL_INJECT_FAILURE-} != after-enable ]] || die "injected firewall failure after enable"
  verify_firewall "$role" "$tx_dir" prepare
  printf '%s\n' APPLY_PENDING >"$tx_dir/state"
  pointer_tmp="$CURRENT_TX.tmp.$$"
  printf '%s\n' "$tx_dir" >"$pointer_tmp"
  chmod 0600 "$pointer_tmp"
  mv -f -- "$pointer_tmp" "$CURRENT_TX"
  RULES_APPLYING=0
  ACTIVE_TX_DIR=
  trap - ERR
  echo "APPLY_PENDING role=$role transaction=$(basename "$tx_dir") guard=ACTIVE"
}

resolve_transaction() {
  local role=$1 tx_id=${2-} tx_dir
  if [[ -n "$tx_id" ]]; then
    [[ "$tx_id" =~ ^[A-Za-z0-9_-]+$ ]] || die "invalid transaction id"
    tx_dir="$TX_ROOT/$tx_id"
  else
    [[ -f "$CURRENT_TX" && ! -L "$CURRENT_TX" ]] || die "current firewall transaction is missing"
    tx_dir=$(cat "$CURRENT_TX")
  fi
  [[ "$tx_dir" == "$TX_ROOT/"* && -d "$tx_dir" ]] || die "unsafe firewall transaction path"
  [[ $(cat "$tx_dir/role") == "$role" ]] || die "transaction role mismatch"
  RESOLVED_TX_DIR=$tx_dir
}

finalize_firewall() {
  local role=$1 tx_id=$2 state
  resolve_transaction "$role" "$tx_id"
  state=$(cat "$RESOLVED_TX_DIR/state")
  [[ "$state" == APPLY_PENDING ]] || die "transaction is not ready to finalize: $state"
  verify_firewall "$role" "$RESOLVED_TX_DIR" finalize
  cancel_guard_after_verification "$RESOLVED_TX_DIR"
  printf '%s\n' FINALIZED >"$RESOLVED_TX_DIR/state"
  echo "FINALIZED role=$role transaction=$(basename "$RESOLVED_TX_DIR") guard=CANCELLED"
}

rollback_firewall() {
  local role=$1 tx_id=${2-} unit
  resolve_transaction "$role" "$tx_id"
  unit=$(cat "$RESOLVED_TX_DIR/guard-unit")
  systemctl stop "$unit.timer" >/dev/null 2>&1 || true
  /bin/bash "$RESOLVED_TX_DIR/rollback-guard.sh" "$RESOLVED_TX_DIR"
  [[ $(cat "$RESOLVED_TX_DIR/state") == ROLLED_BACK ]] || die "firewall rollback did not reach ROLLED_BACK"
  if [[ -f "$CURRENT_TX" && $(cat "$CURRENT_TX") == "$RESOLVED_TX_DIR" ]]; then rm -f -- "$CURRENT_TX"; fi
  echo "ROLLED_BACK role=$role transaction=$(basename "$RESOLVED_TX_DIR")"
}

rollback_files() {
  local role=$1
  if [[ "$INSTALL_ROOT" == / ]]; then
    ! systemctl is-active --quiet minimax-h3-two-node.service || die "stop H3 through its lifecycle owner before rollback"
    ! systemctl is-active --quiet minimax-h3-gateway.service || die "stop the H3 gateway before rollback"
    if [[ -f "$CURRENT_TX" ]]; then
      rollback_firewall "$role"
    elif ufw status numbered | grep -Eq "$OWNED_COMMENTS"; then
      die "H3 UFW rules exist without a recoverable transaction preimage"
    fi
  fi
  [[ -f "$HASH_RECEIPT" ]] || die "installation hash receipt is missing"
  sha256sum -c "$HASH_RECEIPT" >/dev/null || die "installed files changed; refusing destructive rollback"
  [[ -f "$ETC_DIR/coexistence-budget.json" && ! -L "$ETC_DIR/coexistence-budget.json" ]] || die "unsafe coexistence budget path"
  rm -f -- \
    "$SBIN_DIR/minimax-h3-control" \
    "$LIBEXEC_DIR/minimax-h3-gateway" \
    "$LIBEXEC_DIR/minimax-h3-worker-dispatch" \
    "$LIBEXEC_DIR/minimax-h3-worker-model-dispatch" \
    "$LIBEXEC_DIR/minimax-h3-action" \
    "$LIBEXEC_DIR/minimax-h3-runtime-action" \
    "$LIBEXEC_DIR/minimax-h3-worker-action" \
    "$LIBEXEC_DIR/common.sh" \
    "$LIBEXEC_DIR/minimax-h3-llm-oracle" \
    "$LIBEXEC_DIR/minimax-h3-ufw-rollback-guard" \
    "$ETC_DIR/request-policy-v1.json" \
    "$ETC_DIR/request-policy-v2.json" \
    "$ETC_DIR/profiles-v1.json" \
    "$ETC_DIR/runtime-overlays/worker.py" \
    "$ETC_DIR/coexistence-budget.json" \
    "$UNIT_DIR/minimax-h3-two-node.service" \
    "$UNIT_DIR/minimax-h3-gateway.service"
  if [[ -f "$TOKEN_CREATED_MARKER" ]]; then rm -f -- "$ETC_DIR/gateway.token" "$TOKEN_CREATED_MARKER"; fi
  if [[ -f "$RAY_TOKEN_CREATED_MARKER" ]]; then rm -f -- "$ETC_DIR/ray.token" "$RAY_TOKEN_CREATED_MARKER"; fi
  rm -f -- "$HASH_RECEIPT"
  if [[ "$INSTALL_ROOT" == / ]]; then systemctl daemon-reload; fi
}

usage() {
  echo "usage: $0 install|rollback|prepare-firewall ROLE [TRANSACTION]" >&2
  echo "       $0 finalize-firewall|rollback-firewall ROLE TRANSACTION" >&2
  exit 64
}

[[ $# -ge 2 && $# -le 3 ]] || usage
operation=$1
role=$2
tx_id=${3-}
require_role "$role"

case "$operation" in
  install)
    [[ -z "$tx_id" ]] || usage
    install_files
    echo "INSTALLED_NOT_ENABLED role=$role firewall=UNCHANGED"
    ;;
  prepare-firewall)
    [[ -z "$tx_id" ]] || usage
    prepare_firewall "$role"
    ;;
  finalize-firewall)
    [[ -n "$tx_id" ]] || usage
    finalize_firewall "$role" "$tx_id"
    ;;
  rollback-firewall)
    rollback_firewall "$role" "$tx_id"
    ;;
  rollback)
    [[ -z "$tx_id" ]] || usage
    rollback_files "$role"
    echo "ROLLED_BACK role=$role"
    ;;
  *) usage ;;
esac
