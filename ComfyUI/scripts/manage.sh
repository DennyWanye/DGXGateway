#!/usr/bin/env bash
set -euo pipefail

bundle_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
compose_file="$bundle_root/compose.yaml"
project_name=dgx-comfyui-h3
secret_file="$bundle_root/secrets/h3_gateway.token"
control_secret_file="$bundle_root/secrets/h3_profile_control.token"
control_pid_file="$bundle_root/data/control/profile-controller.pid"
control_log_file="$bundle_root/data/control/profile-controller.log"
control_launchd_label=com.dgx.spark.h3-profile-controller
repo_root=$(cd "$bundle_root/.." && pwd)
deployment_lock_root=/Users/operator/.codex/locks/dgx-spark-two-node
workflow_directory="$bundle_root/data/user/default/workflows"
input_directory="$bundle_root/data/input"
workflow_sources=(
  "$bundle_root/workflows/Qwen-2511-版本选择与批量（推荐）.json"
  "$bundle_root/workflows/Qwen-2511-FP8-Huihui编码器-单图.json"
  "$bundle_root/workflows/Qwen-2511-FP8-标准编码器-单图.json"
  "$bundle_root/workflows/H3-1-heretic-ref2va-v1-参考图视频（自动释放）.json"
  "$bundle_root/workflows/工具-MCP调用与状态查询.json"
  "$bundle_root/workflows/H3-3-10eros-fl2va-v1-文字生成视频.json"
  "$bundle_root/workflows/H3-2-heretic-ref2va-hmnsfw-v1-参考图视频.json"
  "$bundle_root/workflows/H3-3-10eros-fl2va-v1-首帧图视频.json"
  "$bundle_root/workflows/H3-0-三套配置切换与状态.json"
)
reference_source="$bundle_root/assets/ref2va-character-yellow-raincoat.png"
reference_audio_name=ref2va-smoke-reference.wav
target=operator@192.0.2.30
ssh_options=(-o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=yes)

usage() {
  cat <<'EOF'
Usage: scripts/manage.sh bootstrap|start|stop|status|logs|doctor [options]

  bootstrap       securely copy the existing H3 gateway token from dgx-head
  start           start only the local production ComfyUI Compose project
  stop            stop only the local ComfyUI project; never removes volumes
  status          show local ComfyUI container status
  logs            show the last 200 local ComfyUI log lines
  doctor          perform local and read-only H3/DeepSeek checks

doctor accepts --skip-secret for non-generating diagnostics.
EOF
}

secret_mode() {
  if stat -f '%Lp' "$1" >/dev/null 2>&1; then
    stat -f '%Lp' "$1"
  else
    stat -c '%a' "$1"
  fi
}

require_secret() {
  if [[ -L "$secret_file" || ! -f "$secret_file" ]]; then
    echo "Missing regular secret file; run: scripts/manage.sh bootstrap" >&2
    exit 4
  fi
  if [[ $(secret_mode "$secret_file") != 600 ]]; then
    echo "Secret file must have mode 0600" >&2
    exit 4
  fi
}

ensure_control_secret() {
  if [[ ! -e "$control_secret_file" ]]; then
    mkdir -p "$(dirname "$control_secret_file")"
    python3 - "$control_secret_file" <<'PY'
import os,secrets,sys
fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,"w") as stream: stream.write(secrets.token_urlsafe(32)+"\n")
PY
  fi
  [[ -f "$control_secret_file" && ! -L "$control_secret_file" && $(secret_mode "$control_secret_file") == 600 ]] || {
    echo "Profile control secret must be a regular mode-0600 file" >&2
    exit 4
  }
}

start_profile_controller() {
  if [[ -n ${FAKE_COMMAND_LOG:-} ]]; then return; fi
  local queue_url
  queue_url="http://$(effective_bind_address):8188/queue"
  mkdir -p "$(dirname "$control_pid_file")"
  if [[ $(uname -s) == Darwin ]]; then
    local domain="gui/$(id -u)" python_bin
    if launchctl print "$domain/$control_launchd_label" >/dev/null 2>&1; then
      return
    fi
    python_bin=$(command -v python3)
    launchctl submit -l "$control_launchd_label" \
      -o "$control_log_file" -e "$control_log_file" -- \
      "$python_bin" "$bundle_root/scripts/h3-profile-controller.py" \
      --repo-root "$repo_root" --lock-root "$deployment_lock_root" \
      --token-file "$control_secret_file" --queue-url "$queue_url"
    return
  fi
  if [[ -f "$control_pid_file" ]] && kill -0 "$(cat "$control_pid_file")" 2>/dev/null; then
    return
  fi
  nohup python3 "$bundle_root/scripts/h3-profile-controller.py" \
    --repo-root "$repo_root" --lock-root "$deployment_lock_root" \
    --token-file "$control_secret_file" --queue-url "$queue_url" \
    >>"$control_log_file" 2>&1 &
  printf '%s\n' "$!" >"$control_pid_file"
}

