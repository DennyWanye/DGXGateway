# Two-node DGX Spark deployment playbook

> 2026-09-11 operator-requested retirement: the V4 Flash, V4 Flash Vision Exp,
> head single-node fallback weights, and worker repair shard backups have been
> deleted. They are no longer available as rollback models. Historical runtime
> archives and service definitions remain; a STOPPED Gateway registration does
> not prove that its weights are installed. See the current
> replacement checkpoint（历史内部记录，公开版不包含）.

This is the model-neutral baseline for deploying one distributed inference
engine across the two DGX Spark systems in this project. Use it for DeepSeek,
MiniMax, and future models. Keep model-specific image, checkpoint, tokenizer,
quantization, context, speculative-decoding, and API decisions in a separate
locked plan.

The purpose of this split is simple: qualify the cluster once, then reuse its
proven infrastructure without assuming that a runtime which supports one model
also supports another.

## Cluster inventory and roles

| Role | Hostname | Management address | Responsibility |
| --- | --- | --- | --- |
| Head / rank 0 | `dgx-head` | `192.0.2.30` | Lifecycle owner, distributed rendezvous, internal inference API, authenticated LAN gateway |
| Worker / rank 1 | `dgx-worker` | `192.0.2.31` | Headless model rank; no public inference listener |

Both hosts use the `operator` account. Mac-to-host SSH public-key access is
configured. Passwordless `sudo -n` is available by explicit operator choice.
Head-to-worker cluster SSH must still be established with a dedicated key and
strictly pinned host keys before a distributed launcher is allowed to run.

The management LAN is for SSH and the authenticated client API. Model
collectives use the direct ConnectX fabric and must never fall back to Wi-Fi,
Docker bridges, VPN/TUN devices, or the management network.

## Current fabric standard

The selected interfaces are currently:

| Rail | Head Ethernet / HCA | Worker Ethernet / HCA | Planned addresses |
| --- | --- | --- | --- |
| 1 | `enp1s0f0np0` / `rocep1s0f0` | `enp1s0f0np0` / `rocep1s0f0` | `192.0.2.33/30` ↔ `192.0.2.34/30` |
| 2 | `enP2p1s0f0np0` / `roceP2p1s0f0` | `enP2p1s0f0np0` / `roceP2p1s0f0` | `192.0.2.35/30` ↔ `192.0.2.36/30` |

Treat interface names as observed state, not permanent identity. Re-read
`ibdev2netdev`, link speed, HCA state, MAC address, and PCI mapping on both
hosts before every persistent network change.

Project policy for each direct rail:

- private point-to-point `/30`;
- MTU 9000 on both endpoints;
- no DHCP, gateway, DNS, or default route;
- role-specific netplan with mode 0600;
- backup and timed rollback guard before `netplan apply`;
- apply one host at a time while a second management SSH session verifies
  reachability;
- verify both directions with ordinary ping and
  `ping -M do -s 8972 -c 20 <peer>` before NCCL;
- repeat link, route, GID, and DF-jumbo checks after every reboot.

NVIDIA's current managed two-Spark profile also qualifies two active 200-Gb/s
ConnectX-7 rails, requires MTU at least 9000 and a private `/30` on each rail,
but binds its current runtime profile to the first qualified rail. Two links
reporting 200000 Mb/s do not prove 400-Gb/s aggregate application throughput.

## Reusable architecture

```text
trusted LAN client
        |
        | authenticated, allowlisted OpenAI-compatible routes
        v
head gateway: 192.0.2.30:11434
        |
        | host network, loopback upstream only
        v
rank-0 runtime: 127.0.0.1:<internal-port>
        |
        | rendezvous/control on fabric rail 1
        | NCCL data on the measured-best exact HCA set
        v
rank-1 worker: headless, no HTTP listener
```

Use tensor parallelism only when the selected model/runtime explicitly supports
partitioning across the two GB10 GPUs. Two independent model servers are
replicas; they do not pool memory. Pipeline parallelism, expert parallelism,
or another executor is a model/runtime-specific choice and must be validated
rather than inferred from this topology.

## Cluster qualification workflow

### 1. Preserve the baseline

Before changing either host, record:

- expected hostname and user;
- OS, kernel, NVIDIA driver, Docker, container toolkit, GPU, and HCA versions;
- management routes, UFW, listeners, and active services;
- disk capacity and existing model trees;
- current production API and one bounded successful inference;
- repository status and hashes/diffs for dirty files that may be touched;
- enabled state and content hash of every service that rollback may restore.

Do not perform an automatic OS, kernel, driver, firmware, or Docker upgrade to
make two hosts look identical. First test the pinned container on both current
stacks. Any required alignment is a separate package-pinned change with local
console recovery.

### 2. Establish cluster SSH deliberately

Use a dedicated head-to-worker key. Verify the worker fingerprint out of band,
then pin both its management and fabric identities. The launcher must pass
`BatchMode=yes` tests without prompts. Never disable host-key checking to make
automation pass.

The upstream launcher may call Docker without `sudo`. If `operator` is added to
the `docker` group, document that this is root-equivalent access and require a
fresh login plus harmless `docker compose version` and image queries on both
hosts.

