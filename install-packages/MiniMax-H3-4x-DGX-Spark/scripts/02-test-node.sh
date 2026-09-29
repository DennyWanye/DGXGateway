#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname -- "$0")/common.sh"
# Start this script on all four hosts, with different NODE_RANK values.
exec docker run "${DOCKER_ARGS[@]}" --name h3-network-test \
  --entrypoint torchrun "$IMAGE" \
  --nnodes=4 --nproc_per_node=1 --node_rank="$NODE_RANK" \
  --master_addr="$HEAD_IP" --master_port="$TEST_PORT" \
  /opt/h3-bundle/scripts/nccl_probe.py
