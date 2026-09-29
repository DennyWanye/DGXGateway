#!/usr/bin/env bash
# Private remote executor. Every mutation requires a fresh controller receipt.
set -euo pipefail

PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

readonly PROJECT='deepseek-v4-flash-dspark'
readonly ENV_FILE='/etc/deepseek-v4-flash-dspark.env'
readonly COMPOSE_FILE='/opt/deepseek-v4-flash-dspark/docker-compose.dspark.yml'
readonly SOURCE_ROOT='/home/operator/dgx-models/deepseek-v4-flash-dspark/source'
readonly COMPOSE_SERVICE='vllm-dspark'
readonly STATE_ROOT='/home/operator/.local/state/deepseek-v4-flash-dspark'
readonly FALLBACK_STATE='/var/lib/deepseek-v4-flash-fallback'
readonly GATEWAY_RUN='/run/deepseek-v4-flash-dspark'

mode=''
role=''
verb=''
receipt_file=''
transaction_id_arg=''
trust_action_name=''
public_key_arg=''
host_public_key_arg=''
fingerprint_arg=''
while (($#)); do
  case "$1" in
    --inspect-state) mode='inspect'; shift ;;
    --receipt-stdin) mode='receipt-stdin'; shift ;;
    --receipt-secret-stdin) mode='receipt-secret-stdin'; shift ;;
    --receipt-file)
      if [[ $mode != trust-action ]]; then
        mode='receipt-file'
      fi
      receipt_file=${2:?}
      shift 2
      ;;
    --capture-fallback) mode='capture-fallback'; shift ;;
    --trust-action) mode='trust-action'; trust_action_name=${2:?}; shift 2 ;;
    --role) role=${2:?}; shift 2 ;;
    --verb) verb=${2:?}; shift 2 ;;
    --transaction-id) transaction_id_arg=${2:?}; shift 2 ;;
    --public-key) public_key_arg=${2:?}; shift 2 ;;
    --host-public-key) host_public_key_arg=${2:?}; shift 2 ;;
    --fingerprint) fingerprint_arg=${2:?}; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done

case "$role" in
  head) expected_hostname='dgx-head' ;;
  worker) expected_hostname='dgx-worker' ;;
  *) echo 'role must be head or worker' >&2; exit 64 ;;
esac
[[ $(hostname) == "$expected_hostname" ]] || {
  echo "hostname/role mismatch" >&2
  exit 75
}

