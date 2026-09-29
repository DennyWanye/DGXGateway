"""Typed graph nodes: Comfy tensors in, generated media tensors/files out."""
import base64
import json
from pathlib import Path
import shutil
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid

from comfy_api.latest import io, InputImpl, ui
from .creative_client import forward, GATEWAY
from .media import stage_reference_image, stage_reference_audio


def call(name, arguments):
    response = forward({"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "tools/call", "params": {"name": name, "arguments": arguments}}, timeout=5 if name.endswith("_job") else 60)
    if response.get("error") or response.get("result", {}).get("isError"):
        raise RuntimeError(json.dumps(response, ensure_ascii=False))
    return response["result"]["structuredContent"]


def register_image(image):
    import folder_paths
    path = stage_reference_image(image, folder_paths.get_temp_directory(), max_encoded_bytes=8*1024*1024)
    try:
        return call("comfy_register_image", {"filename": "reference.png", "image_base64": base64.b64encode(path.read_bytes()).decode()})["asset_id"]
    finally:
        shutil.rmtree(path.parent)


def submit_and_wait(tool, arguments, kind, *, allow_partial=False):
    import folder_paths
    import comfy.model_management as management
    from server import PromptServer
    management.throw_exception_if_processing_interrupted()
    receipts = Path(folder_paths.get_output_directory()) / "dgx-jobs"
    receipts.mkdir(parents=True, exist_ok=True)
    original = dict(arguments)
    record = None
    for candidate in sorted(receipts.glob("*.json"), key=lambda p:p.stat().st_mtime, reverse=True):
        prior = json.loads(candidate.read_text())
        same = {k:v for k,v in prior.get("arguments",{}).items() if k != "idempotency_key"} == original
        if prior.get("tool") == tool and same and (prior.get("state") != "DELIVERED" if allow_partial else prior.get("state") not in {"DELIVERED", "CANCELED", "FAILED", "INTERRUPTED"}):
            record, receipt = prior, candidate
            break
    if record is None:
        key = "comfy-graph-" + uuid.uuid4().hex
        receipt = receipts / (key + ".json")
        record = {"tool": tool, "arguments": {**original, "idempotency_key": key}, "state": "SUBMITTING"}
        receipt.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    if record.get("job_id"):
        row = {"job_id": record["job_id"], "state": record["state"]}
    else:
        # An uncertain retry reuses the persisted request and its exact key.
        try:
            row = call(tool, record["arguments"])
        except Exception as exc:
            raise RuntimeError(f"Submission outcome unknown or rejected. Original request: {receipt.name}. Reuse its idempotency key to resolve, do not blindly resubmit.") from exc
        record.update(job_id=row["job_id"], state=row["state"])
        receipt.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    PromptServer.instance.send_sync("dgx-creative-job", {"id": row["job_id"], "kind": kind})
    deadline = time.monotonic() + (7200 if kind == "comfy" else 10800)
    read_failures = 0
    while time.monotonic() < deadline:
        try:
            management.throw_exception_if_processing_interrupted()
        except management.InterruptProcessingException:
            try:
                canceled = call(kind + "_cancel", {"job_id": row["job_id"]})
                record.update(state="CANCELED", result=canceled)
                receipt.write_text(json.dumps(record, ensure_ascii=False, indent=2))
            except Exception as exc:
                raise RuntimeError(f"Could not confirm server cancellation. Query/cancel {kind} job {row['job_id']} before another run.") from exc
            raise
        try:
            row = call(kind + "_job", {"job_id": row["job_id"]})
            read_failures = 0
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            read_failures += 1
            if read_failures >= 30:
                raise RuntimeError(f"Gateway temporarily unreachable; original job {row['job_id']} retained for the next run to resume.")
            time.sleep(1)
            continue
        if row["state"] in {"SUCCEEDED", "CANCELED", "FAILED", "INTERRUPTED"}:
            record.update(state=row["state"], result=row)
            receipt.write_text(json.dumps(record, ensure_ascii=False, indent=2))
            resource_ready = row.get('memory_released') is True or (
                row['state'] == 'SUCCEEDED' and row.get('resource_settled') is True
                and row.get('resource_status') == 'resident')
            if (row["state"] != "SUCCEEDED" and not allow_partial) or not resource_ready or row.get("cleanup_required"):
                raise RuntimeError(f"DGX job {row['job_id']}: {row['state']}; memory_released={row.get('memory_released')}. {row.get('error', '')}")
            row["_graph_receipt"] = str(receipt)
            return row
        time.sleep(1)
    raise TimeoutError(f"DGX job {row['job_id']} is still running; query original job before retrying")


def artifact_bytes(artifact, max_bytes):
    url = urllib.parse.urlsplit(artifact["url"])
    gateway = urllib.parse.urlsplit(GATEWAY)
    if (url.scheme, url.netloc) != (gateway.scheme, gateway.netloc) or not url.path.startswith("/api/v1/artifacts/"):
        raise ValueError("Unexpected artifact origin")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(artifact["url"], timeout=60) as response:
        data = response.read(max_bytes + 1)
    if not data or len(data) > max_bytes:
        raise ValueError("Invalid artifact size")
    return data


def delivered(row, *, partial=False):
    receipt = Path(row["_graph_receipt"])
    record = json.loads(receipt.read_text())
    record["state"] = "PARTIAL_DELIVERED" if partial else "DELIVERED"
    receipt.write_text(json.dumps(record, ensure_ascii=False, indent=2))


class DGXQwenImageEdit(io.ComfyNode):
    ENCODER = "standard"
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="DGXQwenImageEdit", display_name="Qwen 2511 图片编辑 · DGX", category="DGX/Creative",
            description="兼容旧工作流的单图节点，仅生成并输出1张。需要多候选请改用「Qwen 2511 · 版本选择与批量」。连接加载图像节点，自动加载和释放模型；取消会请求停止原远程任务。",
            inputs=[io.Image.Input("reference_image"), io.String.Input("prompt", default="保持角色和构图，改成柔和的夕阳光线。", multiline=True, dynamic_prompts=False), io.Int.Input("seed", default=42, min=0, max=2147483647), io.Int.Input("steps", default=20, min=20, max=40)],
            outputs=[io.Image.Output("image")])

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        return float("nan")

    @classmethod
    def execute(cls, reference_image, prompt, seed=42, steps=20):
        import io as buffers
        import numpy as np
        import torch
        from PIL import Image
        asset = register_image(reference_image)
        row = submit_and_wait("edit_image", {"asset_ids": [asset], "prompt": prompt, "seed": seed, "steps": steps, **({"encoder":cls.ENCODER} if cls.ENCODER != "standard" else {})}, "comfy")
        artifact = next(a for a in row["artifacts"] if a.get("kind") == "images")
        with Image.open(buffers.BytesIO(artifact_bytes(artifact, 32*1024*1024))) as image:
            output = torch.from_numpy(np.array(image.convert("RGB")).astype(np.float32) / 255.0).unsqueeze(0)
        delivered(row)
        return io.NodeOutput(output)


class DGXH3Video(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="DGXH3Video", display_name="H3 图生视频 · DGX 自动释放", category="DGX/Creative",
            description="连接参考图片和音频，生成4秒视频并预览。自动加载双机和释放；取消等待当前镜头结束。",
            inputs=[io.Image.Input("reference_image"), io.Audio.Input("reference_audio"), io.String.Input("prompt", default="Static camera, subtle natural motion, preserve the reference composition.", multiline=True, dynamic_prompts=False), io.Int.Input("seed", default=42, min=0, max=2147483647)],
            outputs=[io.Video.Output("video")], is_output_node=True)

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        return float("nan")

    @classmethod
    def execute(cls, reference_image, reference_audio, prompt, seed=42):
        import folder_paths
        asset = register_image(reference_image)
        audio_path = stage_reference_audio(reference_audio, folder_paths.get_temp_directory())
        try:
            audio = call("video_register_audio", {"filename": "reference.wav", "audio_base64": base64.b64encode(audio_path.read_bytes()).decode()})["asset_id"]
        finally:
            shutil.rmtree(audio_path.parent)
        row = submit_and_wait("generate_video", {"name": "shot-01", "prompt": prompt, "image_asset_id": asset, "audio_asset_id": audio, "seed": seed}, "video")
        target = Path(folder_paths.get_output_directory()) / ("dgx-h3-" + row["job_id"] + ".mp4")
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(artifact_bytes(row["shots"][0]["artifact"], 256*1024*1024))
        temporary.replace(target)
        delivered(row)
        return io.NodeOutput(InputImpl.VideoFromFile(str(target)), ui=ui.PreviewVideo([ui.SavedResult(target.name, "", io.FolderType.output)]))


RING4_PROFILES = ("heretic-ref2va-v1", "heretic-ref2va-hmnsfw-v1", "10eros-fl2va-v1")
RING4_STABLE = "heretic-ref2va-v1"
RING4_TEXT = "10eros-fl2va-v1"


def _ring4_video_output(row, shot_index=0):
    """Persist the first artifact of one Ring4 shot and return a Comfy video output."""
    import folder_paths
    shots = row.get("shots") or []
    if not shots or not shots[shot_index].get("artifact"):
        raise RuntimeError("Ring4 任务没有已保存的产物；请在工作台核查原任务")
    target = Path(folder_paths.get_output_directory()) / ("dgx-h3ring4-" + row["job_id"] + ".mp4")
    temporary = target.with_suffix(".tmp")
    temporary.write_bytes(artifact_bytes(shots[shot_index]["artifact"], 256 * 1024 * 1024))
    temporary.replace(target)
    delivered(row)
    return io.NodeOutput(InputImpl.VideoFromFile(str(target)),
                         ui=ui.PreviewVideo([ui.SavedResult(target.name, "", io.FolderType.output)]))


def _ring4_submit(shots, profile):
    """One shot per batch: Ring4 executes serially and releases after the batch."""
    row = submit_and_wait("video_batch_submit", {"shots": shots, "profile": profile}, "video")
    return row


class DGXH3Ring4Ref2VA(io.ComfyNode):
    """Ring4 reference-to-video-audio: one image + one 4s audio reference -> one MP4."""

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="DGXH3Ring4Ref2VA",
            display_name="H3 Ring4 参考生视频（图+声）",
            category="DGX/Creative",
            description=(
                "四机 MiniMax H3 Ring4（ref2va-official，768p/24fps/4 秒）。"
                "连接参考图片与 4 秒参考音频生成一条 MP4；模型全程由 Gateway 生命周期管理。"
                "提交后不可重发同一批次；取消等待当前镜头结束。"
            ),
            inputs=[
                io.Image.Input("reference_image"),
                io.Audio.Input("reference_audio"),
                io.String.Input("prompt", default="", multiline=True, dynamic_prompts=False),
                io.Int.Input("seed", default=42, min=0, max=2147483647),
                io.Combo.Input("profile", options=[RING4_STABLE, "heretic-ref2va-hmnsfw-v1"],
                               default=RING4_STABLE, optional=True),
            ],
            outputs=[io.Video.Output("video")], is_output_node=True)

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        return float("nan")

    @classmethod
    def execute(cls, reference_image, reference_audio, prompt, seed=42, profile=RING4_STABLE):
        import folder_paths
        if profile not in (RING4_STABLE, "heretic-ref2va-hmnsfw-v1"):
            raise ValueError("Ring4 参考生视频仅支持 heretic-ref2va-v1 或 heretic-ref2va-hmnsfw-v1")
        if not prompt.strip():
            raise ValueError("提示词不能为空")
        asset = register_image(reference_image)
        audio_path = stage_reference_audio(reference_audio, folder_paths.get_temp_directory())
        try:
            audio = call("video_register_audio", {
                "filename": "reference.wav",
                "audio_base64": base64.b64encode(audio_path.read_bytes()).decode()})["asset_id"]
        finally:
            shutil.rmtree(audio_path.parent)
        row = _ring4_submit([{"name": "ring4-shot-01", "prompt": prompt,
                              "image_asset_id": asset, "audio_asset_id": audio,
                              "seed": seed, "request_duration_seconds": 4,
                              "contract_version": "h3-ref2va-v1"}], profile)
        return _ring4_video_output(row)


