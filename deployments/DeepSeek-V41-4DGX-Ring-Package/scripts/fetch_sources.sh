#!/usr/bin/env bash
set -euo pipefail
BASE=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$BASE/upstream"
checkout() {
  local url=$1 dir=$2 sha=$3
  if [[ ! -d "$dir/.git" ]]; then
    [[ ! -e "$dir" ]] || { echo "Refuse non-git destination: $dir" >&2; exit 1; }
    git init "$dir"
    git -C "$dir" remote add origin "$url"
  fi
  [[ $(git -C "$dir" remote get-url origin) == "$url" ]] || { echo 'Unexpected remote' >&2; exit 1; }
  [[ -z $(git -C "$dir" status --porcelain) ]] || { echo "Dirty checkout: $dir" >&2; exit 1; }
  if ! git -C "$dir" cat-file -e "$sha^{commit}" 2>/dev/null; then
    git -C "$dir" fetch --depth 1 origin "$sha"
  fi
  git -C "$dir" checkout --detach "$sha"
  [[ $(git -C "$dir" rev-parse HEAD) == "$sha" ]]
}
checkout https://github.com/yunwei37/dgx-spark-4-ring-no-switch.git "$BASE/upstream/ring" 94047b444e9fc0c062619ad338f54c8255037984
checkout https://github.com/tonyd2wild/DeepSeek-V4.1-Flash-vLLM-DGX-Spark.git "$BASE/upstream/recipe" 592540c69853a8ce9285236ebfd6e54dfc83a013
printf '%s\n' 'SOURCES_PINNED: do not run the upstream host launch/cleanup scripts.'
