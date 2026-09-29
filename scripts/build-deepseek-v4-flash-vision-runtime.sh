#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
context="$repo_root/containers/deepseek-v4-flash-vision-exp"
base_ref="docker.io/vllm/vllm-openai@sha256:8568b4bbc821903d93a0a9c17dd80382fdc0ba78eaa128e3eb5cb71c3bf06b79"
base_config="sha256:b12d77aecd6ca64639f55ab946f90342ba108ae1c32e4c545242722eff3b04d1"
base_local_ref="vllm/vllm-openai:vision-base-8568b4bbc821"
derived_tag="deepseek-v4-flash-vision-exp-runtime:5ab628dd-flashinfer-7a55473"

if (($#)); then
  echo "usage: $0" >&2
  exit 64
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required" >&2
  exit 69
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "jq is required" >&2
  exit 69
fi
base_json=$(docker image inspect "$base_local_ref") || {
  echo "pinned base is not loaded locally; acquisition is a separate digest-verified step" >&2
  exit 66
}
base_id=$(jq -r '.[0].Id' <<<"$base_json")
base_arch=$(jq -r '.[0].Architecture' <<<"$base_json")
base_os=$(jq -r '.[0].Os' <<<"$base_json")
if [[ "$base_id" != "$base_config" || "$base_arch" != arm64 || "$base_os" != linux ]]; then
  echo "content-addressed local base tag does not resolve to the pinned linux/arm64 config digest" >&2
  exit 65
fi

python3 "$repo_root/scripts/verify-deepseek-v4-flash-vision-runtime.py"
docker build \
  --pull=false \
  --network=none \
  --platform=linux/arm64 \
  --file "$context/Containerfile" \
  --tag "$derived_tag" \
  "$context"

derived_json=$(docker image inspect "$derived_tag")
derived_id=$(jq -r '.[0].Id' <<<"$derived_json")
labels=$(jq -c '.[0].Config.Labels' <<<"$derived_json")
jq -n \
  --arg schema deepseek-v4-flash-vision-runtime-build-v1 \
  --arg base_ref "$base_ref" \
  --arg base_id "$base_id" \
  --arg derived_tag "$derived_tag" \
  --arg derived_id "$derived_id" \
  --argjson labels "$labels" \
  '{schema:$schema,base_ref:$base_ref,base_id:$base_id,derived_tag:$derived_tag,derived_id:$derived_id,labels:$labels}'
