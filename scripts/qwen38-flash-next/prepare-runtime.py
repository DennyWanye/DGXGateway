#!/usr/bin/env python3
"""Build the pinned PLE patch offline. Does not expose GPUs or start a service."""
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import time

BASE = "sha256:d464f3b466fa9c45ddbff8a812e80564503b6879a9fd95c1a47514f3f0df5a4a"
PATCH_SHA = "1c65cca1301890183085dadc9623009440d774d6d344a395511dbf4c6433268f"
ROOT = Path("/home/operator/dgx-models/qwen38-flash-next")
BASE_TAG = "qwen38-flash-next:base-3b0e188ffceb"
FINAL_TAG = "qwen38-flash-next:plefp8-candidate-20260910"


def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def main():
    assert socket.gethostname() == "dgx-head"
    if sys.argv[1:] == ["--wait-download"]:
        deadline = time.monotonic() + 7200
        while not (ROOT / "oci/base-pinned/manifest.json").is_file():
            if time.monotonic() >= deadline:
                raise TimeoutError("pinned OCI download is incomplete; no build attempted")
            time.sleep(15)
    elif sys.argv[1:]:
        raise ValueError("only --wait-download is supported")
    control = ROOT / "control/runtime"
    evidence = control / "evidence"
    evidence.mkdir(exist_ok=True)
    verified = run([sys.executable, str(control / "verify-runtime.py"), str(ROOT / "oci/base-pinned")], capture_output=True, text=True)
    receipt = json.loads(verified.stdout)
    assert receipt["passed"] is True and receipt["image_id"] == BASE
    (evidence / "base-closure.json").write_text(verified.stdout)
    patch = control / "fix-qwen38fn-nvfp4-ple-fp8.sh"
    assert hashlib.sha256(patch.read_bytes()).hexdigest() == PATCH_SHA
    archive = ROOT / "oci/base-pinned.tar"
    # Conversion uses local verified blobs only; Docker load verifies layer DiffIDs.
    if archive.exists():
        raise FileExistsError("archive already exists; inspect previous attempt before resuming")
    run(["sudo", "-n", "skopeo", "copy", "dir:" + str(ROOT / "oci/base-pinned"),
         "docker-archive:" + str(archive) + ":" + BASE_TAG], timeout=1200)
    run(["sudo", "-n", "docker", "load", "--input", str(archive)], timeout=1200)
    base = json.loads(run(["sudo", "-n", "docker", "image", "inspect", BASE_TAG], capture_output=True, text=True).stdout)[0]
    assert (base["Id"], base["Os"], base["Architecture"]) == (BASE, "linux", "arm64")
    assert base["RootFS"]["Layers"] == receipt["rootfs_diff_ids"]
    target = "/usr/local/lib/python3.12/dist-packages/vllm/models/qwen3_8_flash_next/nvidia/ple_layer.py"
    source = run(["sudo", "-n", "docker", "run", "--rm", "--network=none", "--entrypoint=cat", BASE, target], capture_output=True, text=True).stdout
    (evidence / "ple_layer.before.py").write_text(source)
    (control / "Dockerfile").write_text(
        "FROM " + BASE_TAG + "\n"
        "COPY fix-qwen38fn-nvfp4-ple-fp8.sh /opt/qwen-ple-fp8-patch.sh\n"
        "RUN bash /opt/qwen-ple-fp8-patch.sh\n"
        'LABEL com.dgx-spark.model-id="qwen38-flash-next"\n'
        'LABEL com.dgx-spark.base-manifest="3b0e188ffceb3d07e09c3cb5215433a0020eacf02d7f882ed3a8bfd15454477e"\n'
    )
    run(["sudo", "-n", "docker", "build", "--network=none", "--pull=false", "--tag", FINAL_TAG, str(control)], timeout=900)
    result = json.loads(run(["sudo", "-n", "docker", "image", "inspect", FINAL_TAG], capture_output=True, text=True).stdout)[0]
    assert (result["Os"], result["Architecture"]) == ("linux", "arm64")
    source_after = run(["sudo", "-n", "docker", "run", "--rm", "--network=none", "--entrypoint=cat", result["Id"], target], capture_output=True, text=True).stdout
    (evidence / "ple_layer.after.py").write_text(source_after)
    (evidence / "patched-image.json").write_text(json.dumps({
        "image_id": result["Id"], "tag": FINAL_TAG, "base_image_id": BASE,
        "patch_sha256": PATCH_SHA, "architecture": "arm64",
        "ple_before_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "ple_after_sha256": hashlib.sha256(source_after.encode()).hexdigest(),
        "gpu_tested": False,
    }, indent=2) + "\n")
    print(json.dumps({"status": "OFFLINE_BUILD_COMPLETE_GPU_UNTESTED", "image_id": result["Id"]}), flush=True)


if __name__ == "__main__":
    main()
