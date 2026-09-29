#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
controller=$root/scripts/run-glm53-flash-download-queue.sh
worker=$root/scripts/download-glm53-flash-artifact-remote.sh
manifest=$root/manifests/glm53-flash-exl3-download.json

bash -n "$controller"
bash -n "$worker"

grep -F 'Mia-AiLab/GLM-5.3-Flash-EXL3-TR3-4bpw' "$controller"
grep -F '25a44fdbf16862a46b7cc9921142c6c81350af2f' "$controller"
grep -F 'VIDEO_QUEUE_WAIT_FOR_GLOBAL_DOWNLOADS=1' "$controller"
grep -F '/home/operator/dgx-models/glm53-flash-exl3' "$worker"
grep -F 'staging_dir=/var/lib/dgx-glm53-download' "$worker"
grep -F 'runuser -u dgx-hf-direct' "$worker"
grep -F 'unshare --mount --propagation private' "$worker"
grep -F -- '--lowest-speed-limit=64K' "$worker"
grep -F -- '--max-tries=5' "$worker"
grep -F -- '--split=8' "$worker"
grep -F -- '--max-connection-per-server=8' "$worker"
grep -F 'ionice -c2 -n7' "$worker"
grep -F 'nice -n 10' "$worker"

jq -e '
  type == "array" and length == 144 and
  all(.[]; .repo == "Mia-AiLab/GLM-5.3-Flash-EXL3-TR3-4bpw") and
  all(.[]; .revision == "25a44fdbf16862a46b7cc9921142c6c81350af2f") and
  ([.[] | select(.path | test("^model-[0-9]{5}-of-00120\\.safetensors$"))] | length == 120) and
  ([.[].destination] | unique | length == 144)
' "$manifest" >/dev/null

shim=$(mktemp -d)
trap 'rm -rf "$shim"' EXIT
printf '#!/usr/bin/env bash\nprintf "dgx-head\\n"\n' >"$shim/hostname"
chmod +x "$shim/hostname"

set +e
invalid_output=$(PATH="$shim:$PATH" "$worker" glm53-test /run/dgx-video-glm53-test.url \
  /home/operator/dgx-models/deepseek-v4-flash/forbidden.bin 1 gitsha1:0000000000000000000000000000000000000000 2>&1)
invalid_status=$?
set -e
[[ $invalid_status -ne 0 ]]
grep -F 'invalid final path' <<<"$invalid_output"

set +e
allowed_output=$(PATH="$shim:$PATH" "$worker" glm53-test /run/dgx-video-glm53-test.url \
  /home/operator/dgx-models/glm53-flash-exl3/allowed.bin 1 gitsha1:0000000000000000000000000000000000000000 2>&1)
allowed_status=$?
set -e
[[ $allowed_status -ne 0 ]]
grep -F 'missing signed URL' <<<"$allowed_output"

echo "GLM-5.3 download config: PASS"
