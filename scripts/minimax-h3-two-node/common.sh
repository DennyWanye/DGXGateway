#!/usr/bin/env bash

h3_die() {
  printf 'ERROR: %s\n' "$*" >&2
  return 1
}

h3_config_keys() {
  cat <<'EOF'
H3_EXPECTED_USER
H3_HEAD_HOSTNAME
H3_HEAD_MANAGEMENT_IP
H3_HEAD_HOSTKEY_SHA256
H3_WORKER_HOSTNAME
H3_WORKER_MANAGEMENT_IP
H3_WORKER_HOSTKEY_SHA256
H3_HEAD_FABRIC_A
H3_WORKER_FABRIC_A
H3_HEAD_FABRIC_B
H3_WORKER_FABRIC_B
H3_NCCL_SOCKET_IFNAME
H3_NCCL_IB_HCA
H3_NCCL_IB_GID_INDEX
H3_EXTERNAL_BIND
H3_EXTERNAL_PORT
H3_INTERNAL_API_BIND
H3_INTERNAL_API_PORT
H3_RAY_PORT
H3_RENDEZVOUS_PORT
H3_MODEL_ROOT
H3_FL2VA_PATH
H3_MODEL_PATH
H3_PROFILES_PATH
H3_SELECTED_PROFILE
H3_RUNTIME_LOCK
H3_MODEL_LOCK
H3_RUNTIME_IMAGE
H3_RUNTIME_OVERLAY
H3_OUTPUT_DIR
H3_RANK_EVIDENCE_DIR
H3_RAY_AUTH_TOKEN_PATH
H3_WORKER_SSH_IDENTITY
H3_WORKER_SSH_KNOWN_HOSTS
H3_WORKER_SSH
H3_LLM_COEXISTENCE_MODE
H3_ALLOW_RUNTIME_ACTIONS
H3_MAC_LOCK_ATTESTATION_PATH
H3_RESTORE_LLM_ON_STOP
EOF
}

