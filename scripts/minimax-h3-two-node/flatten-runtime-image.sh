#!/usr/bin/env bash
set -euo pipefail

source_image=${1:-}
target_image=${2:-}
archive=${3:-}
container=minimax-h3-flatten-export-v1
transformer=/usr/local/lib/python3.12/dist-packages/vllm_omni/diffusion/models/minimax_h3/minimax_h3_transformer.py
abstract=/usr/local/lib/python3.12/dist-packages/vllm_omni/diffusion/executor/abstract.py

[[ -n $source_image && -n $target_image && -n $archive && $# -eq 3 ]] || {
  printf 'usage: %s SOURCE_IMAGE TARGET_IMAGE ABSOLUTE_ARCHIVE\n' "$0" >&2
  exit 2
}
[[ $archive == /* ]] || { printf 'archive path must be absolute\n' >&2; exit 2; }
[[ ${H3_ALLOW_FLATTEN_EXPORT:-0} == 1 ]] || { printf 'H3_ALLOW_FLATTEN_EXPORT=1 is required\n' >&2; exit 2; }

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  exec sudo -n env H3_ALLOW_FLATTEN_EXPORT=1 H3_EXPECTED_SOURCE_IMAGE_ID="${H3_EXPECTED_SOURCE_IMAGE_ID:-}" \
    "$0" "$source_image" "$target_image" "$archive"
fi

[[ $(hostname) == dgx-head ]] || { printf 'wrong host\n' >&2; exit 1; }
[[ ${H3_EXPECTED_SOURCE_IMAGE_ID:-} =~ ^sha256:[0-9a-f]{64}$ ]] || {
  printf 'H3_EXPECTED_SOURCE_IMAGE_ID is required\n' >&2
  exit 2
}
[[ $(docker image inspect "$source_image" --format '{{.Id}}') == "$H3_EXPECTED_SOURCE_IMAGE_ID" ]] || {
  printf 'source image ID mismatch\n' >&2
  exit 1
}
[[ ! -e $archive ]] || { printf 'refusing to overwrite archive\n' >&2; exit 1; }
if docker image inspect "$target_image" >/dev/null 2>&1; then
  printf 'refusing to overwrite target image\n' >&2
  exit 1
fi
[[ $(docker ps -aq | wc -l | tr -d '[:space:]') == 0 ]] || {
  printf 'refusing flatten operation while containers exist\n' >&2
  exit 1
}

target_created=false
cleanup() {
  local rc=$?
  docker container rm -f "$container" >/dev/null 2>&1 || true
  if [[ $rc -ne 0 && $target_created == true ]]; then
    docker image rm "$target_image" >/dev/null 2>&1 || true
  fi
  exit "$rc"
}
trap cleanup EXIT

mapfile -d '' -t changes < <(docker image inspect "$source_image" | python3 -c '
import json,sys
c=json.load(sys.stdin)[0]["Config"]
values=[]
for item in c.get("Env") or []:
    key,value=item.split("=",1)
    values.append("ENV " + key + "=" + json.dumps(value))
values.extend("LABEL " + key + "=" + json.dumps(value) for key,value in sorted((c.get("Labels") or {}).items()))
if c.get("WorkingDir"):
    values.append("WORKDIR " + c["WorkingDir"])
if c.get("Entrypoint") is not None:
    values.append("ENTRYPOINT " + json.dumps(c["Entrypoint"],separators=(",",":")))
if c.get("Cmd") is not None:
    values.append("CMD " + json.dumps(c["Cmd"],separators=(",",":")))
sys.stdout.buffer.write(b"\0".join(item.encode() for item in values) + b"\0")
')
change_args=()
for change in "${changes[@]}"; do change_args+=(--change "$change"); done

docker create --name "$container" --entrypoint /bin/true "$source_image" >/dev/null
docker export "$container" | docker import "${change_args[@]}" - "$target_image" >/dev/null
target_created=true

[[ $(docker image inspect "$target_image" --format '{{.Architecture}}') == arm64 ]]
[[ $(docker image inspect "$target_image" --format '{{json .Config.Entrypoint}}') == '["/usr/local/bin/start-minimax-h3"]' ]]
[[ $(docker image inspect "$target_image" --format '{{.Config.WorkingDir}}') == /app ]]
docker run --rm --network none --entrypoint sha256sum "$target_image" "$transformer" "$abstract"
docker run --rm --network none --entrypoint python "$target_image" -c \
  'import importlib.metadata as m; assert m.version("ray")=="2.56.1"; assert m.version("vllm")=="0.26.0"; assert m.version("vllm-omni")=="0.1.dev2381+g310b4b477"'

docker save --output "$archive" "$target_image"
printf 'FLAT_IMAGE_ID=%s\n' "$(docker image inspect "$target_image" --format '{{.Id}}')"
printf 'ARCHIVE_SHA256=%s\n' "$(sha256sum "$archive" | cut -d ' ' -f1)"
