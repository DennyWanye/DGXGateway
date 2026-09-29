#!/usr/bin/env bash
set -Eeuo pipefail
KIT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f "$KIT_DIR/cluster.env" && -f "$KIT_DIR/node.env" ]] || {
  echo 'Create and review cluster.env and node.env first.' >&2; exit 2;
}
# These two configuration files are executable shell syntax; only source your own reviewed files.
source "$KIT_DIR/cluster.env"
source "$KIT_DIR/node.env"
ACTION="${1:-}"
N="${2:-1}"
SUITE="${3:-ring}"
[[ "$ACTION" =~ ^(kernel|comm|serve|help)$ ]] || {
  echo 'Usage: scripts/run_node.sh kernel | help | comm {2|4} {ring|collectives|allpairs} | serve {1|2|4}' >&2; exit 2;
}
for key in H3_ROOT IMAGE EXPECTED_IMAGE_ID MASTER_ADDR CONTROL_IFACE NODE_RANK DIST_PORT TEST_PORT API_PORT API_BIND; do
  value="${!key:-}"
  [[ -n "$value" && "$value" != *CHANGE_ME* ]] || { echo "Missing setting: $key" >&2; exit 2; }
done
[[ "$NODE_RANK" =~ ^[0-3]$ && "$N" =~ ^(1|2|4)$ ]] || { echo 'Bad rank/world'; exit 2; }
for port in "$DIST_PORT" "$TEST_PORT" "$API_PORT"; do
  [[ "$port" =~ ^[0-9]+$ && "$port" -ge 1024 && "$port" -le 65535 ]] || { echo 'Bad port'; exit 2; }
done
[[ "$DIST_PORT" != "$TEST_PORT" && "$DIST_PORT" != "$API_PORT" && "$TEST_PORT" != "$API_PORT" ]] || exit 2
ip link show dev "$CONTROL_IFACE" >/dev/null
python3 - "$MASTER_ADDR" <<'PY'
import ipaddress, sys
address = ipaddress.IPv4Address(sys.argv[1])
if address.is_loopback or address.is_unspecified or address.is_multicast:
    raise SystemExit('MASTER_ADDR must be a unicast IPv4 address reachable from all selected nodes')
PY
ACTUAL_ID="$(docker image inspect "$IMAGE" --format '{{.Id}}')"
[[ "$ACTUAL_ID" == "$EXPECTED_IMAGE_ID" ]] || { echo 'Image ID mismatch; stop.' >&2; exit 2; }
ARCH="$(docker image inspect "$IMAGE" --format '{{.Architecture}}')"
[[ "$ARCH" == arm64 ]] || { echo "Expected arm64 image, got $ARCH"; exit 2; }
mkdir -p "$H3_ROOT/cache" "$H3_ROOT/logs"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-${ACTION}-n${N}-rank${NODE_RANK}-$$"
LOG_DIR="$H3_ROOT/logs/$RUN_ID"
mkdir -p "$LOG_DIR"
NAME="h3-ring-${ACTION}-r${NODE_RANK}"
if docker container inspect "$NAME" >/dev/null 2>&1; then
  echo "Container $NAME exists. Inspect it; this script will not replace it." >&2; exit 2
fi
ARGS=(--rm --name "$NAME" --gpus all --network host --ipc host --ulimit memlock=-1
      --mount "type=bind,src=$KIT_DIR,dst=/kit,readonly"
      --mount "type=bind,src=$H3_ROOT/cache,dst=/cache"
      --mount "type=bind,src=$LOG_DIR,dst=/runlogs"
      -e HOME=/cache -e HF_HOME=/cache/huggingface -e CUDA_VISIBLE_DEVICES=0
      -e PYTHONUNBUFFERED=1 -e "NCCL_SOCKET_IFNAME==$CONTROL_IFACE"
      -e "GLOO_SOCKET_IFNAME=$CONTROL_IFACE" -e NCCL_DEBUG=INFO
      -e NCCL_DEBUG_SUBSYS=INIT,NET,GRAPH,COLL
      -e NCCL_DEBUG_FILE=/runlogs/nccl.%h.%p.log
      -e TORCH_NCCL_ASYNC_ERROR_HANDLING=1)
