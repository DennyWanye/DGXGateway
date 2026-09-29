#!/usr/bin/env bash
set -euo pipefail

umask 077

HEAD=operator@192.0.2.30
WORKER=operator@192.0.2.31
HEAD_NAME=dgx-head
WORKER_NAME=dgx-worker
WORKER_ADDRESSES=(192.0.2.31 192.0.2.34 192.0.2.36)
SSH_DIR=/etc/minimax-h3/ssh
IDENTITY=$SSH_DIR/cluster_ed25519
KNOWN_HOSTS=$SSH_DIR/known_hosts
SSH_CONFIG=$SSH_DIR/ssh_config
MANIFEST=$SSH_DIR/provision.env
AUTH_FILE=/home/operator/.ssh/authorized_keys
MARKER='# minimax-h3-two-node-key-v1'
FORCED_COMMAND=/usr/local/libexec/minimax-h3-worker-dispatch
FORCED_AUTH_PREFIX='restrict,command="/usr/local/libexec/minimax-h3-worker-dispatch"'

ssh_management_options=(-o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=yes)
HEAD_PUBLIC=
EXPECTED_KNOWN_SHA=
EXPECTED_CONFIG_SHA=

die() {
  echo "FAIL: $*" >&2
  exit 1
}

require_write_authority() {
  [[ ${H3_SHARED_DEPLOYMENT_LOCK_ASSERTED-} == 1 ]] || die "shared deployment lock assertion is required"
  [[ ${H3_SHARED_DEPLOYMENT_LOCK_OWNER-} == minimax-h3-two-node ]] || die "wrong shared deployment lock owner"
}

verify_management_roles() {
  local head_identity worker_identity
  head_identity=$(ssh "${ssh_management_options[@]}" "$HEAD" 'hostname; whoami')
  worker_identity=$(ssh "${ssh_management_options[@]}" "$WORKER" 'hostname; whoami')
  [[ "$head_identity" == "$HEAD_NAME"$'\n'operator ]] || die "head role mismatch"
  [[ "$worker_identity" == "$WORKER_NAME"$'\n'operator ]] || die "worker role mismatch"
}

fingerprint_public_key() {
  ssh-keygen -lf - | awk '{print $2}'
}

# The address is selected only from the fixed WORKER_ADDRESSES array above.
# shellcheck disable=SC2029
scan_from_head() {
  local address=$1
  ssh "${ssh_management_options[@]}" "$HEAD" "ssh-keyscan -T 5 -t ed25519 $address 2>/dev/null"
}

verify_worker_host_keys() {
  local trusted_public trusted_fingerprint address candidate candidate_fingerprint
  trusted_public=$(ssh "${ssh_management_options[@]}" "$WORKER" 'sudo -n cat /etc/ssh/ssh_host_ed25519_key.pub')
  [[ "$trusted_public" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+([[:space:]].*)?$ ]] || die "invalid authenticated worker host key"
  trusted_fingerprint=$(printf '%s\n' "$trusted_public" | fingerprint_public_key)
  for address in "${WORKER_ADDRESSES[@]}"; do
    if [[ "$address" == 192.0.2.31 ]]; then
      candidate=$(ssh-keyscan -T 5 -t ed25519 "$address" 2>/dev/null)
    else
      candidate=$(scan_from_head "$address")
    fi
    [[ $(grep -c 'ssh-ed25519' <<<"$candidate") -eq 1 ]] || die "ambiguous ssh-keyscan result for $address"
    candidate_fingerprint=$(printf '%s\n' "$candidate" | awk '{print $2, $3}' | fingerprint_public_key)
    [[ "$candidate_fingerprint" == "$trusted_fingerprint" ]] || die "host-key mismatch for $address"
  done
  TRUSTED_PUBLIC=$trusted_public
  TRUSTED_FINGERPRINT=$trusted_fingerprint
}

