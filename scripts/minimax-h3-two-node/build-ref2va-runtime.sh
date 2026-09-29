#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

command_name=${1:-}
[[ -n $command_name ]] || die "usage: build-ref2va-runtime.sh validate|verify-source|build|seal-existing"
shift

lock="$ROOT/manifests/minimax-h3-ref2va-runtime-lock.json"
source_dir=
output_lock=
execute=0

while (($#)); do
  case "$1" in
    --lock) lock=$2; shift 2 ;;
    --source-dir) source_dir=$2; shift 2 ;;
    --output-lock) output_lock=$2; shift 2 ;;
    --execute) execute=1; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done

validate_lock() {
  python3 - "$lock" "$ROOT" <<'PY'
import hashlib,json,pathlib,re,sys
lock_path=pathlib.Path(sys.argv[1]); root=pathlib.Path(sys.argv[2])
try: value=json.loads(lock_path.read_text(encoding="utf-8"))
except Exception as exc: raise SystemExit(f"ERROR: invalid Ref2VA runtime lock: {exc}")
def fail(message): raise SystemExit("ERROR: "+message)
hex64=lambda item: isinstance(item,str) and re.fullmatch(r"[0-9a-f]{64}",item)
image=lambda item: isinstance(item,str) and re.fullmatch(r"sha256:[0-9a-f]{64}",item)
if value.get("schema_version") != 1: fail("unsupported Ref2VA runtime lock schema")
if value.get("required_architecture") != "aarch64": fail("Ref2VA runtime architecture drift")
source=value.get("vllm_omni_source",{})
if source.get("repository") != "https://github.com/vllm-project/vllm-omni.git": fail("source repository drift")
if source.get("commit") != "a65e89cbd47fe7c9f39dba558200c0bc16a758f3": fail("source commit drift")
base=value.get("base_image",{})
if base.get("child_digest") != "sha256:474d635dc5dc2cf66f0df692ddef19967a920253826f431689af2b0e010afc4e": fail("arm64 child digest drift")
parent=value.get("build_parent",{})
if parent.get("tag") != "minimax-h3-two-node:production" or not image(parent.get("final_image_id")): fail("build parent drift")
candidate=value.get("candidate",{})
if candidate.get("tag") != "minimax-h3-two-node:ref2va-candidate": fail("candidate tag drift")
if candidate.get("status") == "unresolved":
    if candidate.get("final_image_id") is not None or candidate.get("sealed_at") is not None: fail("unresolved candidate has sealed fields")
elif candidate.get("status") == "resolved":
    if not image(candidate.get("final_image_id")): fail("resolved candidate image ID is invalid")
    if not isinstance(candidate.get("sealed_at"),str) or not candidate["sealed_at"].endswith("Z"): fail("resolved candidate timestamp is invalid")
else: fail("candidate status is invalid")
patches=0
for entry in value.get("local_inputs",[]):
    path=root/str(entry.get("path",""))
    if not path.is_file() or path.is_symlink(): fail(f"missing or unsafe local input: {path}")
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    if not hex64(entry.get("sha256")) or digest != entry["sha256"]: fail(f"local input hash mismatch: {path}")
    if entry.get("role") == "vllm-omni-source-patch": patches += 1
if patches != 1: fail("exactly one vllm-omni source patch is required")
print("PASS Ref2VA runtime lock")
PY
}

locked_value() {
  python3 - "$lock" "$1" <<'PY'
import json,sys
value=json.load(open(sys.argv[1],encoding="utf-8"))
for part in sys.argv[2].split('.'):
    value=value[part]
print(value)
PY
}

patch_path() {
  python3 - "$lock" "$ROOT" <<'PY'
import json,pathlib,sys
value=json.load(open(sys.argv[1],encoding="utf-8"))
rows=[row for row in value["local_inputs"] if row.get("role")=="vllm-omni-source-patch"]
if len(rows)!=1: raise SystemExit("ERROR: exactly one vllm-omni source patch is required")
print(pathlib.Path(sys.argv[2])/rows[0]["path"])
PY
}

verify_source() {
  [[ -n $source_dir && -d $source_dir ]] || die "--source-dir must be a git checkout"
  git -C "$source_dir" rev-parse --git-dir >/dev/null 2>&1 || die "--source-dir must be a git checkout"
  local expected observed patch
  expected=$(locked_value vllm_omni_source.commit)
  observed=$(git -C "$source_dir" rev-parse HEAD)
  [[ $observed == "$expected" ]] || die "vllm-omni source commit mismatch"
  [[ -z $(git -C "$source_dir" status --porcelain) ]] || die "vllm-omni source checkout is dirty"
  patch=$(patch_path)
  git -C "$source_dir" apply --check "$patch"
  grep -Fq -- '--task-type' "$source_dir/vllm_omni/entrypoints/cli/serve.py" || die "candidate source lacks --task-type"
  grep -Fq '"ref2va"' "$source_dir/vllm_omni/diffusion/models/minimax_h3/pipeline_minimax_h3.py" || die "candidate source lacks ref2va"
  printf 'PASS Ref2VA source %s\n' "$observed"
}