### 3. Qualify each fabric rail and choose the production HCA set

First prove each rail independently with ping, DF-jumbo ping, TCP, RDMA, and
HCA counters. Then run the same locked `nccl-tests` image for three candidates:

1. exact HCA A;
2. exact HCA B;
3. exact dual-HCA selector.

For each candidate, discard one warmup process and retain five independent
runs of:

```bash
all_reduce_perf -b 8M -e 1G -f 2 -g 1 -w 10 -n 50 -c 1
```

For each run, take the median `busbw` from the 256M, 512M, and 1G rows. The
candidate score is the median of the five run medians. Retain every row,
correctness result, exit status, NCCL log, and before/after HCA counter set.

Fail a candidate on any of the following:

- nonzero exit or data-check failure;
- NCCL warning/error, connection retry, CQE/transport error, or socket fallback;
- an unintended network interface;
- an increase in retry, discard, error, or timeout counters;
- `(max(run_score)-min(run_score))/median(run_score) > 0.15`.

Use dual HCA only when it remains stable and scores at least 10% above the best
single HCA. Otherwise use the faster valid single HCA. If the two single-HCA
scores differ by less than 3%, prefer `rocep1s0f0` deterministically. Record
the selected exact head and worker values in the deployment lock and runtime
environment. A second qualified rail may remain a spare.

The initial standalone `ib_write_bw` spike on these hosts measured only about
13.5 Gb/s at MTU 1500 and 14.5 Gb/s at MTU 9000. Therefore link rate is not an
acceptance number; NCCL and end-model throughput are the gates.

### 4. Acquire immutable artifacts

Every deployment must lock:

- source repository and commit;
- container index, architecture-specific manifest, config digest, and image ID;
- model repository and immutable revision;
- full file closure, relative paths, sizes, and hashes;
- tokenizer, encoding, chat template, remote-code files, and generation config;
- local integration patches and their target source hashes.

Download a model once on the head, verify it completely, and replicate the
verified tree over the fabric with resumable transfer. Re-run the full manifest
on the worker. Both ranks use the same absolute host path and the same container
mount path. Runtime starts with offline Hub flags and must not depend on a
mutable branch, tag, symlink, or Internet request.

Large Hugging Face payloads on the head must use UID 996 (`dgx-hf-direct`),
whose route resolves through `wlP9s9`. The normal `operator` identity remains on
the `dgx-vpn` policy. The reusable pattern is:

- Mac performs metadata and signed-URL control requests;
- head downloads large payloads under the direct UID;
- signed URLs and tokens never enter logs;
- a file remains `.part` until size and digest pass;
- low-speed/dead-CDN attempts exit after a bounded retry count;
- the controller refreshes the signed URL, selects another reachable CDN IP,
  and resumes the same partial file;
- 401/403 is a hard pause, never a skipped shard.

This egress policy is specific to artifact acquisition. Distributed inference
traffic still uses the direct fabric.

### 5. Validate the runtime on both hosts

Before loading a full model, use the exact production image to prove on each
host:

- `linux/arm64` and GB10/SM121 compatibility;
- CUDA device access and required kernels;
- `/dev/infiniband`, NCCL, and the selected distributed executor;
- required model architecture and quantization plugins;
- tokenizer/encoding and remote-code import;
- identical runtime and integration-patch hashes;
- no unexpected Internet access under offline flags.

Do not use “the container starts” as proof that the model architecture is
supported. A CPU-only config parse is also insufficient; execute a bounded CUDA
and NCCL probe on both driver stacks.

### 6. Use one paired-rank lifecycle owner

The worker rank starts first. It must remain Running for 15 seconds with zero
restarts, a live `--node-rank 1 --headless` process, and no fatal/NCCL/transport
errors before rank 0 starts. Bound this state to 120 seconds. Bound full API
readiness to 20 minutes unless a model-specific plan justifies another value.

On any start failure, capture both ranks' inspect/log/flight-recorder state and
remove only the exact labeled Compose project. Do not use broad `pkill`, Docker
prune, or name globs.

Use system-level units under `/etc/systemd/system`, running as `operator:operator`
with the explicitly approved supplementary Docker group. The main unit is the
single lifecycle owner:

- `Type=oneshot`;
- `RemainAfterExit=yes`;
- `Restart=no`;
- explicit paired `ExecStart` and `ExecStop`;
- `After`/`Wants` for `network-online.target` and `docker.service`;
- Compose restart policy `no`.

A separate locked watchdog may reconcile only after repeated health failures,
outside maintenance, with a cooldown and one worker-first paired restart.
Never combine Compose auto-restart, systemd auto-restart, and a watchdog; that
creates stale ranks and restart storms.

### 7. Isolate and authenticate the API

Bind the inference runtime to head loopback on an internal port. Run the
gateway with host networking so its `127.0.0.1` upstream is the host's runtime,
not a bridge container's loopback. Bind production only to
`192.0.2.30:11434`.