class DGXH3Ring4T2VA(io.ComfyNode):
    """Ring4 text-to-video-audio: no image, no audio asset; sound comes from the prompt."""

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="DGXH3Ring4T2VA",
            display_name="H3 Ring4 文字生视频",
            category="DGX/Creative",
            description=(
                "四机 MiniMax H3 Ring4 文字生视频（fl2va-official，768p/24fps/4 秒）。"
                "不使用参考图与参考音频，声音需在提示词中描述。模型由 Gateway 生命周期管理。"
            ),
            inputs=[
                io.String.Input("prompt", default="", multiline=True, dynamic_prompts=False),
                io.Int.Input("seed", default=42, min=0, max=2147483647),
            ],
            outputs=[io.Video.Output("video")], is_output_node=True)

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        return float("nan")

    @classmethod
    def execute(cls, prompt, seed=42):
        if not prompt.strip():
            raise ValueError("提示词不能为空")
        row = _ring4_submit([{"name": "ring4-shot-01", "prompt": prompt,
                              "image_asset_id": "", "audio_asset_id": "",
                              "seed": seed, "request_duration_seconds": 4,
                              "contract_version": "h3-ref2va-v1"}], RING4_TEXT)
        return _ring4_video_output(row)


class DGXH3Ring4Status(io.ComfyNode):
    """Read-only Ring4 availability check; never starts or switches a model."""

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="DGXH3Ring4Status",
            display_name="H3 Ring4 状态（只读）",
            category="DGX/Creative",
            description="查询 h3-ring4 与四个 rank 的就绪状态；不启动、不切换、不占用模型。",
            inputs=[], outputs=[io.String.Output("result_json")], is_output_node=True)

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        return float("nan")

    @classmethod
    def execute(cls):
        value = call("dgx_status", {})
        model = next((m for m in value.get("models", []) if m.get("model_id") == "h3-ring4"), None)
        active = value.get("active_local_model") or {}
        summary = {
            "h3_ring4_state": (model or {}).get("state"),
            "h3_ring4_profile": (model or {}).get("profile"),
            "active_model": active.get("model_id"),
            "active_state": active.get("state"),
            "lock": (value.get("lock") or {}).get("state"),
            "current_operation": (value.get("current_operation") or {}).get("phase"),
            "rank_memory_available_gb": {
                n.get("hostname"): round((n.get("unified_memory") or {}).get("available_bytes", 0) / 1e9, 1)
                for n in value.get("nodes", [])},
        }
        text = json.dumps(summary, ensure_ascii=False, indent=2)
        return io.NodeOutput(text, ui={"text": [text]})



