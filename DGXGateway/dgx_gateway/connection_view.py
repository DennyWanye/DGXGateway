from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from .config import GatewayConfig
from .domain import ActiveModelClassification
from .comfy_identity import COMFY_SERVICE, is_comfy_service


TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43,128}")
H3_PROFILES = frozenset({"heretic-ref2va-v1", "heretic-ref2va-hmnsfw-v1", "10eros-fl2va-v1"})


def _read_shared_api_key(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError("shared inference API key is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) not in {0o400, 0o440, 0o600}:
            raise RuntimeError("shared inference API key has unsafe permissions")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(130)
        if len(raw) > 129:
            raise RuntimeError("shared inference API key is too large")
        token = raw.decode("utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError("shared inference API key is unavailable") from exc
    finally:
        os.close(descriptor)
    if not TOKEN_PATTERN.fullmatch(token):
        raise RuntimeError("shared inference API key has an invalid format")
    return token


def active_connection_view(
    config: GatewayConfig,
    active: ActiveModelClassification,
    *,
    observed_state: str = "HEALTHY",
) -> dict | None:
    if active.state != "ACTIVE" or not active.model_id:
        return None
    if observed_state != "HEALTHY":
        return {
            "available": False,
            "reason": "当前模型尚未通过健康检查，暂不发布连接参数",
        }

    head = next(node for node in config.nodes if node.role == "head")
    model_id = active.model_id
    if model_id == "deepseek-v4-flash":
        connection = {
            "api_type": "OpenAI 兼容 · 文本",
            "base_url": f"http://{head.address}:11434/v1",
            "model_name": "deepseek-v4-flash-0731-abliterated-anchored",
            "request_path": "/chat/completions",
            "api_key_required": True,
        }
    elif model_id == "deepseek-v4-flash-vision-exp":
        connection = {
            "api_type": "OpenAI 兼容 · 多模态图像",
            "base_url": f"http://{head.address}:11434/v1",
            "model_name": "deepseek-v4-flash-vision-exp",
            "request_path": "/chat/completions",
            "api_key_required": True,
        }
    elif model_id == "qwen38-flash-next":
        connection = {
            "api_type": "OpenAI 兼容 · Qwen 文本 / 图像 · 32K / 256K",
            "base_url": f"http://{head.address}:11434/v1",
            "model_name": "qwen38-flash-next",
            "request_path": "/chat/completions",
            "api_key_required": True,
        }
    elif model_id == "mia-dsv41-ablit":
        connection = {
            "api_type": "OpenAI 兼容 · Mia DeepSeek V4.1 Abliterated · 256K / 512K",
            "base_url": f"http://{head.address}:11434/v1",
            "model_name": "mia-dsv41-ablit",
            "request_path": "/chat/completions",
            "api_key_required": True,
        }
    elif model_id == "longcat-video-avatar-1.5":
        return {"available": True, "api_type": "LongCat · 异步数字人视频",
                "base_url": f"http://{config.bind}:{config.port}", "model_name": model_id,
                "request_path": "/api/v1/avatar-jobs", "api_key_required": False, "api_key": "",
                "credential_source": "现有 Gateway 会话或局域网访问模式"}
    elif model_id == "minimax-h3":
        profile = active.profile
        if profile not in H3_PROFILES:
            return {
                "available": False,
                "reason": "当前 H3 Profile 无法确认，暂不发布连接参数",
            }
        connection = {
            "api_type": "MiniMax H3 · 视频生成",
            "base_url": f"http://{head.address}:18081/v1",
            "model_name": f"/home/operator/dgx-models/video-generation/snapshots/minimax-h3-{profile}",
            "request_path": "/videos/sync",
            "api_key_required": True,
        }
    elif model_id == "h3-ring4":
        profile = active.profile
        if profile not in {"fl2va-official", "ref2va-official", "ref2va-heretic", "hybrid-experimental"}:
            return {
                "available": False,
                "reason": "当前 H3 Ring4 Profile 无法确认，暂不发布连接参数",
            }
        return {
            "available": True,
            "api_type": "MiniMax H3 Ring4 · 异步视频",
            "base_url": "http://127.0.0.1:30021/v1",
            "model_name": profile,
            "request_path": "/videos",
            "api_key_required": False,
            "api_key": "",
            "credential_source": "SSH 隧道 operator@192.0.2.41 → 127.0.0.1:30021；工作台 4 秒批次经 Gateway 映射到此服务",
        }
    elif model_id == "h3-dual-pair":
        profile = active.profile
        if profile not in {"fl2va-official", "ref2va-official", "ref2va-heretic"}:
            return {"available": False, "reason": "当前双路双节点 H3 Profile 无法确认"}
        return {
            "available": True,
            "api_type": "MiniMax H3 · 双路双节点异步视频",
            "base_url": f"http://{config.bind}:{config.port}",
            "model_name": profile,
            "request_path": "/mcp (video_batch_submit)",
            "api_key_required": False,
            "api_key": "",
            "credential_source": "Gateway 将队列固定派发到 pair-a(0+1) 或 pair-b(2+3)，后端 API 仅绑定各自 head 回环",
        }
    elif is_comfy_service(model_id):
        connection = {
            "api_type": "ComfyUI · 图片 / 音乐工作流",
            "base_url": f"http://{head.address}:{config.music3_port}",
            "model_name": COMFY_SERVICE,
            "request_path": "/prompt",
            "api_key_required": False,
        }
    elif model_id == "personaplex":
        return {"available": True, "api_type": "PersonaPlex · 全双工语音 WebSocket", "base_url": f"http://{head.address}:8998", "model_name": "personaplex-7b-v1", "request_path": "/api/chat", "api_key_required": False, "api_key": "", "credential_source": "局域网免登录"}
    elif model_id == "dsv41-ring-tp4":
        secret = Path(getattr(config, "dsv41_ring_secret", Path.home() / ".config/dgx-cluster-gateway/dsv41-ring.token"))
        try:
            api_key = _read_shared_api_key(secret)
        except RuntimeError:
            return {
                "available": False,
                "reason": "Ring API Key 不可用，请检查 dsv41-ring 凭据文件",
            }
        return {
            "available": True,
            "api_type": "OpenAI 兼容 · DeepSeek-V4.1-Flash 四机 Ring TP4",
            "base_url": "http://127.0.0.1:8891/v1",
            "model_name": "deepseek-v4.1-flash-ring",
            "request_path": "/chat/completions",
            "api_key_required": True,
            "api_key": api_key,
            "credential_source": "SSH 隧道 operator@192.0.2.41 → 127.0.0.1:8891；Ring 专用凭据，不是旧 DeepSeek/Mia 密钥",
        }
    else:
        return {
            "available": False,
            "reason": "当前模型没有可发布的连接契约",
        }

    if not connection["api_key_required"]:
        return {"available": True, **connection, "api_key": "", "credential_source": "局域网免登录"}

    try:
        api_key = _read_shared_api_key(config.deepseek_gateway_secret)
    except RuntimeError:
        return {
            "available": False,
            "reason": "统一 API Key 不可用，请检查旧 DeepSeek 凭据文件",
        }
    return {
        "available": True,
        **connection,
        "api_key": api_key,
        "credential_source": "旧 DeepSeek V4 Flash 统一凭据",
    }