seal_lock() {
  local image_id=$1
  [[ -n $output_lock && ! -e $output_lock ]] || die "--output-lock must name a new file"
  python3 - "$lock" "$output_lock" "$image_id" <<'PY'
import datetime,json,os,pathlib,re,sys,tempfile
source=pathlib.Path(sys.argv[1]); destination=pathlib.Path(sys.argv[2]); image_id=sys.argv[3]
if not re.fullmatch(r"sha256:[0-9a-f]{64}",image_id): raise SystemExit("ERROR: invalid observed image ID")
value=json.loads(source.read_text(encoding="utf-8"))
value["candidate"]={
    "tag":"minimax-h3-two-node:ref2va-candidate",
    "status":"resolved",
    "final_image_id":image_id,
    "sealed_at":datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00","Z"),
}
destination.parent.mkdir(parents=True,exist_ok=True)
descriptor,temporary=tempfile.mkstemp(prefix=f".{destination.name}.",dir=destination.parent)
try:
    with os.fdopen(descriptor,"w",encoding="utf-8") as stream:
        json.dump(value,stream,indent=2,sort_keys=True); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
    os.chmod(temporary,0o600)
    os.replace(temporary,destination)
except BaseException:
    try: os.unlink(temporary)
    except FileNotFoundError: pass
    raise
PY
}

attest_candidate() {
  local candidate_tag=$1
  [[ $(docker image inspect "$candidate_tag" --format '{{.Architecture}}') == arm64 ]] ||
    die "Ref2VA candidate is not arm64"
  docker run --rm --network none --entrypoint python3 "$candidate_tag" -c \
    'import importlib.metadata as m; from vllm_omni.diffusion.registry import _DIFFUSION_MODELS; from vllm_omni.model_executor.models.minimax_h3.checkpoint import MINIMAX_H3_TEXT_ENCODER_DOWNLOAD_PATTERNS as p; d=m.distribution("vllm-omni"); cli=d.locate_file("vllm_omni/entrypoints/cli/serve.py"); assert "MiniMaxH3Pipeline" in _DIFFUSION_MODELS; assert p["ref2va"]==["FL2VA/text_encoder/**"]; assert d.version=="0.1.dev0+ga65e89c"; assert "--task-type" in cli.read_text()' ||
    die "Ref2VA candidate Python attestation failed"
}

case "$command_name" in
  validate)
    validate_lock
    ;;
  verify-source)
    validate_lock >/dev/null
    verify_source
    ;;
  build)
    validate_lock >/dev/null
    verify_source >/dev/null
    [[ $execute == 1 && ${H3_ALLOW_LOCAL_DOCKER_BUILD:-0} == 1 ]] ||
      die "build requires --execute and H3_ALLOW_LOCAL_DOCKER_BUILD=1"
    [[ $(uname -m) == aarch64 ]] || die "Ref2VA runtime build requires aarch64"
    command -v docker >/dev/null || die "docker is required"
    parent_tag=$(locked_value build_parent.tag)
    parent_id=$(locked_value build_parent.final_image_id)
    candidate_tag=$(locked_value candidate.tag)
    [[ $(docker image inspect "$parent_tag" --format '{{.Id}}') == "$parent_id" ]] || die "production parent image ID mismatch"
    [[ $(docker image inspect "$parent_tag" --format '{{.Architecture}}') == arm64 ]] || die "production parent is not arm64"
    if docker image inspect "$candidate_tag" >/dev/null 2>&1; then
      die "candidate tag already exists; preserve it and choose a fresh qualification transaction"
    fi
    build_root=$(mktemp -d "${TMPDIR:-/tmp}/h3-ref2va-build.XXXXXX")
    trap 'rm -rf -- "$build_root"' EXIT
    mkdir -p "$build_root/source"
    git -C "$source_dir" ls-files -z |
      tar -C "$source_dir" --null -T - -cf - |
      tar -C "$build_root/source" -xf -
    git -C "$build_root/source" apply "$(patch_path)"
    cat >"$build_root/Dockerfile" <<'DOCKERFILE'
FROM minimax-h3-two-node:production
ARG VLLM_OMNI_VERSION
COPY source /opt/vllm-omni-ref2va-source
RUN VLLM_OMNI_VERSION_OVERRIDE="$VLLM_OMNI_VERSION" \
    python3 -m pip install --no-deps --no-build-isolation /opt/vllm-omni-ref2va-source
DOCKERFILE
    docker build --pull=false --network=none \
      --build-arg VLLM_OMNI_VERSION=0.1.dev0+ga65e89c \
      --tag "$candidate_tag" "$build_root"
    attest_candidate "$candidate_tag"
    observed=$(docker image inspect "$candidate_tag" --format '{{.Id}}')
    seal_lock "$observed"
    printf 'PASS Ref2VA candidate image %s\n' "$observed"
    ;;
  seal-existing)
    validate_lock >/dev/null
    [[ $execute == 1 && ${H3_ALLOW_LOCAL_DOCKER_BUILD:-0} == 1 ]] ||
      die "seal-existing requires --execute and H3_ALLOW_LOCAL_DOCKER_BUILD=1"
    [[ $(uname -m) == aarch64 ]] || die "Ref2VA runtime seal requires aarch64"
    command -v docker >/dev/null || die "docker is required"
    candidate_tag=$(locked_value candidate.tag)
    docker image inspect "$candidate_tag" >/dev/null 2>&1 || die "Ref2VA candidate image is absent"
    attest_candidate "$candidate_tag"
    observed=$(docker image inspect "$candidate_tag" --format '{{.Id}}')
    seal_lock "$observed"
    printf 'PASS sealed existing Ref2VA candidate image %s\n' "$observed"
    ;;
  *) die "unknown command: $command_name" ;;
esac
