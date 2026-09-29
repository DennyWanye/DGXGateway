#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/scripts/minimax-h3-two-node/build-runtime.sh"
FLATTEN="$ROOT/scripts/minimax-h3-two-node/flatten-runtime-image.sh"
LOCK="$ROOT/manifests/minimax-h3-runtime-lock.json"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

expect_fail() {
  local pattern="$1"
  shift
  if "$@" >"$TMP/stdout" 2>"$TMP/stderr"; then
    echo "expected failure: $*" >&2
    return 1
  fi
  grep -q "$pattern" "$TMP/stderr"
}

"$SCRIPT" validate --phase research --lock "$LOCK" | grep -q 'PASS runtime-lock phase=research'
"$SCRIPT" validate --phase build --lock "$LOCK" | grep -q 'PASS runtime-lock phase=build'
"$SCRIPT" validate --phase research --lock "$LOCK" --proxy-url http://192.0.2.29:7890 | grep -q 'PASS runtime-lock phase=research'
expect_fail 'credentials are forbidden' "$SCRIPT" validate --phase research --lock "$LOCK" --proxy-url http://user:secret@192.0.2.29:7890

python3 - "$LOCK" "$TMP/unresolved-production.json" <<'PY'
import json,sys
data=json.load(open(sys.argv[1]))
data["status"]="resolved"
data["production"]={"final_image_id":"UNRESOLVED","oci_archive_sha256":"UNRESOLVED"}
json.dump(data,open(sys.argv[2],"w"),sort_keys=True)
PY
expect_fail 'UNRESOLVED' "$SCRIPT" validate --phase deploy --lock "$TMP/unresolved-production.json"

python3 - "$LOCK" "$TMP/resolved.json" <<'PY'
import json,sys
data=json.load(open(sys.argv[1]))
data["status"]="resolved"
data["production"]["final_image_id"]="sha256:" + "a"*64
data["production"]["oci_archive_sha256"]="b"*64
for patch in data["patch_targets"]:
    patch["preimage_sha256"]="c"*64
    patch["result_sha256"]="d"*64
data["wheelhouse"]={"status":"resolved","requirements_lock_sha256":"e"*64,"tree_sha256":"f"*64}
json.dump(data,open(sys.argv[2],"w"),sort_keys=True)
PY
"$SCRIPT" validate --phase deploy --lock "$TMP/resolved.json" | grep -q 'PASS runtime-lock phase=deploy'

grep -q 'rank 1' "$ROOT/patches/minimax-h3-two-node/0001-rank1-first-and-evidence.patch"
grep -q 'H3_RANK_EVIDENCE_DIR' "$ROOT/patches/minimax-h3-two-node/0001-rank1-first-and-evidence.patch"
grep -q 'RAY_AUTH_MODE' "$ROOT/patches/minimax-h3-two-node/0002-require-ray-token-auth.patch"
grep -q -- '--require-hashes' "$ROOT/patches/minimax-h3-two-node/0003-offline-ray-wheelhouse.patch"
grep -q 'from vllm_omni.plugins import load_omni_general_plugins' "$ROOT/patches/minimax-h3-two-node/0004-worker-plugin-import.patch"
grep -q 'python -m py_compile' "$ROOT/patches/minimax-h3-two-node/hotfix.Dockerfile"
grep -q 'deployment_overlays' "$LOCK"
grep -q 'HTTPS_PROXY' "$SCRIPT"
grep -q 'verify_target_hash' "$SCRIPT"
grep -q 'upstream_config' "$SCRIPT"
grep -q 'pinned-source-and-target-hashes' "$SCRIPT"
grep -q "H3_ACCEPTED_BASE_IMAGE_ID=\$layer1_id" "$SCRIPT"
if grep -q 'layer1 image ID does not match accepted image' "$SCRIPT"; then
  echo 'historical BuildKit image IDs must not replace source and target hash verification' >&2
  exit 1
fi
if grep -Eq 'systemctl|nft|ufw|ip route (add|del|replace)' "$SCRIPT"; then
  echo 'build script must not mutate host network or daemon configuration' >&2
  exit 1
fi
grep -q 'H3_ALLOW_FLATTEN_EXPORT=1' "$FLATTEN"
grep -q 'docker export' "$FLATTEN"
grep -q 'docker import' "$FLATTEN"
grep -q 'docker save --output' "$FLATTEN"
grep -q 'refusing flatten operation while containers exist' "$FLATTEN"
if grep -Eq 'docker (system|container|image) prune' "$FLATTEN"; then
  echo 'flatten helper must never prune Docker state' >&2
  exit 1
fi

echo "PASS minimax h3 runtime lock"
