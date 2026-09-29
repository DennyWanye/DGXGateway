#!/usr/bin/env bash
set -euo pipefail

host=${DGX_HOST:-operator@192.0.2.30}
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
manifest=$root/manifests/qwen-image-edit-2511-lightning-download.json
remote_root=/home/operator/dgx-models/qwen-image-edit-2511-lightning/d74eba145674fd7e31b949324e148e21e7118abd
remote_log_root=/home/operator/dgx-models/qwen-image-edit-2511-lightning/download-logs
remote_worker=/home/operator/dgx-models/qwen-image-edit-2511-lightning/control/download-artifact.sh
remote_worker=${VIDEO_QUEUE_REMOTE_WORKER:-$remote_worker}
pid_file=$root/logs/qwen-lightning-download.pid

mkdir -p "$root/logs"
if [[ -s $pid_file ]]; then
  old_pid=$(cat "$pid_file")
  if kill -0 "$old_pid" 2>/dev/null; then
    echo "queue already running pid=$old_pid" >&2
    exit 2
  fi
fi
echo $$ >"$pid_file"
trap 'rm -f "$pid_file"' EXIT

[[ $(uname -s) == Darwin ]] || { echo "queue controller must run on the Mac" >&2; exit 1; }
jq -e 'type == "array" and length > 0' "$manifest" >/dev/null
ssh -o BatchMode=yes -o ConnectTimeout=8 "$host" 'test "$(hostname)" = dgx-head'

# Public model; no token is read or transmitted. Mac control requests bind LAN en1.
hf_curl() { command curl --interface en1 --noproxy '*' --connect-timeout 15 --max-time 120 "$@"; }
wait_for_own_download() {
  while ssh -o BatchMode=yes "$host" "pgrep -f '^bash $remote_worker ' >/dev/null"; do
    echo "WAIT preceding_worker_in_same_queue time=$(date -Iseconds)"
    sleep 10
  done
}

metadata_for() {
  jq -er --arg p "$3" '.siblings[] | select(.rfilename == $p) | [(.lfs.size // .size), (.lfs.sha256 // ""), .blobId] | @tsv' "$root/plans/短剧工作流/image-pipeline-optimization-20260912/evidence/lightning-download-metadata.json"
}