# IDENTITY is a fixed root-owned path, not user input.
# shellcheck disable=SC2029
prepare_head_identity() {
  ssh "${ssh_management_options[@]}" "$HEAD" sudo -n bash -s <<'REMOTE'
set -euo pipefail
umask 077
install -d -o root -g root -m 0700 /etc/minimax-h3/ssh
for path in /etc/minimax-h3/ssh/cluster_ed25519 /etc/minimax-h3/ssh/cluster_ed25519.pub /etc/minimax-h3/ssh/provision.env; do
  [[ ! -e "$path" ]] || { echo "existing H3 SSH identity or manifest" >&2; exit 1; }
done
ssh-keygen -q -t ed25519 -N '' -C minimax-h3-two-node-key-v1 -f /etc/minimax-h3/ssh/cluster_ed25519
chown root:root /etc/minimax-h3/ssh/cluster_ed25519 /etc/minimax-h3/ssh/cluster_ed25519.pub
chmod 0600 /etc/minimax-h3/ssh/cluster_ed25519
chmod 0644 /etc/minimax-h3/ssh/cluster_ed25519.pub
REMOTE
  HEAD_PUBLIC=$(ssh "${ssh_management_options[@]}" "$HEAD" "sudo -n cat $IDENTITY.pub")
  [[ "$HEAD_PUBLIC" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+\ minimax-h3-two-node-key-v1$ ]] || die "invalid generated H3 public key"
}

verify_provisioning_preimage() {
  local marker_encoded
  marker_encoded=$(printf '%s' "$MARKER" | base64)
  ssh "${ssh_management_options[@]}" "$HEAD" sudo -n bash -s <<'REMOTE'
set -euo pipefail
for path in \
  /etc/minimax-h3/ssh/cluster_ed25519 \
  /etc/minimax-h3/ssh/cluster_ed25519.pub \
  /etc/minimax-h3/ssh/known_hosts \
  /etc/minimax-h3/ssh/ssh_config \
  /etc/minimax-h3/ssh/provision.env; do
  [[ ! -e "$path" ]] || { echo "existing H3 SSH provisioning state: $path" >&2; exit 1; }
done
REMOTE
  if ssh "${ssh_management_options[@]}" "$WORKER" bash -s -- "$marker_encoded" "$AUTH_FILE" <<'REMOTE'
set -euo pipefail
marker=$(printf '%s' "$1" | base64 -d)
[[ -f "$2" ]] || exit 1
grep -Fxq "$marker" "$2"
REMOTE
  then
    die "existing H3 authorization marker"
  fi
}

install_worker_authorization() {
  local encoded marker_encoded
  encoded=$(printf '%s' "$HEAD_PUBLIC" | base64)
  marker_encoded=$(printf '%s' "$MARKER" | base64)
  ssh "${ssh_management_options[@]}" "$WORKER" sudo -n bash -s -- "$encoded" "$marker_encoded" "$FORCED_COMMAND" <<'REMOTE'
set -euo pipefail
umask 077
public_key=$(printf '%s' "$1" | base64 -d)
marker=$(printf '%s' "$2" | base64 -d)
forced_command=$3
auth_file=/home/operator/.ssh/authorized_keys
[[ -x "$forced_command" && ! -L "$forced_command" ]] || { echo "unique worker dispatcher is missing or unsafe" >&2; exit 1; }
[[ $(stat -c '%u' "$forced_command") == 0 ]] || { echo "unique worker dispatcher is not root-owned" >&2; exit 1; }
[[ $((0$(stat -c '%a' "$forced_command") & 0022)) == 0 ]] || { echo "unique worker dispatcher is writable by group/other" >&2; exit 1; }
install -d -o operator -g operator -m 0700 /home/operator/.ssh
line="restrict,command=\"$forced_command\" $public_key"
if [[ -f "$auth_file" ]] && grep -Fxq "$marker" "$auth_file"; then
  existing=$(grep -A1 -Fx "$marker" "$auth_file" | tail -1)
  [[ "$existing" == "$line" ]] || { echo "conflicting H3 authorization marker" >&2; exit 1; }
  exit 0
fi
temporary=$(mktemp /home/operator/.ssh/authorized_keys.minimax-h3.XXXXXX)
if [[ -f "$auth_file" ]]; then cat "$auth_file" >"$temporary"; fi
printf '%s\n%s\n' "$marker" "$line" >>"$temporary"
chown operator:operator "$temporary"
chmod 0600 "$temporary"
mv -f "$temporary" "$auth_file"
REMOTE
  AUTH_LINE="restrict,command=\"$FORCED_COMMAND\" $HEAD_PUBLIC"
}

