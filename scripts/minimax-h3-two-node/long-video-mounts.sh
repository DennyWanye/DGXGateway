# Shared by both lifecycle helpers. H3-1 receives only the executor health fix;
# duration, memory, and cancellation overlays remain specific to H3-2.
h3_long_video_args() {
  H3_LONG_VIDEO_ARGS=()
  case "$H3_SELECTED_PROFILE" in
    heretic-ref2va-v1|heretic-ref2va-hmnsfw-v1) ;;
    *) return 0 ;;
  esac
  local directory=${1:-/etc/minimax-h3/long-video-overlay}
  if [[ ${H3_TESTING:-0} == 1 && -n ${H3_TEST_OVERLAY_DIR:-} ]]; then
    directory=$H3_TEST_OVERLAY_DIR
  fi
  python3 - "$directory" "$H3_SELECTED_PROFILE" <<'PY' || return $?
import hashlib,json,pathlib,sys
root=pathlib.Path(sys.argv[1]); manifest=json.loads((root/'manifest.json').read_text())
assert manifest['profile']=='heretic-ref2va-hmnsfw-v1'
rows=manifest['files']
if sys.argv[2]=='heretic-ref2va-v1':
 rows=[row for row in rows if row['file']=='executor.py']
 assert len(rows)==1 and rows[0]['source_sha256']=='a888b7e9a89396324068e4a1ea411ecd1ba512e156524fe9f43190b6f2b56cf0'
for row in rows:
 p=root/row['file']
 assert not p.is_symlink() and hashlib.sha256(p.read_bytes()).hexdigest()==row['sha256'], 'long-video overlay hash mismatch'
PY
  if [[ $H3_SELECTED_PROFILE == heretic-ref2va-v1 ]]; then
    H3_LONG_VIDEO_ARGS=(-v "$directory/executor.py:/opt/h3-multinode/h3_multinode/executor.py:ro")
    if [[ -f "$SCRIPT_DIR/performance-experiment.sh" ]]; then
      source "$SCRIPT_DIR/performance-experiment.sh"
      h3_performance_experiment_args || return $?
      H3_LONG_VIDEO_ARGS+=(${H3_PERFORMANCE_MOUNTS[@]+"${H3_PERFORMANCE_MOUNTS[@]}"})
    fi
    return 0
  fi
  H3_LONG_VIDEO_ARGS=(-e VLLM_OMNI_VIDEO_SYNC_TIMEOUT=0)
  H3_LONG_VIDEO_ARGS+=(-v "$directory/executor.py:/opt/h3-multinode/h3_multinode/executor.py:ro")
  H3_LONG_VIDEO_ARGS+=(-v "$directory/api_server.py:/usr/local/lib/python3.12/dist-packages/vllm_omni/entrypoints/openai/api_server.py:ro")
  H3_LONG_VIDEO_ARGS+=(-v "$directory/pipeline_minimax_h3.py:/usr/local/lib/python3.12/dist-packages/vllm_omni/diffusion/models/minimax_h3/pipeline_minimax_h3.py:ro")
  H3_LONG_VIDEO_ARGS+=(-v "$directory/vae.py:/usr/local/lib/python3.12/dist-packages/vllm_omni/diffusion/models/minimax_h3/vae.py:ro")
}
