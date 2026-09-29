# PersonaPlex Gateway operation

2026-09-05. Model lifecycle belongs exclusively to DGX Gateway. The runtime is an on-demand, single-session full-duplex speech model. It is not an OpenAI Realtime-compatible endpoint or a batch TTS API.

## Runtime contract

- Gateway model ID: `personaplex`; no alternate profiles.
- Owned head unit: `dgx-gateway-personaplex.service`, transient, `Restart=no`, control-group termination.
- Head root: `/home/operator/dgx-models/personaplex`.
- HTTP: `http://192.0.2.30:8998`; WebSocket `/api/chat`.
- Local Python `.venv/bin/python`: Torch 2.10.0+cu130. All weights, tokenizer, 18 voices and built UI already present.
- Start uses only local asset flags with `HF_HUB_OFFLINE=1`; no runtime downloads.
- `/healthz`: `model`, `ready`, `draining`, `active_sessions`. HTTP starts after model load and warmup.
- `POST /drain`: marks draining immediately. Existing session continues, new sessions receive HTTP 503. Gateway waits up to 180 seconds for zero sessions before stopping. A drain timeout remains draining and requires closing the client and recovery through Gateway.
- A second session receives HTTP 409 before any shared model mutation. Invalid voice/text/seed requests receive HTTP 400.
- Stop proof requires inactive/failed unit, zero MainPID, empty cgroup, no 8998 listener and no `moshi.server` process. Stopping the unit frees model memory; closing a voice session alone does not.

Local adapter: `DGXGateway/dgx_gateway/adapters/personaplex.py`. Gateway integration must register the adapter and permit the model in admission; merely adding this file does not deploy a running service.

## Repeatable native patch

`python3 scripts/personaplex/patch_runtime.py PATH_TO_NATIVE_SERVER` verifies exact source anchors, parses the proposed result, backs up the original by SHA-256 suffix and patches once. Current remote backup:

`/home/operator/dgx-models/personaplex/moshi/moshi/server.py.pre-gateway-00fd77bc48e2`

The installed wheel copy at `.venv/lib/python3.12/site-packages/moshi/server.py` was also patched with the same backup suffix after real warmup exposed that `python -m` imported the wheel, not the source tree. Adapter launches now explicitly set `PYTHONPATH=<root>/moshi` so reviewed source is authoritative.

Patch changes host binding, query seed lookup, voice filename validation, whole-session admission locking and health/drain routes. It preserves the previous offline configuration-download patch. For rollback first stop through Gateway, then restore the backed-up file; restore the corresponding unavailable adapter before subsequent Gateway operation.

## Speech acceptance

Start PersonaPlex through Gateway/MCP, then run the provided smoke using the existing remote environment. Copying the script does not start a model:

```bash
scp scripts/personaplex/smoke_websocket.py operator@192.0.2.30:/tmp/dgx-personaplex-smoke.py
ssh -o BatchMode=yes operator@192.0.2.30 '/home/operator/dgx-models/personaplex/.venv/bin/python /tmp/dgx-personaplex-smoke.py --url http://192.0.2.30:8998 --output /home/operator/dgx-models/personaplex/outputs/gateway-smoke.wav --check-drain'
```

The supplied 40-second fixture is 24 kHz. Default smoke streams the first 20 seconds plus eight seconds of silence, waits for output, saves WAV and JSON, and requires nonzero finite audio plus returned text. It also checks busy rejection and, with `--check-drain`, rejection of new sessions while the original session finishes. This option deliberately leaves the service draining; follow it with Gateway stop/switch. Omit it for a normal reusable speech call.

The script supports `--input`, `--voice`, `--prompt`, `--seed`, duration and timeout. It never loads a second model. Outputs can be retrieved via SCP and auditioned. Protocol success is insufficient to claim intelligible or production-quality speech: listen to the saved audio and separately assess Mandarin, English and interruption behavior. No actual speech inference was run by the implementation agent; the parent acceptance run owns lifecycle and real output evidence.

Transport framing: wait for `0x00` handshake, then send `0x01` plus streaming Opus bytes. Returned `0x01` frames contain Opus, `0x02` contains UTF-8 text. Voice choices are the basenames under `models/personaplex-7b-v1/voices`, such as `NATF0.pt` and `NATM1.pt`.

## Browser microphone

The plain HTTP LAN API requires no login. Browsers need a secure context for the microphone. After Gateway starts the model, use the existing LAN SSH connection:

```bash
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 8998:127.0.0.1:8998 operator@192.0.2.30
```

Open [PersonaPlex localhost UI](http://localhost:8998), grant microphone access and select a voice. The tunnel does not load another model. Close the conversation before requesting Gateway stop. For another LAN client, run this tunnel on that client. Direct LAN browser microphone support requires a trusted TLS endpoint; no misleading claim of microphone support over plain LAN HTTP is made.

## Validation already performed

- Both SSH role identities verified.
- Asset sizes, environment versions and fixture sample rate inspected.
- Remote runtime and local scripts compile.
- Extracted actual native admission methods tested without model load: busy 409, drain 503, original request finishes, invalid voice/missing text/bad seed do not take the lock.
- No model services were started or stopped by the implementation agent.

Official model instructions: [NVIDIA PersonaPlex](https://github.com/NVIDIA/personaplex/blob/main/README.md).
