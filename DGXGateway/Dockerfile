FROM python:3.13-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY DGXGateway /app/DGXGateway
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
USER 65532:65532
EXPOSE 18082
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
  CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18082/api/v1/snapshot', timeout=8).read(1)"]
ENTRYPOINT ["python3", "-m", "DGXGateway.dgx_gateway"]
