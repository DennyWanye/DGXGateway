# Experimental integration image. Build once on ARM64, then docker save/load it.
# This is NOT a previously GPU-tested 4xDGX Spark image.
ARG BASE_IMAGE=lmsysorg/sglang:nightly-dev-cu13-20260916-c9a8fba9
FROM ${BASE_IMAGE}
USER root
ARG SGLANG_REF=279339f113b79af84f27fd3ac92d0a13bd3f4cbd
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 HF_HUB_DISABLE_TELEMETRY=1
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates ffmpeg iproute2 rdma-core libibverbs1 ibverbs-providers \
      build-essential pkg-config \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --filter=blob:none --no-checkout https://github.com/sgl-project/sglang.git /opt/h3-sglang \
    && git -C /opt/h3-sglang checkout --detach "${SGLANG_REF}" \
    && test "$(git -C /opt/h3-sglang rev-parse HEAD)" = "${SGLANG_REF}"
# Install into the image's Python, never into the host's ComfyUI environment.
RUN python -m pip install --no-cache-dir --pre -e '/opt/h3-sglang/python[diffusion]' \
    && python -m pip freeze > /opt/h3-sglang/pip-freeze.build.txt
WORKDIR /opt/h3-sglang
