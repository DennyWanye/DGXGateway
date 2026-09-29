"""Read-only runtime labels for the live ComfyUI or H3 service.

Job metadata describes the workflow being executed, never observed GPU weights.
Only the manager's active pointer is consulted: historical jobs cannot identify
what is currently loaded in a shared ComfyUI service. The Comfy host identity is
`comfyui`; historical `minimax-music3` labels still map to the same service.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .comfy_identity import COMFY_SERVICE, is_comfy_service, same_service
from .image_assets import _read_regular
_ID = re.compile(r"[a-f0-9]{32}\Z")
_TERMINAL = {"SUCCEEDED", "FAILED", "INTERRUPTED", "CANCELED", "BLOCKED"}
_PHASES = {
    "QUEUED": ("QUEUED", "任务排队中"),
    "LOADING": ("LOADING", "正在准备模型服务"),
    "UPLOADING": ("PREPARING", "正在上传参考素材"),
    "SUBMITTING": ("SUBMITTING", "正在提交制作请求"),
    "RUNNING": ("RUNNING", "工作流执行中（包含权重加载与生成）"),
    "RELEASING": ("RELEASING", "正在处理模型资源"),
    "CANCELING": ("CANCELING", "正在取消，等待安全释放"),
}
_MODELS = {
    "flux2_dev_fp8mixed.safetensors": "FLUX.2 dev · FP8 mixed",
    "LLaDA-Image-Turbo-INT8.safetensors": "LLaDA Image Turbo · INT8",
    "z_image_turbo_bf16.safetensors": "Z-Image Turbo · BF16",
    "qwen_image_edit_2511_fp8mixed.safetensors": "Qwen Image Edit 2511 · FP8",
    "qwen_image_edit_2511_bf16.safetensors": "Qwen Image Edit 2511 · BF16",
    "minimax_music3_dit_fp16.safetensors": "MiniMax Music 3 · FP16",
}


def service_name(model_id):
    return "ComfyUI 制作服务" if is_comfy_service(model_id) else model_id


def job_runtime_view(row: dict, kind: str) -> dict:
    """Safe task detail, including historical tasks, without claiming residency."""
    model_name = None
    if kind == "video":
        model_name = "MiniMax H3"
    elif kind == "creative":
        # Use actual workflow loaders, not user-provided display names or a
        # typed-request label that could disagree with a raw graph.
        workflow = row.get("workflow") or {}
        loaders = [node["inputs"].get("unet_name", node["inputs"].get("diffusion_model"))
                   for node in (workflow.values() if isinstance(workflow, dict) else ())
                   if isinstance(node, dict) and node.get("class_type") in {"UNETLoader", "LLaDAImageLoader"} and isinstance(node.get("inputs"), dict)]
        names = {name for name in loaders if isinstance(name, str)}
        if len(names) == 1:
            model_name = _MODELS.get(next(iter(names)))
            if next(iter(names)) == 'flux2_dev_fp8mixed.safetensors':
                clips = {node.get('inputs', {}).get('clip_name') for node in workflow.values()
                         if isinstance(node, dict) and node.get('class_type') == 'CLIPLoader'}
                if clips == {'mistral_small_3_abliterated_v2_fp8mixed.safetensors'}:
                    model_name += ' · silveroxides 去限制编码器'
                elif clips == {'mistral_3_small_flux2_bf16.safetensors'}:
                    model_name += ' · 标准编码器'

    phase, label = _PHASES.get(row.get("state"), ("UNKNOWN", "任务状态待确认"))
    if row.get("cleanup_required") or row.get("state") in {"INTERRUPTED", "SUBMISSION_UNKNOWN"}:
        phase, label = "UNKNOWN", "需要核查原任务与资源状态"
    elif row.get("state") in _TERMINAL:
        if row.get("memory_released") is True:
            phase, label = "RELEASED", "任务结束，资源已释放"
        elif (row.get("resource_settled") is True and row.get("resource_status") == "resident"
              and row.get("memory_released") is False):
            phase, label = "RESIDENT", "任务结束，模型暂留待复用；空闲后自动释放"
        else:
            phase, label = "UNKNOWN", "任务结束，资源状态待确认"
    return {
        "job_id": row.get("job_id"), "kind": kind,
        "service_id": "minimax-h3" if kind == "video" else COMFY_SERVICE,
        "model_name": model_name, "model_identity_basis": "workflow_definition" if model_name else "unknown",
        "profile": row.get("profile"), "phase": phase, "phase_label": label,
        "start_operation_id": row.get("start_operation_id"),
        "observed_weights": False,
    }


def runtime_view(snapshot: dict, active_jobs=(), *, operation_origin=None, read_error=False) -> dict:
    active = snapshot.get("active_local_model") or {}
    operation = snapshot.get("current_operation") or snapshot.get("latest_operation") or {}
    service_id = active.get("model_id")
    service_state = active.get("state", "UNKNOWN")
    result = {
        "service_id": service_id, "service_name": service_name(service_id), "service_state": service_state,
        "phase": "UNKNOWN", "phase_label": "运行状态待确认", "workload": None,
        "origin": {"kind": "unknown", "label": "启动来源未知"},
        "note": "服务健康不代表已加载具体工作流权重。" if is_comfy_service(service_id) else "",
    }
    jobs = [row for row in active_jobs if row.get("state") not in _TERMINAL or row.get("cleanup_required")]
    if read_error or len(jobs) > 1:
        result["note"] = "活动任务信息不可确认，请检查任务与资源状态。"
        return result
    if operation.get("phase") == "RECOVERY_REQUIRED":
        result["note"] = "生命周期操作需要恢复确认，不能据旧任务判断当前模型。"
        return result
    if jobs:
        row = jobs[0]
        workload = job_runtime_view(row, row["kind"])
        result.update(workload=workload, phase=workload["phase"], phase_label=workload["phase_label"])
        bound_ids = {row.get(f"{suffix}_operation_id") for suffix in ("start", "stop", "cleanup", "reconcile_stop")}
        bound_ids.discard(None)
        bound = operation.get("operation_id") in bound_ids and bool(row.get("start_operation_id"))
        if bound:
            result["origin"] = {"kind": "automatic_job", "label": "制作任务自动启动", "job_id": row["job_id"],
                                "operation_id": row["start_operation_id"]}
        elif operation.get("operation_id"):
            # A foreign/later lifecycle change supersedes this job's claim.
            result.update(phase="UNKNOWN", phase_label="任务与当前服务归属待确认")
        if service_id and not same_service(service_id, workload["service_id"]) and workload["phase"] not in {"QUEUED", "LOADING"}:
            result.update(phase="UNKNOWN", phase_label="任务与当前服务不一致")
        if workload["phase"] in {"RUNNING", "PREPARING", "SUBMITTING"} and service_state != "ACTIVE":
            result.update(phase="UNKNOWN", phase_label="任务仍有记录，服务运行状态待确认")
        return result
    residency = snapshot.get("residency") or {}
    if residency.get("state") in {"RELEASING", "RECOVERY_REQUIRED"}:
        result.update(phase="RELEASING" if residency["state"] == "RELEASING" else "UNKNOWN",
                      phase_label="正在安全释放驻留资源" if residency["state"] == "RELEASING" else "驻留资源需要恢复确认")
        return result
    if residency.get("state") == "IDLE":
        latest = snapshot.get("latest_operation") or {}
        resident_proven = (
            not snapshot.get("current_operation")
            and bool(residency.get("owner_job_id"))
            and bool(residency.get("start_operation_id"))
            and latest.get("operation_id") == residency["start_operation_id"]
            and latest.get("phase") == "RELEASED" and latest.get("outcome") == "COMMITTED"
            and same_service(residency.get("model_id"), service_id) and service_state == "ACTIVE"
            and same_service(latest.get("target_model"), service_id)
            and (service_id != "minimax-h3" or active.get("profile") == residency.get("profile"))
        )
        if not resident_proven:
            result.update(phase="UNKNOWN", phase_label="驻留归属与当前服务待确认",
                          note="驻留记录未与最新已完成启动操作一致，不能认定为可复用模型。")
            return result
        result.update(phase="IDLE", phase_label="空闲待复用，稍后自动释放",
                      origin={"kind": "automatic_job", "label": "制作任务保留资源，等待复用",
                              "job_id": residency["owner_job_id"], "operation_id": residency["start_operation_id"]})
        # Residency is a live ownership contract from the controller, not a
        # historical job lookup. H3 identity follows the observed service;
        # Comfy weights remain unknown without a verified workload contract.
        return result
    if snapshot.get("current_operation"):
        phase = operation.get("phase")
        if phase in {"TARGET_STARTING", "TARGET_HEALTH", "TARGET_HEALTHY"}:
            result.update(phase="LOADING", phase_label="正在启动或检查目标服务")
        elif phase in {"SOURCE_DRAINING", "SOURCE_STOPPING", "SOURCE_STOPPED_PROVEN", "RELEASING"}:
            result.update(phase="RELEASING", phase_label="正在切换并释放资源")
    elif service_state == "NONE":
        result.update(phase="STOPPED", phase_label="当前没有运行中的本地模型服务")
    elif service_state == "ACTIVE":
        result.update(phase="IDLE", phase_label="服务已就绪；没有 Gateway 活动制作任务")
    if (operation_origin and operation_origin.get("kind") in {"manual", "api", "gateway_restart"}
            and operation_origin.get("operation_id") == operation.get("operation_id")):
        result["origin"] = {"kind": operation_origin["kind"], "operation_id": operation["operation_id"],
                            "label": {"manual": "用户在 Gateway 手动操作", "api": "API 发起的生命周期操作",
                                      "gateway_restart": "Gateway API 服务重启（不代表模型切换）"}[operation_origin["kind"]]}
    return result


class RuntimeViewProvider:
    """Local bounded reads only; no get()/reconcile(), probes or mutations."""
    def __init__(self, app):
        self.app = app

    def __call__(self, snapshot):
        rows, read_error = [], False
        for manager_name, kind in (("creative_jobs", "creative"), ("video_jobs", "video")):
            manager = getattr(self.app, manager_name, None)
            job_id = getattr(manager, "active", None)
            if job_id is None:
                continue
            try:
                if not isinstance(job_id, str) or not _ID.fullmatch(job_id):
                    raise ValueError("invalid active pointer")
                row = json.loads(_read_regular(Path(manager.root) / (job_id + ".json"), 2 * 1024 * 1024))
                if not isinstance(row, dict) or row.get("job_id") != job_id:
                    raise ValueError("active identity mismatch")
                row["kind"] = kind
                rows.append(row)
            except (ValueError, OSError, TypeError):
                read_error = True
        operation = snapshot.get("current_operation") or snapshot.get("latest_operation") or {}
        origin = None
        if operation.get("operation_id"):
            try:
                for event in self.app.store.events(operation["operation_id"]):
                    if event.get("kind") == "request-origin":
                        origin = {"kind": (event.get("payload") or {}).get("kind"), "operation_id": operation["operation_id"]}
                        break
            except (OSError, ValueError):
                pass  # Origin is optional evidence, never guessed on failure.
        return runtime_view(snapshot, rows, operation_origin=origin, read_error=read_error)
