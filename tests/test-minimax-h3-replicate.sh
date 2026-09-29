#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/scripts/minimax-h3-two-node/replicate-model.sh"
DISPATCH="$ROOT/scripts/minimax-h3-two-node/worker-dispatch"
MODEL_DISPATCH="$ROOT/scripts/minimax-h3-two-node/worker-model-dispatch"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

grep -q -- '--partial' "$SCRIPT"
grep -q -- '--append-verify' "$SCRIPT"
grep -q -- '--checksum' "$SCRIPT"
grep -q 'StrictHostKeyChecking=yes' "$SCRIPT"
grep -q 'verify-existing' "$SCRIPT"
grep -q 'prepare-model-staging' "$SCRIPT"
grep -q 'finalize-model' "$SCRIPT"
grep -q 'rsync --server' "$DISPATCH"
grep -q 'verify-model' "$DISPATCH"
grep -q 'management_ip=' "$MODEL_DISPATCH"
if grep -Eq 'rm[[:space:]]+-rf|StrictHostKeyChecking=no|UserKnownHostsFile=/dev/null' "$SCRIPT"; then
  echo 'unsafe replication behavior found' >&2
  exit 1
fi
if grep -Eq 'eval|rm[[:space:]]+-rf' "$DISPATCH" "$MODEL_DISPATCH"; then
  echo 'unsafe dispatcher behavior found' >&2
  exit 1
fi

mkdir -p "$TMP/bin"
printf '%s\n' '#!/usr/bin/env bash' 'printf "fake-rsync %s\\n" "$*"' >"$TMP/bin/rsync"
chmod +x "$TMP/bin/rsync"
SSH_ORIGINAL_COMMAND='rsync --server -logDtpre.iLsfxCIvu --log-format=X --numeric-ids . /home/operator/dgx-models/video-generation/snapshots/minimax-h3-official.staging-20260830T000000Z/' \
  H3_TESTING=1 H3_WORKER_MODEL_HELPER="$MODEL_DISPATCH" PATH="$TMP/bin:$PATH" "$DISPATCH" | grep -q 'fake-rsync --server'
if SSH_ORIGINAL_COMMAND='rsync --server -logDtpre.iLsfxCIvu . /tmp/escape/' H3_TESTING=1 H3_WORKER_MODEL_HELPER="$MODEL_DISPATCH" PATH="$TMP/bin:$PATH" "$DISPATCH"; then
  echo 'dispatcher accepted an out-of-scope destination' >&2
  exit 1
fi

output="$("$SCRIPT" plan --config "$ROOT/config/minimax-h3-two-node.env.example" \
  --lock "$ROOT/manifests/minimax-h3-fl2va-lock.json" 2>&1 || true)"
grep -q 'PLAN verify-existing=' <<<"$output"

echo 'PASS minimax h3 replication contract'
