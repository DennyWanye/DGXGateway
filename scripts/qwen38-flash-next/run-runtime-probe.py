#!/usr/bin/env python3
"""Run the finite two-node probe under the existing Gateway resource authority."""
import json
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from DGXGateway.dgx_gateway.authority import GatewayLease
from DGXGateway.dgx_gateway.config import load_config
from DGXGateway.dgx_gateway.maintenance import set_mutation_fence
from DGXGateway.dgx_gateway.ssh import FixedSSH

IMAGE = "sha256:393454aa5bc87f02e7a5fe2eed0d833e295e7c3bb97f0b20f6ff66689df0ed90"
REMOTE = "/home/operator/dgx-models/qwen38-flash-next/control/runtime/probe-nccl.py"
TASK = "qwen-runtime-probe-20260910"


def main(model_smoke=False, kv8=False, functional=False):
    task = "qwen-functional-20260910" if functional else ("qwen-model-smoke-kv8-20260910" if kv8 else ("qwen-model-smoke-20260910" if model_smoke else TASK))
    prefix = "functional" if functional else ("model-smoke-kv8" if kv8 else ("model-smoke" if model_smoke else "runtime-probe"))
    port = "29542" if model_smoke else "29541"
    config_path = Path.home() / ".config/dgx-cluster-gateway/gateway.toml"
    config = load_config(config_path)
    ssh = FixedSSH()
    evidence = ROOT / "plans/qwen38-flash-next/evidence/deploy"
    with sqlite3.connect("file:" + str(config.state_dir / "gateway.sqlite3") + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT value FROM metadata WHERE key='mutation_fenced'").fetchone()
    if row not in (None, ("0",)):
        raise RuntimeError("existing maintenance fence must not be taken over")
    with urllib.request.urlopen(f"http://{config.bind}:{config.port}/api/v1/snapshot", timeout=25) as response:
        before = json.load(response)
    assert before["active_local_model"]["state"] == "NONE" and before["lock"]["state"] == "UNLOCKED"
    assert before["current_operation"] is None and before["residency"]["state"] == "NONE"
    (evidence / (prefix + "-before.json")).write_text(json.dumps(before, indent=2))
    for node in config.nodes:
        ssh.verify_role(node)
        result = ssh.run(node, ["sudo", "-n", "docker", "image", "inspect", IMAGE, "--format", "{{.Id}} {{.Architecture}}"])
        assert result.returncode == 0 and result.stdout.strip() == IMAGE + " arm64"
        result = ssh.run(node, ["ss", "-H", "-ltn", "sport = :" + port])
        assert result.returncode == 0 and not result.stdout.strip()
        if model_smoke:
            result = ssh.run(node, ["ss", "-H", "-ltn", "sport = :18890"])
            assert result.returncode == 0 and not result.stdout.strip()
    lease = GatewayLease(lock_cli=config.lock_cli, lock_root=config.lock_root, token_dir=config.token_dir,
                         operation_id=task, purpose="bounded Qwen " + prefix, lease_seconds=600)
    acquired = False
    children = []
    streams = []
    model_succeeded = False
    set_mutation_fence(config_path, True)
    try:
        receipt = lease.acquire()
        acquired = True
        lease.start_heartbeat(interval_seconds=30)
        (evidence / (prefix + "-lease.json")).write_text(json.dumps(receipt, indent=2))
        for node in reversed(config.nodes):
            lease.check_supervision()
            rank = 0 if node.role == "head" else 1
            name = task + "-" + node.role
            env = {"RANK": str(rank), "WORLD_SIZE": "2", "MASTER_ADDR": "192.0.2.33", "MASTER_PORT": port,
                   "NCCL_SOCKET_IFNAME": "=enp1s0f0np0", "GLOO_SOCKET_IFNAME": "enp1s0f0np0",
                   "NCCL_IB_HCA": "=rocep1s0f0:1,roceP2p1s0f0:1", "NCCL_IB_GID_INDEX": "3",
                   "NCCL_CROSS_NIC": "0", "NCCL_IB_DISABLE": "0", "NCCL_DEBUG": "INFO",
                   "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
            args = ["sudo", "-n", "docker", "run", "--rm", "--name", name, "--label", "com.dgx.task=" + task,
                    "--gpus=all", "--network=host", "--shm-size=" + ("16g" if model_smoke else "1g"),
                    "--ulimit", "memlock=-1:-1", "--device=/dev/infiniband"]
            if model_smoke:
                env["VLLM_HOST_IP"] = "192.0.2.33" if rank == 0 else "192.0.2.34"
                env["TRITON_CACHE_DIR"] = "/root/.cache/triton"
                env["CUDA_CACHE_PATH"] = "/root/.cache/cuda"
                args.extend(["--mount", "type=bind,src=/home/operator/dgx-models/qwen38-flash-next/weights/qwen3.8-flash-next-abliterated-NVFP4-plefp8,dst=/model,readonly", "--mount", "type=bind,src=/home/operator/dgx-models/qwen38-flash-next/control/runtime/cache,dst=/root/.cache", "--entrypoint=vllm"])
            else:
                args.extend(["--mount", "type=bind,src=" + REMOTE + ",dst=/probe.py,readonly", "--entrypoint=python3"])
            for key, value in env.items():
                args.extend(["--env", key + "=" + value])
            if model_smoke:
                args.extend([IMAGE, "serve", "/model", "--served-model-name", "qwen38-flash-next",
                             "--distributed-executor-backend", "mp", "--nnodes", "2", "--node-rank", str(rank),
                             "--master-addr", "192.0.2.33", "--master-port", port,
                             "--tensor-parallel-size", "2", "--pipeline-parallel-size", "1",
                             "--max-model-len", "32768", "--max-num-seqs", "2", "--max-num-batched-tokens", "4096",
                             "--gpu-memory-utilization", "0.70" if kv8 else "0.80", "--enforce-eager", "--no-enable-flashinfer-autotune",
                             "--compilation-config", '{"mode":0}', "--moe-backend", "marlin",
                             "--load-format", "safetensors", "--safetensors-load-strategy", "lazy",
                             "--limit-mm-per-prompt", '{"image":1,"video":0}',
                             "--reasoning-parser", "qwen3", "--tool-call-parser", "qwen3_coder", "--enable-auto-tool-choice"])
                if kv8:
                    # In this pinned runtime utilization still gates startup free memory,
                    # while explicit bytes control the actual KV allocation (gpu_worker.py).
                    args.extend(["--kv-cache-memory-bytes", "8589934592"])
                args.extend(["--headless"] if rank else ["--host", "127.0.0.1", "--port", "18890"])
            else:
                args.extend([IMAGE, "/probe.py"])
            stream = (evidence / (prefix + "-" + node.role + ".log")).open("w")
            streams.append(stream)
            children.append(subprocess.Popen(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                                               f"{node.user}@{node.address}", shlex.join(args)], stdout=stream, stderr=subprocess.STDOUT))
            if model_smoke and rank == 1:
                for _ in range(15):
                    lease.check_supervision()
                    assert children[-1].poll() is None, "worker exited before head launch"
                    time.sleep(1)
        deadline = time.monotonic() + (1200 if model_smoke else 300)
        next_health = 0
        while any(child.poll() is None for child in children):
            lease.check_supervision()
            if any(child.poll() not in (None, 0) for child in children):
                raise RuntimeError("runtime probe rank failed; inspect saved logs")
            if time.monotonic() >= deadline:
                raise TimeoutError(prefix + " readiness deadline exceeded")
            if model_smoke and time.monotonic() >= next_health:
                next_health = time.monotonic() + 10
                for node in config.nodes:
                    memory = ssh.run(node, ["awk", '/^MemAvailable:/ {print $2}', "/proc/meminfo"])
                    assert memory.returncode == 0 and int(memory.stdout.strip()) >= 16 * 1024 * 1024, "node has less than 16 GiB available"
                head = next(node for node in config.nodes if node.role == "head")
                health = ssh.run(head, ["curl", "-fsS", "--max-time", "3", "http://127.0.0.1:18890/v1/models"])
                if health.returncode == 0:
                    models = json.loads(health.stdout)
                    assert any(row["id"] == "qwen38-flash-next" for row in models["data"])
                    (evidence / (prefix + "-api-models.json")).write_text(health.stdout)
                    lease.check_supervision()
                    if functional:
                        log = (evidence / 'functional-api-controller.log').open('w')
                        client = subprocess.Popen(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                            f'{head.user}@{head.address}', 'python3 /home/operator/dgx-models/qwen38-flash-next/control/runtime/functional-api-probe.py'], stdout=log, stderr=subprocess.STDOUT)
                        try:
                            test_deadline = time.monotonic() + 1200
                            while client.poll() is None:
                                lease.check_supervision()
                                if time.monotonic() >= test_deadline:
                                    raise TimeoutError('functional API suite exceeded 20 minutes')
                                for node in config.nodes:
                                    memory = ssh.run(node, ['awk', '/^MemAvailable:/ {print $2}', '/proc/meminfo'])
                                    assert memory.returncode == 0 and int(memory.stdout.strip()) >= 16 * 1024 * 1024, 'functional test memory guard'
                                time.sleep(5)
                            assert client.returncode == 0, 'functional transport checks failed; inspect retained results'
                        finally:
                            if client.poll() is None:
                                client.terminate()
                                client.wait(timeout=10)
                            log.close()
                        model_succeeded = True
                        print('FUNCTIONAL_TRANSPORT_COMPLETE_SEMANTIC_REVIEW_PENDING', flush=True)
                        break
                    payload = {"model": "qwen38-flash-next", "messages": [{"role": "user", "content": "请用中文简短回答：17乘以19等于多少？"}], "max_tokens": 256, "temperature": 0}
                    answer = ssh.run(head, ["curl", "-fsS", "--max-time", "120", "-H", "Content-Type: application/json", "--data-binary", json.dumps(payload, ensure_ascii=False), "http://127.0.0.1:18890/v1/chat/completions"], timeout=130)
                    (evidence / (prefix + "-answer.json")).write_text(answer.stdout)
                    assert answer.returncode == 0 and json.loads(answer.stdout).get("choices"), "first completion failed"
                    model_succeeded = True
                    print("MODEL_LOAD_AND_FIRST_COMPLETION_PASSED", flush=True)
                    break
            time.sleep(2)
        if not model_smoke:
            assert all(child.returncode == 0 for child in children), "probe process failed"
            print("TWO_RANK_PROBE_EXIT_ZERO", flush=True)
        elif not model_succeeded:
            raise RuntimeError("model ranks exited without a verified completion")
    finally:
        clean = True
        for node in config.nodes:
            name = task + "-" + node.role
            result = ssh.run(node, ["sudo", "-n", "docker", "ps", "-a", "--filter", "name=^/" + name + "$", "--format", "{{.ID}}"], timeout=20)
            if result.returncode:
                clean = False
                continue
            if result.stdout.strip():
                check = ssh.run(node, ["sudo", "-n", "docker", "inspect", name, "--format", '{{index .Config.Labels "com.dgx.task"}}'])
                if check.returncode or check.stdout.strip() != task:
                    clean = False
                    continue
                result = ssh.run(node, ["sudo", "-n", "docker", "rm", "-f", name], timeout=40)
                clean = clean and result.returncode == 0
            proof = ssh.run(node, ["sudo", "-n", "docker", "ps", "-a", "--filter", "name=^/" + name + "$", "--format", "{{.ID}}"])
            clean = clean and proof.returncode == 0 and not proof.stdout.strip()
        for child in children:
            child.wait(timeout=15)
        for stream in streams:
            stream.close()
        if not clean:
            raise RuntimeError("probe cleanup unproven; keep fence and lease for recovery")
        if acquired:
            (evidence / (prefix + "-release.json")).write_text(json.dumps(lease.release(), indent=2))
        set_mutation_fence(config_path, False)
        print("PROBE_CONTAINERS_ABSENT_LEASE_RELEASED_FENCE_RESTORED", flush=True)


if __name__ == "__main__":
    if sys.argv[1:] not in ([], ["--model-smoke"], ["--model-smoke-kv8"], ["--functional"]):
        raise SystemExit("unsupported probe mode")
    main(model_smoke=bool(sys.argv[1:]), kv8=sys.argv[1:] in (["--model-smoke-kv8"], ["--functional"]), functional=sys.argv[1:] == ["--functional"])
