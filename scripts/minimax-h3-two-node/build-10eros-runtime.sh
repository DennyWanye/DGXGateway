#!/bin/sh
set -eu

context=${1:?build context required}
image=${2:-minimax-h3-two-node:10eros-v1}
mkdir -p "$context"
cp -p "$(dirname "$0")/patch-10eros-runtime.py" "$context/patch-10eros-runtime.py"
cp -p "$(dirname "$0")/patch-10eros-schedule.py" "$context/patch-10eros-schedule.py"
cp -p "$(dirname "$0")/Dockerfile.10eros" "$context/Dockerfile"
touch -t 202409010000 "$context/patch-10eros-runtime.py" "$context/patch-10eros-schedule.py" "$context/Dockerfile"
TZ=UTC SOURCE_DATE_EPOCH=1725148800 docker buildx build --load --provenance=false \
  --output type=docker,rewrite-timestamp=true --network=none --pull=false --no-cache \
  -t "$image" "$context"
docker image inspect "$image" --format '{{.Id}}'