install_head_trust() {
  local key_blob known_content known_encoded config_content config_encoded auth_hash
  key_blob=$(awk '{print $2}' <<<"$TRUSTED_PUBLIC")
  known_content="${WORKER_NAME} ssh-ed25519 ${key_blob}"$'\n'
  local address
  for address in "${WORKER_ADDRESSES[@]}"; do
    known_content+="${address} ssh-ed25519 ${key_blob}"$'\n'
  done
  config_content=$(cat <<EOF
Host minimax-h3-worker
    HostName=192.0.2.34
    User=operator
    IdentityFile=$IDENTITY
    UserKnownHostsFile=$KNOWN_HOSTS
    BatchMode=yes
    IdentitiesOnly=yes
    StrictHostKeyChecking=yes
    UpdateHostKeys=no
    ClearAllForwardings=yes
    RequestTTY=no
EOF
)
  known_encoded=$(printf '%s' "$known_content" | base64)
  config_encoded=$(printf '%s' "$config_content" | base64)
  EXPECTED_KNOWN_SHA=$(printf '%s' "$known_content" | sha256sum | awk '{print $1}')
  EXPECTED_CONFIG_SHA=$(printf '%s' "$config_content" | sha256sum | awk '{print $1}')
  auth_hash=$(printf '%s' "$AUTH_LINE" | sha256sum | awk '{print $1}')
  ssh "${ssh_management_options[@]}" "$HEAD" sudo -n bash -s -- \
    "$known_encoded" "$config_encoded" "$TRUSTED_FINGERPRINT" "$auth_hash" <<'REMOTE'
set -euo pipefail
umask 077
ssh_dir=/etc/minimax-h3/ssh
known_hosts=$ssh_dir/known_hosts
ssh_config=$ssh_dir/ssh_config
manifest=$ssh_dir/provision.env
[[ ! -e "$known_hosts" && ! -e "$ssh_config" && ! -e "$manifest" ]] || { echo "existing H3 trust files" >&2; exit 1; }
printf '%s' "$1" | base64 -d >"$known_hosts"
printf '%s' "$2" | base64 -d >"$ssh_config"
chmod 0600 "$known_hosts" "$ssh_config"
identity_sha=$(sha256sum "$ssh_dir/cluster_ed25519" | awk '{print $1}')
public_sha=$(sha256sum "$ssh_dir/cluster_ed25519.pub" | awk '{print $1}')
known_sha=$(sha256sum "$known_hosts" | awk '{print $1}')
config_sha=$(sha256sum "$ssh_config" | awk '{print $1}')
{
  printf 'MARKER=%q\n' '# minimax-h3-two-node-key-v1'
  printf 'WORKER_HOST_FINGERPRINT=%q\n' "$3"
  printf 'AUTH_LINE_SHA256=%q\n' "$4"
  printf 'IDENTITY_SHA256=%q\n' "$identity_sha"
  printf 'PUBLIC_SHA256=%q\n' "$public_sha"
  printf 'KNOWN_HOSTS_SHA256=%q\n' "$known_sha"
  printf 'SSH_CONFIG_SHA256=%q\n' "$config_sha"
} >"$manifest"
chmod 0600 "$manifest"
REMOTE
}