run_trust_action() {
  [[ "$receipt_file" == /proc/self/fd/3 ]] || {
    echo 'trust action requires the consumed running receipt on FD3' >&2
    return 75
  }
  python3 - "$trust_action_name" "$role" "$verb" "$expected_hostname" \
    "$transaction_id_arg" "$public_key_arg" "$host_public_key_arg" \
    "$fingerprint_arg" "$receipt_file" <<'PY'
import base64, hashlib, json, os, pathlib, re, socket, stat, subprocess, sys, time

action, role, verb, expected_host, transaction, public_key, host_public_key, fingerprint, receipt_file = sys.argv[1:]
project = "deepseek-v4-flash-dspark"
key_path = pathlib.Path("/home/operator/.ssh/deepseek-v4-flash-worker")
pub_path = pathlib.Path(f"{key_path}.pub")
known_path = pathlib.Path("/home/operator/.ssh/deepseek-v4-flash-worker-known_hosts")
authorized_path = pathlib.Path("/home/operator/.ssh/authorized_keys")
state_dir = pathlib.Path("/home/operator/.local/state/deepseek-v4-flash-dspark/artifacts")
marker_path = state_dir / "trust-head-key.json"
allowed = {
    "ensure-head-worker-key": "head",
    "install-worker-authorized-key": "worker",
    "install-head-worker-known-hosts": "head",
}
if action != verb or allowed.get(action) != role or socket.gethostname() != expected_host:
    raise SystemExit("trust action target binding mismatch")
if not re.fullmatch(r"[0-9a-f]{32}", transaction):
    raise SystemExit("invalid trust transaction")
running = json.loads(pathlib.Path(receipt_file).read_text(encoding="utf-8"))
receipt = running.get("receipt", {})
if running.get("state") != "running" or (
    receipt.get("transaction_id"), receipt.get("host"), receipt.get("role"),
    receipt.get("verb"), receipt.get("resource")
) != (transaction, expected_host, role, verb, f"{project}:trust:{verb}"):
    raise SystemExit("trust running receipt binding mismatch")
generation = receipt.get("lease_generation", "")
if not re.fullmatch(r"[0-9a-f]{32}", generation):
    raise SystemExit("trust running receipt omitted lease generation")

def key_parts(value):
    fields = value.strip().split()
    if len(fields) not in {2, 3} or fields[0] != "ssh-ed25519":
        raise SystemExit("only one normalized ssh-ed25519 public key is accepted")
    try:
        blob = base64.b64decode(fields[1], validate=True)
    except Exception as exc:
        raise SystemExit("invalid OpenSSH public key encoding") from exc
    derived = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return " ".join(fields), derived

def read_regular(path):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise SystemExit(f"unsafe trust path: {path}")
    return path.read_bytes() if path.exists() else None

def atomic(path, data, mode):
    temporary = path.with_name(f".{path.name}.deepseek-trust.{os.getpid()}.{time.time_ns()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        os.write(fd, data); os.fsync(fd); os.fchmod(fd, mode)
        os.fchown(fd, os.getuid(), os.getgid())
    finally:
        os.close(fd)
    os.replace(temporary, path)

state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(state_dir, 0o700)
preimage = None
postimage = None
result_fingerprint = fingerprint
if action == "ensure-head-worker-key":
    private = read_regular(key_path); public = read_regular(pub_path); marker = read_regular(marker_path)
    if (private is None) != (public is None):
        raise SystemExit("partial dedicated key preimage is rejected")
    if private is None:
        if marker is not None:
            raise SystemExit("dedicated key marker exists without its key")
        subprocess.run([
            "/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-a", "64", "-N", "",
            "-C", "deepseek-v4-flash-dspark", "-f", str(key_path),
        ], check=True, timeout=30)
        os.chmod(key_path, 0o600); os.chmod(pub_path, 0o644)
        public = pub_path.read_bytes()
        created = True
    else:
        created = False
        if marker is None:
            raise SystemExit("unmarked pre-existing dedicated key is rejected")
        if stat.S_IMODE(key_path.stat().st_mode) != 0o600 or stat.S_IMODE(pub_path.stat().st_mode) != 0o644:
            raise SystemExit("dedicated key modes are not exact")
    try:
        normalized, result_fingerprint = key_parts(public.decode())
        derived = subprocess.run(
            ["/usr/bin/ssh-keygen", "-y", "-f", str(key_path)],
            text=True, capture_output=True, check=True, timeout=30,
        ).stdout.strip()
        if key_parts(derived)[0].split()[:2] != normalized.split()[:2]:
            raise SystemExit("dedicated public/private key mismatch")
        expected_marker = {
            "schema_version": 1, "kind": "deepseek-head-worker-key",
            "path": str(key_path), "fingerprint": result_fingerprint,
        }
        if marker is not None and json.loads(marker) != expected_marker:
            raise SystemExit("existing dedicated key marker fingerprint mismatch")
        atomic(marker_path, (json.dumps(expected_marker, sort_keys=True) + "\n").encode(), 0o600)
        postimage = hashlib.sha256(public).hexdigest()
    except BaseException:
        if created:
            key_path.unlink(missing_ok=True); pub_path.unlink(missing_ok=True); marker_path.unlink(missing_ok=True)
        raise
elif action == "install-worker-authorized-key":
    normalized, derived = key_parts(public_key)
    if derived != fingerprint:
        raise SystemExit("worker authorized key fingerprint mismatch")
    old = read_regular(authorized_path)
    preimage = None if old is None else base64.b64encode(old).decode()
    marker = "# deepseek-v4-flash-dspark"
    block = f'{marker} {fingerprint}\nrestrict {normalized}\n{marker} end\n'.encode()
    if old is not None and marker.encode() in old:
        if block not in old:
            raise SystemExit("different dedicated authorized-key marker exists")
        new = old
    else:
        new = (old or b"").rstrip(b"\n") + (b"\n" if old else b"") + block
    try:
        atomic(authorized_path, new, 0o600)
        postimage = hashlib.sha256(new).hexdigest()
    except BaseException:
        if old is None: authorized_path.unlink(missing_ok=True)
        else: atomic(authorized_path, old, 0o600)
        raise
elif action == "install-head-worker-known-hosts":
    normalized, derived = key_parts(host_public_key)
    if derived != fingerprint:
        raise SystemExit("worker host key fingerprint mismatch")
    key_type, key_blob = normalized.split()[:2]
    legacy = (f"192.0.2.31 {key_type} {key_blob}\n"
              f"192.0.2.34 {key_type} {key_blob}\n").encode()
    new = (legacy + f"192.0.2.36 {key_type} {key_blob}\n".encode())
    old = read_regular(known_path)
    preimage = None if old is None else base64.b64encode(old).decode()
    if old is not None and old not in {legacy, new}:
        raise SystemExit("different dedicated known_hosts file exists")
    try:
        atomic(known_path, new, 0o644)
        postimage = hashlib.sha256(new).hexdigest()
    except BaseException:
        if old is None: known_path.unlink(missing_ok=True)
        else: atomic(known_path, old, 0o644)
        raise
else:
    raise SystemExit("unsupported trust action")

action_receipt = {
    "schema_version": 1, "kind": "deepseek-trust-action-receipt", "terminal": "PASS",
    "transaction_id": transaction, "lease_generation": generation, "host": expected_host,
    "role": role, "verb": verb, "fingerprint": result_fingerprint,
    "preimage_base64": preimage, "postimage_sha256": postimage,
}
receipt_path = state_dir / f"trust-{verb}.receipt.json"
atomic(receipt_path, (json.dumps(action_receipt, sort_keys=True) + "\n").encode(), 0o600)
print(json.dumps({"terminal": "PASS", "fingerprint": result_fingerprint}, sort_keys=True))
PY
}

if [[ "$mode" == trust-action ]]; then
  run_trust_action
  exit $?
fi

mkdir_state() {
  install -d -m 0700 "$STATE_ROOT" "$STATE_ROOT/receipts"
}

capture_fallback() {
  [[ "$role" == head ]] || { echo 'fallback preimage is head-only' >&2; return 64; }
  python3 - <<'PY'
import json, subprocess, urllib.request

def user_state(kind, unit):
    result = subprocess.run(
        ["systemctl", "--user", kind, unit], text=True, capture_output=True, timeout=10
    )
    text = (result.stdout or result.stderr).strip()
    if kind == "is-enabled":
        if text not in {"enabled", "disabled"}:
            raise SystemExit(f"unexpected enablement for {unit}: {text}")
        return text == "enabled"
    if text not in {"active", "inactive", "failed"}:
        raise SystemExit(f"unexpected active state for {unit}: {text}")
    return text == "active"

with urllib.request.urlopen("http://192.0.2.30:11434/api/version", timeout=5) as response:
    version = json.load(response).get("version")
with urllib.request.urlopen("http://192.0.2.30:11434/api/tags", timeout=10) as response:
    models = {item.get("name") for item in json.load(response).get("models", [])}
model = "qwen38-q4-256k:latest"
if version != "0.32.15" or model not in models:
    raise SystemExit("legacy Ollama version/model preimage does not match the frozen oracle")
units = {}
for unit in ("ollama.service", "dgx-codex-production.service"):
    units[unit] = {
        "enabled": user_state("is-enabled", unit),
        "active": user_state("is-active", unit),
    }
if units["ollama.service"] != {"enabled": True, "active": True}:
    raise SystemExit("legacy Ollama is not enabled and active")
print(json.dumps({
    "schema_version": 1,
    "state": "FALLBACK_ARMED",
    "head_hostname": "dgx-head",
    "old_model": model,
    "old_ollama_version": version,
    "legacy_units": units,
}, sort_keys=True))
PY
}

state_json() {
  local observed
  case "$verb" in
    start|stop|reboot)
      observed=''
      while IFS= read -r container_id; do
        [[ -z "$container_id" ]] && continue
        observed+=$(sudo -n docker inspect --format \
          '{{.Id}}|{{.Image}}|{{.State.Status}}|{{.State.Running}}|{{.State.Restarting}}|{{.State.ExitCode}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}|{{index .Config.Labels "com.docker.compose.config-hash"}}|{{index .Config.Labels "com.docker.compose.service"}}' \
          "$container_id")
        observed+=$'\n'
      done < <(sudo -n docker ps -a \
        --filter "label=com.docker.compose.project=$PROJECT" \
        --format '{{.ID}}' | sort)
      ;;
    finalize-model-lock|copy-model|acquire-images|save-runtime-archive|copy-runtime-archive|load-runtime-archive|apply-runtime-patch|install-integration-artifacts|finalize-deployment-lock|copy-patched-source|record-license-acceptance|record-node-identity|install-service-assets|qualify-fabric-consumer|ensure-head-worker-key|install-worker-authorized-key|install-head-worker-known-hosts)
      observed=$(stat -c '%n %F %s %Y' \
        /home/operator/dgx-models/deepseek-v4-flash-ablit-anchored \
        /home/operator/.local/state/deepseek-v4-flash-dspark/artifacts \
        /home/operator/dgx-models/deepseek-v4-flash-dspark/source \
        /home/operator/dgx-models/deepseek-v4-flash-dspark/artifacts/dspark-vllm-gx10-a83948492cf13df4.tar \
        /opt/deepseek-v4-flash-dspark/manifests/deepseek-v4-flash-dspark.lock.json \
        2>/dev/null || true)
      ;;
    cutover|production-activate-gateway|production-deactivate-gateway|production-fence-gateway|stage-gateway|stop-staging-gateway)
      observed=$(sudo -n systemctl show deepseek-v4-flash-gateway.service \
        deepseek-v4-flash-gateway-staging.service \
        --property=Id,ActiveState,UnitFileState --value)
      ;;
    rollback|arm-fallback|refresh-fallback|quiesce-legacy|disarm-fallback|cancel-fallback)
      # Rollback authority must remain local and usable when Docker/worker fail.
      observed=$(sudo -n systemctl show deepseek-v4-flash-fallback.service \
        deepseek-v4-flash-fallback.timer \
        --property=Id,ActiveState,UnitFileState --value)
      observed+=$(sudo -n sha256sum \
        "$FALLBACK_STATE/armed.json" "$FALLBACK_STATE/running.json" \
        "$FALLBACK_STATE/recovery-succeeded.json" 2>/dev/null || true)
      ;;
    *) echo 'unsupported inspection verb' >&2; return 64 ;;
  esac
  python3 - "$role" "$verb" "$observed" <<'PY'
