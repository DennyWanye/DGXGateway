#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
host=${DGX_HOST:-operator@192.0.2.30}
remote_root=/home/operator/dgx-models/deepseek-v4-flash-vision-exp-ablit
remote_worker=$remote_root/scripts/download-deepseek-v4-vision-artifact-remote.sh

manifest=$root/manifests/deepseek-v4-flash-vision-exp-ablit-download.json
repo=drowzeys/keys-DeepSeekV4Flash-Vision-EXP-ablit
revision=48095b3452a17f3e3ae8f77892399389c45de9e1

jq -e --arg repo "$repo" --arg revision "$revision" '
  type == "array" and length == 85 and
  all(.[]; .repo == $repo) and all(.[]; .revision == $revision)
' "$manifest" >/dev/null

ssh -o BatchMode=yes -o ConnectTimeout=8 "$host" '
  test "$(hostname)" = dgx-head &&
  sudo -n -u dgx-hf-direct ip route get 1.1.1.1 | grep -F "dev wlP9s9" >/dev/null &&
  ip route get 1.1.1.1 | grep -F "dev dgx-vpn" >/dev/null
'
ssh -o BatchMode=yes "$host" \
  "sudo -n install -d -o operator -g operator -m 0755 '$remote_root/scripts'"
ssh -o BatchMode=yes "$host" \
  "sudo -n install -o operator -g operator -m 0755 /dev/stdin '$remote_worker'" \
  <"$root/scripts/download-deepseek-v4-vision-artifact-remote.sh"

export VIDEO_QUEUE_MANIFEST="$manifest"
export VIDEO_QUEUE_REMOTE_ROOT="$remote_root"
export VIDEO_QUEUE_REMOTE_LOG_ROOT="$remote_root/logs"
export VIDEO_QUEUE_REMOTE_WORKER="$remote_worker"
export VIDEO_QUEUE_PID_FILE="$root/logs/deepseek-v4-vision-download-queue.pid"
export VIDEO_QUEUE_WAIT_FOR_GLOBAL_DOWNLOADS=1

exec "$root/scripts/run-deepseek-v4-flash-download-queue.sh"
