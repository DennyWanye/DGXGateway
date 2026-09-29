#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
host=${DGX_HOST:-operator@192.0.2.30}
revision=6821d6ad3681a4b137b066b76094fa82ebd0a380
official_parent=/home/operator/dgx-models/deepseek-v4-flash-vision-exp-official
staging_root=$official_parent/.staging-$revision
final_root=$official_parent/$revision
source_root=/home/operator/dgx-models/deepseek-v4-flash-vision-exp-ablit
control_root=/home/operator/.local/libexec/dgx-deepseek-v4-flash-vision-exp-official
remote_manager=$control_root/manage-official-snapshot.py
remote_manifest=$control_root/exact.json
remote_worker=$control_root/download-official-artifact-remote.sh
manifest=$root/manifests/deepseek-v4-flash-vision-exp-official-exact.json
queue=$(mktemp "${TMPDIR:-/tmp}/dsv4vo-queue.XXXXXX.json")
trap 'rm -f "$queue"' EXIT

jq -e --arg revision "$revision" '
  .schema == 1 and .repo == "deepseek-ai/DeepSeek-V4-Flash-Vision-Exp" and
  .revision == $revision and .file_count == 84 and (.reuse.paths | length == 55) and
  (.files | length == 84) and .declared_license == "mit" and .pipeline_tag == "image-text-to-text"
' "$manifest" >/dev/null
jq --arg revision "$revision" '[.files | to_entries[] | {
  id: ("dsv4vo-" + ((.key + 1) | tostring | if length == 1 then "00" + . elif length == 2 then "0" + . else . end)),
  repo: "deepseek-ai/DeepSeek-V4-Flash-Vision-Exp", revision: $revision,
  path: .value.path, destination: .value.path
}]' "$manifest" >"$queue"

ssh -o BatchMode=yes -o ConnectTimeout=8 "$host" '
  test "$(hostname)" = dgx-head && test "$(whoami)" = operator &&
  sudo -n -u dgx-hf-direct ip route get 1.1.1.1 | grep -F "dev wlP9s9" >/dev/null &&
  ip route get 1.1.1.1 | grep -F "dev dgx-vpn" >/dev/null
'
ssh -o BatchMode=yes "$host" "install -d -m 0755 '$control_root' '$staging_root'"
ssh -o BatchMode=yes "$host" "install -m 0755 /dev/stdin '$remote_manager'" \
  <"$root/scripts/manage-deepseek-v4-flash-vision-exp-official.py"
ssh -o BatchMode=yes "$host" "install -m 0644 /dev/stdin '$remote_manifest'" <"$manifest"
ssh -o BatchMode=yes "$host" "install -m 0755 /dev/stdin '$remote_worker'" \
  <"$root/scripts/download-deepseek-v4-flash-vision-exp-official-artifact-remote.sh"

ssh -o BatchMode=yes "$host" \
  "python3 '$remote_manager' seed --manifest '$remote_manifest' --source-root '$source_root' --staging-root '$staging_root'"

export VIDEO_QUEUE_MANIFEST="$queue"
export VIDEO_QUEUE_REMOTE_ROOT="$staging_root"
export VIDEO_QUEUE_REMOTE_LOG_ROOT="$control_root/logs"
export VIDEO_QUEUE_REMOTE_WORKER="$remote_worker"
export VIDEO_QUEUE_PID_FILE="$root/logs/deepseek-v4-flash-vision-exp-official-download-queue.pid"
export VIDEO_QUEUE_WAIT_FOR_GLOBAL_DOWNLOADS=1
"$root/scripts/run-deepseek-v4-flash-download-queue.sh"

ssh -o BatchMode=yes "$host" \
  "python3 '$remote_manager' promote --manifest '$remote_manifest' --staging-root '$staging_root' --final-root '$final_root'"