class DGXQwenImageEditHuihui(DGXQwenImageEdit):
    ENCODER = "huihui-abliterated-bf16"

    @classmethod
    def define_schema(cls):
        schema = super().define_schema()
        schema.node_id = "DGXQwenImageEditHuihui"
        schema.display_name = "Qwen 2511 · Huihui 去限制编码器"
        schema.description = "旧工作流单图节点，仅输出1张；多候选请用版本选择与批量节点并选择Huihui编码器。同一2511 FP8主模型，改用Huihui Abliterated BF16编码器。去限制的是编码器语言部分，不代表整个模型或画质全面增强。原生运行、取消及自动释放与标准版相同。"
        return schema


def validate_batch_controls(batch_count, parallelism):
    if type(batch_count) is not int or batch_count < 1:
        raise ValueError("候选总数必须是正整数。")
    if type(parallelism) is not int or parallelism < 1:
        raise ValueError("每批并行数量必须是正整数。")


def image_batch_result(row, requested_count):
    """Keep usable outputs even if another candidate or download failed.

    Candidate identity comes from the versioned Gateway manifest. For historical
    tasks without it we retain artifact ordering and never invent per-image seeds.
    """
    import io as buffers
    import numpy as np
    import torch
    from PIL import Image
    manifest = row.get("image_batch")
    candidates = {c["index"]: c for c in (manifest or {}).get("candidates", [])}
    artifacts = [(index, artifact) for index, artifact in enumerate(row.get("artifacts", [])) if artifact.get("kind") == "images"]
    def order(pair):
        index, artifact = pair
        ordinal = artifact.get("candidate_index")
        return (0, ordinal) if type(ordinal) is int and ordinal >= 0 else (1, index)
    images, decoded, errors, seen = [], [], [], set()
    shape = None
    for source_position, artifact in sorted(artifacts, key=order):
        identity = {"sourcePosition": source_position, "candidateIndex": artifact.get("candidate_index"), "seed": artifact.get("seed"), "url": artifact.get("url")}
        try:
            ordinal = artifact.get("candidate_index")
            if manifest:
                candidate = candidates.get(ordinal)
                if type(ordinal) is not int or candidate is None or ordinal in seen:
                    raise ValueError("候选身份缺失、重复或未列在原任务清单中")
                if artifact.get("seed") != candidate.get("seed"):
                    raise ValueError("产物seed与原候选清单不一致")
            with Image.open(buffers.BytesIO(artifact_bytes(artifact, 32*1024*1024))) as image:
                value = torch.from_numpy(np.array(image.convert("RGB")).astype(np.float32) / 255.0)
            if shape is not None and tuple(value.shape) != shape:
                raise ValueError("图片尺寸与同批成功图不一致，不能合并为IMAGE batch")
            shape = tuple(value.shape)
            if ordinal is not None: seen.add(ordinal)
            images.append(value)
            decoded.append(identity)
        except Exception as exc:
            errors.append({**identity, "error": str(exc)})
    summary = {"job": {key: value for key, value in row.items() if key != "_graph_receipt"},
               "delivery": {"requestedCount": requested_count, "returnedCount": len(images),
                            "complete": row.get("state") == "SUCCEEDED" and len(images) == requested_count and not errors,
                            "identitySource": "gateway_manifest" if manifest else "legacy_artifact_order",
                            "images": decoded, "errors": errors}}
    if not summary["delivery"]["complete"]:
        summary["delivery"]["nextAction"] = "部分结果保留在原任务。同参数再次运行只重新读取原已成功图片，不重拍成功项；请查看任务失败详情。修改seed或生成参数才会创建新一组，本节点不提供失败子集重拍。"
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    if not images:
        raise RuntimeError(f"任务 {row.get('job_id')} 没有可交付图片；原任务回执仍保留，请核查后再创建新一组。详情：{text}")
    if summary["delivery"]["complete"]:
        delivered(row)
    else:
        delivered(row, partial=True)
    return io.NodeOutput(torch.stack(images), text, ui={"text": [text]})


