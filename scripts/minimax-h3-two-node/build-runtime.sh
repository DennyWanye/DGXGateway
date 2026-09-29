#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

command_name="${1:-}"
[[ -n "$command_name" ]] || { h3_die "usage: build-runtime.sh validate|verify-sources|prepare|build|probe-local"; exit 2; }
shift

phase=research
lock="$ROOT/manifests/minimax-h3-runtime-lock.json"
source_dir=
output_dir=
wheelhouse=
requirements_lock=
image=
proxy_url=
execute=0

while (($#)); do
  case "$1" in
    --phase) phase="$2"; shift 2 ;;
    --lock) lock="$2"; shift 2 ;;
    --source-dir) source_dir="$2"; shift 2 ;;
    --output-dir) output_dir="$2"; shift 2 ;;
    --wheelhouse) wheelhouse="$2"; shift 2 ;;
    --requirements-lock) requirements_lock="$2"; shift 2 ;;
    --image) image="$2"; shift 2 ;;
    --proxy-url) proxy_url="$2"; shift 2 ;;
    --execute) execute=1; shift ;;
    *) h3_die "unknown argument: $1"; exit 2 ;;
  esac
done

if [[ -n "$proxy_url" ]]; then
  python3 - "$proxy_url" <<'PY'
import sys,urllib.parse
value=sys.argv[1]
parsed=urllib.parse.urlsplit(value)
if parsed.scheme not in ("http","https") or not parsed.hostname or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
    raise SystemExit("ERROR: proxy URL must be an http(s) origin")
if parsed.username is not None or parsed.password is not None:
    raise SystemExit("ERROR: proxy credentials are forbidden")
if parsed.port is None:
    raise SystemExit("ERROR: proxy URL must include an explicit port")
PY
fi

validate_lock() {
  python3 - "$lock" "$phase" "$ROOT" <<'PY'
import hashlib,json,pathlib,re,sys
path=pathlib.Path(sys.argv[1]); phase=sys.argv[2]; root=pathlib.Path(sys.argv[3])
try: data=json.loads(path.read_text())
except Exception as exc: raise SystemExit(f"ERROR: invalid runtime lock: {exc}")
def fail(message): raise SystemExit("ERROR: " + message)
def hex64(value): return isinstance(value,str) and re.fullmatch(r"[0-9a-f]{64}",value) is not None
def image_id(value): return isinstance(value,str) and re.fullmatch(r"sha256:[0-9a-f]{64}",value) is not None
if data.get("schema_version") != 1: fail("unsupported runtime lock schema")
if data.get("required_architecture") != "aarch64": fail("runtime architecture is not locked to aarch64")
expected_upstream="vllm/vllm-omni:minimax-h3@sha256:e930db8e225162d01e17a49dddc43fd0e844208908d8356a028e5c4e7357696e"
if data.get("upstream_image") != expected_upstream: fail("upstream image digest drift")
platform=data.get("upstream_platform",{})
if platform != {"architecture":"arm64","child_digest":"sha256:474d635dc5dc2cf66f0df692ddef19967a920253826f431689af2b0e010afc4e","compressed_size_bytes":10825926989,"config_digest":"sha256:c3cbf972d026ba07223135b1d6b603edb980aa3123c1ead1dc918f057f21f4e3","os":"linux"}: fail("upstream arm64 child identity drift")
if data.get("historical_layer1_image_id") != "sha256:2383642e221530d3dc26a8f8632c37e00470b051979f0845c2ec0ff9513e04b2": fail("historical layer1 image ID drift")
if data.get("layer1_acceptance") != "pinned-source-and-target-hashes": fail("layer1 acceptance policy drift")
for name,source in data.get("sources",{}).items():
    if not re.fullmatch(r"[0-9a-f]{40}",str(source.get("commit",""))): fail(f"invalid {name} commit")
    if not hex64(source.get("archive_sha256")) or not hex64(source.get("content_tree_sha256")): fail(f"invalid {name} archive identity")
for entry in data.get("source_patches",[]):
    patch=root / entry.get("path","")
    if not patch.is_file(): fail(f"missing source patch: {patch}")
    digest=hashlib.sha256(patch.read_bytes()).hexdigest()
    if digest != entry.get("sha256"): fail(f"source patch hash mismatch: {patch}")
for entry in data.get("deployment_overlays",[]):
    overlay=root / entry.get("path","")
    if not overlay.is_file(): fail(f"missing deployment overlay: {overlay}")
    digest=hashlib.sha256(overlay.read_bytes()).hexdigest()
    if digest != entry.get("sha256"): fail(f"deployment overlay hash mismatch: {overlay}")
    if not str(entry.get("target","")).startswith("/opt/h3-multinode/"): fail("unsafe deployment overlay target")
profile=data.get("canonical_profile",{})
required={"attention_backend":"CUDNN_ATTN","cache_dit":False,"num_gpus":2,"usp":2,"ring":1,"vae_parallel_mode":"tile"}
for key,value in required.items():
    if profile.get(key) != value: fail(f"canonical profile drift: {key}")
if len(profile.get("fp8_ignored_layers",[])) != 6: fail("full FP8 ignored-layers profile is required")
if phase not in ("research","build","deploy"): fail("phase must be research, build, or deploy")
targets=data.get("patch_targets",[])
if len(targets) < 2: fail("patch target lock is incomplete")
if phase in ("build","deploy"):
    for target in targets:
        if not hex64(target.get("preimage_sha256")) or not hex64(target.get("result_sha256")): fail(f"UNRESOLVED patch target: {target.get('path')}")
    wheelhouse=data.get("wheelhouse",{})
    if wheelhouse.get("status") != "resolved" or not hex64(wheelhouse.get("requirements_lock_sha256")) or not hex64(wheelhouse.get("tree_sha256")): fail("UNRESOLVED wheelhouse lock")
if phase == "deploy":
    if data.get("status") != "resolved": fail("runtime lock status is not resolved")
    production=data.get("production",{})
    if not image_id(production.get("final_image_id")): fail("UNRESOLVED production final_image_id")
    if not hex64(production.get("oci_archive_sha256")): fail("UNRESOLVED production oci_archive_sha256")
print(f"PASS runtime-lock phase={phase}")
PY
}

