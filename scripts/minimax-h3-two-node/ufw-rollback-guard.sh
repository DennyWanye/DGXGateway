#!/bin/bash
set -euo pipefail

umask 077

[[ $# -eq 1 ]] || { echo "FAIL: transaction directory required" >&2; exit 64; }
TX_DIR=$1
[[ "$TX_DIR" == /* && -d "$TX_DIR/preimage" && -f "$TX_DIR/transaction.env" ]] || {
  echo "FAIL: invalid firewall transaction directory" >&2
  exit 64
}

# transaction.env is created root-owned/mode-0600 in a root-only directory
# before the guard is armed. It contains paths and the previous active state,
# never credentials or shell fragments from untrusted input.
# shellcheck disable=SC1090,SC1091
source "$TX_DIR/transaction.env"

[[ -f "$TX_DIR/preimage.sha256" && ! -L "$TX_DIR/preimage.sha256" ]] || {
  echo "FAIL: firewall preimage receipt is missing or unsafe" >&2
  exit 1
}
sha256sum -c "$TX_DIR/preimage.sha256" >/dev/null || {
  echo "FAIL: firewall preimage receipt validation failed" >&2
  exit 1
}

exec 9>"$TX_DIR/rollback.lock"
flock 9

state=$(cat "$TX_DIR/state" 2>/dev/null || true)
case "$state" in
  ROLLED_BACK) exit 0 ;;
  PREIMAGE_CAPTURED|GUARD_ARMED|APPLYING|APPLY_PENDING|FINALIZED|ROLLBACK_FAILED) ;;
  *) echo "FAIL: ambiguous transaction state: $state" >&2; exit 1 ;;
esac

printf '%s\n' ROLLING_BACK >"$TX_DIR/state"

restore_file() {
  local source=$1 destination=$2 temporary
  [[ -f "$source" && ! -L "$source" ]] || { echo "FAIL: unsafe preimage $source" >&2; exit 1; }
  [[ -f "$destination" && ! -L "$destination" ]] || { echo "FAIL: unsafe target $destination" >&2; exit 1; }
  temporary="${destination}.minimax-h3-rollback.$$"
  cp -a -- "$source" "$temporary"
  mv -f -- "$temporary" "$destination"
}

restore_file "$TX_DIR/preimage/user.rules" "$USER_RULES"
restore_file "$TX_DIR/preimage/user6.rules" "$USER6_RULES"
restore_file "$TX_DIR/preimage/ufw.conf" "$UFW_CONF"
restore_file "$TX_DIR/preimage/default.ufw" "$DEFAULT_UFW"

case "$PREVIOUS_ACTIVE" in
  active)
    "$UFW_COMMAND" --force enable >/dev/null
    "$UFW_COMMAND" reload >/dev/null
    ;;
  inactive)
    "$UFW_COMMAND" --force disable >/dev/null
    ;;
  *)
    printf '%s\n' ROLLBACK_FAILED >"$TX_DIR/state"
    echo "FAIL: invalid previous UFW state" >&2
    exit 1
    ;;
esac

# enable/disable can rewrite ufw.conf; restore the exact preimage one final time.
restore_file "$TX_DIR/preimage/ufw.conf" "$UFW_CONF"

cmp -s "$TX_DIR/preimage/user.rules" "$USER_RULES" || { printf '%s\n' ROLLBACK_FAILED >"$TX_DIR/state"; exit 1; }
cmp -s "$TX_DIR/preimage/user6.rules" "$USER6_RULES" || { printf '%s\n' ROLLBACK_FAILED >"$TX_DIR/state"; exit 1; }
cmp -s "$TX_DIR/preimage/ufw.conf" "$UFW_CONF" || { printf '%s\n' ROLLBACK_FAILED >"$TX_DIR/state"; exit 1; }
cmp -s "$TX_DIR/preimage/default.ufw" "$DEFAULT_UFW" || { printf '%s\n' ROLLBACK_FAILED >"$TX_DIR/state"; exit 1; }

"$UFW_COMMAND" status numbered >"$TX_DIR/rollback-ufw-status-numbered.txt"
if [[ -f "$TX_DIR/preimage/ufw-status-numbered.txt" ]]; then
  cmp -s "$TX_DIR/preimage/ufw-status-numbered.txt" "$TX_DIR/rollback-ufw-status-numbered.txt" || {
    printf '%s\n' ROLLBACK_FAILED >"$TX_DIR/state"
    echo "FAIL: UFW semantic state does not match preimage" >&2
    exit 1
  }
fi

if [[ -n "${IPTABLES_SAVE_COMMAND-}" && -f "$TX_DIR/preimage/iptables-save.txt" ]]; then
  "$IPTABLES_SAVE_COMMAND" >"$TX_DIR/rollback-iptables-save.txt"
  cmp -s "$TX_DIR/preimage/iptables-save.txt" "$TX_DIR/rollback-iptables-save.txt" || {
    printf '%s\n' ROLLBACK_FAILED >"$TX_DIR/state"
    echo "FAIL: IPv4 firewall backend does not match preimage" >&2
    exit 1
  }
fi
if [[ -n "${IP6TABLES_SAVE_COMMAND-}" && -f "$TX_DIR/preimage/ip6tables-save.txt" ]]; then
  "$IP6TABLES_SAVE_COMMAND" >"$TX_DIR/rollback-ip6tables-save.txt"
  cmp -s "$TX_DIR/preimage/ip6tables-save.txt" "$TX_DIR/rollback-ip6tables-save.txt" || {
    printf '%s\n' ROLLBACK_FAILED >"$TX_DIR/state"
    echo "FAIL: IPv6 firewall backend does not match preimage" >&2
    exit 1
  }
fi

printf '%s\n' ROLLED_BACK >"$TX_DIR/state"