stop_profile_controller() {
  if [[ -n ${FAKE_COMMAND_LOG:-} ]]; then return; fi
  if [[ $(uname -s) == Darwin ]]; then
    launchctl remove "$control_launchd_label" 2>/dev/null || true
    return
  fi
  if [[ -f "$control_pid_file" ]]; then
    local pid
    pid=$(cat "$control_pid_file")
    kill "$pid" 2>/dev/null || true
    rm -f "$control_pid_file"
  fi
}

install_default_workflow() {
  mkdir -p "$workflow_directory"
  local source target
  for source in "${workflow_sources[@]}"; do
    [[ -f "$source" && ! -L "$source" ]] || {
      echo "Bundled default workflow is unavailable: $(basename "$source")" >&2
      exit 4
    }
    target="$workflow_directory/$(basename "$source")"
    if [[ ! -e "$target" && ! -L "$target" ]]; then
      cp -p -n "$source" "$target"
    fi
  done
}

install_default_reference() {
  [[ -f "$reference_source" && ! -L "$reference_source" ]] || {
    echo "Bundled Ref2VA reference is unavailable" >&2
    exit 4
  }
  mkdir -p "$input_directory"
  local target="$input_directory/$(basename "$reference_source")"
  if [[ ! -e "$target" && ! -L "$target" ]]; then
    cp -p -n "$reference_source" "$target"
  fi
}

install_default_reference_audio() {
  local target="$input_directory/$reference_audio_name"
  [[ ! -L "$target" ]] || { echo "Ref2VA reference audio target is unsafe" >&2; exit 4; }
  [[ -e "$target" ]] && return
  python3 - "$target" <<'PY'
import math,os,struct,sys,wave
path=sys.argv[1]; rate=24000; count=rate*4
fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o644); os.close(fd)
try:
    with wave.open(path,"wb") as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(rate)
        frames=[]
        for index in range(count):
            t=index/rate
            envelope=min(1.0,t*8.0,(4.0-t)*8.0)*(0.72+0.28*math.sin(2*math.pi*2.3*t)**2)
            sample=envelope*(0.55*math.sin(2*math.pi*180*t)+0.25*math.sin(2*math.pi*360*t)+0.12*math.sin(2*math.pi*540*t))
            frames.append(struct.pack("<h",max(-32767,min(32767,round(sample*12000)))))
        output.writeframes(b"".join(frames))
except BaseException:
    os.unlink(path)
    raise
PY
}

compose() {
  docker compose --project-name "$project_name" --file "$compose_file" "$@"
}

effective_bind_address() {
  local value=${COMFYUI_BIND_ADDRESS:-}
  if [[ -z $value && -f "$bundle_root/.env" && ! -L "$bundle_root/.env" ]]; then
    value=$(awk -F= '$1 == "COMFYUI_BIND_ADDRESS" {print substr($0, index($0, "=") + 1)}' "$bundle_root/.env")
  fi
  printf '%s\n' "${value:-127.0.0.1}"
}

validate_bind_address() {
  local value=$1
  if ! python3 - "$value" >/dev/null 2>&1 <<'PY'
import ipaddress
import sys

raw = sys.argv[1]
address = ipaddress.IPv4Address(raw)
if str(address) != raw:
    raise SystemExit(1)
if address.is_unspecified or address.is_multicast or address.is_link_local or address.is_reserved:
    raise SystemExit(1)
if address.is_loopback and address != ipaddress.IPv4Address("127.0.0.1"):
    raise SystemExit(1)
PY
  then
    echo "Invalid COMFYUI_BIND_ADDRESS: expected one assigned IPv4 address" >&2
    return 2
  fi
  if [[ $value != 127.0.0.1 ]] && ! ifconfig | awk -v target="$value" \
      '$1 == "inet" && $2 == target {found=1} END {exit(found ? 0 : 1)}'; then
    echo "Invalid COMFYUI_BIND_ADDRESS: address is not assigned to this Mac" >&2
    return 2
  fi
}

