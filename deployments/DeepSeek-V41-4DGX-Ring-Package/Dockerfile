# Only immutable images. No model weights and no credentials are copied.
# The DeepSeek runtime alone DOES NOT supply NCCL Mesh; copy the pinned donor libraries.
ARG MESH_IMAGE=ghcr.io/yunwei37/dgx-spark-4-ring-no-switch@sha256:de17ff1d442fe41d461bf23c7d11213ac54afe7cc52a5f42a9489d434ec4db33
ARG DEEPSEEK_IMAGE=ghcr.io/yunwei37/dgx-spark-4-ring-no-switch@sha256:2f8e2a70e73541eacf8c71a8990e907fa18fcd8b2a8d02cd0d54a7c81382a13c
FROM ${MESH_IMAGE} AS mesh
FROM ${DEEPSEEK_IMAGE}
USER root
COPY --from=mesh /opt/nccl-mesh /opt/nccl-mesh
COPY upstream/recipe/patch/ /opt/dsv41-sources/recipe/patch/
COPY upstream/recipe/LICENSE /opt/dsv41-sources/recipe/LICENSE
COPY upstream/ring/images/runtime/deepseek41/ /opt/dsv41-sources/ring/images/runtime/deepseek41/
COPY upstream/ring/LICENSE /opt/dsv41-sources/ring/LICENSE
COPY upstream/ring/THIRD_PARTY_NOTICES.md /opt/dsv41-sources/ring/THIRD_PARTY_NOTICES.md
COPY scripts/ /opt/dsv41-ring/scripts/
RUN test -f /opt/nccl-mesh/lib/libnccl.so.2.29.7 \
 && test -e /opt/nccl-mesh/lib/libnccl-net-mesh.so \
 && python3 /opt/dsv41-ring/scripts/install_patches.py \
 && python3 -m compileall -q /opt/dsv41-ring/scripts \
 && sha256sum /opt/nccl-mesh/lib/libnccl.so.2.29.7 /opt/nccl-mesh/lib/libnccl-net.so > /opt/dsv41-ring/transport.sha256
ENV LD_LIBRARY_PATH=/opt/nccl-mesh/lib:/usr/local/cuda/lib64:${LD_LIBRARY_PATH}
ENV VLLM_NCCL_SO_PATH=/opt/nccl-mesh/lib/libnccl.so.2
ENV PYTHONPATH=/opt/dsv41-ring/runtime
LABEL local.dsv41-ring.image="step-by-step-20260917" \
      local.dsv41-ring.source="94047b444e9fc0c062619ad338f54c8255037984" \
      local.dsv41-ring.recipe="592540c69853a8ce9285236ebfd6e54dfc83a013"
ENTRYPOINT ["python3", "/opt/dsv41-ring/scripts/serve.py"]
