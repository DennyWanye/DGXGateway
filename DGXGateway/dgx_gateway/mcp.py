"""Stateless JSON-RPC MCP endpoint backed by the existing Gateway authority."""
from __future__ import annotations
import json
from .operation_view import operation_view
from .comfy_identity import canonical_service


def tool(name, description, properties=None, required=(), read_only=False):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties or {},
                            "required": list(required), "additionalProperties": False},
            "annotations": {"readOnlyHint": read_only, "destructiveHint": not read_only,
                            "openWorldHint": False}}


TOOLS = [
    tool("text_capabilities", "Read text generation qualification and safe unavailable reason; does not load models.", read_only=True),
    tool("text_submit", "Create a persistent frozen text draft task. Generation is unavailable until qualified. Never approves business content or retries ambiguous inference.", {"task_kind":{"type":"string","enum":["extract_entities","draft_beats","draft_script","draft_storyboard","draft_video_prompt"]},"inputs":{"type":"object"},"idempotency_key":{"type":"string","minLength":8,"maxLength":128}}, ("task_kind","inputs","idempotency_key")),
    tool("text_job", "Read text job result and resource ownership separately.", {"job_id":{"type":"string"}}, ("job_id",), True),
    tool("text_job_find", "Find the original text job by key; read only, never redispatch.", {"idempotency_key":{"type":"string","minLength":8,"maxLength":128}}, ("idempotency_key",), True),
    tool("text_cancel", "Persist cancel intent. In-flight model completion and safe release still require confirmation.", {"job_id":{"type":"string"}}, ("job_id",)),
    tool("text_reconcile", "Explicitly reconcile existing text ownership; never infer again or silently retry a prompt.", {"job_id":{"type":"string"}}, ("job_id",)),
    tool("comfy_cancel", "Request durable cancellation of an image job. Interrupts owned Comfy sampling at a safe checkpoint, then saves available outputs and releases. Poll comfy_job; cancellation is not instantaneous.", {"job_id":{"type":"string"}}, ("job_id",)),
    tool("video_cancel", "Request cancellation of an H3 batch. Ring4 batches send a one-shot remote DELETE and then use Gateway lifecycle control to stop all four ranks, so an in-flight clip is actually terminated and resources are released; legacy two-node batches retain the safe clip-boundary behavior. Poll video_job for cancel_status, memory_released and resource_settled.", {"job_id":{"type":"string"}}, ("job_id",)),
    tool("video_register_audio", "Register an exact four-second mono PCM16 24 kHz WAV reference (base64, up to 256 KiB). Does not start a model. Returns a reusable content-addressed audio_asset_id.",
         {"filename":{"type":"string","minLength":1,"maxLength":180},"audio_base64":{"type":"string","minLength":4,"maxLength":349528}}, ("filename","audio_base64")),
    tool("comfy_register_image", "Register a PNG/JPEG/WebP (base64, up to 8 MiB) on Gateway for later reference editing. Does not start a model. Returns asset_id and the Comfy filename; filenames must be plain basenames.",
         {"filename":{"type":"string","minLength":1,"maxLength":180},"image_base64":{"type":"string","minLength":4,"maxLength":11184812}}, ("filename","image_base64")),
    tool("prepare_image_cutout", "Remove the background from a registered image with pinned local U2Net on the Mac. Returns a media job immediately; poll media_job for a reusable transparent PNG asset. Does not use DGX memory.",
         {"asset_id":{"type":"string","minLength":64,"maxLength":64},"idempotency_key":{"type":"string","minLength":8,"maxLength":128}}, ("asset_id","idempotency_key")),
    tool("compose_image_layers", "Create a deterministic composite from one registered background and 1–16 transparent registered image layers. Coordinates are pixels in the background canvas. Returns a media job; poll media_job. Does not use DGX memory.",
         {"background_asset_id":{"type":"string","minLength":64,"maxLength":64},"layers":{"type":"array","minItems":1,"maxItems":16,"items":{"type":"object","properties":{"name":{"type":"string","minLength":1,"maxLength":80},"asset_id":{"type":"string","minLength":64,"maxLength":64},"x":{"type":"integer","minimum":-4096,"maximum":4096},"y":{"type":"integer","minimum":-4096,"maximum":4096},"width":{"type":"integer","minimum":1,"maximum":4096},"height":{"type":"integer","minimum":1,"maximum":4096},"opacity":{"type":"integer","minimum":1,"maximum":100},"flip_x":{"type":"boolean"}},"required":["name","asset_id","x","y","width","height","opacity"],"additionalProperties":False}},"idempotency_key":{"type":"string","minLength":8,"maxLength":128}}, ("background_asset_id","layers","idempotency_key")),
    tool("media_job", "Read a local background-removal or layer-composition job and its reusable output_asset. These jobs do not load a DGX model.", {"job_id":{"type":"string"}}, ("job_id",), True),
    tool("edit_image", "Quality path for changing an existing image, including a locked identity. Default model is qwen-image-edit-2511. flux2-dev is the explicit FLUX.2 edit and accepts 1–8 reference images; image 1 sets the output size. Do not redraw a locked identity with generate_image unless the user asked for FLUX.2. Do not call while a video job is running. Poll comfy_job. Success is not approval or selection.",
         {"model":{"type":"string","enum":["qwen-image-edit-2511","llada-image-turbo","flux2-dev"]},"width":{"type":"integer","minimum":256,"maximum":1536},"height":{"type":"integer","minimum":256,"maximum":1536},"prompt":{"type":"string","minLength":1,"maxLength":8000},"asset_ids":{"type":"array","items":{"type":"string"},"minItems":1,"maxItems":8},"seed":{"type":"integer","minimum":0,"maximum":18446744073709551615},"steps":{"type":"integer","minimum":4,"maximum":40},"encoder":{"type":"string","enum":["standard","huihui-abliterated-bf16"]},"model_precision":{"type":"string","enum":["fp8","bf16"]},"batch_count":{"type":"integer","minimum":1},"parallelism":{"type":"integer","minimum":1},"candidate_indices":{"type":"array","minItems":1,"items":{"type":"integer","minimum":0}},"idempotency_key":{"type":"string","minLength":8,"maxLength":128}}, ("prompt","asset_ids","idempotency_key")),
    tool("generate_image", "Quality-first text-to-image, and the only new-image path. Omit model to use flux2-dev. Optional asset_ids on flux2-dev add up to 8 reference images without changing the requested canvas. Do not redraw a locked identity unless the user asked for FLUX.2. z-image-turbo requires preview=true and is only for an explicit quick look; do not keep it or send it to video. flux2-dev-abliterated and llada-image-turbo are rejected. Do not call while a video job is running, and do not use a raw workflow, script, or SSH command. Poll comfy_job. Success is not approval.",
         {"model":{"type":"string","enum":["z-image-turbo","flux2-dev","flux2-dev-abliterated","llada-image-turbo"]},"preview":{"type":"boolean","description":"Required true only for z-image-turbo, and only after the user explicitly asked for a quick look. Those outputs are not keepers."},"prompt":{"type":"string","minLength":1,"maxLength":8000},"asset_ids":{"type":"array","items":{"type":"string"},"minItems":1,"maxItems":8},"width":{"type":"integer","minimum":256,"maximum":1536},"height":{"type":"integer","minimum":256,"maximum":1536},"seed":{"type":"integer","minimum":0,"maximum":18446744073709551615},"batch_count":{"type":"integer","minimum":1},"parallelism":{"type":"integer","minimum":1},"candidate_indices":{"type":"array","minItems":1,"items":{"type":"integer","minimum":0}},"idempotency_key":{"type":"string","minLength":8,"maxLength":128}}, ("prompt","idempotency_key")),
    tool("retry_image_candidates", "Explicitly retry only selected known failed image candidates from a released typed image job, with their original indices/seeds and a new idempotency key. Saved or unknown candidates cannot regenerate; use comfy_reconcile for saved download recovery.",
         {"job_id":{"type":"string"},"candidate_indices":{"type":"array","minItems":1,"items":{"type":"integer","minimum":0}},"idempotency_key":{"type":"string","minLength":8,"maxLength":128}}, ("job_id","candidate_indices","idempotency_key")),
    tool("personaplex_session", "Get PersonaPlex voice/session connection information. First switch model to personaplex and wait for operation completion. No model mutation.", read_only=True),
    tool("comfy_reconcile", "Recover an interrupted creative job after queue finishes, collect its result without resubmitting, then release model memory.", {"job_id":{"type":"string"}}, ("job_id",)),
    tool("comfy_catalog", "Inspect head ComfyUI installed model loaders and nodes. Does not load weights.", read_only=True),
    tool("comfy_submit", "Submit a saved non-image ComfyUI workflow, such as Music 3. A raw image workflow is rejected: new or edited images must use generate_image or edit_image. Gateway starts Comfy, saves artifacts, and stops it to release model memory. Query comfy_job. Serial hardware; busy returns error. No automatic retries of ambiguous submissions.",
         {"workflow":{"type":"object"}, "input_assets":{"type":"array","items":{"type":"string"},"maxItems":16}, "idempotency_key":{"type":"string","minLength":8,"maxLength":128}}, ("workflow","idempotency_key")),
    tool("comfy_job", "Read persistent job state, memory_released, errors and downloadable artifacts. SUCCEEDED means output saved and model stopped.", {"job_id":{"type":"string"}}, ("job_id",), True),
    tool("generate_video", "Submit one four-second H3 Ring4 Ref2VA clip. Text, image and audio are optional; empty references remain Ref2VA. Poll video_job for the saved MP4 and resource status.",
         {"name":{"type":"string","minLength":1,"maxLength":80},"prompt":{"type":"string","maxLength":16384},"image_asset_id":{"type":"string","minLength":0,"maxLength":64},"audio_asset_id":{"type":"string","minLength":0,"maxLength":64},"seed":{"type":"integer","minimum":0,"maximum":2147483647},"idempotency_key":{"type":"string","minLength":8,"maxLength":128},"non_preemptive":{"type":"boolean"}}, ("name","seed","idempotency_key")),
    tool("video_batch_submit", "Submit 1–32 ordered clips through four-node H3 Ring4. Text, images, video and audio are optional, including empty inputs. Use Ref2VA for new work; 10eros-fl2va-v1 is a legacy explicit T2VA route, never automatically selected for missing references. Use heretic-ref2va-hmnsfw-v1 for 4–15 integer seconds. Poll the original video_job; acceptance is not completion.",
         {"diagnostic":{"type":"object","properties":{"attention_probe":{"type":"boolean"},"trace_step":{"type":"integer","minimum":-1,"maximum":5},"max_steps":{"type":"integer","minimum":1,"maximum":6},"max_seconds":{"type":"integer","minimum":60,"maximum":3600}},"required":["trace_step","max_steps","max_seconds"],"additionalProperties":False},"shots":{"type":"array","minItems":1,"maxItems":32,"items":{"type":"object","properties":{"name":{"type":"string","minLength":1,"maxLength":80},"prompt":{"type":"string","maxLength":16384},"image_asset_id":{"type":"string","minLength":0,"maxLength":64},"action_image_asset_id":{"type":"string","minLength":0,"maxLength":64},"reference_image_ids":{"type":"array","maxItems":9,"items":{"type":"string","minLength":64,"maxLength":64}},"video_asset_id":{"type":"string","minLength":0,"maxLength":64},"audio_asset_id":{"type":"string","minLength":0,"maxLength":64},"seed":{"type":"integer","minimum":0,"maximum":2147483647},"request_duration_seconds":{"type":"integer","minimum":4,"maximum":15},"contract_version":{"type":"string","enum":["h3-ref2va-v1"]}},"required":["name","seed"],"additionalProperties":False}},"profile":{"type":"string","enum":["heretic-ref2va-v1","heretic-ref2va-hmnsfw-v1","10eros-fl2va-v1"]},"idempotency_key":{"type":"string","minLength":8,"maxLength":128},"non_preemptive":{"type":"boolean"}}, ("shots","idempotency_key")),
    tool("video_job", "Read a persistent H3 video batch, including per-shot state, saved MP4 URLs, execution_mode and memory_released.", {"job_id":{"type":"string"}}, ("job_id",), True),
    tool("video_job_find", "Read-only lookup of an existing H3 batch by its original idempotency key. Never submits or resumes a clip. Returns job or null.",
         {"idempotency_key":{"type":"string","minLength":8,"maxLength":128}}, ("idempotency_key",), True),
    tool("video_reconcile", "Reconcile an interrupted video batch while preserving completed MP4s. Running Ring4 jobs resume background observation of their original ID and collect the result without restarting or resubmitting inference. Pair jobs retain their original backend identity; owned legacy runtimes are safely released.", {"job_id":{"type":"string"}}, ("job_id",)),
    tool("dgx_recover_stop", "Recover a failed switch to all-models-stopped using its existing live Gateway lease. Only for RECOVERY_REQUIRED operations.", {"operation_id": {"type": "string"}}, ("operation_id",)),
    tool("dgx_status", "Read live cluster/model readiness and active operation. Does not start models. Credentials are omitted.", read_only=True),
    tool("dgx_switch_model", "Start/switch a model through the unique DGX Gateway lifecycle. Returns operation_id immediately; poll dgx_operation. Busy generation is never interrupted.",
         {"model": {"type": "string"}, "profile": {"type": "string"},
          "idempotency_key": {"type": "string", "minLength": 8, "maxLength": 128}}, ("model", "idempotency_key")),
    tool("dgx_stop", "Stop current model and release its memory through Gateway. Poll returned operation; never equate submission with completion.",
         {"idempotency_key": {"type": "string", "minLength": 8, "maxLength": 128}}, ("idempotency_key",)),
    tool("dgx_operation", "Query a model lifecycle operation and its outcome.",
         {"operation_id": {"type": "string"}}, ("operation_id",), True),
    tool("dgx_cancel_operation", "Request cancellation of an unfinished model switch at its safe boundary.",
         {"operation_id": {"type": "string"}}, ("operation_id",)),
]


