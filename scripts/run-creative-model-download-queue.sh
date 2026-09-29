#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

export VIDEO_QUEUE_MANIFEST="$root/manifests/creative-model-download-queue.json"
export VIDEO_QUEUE_REMOTE_ROOT=/home/operator/ai/ComfyUI/models
export VIDEO_QUEUE_REMOTE_LOG_ROOT=/home/operator/dgx-models/creative-model-download-logs
export VIDEO_QUEUE_REMOTE_WORKER=/home/operator/dgx-models/video-generation/scripts/download-video-artifact-remote.sh
export VIDEO_QUEUE_PID_FILE="$root/logs/creative-model-download-queue.pid"
export VIDEO_QUEUE_WAIT_FOR_GLOBAL_DOWNLOADS=0

exec "$root/scripts/run-video-model-download-queue.sh"
