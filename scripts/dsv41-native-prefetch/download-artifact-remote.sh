#!/usr/bin/env bash
# Public payload only. Signed URL comes via a private file, never argv or logs.
set -euo pipefail
artifact_id=${1:?artifact id}
url_file=${2:?private URL file}
final_path=${3:?final destination}
expected_size=${4:?size}
expected_digest=${5:?digest}
download_limit=${6:-32M}
base=/home/operator/dgx-models/dsv41-native-shared
status_file="$base/download-logs/$artifact_id.status"

[[ $(hostname) == dgx-head && $(id -u) == 0 && $(id -u dgx-hf-direct) == 996 ]]
[[ $artifact_id =~ ^dsv41n-[0-9]{3}$ ]]
[[ $url_file == /run/dgx-dsv41n-"$artifact_id".url ]]
[[ $expected_size =~ ^[1-9][0-9]*$ && $expected_digest =~ ^[0-9a-f]{64}$ ]]
[[ $download_limit =~ ^[1-9][0-9]*[KM]?$ ]]
[[ $final_path == "$base/checkpoint-fb2764a/"* || $final_path == "$base/assets/native-ablit-overlay/"* ]]
[[ $final_path != *'/../'* && $final_path != *'/./'* ]]
hosts_file="$url_file.hosts"
transfer_log="$url_file.transfer.log"
verifier="$base/tools/verify-file.py"

[[ ! -L $base/download-logs && ! -L $base/download-logs/worker.lock && ! -L $status_file ]]
install -d -o operator -g operator -m 0755 "$base/download-logs"
exec 9>"$base/download-logs/worker.lock"
flock -n 9 || { echo 'BUSY another native stock worker owns the lock'; exit 75; }
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

python3 - "$base" "$final_path" <<'PY'
from pathlib import Path
import sys
root = Path(sys.argv[1])
target = Path(sys.argv[2])
allowed = (root / "checkpoint-fb2764a", root / "assets" / "native-ablit-overlay")
assert any(base in target.parents or target.parent == base for base in allowed)
assert not any(p.is_symlink() for p in [target, *target.parents])
PY
install -d -o operator -g operator -m 0755 "$(dirname "$final_path")"
staging_dir="$base/.download"
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
if [[ -f $staging_path && ! -e $staging_path.aria2 && $(stat -c %s "$staging_path") == "$expected_size" ]]; then
  verify "$staging_path" || { echo "PAUSE corrupt completed staging artifact=$artifact_id"; exit 4; }
else
  [[ ! -f $staging_path || $(stat -c %s "$staging_path") -le $expected_size ]]
  python3 - "$url_file" "$hosts_file" <<'PY'
import ipaddress, sys, urllib.parse
from pathlib import Path
raw = Path(sys.argv[1]).read_text()
assert not any(c.isspace() for c in raw)
u = urllib.parse.urlsplit(raw)
assert u.scheme == "https" and not u.username and not u.password and u.port in (None, 443)
rows = [s.split() for s in Path(sys.argv[2]).read_text().splitlines()]
ips = [r[0] for r in rows if len(r) == 2 and r[1] == u.hostname]
assert len(ips) == 1
ip = ipaddress.ip_address(ips[0])
assert ip.version == 4 and ip.is_global and ip not in ipaddress.ip_network("198.18.0.0/15")
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
    --max-overall-download-limit="$download_limit" --split=2 --max-connection-per-server=2 \
    --min-split-size=16M --file-allocation=none --check-integrity=true \
    --checksum="sha-256=$expected_digest" --auto-file-renaming=false --allow-overwrite=false \
    --async-dns=false --connect-timeout=20 --timeout=180 --lowest-speed-limit=4K \
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
chown operator:operator "$staging_path"
chmod 0644 "$staging_path"
python3 - "$staging_path" "$final_path" <<'PY'
import os, sys
os.link(sys.argv[1], sys.argv[2])
os.unlink(sys.argv[1])
PY
echo "DOWNLOAD_PASS artifact=$artifact_id bytes=$expected_size limit=$download_limit"