def _validate(schema, value):
    if not isinstance(value, dict) or set(value) - set(schema["properties"]) or set(schema["required"]) - set(value):
        raise ValueError("invalid tool arguments")
    for key, item in value.items():
        spec = schema["properties"][key]
        _validate_value(spec, item, key)


def _validate_value(spec, item, key):
    expected = spec["type"]
    if expected == "string" and (not isinstance(item, str) or not spec.get("minLength", 0) <= len(item) <= spec.get("maxLength", 65536)):
        raise ValueError(f"invalid {key}")
    if expected == "array":
        if not isinstance(item, list) or (len(item) < spec.get("minItems", 0) or ("maxItems" in spec and len(item) > spec["maxItems"])):
            raise ValueError(f"invalid {key}")
        for index, value in enumerate(item):
            _validate_value(spec["items"], value, f"{key}[{index}]")
    if expected == "object":
        if not isinstance(item, dict):
            raise ValueError(f"invalid {key}")
        properties = spec.get("properties")
        if properties is not None:
            if set(item) - set(properties) or set(spec.get("required", ())) - set(item):
                raise ValueError(f"invalid {key}")
            for child_key, child in item.items():
                _validate_value(properties[child_key], child, f"{key}.{child_key}")
    if expected == "boolean" and not isinstance(item, bool):
        raise ValueError(f"invalid {key}")
    if expected == "integer" and (isinstance(item, bool) or not isinstance(item, int) or item < spec.get("minimum", 0) or ("maximum" in spec and item > spec["maximum"])):
        raise ValueError(f"invalid {key}")
    if "enum" in spec and item not in spec["enum"]:
        raise ValueError(f"invalid {key}")