verify_sources() {
  [[ -n "$source_dir" ]] || h3_die "--source-dir is required"
  python3 - "$lock" "$source_dir" <<'PY'
import hashlib,json,pathlib,tarfile,sys
lock=json.load(open(sys.argv[1])); root=pathlib.Path(sys.argv[2])
for name,source in lock["sources"].items():
    archive=root/source["archive"]
    if not archive.is_file(): raise SystemExit(f"ERROR: missing {name} archive: {archive}")
    actual=hashlib.sha256(archive.read_bytes()).hexdigest()
    if actual != source["archive_sha256"]: raise SystemExit(f"ERROR: {name} archive SHA-256 mismatch")
    rows=[]
    with tarfile.open(archive,"r:gz") as stream:
        for member in stream.getmembers():
            parts=pathlib.PurePosixPath(member.name).parts
            if member.name.startswith("/") or ".." in parts or member.issym() or member.islnk():
                raise SystemExit(f"ERROR: unsafe member in {name} archive: {member.name}")
        for member in sorted((item for item in stream.getmembers() if item.isfile()),key=lambda item:item.name):
            payload=stream.extractfile(member).read()
            rows.append((member.name,member.mode,len(payload),hashlib.sha256(payload).hexdigest()))
    tree=hashlib.sha256(json.dumps(rows,separators=(",",":")).encode()).hexdigest()
    if tree != source["content_tree_sha256"]: raise SystemExit(f"ERROR: {name} content tree mismatch")
print("PASS runtime source archives")
PY
}