h3_apply_profile() {
  local selected="$1" profiles_path="${H3_PROFILES_PATH:-}"
  [[ "$selected" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || h3_die "invalid H3 profile name" || return
  [[ -n "$profiles_path" && -f "$profiles_path" && ! -L "$profiles_path" ]] ||
    h3_die "H3 profile catalog is missing or unsafe" || return
  if [[ ${H3_TESTING:-0} != 1 ]]; then
    [[ $(stat -c '%u' "$profiles_path") == 0 ]] || h3_die "H3 profile catalog must be root-owned" || return
    [[ $(stat -c '%a' "$profiles_path") == 600 ]] || h3_die "H3 profile catalog must have mode 0600" || return
  fi

  local profile_output
  profile_output=$(python3 - "$profiles_path" "$selected" <<'PY'
import hashlib,json,os,re,sys
path,name=sys.argv[1:]
try:
    catalog=json.load(open(path,encoding="utf-8"))
    schema=catalog.get("schema_version")
    profiles=catalog.get("profiles",{})
    if schema == 1 and set(profiles) != {"heretic-hmnsfw-v1","heretic-ref2va-v1"}:
        raise ValueError("legacy catalog allowlist mismatch")
    if schema not in {1,2} or not isinstance(profiles,dict) or name not in profiles:
        raise ValueError("catalog schema/profile selection mismatch")
    value=catalog["profiles"][name]
except Exception as exc:
    raise SystemExit(f"invalid H3 profile catalog: {exc}")
required={"task_type","model_root","model_path","model_lock","runtime_lock","runtime_image","runtime_overlay","expected_image_id","profile_tree_sha256"}
v2={"visibility","qualification","supported_tasks","text_encoder","transformer","modifier","curve","runtime_overlay_sha256"}
expected=required if schema == 1 else required|v2
if set(value) != expected: raise SystemExit("profile fields are incomplete or unknown")
if value["task_type"] not in {"fl2va","ref2va"}: raise SystemExit("invalid profile task type")
for key in ("model_root","model_path","model_lock","runtime_lock","runtime_overlay"):
    item=value[key]
    if not isinstance(item,str) or not os.path.isabs(item) or any(ch in item for ch in "\n\r\t\0"):
        raise SystemExit(f"invalid absolute profile path: {key}")
try:
    if os.path.commonpath([value["model_root"],value["model_path"]]) != os.path.normpath(value["model_root"]):
        raise ValueError
except ValueError:
    raise SystemExit("profile model path escapes model root")
if not isinstance(value["runtime_image"],str) or not re.fullmatch(r"[a-z0-9][a-z0-9._/-]*:[a-z0-9][a-z0-9._-]*",value["runtime_image"]):
    raise SystemExit("invalid runtime image reference")
if not re.fullmatch(r"sha256:[0-9a-f]{64}",str(value["expected_image_id"])):
    raise SystemExit("profile runtime image is not sealed")
if not re.fullmatch(r"[0-9a-f]{64}",str(value["profile_tree_sha256"])):
    raise SystemExit("profile model tree is not sealed")
contract=dict(value); contract["name"]=name
canonical=json.dumps(contract,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()
values=[name,value["task_type"],value["model_root"],value["model_path"],value["model_lock"],value["runtime_lock"],value["runtime_image"],value["runtime_overlay"],value["expected_image_id"],value["profile_tree_sha256"],hashlib.sha256(canonical).hexdigest()]
print("\x1f".join(values))
PY
  ) || return
  IFS=$'\x1f' read -r H3_SELECTED_PROFILE H3_TASK_TYPE H3_MODEL_ROOT H3_MODEL_PATH \
    H3_MODEL_LOCK H3_RUNTIME_LOCK H3_RUNTIME_IMAGE H3_RUNTIME_OVERLAY H3_EXPECTED_IMAGE_ID \
    H3_EXPECTED_PROFILE_TREE_SHA256 H3_PROFILE_CONTRACT_SHA256 <<<"$profile_output"
  [[ -n "$H3_PROFILE_CONTRACT_SHA256" ]] || h3_die "profile resolver returned an incomplete contract" || return
  python3 - "$H3_SELECTED_PROFILE" "$H3_TASK_TYPE" "$H3_MODEL_ROOT" "$H3_MODEL_PATH" \
    "$H3_MODEL_LOCK" "$H3_RUNTIME_LOCK" "$H3_RUNTIME_OVERLAY" "$H3_EXPECTED_PROFILE_TREE_SHA256" \
    "$H3_EXPECTED_IMAGE_ID" <<'PY' || return
import json,os,sys
name,task,root,model_path,model_lock,runtime_lock,overlay,expected,expected_image=sys.argv[1:]
for path,label in ((root,"model root"),(model_path,"model path")):
    if os.path.islink(path) or not os.path.exists(path): raise SystemExit(f"profile {label} is missing or unsafe")
for path,label in ((model_lock,"model lock"),(runtime_lock,"runtime lock"),(overlay,"runtime overlay")):
    if os.path.islink(path) or not os.path.isfile(path): raise SystemExit(f"profile {label} is missing or unsafe")
try: runtime=json.load(open(runtime_lock,encoding="utf-8"))
except Exception as exc: raise SystemExit(f"runtime lock is invalid: {exc}")
matching=[name for name in ("production","candidate") if isinstance(runtime.get(name),dict) and runtime[name].get("final_image_id")==expected_image]
if len(matching) != 1:
    raise SystemExit("runtime lock does not seal the selected profile image")
if task == "ref2va":
    profile_path=os.path.join(root,"profile.json")
    if os.path.islink(profile_path) or not os.path.isfile(profile_path): raise SystemExit("Ref2VA profile.json is missing or unsafe")
    try: document=json.load(open(profile_path,encoding="utf-8"))
    except Exception as exc: raise SystemExit(f"Ref2VA profile.json is invalid: {exc}")
    if document.get("schema_version") != 1 or document.get("profile_id") != name or document.get("task_type") != task or document.get("profile_tree_sha256") != expected:
        raise SystemExit("Ref2VA profile.json does not match the selected contract")
else:
    try: document=json.load(open(model_lock,encoding="utf-8"))
    except Exception as exc: raise SystemExit(f"FL2VA model lock is invalid: {exc}")
    if document.get("tree_sha256") != expected: raise SystemExit("FL2VA model lock does not match the selected contract")
PY
  export H3_SELECTED_PROFILE H3_TASK_TYPE H3_MODEL_ROOT H3_MODEL_PATH H3_MODEL_LOCK \
    H3_RUNTIME_LOCK H3_RUNTIME_IMAGE H3_RUNTIME_OVERLAY H3_EXPECTED_IMAGE_ID \
    H3_EXPECTED_PROFILE_TREE_SHA256 H3_PROFILE_CONTRACT_SHA256
}

h3_is_config_key() {
  local needle="$1"
  h3_config_keys | grep -Fxq -- "$needle"
}

h3_is_ipv4() {
  python3 - "$1" <<'PY'
import ipaddress,sys
try:
    value=ipaddress.ip_address(sys.argv[1])
except ValueError:
    raise SystemExit(1)
raise SystemExit(0 if value.version == 4 else 1)
PY
}

h3_load_config() {
  local file="$1" line key value
  [[ -f "$file" ]] || h3_die "config file not found: $file" || return

  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" == *=* ]] || h3_die "invalid config line" || return
    key="${line%%=*}"
    value="${line#*=}"
    [[ "$key" =~ ^[A-Z][A-Z0-9_]*$ ]] || h3_die "invalid config key: $key" || return
    h3_is_config_key "$key" || h3_die "unknown config key: $key" || return
    [[ -n "$value" ]] || h3_die "empty config value: $key" || return
    [[ "$value" != *[$' \t\n\r`$;&|<>']* && "$value" != *"'"* && "$value" != *'"'* ]] ||
      h3_die "unsafe characters in $key" || return
    printf -v "$key" '%s' "$value"
    export "${key?}"
  done <"$file"

  while IFS= read -r key; do
    [[ -n "${!key:-}" ]] || h3_die "missing config key: $key" || return
  done < <(h3_config_keys)

  [[ "$H3_EXPECTED_USER" == operator ]] || h3_die "H3_EXPECTED_USER must be operator" || return
  [[ "$H3_HEAD_HOSTNAME" == dgx-head ]] || h3_die "unexpected head hostname" || return
  [[ "$H3_WORKER_HOSTNAME" == dgx-worker ]] || h3_die "unexpected worker hostname" || return
  [[ "$H3_HEAD_MANAGEMENT_IP" == 192.0.2.30 ]] || h3_die "unexpected head management IP" || return
  [[ "$H3_WORKER_MANAGEMENT_IP" == 192.0.2.31 ]] || h3_die "unexpected worker management IP" || return
  [[ "$H3_HEAD_FABRIC_A" == 192.0.2.33 && "$H3_WORKER_FABRIC_A" == 192.0.2.34 ]] ||
    h3_die "unexpected fabric A endpoints" || return
  [[ "$H3_HEAD_FABRIC_B" == 192.0.2.35 && "$H3_WORKER_FABRIC_B" == 192.0.2.36 ]] ||
    h3_die "unexpected fabric B endpoints" || return
  for value in "$H3_HEAD_MANAGEMENT_IP" "$H3_WORKER_MANAGEMENT_IP" \
    "$H3_HEAD_FABRIC_A" "$H3_WORKER_FABRIC_A" "$H3_HEAD_FABRIC_B" "$H3_WORKER_FABRIC_B" \
    "$H3_EXTERNAL_BIND" "$H3_INTERNAL_API_BIND"; do
    h3_is_ipv4 "$value" || h3_die "invalid IPv4 address: $value" || return
  done
  [[ "$H3_EXTERNAL_BIND" == "$H3_HEAD_MANAGEMENT_IP" && "$H3_EXTERNAL_BIND" != 0.0.0.0 ]] ||
    h3_die "unsafe bind: H3_EXTERNAL_BIND must be the head management IP" || return
  [[ "$H3_INTERNAL_API_BIND" == "$H3_HEAD_FABRIC_A" && "$H3_INTERNAL_API_BIND" != 0.0.0.0 ]] ||
    h3_die "unsafe internal API bind" || return
  [[ "$H3_EXTERNAL_PORT" == 18081 ]] || h3_die "H3_EXTERNAL_PORT must be 18081" || return
  case "$H3_EXTERNAL_PORT" in 8080|8000|8188|11434) h3_die "reserved external port" || return ;; esac
  for key in H3_INTERNAL_API_PORT H3_RAY_PORT H3_RENDEZVOUS_PORT; do
    value="${!key}"
    [[ "$value" =~ ^[0-9]+$ && "$value" -ge 1 && "$value" -le 65535 ]] ||
      h3_die "invalid port: $key" || return
  done
  [[ "$H3_NCCL_IB_GID_INDEX" =~ ^[0-9]+$ ]] || h3_die "invalid GID index" || return
  for value in "$H3_NCCL_SOCKET_IFNAME" "$H3_NCCL_IB_HCA"; do
    [[ "$value" != *'*'* && "$value" != *'?'* && "$value" != *'['* && "$value" != *']'* ]] ||
      h3_die "wildcard fabric selector is forbidden" || return
    [[ "$value" != *wl* && "$value" != *docker* && "$value" != *tun* && "$value" != *vpn* ]] ||
      h3_die "non-fabric selector is forbidden" || return
  done
  case "$H3_LLM_COEXISTENCE_MODE" in
    exclusive|concurrent) ;;
    *) h3_die "H3_LLM_COEXISTENCE_MODE must be exclusive or concurrent" || return ;;
  esac
  [[ "$H3_ALLOW_RUNTIME_ACTIONS" == 0 || "$H3_ALLOW_RUNTIME_ACTIONS" == 1 ]] ||
    h3_die "H3_ALLOW_RUNTIME_ACTIONS must be 0 or 1" || return
  [[ "$H3_RESTORE_LLM_ON_STOP" == 0 || "$H3_RESTORE_LLM_ON_STOP" == 1 ]] ||
    h3_die "H3_RESTORE_LLM_ON_STOP must be 0 or 1" || return
  for key in H3_OUTPUT_DIR H3_RANK_EVIDENCE_DIR H3_RAY_AUTH_TOKEN_PATH H3_MAC_LOCK_ATTESTATION_PATH; do
    value="${!key}"
    [[ "$value" == /* ]] || h3_die "$key must be an absolute path" || return
  done
}

h3_assert_host_role() {
  local role="$1" observed_hostname="$2" observed_user="$3" observed_ip="$4" observed_key="$5"
  local expected_hostname expected_ip expected_key
  case "$role" in
    head)
      expected_hostname="$H3_HEAD_HOSTNAME"
      expected_ip="$H3_HEAD_MANAGEMENT_IP"
      expected_key="$H3_HEAD_HOSTKEY_SHA256"
      ;;
    worker)
      expected_hostname="$H3_WORKER_HOSTNAME"
      expected_ip="$H3_WORKER_MANAGEMENT_IP"
      expected_key="$H3_WORKER_HOSTKEY_SHA256"
      ;;
    *) h3_die "unknown role: $role" || return ;;
  esac
  [[ "$observed_hostname" == "$expected_hostname" ]] || h3_die "hostname mismatch for $role" || return
  [[ "$observed_user" == "$H3_EXPECTED_USER" ]] || h3_die "user mismatch for $role" || return
  [[ "$observed_ip" == "$expected_ip" ]] || h3_die "management IP mismatch for $role" || return
  [[ "$observed_key" == "$expected_key" ]] || h3_die "host key mismatch for $role" || return
}

h3_redact() {
  if (($#)); then
    printf '%s\n' "$*"
  else
    cat
  fi | sed -E \
      -e 's/(Authorization:[[:space:]]*Bearer[[:space:]]+)[^[:space:]]+/\1[REDACTED]/Ig' \
      -e 's/((TOKEN|PASSWORD|SECRET|SIGNED_URL)[A-Z0-9_]*=)[^[:space:]]+/\1[REDACTED]/Ig'
}

h3_sha256_file() {
  python3 - "$1" <<'PY'
import hashlib,sys
h=hashlib.sha256()
with open(sys.argv[1],"rb") as stream:
    for chunk in iter(lambda: stream.read(1024*1024), b""):
        h.update(chunk)
print(h.hexdigest())
PY
}
