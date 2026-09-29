#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CONFIG_PATH=${DGX_GATEWAY_CONFIG:-"$HOME/.config/dgx-cluster-gateway/gateway.toml"}
STATE_DIR=${DGX_GATEWAY_STATE_DIR:-"$HOME/.local/state/dgx-cluster-gateway"}
TOKEN_PATH=${DGX_GATEWAY_TOKEN_PATH:-"$HOME/.config/dgx-cluster-gateway/admin.token"}
LOG_PATH="$STATE_DIR/gateway.log"
LABEL="com.dgx.cluster-gateway"
SERVICE_TARGET="gui/$(id -u)/$LABEL"
READY_URL=${DGX_GATEWAY_READY_URL:-"http://127.0.0.1:18082/api/v1/snapshot"}

is_running() {
    launchctl print "$SERVICE_TARGET" 2>/dev/null | grep -q 'state = running'
}

initialize() {
    mkdir -p "$(dirname "$CONFIG_PATH")" "$STATE_DIR"
    chmod 700 "$(dirname "$CONFIG_PATH")" "$STATE_DIR"
    if [ ! -f "$CONFIG_PATH" ]; then
        cp "$SCRIPT_DIR/config/gateway.example.toml" "$CONFIG_PATH"
        chmod 600 "$CONFIG_PATH"
    fi
    if [ ! -f "$TOKEN_PATH" ]; then
        umask 077
        openssl rand -hex 32 > "$TOKEN_PATH"
    fi
    chmod 600 "$TOKEN_PATH"
    printf '%s\n' "initialized config=$CONFIG_PATH token=$TOKEN_PATH"
}

bootstrap() {
    initialize >/dev/null
    PYTHONPATH="$(dirname "$SCRIPT_DIR")" python3 -m DGXGateway.dgx_gateway.admission init --config "$CONFIG_PATH" >/dev/null
    printf '%s\n' "initialized config=$CONFIG_PATH token=$TOKEN_PATH authority-admission=ready"
}

ensure_no_active_operation() {
    if ! PYTHONPATH="$(dirname "$SCRIPT_DIR")" python3 -m DGXGateway.dgx_gateway.maintenance set-mutation-fence --config "$CONFIG_PATH" --enabled true; then
        printf '%s\n' "refusing to stop Gateway while a model operation is active" >&2
        exit 1
    fi
}

start_gateway() {
    initialize >/dev/null
    PYTHONPATH="$(dirname "$SCRIPT_DIR")" python3 -m DGXGateway.dgx_gateway.admission check --config "$CONFIG_PATH" >/dev/null
    if is_running; then
        ensure_no_active_operation >/dev/null
        if ! curl -fsS --max-time 12 "$READY_URL" >/dev/null; then
            printf '%s\n' "gateway failed readiness; mutation remains fenced; inspect $LOG_PATH" >&2
            exit 1
        fi
        PYTHONPATH="$(dirname "$SCRIPT_DIR")" python3 -m DGXGateway.dgx_gateway.maintenance set-mutation-fence --config "$CONFIG_PATH" --enabled false >/dev/null
        printf '%s\n' "already running http://127.0.0.1:18082/"
        return
    fi
    # Cold startup must be possible after a crash so retained jobs can reconcile.
    PYTHONPATH="$(dirname "$SCRIPT_DIR")" python3 -m DGXGateway.dgx_gateway.maintenance set-mutation-fence --config "$CONFIG_PATH" --enabled true --allow-creative-recovery >/dev/null
    launchctl remove "$LABEL" >/dev/null 2>&1 || true
    launchctl submit -l "$LABEL" -o "$LOG_PATH" -e "$LOG_PATH" -- \
        "$SCRIPT_DIR/run-gateway.sh" "$CONFIG_PATH"
    count=0
    while ! is_running && [ "$count" -lt 50 ]; do
        sleep 0.1
        count=$((count + 1))
    done
    if ! is_running; then
        printf '%s\n' "gateway failed to start; inspect $LOG_PATH" >&2
        exit 1
    fi
    count=0
    while ! curl -fsS --max-time 1 "$READY_URL" >/dev/null 2>&1 && [ "$count" -lt 50 ]; do
        sleep 0.2
        count=$((count + 1))
    done
    if ! curl -fsS --max-time 12 "$READY_URL" >/dev/null; then
        launchctl remove "$LABEL" >/dev/null 2>&1 || true
        printf '%s\n' "gateway failed readiness; mutation remains fenced; inspect $LOG_PATH" >&2
        exit 1
    fi
    PYTHONPATH="$(dirname "$SCRIPT_DIR")" python3 -m DGXGateway.dgx_gateway.maintenance set-mutation-fence --config "$CONFIG_PATH" --enabled false >/dev/null
    printf '%s\n' "started http://127.0.0.1:18082/"
}

