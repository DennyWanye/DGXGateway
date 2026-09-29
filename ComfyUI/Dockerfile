# syntax=docker/dockerfile:1.9

ARG PYTHON_BASE=python:3.13-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e
ARG COMFYUI_COMMIT=169fcf35a2fc163fec31338b816503ddac0d3fcf

FROM ${PYTHON_BASE} AS source
ARG COMFYUI_COMMIT
RUN test "$COMFYUI_COMMIT" = "169fcf35a2fc163fec31338b816503ddac0d3fcf" \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && git init /opt/ComfyUI \
    && git -C /opt/ComfyUI remote add origin https://github.com/Comfy-Org/ComfyUI.git \
    && git -C /opt/ComfyUI fetch --depth=1 origin "$COMFYUI_COMMIT" \
    && git -C /opt/ComfyUI checkout --detach FETCH_HEAD \
    && test "$(git -C /opt/ComfyUI rev-parse HEAD)" = "$COMFYUI_COMMIT" \
    && printf '%s\n' "$COMFYUI_COMMIT" > /opt/ComfyUI/.source-commit

FROM ${PYTHON_BASE} AS dependencies
ARG TARGETARCH
COPY requirements/ /tmp/requirements/
RUN case "$TARGETARCH" in \
        arm64) lock=/tmp/requirements/arm64.lock ;; \
        amd64) lock=/tmp/requirements/amd64.lock ;; \
        *) printf 'unsupported TARGETARCH: %s\n' "$TARGETARCH" >&2; exit 64 ;; \
    esac \
    && python3 -m pip install --no-cache-dir --require-hashes \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        -r "$lock"

FROM ${PYTHON_BASE} AS runtime-base
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=dependencies /usr/local /usr/local
COPY --from=source /opt/ComfyUI /opt/ComfyUI
COPY container/ /opt/dgx/container/
COPY contracts/comfyui-source.json contracts/capability-policy.json /opt/dgx/contracts/
COPY custom_nodes/dgx_remote_h3/ /opt/ComfyUI/custom_nodes/dgx_remote_h3/
RUN test "$(cat /opt/ComfyUI/.source-commit)" = "169fcf35a2fc163fec31338b816503ddac0d3fcf" \
    && printf '%s\n' 'dgx-comfyui:x:65532:' >> /etc/group \
    && printf '%s\n' 'dgx-comfyui:x:65532:65532:DGX ComfyUI:/data/user:/usr/sbin/nologin' >> /etc/passwd \
    && chmod 0555 /opt/dgx/container/entrypoint.sh /opt/dgx/container/steady-state.sh \
    && mkdir -p /data/input /data/output /data/user /data/temp \
    && chown -R 65532:65532 /data
ENV HOME=/data/user \
    TMPDIR=/data/temp \
    XDG_CACHE_HOME=/data/temp/cache \
    TORCHINDUCTOR_CACHE_DIR=/data/temp/torchinductor
WORKDIR /opt/ComfyUI
ENTRYPOINT ["/opt/dgx/container/entrypoint.sh"]
EXPOSE 8188
LABEL org.opencontainers.image.source="https://github.com/Comfy-Org/ComfyUI" \
      org.opencontainers.image.revision="169fcf35a2fc163fec31338b816503ddac0d3fcf" \
      org.opencontainers.image.licenses="GPL-3.0-only" \
      io.dgx.h3.cpu-only="true"

FROM runtime-base AS production
COPY contracts/production-runtime.json /opt/dgx/contracts/runtime.json
LABEL io.dgx.h3.runtime-schema="dgx-h3-production-v1"

FROM runtime-base AS test
COPY contracts/test-runtime.json /opt/dgx/contracts/runtime.json
COPY contracts/test-deployment.py /opt/ComfyUI/custom_nodes/dgx_remote_h3/deployment.py
LABEL io.dgx.h3.runtime-schema="dgx-h3-test-v1"
