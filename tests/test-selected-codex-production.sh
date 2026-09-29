#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
fixture=$(mktemp -d "${TMPDIR:-/tmp}/selected-codex-production.XXXXXX")
cleanup() {
  case "$fixture" in
    /tmp/*|/var/folders/*) rm -rf -- "$fixture" ;;
    *) echo "refusing unexpected fixture: $fixture" >&2; exit 1 ;;
  esac
}
trap cleanup EXIT

mkdir -p "$fixture/evidence/codex-agentic-20260827" "$fixture/scripts"
cat >"$fixture/scripts/run-codex-compatible-server.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'started:%s\n' "$1"
EOF
chmod +x "$fixture/scripts/run-codex-compatible-server.sh"

printf '{"winner":"gemma4"}\n' >"$fixture/evidence/codex-agentic-20260827/summary.json"
test "$(DGX_MATRIX_ROOT="$fixture" "$root/scripts/run-selected-codex-production.sh")" = started:gemma4

printf '{"winner":"unreviewed-model"}\n' >"$fixture/evidence/codex-agentic-20260827/summary.json"
if DGX_MATRIX_ROOT="$fixture" "$root/scripts/run-selected-codex-production.sh" >"$fixture/unexpected-start.log" 2>&1; then
  echo "unknown winner unexpectedly started" >&2
  exit 1
fi
if grep -q '^started:' "$fixture/unexpected-start.log"; then
  echo "unknown winner reached the server runner" >&2
  exit 1
fi

printf '{"winner":null}\n' >"$fixture/evidence/codex-agentic-20260827/summary.json"
if DGX_MATRIX_ROOT="$fixture" "$root/scripts/run-selected-codex-production.sh" >"$fixture/null-start.log" 2>&1; then
  echo "null winner unexpectedly started" >&2
  exit 1
fi
if grep -q '^started:' "$fixture/null-start.log"; then
  echo "null winner reached the server runner" >&2
  exit 1
fi

echo "Selected Codex production runner: PASS"