doctor() {
  local skip_secret=false
  if [[ ${1:-} == --skip-secret ]]; then
    skip_secret=true
    shift
  fi
  if (($#)); then
    echo "Unknown doctor option: $1" >&2
    exit 2
  fi
  command -v docker >/dev/null
  command -v ssh >/dev/null
  if [[ $skip_secret == false ]]; then
    require_secret
    ensure_control_secret
  fi

  local observation
  observation=$(ssh "${ssh_options[@]}" "$target" \
    "hostname; whoami; systemctl is-active minimax-h3-two-node.service || true; systemctl is-active minimax-h3-gateway.service || true; systemctl is-active deepseek-v4-flash-dspark.service || true; systemctl is-active deepseek-v4-flash-gateway.service || true; sudo -n /usr/local/sbin/minimax-h3-control status; ss -H -ltn '( sport = :18081 or sport = :11434 )' || true")
  local lines=()
  while IFS= read -r line; do
    lines+=("$line")
  done <<<"$observation"
  if [[ ${lines[0]:-} != dgx-head || ${lines[1]:-} != operator ]]; then
    echo "Head role check failed" >&2
    exit 5
  fi
  # The lifecycle unit is deliberately disabled and may be inactive when the
  # explicit Mac controller started H3 directly.  Runtime truth is the sealed
  # exact-two READY state plus the active gateway, not the oneshot unit state.
  if [[ ${lines[3]:-} != active ]]; then
    echo "H3 gateway is not active; ComfyUI generation is not ready" >&2
    exit 6
  fi
  if [[ ${lines[4]:-} != inactive || ${lines[5]:-} != inactive ]]; then
    echo "DeepSeek is not stopped; refusing the H3-only operating state" >&2
    exit 7
  fi
  if ! python3 -c 'import json,sys; value=json.load(sys.stdin); raise SystemExit(0 if value.get("state")=="READY" and value.get("healthy") is True and value.get("exact_two") is True else 1)' <<<"${lines[6]:-}"; then
    echo "H3 exact-two runtime is not READY; ComfyUI generation is not ready" >&2
    exit 6
  fi
  if ! grep -q '18081' <<<"$observation"; then
    echo "H3 listener 18081 was not observed" >&2
    exit 8
  fi
  if tail -n +8 <<<"$observation" | grep -q '11434'; then
    echo "DeepSeek/legacy listener 11434 is present; expected stopped" >&2
    exit 7
  fi
  echo "Doctor PASS: H3 active on 18081; DeepSeek inactive; checks were read-only"
}

action=${1:-}
shift || true
case "$action" in
  bootstrap)
    (($# == 0)) || { usage >&2; exit 2; }
    exec "$bundle_root/scripts/bootstrap-secret.sh" "$secret_file"
    ;;
  start)
    (($# == 0)) || { usage >&2; exit 2; }
    bind_address=$(effective_bind_address)
    validate_bind_address "$bind_address"
    export COMFYUI_BIND_ADDRESS=$bind_address
    require_secret
    doctor
    mkdir -p "$bundle_root/data/input" "$bundle_root/data/output" \
      "$bundle_root/data/temp" "$bundle_root/data/user/default/workflows"
    install_default_workflow
    install_default_reference
    install_default_reference_audio
    compose up -d
    start_profile_controller
    echo "ComfyUI requested at http://$bind_address:8188"
    ;;
  stop)
    (($# == 0)) || { usage >&2; exit 2; }
    compose down
    stop_profile_controller
    ;;
  status)
    (($# == 0)) || { usage >&2; exit 2; }
    compose ps
    if [[ $(uname -s) == Darwin ]]; then
      if launchctl print "gui/$(id -u)/$control_launchd_label" 2>/dev/null | grep -q 'state = running'; then
        echo "H3 profile controller: running"
      else
        echo "H3 profile controller: stopped"
      fi
    elif [[ -f "$control_pid_file" ]] && kill -0 "$(cat "$control_pid_file")" 2>/dev/null; then
      echo "H3 profile controller: running"
    else
      echo "H3 profile controller: stopped"
    fi
    ;;
  logs)
    (($# == 0)) || { usage >&2; exit 2; }
    compose logs --tail 200
    ;;
  doctor)
    doctor "$@"
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