def call(app, name, args):
    if name == "text_capabilities": return app.text_jobs.capabilities()
    if name == "text_submit": return app.text_jobs.submit(args["task_kind"], args["inputs"], args["idempotency_key"])
    if name == "text_job": return app.text_jobs.get(args["job_id"])
    if name == "text_job_find": return {"job": app.text_jobs.find(args["idempotency_key"])}
    if name == "text_cancel": return app.text_jobs.cancel(args["job_id"])
    if name == "text_reconcile": return app.text_jobs.reconcile(args["job_id"])
    if name == "comfy_cancel": return app.creative_jobs.cancel(args["job_id"])
    if name == "video_cancel": return app.video_jobs.cancel(args["job_id"])
    if name == "video_register_audio":
        return app.video_jobs.audio.register_base64(args["filename"], args["audio_base64"])
    if name == "comfy_register_image":
        return app.creative_jobs.assets.register_base64(args['filename'],args['image_base64'])
    if name == "prepare_image_cutout":
        return app.media_jobs.submit_cutout(args["asset_id"], args["idempotency_key"])
    if name == "compose_image_layers":
        return app.media_jobs.submit_composite(args["background_asset_id"], args["layers"], args["idempotency_key"])
    if name == "media_job": return app.media_jobs.get(args["job_id"])
    if name == "edit_image":
        from .image_workflows import edit_image, validate_edit_parameters, assert_image_policy
        assert_image_policy(name, args)
        validate_edit_parameters(args)
        assets=[app.creative_jobs.assets.get(asset_id) for asset_id in args['asset_ids']]
        workflow=edit_image(args,[a['comfy_filename'] for a in assets])
        return app.creative_jobs.submit(workflow,args['idempotency_key'],input_assets=args['asset_ids'],image_request={'tool':name,'parameters':{k:v for k,v in args.items() if k!='idempotency_key'}})
    if name == "generate_image":
        from .image_workflows import generate_image, assert_image_policy, resolve_generate_model
        assert_image_policy(name, args)
        model = resolve_generate_model(args.get('model'))
        saved = {k: v for k, v in args.items() if k != 'idempotency_key'}
        saved['model'] = model
        asset_ids = list(args.get('asset_ids') or [])
        filenames = [app.creative_jobs.assets.get(asset_id)['comfy_filename'] for asset_id in asset_ids]
        return app.creative_jobs.submit(generate_image(args['prompt'],args.get('width',1024),args.get('height',1024),args.get('seed',42),args.get('batch_count',1),args.get('parallelism',2),args.get('candidate_indices'),model,image_filenames=filenames or None),args['idempotency_key'],input_assets=asset_ids,image_request={'tool':name,'parameters':saved})
    if name == "retry_image_candidates": return app.creative_jobs.retry_images(args["job_id"],args["candidate_indices"],args["idempotency_key"])
    if name == "personaplex_session":
        adapter=app.operations.adapters['personaplex']
        return {'state':adapter.observe().state,'base_url':f'http://{adapter.head.address}:{adapter.port}','websocket':f'ws://{adapter.head.address}:{adapter.port}/api/chat','mac_browser_url':'http://127.0.0.1:8998','voice_example':'NATF0.pt','input_audio':'24kHz Opus: binary 0x01 + Opus payload','output':'0x00 handshake, 0x01 Opus audio, 0x02 UTF-8 text','note':'Browser microphone requires localhost or trusted HTTPS. Native LAN API requires no auth.'}
    if name == "comfy_reconcile": return app.creative_jobs.reconcile(args["job_id"])
    if name == "comfy_catalog": return app.creative_jobs.catalog()
    if name == "comfy_submit":
        from .image_workflows import assert_typed_image_path
        assert_typed_image_path(args['workflow'])
        return app.creative_jobs.submit(args['workflow'],args['idempotency_key'],input_assets=args.get('input_assets'))
    if name == "comfy_job": return app.creative_jobs.get(args['job_id'])
    if name == "generate_video":
        shot = {key: args[key] for key in ("name", "prompt", "image_asset_id", "audio_asset_id", "seed") if key in args}
        return app.video_jobs.submit([shot], args["idempotency_key"], non_preemptive=args.get("non_preemptive", False))
    if name == "video_batch_submit":
        return app.video_jobs.submit(args["shots"], args["idempotency_key"], args.get("profile", "heretic-ref2va-v1"), non_preemptive=args.get("non_preemptive", False), diagnostic=args.get("diagnostic"))
    if name == "video_job": return app.video_jobs.get(args["job_id"])
    if name == "video_job_find": return {"job": app.video_jobs.find(args["idempotency_key"])}
    if name == "video_reconcile": return app.video_jobs.reconcile(args["job_id"])
    if name == "dgx_recover_stop":
        return {"operation": operation_view(app.recover_stop(args["operation_id"]))}
    if name == "dgx_status":
        value = app.snapshot.build()
        value.pop("active_connection", None)
        return value
    if name in {"dgx_switch_model", "dgx_stop"}:
        target = canonical_service(args.get("model")) if name == "dgx_switch_model" else None
        profile = args.get("profile")
        if target is not None:
            if target not in app.operations.adapters:
                raise ValueError("unknown model; query dgx_status")
            if profile is None:
                profile = getattr(app.operations.adapters[target], "default_profile", None)
            profiles = getattr(app.operations.adapters[target], "profiles", ())
            if (profiles and profile not in profiles) or (not profiles and profile is not None):
                raise ValueError("invalid profile; query dgx_status")
        operation, _ = app.submit_switch(target, profile, args["idempotency_key"])
        return {"operation": operation_view(operation)}
    if name == "dgx_operation":
        return {"operation": operation_view(app.store.get_operation(args["operation_id"]))}
    if name == "dgx_cancel_operation":
        return {"operation": operation_view(app.cancel_operation(args["operation_id"]) if hasattr(app, "cancel_operation") else app.store.request_cancel(args["operation_id"]))}
    raise ValueError("unknown tool")


