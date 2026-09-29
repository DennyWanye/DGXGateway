#!/usr/bin/env bash
set -euo pipefail

artifact_id=${1:?usage: download-deepseek-v4-flash-artifact-remote.sh ARTIFACT_ID URL_FILE FINAL_PATH EXPECTED_SIZE EXPECTED_DIGEST}
url_file=${2:?missing signed URL control file}
final_path=${3:?missing final path}
expected_size=${4:?missing expected size}
expected_digest=${5:?missing expected digest}
hosts_file="$url_file.hosts"

[[ $(hostname) == dgx-head ]] || { echo "wrong host" >&2; exit 1; }
[[ $artifact_id =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "invalid artifact id" >&2; exit 2; }
[[ $url_file == /run/dgx-video-*.url ]] || { echo "invalid URL control path" >&2; exit 2; }
case $final_path in
  /home/operator/dgx-models/deepseek-v4-flash-ablit-anchored/*|/home/operator/ai/ComfyUI/models/*) ;;
  *) echo "invalid final path" >&2; exit 2 ;;
esac
[[ $expected_size =~ ^[1-9][0-9]*$ ]] || { echo "invalid expected size" >&2; exit 2; }
if [[ $expected_digest =~ ^[0-9a-f]{64}$ ]]; then
  digest_type=sha256
  expected_hash=$expected_digest
elif [[ $expected_digest =~ ^gitsha1:([0-9a-f]{40})$ ]]; then
  digest_type=gitsha1
  expected_hash=${BASH_REMATCH[1]}
else
  echo "invalid digest" >&2
  exit 2
fi
[[ -s $url_file ]] || { echo "missing signed URL" >&2; exit 2; }
[[ -s $hosts_file ]] || { echo "missing isolated hosts mapping" >&2; exit 2; }

filename=$(basename "$final_path")
staging_dir=/var/lib/dgx-deepseek-download
staging_path="$staging_dir/$filename.part"

verify_digest() {
  local path=$1 actual size
  if [[ $digest_type == sha256 ]]; then
    printf '%s  %s\n' "$expected_hash" "$path" | sha256sum --check --status
    return
  fi
  size=$(stat -c %s "$path")
  actual=$({ printf 'blob %s\0' "$size"; cat "$path"; } | sha1sum | awk '{print $1}')
  [[ $actual == "$expected_hash" ]]
}

install -d -o operator -g operator -m 0755 "$(dirname "$final_path")"
install -d -o dgx-hf-direct -g dgx-hf-direct -m 0700 "$staging_dir"

if [[ -f $final_path ]]; then
  [[ $(stat -c %s "$final_path") == "$expected_size" ]]
  verify_digest "$final_path"
  echo "VERIFIED_EXISTING artifact=$artifact_id path=$final_path"
  rm -f "$url_file" "$hosts_file"
  exit 0
fi

if [[ -e $staging_path ]]; then
  chown dgx-hf-direct:dgx-hf-direct "$staging_path" "$staging_path.aria2" 2>/dev/null || true
fi

url=$(runuser -u dgx-hf-direct -- cat "$url_file")
[[ $url == https://* ]] || { echo "signed URL is not HTTPS" >&2; exit 2; }
url_host=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.urlsplit(sys.argv[1]).hostname)' "$url")
awk -v host="$url_host" 'NF == 2 && $2 == host { found=1 } END { exit found ? 0 : 1 }' "$hosts_file" \
  || { echo "hosts mapping does not match signed URL" >&2; exit 2; }

# The egress rule is shared with other downloads and is deliberately not
# installed or removed here.
checksum_args=()
if [[ $digest_type == sha256 ]]; then
  checksum_args+=("--checksum=sha-256=$expected_hash")
fi
# The inner single-quoted script must expand only after entering the mount namespace.
# shellcheck disable=SC2016
unshare --mount --propagation private /bin/bash -c '
  set -euo pipefail
  hosts_file=$1
  shift
  mount --bind "$hosts_file" /etc/hosts
  exec runuser -u dgx-hf-direct -- "$@"
' bash "$hosts_file" aria2c \
  --continue=true \
  --split=4 \
  --max-connection-per-server=4 \
  --min-split-size=16M \
  --file-allocation=none \
  --check-integrity=true \
  "${checksum_args[@]}" \
  --auto-file-renaming=false \
  --allow-overwrite=false \
  --async-dns=false \
  --connect-timeout=20 \
  --timeout=60 \
  --lowest-speed-limit=64K \
  --max-tries=5 \
  --retry-wait=10 \
  --summary-interval=60 \
  --dir="$staging_dir" \
  --out="$filename.part" \
  "$url"

[[ $(stat -c %s "$staging_path") == "$expected_size" ]]
verify_digest "$staging_path"
chown operator:operator "$staging_path"
mv "$staging_path" "$final_path"
rm -f "$url_file" "$hosts_file"

echo "DOWNLOAD_PASS artifact=$artifact_id bytes=$expected_size digest=$expected_digest path=$final_path"