import hashlib, json, sys
role, verb, raw = sys.argv[1:]
canonical = json.dumps(
    {"project": "deepseek-v4-flash-dspark", "role": role, "verb": verb, "observed": raw},
    sort_keys=True,
    separators=(",", ":"),
)
print(json.dumps({
    "role": role,
    "verb": verb,
    "state": "observed",
    "prior_state_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
}, sort_keys=True))
PY
}

validate_runtime_argv() {
  local container_id=$1 runtime_path runtime_args
  runtime_path=$(sudo -n docker inspect --format '{{json .Path}}' "$container_id")
  runtime_args=$(sudo -n docker inspect --format '{{json .Args}}' "$container_id")
  python3 - "$role" "$runtime_path" "$runtime_args" <<'PY'
import json, re, sys

role = sys.argv[1]
path = json.loads(sys.argv[2])
args = json.loads(sys.argv[3])
if not isinstance(path, str) or not isinstance(args, list) or not all(
    isinstance(value, str) for value in args
):
    raise SystemExit("invalid container runtime argv")
argv = [path, *args]
shell_script = None
if path in {"/bin/bash", "bash"} and args[:1] == ["-lc"] and len(args) == 2:
    shell_script = args[1]

def has_option(name, value=None):
    if shell_script is not None:
        option = re.escape(name)
        if value is None:
            return re.search(rf"(?<!\S){option}(?=\s|$)", shell_script) is not None
        expected = re.escape(value)
        return re.search(
            rf"(?<!\S){option}(?:=|\s+){expected}(?=\s|$)", shell_script
        ) is not None
    if value is None:
        return name in argv
    return f"{name}={value}" in argv or any(
        item == name and index + 1 < len(argv) and argv[index + 1] == value
        for index, item in enumerate(argv)
    )

rank = "1" if role == "worker" else "0"
if not has_option("--node-rank", rank):
    raise SystemExit(f"{role} runtime argv does not pin --node-rank {rank}")
if role == "worker":
    if not has_option("--headless"):
        raise SystemExit("worker runtime argv is not headless")
else:
    if has_option("--headless"):
        raise SystemExit("head runtime argv must not be headless")
    if not has_option("--host", "127.0.0.1") or not has_option("--port", "18888"):
        raise SystemExit("head runtime argv is not pinned to 127.0.0.1:18888")
PY
}

