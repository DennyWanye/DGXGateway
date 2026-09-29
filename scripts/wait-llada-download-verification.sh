#!/usr/bin/env bash
# Completion check for this running download, not a recurring automation.
set -euo pipefail
controller_pid=${1:?controller pid required}
[[ $controller_pid =~ ^[1-9][0-9]*$ ]]
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
while kill -0 "$controller_pid" 2>/dev/null; do sleep 30; done
if ! tail -n 1 "$root/logs/llada-image-base-download.log" | grep -q '^QUEUE_COMPLETE '; then
  echo 'DOWNLOAD_PAUSED: queue did not complete; installation remains blocked.'
  exit 1
fi
evidence="$root/plans/llada-image-evaluation/evidence/base-download-verification.json"
ssh -o BatchMode=yes -o ConnectTimeout=8 operator@192.0.2.30 \
  'nice -n 15 ionice -c 3 python3 /home/operator/dgx-models/llada-image/control/verify-files.py' \
  >"$evidence.part"
mv "$evidence.part" "$evidence"
echo 'DOWNLOAD_VERIFIED: files ready; installation still requires user confirmation.'
