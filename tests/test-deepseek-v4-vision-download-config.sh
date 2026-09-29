#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
controller=$root/scripts/run-deepseek-v4-vision-download-queue.sh
worker=$root/scripts/download-deepseek-v4-vision-artifact-remote.sh
manifest=$root/manifests/deepseek-v4-flash-vision-exp-ablit-download.json

bash -n "$controller"
bash -n "$worker"

grep -F 'drowzeys/keys-DeepSeekV4Flash-Vision-EXP-ablit' "$controller"
grep -F '48095b3452a17f3e3ae8f77892399389c45de9e1' "$controller"
grep -F 'VIDEO_QUEUE_WAIT_FOR_GLOBAL_DOWNLOADS=1' "$controller"
grep -F '/home/operator/dgx-models/deepseek-v4-flash-vision-exp-ablit' "$worker"
grep -F 'staging_dir=/var/lib/dgx-deepseek-v4-flash-vision-exp-download' "$worker"
grep -F 'runuser -u dgx-hf-direct' "$worker"
grep -F 'unshare --mount --propagation private' "$worker"
grep -F -- '--lowest-speed-limit=64K' "$worker"
grep -F -- '--max-tries=5' "$worker"
grep -F 'ionice -c2 -n7' "$worker"
grep -F 'nice -n 10' "$worker"

jq -e '
  type == "array" and length == 85 and
  all(.[]; .repo == "drowzeys/keys-DeepSeekV4Flash-Vision-EXP-ablit") and
  all(.[]; .revision == "48095b3452a17f3e3ae8f77892399389c45de9e1") and
  ([.[] | select(.path | test("^model-[0-9]{5}-of-00048\\.safetensors$"))] | length == 48) and
  ([.[].destination] | unique | length == 85)
' "$manifest" >/dev/null

shim=$(mktemp -d)
trap 'rm -rf "$shim"' EXIT
printf '#!/usr/bin/env bash\nprintf "dgx-head\\n"\n' >"$shim/hostname"
chmod +x "$shim/hostname"

set +e
invalid_output=$(PATH="$shim:$PATH" "$worker" dsv4v-test /run/dgx-video-dsv4v-test.url \
  /home/operator/dgx-models/deepseek-v4-flash-ablit-anchored/forbidden.bin 1 gitsha1:0000000000000000000000000000000000000000 2>&1)
invalid_status=$?
set -e
[[ $invalid_status -ne 0 ]]
grep -F 'invalid final path' <<<"$invalid_output"

set +e
allowed_output=$(PATH="$shim:$PATH" "$worker" dsv4v-test /run/dgx-video-dsv4v-test.url \
  /home/operator/dgx-models/deepseek-v4-flash-vision-exp-ablit/allowed.bin 1 gitsha1:0000000000000000000000000000000000000000 2>&1)
allowed_status=$?
set -e
[[ $allowed_status -ne 0 ]]
grep -F 'missing signed URL' <<<"$allowed_output"

echo "DeepSeek V4 Flash Vision download config: PASS"