prepare_sources() {
  [[ -n "$output_dir" ]] || h3_die "--output-dir is required"
  [[ ! -e "$output_dir" ]] || h3_die "refusing to overwrite output directory: $output_dir"
  verify_sources
  mkdir -m 0700 "$output_dir"
  local companion_archive two_archive
  companion_archive="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["sources"]["companion"]["archive"])' "$lock")"
  two_archive="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["sources"]["two_node"]["archive"])' "$lock")"
  tar -xzf "$source_dir/$companion_archive" -C "$output_dir"
  tar -xzf "$source_dir/$two_archive" -C "$output_dir"
  local two_root="$output_dir/minimax-h3-two-node"
  while IFS= read -r patch_path; do
    patch --batch --forward -p1 -d "$two_root" <"$ROOT/$patch_path"
  done < <(python3 - "$lock" <<'PY'
import json,sys
for item in json.load(open(sys.argv[1]))["source_patches"]: print(item["path"])
PY
)
  printf 'PASS prepared runtime sources at %s\n' "$output_dir"
}

locked_target_value() {
  local index="$1" field="$2"
  python3 - "$lock" "$index" "$field" <<'PY'
import json,sys
print(json.load(open(sys.argv[1]))["patch_targets"][int(sys.argv[2])][sys.argv[3]])
PY
}

verify_target_hash() {
  local image_ref="$1" target_path="$2" expected="$3" label="$4" actual
  actual="$(docker run --rm --network none --entrypoint sha256sum "$image_ref" "$target_path" | awk '{print $1}')"
  [[ "$actual" == "$expected" ]] || {
    h3_die "$label hash mismatch for $target_path: expected $expected, got $actual"
    return 1
  }
}

case "$command_name" in
  validate)
    validate_lock
    ;;
  verify-sources)
    phase=research
    validate_lock >/dev/null
    verify_sources
    ;;
  prepare)
    phase=research
    validate_lock >/dev/null
    prepare_sources
    ;;
  build)
    phase=build
    validate_lock >/dev/null
    [[ "$execute" == 1 && "${H3_ALLOW_LOCAL_DOCKER_BUILD:-0}" == 1 ]] ||
      { h3_die "build requires --execute and H3_ALLOW_LOCAL_DOCKER_BUILD=1"; exit 2; }
    [[ "$(uname -m)" == aarch64 ]] || { h3_die "runtime build requires aarch64"; exit 2; }
    [[ -n "$output_dir" && -n "$wheelhouse" && -n "$requirements_lock" ]] ||
      { h3_die "build requires output, wheelhouse, and requirements lock"; exit 2; }
    command -v docker >/dev/null || { h3_die "docker is required"; exit 2; }
    expected_requirements="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["wheelhouse"]["requirements_lock_sha256"])' "$lock")"
    [[ "$(h3_sha256_file "$requirements_lock")" == "$expected_requirements" ]] ||
      { h3_die "requirements lock SHA-256 mismatch"; exit 2; }
    actual_wheelhouse="$(python3 - "$wheelhouse" <<'PY'
import hashlib,json,pathlib,sys
root=pathlib.Path(sys.argv[1]); rows=[]
for path in sorted(p for p in root.rglob('*') if p.is_file()):
    rows.append((path.relative_to(root).as_posix(),hashlib.sha256(path.read_bytes()).hexdigest()))
