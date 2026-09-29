#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
egress="$root/scripts/direct-download-egress.sh"
download="$root/scripts/download-model-matrix.sh"

grep -F 'DGX_DIRECT_USER=dgx-hf-direct' "$egress"
grep -F 'ip -4 rule add pref 8000 uidrange' "$egress"
grep -F 'oifname "wlP9s9" tcp dport 443' "$egress"
grep -F 'dgx-hf-direct-download' "$egress"
grep -F 'table 2022' "$egress"
grep -F -- '--continue=true' "$download"
grep -F -- '--split=16' "$download"
grep -F -- '--check-integrity=true' "$download"
grep -F 'dgx-vpn/statistics/rx_bytes' "$download"
grep -F 'sha256sum' "$download"
grep -F '500' "$download"
! grep -E 'systemctl .*stop .*dgx-mihomo|nft delete table inet dgx_vpn_killswitch' "$download"

echo "Direct model download configuration contract: PASS"
