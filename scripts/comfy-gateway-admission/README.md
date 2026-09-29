# Comfy Gateway admission fence

Installed as `/home/operator/ai/ComfyUI/custom_nodes/dgx_gateway_admission/__init__.py`; takes effect on the next Gateway-controlled Comfy startup. No weights are loaded by this extension.

The native `PromptServer.instance.app` middleware counts accepted POST requests to both `/prompt` and `/api/prompt`, including asynchronous validation before enqueue. Draining rejects new requests before a handler runs, waits up to ten seconds for already accepted handlers to finish, then inspects `prompt_queue.get_current_queue_volatile()`.

- `GET /dgx/health`: version, draining flag, in-flight submission count, running/pending counts.
- `POST /dgx/drain`: HTTP 200 with `drained:true` means no accepted submission can subsequently enqueue, native queue is empty, and admission stays fenced until process exit.
- HTTP 409 with `comfy_busy` or `submission_drain_timeout` leaves admission resumed and queue contents untouched. Gateway must not stop the process on this result.
- HTTP failure/404 is not stopped proof; do not fall back to a queue-only check.
- Gateway HTTP timeout must exceed ten seconds (15 recommended).

No resume endpoint is exposed: successful drain belongs to Gateway process retirement. This extension never cancels, deletes, interrupts or retries jobs and requires no LAN login.

`test_admission.py` uses actual aiohttp middleware and scheduled in-flight handlers without importing Comfy or models. Tests cover validation completing/enqueueing after drain starts, invalid submission completing to an idle fence, timeout resuming admission, and preserving an already running external job. Four tests passed in the remote existing PersonaPlex venv.
