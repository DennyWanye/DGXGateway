#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
host=${DGX_HOST:-operator@192.0.2.30}
remote_root=/home/operator/dgx-models/glm53-flash-exl3
remote_worker=$remote_root/scripts/download-glm53-flash-artifact-remote.sh

manifest=$root/manifests/glm53-flash-exl3-download.json
repo=Mia-AiLab/GLM-5.3-Flash-EXL3-TR3-4bpw
revision=25a44fdbf16862a46b7cc9921142c6c81350af2f

jq -e --arg repo "$repo" --arg revision "$revision" '
  type == "array" and length == 144 and
  all(.[]; .repo == $repo) and all(.[]; .revision == $revision)
' "$manifest" >/dev/null

ssh -o BatchMode=yes -o ConnectTimeout=8 "$host" \
  "test \"\$(hostname)\" = dgx-head && install -d -m 0755 '$remote_root/scripts'"
ssh -o BatchMode=yes "$host" \
  "install -m 0755 /dev/stdin '$remote_worker'" \
  <"$root/scripts/download-glm53-flash-artifact-remote.sh"

export VIDEO_QUEUE_MANIFEST="$manifest"
export VIDEO_QUEUE_REMOTE_ROOT="$remote_root"
export VIDEO_QUEUE_REMOTE_LOG_ROOT="$remote_root/logs"
export VIDEO_QUEUE_REMOTE_WORKER="$remote_worker"
export VIDEO_QUEUE_PID_FILE="$root/logs/glm53-flash-download-queue.pid"
export VIDEO_QUEUE_WAIT_FOR_GLOBAL_DOWNLOADS=1

exec "$root/scripts/run-deepseek-v4-flash-download-queue.sh"