case "$verb" in
  start|stop|reboot) ;;
  finalize-model-lock|copy-model|acquire-images|save-runtime-archive|copy-runtime-archive|apply-runtime-patch|install-integration-artifacts|finalize-deployment-lock|copy-patched-source|record-license-acceptance|qualify-fabric-consumer|ensure-head-worker-key|install-head-worker-known-hosts)
    [[ "$role" == head ]] || { echo 'head-only artifact verb' >&2; exit 64; }
    ;;
  load-runtime-archive|install-worker-authorized-key)
    [[ "$role" == worker ]] || { echo 'worker-only artifact verb' >&2; exit 64; }
    ;;
  record-node-identity) ;;
  install-service-assets) ;;
  cutover|production-activate-gateway|production-deactivate-gateway|production-fence-gateway|rollback|arm-fallback|refresh-fallback|quiesce-legacy|disarm-fallback|cancel-fallback|stage-gateway|stop-staging-gateway)
    [[ "$role" == head ]] || { echo 'head-only verb' >&2; exit 64; }
    ;;
  *) echo 'unsupported verb' >&2; exit 64 ;;
esac
if [[ "$mode" == capture-fallback ]]; then
  capture_fallback
  exit 0
fi
if [[ "$mode" == inspect ]]; then
  state_json
  exit 0
fi

[[ "$mode" == receipt-stdin || "$mode" == receipt-secret-stdin || "$mode" == receipt-file ]] || {
  echo 'a receipt source is required' >&2
  exit 64
}

receipt_tmp=$(mktemp)
secret_tmp=''
action_stdout=''
action_stderr=''
cleanup() {
  rm -f "$receipt_tmp"
  [[ -z "$action_stdout" ]] || rm -f "$action_stdout"
  [[ -z "$action_stderr" ]] || rm -f "$action_stderr"
  [[ -z "$secret_tmp" ]] || rm -f "$secret_tmp"
}
trap cleanup EXIT
chmod 0600 "$receipt_tmp"
if [[ "$mode" == receipt-stdin ]]; then
  cat >"$receipt_tmp"
elif [[ "$mode" == receipt-secret-stdin ]]; then
  [[ "$role" == head && "$verb" == install-service-assets ]] || {
    echo 'secret framing is restricted to head service asset install' >&2
    exit 75
  }
  IFS= read -r receipt_size
  [[ "$receipt_size" =~ ^[1-9][0-9]{1,5}$ ]] || { echo 'invalid receipt frame length' >&2; exit 75; }
  ((receipt_size <= 65536)) || { echo 'receipt frame is too large' >&2; exit 75; }
  dd if=/dev/stdin of="$receipt_tmp" bs=1 count="$receipt_size" status=none
  secret_tmp=$(mktemp)
  chmod 0600 "$secret_tmp"
  cat >"$secret_tmp"
  python3 - "$secret_tmp" <<'PY'
import pathlib, re, sys
value = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", value):
    raise SystemExit("invalid gateway secret frame")
