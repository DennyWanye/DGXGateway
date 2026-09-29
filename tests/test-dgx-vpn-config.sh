#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
config="$root/mihomo/config.yaml"
killswitch="$root/mihomo/dgx-vpn-killswitch.nft"
installer="$root/scripts/install-dgx-vpn.sh"
rollback="$root/scripts/rollback-dgx-vpn.sh"
status_script="$root/scripts/dgx-vpn-status.sh"
wait_script="$root/scripts/wait-dgx-vpn-ready.sh"
mihomo_unit="$root/systemd/dgx-mihomo.service"
kill_unit="$root/systemd/dgx-vpn-killswitch.service"

grep -F 'device: dgx-vpn' "$config"
grep -F 'auto-route: true' "$config"
grep -F 'auto-redirect: true' "$config"
grep -F 'strict-route: true' "$config"
grep -F 'listen: 127.0.0.1:1053' "$config"
grep -F 'server: 192.0.2.29' "$config"
grep -F 'port: 7890' "$config"
grep -F 'udp: true' "$config"
grep -F -- '- MATCH,agentos-vpn' "$config"
grep -F '192.0.2.26/16' "$config"

grep -F 'table inet dgx_vpn_killswitch' "$killswitch"
grep -F 'oifname "dgx-vpn" accept' "$killswitch"
grep -F 'ip daddr 192.0.2.26/16 accept' "$killswitch"
grep -F 'reject with icmp type admin-prohibited' "$killswitch"
grep -F 'reject with icmpv6 type admin-prohibited' "$killswitch"
grep -F 'ip daddr 192.0.2.28 meta l4proto { tcp, udp } th dport 53 accept' "$killswitch"
if grep -F 'th dport 53 reject' "$killswitch"; then
  echo "simple mode intentionally keeps Router A DNS available" >&2
  exit 1
fi
if grep -F 'flush ruleset' "$killswitch"; then
  echo "kill switch must not flush user-owned firewall state" >&2
  exit 1
fi

grep -F 'VERSION=v1.19.30' "$installer"
grep -F 'SIZE=16965828' "$installer"
grep -F 'SHA256=58896873736d28628f66de3677c8654fa0f180662523148e136cff4f6e890069' "$installer"
grep -F 'ExecStart=/usr/local/bin/mihomo -d /var/lib/mihomo -f /etc/mihomo/config.yaml' "$mihomo_unit"
grep -F 'ExecStartPost=/usr/local/libexec/wait-dgx-vpn-ready.sh' "$mihomo_unit"
grep -F '/sys/class/net/dgx-vpn' "$wait_script"
grep -F 'ExecStart=/usr/sbin/nft -f /etc/mihomo/dgx-vpn-killswitch.nft' "$kill_unit"
grep -F 'delete table inet dgx_vpn_killswitch' "$rollback"

shellcheck "$installer" "$rollback" "$status_script" "$wait_script" "$root/tests/test-dgx-vpn-config.sh"
echo "DGX VPN configuration contract: PASS"
