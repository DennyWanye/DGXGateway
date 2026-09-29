#!/usr/bin/env bash
# Source only from this reviewed bundle. Environment files are trusted shell code.
set -Eeuo pipefail
BUNDLE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
for f in cluster.env node.env; do
  [[ -f "$BUNDLE_ROOT/$f" ]] || fail "Copy and edit $f.example -> $f first."
done
# shellcheck disable=SC1091
source "$BUNDLE_ROOT/cluster.env"
# shellcheck disable=SC1091
source "$BUNDLE_ROOT/node.env"
[[ "${NODE_RANK:-}" =~ ^[0-3]$ ]] || fail 'NODE_RANK must be 0, 1, 2, or 3.'
for k in H3_ROOT IMAGE HEAD_IP NET_IFACE NETWORK_MODE PROFILE; do
  v="${!k:-}"
  [[ -n "$v" && "$v" != *CHANGE_ME* ]] || fail "Set $k."
done
[[ "$H3_ROOT" = /* && "$H3_ROOT" != / && "$H3_ROOT" != *:* ]] || fail 'H3_ROOT must be a dedicated absolute Linux directory.'
[[ -d "$H3_ROOT" ]] || fail "Create $H3_ROOT first."
[[ "$PROFILE" =~ ^(q|u-q|p|u-p)$ ]] || fail 'Unknown PROFILE.'
for p in DIST_PORT TEST_PORT API_PORT; do
  v="${!p:-}"
  [[ "$v" =~ ^[0-9]+$ ]] && (( v >= 1024 && v <= 65535 )) || fail "Invalid $p."
done
command -v docker >/dev/null || fail 'Docker is required.'
command -v ip >/dev/null || fail 'iproute2 is required on the host.'
ip link show dev "$NET_IFACE" >/dev/null 2>&1 || fail "No interface $NET_IFACE on this host."
case "$(uname -m)" in aarch64|arm64) ;; *) fail 'Run node scripts on a DGX Spark ARM64 host, not on your Mac/PC.';; esac
DOCKER_ARGS=(--rm --gpus all --network host --ipc host --ulimit memlock=-1 \
  --mount "type=bind,src=$H3_ROOT,dst=$H3_ROOT" \
  --mount "type=bind,src=$BUNDLE_ROOT,dst=/opt/h3-bundle,readonly" \
  -e "H3_ROOT=$H3_ROOT" -e "H3_PROFILE=$PROFILE" -e "H3_NETWORK_MODE=$NETWORK_MODE" -e "HF_HOME=$H3_ROOT/cache" \
  -e "NCCL_SOCKET_IFNAME==$NET_IFACE" -e "GLOO_SOCKET_IFNAME=$NET_IFACE" \
  -e NCCL_DEBUG=INFO -e OMP_NUM_THREADS=8 \
  -e SGLANG_DIFFUSION_IPC_A2A=0 -e SGLANG_CACHE_DIT_ENABLED=false)
case "$NETWORK_MODE" in
  tcp) DOCKER_ARGS+=(-e NCCL_IB_DISABLE=1);;
  rdma)
    [[ -d /dev/infiniband ]] || fail '/dev/infiniband is missing.'
    [[ -n "${NCCL_HCA:-}" && "$NCCL_HCA" != *CHANGE_ME* ]] || fail 'Set the actual NCCL_HCA for this host.'
    DOCKER_ARGS+=(--device /dev/infiniband -e NCCL_IB_DISABLE=0 -e "NCCL_IB_HCA=$NCCL_HCA");;
  *) fail 'NETWORK_MODE must be tcp or rdma.';;
esac
