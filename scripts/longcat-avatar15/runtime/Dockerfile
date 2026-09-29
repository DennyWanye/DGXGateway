FROM longcat-avatar15:ngc25.11-base

# The parent image ID must equal the verified NGC config digest before building.
USER root
RUN sed -i 's|http://ports.ubuntu.com|https://ports.ubuntu.com|g' /etc/apt/sources.list.d/ubuntu.sources && apt-get -o Acquire::Retries=1 -o Acquire::https::Timeout=30 update && apt-get install -y --no-install-recommends ffmpeg libsndfile1 && rm -rf /var/lib/apt/lists/*
RUN python -m venv --system-site-packages /opt/longcat-venv
ENV PATH=/opt/longcat-venv/bin:$PATH
ENV PIP_CONSTRAINT=/opt/longcat-core-constraints.txt
RUN python - <<'PY'
import importlib.metadata as m
from pathlib import Path
names = ['torch','torchvision','torchaudio','triton','pytorch-triton','flash-attn','numpy','sympy']
rows=[]
for name in names:
    try: rows.append(f'{name}=={m.version(name)}')
    except m.PackageNotFoundError: pass
Path('/opt/longcat-core-constraints.txt').write_text('\n'.join(rows)+'\n')
PY
COPY requirements.longcat.txt /opt/requirements.longcat.txt
RUN python -m pip install --index-url https://pypi.org/simple --dry-run --report /opt/longcat-pip-dry-run.json -r /opt/requirements.longcat.txt
RUN python -m pip install --index-url https://pypi.org/simple --report /opt/longcat-pip-install.json -r /opt/requirements.longcat.txt && python -m pip check && python -m pip freeze --all > /opt/longcat-pip-freeze.txt
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
ENV TORCH_ALLOW_TF32_CUBLAS_OVERRIDE=0
WORKDIR /workspace/src
