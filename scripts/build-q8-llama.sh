#!/usr/bin/env bash
set -euo pipefail

root="${DGX_MODEL_ROOT:-$HOME/dgx-models/qwen38-27b}"
repo="$root/src/llama.cpp-q8-256k"
remote="https://gitcode.com/gh_mirrors/ll/llama.cpp.git"
commit="3737e41370da1830a44c663f9929a0f27591ffa6"

mkdir -p "$root/src"
if [[ ! -d "$repo/.git" ]]; then
  git clone --filter=blob:none "$remote" "$repo"
fi
git -C "$repo" remote set-url origin "$remote"
git -C "$repo" fetch --filter=blob:none origin master
git -C "$repo" cat-file -e "$commit^{commit}"
git -C "$repo" checkout --detach "$commit"

build="$repo/build-cuda"
cmake -S "$repo" -B "$build" \
  -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc \
  -DCMAKE_CUDA_ARCHITECTURES=121 \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_CUDA=ON \
  -DLLAMA_BUILD_UI=OFF \
  -DLLAMA_USE_PREBUILT_UI=OFF \
  -DLLAMA_BUILD_TESTS=OFF \
  -DLLAMA_BUILD_EXAMPLES=OFF
cmake --build "$build" --config Release -j"$(nproc)" --target llama-server llama-bench

test "$(git -C "$repo" rev-parse HEAD)" = "$commit"
"$build/bin/llama-server" --version