# SSH_CONFIG and KNOWN_HOSTS are fixed root-owned paths, not user input.
# shellcheck disable=SC2029
verify_provisioning() {
  local identity
  verify_management_roles
  identity=$(ssh "${ssh_management_options[@]}" "$HEAD" \
    "sudo -n ssh -F $SSH_CONFIG minimax-h3-worker status")
  grep -Fq "role=worker host=$WORKER_NAME user=operator management_ip=192.0.2.31" <<<"$identity" \
    || die "forced status identity failed"
  if ssh "${ssh_management_options[@]}" "$HEAD" \
    "sudo -n ssh -F $SSH_CONFIG minimax-h3-worker id" >/dev/null 2>&1; then
    die "arbitrary command unexpectedly succeeded"
  fi
  if ssh "${ssh_management_options[@]}" "$HEAD" \
    "sudo -n scp -F $SSH_CONFIG /etc/hostname minimax-h3-worker:/tmp/minimax-h3-forbidden" >/dev/null 2>&1; then
    die "scp unexpectedly succeeded"
  fi
  ssh "${ssh_management_options[@]}" "$HEAD" \
    "sudo -n ssh-keygen -F 192.0.2.31 -f $KNOWN_HOSTS >/dev/null && \
     sudo -n ssh-keygen -F 192.0.2.34 -f $KNOWN_HOSTS >/dev/null && \
     sudo -n ssh-keygen -F 192.0.2.36 -f $KNOWN_HOSTS >/dev/null"
  echo "PASS dedicated H3 SSH trust and unique forced-command"
}

prepare_provisioning() {
  require_write_authority
  verify_management_roles
  verify_worker_host_keys
  verify_provisioning_preimage
  trap rollback_after_prepare_failure ERR
  prepare_head_identity
  install_worker_authorization
  install_head_trust
  verify_provisioning
  trap - ERR
  echo "PREPARED marker=minimax-h3-two-node-key-v1"
}

rollback_after_prepare_failure() {
  local exit_code=$?
  trap - ERR
  rollback_partial_provisioning || true
  exit "$exit_code"
}

remove_worker_authorization() {
  local public_key=$1 auth_hash marker_encoded
  [[ -n "$public_key" ]] || return 0
  auth_hash=$(printf '%s' "$FORCED_AUTH_PREFIX $public_key" | sha256sum | awk '{print $1}')
  marker_encoded=$(printf '%s' "$MARKER" | base64)
  ssh "${ssh_management_options[@]}" "$WORKER" sudo -n bash -s -- "$marker_encoded" "$auth_hash" <<'REMOTE'
set -euo pipefail
auth_file=/home/operator/.ssh/authorized_keys
marker=$(printf '%s' "$1" | base64 -d)
expected_hash=$2
[[ -f "$auth_file" && ! -L "$auth_file" ]] || exit 0
if ! grep -Fxq "$marker" "$auth_file"; then exit 0; fi
line=$(grep -A1 -Fx "$marker" "$auth_file" | tail -1)
actual_hash=$(printf '%s' "$line" | sha256sum | awk '{print $1}')
[[ "$actual_hash" == "$expected_hash" ]] || { echo "authorization line hash mismatch" >&2; exit 1; }
temporary=$(mktemp /home/operator/.ssh/authorized_keys.minimax-h3.XXXXXX)
awk -v marker="$marker" 'skip {skip=0; next} $0==marker {skip=1; next} {print}' "$auth_file" >"$temporary"
chown operator:operator "$temporary"
chmod 0600 "$temporary"
mv -f "$temporary" "$auth_file"
REMOTE
}

