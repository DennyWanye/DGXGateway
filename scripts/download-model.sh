#!/usr/bin/env bash
set -euo pipefail

root="${DGX_MODEL_ROOT:-$HOME/dgx-models/qwen38-27b}"
models="$root/models"
repo="https://huggingface.co/HauhauCS/Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-MTP-GGUF/resolve/main"
quant="${DGX_MODEL_QUANT:-Q8_K_P}"
case "$quant" in
  Q8_K_P|Q6_K_P|Q5_K_P|Q4_K_P) ;;
  *) echo "unsupported quant: $quant" >&2; exit 2 ;;
esac
model="Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-${quant}.gguf"

mkdir -p "$models"
cd "$models"
for artifact in \
  HauhauCS-RELEASE-MANIFEST.json \
  HauhauCS-RELEASE-MANIFEST.json.sig \
  HauhauCS-FastMTP-Ed25519-PUBLIC.pem
do
  curl -L --fail --retry 5 -o "$artifact" "$repo/$artifact"
done

openssl pkeyutl -verify -rawin -pubin \
  -inkey HauhauCS-FastMTP-Ed25519-PUBLIC.pem \
  -in HauhauCS-RELEASE-MANIFEST.json \
  -sigfile HauhauCS-RELEASE-MANIFEST.json.sig

aria2c \
  --allow-overwrite=true \
  --auto-file-renaming=false \
  --continue=true \
  --file-allocation=none \
  --max-connection-per-server=8 \
  --max-download-limit=0 \
  --min-split-size=1M \
  --retry-wait=2 \
  --split=8 \
  --summary-interval=30 \
  --dir="$models" \
  --out="$model" \
  "$repo/$model"
"$root/scripts/verify-model.py" --root "$models" --quant "$quant"