class DGXQwenImageEditBatch(DGXQwenImageEdit):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="DGXQwenImageEditBatch", display_name="Qwen 2511 · 版本选择与批量", category="DGX/Creative",
            description="同一参考图生成自定义数量的候选，每批并行数自定，种子逐张加1。并行实际值以任务JSON为准。第一输出为所有成功图片；第二输出记录完整任务、候选身份与部分失败；部分交付后同参重跑仍读原任务，更改seed/参数才是新组。参考输入仍为1张。",
            inputs=[io.Image.Input("reference_image"),
                io.String.Input("prompt", default="保持角色和构图，改成柔和的夕阳光线。", multiline=True, dynamic_prompts=False),
                io.Combo.Input("model_precision", options=["fp8", "bf16"], default="fp8"),
                io.Combo.Input("encoder", options=["standard", "huihui-abliterated-bf16"], default="standard"),
                io.Int.Input("batch_count", default=1, min=1),
                io.Int.Input("seed", default=42, min=0, max=2147483647),
                io.Int.Input("steps", default=20, min=20, max=40),
                io.Int.Input("parallelism", default=2, min=1, optional=True)],
            outputs=[io.Image.Output("image"), io.String.Output("job_json")])

    @classmethod
    def execute(cls, reference_image, prompt, model_precision="fp8", encoder="standard", batch_count=1, seed=42, steps=20, parallelism=2):
        validate_batch_controls(batch_count, parallelism)
        if len(reference_image) != 1:
            raise ValueError("请选择一张参考图；batch_count用于生成多张候选图片。")
        asset = register_image(reference_image)
        row = submit_and_wait("edit_image", {"asset_ids":[asset], "prompt":prompt, "seed":seed, "steps":steps,
            "model_precision":model_precision, "encoder":encoder, "batch_count":batch_count, "parallelism":parallelism}, "comfy", allow_partial=True)
        return image_batch_result(row, batch_count)


