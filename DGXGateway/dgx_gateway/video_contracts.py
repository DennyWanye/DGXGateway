"""Versioned, server-owned H3 generation contracts (runtime evidence != integration evidence)."""
from copy import deepcopy

CONTRACT_VERSION = "h3-ref2va-v1"
STABLE_PROFILE = "heretic-ref2va-v1"
LONG_PROFILE = "heretic-ref2va-hmnsfw-v1"
TEXT_PROFILE = "10eros-fl2va-v1"
PROFILES = (STABLE_PROFILE, LONG_PROFILE, TEXT_PROFILE)
RING4_MODEL = "h3-ring4"
# Retained for reading and safely finishing historical dual-pair jobs. New
# production submissions are routed through Ring4 below.
RUNTIME_MODEL = "h3-dual-pair"
TWO_NODE_MODEL = "minimax-h3"
# Workbench IDs stay stable so saved shots still validate. Runtime mapping is
# Gateway-owned: newly submitted work uses the four-node Ring4 service.
# Historical rows freeze their runtime identity and are not rewritten.
RUNTIME_BY_PROFILE = {
    STABLE_PROFILE: (RING4_MODEL, "ref2va-official", "ref2va"),
    TEXT_PROFILE: (RING4_MODEL, "fl2va-official", "t2va"),
    LONG_PROFILE: (RING4_MODEL, "ref2va-heretic", "ref2va"),
}


def runtime_target(profile):
    if profile not in RUNTIME_BY_PROFILE:
        raise ValueError("unsupported H3 video profile")
    return RUNTIME_BY_PROFILE[profile]


def normalize_generation(value=None, *, workbench=True):
    if value is None:
        value = {"contractVersion": CONTRACT_VERSION, "profile": STABLE_PROFILE, "requestDurationSeconds": 4}
    if not isinstance(value, dict) or set(value) != {"contractVersion", "profile", "requestDurationSeconds"}:
        raise ValueError("生成设置字段不符合已发布合同")
    profile, duration = value["profile"], value["requestDurationSeconds"]
    if value["contractVersion"] != CONTRACT_VERSION or profile not in PROFILES:
        raise ValueError("生成合同版本或 H3 profile 不受支持")
    if type(duration) is not int or not (
            duration == 4 if profile == STABLE_PROFILE
            else 4 <= duration <= 15):
        raise ValueError("历史稳定档固定 4 秒；H3 双路双节点支持 4-15 秒整数时长")
    return dict(value)


def generation_for_references(value, has_reference):
    generation = normalize_generation(value)
    # Every new workbench request stays Ref2VA, including zero references.
    # Historical profile IDs remain readable; frozen jobs are not rewritten.
    return {**generation, "profile": LONG_PROFILE}


def generation_for_reference(value, image):
    """Backward-compatible alias for older one-image callers."""
    return generation_for_references(value, bool(image))


def gateway_generation(profile=STABLE_PROFILE, shot=None):
    shot = shot or {}
    return normalize_generation({"contractVersion": shot.get("contract_version", CONTRACT_VERSION),
                                 "profile": profile, "requestDurationSeconds": shot.get("request_duration_seconds", 4)}, workbench=False)


def video_capabilities():
    common = {"contractVersion": CONTRACT_VERSION, "inputMode": "adaptive_multimodal", "persistentBatchSupported": True,
              "output": {"width": 1344, "height": 768, "fps": 24}, "referenceAudioSeconds": 4,
              "executionMode": "ring4_four_node", "executionSlots": 1, "singleProfilePerBatch": True}
    return {"schemaVersion": "drama-video-capabilities-v2",
            "workbenchMode": {"id": "h3-ring4", "name": "H3 Ring4", "durationSeconds": {"minimum": 4, "maximum": 15, "step": 1},
                              "optionalInputs": ["text", "reference_image", "action_image", "reference_video", "reference_audio"],
                              "routing": "ref2va_optional_inputs"},
            "profiles": [
        {**deepcopy(common), "id": STABLE_PROFILE, "name": "H3 Ring4 · 稳定 4 秒", "allowedDurationSeconds": [4],
         "verifiedDirectDurationSeconds": [4], "verifiedPersistentDurationSeconds": [4], "workbenchDurationSeconds": [4],
         "wait": {"connectTimeoutSeconds": 3700, "totalTimeoutSeconds": 3700}, "cancellation": "clip_boundary",
         "evidence": "Ring4 四节点稳定4秒输入合同"},
        {**deepcopy(common), "id": TEXT_PROFILE, "name": "H3 Ring4 · 文字路由", "inputMode": "text",
         "referenceAudioSeconds": None, "allowedDurationSeconds": {"minimum": 4, "maximum": 15, "step": 1}, "workbenchDurationSeconds": list(range(4, 16)),
         "verifiedDirectDurationSeconds": [], "verifiedPersistentDurationSeconds": [],
         "wait": {"connectTimeoutSeconds": 30, "totalTimeoutSeconds": None}, "cancellation": "clip_boundary",
         "evidence": "Ring4 四节点 FL2VA 文字路由"},
        {**deepcopy(common), "id": LONG_PROFILE, "name": "H3 Ring4 · 参考路由", "allowedDurationSeconds": {"minimum": 4, "maximum": 15, "step": 1},
         "verifiedDirectDurationSeconds": [4, 15], "verifiedPersistentDurationSeconds": [4, 15],
         "workbenchDurationSeconds": list(range(4, 16)),
         "wait": {"connectTimeoutSeconds": 10, "totalTimeoutSeconds": None}, "cancellation": "clip_boundary",
         "evidence": "Ring4 四节点 Ref2VA 参考路由，支持4–15秒整数时长"}]}
