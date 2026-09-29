#!/bin/sh
set -eu

requirements_dir=$(CDPATH='' cd -- "$(dirname "$0")" && pwd)
common_args="--python-version=3.13 --only-binary=:all: --generate-hashes --torch-backend=cpu --no-header"

# shellcheck disable=SC2086
uv pip compile "$requirements_dir/upstream.in" $common_args \
    --python-platform=aarch64-manylinux_2_36 \
    --output-file="$requirements_dir/arm64.lock"
# shellcheck disable=SC2086
uv pip compile "$requirements_dir/upstream.in" $common_args \
    --python-platform=x86_64-manylinux_2_36 \
    --output-file="$requirements_dir/amd64.lock"