class DGXZImageBatch(DGXQwenImageEdit):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="DGXZImageBatch", display_name="Z-Image · 文字生图与多候选", category="DGX/Creative",
            description="从文字生成自定义数量的快图预览。这是显式预览节点，不是质量成片路径；成片请用 FLUX 标准模型。Gateway统一排程及释放模型；成功图片完整输出，部分失败保留成功图并在job_json说明。",
            inputs=[io.String.Input("prompt", default="电影剧照，湖畔的人物，全景，柔和的黄昏光线。", multiline=True, dynamic_prompts=False),
                io.Int.Input("width", default=1024, min=256, max=1536, step=16),
                io.Int.Input("height", default=1024, min=256, max=1536, step=16),
                io.Int.Input("batch_count", default=1, min=1),
                io.Int.Input("seed", default=42, min=0, max=2147483647),
                io.Int.Input("parallelism", default=2, min=1)],
            outputs=[io.Image.Output("image"), io.String.Output("job_json")])

    @classmethod
    def execute(cls, prompt, width=1024, height=1024, batch_count=1, seed=42, parallelism=2):
        validate_batch_controls(batch_count, parallelism)
        row = submit_and_wait("generate_image", {"model": "z-image-turbo", "preview": True, "prompt":prompt, "width":width, "height":height,
            "seed":seed, "batch_count":batch_count, "parallelism":parallelism}, "comfy", allow_partial=True)
        return image_batch_result(row, batch_count)