case "${NETWORK_MODE:-}" in
  ib)
    [[ -d /dev/infiniband ]] || { echo 'No RDMA devices'; exit 2; }
    [[ "${NCCL_HCA:-}" == =* && "$NCCL_HCA" != *CHANGE_ME* ]] || { echo 'Fill exact NCCL_HCA'; exit 2; }
    ARGS+=(--device /dev/infiniband -e NCCL_NET=IB -e NCCL_IB_DISABLE=0 -e "NCCL_IB_HCA=$NCCL_HCA")
    ;;
  socket) ARGS+=(-e NCCL_NET=Socket -e NCCL_IB_DISABLE=1) ;;
  *) echo 'Supported modes: ib or socket. Port custom plugins explicitly before using them.'; exit 2 ;;
esac
case "$ACTION" in
  kernel) CMD=(python /kit/scripts/fa_probe.py) ;;
  help) CMD=(sglang serve --help) ;;
  comm)
    [[ "$N" != 1 && "$NODE_RANK" -lt "$N" ]] || { echo 'Rank outside comm world'; exit 2; }
    [[ "$SUITE" =~ ^(ring|collectives|allpairs)$ ]] || exit 2
    CMD=(torchrun --nnodes "$N" --nproc-per-node 1 --node-rank "$NODE_RANK"
         --master-addr "$MASTER_ADDR" --master-port "$TEST_PORT"
         /kit/scripts/comm_probe.py --suite "$SUITE")
    ;;
  serve)
    [[ "$NODE_RANK" -lt "$N" ]] || { echo 'Rank outside service world'; exit 2; }
    [[ -f "$MODEL_HOST_PATH/model_index.json" ]] || { echo 'Missing model root'; exit 2; }
    case "$MODEL_VARIANT" in
      fl2va) PARTITION=FL2VA ;;
      ref2va) PARTITION=Ref2VA ;;
      *) echo 'Unsupported model variant'; exit 2 ;;
    esac
    [[ -f "$MODEL_HOST_PATH/$PARTITION/model_index.json" ]] || { echo 'Missing task partition'; exit 2; }
    ARGS+=(--mount "type=bind,src=$MODEL_HOST_PATH,dst=/models/h3,readonly"
           -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1)
    CMD=(sglang serve --model-path /models/h3 --model-variant "$MODEL_VARIANT"
         --num-gpus "$N" --nnodes "$N" --node-rank "$NODE_RANK"
         --dist-init-addr "$MASTER_ADDR:$DIST_PORT"
         --tp-size 1 --sp-degree "$N" --ulysses-degree 1 --ring-degree "$N"
         --encoder-parallel replicate --attention-backend fa
         --performance-mode speed --enable-torch-compile false --use-fsdp-inference false
         --host "$API_BIND" --port "$API_PORT")
    case "${QUANTIZATION:-}" in
      fp8) CMD+=(--quantization fp8) ;;
      bf16) ;; # Experimental memory baseline only; this is not the recommended Ring4 preset.
      *) echo 'QUANTIZATION must be fp8 or bf16'; exit 2 ;;
    esac
    ;;
esac
{
  printf 'image_id=%s\nnetwork_mode=%s\n' "$ACTUAL_ID" "$NETWORK_MODE"
  printf 'command='; printf '%q ' "${CMD[@]}"; printf '\n'
} | tee "$LOG_DIR/launch.txt"
printf 'Container: %s\nLogs: %s\n' "$NAME" "$LOG_DIR"
# No privileged mode, no network rewrites, no automatic restarts, no silent Socket fallback.
docker run "${ARGS[@]}" "$IMAGE" "${CMD[@]}" 2>&1 | tee "$LOG_DIR/stdout.log"
