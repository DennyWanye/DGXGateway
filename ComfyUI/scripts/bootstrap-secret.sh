#!/usr/bin/env bash
set -euo pipefail

bundle_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
destination=${1:-"$bundle_root/secrets/h3_gateway.token"}
target=operator@192.0.2.30
ssh_options=(-o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=yes)

if [[ -L "$destination" ]]; then
  echo "Refusing symlink secret destination" >&2
  exit 2
fi
if [[ -e "$destination" && ! -f "$destination" ]]; then
  echo "Secret destination must be a regular file" >&2
  exit 2
fi

role=$(ssh "${ssh_options[@]}" "$target" 'hostname; whoami')
if [[ "$role" != $'dgx-head\noperator' ]]; then
  echo "Head role check failed; expected dgx-head/operator" >&2
  exit 3
fi

destination_dir=$(dirname "$destination")
mkdir -p "$destination_dir"
chmod 700 "$destination_dir"
temporary=$(mktemp "$destination_dir/.h3_gateway.token.tmp.XXXXXX")
cleanup() {
  rm -f -- "$temporary"
}
trap cleanup EXIT HUP INT TERM
chmod 600 "$temporary"

ssh "${ssh_options[@]}" "$target" \
  'sudo -n cat /etc/minimax-h3/gateway.token' >"$temporary"

python3 - "$temporary" <<'PY'
from pathlib import Path
import sys

data = Path(sys.argv[1]).read_bytes()
if not (2 <= len(data) <= 4097):
    raise SystemExit("gateway token length is outside the accepted range")
if b"\0" in data or b"\r" in data or data.count(b"\n") != 1 or not data.endswith(b"\n"):
    raise SystemExit("gateway token must be one non-empty newline-terminated line")
PY

chmod 600 "$temporary"
mv -f -- "$temporary" "$destination"
temporary="$destination_dir/.bootstrap-complete"
chmod 600 "$destination"
trap - EXIT HUP INT TERM
echo "H3 gateway secret installed with mode 0600 at $destination"
