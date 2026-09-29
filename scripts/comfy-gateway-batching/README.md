# DGX native candidate batches

MF05 runtime nodes. Copy only this directory's `__init__.py` into an enabled Comfy custom-node directory through the Gateway-controlled deployment procedure. This source edit does not deploy/restart anything. Requires existing Comfy/PyTorch/aiohttp; no extra model or service.

## Nodes and graph contract

`DGXSeedBatchKSampler` takes every ordinary `KSampler` input, plus required `candidate_indices` (STRING JSON, default `[0,1]`, a nonempty list of unique nonnegative integer original indices; user-selected group size). Optional `after_batch` is `DGX_BATCH_INFO`; hidden `unique_id` is `UNIQUE_ID`. Returns `(LATENT, DGX_BATCH_INFO)`.

Each candidate uses `seed=(base_seed+original_index)%2**64`. Each single source latent gets its own `prepare_noise(single_latent, candidate_seed)` call, followed by one concatenated-noise batch sampling call. Source latent must have batch size 1. No serial per-candidate denoising and no retry. This preserves initial-noise seed semantics across 2+2 groups and failure subsets; floating-point computation may differ from separate single-image execution.

`DGXBatchSaveImage` takes `images: IMAGE`, `batch_info: DGX_BATCH_INFO`, `filename_prefix: STRING`; hidden `unique_id`, `prompt`, `extra_pnginfo`. Uses native `SaveImage`, returns `{'ui':{'images':[native image receipts]}, 'result':(saved_batch_info,)}`. It is an `OUTPUT_NODE`; the returned dependency can be consumed by the next batch sampler.

For four candidates:

1. sampler A candidate_indices=`[0,1]` → VAE Decode → save A.
2. sampler B candidate_indices=`[2,3]`, after_batch=`[save A node ID,0]` → VAE Decode → save B.
3. Both samplers use the same model, conditioning, single source latent and base seed. Save's image array position maps to that group's original indices array position. Never infer candidate order by directory listing.

## Read-only progress

`GET /dgx/batch-progress/{prompt_id}?client_id={gateway_client_id}`

```json
{
  "version": 1,
  "prompt_id": "native-Comfy-prompt-id",
  "client_id": "gateway-client-id",
  "groups": {
    "sampler-node-id": {
      "indices": [0, 1],
      "seeds": [42, 43],
      "state": "SAMPLING",
      "step": 1,
      "total_steps": 20,
      "actual_candidate_parallelism": 2,
      "forward_count": 1,
      "trace": [{"input_shape": [4, 16, 1, 100, 166], "device": "cuda:0", "dtype": "torch.bfloat16", "cond_or_uncond": [0, 1], "candidate_batch": 2}],
      "duration_seconds": 0,
      "peak_memory_allocated": 0,
      "peak_memory_reserved": 0
    }
  },
  "outputs": {
    "save-node-id": {
      "images": [{"filename": "candidate-0.png", "subfolder": "", "type": "output"}],
      "sampler_id": "sampler-node-id",
      "indices": [0, 1],
      "seeds": [42, 43]
    }
  }
}
```

The example abbreviates the images array: a real successful two-candidate save requires exactly two receipts. `seeds` are unsigned Python integer JSON values; consumers must not round them through IEEE754 before replay.

States: `SAMPLING` → `SAMPLED` (sampling succeeded, not yet saved) → `SAVED`; caught sampler/save errors become `FAILED` with exception class `error_type`. Comfy interruption is rethrown unchanged; Gateway remains authoritative about cancel state and resource release. A VAE error between sampler and save leaves local group `SAMPLED`; Gateway must combine native execution failure/history with unsaved group state, not call it a completed candidate.

Successful save publishes image receipts before returning to permit incremental polling while later groups run. No inference from last prompt ID: each node binds to the unique **running** `prompt_queue` entry containing its node ID/class and reads the entry's explicit `prompt_id` and `client_id`. Missing/ambiguous owner fails closed. The endpoint retains this recorded owner when a prompt leaves the queue: different owner 403, absent/evicted/restarted progress 404, invalid query 400. Client ID scopes the existing trusted-LAN contract; it is not a new authentication mechanism.

In-memory evidence is limited to 64 prompt records, 8 groups per prompt and 32 trace samples per group. `forward_count` counts every actual diffusion-module forward pre-hook; `apply_forward_count`/`apply_trace` separately retain apply_model entry observations; parallelism is conservatively checked on every call even after the trace fills. Existing model wrappers and their optional `.models()`/`.to()` lifecycle are preserved. A task-scoped diffusion-module pre-hook reads the actual positional/keyword input tensor with thread-local CFG context; only this hook qualifies actual parallelism. The shared-module hook is removed in `finally` after success, error or interruption. `candidate_batch = input_batch / CFG_condition_count`; CPU or inconsistent batch does not qualify as GPU parallelism. Model cloning avoids altering a shared original model. This is candidate tensor batching evidence, not proof of two independent GPU streams.

Peak allocator metrics are reset/measured around this group's sampling and do not include later VAE decoding. `duration_seconds` includes node preparation and sampler work; elapsed GPU work is synchronized at boundaries. `step` is 1-based callback progress, 0 before the first callback. No tensor payloads or prompts are placed in trace.

The progress cache is not durable; Gateway must persist observed group/output identity and use native history/files for recovery. GET never changes state or starts work. A missing cache entry does not authorize re-generation.

## Isolated tests

Run with the project's Python containing aiohttp:

```bash
python -m unittest discover -s scripts/comfy-gateway-batching -p 'test_*.py' -v
```

Tests use fake torch/model/node modules and a real local aiohttp test application. They prove seed/index mapping, one batch call, CFG normalization, preserved wrappers, bounded trace, owner checks, saved-group dependencies, partial output preservation and error/cancel propagation. These are not GPU performance or memory qualification. Real Qwen and ZImage batch2/4 validation remains a separate Gateway-driven acceptance step.

## Implementation checkpoint

2026-09-08: 11/11 isolated tests pass using `dgx-drama-p1-preview`'s existing Python. Source/tests were copied only to `/tmp/mf05-runtime-tests`; no node registration or service restart occurred. Ten cases use fake tensors/models plus real aiohttp routes; one additionally uses real torch **CPU** tensors to verify identical original-index noise across 2+2 groups and a one-candidate subset, distinct candidate noise, and exact single-seed mapping. GPU traces/memory in mocked cases are synthetic test data, not qualification evidence.

Read-only head 0.34.0 compatibility check: `execution.py:1254` uses `threading.RLock` (nested `get_current_queue_volatile` locking safe); SHA256 `60e8333ec976e8723bba3aa854993963e2edfa21d62f2446fc68b8ea7ca7c480`. `samplers.py:333` passes the implemented wrapper `(apply_model,{input,timestep,c,cond_or_uncond})`; `model_base.py:204` accepts `(x,t,**conditioning)`. `model_patcher.py:453` clones model_options before our wrapper replacement. Final trace location is the diffusion-module forward pre-hook, not just `apply_model` entry. The latter is kept only in `apply_trace` and never qualifies parallelism alone. Further model-internal extensions should still be recorded when qualifying the real model.

Final candidate checkpoint: **14/14 tests PASS**, including diffusion kwargs input, internal apply-model batch splitting, shared hook removal after error, wrapper model-lifecycle preservation, and real torch CPU seed/group/subset equivalence. Real GPU qualification and production installation have not been performed by this subtask.