def dispatch(app, request):
    request_id = request.get("id")
    def error(code, message):
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
        return error(-32600, "Invalid Request")
    method = request["method"]
    if "id" not in request:
        return None
    params = request.get("params", {})
    if not isinstance(params, dict):
        return error(-32602, "Invalid params")
    if method == "initialize":
        version = params.get("protocolVersion")
        if not isinstance(version, str): return error(-32602, "protocolVersion must be a string")
        result = {"protocolVersion": version if version in {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"} else "2025-11-25",
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "dgx-creative-gateway", "version": "1.0.0"},
                  "instructions": "LAN DGX control. Query status first. Model switches are async; poll operation until terminal. Never bypass Gateway with SSH lifecycle commands. Image generation is generate_image or edit_image only: omit model for flux2-dev, change a locked identity with edit_image, and do not submit a raw image workflow."}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        spec = next((t for t in TOOLS if t["name"] == params.get("name")), None)
        if spec is None:
            return error(-32602, "Unknown tool")
        args = params.get("arguments", {})
        try:
            _validate(spec["inputSchema"], args)
        except ValueError as exc:
            return error(-32602, str(exc))
        try:
            value = call(app, spec["name"], args)
            result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                      "structuredContent": value, "isError": False}
        except Exception as exc:
            # Do not leak remote stderr, credentials, or arbitrary paths.
            message = str(exc) if isinstance(exc, (ValueError, BlockingIOError, KeyError)) else "Gateway operation failed; inspect status and local service logs"
            result = {"content": [{"type": "text", "text": message}], "isError": True}
            if spec["name"] in {"generate_image", "edit_image"} and isinstance(exc, BlockingIOError):
                result["structuredContent"] = {"error": {"code": "DGX_BUSY", "message": "DGX resources busy; no image task accepted"}}
            if spec["name"].startswith("text_") and hasattr(exc, "code"):
                result["structuredContent"] = {"error": {"code": exc.code, "message": str(exc)}}
            if spec["name"] in {"video_batch_submit", "generate_video", "video_reconcile"} and isinstance(exc, (BlockingIOError, ValueError)):
                result["structuredContent"] = {"error": {"code": "DGX_BUSY" if isinstance(exc, BlockingIOError) else "VIDEO_REJECTED", "message": message}}
    else:
        return error(-32601, "Method not found")
    return {"jsonrpc": "2.0", "id": request_id, "result": result}
