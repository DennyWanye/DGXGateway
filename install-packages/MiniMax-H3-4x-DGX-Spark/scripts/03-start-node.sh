#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname -- "$0")/common.sh"
# This is a source-based TP4 experiment, not a certified four-Spark recipe.
python3 "$BUNDLE_ROOT/scripts/check_models.py" --root "$H3_ROOT" --profile "$PROFILE"
ARGS=(serve --model-path "$H3_ROOT/models/MiniMax-H3" \
  --model-variant fl2va --served-model-name "h3-fl2va-$PROFILE" \
  --num-gpus 4 --nnodes 4 --node-rank "$NODE_RANK" \
  --dist-init-addr "$HEAD_IP:$DIST_PORT" \
  --tp-size 4 --sp-degree 1 --ulysses-degree 1 --ring-degree 1 \
  --encoder-parallel replicate --performance-mode speed \
  --attention-backend torch_sdpa --enable-torch-compile false \
  --warmup-mode off --host "$API_BIND" --port "$API_PORT")
if [[ "$PROFILE" = u-q || "$PROFILE" = u-p ]]; then
  ARGS+=(--component-paths.text_encoder "$H3_ROOT/models/H3-Heretic-Encoder")
fi
if [[ "$PROFILE" = p || "$PROFILE" = u-p ]]; then
  ARGS+=(--lora-path "$H3_ROOT/models/H3-Turbo-v4" \
    --lora-weight-name minimax_h3_turbo_v4_step600_ema.safetensors \
    --lora-nickname h3-turbo-v4 --lora-scale 1.0 --lora-merge-mode auto)
fi
printf 'rank=%s profile=%s network=%s\n' "$NODE_RANK" "$PROFILE" "$NETWORK_MODE"
printf '%q ' sglang "${ARGS[@]}"; printf '\n'
# No model download at serving time; missing assets must be fixed deliberately.
exec docker run "${DOCKER_ARGS[@]}" --name h3-infer \
  --mount "type=bind,src=$H3_ROOT/models,dst=$H3_ROOT/models,readonly" \
  -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 \
  --entrypoint sglang "$IMAGE" "${ARGS[@]}"
