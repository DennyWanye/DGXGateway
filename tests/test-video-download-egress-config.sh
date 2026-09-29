#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
controller=$root/scripts/run-video-model-download-queue.sh
worker=$root/scripts/download-video-artifact-remote.sh

# The controller must resolve a real public IPv4 address outside the DGX VPN's
# synthetic 198.18.0.0/15 DNS range and pass it as an ephemeral hosts file.
grep -F 'resolve_public_ipv4()' "$controller"
grep -F 'doh.pub:443:1.12.12.12' "$controller"
grep -F '198.18.0.0/15' "$controller"
grep -F 'PROBE public_dns' "$controller"
grep -F -- '--connect-timeout 4 --resolve' "$controller"
grep -F '"$url_file.hosts"' "$controller"
grep -F 'SKIP gated artifact=' "$controller"
grep -F 'signed_url_status == 3' "$controller"
grep -F 'install -o root -g root -m 0644 /dev/stdin' "$controller"
grep -F 'gitsha1:' "$controller"
grep -F 'remote_hf_token_file=' "$controller"
grep -F 'Authorization: Bearer %s' "$controller"
grep -F 'hf_curl()' "$controller"
grep -F 'remote_hf_control_curl()' "$controller"
grep -F 'hf-mirror.com' "$controller"
grep -F '%{redirect_url}' "$controller"
grep -F 'http_code == 302' "$controller"
if grep -Fq 'socks5h://127.0.0.1:10888' "$controller"; then
  echo "controller still routes HF control traffic through the VPN proxy" >&2
  exit 1
fi
grep -F 'download_small_git_blob()' "$controller"
grep -F '67108864' "$controller"
grep -F 'git blob digest mismatch' "$controller"
grep -F 'RETRY small_git_blob' "$controller"
grep -F 'if [[ $expected_digest == gitsha1:* ]]' "$controller"
awk '
  /^download_small_git_blob\(\)/ { in_helper=1 }
  in_helper && /sudo -n install -d -o operator/ { found=1 }
  in_helper && /^}/ { exit found ? 0 : 1 }
  END { if (!found) exit 1 }
' "$controller"

# The worker must consume that hosts file in an isolated mount namespace.
# Global /etc/hosts must remain untouched, and aria2 must use libc resolution.
grep -F 'hosts_file="$url_file.hosts"' "$worker"
grep -F 'unshare --mount --propagation private' "$worker"
grep -F 'mount --bind "$hosts_file" /etc/hosts' "$worker"
grep -F -- '--async-dns=false' "$worker"
grep -F 'gitsha1:' "$worker"
grep -F "printf 'blob %s\\0'" "$worker"

if grep -Fq -- '--async-dns-server=' "$worker"; then
  echo "worker still bypasses the isolated hosts mapping" >&2
  exit 1
fi

echo "video download egress config: PASS"