# MANIFEST and IDENTITY are fixed root-owned paths, not user input.
# shellcheck disable=SC2029
rollback_partial_provisioning() {
  local public_key=$HEAD_PUBLIC public_encoded
  if ssh "${ssh_management_options[@]}" "$HEAD" "sudo -n test -f $MANIFEST"; then
    rollback_provisioning
    return
  fi
  if [[ -z "$public_key" ]]; then
    public_key=$(ssh "${ssh_management_options[@]}" "$HEAD" \
      "sudo -n sh -c 'test ! -f $IDENTITY.pub || cat $IDENTITY.pub'")
  fi
  if [[ -n "$public_key" ]]; then
    [[ "$public_key" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+\ minimax-h3-two-node-key-v1$ ]] \
      || die "partial H3 public key is ambiguous; refusing cleanup"
    remove_worker_authorization "$public_key"
  fi
  public_encoded=$(printf '%s' "$public_key" | base64)
  ssh "${ssh_management_options[@]}" "$HEAD" sudo -n bash -s -- \
    "$public_encoded" "$EXPECTED_KNOWN_SHA" "$EXPECTED_CONFIG_SHA" <<'REMOTE'
set -euo pipefail
ssh_dir=/etc/minimax-h3/ssh
identity=$ssh_dir/cluster_ed25519
public=$identity.pub
known=$ssh_dir/known_hosts
config=$ssh_dir/ssh_config
expected_public=$(printf '%s' "$1" | base64 -d)
expected_known_sha=$2
expected_config_sha=$3
for path in "$identity" "$public" "$known" "$config"; do
  if [[ -e "$path" ]]; then
    [[ -f "$path" && ! -L "$path" && $(stat -c '%u' "$path") == 0 ]] \
      || { echo "unsafe partial H3 SSH file: $path" >&2; exit 1; }
  fi
done
if [[ -e "$identity" || -e "$public" ]]; then
  [[ -f "$identity" && -f "$public" && -n "$expected_public" ]] \
    || { echo "incomplete H3 identity pair; refusing cleanup" >&2; exit 1; }
  [[ $(cat "$public") == "$expected_public" ]] \
    || { echo "partial H3 public key changed; refusing cleanup" >&2; exit 1; }
fi
if [[ -e "$known" ]]; then
  [[ -n "$expected_known_sha" && $(sha256sum "$known" | awk '{print $1}') == "$expected_known_sha" ]] \
    || { echo "partial known_hosts changed; refusing cleanup" >&2; exit 1; }
fi
if [[ -e "$config" ]]; then
  [[ -n "$expected_config_sha" && $(sha256sum "$config" | awk '{print $1}') == "$expected_config_sha" ]] \
    || { echo "partial ssh_config changed; refusing cleanup" >&2; exit 1; }
fi
rm -f -- "$identity" "$public" "$known" "$config"
REMOTE
}

# MANIFEST and IDENTITY are fixed root-owned paths, not user input.
# shellcheck disable=SC2029
rollback_provisioning() {
  require_write_authority
  local manifest_values auth_hash identity_sha public_sha known_sha config_sha public_key
  manifest_values=$(ssh "${ssh_management_options[@]}" "$HEAD" \
    "sudo -n bash -c 'source $MANIFEST; printf \"%s %s %s %s %s\\n\" \"\$AUTH_LINE_SHA256\" \"\$IDENTITY_SHA256\" \"\$PUBLIC_SHA256\" \"\$KNOWN_HOSTS_SHA256\" \"\$SSH_CONFIG_SHA256\"'")
  read -r auth_hash identity_sha public_sha known_sha config_sha <<<"$manifest_values"
  public_key=$(ssh "${ssh_management_options[@]}" "$HEAD" "sudo -n cat $IDENTITY.pub")
  [[ $(printf '%s' "$FORCED_AUTH_PREFIX $public_key" | sha256sum | awk '{print $1}') == "$auth_hash" ]] \
    || die "manifest authorization hash does not match installed public key"
  remove_worker_authorization "$public_key"
  ssh "${ssh_management_options[@]}" "$HEAD" sudo -n bash -s -- \
    "$identity_sha" "$public_sha" "$known_sha" "$config_sha" <<'REMOTE'
set -euo pipefail
ssh_dir=/etc/minimax-h3/ssh
check() { [[ $(sha256sum "$1" | awk '{print $1}') == "$2" ]] || { echo "hash mismatch: $1" >&2; exit 1; }; }
check "$ssh_dir/cluster_ed25519" "$1"
check "$ssh_dir/cluster_ed25519.pub" "$2"
check "$ssh_dir/known_hosts" "$3"
check "$ssh_dir/ssh_config" "$4"
rm -f -- "$ssh_dir/cluster_ed25519" "$ssh_dir/cluster_ed25519.pub" "$ssh_dir/known_hosts" "$ssh_dir/ssh_config" "$ssh_dir/provision.env"
REMOTE
  echo "ROLLED_BACK marker=minimax-h3-two-node-key-v1"
}

[[ $# -eq 1 ]] || die "usage: $0 prepare|verify|rollback"
case "$1" in
  prepare) prepare_provisioning ;;
  verify) verify_provisioning ;;
  rollback) rollback_provisioning ;;
  *) die "usage: $0 prepare|verify|rollback" ;;
esac