resolve_signed_url() {
  local repo=$1 revision=$2 repo_path=$3 encoded_path origin result http_code effective_url
  encoded_path=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe="/"))' "$repo_path")
  origin="https://huggingface.co/$repo/resolve/$revision/$encoded_path?download=true"
  while true; do
    if result=$(hf_curl --silent --show-error --head --location --output /dev/null \
      --write-out $'%{http_code}\t%{url_effective}' "$origin"); then
      http_code=${result%%$'\t'*}
      effective_url=${result#*$'\t'}
      if [[ $http_code == 401 || $http_code == 403 ]]; then
        echo "GATED file repo=$repo path=$repo_path http=$http_code" >&2
        return 3
      fi
      if [[ ( $http_code == 200 || $http_code == 206 ) && $effective_url == https://* ]]; then
        printf '%s' "$effective_url"
        return 0
      fi
      echo "signed URL HTTP $http_code repo=$repo path=$repo_path" >&2
    fi
    echo "RETRY signed_url repo=$repo path=$repo_path time=$(date -Iseconds)" >&2
    sleep 10
  done
}

resolve_public_ipv4() {
  local host=$1 response ip
  if [[ $host == us.aws.cdn.hf.co ]]; then printf '%s\n' 3.0.71.232; return 0; fi
  [[ $host =~ ^[A-Za-z0-9.-]+$ ]] || { echo "invalid CDN host: $host" >&2; return 2; }
  while true; do
    if response=$(ssh -o BatchMode=yes "${DGX_HOST:-operator@192.0.2.30}" \
      "sudo -n runuser -u dgx-hf-direct -- curl --noproxy '*' -4 --fail --silent --show-error --max-time 30 --resolve doh.pub:443:1.12.12.12 'https://doh.pub/dns-query?name=$host&type=A' -H 'accept: application/dns-json'"); then
      while IFS= read -r ip; do
        if python3 - "$ip" <<'PY'
import ipaddress
import sys

address = ipaddress.ip_address(sys.argv[1])
synthetic = ipaddress.ip_network("198.18.0.0/15")
raise SystemExit(0 if address.version == 4 and address.is_global and address not in synthetic else 1)
PY
        then
          echo "PROBE public_dns host=$host ip=$ip" >&2
          if ssh -n -o BatchMode=yes "${DGX_HOST:-operator@192.0.2.30}" \
            "sudo -n runuser -u dgx-hf-direct -- curl --noproxy '*' -4 --silent --show-error --head --output /dev/null --max-time 8 --connect-timeout 4 --resolve '$host:443:$ip' 'https://$host/'"; then
            printf '%s\n' "$ip"
            return 0
          fi
        fi
      done < <(jq -r '.Answer[]? | select(.type == 1) | .data' <<<"$response")
    fi
    echo "RETRY public_dns host=$host time=$(date -Iseconds)" >&2
    sleep 10
  done
}

download_small_git_blob() {
  local artifact_id=$1 repo=$2 revision=$3 repo_path=$4 final_path=$5 expected_size=$6 expected_blob=$7
  local encoded_path origin temp_dir temp_file actual_size actual_blob
  if (( expected_size > 67108864 )); then
    echo "ordinary Git blob exceeds 64 MiB safety limit: $artifact_id" >&2
    return 2
  fi

  temp_dir=$(mktemp -d)
  temp_file=$temp_dir/blob
  encoded_path=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe="/"))' "$repo_path")
  origin="https://huggingface.co/$repo/resolve/$revision/$encoded_path?download=true"
  local http_code attempt
  for attempt in 1 2 3; do
    http_code=$(hf_curl --silent --show-error --location --output "$temp_file" --write-out '%{http_code}' "$origin") || http_code=000
    if [[ $http_code == 401 || $http_code == 403 ]]; then
      rm -rf "$temp_dir"
      echo "PAUSE HTTP_401_403 artifact=$artifact_id" >&2
      return 3
    fi
    [[ $http_code == 200 ]] && break
    if (( attempt == 3 )); then
      rm -rf "$temp_dir"
      echo "PAUSE small_git_blob artifact=$artifact_id http=$http_code" >&2
      return 1
    fi
    sleep 15
  done

  actual_size=$(stat -f %z "$temp_file")
  [[ $actual_size == "$expected_size" ]] || {
    echo "Git blob size mismatch: $artifact_id expected=$expected_size actual=$actual_size" >&2
    rm -rf "$temp_dir"
    return 1
  }
  actual_blob=$({ printf 'blob %s\0' "$actual_size"; cat "$temp_file"; } | shasum | awk '{print $1}')
  [[ $actual_blob == "$expected_blob" ]] || {
    echo "git blob digest mismatch: $artifact_id" >&2
    rm -rf "$temp_dir"
    return 1
  }

  ssh -o BatchMode=yes "$host" "sudo -n install -d -o operator -g operator -m 0755 '$(dirname "$final_path")'"
  ssh -o BatchMode=yes "$host" "sudo -n install -o operator -g operator -m 0644 /dev/stdin '$final_path'" <"$temp_file"
  rm -rf "$temp_dir"
}

