#!/usr/bin/env python3
"""One-node H3 profile validation/launch. Does not rewrite networks or orchestrate SSH.

Use render for an existing Gateway/supervisor. Standalone run/stop requires an explicit flag.
Four nodes must pass matching receipts; that is an offline gate, not proof of live RDMA.
"""
from __future__ import annotations
import argparse
import datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from assets_v2 import ASSETS, HYBRID_FILE, canonical, verify_receipt
from check_encoder import check as check_encoder

KIT = Path(__file__).resolve().parents[1]
IDS = ("fl2va-official", "ref2va-official", "ref2va-heretic", "hybrid-experimental")


def load(path: Path):
    return json.loads(path.read_text())


def validate_profile(p: dict) -> None:
    if p["id"] not in IDS:
        raise ValueError("Unknown profile")
    if p["performance_mode"] not in ("auto", "memory", "speed"):
        raise ValueError("Unreviewed performance mode")
    if p["online_quantization"] not in ("fp8", "none"):
        raise ValueError("Unreviewed quantization")
    expected = {
        "fl2va-official": ("fl2va", False, False, ["official-fl"], ["t2va", "fl2va"]),
        "ref2va-official": ("ref2va", False, False, ["official-ref"], ["ref2va"]),
        "ref2va-heretic": ("ref2va", True, False, ["official-ref", "encoder-heretic"], ["ref2va"]),
        "hybrid-experimental": ("hybrid", False, True, ["official-fl", "official-ref", "hybrid-singularity"], ["t2va", "fl2va", "ref2va"]),
    }[p["id"]]
    actual = (p["model_variant"], p["encoder_override"], p["transformer_override"], p["asset_ids"], p["tasks"])
    if actual != expected:
        raise ValueError("Profile components/tasks differ from reviewed design; make an explicit code/config revision")
    if p["transformer_override"] and p["online_quantization"] != "none":
        raise ValueError("Never requantize the prequantized Hybrid checkpoint")
    if bool(p["experimental"]) != (p["id"] == "hybrid-experimental"):
        raise ValueError("Invalid experimental classification")
    placement = p.get("memory_placement")
    if placement is not None:
        if (placement not in ("memory-dit-half-v1", "memory-dit-resident-v1") or p["id"] != "ref2va-official"
                or p["performance_mode"] != "memory" or p["online_quantization"] != "fp8"
                or p.get("dit_layerwise_resident_layers", 0) != 0
                or p.get("dit_offload_prefetch_size", 0) != 0
                or p.get("component_residency")):
            raise ValueError("Unreviewed H3 memory placement configuration")

    fp8_probe = p.get("fp8_probe_candidate")
    if fp8_probe is not None and (fp8_probe != "triton-m128n256k64-scalealigned-v2"
            or p["id"] != "ref2va-official" or p["performance_mode"] != "memory"
            or p["online_quantization"] != "fp8" or p.get("memory_placement")
            or p.get("component_residency") or p.get("attention_probe_candidate")):
        raise ValueError("Unreviewed FP8 probe configuration")
    probe = p.get("attention_probe_candidate")
    if probe is not None and (probe != "m256n64-t256-v1" or p["id"] != "ref2va-official"
                              or p["performance_mode"] != "memory" or p["online_quantization"] != "fp8"
                              or p.get("memory_placement") or p.get("component_residency")):
        raise ValueError("Unreviewed next attention probe configuration")


def roots(c: dict) -> dict[str, Path]:
    return {"official-fl": Path(c["base_model_root"]), "official-ref": Path(c["base_model_root"]),
            "encoder-heretic": Path(c["heretic_root"]), "hybrid-singularity": Path(c["hybrid_root"])}


