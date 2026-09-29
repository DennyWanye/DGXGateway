# Experimental ARM64 build. Pin BASE to the digest you inspected before building.
ARG BASE=nvidia/cuda:13.0.3-cudnn-devel-ubuntu24.04
FROM ${BASE}
ENV DEBIAN_FRONTEND=noninteractive PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-dev python3-venv build-essential cmake ninja-build \
    pkg-config protobuf-compiler libssl-dev libffi-dev libnuma-dev \
    git ca-certificates curl ffmpeg libsndfile1 libgl1 libglib2.0-0 \
    libibverbs1 libibverbs-dev ibverbs-providers librdmacm1 librdmacm-dev \
    rdma-core perftest iproute2 ethtool libopenmpi-dev \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/h3env
ENV PATH=/opt/h3env/bin:/root/.cargo/bin:${PATH}
# Keep the downloaded installer in the image for the build audit.
RUN curl --proto '=https' --tlsv1.2 -fsSL https://sh.rustup.rs -o /tmp/rustup-init.sh \
    && sh /tmp/rustup-init.sh -y --profile minimal --default-toolchain stable \
    && rustc --version && cargo --version
COPY sglang-src /opt/sglang-src
WORKDIR /opt/sglang-src
RUN test "$(git rev-parse HEAD)" = e970453b433e1467e22f6d925f270563417540b5
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install --pre --extra-index-url https://download.pytorch.org/whl/cu130 \
       -e '/opt/sglang-src/python[diffusion]' \
    && python -m pip check \
    && python -m pip freeze --all > /opt/h3-build-freeze.txt
ENV PYTHONUNBUFFERED=1
WORKDIR /kit
CMD ["sglang", "serve", "--help"]