print(hashlib.sha256(json.dumps(rows,separators=(',',':')).encode()).hexdigest())
PY
)"
    expected_wheelhouse="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["wheelhouse"]["tree_sha256"])' "$lock")"
    [[ "$actual_wheelhouse" == "$expected_wheelhouse" ]] || { h3_die "wheelhouse tree mismatch"; exit 2; }
    prepare_sources
    companion="$output_dir/minimax-h3-single"
    two="$output_dir/minimax-h3-two-node"
    install -m 0644 "$requirements_lock" "$two/requirements-ray.lock"
    mkdir -m 0755 "$two/wheelhouse"
    cp -R "$wheelhouse"/. "$two/wheelhouse"/
    upstream="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["upstream_image"])' "$lock")"
    upstream_config="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["upstream_platform"]["config_digest"])' "$lock")"
    docker image inspect "$upstream" >/dev/null
    [[ "$(docker image inspect "$upstream" --format '{{.Architecture}}')" == arm64 ]] ||
      { h3_die "pinned upstream image is not arm64"; exit 2; }
    [[ "$(docker image inspect "$upstream" --format '{{.Id}}')" == "$upstream_config" ]] ||
      { h3_die "pinned upstream config digest mismatch"; exit 2; }
    transformer_target="$(locked_target_value 0 path)"
    abstract_target="$(locked_target_value 1 path)"
    verify_target_hash "$upstream" "$transformer_target" "$(locked_target_value 0 preimage_sha256)" preimage
    proxy_args=()
    if [[ -n "$proxy_url" ]]; then
      proxy_args+=(--build-arg "HTTP_PROXY=$proxy_url" --build-arg "HTTPS_PROXY=$proxy_url")
    fi
    docker build --network=none "${proxy_args[@]}" --build-arg "BASE_IMAGE=$upstream" -t minimax-h3-layer1:candidate "$companion"
    verify_target_hash minimax-h3-layer1:candidate "$transformer_target" "$(locked_target_value 0 result_sha256)" result
    verify_target_hash minimax-h3-layer1:candidate "$abstract_target" "$(locked_target_value 1 preimage_sha256)" preimage
    layer1_id="$(docker image inspect minimax-h3-layer1:candidate --format '{{.Id}}')"
    docker build --network=none "${proxy_args[@]}" \
      --build-arg BASE_IMAGE=minimax-h3-layer1:candidate \
      --build-arg "H3_ACCEPTED_BASE_IMAGE_ID=$layer1_id" \
      -t minimax-h3-two-node:candidate "$two"
    verify_target_hash minimax-h3-two-node:candidate "$abstract_target" "$(locked_target_value 1 result_sha256)" result
    candidate_id="$(docker image inspect minimax-h3-two-node:candidate --format '{{.Id}}')"
    archive="$output_dir/minimax-h3-two-node-candidate.oci.tar"
    docker save --output "$archive" minimax-h3-two-node:candidate
    receipt="$output_dir/candidate-runtime-receipt.json"
    [[ ! -e "$receipt" ]] || { h3_die "candidate receipt already exists"; exit 2; }
    python3 - "$candidate_id" "$layer1_id" "$archive" "$receipt" <<'PY'
import hashlib,json,pathlib,sys
archive=pathlib.Path(sys.argv[3]); out=pathlib.Path(sys.argv[4])
data={"candidate_image_id":sys.argv[1],"layer1_image_id":sys.argv[2],"oci_archive":archive.name,"oci_archive_sha256":hashlib.sha256(archive.read_bytes()).hexdigest(),"status":"candidate-not-deployable-until-lock-is-resolved"}
out.write_text(json.dumps(data,sort_keys=True,indent=2)+"\n")
PY
    printf 'PASS candidate built; resolve and revalidate runtime lock before deployment\n'
    ;;
  probe-local)
    phase=deploy
    validate_lock >/dev/null
    [[ "$execute" == 1 ]] || { h3_die "probe-local requires --execute"; exit 2; }
    [[ -n "$image" ]] || { h3_die "probe-local requires --image"; exit 2; }
    expected="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["production"]["final_image_id"])' "$lock")"
    [[ "$(docker image inspect "$image" --format '{{.Id}}')" == "$expected" ]] || { h3_die "probe image ID mismatch"; exit 2; }
    docker run --rm --network none --gpus all --entrypoint python "$image" - <<'PY'
import importlib.metadata,platform,torch
assert platform.machine()=="aarch64"
assert torch.cuda.is_available()
assert torch.version.cuda=="13.0"
assert torch.cuda.get_device_capability()==(12,1)
assert importlib.metadata.version("ray")=="2.56.1"
assert importlib.metadata.version("vllm")=="0.26.0"
assert importlib.metadata.version("vllm-omni")=="0.1.dev2381+g310b4b477"
x=torch.ones(32,device="cuda",dtype=torch.bfloat16)
assert torch.isfinite(x.sum())
print("PASS local GB10/CUDA/runtime probe")
PY
    ;;
  *) h3_die "unknown command: $command_name"; exit 2 ;;
esac