PY
else
  [[ "$receipt_file" == /run/deepseek-v4-flash-dspark/*.json ]] || {
    echo 'systemd receipt must be under the fixed /run path' >&2
    exit 75
  }
  cp -- "$receipt_file" "$receipt_tmp"
fi
(( $(wc -c <"$receipt_tmp") <= 65536 )) || { echo 'receipt is too large' >&2; exit 75; }

mkdir_state
exec 9>"$STATE_ROOT/lifecycle.lock"
flock -w 10 -x 9 || { echo 'remote lifecycle mutex timeout' >&2; exit 75; }

prior=$(state_json)
prior_sha=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["prior_state_sha256"])' <<<"$prior")

validation=$(python3 - \
  "$receipt_tmp" "$role" "$verb" "$expected_hostname" "$prior_sha" \
  "$STATE_ROOT/receipts" "$ENV_FILE" "$COMPOSE_FILE" "$PROJECT" <<'PY'
import datetime, json, os, sys
from pathlib import Path

receipt_path, role, verb, hostname, prior_sha, store, env_file, compose_file, project = sys.argv[1:]
receipt = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
transaction_id = receipt.get("transaction_id", "")
fallback_lock = "/run/lock/deepseek-v4-flash-fallback.lifecycle.lock"
recovery = "/usr/local/libexec/recover-deepseek-v4-flash"
prepare = "/usr/local/libexec/prepare-deepseek-v4-flash-dspark.sh"
model = "/home/operator/dgx-models/deepseek-v4-flash-ablit-anchored"
state = "/home/operator/.local/state/deepseek-v4-flash-dspark/artifacts"
source = "/home/operator/dgx-models/deepseek-v4-flash-dspark/source"
archive = "/home/operator/dgx-models/deepseek-v4-flash-dspark/artifacts/dspark-vllm-gx10-a83948492cf13df4.tar"
archive_receipt = archive + ".receipt.json"
inputs = "/opt/deepseek-v4-flash-dspark"
fd = "/proc/self/fd/3"
if verb == "start":
    argv = ["sudo", "-n", "docker", "compose", "--env-file", env_file,
            "--file", compose_file, "--project-directory", source,
            "--project-name", project,
            "up", "--detach", "--no-deps", "vllm-dspark"]
elif verb == "stop":
    argv = ["sudo", "-n", "docker", "compose", "--env-file", env_file,
            "--file", compose_file, "--project-directory", source,
            "--project-name", project,
            "down", "--remove-orphans"]
elif verb in {"cutover", "production-activate-gateway"}:
    argv = ["sudo", "-n", recovery, "gateway-start", "--transaction-id", transaction_id]
elif verb == "production-fence-gateway":
    supplied = receipt.get("argv", [])
    if len(supplied) != 8 or supplied[-2] != "--container-id":
        raise SystemExit("gateway fence omitted exact container ID")
    cid = supplied[-1]
    if len(cid) != 64 or any(c not in "0123456789abcdef" for c in cid):
        raise SystemExit("invalid gateway fence container ID")
    argv = ["sudo", "-n", recovery, "gateway-fence", "--transaction-id", transaction_id,
            "--container-id", cid]
elif verb == "production-deactivate-gateway":
    argv = ["sudo", "-n", recovery, "gateway-stop", "--transaction-id", transaction_id]
elif verb == "rollback":
    argv = ["sudo", "-n", "systemctl", "start", "deepseek-v4-flash-fallback.service"]
elif verb in {"arm-fallback", "refresh-fallback", "quiesce-legacy", "disarm-fallback", "cancel-fallback"}:
    command = verb.removesuffix("-fallback").replace("quiesce-legacy", "quiesce")
    argv = ["sudo", "-n", "flock", "-x", fallback_lock, recovery, command]
    if verb == "arm-fallback":
        if len(receipt.get("argv", [])) < 2 or receipt["argv"][-2] != "--config-json":
            raise SystemExit("arm receipt omitted exact fallback config")
        try:
            config = json.loads(receipt["argv"][-1])
        except (TypeError, json.JSONDecodeError):
            raise SystemExit("arm receipt fallback config is not JSON")
        expected_keys = {"schema_version", "state", "transaction_id", "head_hostname", "old_model", "old_ollama_version", "legacy_units"}
        if set(config) != expected_keys or config.get("transaction_id") != transaction_id:
            raise SystemExit("arm receipt fallback config binding mismatch")
        encoded = json.dumps(config, sort_keys=True, separators=(",", ":"))
        argv += ["--config-json", encoded]
    else:
        argv += ["--transaction-id", transaction_id]
elif verb == "stage-gateway":
    argv = ["sudo", "-n", "systemctl", "start", "deepseek-v4-flash-gateway-staging.service"]
elif verb == "stop-staging-gateway":
    argv = ["sudo", "-n", "systemctl", "stop", "deepseek-v4-flash-gateway-staging.service"]
elif verb == "reboot":
    argv = ["sudo", "-n", "systemd-run",
            f"--unit=deepseek-v4-flash-reboot-{role}-{transaction_id}",
            "--on-active=2s", "/usr/bin/systemctl", "reboot"]
elif verb in {"ensure-head-worker-key", "install-worker-authorized-key", "install-head-worker-known-hosts"}:
    argv = ["/usr/local/libexec/run-deepseek-v4-flash-dspark.sh", "--trust-action", verb,
            "--role", role, "--verb", verb, "--transaction-id", transaction_id]
    if verb == "install-worker-authorized-key":
        argv += ["--public-key", receipt["argv"][receipt["argv"].index("--public-key") + 1],
                 "--fingerprint", receipt["argv"][receipt["argv"].index("--fingerprint") + 1]]
    elif verb == "install-head-worker-known-hosts":
        argv += ["--host-public-key", receipt["argv"][receipt["argv"].index("--host-public-key") + 1],
                 "--fingerprint", receipt["argv"][receipt["argv"].index("--fingerprint") + 1]]
    argv += ["--receipt-file", fd]
elif verb == "record-node-identity":
    argv = [prepare, verb, "--role", role, "--lock", f"{state}/model.lock.json", "--model-root", model,
            "--image-journal", f"{state}/image-acquisition.journal.json", "--archive-receipt", archive_receipt,
            "--output", f"{state}/{role}.identity.json", "--receipt-file", fd]
elif verb != "install-service-assets":
    artifact_argv = {
        "finalize-model-lock": [prepare, verb, "--model-root", model, "--download-manifest", f"{inputs}/manifests/deepseek-v4-flash-ablit-anchored-download.json", "--lock-template", f"{inputs}/manifests/deepseek-v4-flash-dspark.lock.json", "--official-anchor-inventory", f"{inputs}/manifests/deepseek-v4-flash-official-anchor-inventory.json", "--official-metadata", f"{inputs}/manifests/deepseek-v4-flash-official-metadata.json", "--output", f"{state}/model.lock.json", "--role", "head", "--receipt-file", fd],
        "copy-model": [prepare, verb, "--download-manifest", f"{inputs}/manifests/deepseek-v4-flash-ablit-anchored-download.json", "--lock", f"{state}/model.lock.json", "--source-root", model, "--role", "head", "--receipt-file", fd],
        "acquire-images": [prepare, verb, "--role", "head", "--lock", f"{state}/model.lock.json", "--journal", f"{state}/image-acquisition.journal.json", "--rx-counter", "/sys/class/net/dgx-vpn/statistics/rx_bytes", "--poll-seconds", "0.25", "--receipt-file", fd],
        "save-runtime-archive": [prepare, verb, "--role", "head", "--lock", f"{state}/model.lock.json", "--journal", f"{state}/image-acquisition.journal.json", "--archive", archive, "--receipt", archive_receipt, "--receipt-file", fd],
        "copy-runtime-archive": [prepare, verb, "--lock", f"{state}/model.lock.json", "--archive", archive, "--receipt", archive_receipt, "--journal", f"{state}/image-acquisition.journal.json", "--role", "head", "--receipt-file", fd],
        "load-runtime-archive": [prepare, verb, "--role", "worker", "--lock", f"{state}/model.lock.json", "--archive", archive, "--receipt", archive_receipt, "--receipt-file", fd],
        "apply-runtime-patch": [prepare, verb, "--lock", f"{inputs}/manifests/deepseek-v4-flash-dspark.lock.json", "--source-root", source, "--patch", f"{inputs}/patches/deepseek-v4-flash-dspark-local-runtime.patch", "--source-receipt", f"{state}/source.receipt.json", "--role", "head", "--receipt-file", fd],
        "install-integration-artifacts": [prepare, verb, "--integration-receipt", f"{state}/integration.receipt.json", "--role", "head", "--receipt-file", fd],
        "finalize-deployment-lock": [prepare, verb, "--bootstrap-lock", f"{inputs}/manifests/deepseek-v4-flash-bootstrap-input.lock.json", "--model-lock", f"{state}/model.lock.json", "--source-receipt", f"{state}/source.receipt.json", "--license-receipt", f"{state}/license.receipt.json", "--integration-receipt", f"{state}/integration.receipt.json", "--image-journal", f"{state}/image-acquisition.journal.json", "--runtime-archive", archive, "--archive-receipt", archive_receipt, "--head-identity-receipt", f"{state}/head.identity.json", "--worker-identity-receipt", f"{state}/worker.identity.json", "--output", f"{state}/deployment.lock.json", "--role", "head", "--receipt-file", fd],
        "copy-patched-source": [prepare, verb, "--role", "head", "--lock", f"{state}/model.lock.json", "--source-root", source, "--source-receipt", f"{state}/source.receipt.json", "--receipt-file", fd],
        "record-license-acceptance": [prepare, verb, "--role", "head", "--lock", f"{state}/model.lock.json", "--operator", "operator", "--approval-basis-id", "AC-DS-RESPONSIBLE-USE-20260830-USER-CONFIRMED", "--approval-basis", "User explicitly confirmed the selected DeepSeek V4 Flash model and replacement deployment in the controlling Codex task.", "--authority-commit", "52ccc1adc05922d203614dc263d91dd6dea2076d", "--output", f"{state}/license.receipt.json", "--receipt-file", fd],
        "qualify-fabric-consumer": [prepare, verb, "--role", "head", "--fabric-lock", f"{inputs}/manifests/dgx-spark-fabric-qualification.lock.json", "--provenance-addendum", f"{inputs}/plans/deepseek-v4-flash-dspark-2node/verification/20260829T1951/h3-lock-provenance-addendum-v1.json", "--deployment-lock", f"{state}/deployment.lock.json", "--output", f"{state}/fabric-consumer.receipt.json", "--receipt-file", fd],
    }
    argv = artifact_argv[verb]
else:
    argv = [prepare, verb, "--role", role, "--final-lock", f"{state}/deployment.lock.json",
            "--source-root", source, "--asset-receipt", f"{state}/service-assets-{role}.receipt.json",
            "--receipt-file", fd]
    if role == "head":
        argv += ["--secret-fd", "/proc/self/fd/4"]
if verb in {"start", "stop"}:
    resource = project
elif verb in {"arm-fallback", "refresh-fallback", "quiesce-legacy", "disarm-fallback", "cancel-fallback"}:
    resource = "/var/lib/deepseek-v4-flash-fallback"
elif verb in {"stage-gateway", "stop-staging-gateway"}:
    resource = "deepseek-v4-flash-gateway-staging.service"
elif verb in {"cutover", "production-activate-gateway", "production-deactivate-gateway", "production-fence-gateway"}:
    resource = "deepseek-v4-flash-gateway.service"
elif verb == "rollback":
    resource = "deepseek-v4-flash-fallback.service"
elif verb in {"finalize-model-lock", "copy-model", "acquire-images", "save-runtime-archive", "copy-runtime-archive", "load-runtime-archive", "apply-runtime-patch", "install-integration-artifacts", "finalize-deployment-lock", "copy-patched-source", "record-license-acceptance", "record-node-identity", "qualify-fabric-consumer"}:
    resource = f"deepseek-v4-flash-dspark:{verb}"
elif verb == "install-service-assets":
    resource = f"deepseek-v4-flash-dspark:install-service-assets:{role}"
elif verb in {"ensure-head-worker-key", "install-worker-authorized-key", "install-head-worker-known-hosts"}:
    resource = f"deepseek-v4-flash-dspark:trust:{verb}"
else:
    resource = f"{hostname}:reboot"

def parse_time(value):
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))

required = {
    "schema", "transaction_id", "host", "role", "verb", "resource", "argv",
    "prior_state_sha256", "lease_generation", "nonce", "issued_at", "not_after",
}
if required - receipt.keys():
    raise SystemExit("receipt missing required binding")
if any(not isinstance(receipt[key], str) or not receipt[key] for key in (
    "schema", "transaction_id", "host", "role", "verb", "resource",
    "prior_state_sha256", "lease_generation", "nonce", "issued_at", "not_after",
)):
    raise SystemExit("receipt contains an invalid string binding")
if not isinstance(receipt["argv"], list) or not all(
    isinstance(value, str) and value for value in receipt["argv"]
):
    raise SystemExit("receipt argv must be a non-empty string array")
if receipt["schema"] != "deepseek-dspark-receipt-v1":
    raise SystemExit("wrong receipt schema")
transaction_id = receipt["transaction_id"]
if len(transaction_id) != 32 or any(
    character not in "0123456789abcdef" for character in transaction_id
):
    raise SystemExit("invalid transaction ID")
if (receipt["host"], receipt["role"], receipt["verb"], receipt["resource"]) != (
    hostname, role, verb, resource
):
    raise SystemExit("receipt target binding mismatch")
if receipt["argv"] != argv or receipt["prior_state_sha256"] != prior_sha:
    raise SystemExit("receipt argv or prior-state binding mismatch")
now = datetime.datetime.now(datetime.timezone.utc)
issued_at = parse_time(receipt["issued_at"])
not_after = parse_time(receipt["not_after"])
if now < issued_at - datetime.timedelta(seconds=5):
    raise SystemExit("receipt issued in the future")
if not datetime.timedelta(0) < not_after - issued_at <= datetime.timedelta(seconds=300):
    raise SystemExit("receipt lifetime exceeds the v1 bound")
if now >= not_after:
    raise SystemExit("receipt expired")
nonce = receipt["nonce"]
if len(nonce) != 32 or any(character not in "0123456789abcdef" for character in nonce):
    raise SystemExit("invalid receipt nonce")
prior_digest = receipt["prior_state_sha256"]
if len(prior_digest) != 64 or any(
    character not in "0123456789abcdef" for character in prior_digest
):
    raise SystemExit("invalid prior-state digest")
lease_generation = receipt["lease_generation"]
if len(lease_generation) != 32 or any(
    character not in "0123456789abcdef" for character in lease_generation
):
    raise SystemExit("invalid lease generation")
directory = Path(store)
directory.mkdir(mode=0o700, parents=True, exist_ok=True)
pending = directory / f"{nonce}.pending.json"
running = directory / f"{nonce}.running.json"
terminal = directory / f"{nonce}.terminal.json"
if pending.exists() or running.exists() or terminal.exists():
    raise SystemExit("receipt replay rejected")
fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as stream:
    payload = {"receipt": receipt, "state": "pending", "received_at": now.isoformat()}
    json.dump(payload, stream, sort_keys=True)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
payload.update({"state": "running", "consumed_at": now.isoformat()})
os.replace(pending, running)
temporary = directory / f".{nonce}.running.tmp"
fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as stream:
    json.dump(payload, stream, sort_keys=True)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
os.replace(temporary, running)
print(json.dumps({
    "argv": argv,
    "nonce": nonce,
    "running": str(running),
    "transaction_id": receipt["transaction_id"],
}, sort_keys=True))
PY
) || { echo 'receipt rejected' >&2; exit 75; }

mapfile -d '' -t exact_argv < <(python3 -c '
import json,sys
for value in json.load(sys.stdin)["argv"]:
    print(value, end="\0")
' <<<"$validation")
nonce=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["nonce"])' <<<"$validation")
transaction_id=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["transaction_id"])' <<<"$validation")
running_path="$STATE_ROOT/receipts/$nonce.running.json"
terminal_path="$STATE_ROOT/receipts/$nonce.terminal.json"

rc=0
action_started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
action_stdout=$(mktemp)
action_stderr=$(mktemp)
chmod 0600 "$action_stdout" "$action_stderr"
case "$verb" in
  install-service-assets)
    if [[ "$role" == head ]]; then
      "${exact_argv[@]}" 3<"$running_path" 4<"$secret_tmp" >"$action_stdout" 2>"$action_stderr" || rc=$?
    else
      "${exact_argv[@]}" 3<"$running_path" >"$action_stdout" 2>"$action_stderr" || rc=$?
    fi
    ;;
  finalize-model-lock|copy-model|acquire-images|save-runtime-archive|copy-runtime-archive|load-runtime-archive|apply-runtime-patch|install-integration-artifacts|finalize-deployment-lock|copy-patched-source|record-license-acceptance|record-node-identity|qualify-fabric-consumer)
    "${exact_argv[@]}" 3<"$running_path" >"$action_stdout" 2>"$action_stderr" || rc=$?
    ;;
  ensure-head-worker-key|install-worker-authorized-key|install-head-worker-known-hosts)
    "${exact_argv[@]}" 3<"$running_path" >"$action_stdout" 2>"$action_stderr" || rc=$?
    ;;
  *)
    "${exact_argv[@]}" >"$action_stdout" 2>"$action_stderr" || rc=$?
    ;;
esac
action_output_sha256=$(
  { sha256sum "$action_stdout"; sha256sum "$action_stderr"; } | sha256sum | awk '{print $1}'
)

if ((rc == 0)) && [[ "$verb" == start ]]; then
  compose=(sudo -n docker compose --env-file "$ENV_FILE" --file "$COMPOSE_FILE" --project-directory "$SOURCE_ROOT" --project-name "$PROJECT")
  if [[ "$role" == worker ]]; then
    deadline=$((SECONDS + 120))
  else
    deadline=$((SECONDS + 1200))
  fi
  stable_since=0
  while ((SECONDS < deadline)); do
    container_id=$("${compose[@]}" ps -q "$COMPOSE_SERVICE" 2>/dev/null || true)
    if [[ -n "$container_id" ]]; then
      inspect=$(sudo -n docker inspect --format '{{.State.Status}} {{.RestartCount}}' "$container_id" 2>/dev/null || true)
      if [[ "$inspect" == 'running 0' ]]; then
        if ((stable_since == 0)); then stable_since=$SECONDS; fi
        if [[ "$role" == worker ]] && ((SECONDS - stable_since >= 15)); then
          if ! validate_runtime_argv "$container_id"; then
            rc=75
          elif ss -H -ltn 'sport = :18888' | grep -q .; then
            echo 'worker unexpectedly exposes the internal HTTP port' >&2
            rc=75
          fi
          break
        fi
        if [[ "$role" == head ]] && python3 - <<'PY'
import http.client
connection = http.client.HTTPConnection("127.0.0.1", 18888, timeout=2)
connection.request("GET", "/health")
response = connection.getresponse()
raise SystemExit(0 if 200 <= response.status < 300 else 1)
PY
        then
          if ! validate_runtime_argv "$container_id"; then
            rc=75
          fi
          break
        fi
      else
        stable_since=0
      fi
    fi
    sleep 2
  done
  if ((SECONDS >= deadline)); then
    echo "$role readiness gate timed out" >&2
    rc=75
  fi
fi

if ((rc == 0)) && [[ "$verb" == stop ]]; then
  if sudo -n docker compose --env-file "$ENV_FILE" --file "$COMPOSE_FILE" \
      --project-directory "$SOURCE_ROOT" --project-name "$PROJECT" ps -q | grep -q .; then
    echo 'exact project cleanup left containers behind' >&2
    rc=75
  fi
fi

if ((rc == 0)) && [[ "$verb" == cutover || "$verb" == production-activate-gateway ]]; then
  deadline=$((SECONDS + 30))
  active=''
  while ((SECONDS < deadline)); do
    active=$(sudo -n systemctl show deepseek-v4-flash-gateway.service --property=ActiveState --value) || { rc=75; break; }
    if [[ "$active" == active ]] && \
        sudo -n test ! -e "$GATEWAY_RUN/gateway-authorized.pending" && \
        sudo -n test -f "$GATEWAY_RUN/gateway-authorized.running"; then
      break
    fi
    sleep 1
  done
  if [[ "$active" != active ]] || \
      sudo -n test -e "$GATEWAY_RUN/gateway-authorized.pending" || \
      ! sudo -n test -f "$GATEWAY_RUN/gateway-authorized.running"; then
    echo 'condition-skipped gateway start did not prove ActiveState=active' >&2
    rc=75
  elif ! sudo -n python3 - "$GATEWAY_RUN/gateway-authorized.running" "$transaction_id" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
raise SystemExit(0 if value == {"schema_version": 1, "transaction_id": sys.argv[2]} else 1)
PY
  then
    echo 'gateway authorization transaction mismatch' >&2
    rc=75
  fi
fi

if ((rc == 0)) && [[ "$verb" == production-deactivate-gateway ]]; then
  active=$(sudo -n systemctl show deepseek-v4-flash-gateway.service --property=ActiveState --value) || rc=75
  if [[ "$active" == active ]] || \
      sudo -n test -e "$GATEWAY_RUN/gateway-authorized.pending" || \
      sudo -n test -e "$GATEWAY_RUN/gateway-authorized.running" || \
      sudo -n docker inspect deepseek-v4-flash-gateway >/dev/null 2>&1 || \
      ss -H -ltn 'sport = :11434' | grep -q .; then
    echo 'production gateway resources remained after deactivate' >&2
    rc=75
  fi
fi

if ((rc == 0)) && [[ "$verb" == rollback ]]; then
  if ! sudo -n python3 - "$FALLBACK_STATE/recovery-succeeded.json" "$transaction_id" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
if not path.is_file():
    raise SystemExit(1)
value = json.loads(path.read_text(encoding="utf-8"))
raise SystemExit(0 if value.get("transaction_id") == sys.argv[2] else 1)
PY
  then
    echo 'rollback recovery transaction mismatch or recovery-succeeded.json absent' >&2
    rc=75
  fi
fi

if ((rc == 0)) && [[ "$verb" == stage-gateway ]]; then
  deadline=$((SECONDS + 30))
  active=''
  listener=''
  while ((SECONDS < deadline)); do
    active=$(sudo -n systemctl show deepseek-v4-flash-gateway-staging.service --property=ActiveState --value) || { rc=75; break; }
    listener=$(ss -H -ltn 'sport = :11435' || true)
    if [[ "$active" == active && "$listener" == *'127.0.0.1:11435'* ]]; then
      break
    fi
    sleep 1
  done
  if [[ "$active" != active || "$listener" != *'127.0.0.1:11435'* ]]; then
    echo 'loopback staging gateway did not become active on 127.0.0.1:11435' >&2
    rc=75
  fi
fi

if ((rc == 0)) && [[ "$verb" == stop-staging-gateway ]]; then
  if ss -H -ltn 'sport = :11435' | grep -q .; then
    echo 'staging gateway listener remained after stop' >&2
    rc=75
  fi
fi

evidence_sha256=''
if ((rc == 0)) && [[ "$verb" == start ]]; then
  if ! container_id=$(sudo -n docker compose --env-file "$ENV_FILE" --file "$COMPOSE_FILE" \
      --project-directory "$SOURCE_ROOT" --project-name "$PROJECT" ps -q "$COMPOSE_SERVICE"); then
    rc=75
  elif ! evidence_sha256=$(sudo -n docker inspect "$container_id" | sha256sum | awk '{print $1}'); then
    rc=75
  else
    startup_log=$(sudo -n docker logs --since "$action_started_at" "$container_id" 2>&1) || rc=75
    if grep -Eiq 'NCCL WARN|NCCL.*(fatal|unhandled)|socket fallback|CUDA error|Traceback' \
        <<<"$startup_log"; then
      echo 'fatal runtime signature found in bounded startup logs' >&2
      rc=75
    fi
  fi
fi

python3 - "$running_path" "$terminal_path" "$rc" "$evidence_sha256" "$action_output_sha256" <<'PY'
import datetime, json, os, sys
from pathlib import Path
running, terminal, rc, evidence, action_output = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]), sys.argv[4], sys.argv[5]
value = json.loads(running.read_text(encoding="utf-8"))
value.update({
    "state": "succeeded" if rc == 0 else "failed",
    "exit_code": rc,
    "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "action_output_sha256": action_output,
})
if evidence:
    value["evidence_sha256"] = evidence
fd = os.open(terminal, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as stream:
    json.dump(value, stream, sort_keys=True)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
running.unlink()
PY

if ((rc != 0)); then
  echo "$verb failed stderr (redacted, first 80 lines):" >&2
  sed -n '1,80p' "$action_stderr" |
    sed -E 's/[A-Za-z0-9_-]{43,}/[REDACTED]/g' >&2
  printf '{"terminal":"failed","exit_code":%d}\n' "$rc"
  exit "$rc"
fi
if [[ -n "$evidence_sha256" ]]; then
  printf '{"terminal":"succeeded","exit_code":0,"evidence_sha256":"%s"}\n' "$evidence_sha256"
else
  printf '{"terminal":"succeeded","exit_code":0}\n'
fi