verify_remote_existing() {
  local final_path=$1 expected_size=$2 expected_digest=$3
  local encoded_path encoded_digest
  encoded_path=$(printf '%s' "$final_path" | base64 | tr -d '\n')
  encoded_digest=$(printf '%s' "$expected_digest" | base64 | tr -d '\n')
  ssh -o BatchMode=yes "$host" bash -s -- "$encoded_path" "$expected_size" "$encoded_digest" <<'REMOTE'
set -euo pipefail
path=$(printf '%s' "$1" | base64 -d)
expected_size=$2
expected_digest=$(printf '%s' "$3" | base64 -d)
[[ -f $path ]]
[[ $(stat -c %s "$path") == "$expected_size" ]]
if [[ $expected_digest == gitsha1:* ]]; then
  expected_hash=${expected_digest#gitsha1:}
  actual_hash=$({ printf 'blob %s\0' "$expected_size"; cat "$path"; } | sha1sum | awk '{print $1}')
else
  expected_hash=$expected_digest
  actual_hash=$(sha256sum "$path" | awk '{print $1}')
fi
[[ $actual_hash == "$expected_hash" ]]
REMOTE
}

# Independent payload queue; separate targets, each capped at 10 MiB/s.
payload_attempts=0
count=$(jq 'length' "$manifest")
for ((index=0; index<count; index++)); do
  id=$(jq -r ".[$index].id" "$manifest")
  repo=$(jq -r ".[$index].repo" "$manifest")
  revision=$(jq -r ".[$index].revision" "$manifest")
  repo_path=$(jq -r ".[$index].path" "$manifest")
  destination=$(jq -r ".[$index].destination" "$manifest")
  final_path="$remote_root/$destination"
  log_file="$remote_log_root/$id.download.log"
  url_file="/run/dgx-video-$id.url"
  hosts_file="$url_file.hosts"

  ssh -o BatchMode=yes "$host" "sudo -n -u dgx-hf-direct ip route get 1.1.1.1 | grep -F 'dev wlP9s9' >/dev/null" || { echo 'PAUSE direct route lost' >&2; exit 1; }
  wait_for_own_download
  if metadata=$(metadata_for "$repo" "$revision" "$repo_path"); then
    :
  else
    metadata_status=$?
    if [[ $metadata_status == 3 ]]; then
      echo "PAUSE gated artifact=$id repo=$repo" >&2
      exit 3
    fi
    exit "$metadata_status"
  fi
  expected_size=${metadata%%$'\t'*}
  digest_fields=${metadata#*$'\t'}
  expected_sha=${digest_fields%%$'\t'*}
  expected_blob=${digest_fields#*$'\t'}
  [[ $expected_size =~ ^[1-9][0-9]*$ ]]
  if [[ $expected_sha =~ ^[0-9a-f]{64}$ ]]; then
    expected_digest=$expected_sha
  elif [[ $expected_blob =~ ^[0-9a-f]{40}$ ]]; then
    expected_digest="gitsha1:$expected_blob"
  else
    echo "artifact has no usable digest: $id" >&2
    exit 1
  fi

  if ssh -o BatchMode=yes "$host" "test -f '$final_path'"; then
    if verify_remote_existing "$final_path" "$expected_size" "$expected_digest"; then
      echo "SKIP verified_existing artifact=$id"
      continue
    fi
    echo "existing artifact failed size/digest verification: $id path=$final_path" >&2
    exit 1
  fi

  if [[ $expected_digest == gitsha1:* ]]; then
    echo "START small_git_blob artifact=$id bytes=$expected_size time=$(date -Iseconds)"
    download_small_git_blob "$id" "$repo" "$revision" "$repo_path" "$final_path" "$expected_size" "$expected_blob"
    echo "PASS artifact=$id time=$(date -Iseconds)"
    continue
  fi

  echo "START artifact=$id bytes=$expected_size time=$(date -Iseconds)"
  ssh -o BatchMode=yes "$host" "sudo -n install -d -o operator -g operator -m 0755 '$remote_log_root'"
  if signed_url=$(resolve_signed_url "$repo" "$revision" "$repo_path"); then
    :
  else
    signed_url_status=$?
    if [[ $signed_url_status == 3 ]]; then
      echo "PAUSE gated artifact=$id repo=$repo" >&2
      exit 3
    fi
    exit "$signed_url_status"
  fi
  signed_host=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.urlsplit(sys.argv[1]).hostname)' "$signed_url")
  resolved_ip=$(resolve_public_ipv4 "$signed_host")

  printf '%s' "$signed_url" | ssh -o BatchMode=yes "$host" \
    "sudo -n install -o dgx-hf-direct -g dgx-hf-direct -m 0600 /dev/stdin '$url_file'"
  printf '127.0.0.1 localhost\n::1 localhost\n%s %s\n' "$resolved_ip" "$signed_host" \
    | ssh -o BatchMode=yes "$host" \
      "sudo -n install -o root -g root -m 0644 /dev/stdin '$hosts_file'"
  ssh -o BatchMode=yes "$host" \
    "sudo -n nohup nice -n 15 ionice -c 3 '$remote_worker' '$id' '$url_file' '$final_path' '$expected_size' '$expected_digest' >'$log_file' 2>&1 </dev/null &"

  sleep 3
  while ! ssh -o BatchMode=yes "$host" "test -f '$final_path'"; do
    if ! ssh -o BatchMode=yes "$host" "pgrep -f '^bash $remote_worker $id ' >/dev/null"; then
      if ssh -o BatchMode=yes "$host" "test -f '$log_file.blocked'"; then
        echo "PAUSE HTTP_401_403 artifact=$id" >&2; exit 3
      fi
      payload_attempts=$((payload_attempts+1))
      if (( payload_attempts >= 3 )); then echo "PAUSE exhausted artifact=$id" >&2; exit 1; fi
      echo "RETRY artifact=$id reason=no_final_file time=$(date -Iseconds)" >&2
      break
    fi
    sleep 10
  done
  if ssh -o BatchMode=yes "$host" "test -f '$final_path'"; then
    payload_attempts=0
    echo "PASS artifact=$id time=$(date -Iseconds)"
  else
    index=$((index-1))
    sleep 10
  fi
done

echo "QUEUE_COMPLETE time=$(date -Iseconds)"