The gateway owns authentication and an explicit route allowlist. Keep its
bearer secret in a mode-0600 head-only file; never put it in a shared runtime
environment that is copied to rank 1. Deny the internal runtime port from the
LAN even when it is loopback-bound as defense in depth.

Do not assume a vLLM API key protects every route. Test correct, missing, and
wrong credentials against every allowed and denied endpoint, including
operational, tokenize, scoring, metrics, and alternate inference routes.

Stage the gateway at `127.0.0.1:11435`. Cutover replaces only that gateway
listener with `192.0.2.30:11434`; the already-qualified model ranks stay
hot.

### 8. Prove the service, failure modes, and rollback

Every model deployment needs evidence for:

- inventory/model-list response;
- non-streaming and streaming generation;
- exact API wire contract used by real clients;
- structured output, reasoning, and tools when advertised;
- tokenizer-rendered context ladder with output-token headroom;
- cold single-stream and representative concurrent throughput;
- worker loss, explicit request failure, and worker-first recovery;
- worker reboot, then head reboot;
- multi-hour soak for long generations and terminal stream events;
- unauthorized and direct-upstream rejection;
- transactional cutover and rollback.

Keep the old service, models, and unit files intact until every gate passes.
Rollback must restore the local head service even when the worker is
unreachable. Remote rank cleanup is a separate degraded-state follow-up and
must not block local service restoration.

## Model adapter checklist

Create one model-specific plan and lock manifest for every new deployment.
Answer all of these before implementation:

| Decision | Required evidence |
| --- | --- |
| Architecture support | Exact runtime commit/image imports and executes the model class on GB10 |
| Weight format | Safetensors, GGUF, FP8/NVFP4/BF16, or other format is supported without an unreviewed conversion |
| Distributed method | TP/PP/EP degree is supported; tensor/expert dimensions partition correctly across two ranks |
| Memory budget | Weights, runtime workspace, CUDA graphs, KV cache, and safety margin fit both hosts |
| Context | Configured ceiling and practical validated window are stated separately |
| Tokenizer/remote code | Exact revision, hashes, container paths, and offline behavior are proven |
| Speculative decoding | Draft/MTP compatibility, minimum depth, tensor coupling, and acceptance behavior are model-specific |
| API contract | Chat/Responses/tool/reasoning features are tested with the actual client |
| Security | Gateway route matrix and secret location are locked; worker receives no public secret |
| License/gate | Terms are accepted and redistribution/use obligations are recorded |
| Performance | Fixed prompts, outputs, concurrency, warmup, repetitions, and thresholds are defined before tuning |
| Recovery | Model-specific load timeout, health signal, stale-rank cleanup, and rollback are executable |

Never copy DeepSeek-specific settings such as `nvfp4_ds_mla`, DSpark MTP depth,
DeepSeek tokenizer mode, reasoning parser, or hotfixes into another model's
adapter unless that model's pinned runtime explicitly requires and validates
them.

## Applying this to current and future models

- DeepSeek V4 Flash: use the model-specific plan at
  `../plans/deepseek-v4-flash-dspark-2node.md`（历史内部记录，公开版不包含）.
  It inherits this cluster baseline and adds the selected checkpoint, runtime,
  DSpark/MTP, 1M-context, Responses, benchmark, and cutover details.
- MiniMax H3: reuse the host roles, SSH, fabric qualification, artifact
  integrity, lifecycle, gateway, evidence, and rollback sections here. Create a
  separate adapter plan only after confirming the exact H3 checkpoint, weight
  format, model implementation, GB10 kernels, supported parallelism, memory/KV
  budget, tokenizer/remote code, context, and API behavior. Do not assume the
  DeepSeek Anemll image supports it.
- Later models: follow the same rule—reuse proven infrastructure, revalidate
  the model adapter.

## Current state versus planned state

As observed on 2026-08-29:

- both selected Ethernet links report 200000 Mb/s and both RoCE HCAs are
  `ACTIVE`;
- reversible IP/GID spikes passed on both rails and found IP-backed RoCEv2 GID
  index 3 on the four selected HCAs;
- persistent `/30` addresses and MTU 9000 are not yet installed;
- deterministic single-A/single-B/dual-HCA NCCL qualification is still pending;
- Mac-to-both-host SSH works, while dedicated head-to-worker trust is pending;
- host OS/kernel/driver versions differ and require container-level validation,
  not an automatic upgrade;
- the existing production service remains the rollback baseline until a new
  two-node deployment passes its model-specific gates.

Do not let documentation of a planned address, service, or model be interpreted
as proof that it is already deployed. Update this section only from retained
evidence after each phase passes.

## Sources

- [NVIDIA: Set Up vLLM on Two DGX Sparks](https://docs.nvidia.com/nemoclaw/user-guide/openclaw/inference/local-inference/set-up-vllm-on-two-dgx-sparks)
- [MiaAI-Lab: DeepSeek V4 Flash recipe for two DGX Sparks](https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark)
- `AGENTS.md`（历史内部记录，公开版不包含） for current SSH identities and agent safety rules
- `plans/deepseek-v4-flash-dspark-2node.md`（历史内部记录，公开版不包含）
  for the first model-specific implementation plan built on this baseline
