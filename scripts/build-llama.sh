#!/usr/bin/env bash
set -euo pipefail

root="${DGX_MODEL_ROOT:-$HOME/dgx-models/qwen38-27b}"
repo="$root/src/llama.cpp"
commit="5ea87ddad22541a37053c7ba92b02ec1923617c6"

mkdir -p "$root/src"
if [[ ! -d "$repo/.git" ]]; then
  git clone --filter=blob:none https://github.com/ggml-org/llama.cpp.git "$repo"
fi

git -C "$repo" fetch origin "$commit"
git -C "$repo" checkout --detach "$commit"
rm -rf "$repo/build-cuda"

CUDACXX=/usr/local/cuda/bin/nvcc cmake -S "$repo" -B "$repo/build-cuda" \
  -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc \
  -DCMAKE_CUDA_ARCHITECTURES=121 \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_CUDA=ON \
  -DLLAMA_BUILD_TESTS=OFF \
  -DLLAMA_BUILD_EXAMPLES=OFF

cmake --build "$repo/build-cuda" --config Release -j"$(nproc)" \
  --target llama-server llama-cli llama-bench
