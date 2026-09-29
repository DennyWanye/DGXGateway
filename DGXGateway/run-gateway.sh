#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CONFIG_PATH=${1:?gateway config path required}
export PYTHONPATH="$(dirname "$SCRIPT_DIR")${PYTHONPATH:+:$PYTHONPATH}"
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
exec python3 -m DGXGateway.dgx_gateway --config "$CONFIG_PATH"
