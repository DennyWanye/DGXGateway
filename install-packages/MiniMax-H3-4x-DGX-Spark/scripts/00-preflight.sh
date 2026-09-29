#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname -- "$0")/common.sh"
mkdir -p "$H3_ROOT"/{models,cache,manifests,input,output,logs}
printf '\n=== HOST ===\n'
uname -a
free -h
df -h "$H3_ROOT"
ip -br addr show "$NET_IFACE"
ip route get "$HEAD_IP"
printf '\n=== IMAGE ===\n'
docker image inspect "$IMAGE" --format '{{.Id}} {{.Architecture}}'
[[ "$(docker image inspect "$IMAGE" --format '{{.Architecture}}')" = arm64 ]] || fail 'The image is not ARM64.'
printf '\n=== RUNTIME ===\n'
docker run "${DOCKER_ARGS[@]}" --entrypoint python "$IMAGE" \
  /opt/h3-bundle/scripts/probe_runtime.py
printf '\nPreflight passed. This does not establish four-node H3 compatibility.\n'
