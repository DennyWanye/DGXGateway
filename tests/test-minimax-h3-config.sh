#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/scripts/minimax-h3-two-node/common.sh"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

make_config() {
  cp "$ROOT/config/minimax-h3-two-node.env.example" "$TMP/config.env"
}

expect_fail() {
  local pattern="$1"
  shift
  if "$@" >"$TMP/stdout" 2>"$TMP/stderr"; then
    echo "expected failure: $*" >&2
    return 1
  fi
  grep -q "$pattern" "$TMP/stderr"
}

make_config
h3_load_config "$TMP/config.env"
[[ "$H3_EXTERNAL_PORT" == 18081 ]]
[[ "$H3_LLM_COEXISTENCE_MODE" == exclusive ]]
[[ "$H3_SELECTED_PROFILE" == heretic-hmnsfw-v1 ]]

make_config
sed -i.bak 's/^H3_EXTERNAL_PORT=.*/H3_EXTERNAL_PORT=8080/' "$TMP/config.env"
expect_fail 'H3_EXTERNAL_PORT must be 18081' h3_load_config "$TMP/config.env"

make_config
sed -i.bak 's/^H3_EXTERNAL_BIND=.*/H3_EXTERNAL_BIND=0.0.0.0/' "$TMP/config.env"
expect_fail 'unsafe bind' h3_load_config "$TMP/config.env"

make_config
printf 'H3_GATEWAY_TOKEN=should-not-be-loaded\n' >>"$TMP/config.env"
expect_fail 'unknown config key' h3_load_config "$TMP/config.env"

make_config
sed -i.bak "s|^H3_MODEL_ROOT=.*|H3_MODEL_ROOT=/models/'bad|" "$TMP/config.env"
expect_fail 'unsafe characters' h3_load_config "$TMP/config.env"

expect_fail 'hostname mismatch' h3_assert_host_role \
  head spark-wrong operator 192.0.2.30 SHA256:YT7DxbMuiqEtnnYJ05MFaGwvjfTml3ffhsiOfVM+9RY
h3_assert_host_role \
  head dgx-head operator 192.0.2.30 SHA256:YT7DxbMuiqEtnnYJ05MFaGwvjfTml3ffhsiOfVM+9RY

redacted="$(h3_redact 'Authorization: Bearer topsecret H3_GATEWAY_TOKEN=topsecret')"
[[ "$redacted" != *topsecret* ]] || { echo 'redactor leaked secret' >&2; exit 1; }
[[ "$redacted" == *Authorization* ]] || { echo 'redactor discarded input' >&2; exit 1; }
[[ "$redacted" == *'[REDACTED]'* ]] || { echo 'redactor did not mark replacement' >&2; exit 1; }

expect_fail 'resolved model lock is required for deploy' "$ROOT/scripts/minimax-h3-two-node/preflight.sh" \
  --phase deploy --config "$ROOT/config/minimax-h3-two-node.env.example" \
  --runtime-lock "$ROOT/manifests/minimax-h3-runtime-lock.json" --fixture "$ROOT/tests/fixtures/minimax-h3/head-facts.env"

"$ROOT/scripts/minimax-h3-two-node/preflight.sh" \
  --phase research --config "$ROOT/config/minimax-h3-two-node.env.example" \
  --runtime-lock "$ROOT/manifests/minimax-h3-runtime-lock.json" --fixture "$ROOT/tests/fixtures/minimax-h3/head-facts.env" \
  | grep -q 'PASS phase=research role=head'
grep -q 'lock-model.py.*verify' "$ROOT/scripts/minimax-h3-two-node/preflight.sh"

echo "PASS minimax h3 config and read-only preflight"