stop_gateway() {
    if ! is_running; then
        launchctl remove "$LABEL" >/dev/null 2>&1 || true
        printf '%s\n' "not running"
        return
    fi
    ensure_no_active_operation
    launchctl kill SIGTERM "$SERVICE_TARGET"
    launchctl remove "$LABEL"
    printf '%s\n' "stopped"
}

case "${1:-}" in
    init) bootstrap ;;
    start) start_gateway ;;
    stop) stop_gateway ;;
    recovery-restart)
        PYTHONPATH="$(dirname "$SCRIPT_DIR")" python3 -m DGXGateway.dgx_gateway.maintenance set-mutation-fence --config "$CONFIG_PATH" --enabled true --recovery-restart
        if is_running; then
            launchctl kill SIGTERM "$SERVICE_TARGET"
            launchctl remove "$LABEL"
        fi
        start_gateway
        ;;
    status)
        if is_running; then
            PYTHONPATH="$(dirname "$SCRIPT_DIR")" python3 -m DGXGateway.dgx_gateway.admission check --config "$CONFIG_PATH" >/dev/null
            printf '%s\n' "running http://127.0.0.1:18082/"
        else
            printf '%s\n' "stopped"
            exit 1
        fi
        ;;
    logs) tail -n "${2:-100}" "$LOG_PATH" ;;
    export)
        [ "$#" -eq 2 ] || { printf '%s\n' "usage: $0 export OUTPUT.tar.gz" >&2; exit 2; }
        PYTHONPATH="$(dirname "$SCRIPT_DIR")" python3 -m DGXGateway.dgx_gateway.maintenance export --config "$CONFIG_PATH" --output "$2"
        ;;
    verify)
        [ "$#" -eq 2 ] || { printf '%s\n' "usage: $0 verify ARCHIVE.tar.gz" >&2; exit 2; }
        PYTHONPATH="$(dirname "$SCRIPT_DIR")" python3 -m DGXGateway.dgx_gateway.maintenance verify --archive "$2"
        ;;
    import)
        [ "$#" -eq 2 ] || { printf '%s\n' "usage: $0 import ARCHIVE.tar.gz" >&2; exit 2; }
        is_running && { printf '%s\n' "stop Gateway before import" >&2; exit 1; }
        PYTHONPATH="$(dirname "$SCRIPT_DIR")" python3 -m DGXGateway.dgx_gateway.maintenance import --config "$CONFIG_PATH" --archive "$2"
        ;;
    recover-preflight-stop)
        [ "$#" -eq 2 ] || { printf '%s\n' "usage: $0 recover-preflight-stop OPERATION_ID" >&2; exit 2; }
        PYTHONPATH="$(dirname "$SCRIPT_DIR")" python3 -m DGXGateway.dgx_gateway.recovery_preflight --config "$CONFIG_PATH" --operation-id "$2"
        ;;
    recover-release)
        [ "$#" -eq 3 ] || { printf '%s\n' "usage: $0 recover-release OPERATION_ID SAFE_PROOF.json" >&2; exit 2; }
        is_running && { printf '%s\n' "stop Gateway before explicit release recovery" >&2; exit 1; }
        PYTHONPATH="$(dirname "$SCRIPT_DIR")" python3 -m DGXGateway.dgx_gateway.recovery --config "$CONFIG_PATH" --operation-id "$2" --safe-proof "$3"
        ;;
    *) printf '%s\n' "usage: $0 {init|start|stop|recovery-restart|status|logs [lines]|export FILE|verify FILE|import FILE|recover-preflight-stop OPERATION_ID|recover-release OPERATION_ID PROOF}" >&2; exit 2 ;;
esac