class DGXImageModelBatch(DGXQwenImageEdit):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="DGXImageModelBatch", display_name="DGX · 质量生图", category="DGX/Creative",
            description="通过唯一Gateway生成多候选。默认质量模型是 flux2-dev。z-image-turbo 只作为显式预览。候选总数和每批并行数由用户填写；逐张种子及实际GPU并行见job_json。",
            inputs=[io.Combo.Input("model", options=["flux2-dev", "z-image-turbo"]),
                io.String.Input("prompt", default="电影剧照，湖畔的人物，全景，柔和的黄昏光线。", multiline=True, dynamic_prompts=False),
                io.Int.Input("width", default=1024, min=256, max=1536, step=32),
                io.Int.Input("height", default=1024, min=256, max=1536, step=32),
                io.Int.Input("batch_count", default=2, min=1),
                io.Int.Input("seed", default=42, min=0, max=2147483647),
                io.Int.Input("parallelism", default=2, min=1)],
            outputs=[io.Image.Output("image"), io.String.Output("job_json")])

    @classmethod
    def execute(cls, model, prompt, width=1024, height=1024, batch_count=2, seed=42, parallelism=2):
        validate_batch_controls(batch_count, parallelism)
        payload = {"model": model, "prompt": prompt, "width": width, "height": height,
            "seed": seed, "batch_count": batch_count, "parallelism": parallelism}
        if model == "z-image-turbo":
            payload["preview"] = True
        row = submit_and_wait("generate_image", payload, "comfy", allow_partial=True)
        return image_batch_result(row, batch_count)
