#!/usr/bin/env bash
# Public payload only. Signed URL comes via a private file, never argv or logs.
set -euo pipefail
artifact_id=${1:?artifact id}
url_file=${2:?private URL file}
final_path=${3:?final destination}
expected_size=${4:?size}
expected_digest=${5:?digest}
download_limit=${6:-32M}
base=/home/operator/dgx-models/mia-deepseek-v41-download
status_file="$base/download-logs/$artifact_id.status"

[[ $(hostname) == dgx-head && $(id -u) == 0 && $(id -u dgx-hf-direct) == 996 ]]
[[ $artifact_id =~ ^miav41-[0-9]{2}$ ]]
[[ $url_file == /run/dgx-miav41-"$artifact_id".url ]]
[[ $expected_size =~ ^[1-9][0-9]*$ && $expected_digest =~ ^[0-9a-f]{64}$ ]]
[[ $download_limit =~ ^[1-9][0-9]*[KM]?$ ]]
[[ $final_path == "$base/weights/DeepSeek-V4.1-Flash-EXL3-2.9bpw"/* || $final_path == "$base/weights/DeepSeek-V4.1-Flash"/* ]]
[[ $final_path != *'/../'* && $final_path != *'/./'* ]]
hosts_file="$url_file.hosts"
transfer_log="$url_file.transfer.log"
verifier="$base/control/verify-downloads.py"

[[ ! -L $base/download-logs && ! -L $base/download-logs/worker.lock && ! -L $status_file ]]
install -d -o operator -g operator -m 0755 "$base/download-logs"
# One worker across this model's queue; a lost Mac connection cannot double-write.
exec 9>"$base/download-logs/worker.lock"
flock -n 9 || { echo 'BUSY another MiaV41 worker owns the lock'; exit 75; }
finish() {
  rc=$?
  rm -f "$url_file" "$hosts_file" "$transfer_log"
  if [[ ! -f $status_file ]] || [[ $(cat "$status_file") == RUNNING ]]; then
    if ((rc == 0)); then printf 'PASS\n' >"$status_file"; else printf 'FAIL:%s\n' "$rc" >"$status_file"; fi
  fi
}
trap finish EXIT
printf 'RUNNING\n' >"$status_file"
[[ -s $url_file && -s $hosts_file ]]
runuser -u dgx-hf-direct -- ip route get 1.1.1.1 | grep -F 'dev wlP9s9' >/dev/null
runuser -u operator -- ip route get 1.1.1.1 | grep -F 'dev dgx-vpn' >/dev/null

# Reject symlink ancestors and keep staging/final on the same filesystem.
python3 - "$base/weights" "$final_path" <<'PY'
from pathlib import Path
import sys
root, target = map(Path, sys.argv[1:])
assert root in target.parents
assert not any(p.is_symlink() for p in [target, *target.parents])
PY
install -d -o operator -g operator -m 0755 "$(dirname "$final_path")"
staging_dir="$base/weights/.download"
[[ ! -L $staging_dir ]]
install -d -o dgx-hf-direct -g dgx-hf-direct -m 0700 "$staging_dir"
staging_path="$staging_dir/$artifact_id.part"
[[ $(stat -c %d "$staging_dir") == $(stat -c %d "$(dirname "$final_path")") ]]
verify() { python3 "$verifier" --file "$1" --size "$expected_size" --digest "$expected_digest"; }
if [[ -e $final_path ]]; then
  verify "$final_path"
  echo "VERIFIED_EXISTING artifact=$artifact_id"
  exit 0
fi
[[ ! -L $staging_path && ! -L $staging_path.aria2 ]]
# aria2 may preallocate a partial file; an existing .aria2 map means incomplete.
if [[ -f $staging_path && ! -e $staging_path.aria2 && $(stat -c %s "$staging_path") == "$expected_size" ]]; then
  verify "$staging_path" || { echo "PAUSE corrupt completed staging artifact=$artifact_id"; exit 4; }
else
  [[ ! -f $staging_path || $(stat -c %s "$staging_path") -le $expected_size ]]
  # Validate URL and public IPv4 mapping without displaying their secret query.
  python3 - "$url_file" "$hosts_file" <<'PY'
import ipaddress, sys, urllib.parse
from pathlib import Path
raw = Path(sys.argv[1]).read_text()
assert not any(c.isspace() for c in raw)
u = urllib.parse.urlsplit(raw)
assert u.scheme == 'https' and not u.username and not u.password and u.port in (None, 443)
rows = [s.split() for s in Path(sys.argv[2]).read_text().splitlines()]
ips = [r[0] for r in rows if len(r) == 2 and r[1] == u.hostname]
assert len(ips) == 1
ip = ipaddress.ip_address(ips[0])
assert ip.version == 4 and ip.is_global and ip not in ipaddress.ip_network('198.18.0.0/15')
PY
  resolved_ip=$(awk 'NR==3 {print $1}' "$hosts_file")
  runuser -u dgx-hf-direct -- ip route get "$resolved_ip" | grep -F 'dev wlP9s9' >/dev/null
  umask 077
  set +e
  { cat "$url_file"; printf '\n out=%s.part\n' "$artifact_id"; } | unshare --mount --propagation private /bin/bash -c '
    set -euo pipefail
    hosts_file=$1; shift
    mount --bind "$hosts_file" /etc/hosts
    exec runuser -u dgx-hf-direct -- env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy "$@"
  ' bash "$hosts_file" aria2c \
    --no-conf=true --continue=true --disable-ipv6=true --all-proxy='' --interface=wlP9s9 \
    --max-overall-download-limit="$download_limit" --split=4 --max-connection-per-server=4 \
    --min-split-size=16M --file-allocation=none --check-integrity=true \
    --checksum="sha-256=$expected_digest" --auto-file-renaming=false --allow-overwrite=false \
    --async-dns=false --connect-timeout=20 --timeout=60 --lowest-speed-limit=16K \
    --max-tries=1 --retry-wait=10 --summary-interval=60 \
    --dir="$staging_dir" --out="$artifact_id.part" --input-file=- >"$transfer_log" 2>&1
  transfer_exit=$?
  set -e
  if ((transfer_exit != 0)); then
    if grep -Eiq 'status[=: ]+(401|403)|HTTP/[^ ]+ (401|403)|Authorization failed' "$transfer_log"; then
      printf 'BLOCKED:HTTP_401_403\n' >"$status_file"
      echo "PAUSE HTTP_401_403 artifact=$artifact_id"
      exit 3
    fi
    codes=$(grep -Eo 'errorCode=[0-9]+|status=[0-9]+' "$transfer_log" | sort -u | tr '\n' ',' || true)
    echo "TRANSFER_FAILED artifact=$artifact_id exit=$transfer_exit codes=$codes"
    exit "$transfer_exit"
  fi
  verify "$staging_path"
fi
# Both hash and size have passed. Rename is atomic on the checked filesystem.
chown operator:operator "$staging_path"
chmod 0644 "$staging_path"
# link() atomically publishes the completed inode and refuses to overwrite a
# destination that appeared after the initial check. Same filesystem checked above.
python3 - "$staging_path" "$final_path" <<'PY'
import os, sys
os.link(sys.argv[1], sys.argv[2])
os.unlink(sys.argv[1])
PY
echo "DOWNLOAD_PASS artifact=$artifact_id bytes=$expected_size limit=$download_limit"
