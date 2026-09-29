#!/usr/bin/env bash
set -euo pipefail

profile=${1:?usage: build-codex-compatible-runtimes.sh gemma4|qwen38-q8-fastmtp}
model_root=${DGX_MODEL_ROOT:-/home/operator/dgx-models/qwen38-27b}
matrix_root=${DGX_MATRIX_ROOT:-/home/operator/dgx-models/model-matrix}
patch_root="$matrix_root/patches"
test_patch="$patch_root/llama-codex-namespace-test.patch"
implementation_patch="$patch_root/llama-codex-namespace-tools.patch"
history_test_patch="$patch_root/llama-codex-history-items-test.patch"
history_implementation_patch="$patch_root/llama-codex-history-items.patch"
gemma_commit=5ea87ddad22541a37053c7ba92b02ec1923617c6
fastmtp_commit=4df29be4f4c3673f428170fda944a5b19f743bb8
author_patch="$model_root/models/HauhauCS-FastMTP-llama.cpp.patch"
author_patch_sha=981285400b59dc45cf99936b6ff66d4b3aa0f1b532f85fa51418cb407e51d615

case "$profile" in
  gemma4)
    source="$model_root/src/llama.cpp"
    destination="$model_root/src/llama.cpp-codex-gemma"
    expected_commit=$gemma_commit
    apply_fastmtp=0
    ;;
  qwen38-q8-fastmtp)
    source="$model_root/src/llama.cpp-fastmtp"
    destination="$model_root/src/llama.cpp-codex-fastmtp"
    expected_commit=$fastmtp_commit
    apply_fastmtp=1
    ;;
  *)
    echo "unknown profile: $profile" >&2
    exit 2
    ;;
esac

[[ -f $test_patch && -f $implementation_patch ]]
[[ -f $history_test_patch && -f $history_implementation_patch ]]
[[ $(git -C "$source" rev-parse HEAD) == "$expected_commit" ]]
if [[ -e $destination ]]; then
  echo "destination already exists; inspect it before retrying: $destination" >&2
  exit 1
fi

git clone --no-hardlinks "$source" "$destination"
git -C "$destination" checkout --detach "$expected_commit"

if [[ $apply_fastmtp == 1 ]]; then
  [[ $(sha256sum "$author_patch" | awk '{print $1}') == "$author_patch_sha" ]]
  git -C "$destination" apply --check "$author_patch"
  git -C "$destination" apply "$author_patch"
fi

git -C "$destination" apply --check "$test_patch"
git -C "$destination" apply "$test_patch"
git -C "$destination" apply --check "$history_test_patch"
git -C "$destination" apply "$history_test_patch"

cmake -S "$destination" -B "$destination/build-codex" \
  -DGGML_CUDA=ON \
  -DCMAKE_CUDA_COMPILER=/usr/local/cuda-13.0/bin/nvcc \
  -DCMAKE_CUDA_ARCHITECTURES=121 \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLAMA_BUILD_UI=OFF \
  -DLLAMA_USE_PREBUILT_UI=OFF
cmake --build "$destination/build-codex" -j"$(nproc)" --target test-chat

set +e
(cd "$destination" && ./build-codex/bin/test-chat) >"$destination/namespace-red.log" 2>&1
red_status=$?
set -e
if [[ $red_status == 0 ]]; then
  echo "namespace RED test unexpectedly passed" >&2
  exit 1
fi

git -C "$destination" apply --check "$implementation_patch"
git -C "$destination" apply "$implementation_patch"
git -C "$destination" apply --check "$history_implementation_patch"
git -C "$destination" apply "$history_implementation_patch"
cmake --build "$destination/build-codex" -j"$(nproc)" --target test-chat llama-server
(cd "$destination" && ./build-codex/bin/test-chat)

test -x "$destination/build-codex/bin/llama-server"
printf 'CODEX_RUNTIME_BUILD_PASS profile=%s commit=%s binary_sha256=%s\n' \
  "$profile" "$expected_commit" \
  "$(sha256sum "$destination/build-codex/bin/llama-server" | awk '{print $1}')"
