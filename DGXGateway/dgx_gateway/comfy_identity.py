"""Head ComfyUI lifecycle identity.

Image and music workflows share one GPU Comfy process. The process is not MiniMax
Music 3. New operations, residency, and catalog entries use `comfyui`. The
historical `minimax-music3` id remains an input alias and a display alias so
in-flight and historical ledgers stay readable.
"""
from __future__ import annotations

COMFY_SERVICE = "comfyui"
LEGACY_COMFY_SERVICE = "minimax-music3"
COMFY_SERVICE_IDS = frozenset({COMFY_SERVICE, LEGACY_COMFY_SERVICE})
COMFY_UNIT = "dgx-gateway-comfyui"
LEGACY_COMFY_UNIT = "dgx-gateway-music3"


def is_comfy_service(model_id: str | None) -> bool:
    return model_id in COMFY_SERVICE_IDS


def canonical_service(model_id: str | None) -> str | None:
    if model_id == LEGACY_COMFY_SERVICE:
        return COMFY_SERVICE
    return model_id


def same_service(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return False
    return canonical_service(left) == canonical_service(right)