def build(c: dict, n: dict, p: dict, receipts: dict, world: int, deployment: str, log_dir: Path,
          container_prefix: str = "h3-ring-serve-r") -> tuple[list[str], dict]:
    """Pure command/identity assembly; callers must validate runtime/files first."""
    validate_profile(p)
    if (p.get("memory_placement") or p.get("attention_probe_candidate") or p.get("fp8_probe_candidate")) and world != 4:
        raise ValueError("H3 memory placement candidate requires Ring4")
    fa_patch = KIT / "patches" / "fa-sm120-cuda.py"
    if not fa_patch.is_file():
        raise ValueError("Missing FA SM120 patch file")
    fa_patch_sha256 = hashlib.sha256(fa_patch.read_bytes()).hexdigest()
    optional_root = KIT / "patches" / "ref2va-optional"
    optional_manifest = None
    if p["model_variant"] == "ref2va":
        optional_manifest = load(optional_root / "manifest.json")
        if optional_manifest["source_commit"] != c["source_commit"]:
            raise ValueError("Ref2VA optional-input overlay source version mismatch")
        if set(optional_manifest["files"]) != {"request_validation.py", "presentation.py", "stages/denoising.py"}:
            raise ValueError("Unexpected Ref2VA optional-input overlay files")
        for rel, entry in optional_manifest["files"].items():
            if hashlib.sha256((optional_root / rel).read_bytes()).hexdigest() != entry["sha256"]:
                raise ValueError(f"Ref2VA optional-input overlay checksum mismatch: {rel}")
    performance_root = KIT / "patches" / "performance"
    performance_manifest = load(performance_root / "manifest.json") if (performance_root / "manifest.json").is_file() else None
    if performance_manifest:
        if performance_manifest["source_commit"] != c["source_commit"]:
            raise ValueError("H3 performance overlay source mismatch")
        for rel, entry in performance_manifest["files"].items():
            if hashlib.sha256((performance_root / rel).read_bytes()).hexdigest() != entry["sha256"]:
                raise ValueError(f"H3 performance overlay checksum mismatch: {rel}")
    identity = {"h3_performance": performance_manifest, "profile": p, "world": world, "tp": 1, "ulysses": 1, "ring": world,
                "encoder_parallel": "replicate", "attention": "fa", "source_commit": c["source_commit"],
                "image_id": c["image_id"], "master_addr": c["master_addr"], "dist_port": c["dist_port"],
                "api_bind": c["api_bind"], "api_port": c["api_port"], "network_mode": n["network_mode"],
                "deployment_id": deployment, "receipts": receipts,
                "fa_sm120_patch_sha256": fa_patch_sha256,
                "ref2va_optional_inputs": optional_manifest}
    fingerprint = hashlib.sha256(canonical(identity)).hexdigest()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,48}", container_prefix):
        raise ValueError("Invalid container prefix")
    name = f'{container_prefix}{n["rank"]}'
    runtime = Path(c["state_root"]) / "runtime" / deployment
    cache = Path(c["state_root"]) / "cache"
    args = ["docker", "run", "--rm", "--name", name, "--gpus", "all", "--network", "host", "--ipc", "host", "--ulimit", "memlock=-1",
            # GB10 shares one 121Gi pool. Uncapped page-cache + 48Gi encoder + 62Gi DiT
            # starved sshd and forced Gateway into RECOVERY_REQUIRED. Cap the cgroup so
            # the container OOMs instead of wedging the host.
            "--memory", "104g", "--memory-swap", "104g",
            "--label", f'h3.profile={p["id"]}', "--label", f"h3.fingerprint={fingerprint}", "--label", f"h3.deployment={deployment}",
            "--workdir", "/work"]
    def mount(src, dst, readonly=True):
        if "," in str(src):
            raise ValueError("Commas are not supported in Docker bind paths")
        args.extend(["--mount", f"type=bind,src={src},dst={dst}" + (",readonly" if readonly else "")])
    mount(KIT, "/kit")
    mount(fa_patch, "/opt/sglang-src/python/sglang/multimodal_gen/runtime/platforms/cuda.py")
    if optional_manifest:
        base = "/opt/sglang-src/python/sglang/multimodal_gen/runtime/pipelines_core/stages/model_specific_stages/minimax_h3"
        for rel in optional_manifest["files"]:
            if performance_manifest and rel == "stages/denoising.py":
                continue
            mount(optional_root / rel, f"{base}/{rel}")
    if performance_manifest:
        for rel, entry in performance_manifest["files"].items():
            mount(performance_root / rel, "/opt/sglang-src/python/sglang/multimodal_gen/" + entry["target"])
    if n["network_mode"]=="mesh":
        mount(Path(n.get("mesh_lib_root","/srv/h3-ring/mesh")), "/opt/nccl-mesh/lib")
    mount(c["base_model_root"], "/models/h3")
    # Official root model_index.json is MiniMaxH3ModularPipeline (3-element
    # specs). SGLang verifies that file before joining --model-variant, and
    # then requires transformer/ and vae/ at repo root. Overlay a 2-tuple
    # MiniMaxH3Pipeline probe so native selection can reach FL2VA/.
    mount(KIT / "config/serve-root-model-index.json", "/models/h3/model_index.json")
    mount(c["assets_root"], "/data/h3")
    mount(cache, "/cache", False)
    mount(runtime, "/work", False)
    mount(log_dir, "/runlogs", False)
    if p["encoder_override"]:
        mount(c["heretic_root"], "/models/heretic")
    if p["transformer_override"]:
        mount(c["hybrid_root"], "/models/hybrid")
    env = {"HOME": "/cache", "HF_HOME": "/cache/huggingface", "CUDA_VISIBLE_DEVICES": "0",
           "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "PYTHONUNBUFFERED": "1",
           "NCCL_SOCKET_IFNAME": "=" + n["control_iface"], "GLOO_SOCKET_IFNAME": n["control_iface"],
           "NCCL_DEBUG": "INFO", "NCCL_DEBUG_SUBSYS": "INIT,NET,GRAPH,COLL", "NCCL_DEBUG_FILE": "/runlogs/nccl.%h.%p.log",
           "TORCH_NCCL_ASYNC_ERROR_HANDLING": "1"}
    candidate = (performance_manifest or {}).get("attention_candidate")
    if candidate:
        if candidate != "m128n64-t256-15s":
            raise ValueError("Unknown H3 attention candidate")
        if world == 4 and p["id"] == "ref2va-official":
            env["H3_ATTENTION_CANDIDATE"] = candidate
    if p.get("memory_placement"):
        if not performance_manifest or "memory_placement.py" not in performance_manifest["files"]:
            raise ValueError("Missing H3 memory placement guard")
        env["H3_MEMORY_PLACEMENT"] = p["memory_placement"]
        env["H3_MEMORY_RUN_ID"] = deployment
    if p.get("attention_probe_candidate"):
        env["H3_ATTENTION_PROBE"] = p["attention_probe_candidate"]
    if p.get("fp8_probe_candidate"):
        if not performance_manifest or "fp8_probe.py" not in performance_manifest["files"]:
            raise ValueError("Missing FP8 probe implementation")
        env["H3_FP8_PROBE"] = p["fp8_probe_candidate"]
    if n["network_mode"] == "ib":
        args += ["--device", "/dev/infiniband"]
        env.update(NCCL_NET="IB", NCCL_IB_DISABLE="0", NCCL_IB_HCA=n["hcas"])
    elif n["network_mode"] == "mesh":
        args += ["--device", "/dev/infiniband"]
        env.update({
            "LD_LIBRARY_PATH": "/opt/nccl-mesh/lib:/opt/h3env/lib/python3.12/site-packages/nvidia/nccl/lib:/usr/local/nvidia/lib:/usr/local/nvidia/lib64:/usr/local/cuda/lib64",
            "NCCL_NET_PLUGIN": "mesh",
            "NCCL_NET": "Mesh",
            "NCCL_ALGO": "Ring",
            "NCCL_RUNTIME_CONNECT": "1",
            "NCCL_SOCKET_FAMILY": "AF_INET",
            "NCCL_MESH_GID_INDEX": str(n.get("mesh_gid_index", 3)),
            "NCCL_MESH_DISABLE_RDMA": "0",
            "NCCL_MESH_DEBUG": "1",
            "NCCL_NVLS_ENABLE": "0",
            "EP_SUPPRESS_NCCL_CHECK": "1",
            "NCCL_IGNORE_CPU_AFFINITY": "1",
        })
    else:
        env.update(NCCL_NET="Socket", NCCL_IB_DISABLE="1")
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    cmd = ["sglang", "serve", "--model-path", "/models/h3", "--model-variant", p["model_variant"],
           "--pipeline-class-name", "MiniMaxH3Pipeline",
           "--num-gpus", str(world), "--nnodes", str(world), "--node-rank", str(n["rank"]),
           "--dist-init-addr", f'{c["master_addr"]}:{c["dist_port"]}', "--tp-size", "1", "--sp-degree", str(world),
           "--ulysses-degree", "1", "--ring-degree", str(world), "--encoder-parallel", "replicate",
           "--attention-backend", "fa", "--performance-mode", p["performance_mode"],
           "--enable-torch-compile", "false", "--use-fsdp-inference", "false",
           "--warmup-mode", p.get("warmup_mode", "off"),
           "--pin-cpu-memory", "false",
           "--dit-offload-prefetch-size", str(p.get("dit_offload_prefetch_size", 0)),
           "--dit-layerwise-resident-layers", str(p.get("dit_layerwise_resident_layers", 0)),
           "--host", c["api_bind"], "--port", str(c["api_port"])]
    if p.get("component_residency"):
        cmd += ["--component-residency", *p["component_residency"]]
    if p.get("memory_placement") == "memory-dit-resident-v1":
        cmd += ["--component-residency", "transformer=resident"]
    if p.get("memory_placement") == "memory-dit-half-v1":
        # Exact pipeline component names: do not leak a DiT group default to
        # auxiliary components. Existing prefetch and placement stay unchanged.
        cmd += ["--layerwise-resident-layers", "transformer=0.5,text_encoder=0,image_encoder=0,vae=0"]
    if p["online_quantization"] != "none":
        cmd += ["--quantization", p["online_quantization"]]
    if p["encoder_override"]:
        cmd += ["--component-paths.text_encoder", "/models/heretic"]
    if p["transformer_override"]:
        cmd += ["--component-weights-paths.transformer", f"/models/hybrid/{HYBRID_FILE}"]
    report = {"profile_id": p["id"], "rank": n["rank"], "world": world,
              "deployment_id": deployment, "fingerprint": fingerprint, "network_mode": n["network_mode"],
              "identity": identity, "container": name, "log_dir": str(log_dir),
              "evidence_scope": "Offline config/bytes only; live startup, transport and video acceptance are still required"}
    return args + [c["image"]] + cmd, report


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=["check", "render", "run", "status", "stop"])
    ap.add_argument("profile", choices=IDS)
    ap.add_argument("--cluster", type=Path, default=KIT/"config/cluster.json")
    ap.add_argument("--node", type=Path, default=KIT/"config/node.json")
    ap.add_argument("--world", type=int, choices=[1,2,4], default=4)
    ap.add_argument("--container-prefix", default="h3-ring-serve-r",
                    help="Reviewed service-manager namespace; defaults to the Ring4 identity")
    ap.add_argument("--deployment-id", required=True, help="Same reviewed ID on all selected nodes")
    ap.add_argument("--full-hash", action="store_true")
    ap.add_argument("--allow-experiment", action="store_true")
    ap.add_argument("--standalone-approved", action="store_true", help="Only after confirming no Gateway/supervisor owns this service")
    ap.add_argument("--report", type=Path)
    a=ap.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}",a.deployment_id):
        ap.error("Use a simple deployment id without slashes")
    c,n,p=load(a.cluster),load(a.node),load(KIT/"profiles"/(a.profile+".json"))
    validate_profile(p)
    if p["experimental"] and not a.allow_experiment:
        raise RuntimeError("Hybrid is disabled by default; finish earlier gates and explicitly pass --allow-experiment")
    if not isinstance(n["rank"],int) or not 0<=n["rank"]<a.world:
        raise ValueError("Rank outside selected world")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,48}", a.container_prefix):
        raise ValueError("Invalid container prefix")
    name=f'{a.container_prefix}{n["rank"]}'
    if a.action in ("status","stop"):
        info=load_docker_container(name)
        labels=info["Config"].get("Labels") or {}
        if labels.get("h3.profile") != a.profile or labels.get("h3.deployment") != a.deployment_id:
            raise RuntimeError("Container labels differ; do not stop an unrelated/old deployment")
        if a.action=="status":
            print(json.dumps({"name":name,"state":info["State"],"labels":labels},indent=2)); return
        if not a.standalone_approved:
            raise RuntimeError("Use the recorded Gateway/supervisor stop; standalone stop needs explicit approval")
        subprocess.run(["docker","stop","--time","60",name],check=True);return
    for key in ("state_root","base_model_root","heretic_root","hybrid_root","assets_root"):
        if not Path(c[key]).is_absolute() or "CHANGE_ME" in c[key]:
            raise ValueError(f"Invalid absolute path: {key}")
    ip=ipaddress.IPv4Address(c["master_addr"])
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast:
        raise ValueError("Head address must be reachable unicast IPv4")
    if c["api_bind"]!="127.0.0.1":
        raise ValueError("V2 requires localhost API; expose only through an authenticated existing gateway")
    for key in ("dist_port","api_port"):
        if not isinstance(c[key],int) or not 1024<=c[key]<=65535:raise ValueError("Bad port")
    if c["dist_port"]==c["api_port"]:raise ValueError("Ports overlap")
    if n["network_mode"] not in ("ib","socket","mesh"):raise ValueError("No implicit custom network plugin support")
    if not n["control_iface"] or "CHANGE_ME" in n["control_iface"]:raise ValueError("Set real control interface")
    subprocess.run(["ip","link","show","dev",n["control_iface"]],check=True,stdout=subprocess.DEVNULL)
    if n["network_mode"] in ("ib","mesh"):
        if not Path("/dev/infiniband").is_dir():
            raise ValueError("Missing /dev/infiniband for RDMA network mode")
    if n["network_mode"]=="ib":
        if not n["hcas"].startswith("=") or "CHANGE_ME" in n["hcas"]:
            raise ValueError("Missing verified RDMA device/HCA mapping")
    if n["network_mode"]=="mesh":
        mesh_root=Path(n.get("mesh_lib_root","/srv/h3-ring/mesh"))
        if not mesh_root.is_dir() or not (mesh_root/"libnccl-net.so").is_file():
            raise ValueError("Missing Mesh NCCL plugin library root")
        if not isinstance(n.get("mesh_gid_index"),int):
            raise ValueError("Missing integer mesh_gid_index for Mesh mode")
    if c["source_commit"]!="e970453b433e1467e22f6d925f270563417540b5":
        raise ValueError("V2 is reviewed against the pinned SGLang commit; audit/revise the plan before upgrading")
    image=json.loads(subprocess.check_output(["docker","image","inspect",c["image"]]))[0]
    if image["Architecture"]!="arm64":
        raise ValueError("Image ID/architecture mismatch")
    if image["Id"]!=c["image_id"]:
        layers="".join(image.get("RootFS",{}).get("Layers") or [])
        expected=c.get("image_layers_sha256")
        if not expected or hashlib.sha256(layers.encode()).hexdigest()!=expected:
            raise ValueError("Image ID/architecture mismatch")
    if not Path(c["assets_root"]).is_dir():raise ValueError("Create the dedicated assets directory first")
    receipts={}
    for asset in p["asset_ids"]:
        receipt=load(Path(c["state_root"])/"manifests"/(asset+".receipt.json"))
        if receipt.get("asset")!=asset or receipt.get("repo")!=ASSETS[asset][0]:raise ValueError("Wrong asset receipt")
        if not re.fullmatch(r"[0-9a-f]{40}",receipt.get("revision","")):raise ValueError("Unpinned model revision")
        verify_receipt(roots(c)[asset],receipt,full_hash=a.full_hash)
        receipts[asset]=receipt
    official_revs={v["revision"] for k,v in receipts.items() if k.startswith("official-")}
    if len(official_revs)>1:raise ValueError("Official FL/Ref revisions differ")
    if p["encoder_override"]:
        check_encoder(Path(c["base_model_root"])/"Ref2VA/text_encoder",Path(c["heretic_root"]))
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log=Path(c["state_root"])/"logs"/a.deployment_id/f'r{n["rank"]}-{stamp}-{os.getpid()}'
    cmd,report=build(c,n,p,receipts,a.world,a.deployment_id,log,a.container_prefix)
    report["full_hash_verified_now"]=a.full_hash
    report["command"]=cmd
    if a.report:
        a.report.parent.mkdir(parents=True,exist_ok=True)
        with a.report.open("x") as f:json.dump(report,f,indent=2)
    if a.action=="check":print(json.dumps(report,ensure_ascii=False,indent=2));return
    if a.action=="render":
        for d in (log,Path(c["state_root"])/"runtime"/a.deployment_id,Path(c["state_root"])/"cache"):
            d.mkdir(parents=True,exist_ok=True)
        (log/"launch.json").write_text(json.dumps(report,indent=2))
        print(shlex.join(cmd));return
    if not a.standalone_approved:
        raise RuntimeError("Use render to register this exact foreground command with the existing manager; no bypass")
    exists=subprocess.run(["docker","container","inspect",name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if exists.returncode==0:raise RuntimeError("An old same-rank H3 container exists; drain/stop via its manager first")
    for d in (log,Path(c["state_root"])/"runtime"/a.deployment_id,Path(c["state_root"])/"cache"):
        d.mkdir(parents=True,exist_ok=True)
    (log/"launch.json").write_text(json.dumps(report,indent=2))
    print("Launching reviewed profile; logs="+str(log),flush=True)
    with (log/"stdout.log").open("w") as f:
        proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
        try:
            for line in proc.stdout:
                f.write(line);f.flush();print(line,end="",flush=True)
            code=proc.wait()
        except KeyboardInterrupt:
            print("Interrupted. Verify the named container/ranks through the service manager; do not restart blindly.",file=sys.stderr)
            raise
    raise SystemExit(code)


def load_docker_container(name):
    return json.loads(subprocess.check_output(["docker","container","inspect",name]))[0]

if __name__=="__main__":main()
