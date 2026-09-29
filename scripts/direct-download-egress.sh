#!/usr/bin/env bash
set -euo pipefail

DGX_DIRECT_USER=dgx-hf-direct
rule_pref=8000
rule_comment=dgx-hf-direct-download
physical_interface=wlP9s9

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  exec sudo -n "$0" "$@"
fi

[[ $(hostname) == dgx-head ]] || { echo "wrong host" >&2; exit 1; }
ip -4 -o addr show dev "$physical_interface" | grep -Fq ' 192.0.2.30/24 '

remove_rules() {
  if id "$DGX_DIRECT_USER" >/dev/null 2>&1; then
    uid=$(id -u "$DGX_DIRECT_USER")
    while ip -4 rule del pref "$rule_pref" uidrange "$uid-$uid" lookup main 2>/dev/null; do :; done
  fi
  while true; do
    handle=$(nft -a list chain inet dgx_vpn_killswitch output 2>/dev/null |
      awk -v comment="$rule_comment" '$0 ~ comment {sub(/^.* handle /, "", $0); print $1; exit}')
    [[ -n ${handle:-} ]] || break
    nft delete rule inet dgx_vpn_killswitch output handle "$handle"
  done
}

install_rules() {
  if ! id "$DGX_DIRECT_USER" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$DGX_DIRECT_USER"
  fi
  uid=$(id -u "$DGX_DIRECT_USER")
  remove_rules
  ip -4 rule add pref 8000 uidrange "$uid-$uid" lookup main
  reject_handle=$(nft -a list chain inet dgx_vpn_killswitch output |
    awk '/reject with icmp admin-prohibited/ {sub(/^.* handle /, "", $0); print $1; exit}')
  [[ -n $reject_handle ]] || { echo "killswitch reject rule missing" >&2; exit 1; }
  nft insert rule inet dgx_vpn_killswitch output position "$reject_handle" \
    meta skuid "$uid" oifname "wlP9s9" tcp dport 443 counter accept comment "$rule_comment"
  ip -4 rule show pref "$rule_pref" | grep -F "uidrange $uid-$uid lookup main"
  nft list chain inet dgx_vpn_killswitch output | grep -F "$rule_comment"
  ip route get 1.1.1.1 uid 1000 | grep -F 'table 2022'
}

status_rules() {
  id "$DGX_DIRECT_USER"
  uid=$(id -u "$DGX_DIRECT_USER")
  ip -4 rule show pref "$rule_pref"
  nft list chain inet dgx_vpn_killswitch output | grep -F "$rule_comment"
  ip route get 1.1.1.1 uid "$uid"
  ip route get 1.1.1.1 uid 1000 | grep -F 'table 2022'
}

case "${1:-}" in
  install) install_rules ;;
  remove) remove_rules ;;
  status) status_rules ;;
  exec)
    shift
    install_rules >/dev/null
    exec runuser -u "$DGX_DIRECT_USER" -- "$@"
    ;;
  *) echo "usage: $0 install|remove|status|exec COMMAND..." >&2; exit 2 ;;
esac

