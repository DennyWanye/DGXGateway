from __future__ import annotations

import pathlib

import folder_paths
from comfy_api.latest import ComfyExtension, InputImpl, io, ui

from .contract import load_runtime_contract
from .creative_node import DGXCreativeTool
from .drama_node import DGXDramaShotDraft
from .creative_graph import DGXQwenImageEdit, DGXH3Video, DGXQwenImageEditHuihui, DGXQwenImageEditBatch, DGXZImageBatch, DGXImageModelBatch, DGXH3Ring4Ref2VA, DGXH3Ring4T2VA, DGXH3Ring4Status
from .deployment import EXPECTED_PROFILE
from .policy import InputError, MAX_SEED, RESOLUTIONS, validate_fl2va_inputs, validate_inputs, validate_ref2va_inputs
from .profile_control import PUBLIC_PROFILES, profile_action
from .service import execute_fl2va_request, execute_ref2va_request, execute_request
from .storage import cleanup_stale_staging


class DGXRemoteMiniMaxH3ProfileManager(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="DGXRemoteMiniMaxH3ProfileManager",
            display_name="DGX MiniMax H3 Profile Manager",
            category="DGX/Remote H3",
            description="Explicitly inspect, switch, or stop the two-node H3 profile. Never switches during generation.",
            inputs=[
                io.Combo.Input("profile", display_name="Profile", options=list(PUBLIC_PROFILES), default="heretic-ref2va-v1"),
                io.Combo.Input("action", display_name="Action", options=["status", "switch", "stop"], default="status"),
            ],
            outputs=[io.String.Output("result", display_name="Profile status")],
            is_output_node=True,
        )

    @classmethod
    def fingerprint_inputs(cls, profile, action):
        del profile, action
        return float("nan")

    @classmethod
    def execute(cls, profile, action) -> io.NodeOutput:
        import json

        result = profile_action(action, profile)
        return io.NodeOutput(json.dumps(result, ensure_ascii=False, sort_keys=True))


class DGXRemoteMiniMaxH3T2VA(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="DGXRemoteMiniMaxH3T2VA",
            display_name="DGX Remote MiniMax H3 T2VA",
            category="DGX/Remote H3",
            description=(
                "Generate one audio-bearing MP4 through the fixed two-node MiniMax H3 gateway. "
                "The synchronous request is never retried; cancelling the UI does not guarantee server-side cancellation."
            ),
            inputs=[
                io.String.Input(
                    "prompt",
                    display_name="Prompt",
                    multiline=True,
                    default="",
                    dynamic_prompts=False,
                ),
                io.Combo.Input(
                    "resolution",
                    display_name="Resolution",
                    options=list(RESOLUTIONS),
                    default="768x448",
                ),
                io.Int.Input(
                    "duration",
                    display_name="Duration (seconds)",
                    default=2,
                    min=2,
                    max=4,
                    step=1,
                ),
                io.Int.Input(
                    "seed",
                    display_name="Seed",
                    default=0,
                    min=0,
                    max=MAX_SEED,
                    step=1,
                ),
            ],
            outputs=[io.Video.Output("video", display_name="Generated video")],
            is_output_node=True,
        )

    @classmethod
    def validate_inputs(cls, prompt, resolution, duration, seed):
        try:
            validate_inputs(prompt, resolution, duration, seed)
        except InputError as exc:
            return str(exc)
        return True

    @classmethod
    def fingerprint_inputs(cls, prompt, resolution, duration, seed):
        del prompt, resolution, duration, seed
        return float("nan")

    @classmethod
    def execute(cls, prompt, resolution, duration, seed) -> io.NodeOutput:
        request = validate_inputs(prompt, resolution, duration, seed)
        contract = load_runtime_contract(expected_profile=EXPECTED_PROFILE)
        output_directory = folder_paths.get_output_directory()
        cleanup_stale_staging(output_directory)
        published = execute_request(
            request,
            output_directory=output_directory,
            contract=contract,
            required_profile="10eros-fl2va-v1",
        )
        output_root = pathlib.Path(output_directory).resolve()
        published = pathlib.Path(published).resolve(strict=True)
        if published.parent != output_root:
            raise RuntimeError("Generated video was not published in the ComfyUI output directory.")
        video = InputImpl.VideoFromFile(str(published))
        preview = ui.PreviewVideo(
            [ui.SavedResult(published.name, "", io.FolderType.output)]
        )
        return io.NodeOutput(video, ui=preview)


class DGXRemoteMiniMaxH3FL2VA(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="DGXRemoteMiniMaxH3FL2VA",
            display_name="DGX Remote MiniMax H3 FL2VA",
            category="DGX/Remote H3",
            description=(
                "Animate one first-frame image with the active 10Eros FL2VA two-node profile. "
                "The node never switches profiles automatically."
            ),
            inputs=[
                io.Image.Input("first_frame"),
                io.String.Input(
                    "prompt", display_name="Prompt", multiline=True, default="", dynamic_prompts=False
                ),
                io.Combo.Input(
                    "resolution", display_name="Resolution", options=list(RESOLUTIONS), default="768x448"
                ),
                io.Int.Input("duration", display_name="Duration (seconds)", default=4, min=2, max=4, step=1),
                io.Int.Input("seed", display_name="Seed", default=0, min=0, max=MAX_SEED, step=1),
            ],
            outputs=[io.Video.Output("video", display_name="Generated video")],
            is_output_node=True,
        )

    @classmethod
    def validate_inputs(cls, first_frame, prompt, resolution, duration, seed):
        del first_frame
        try:
            validate_fl2va_inputs(prompt, resolution, duration, seed)
        except InputError as exc:
            return str(exc)
        return True

    @classmethod
    def fingerprint_inputs(cls, first_frame, prompt, resolution, duration, seed):
        del first_frame, prompt, resolution, duration, seed
        return float("nan")

    @classmethod
    def execute(cls, first_frame, prompt, resolution, duration, seed) -> io.NodeOutput:
        request = validate_fl2va_inputs(prompt, resolution, duration, seed)
        contract = load_runtime_contract(expected_profile=EXPECTED_PROFILE)
        output_directory = folder_paths.get_output_directory()
        cleanup_stale_staging(output_directory)
        published = execute_fl2va_request(
            request,
            first_frame,
            output_directory=output_directory,
            contract=contract,
            required_profile="10eros-fl2va-v1",
        )
        output_root = pathlib.Path(output_directory).resolve()
        published = pathlib.Path(published).resolve(strict=True)
        if published.parent != output_root:
            raise RuntimeError("Generated video was not published in the ComfyUI output directory.")
        video = InputImpl.VideoFromFile(str(published))
        preview = ui.PreviewVideo([ui.SavedResult(published.name, "", io.FolderType.output)])
        return io.NodeOutput(video, ui=preview)


class DGXRemoteMiniMaxH3Ref2VA(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="DGXRemoteMiniMaxH3Ref2VA",
            display_name="DGX Remote MiniMax H3 Ref2VA",
            category="DGX/Remote H3",
            description=(
                "Generate one 1344x768, four-second audio-bearing MP4 from one reference image. "
                "The active two-node H3 profile must be heretic-ref2va-v1."
            ),
            inputs=[
                io.Image.Input("reference_image"),
                io.Audio.Input("reference_audio"),
                io.Combo.Input(
                    "profile",
                    display_name="Profile",
                    options=["heretic-ref2va-v1", "heretic-ref2va-hmnsfw-v1"],
                    default="heretic-ref2va-v1",
                    optional=True,
                ),
                io.String.Input(
                    "prompt",
                    display_name="Prompt",
                    multiline=True,
                    default="",
                    dynamic_prompts=False,
                ),
                io.Int.Input(
                    "seed",
                    display_name="Seed",
                    default=0,
                    min=0,
                    max=MAX_SEED,
                    step=1,
                ),
            ],
            outputs=[io.Video.Output("video", display_name="Generated video")],
            is_output_node=True,
        )

    @classmethod
    def validate_inputs(
        cls,
        reference_image,
        reference_audio,
        profile="heretic-ref2va-v1",
        prompt="",
        seed=0,
    ):
        # Linked values are deliberately unavailable during ComfyUI's static
        # graph validation and arrive here as None. The required IMAGE schema
        # validates presence and link type before execution.
        del reference_image, reference_audio
        if profile not in {"heretic-ref2va-v1", "heretic-ref2va-hmnsfw-v1"}:
            return "Unsupported Ref2VA profile."
        try:
            validate_ref2va_inputs(prompt, seed)
        except InputError as exc:
            return str(exc)
        return True

    @classmethod
    def fingerprint_inputs(
        cls,
        reference_image,
        reference_audio,
        profile="heretic-ref2va-v1",
        prompt="",
        seed=0,
    ):
        del reference_image, reference_audio, profile, prompt, seed
        return float("nan")

    @classmethod
    def execute(
        cls,
        reference_image,
        reference_audio,
        profile="heretic-ref2va-v1",
        prompt="",
        seed=0,
    ) -> io.NodeOutput:
        request = validate_ref2va_inputs(prompt, seed)
        contract = load_runtime_contract(expected_profile=EXPECTED_PROFILE)
        output_directory = folder_paths.get_output_directory()
        cleanup_stale_staging(output_directory)
        published = execute_ref2va_request(
            request,
            reference_image,
            reference_audio,
            output_directory=output_directory,
            contract=contract,
            required_profile=profile,
        )
        output_root = pathlib.Path(output_directory).resolve()
        published = pathlib.Path(published).resolve(strict=True)
        if published.parent != output_root:
            raise RuntimeError("Generated video was not published in the ComfyUI output directory.")
        video = InputImpl.VideoFromFile(str(published))
        preview = ui.PreviewVideo([ui.SavedResult(published.name, "", io.FolderType.output)])
        return io.NodeOutput(video, ui=preview)


class DGXRemoteMiniMaxH3HMNSFWControls(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="DGXRemoteMiniMaxH3HMNSFWControls",
            display_name="H3-2 HMNSFW · 时长与帧率", category="DGX/Remote H3",
            description="第二套参考图视频配置。时长4–15秒；当前H3原生帧率固定24。分辨率1344×768，采样20步。长片段更慢、占用更多内存；本机已实测4秒和15秒生成播放；15秒本次约73分钟。先用H3-0切换到heretic-ref2va-hmnsfw-v1。",
            inputs=[io.Image.Input("reference_image", display_name="参考图片"),
                io.Audio.Input("reference_audio", display_name="参考音频"),
                io.String.Input("prompt", display_name="提示词｜描述画面与动作", multiline=True, dynamic_prompts=False, default=""),
                io.Int.Input("seed", display_name="Seed｜相同参数便于对比", default=42, min=0, max=MAX_SEED),
                io.Int.Input("duration", display_name="时长（秒）｜越长越慢", default=4, min=4, max=15, step=1),
                io.Int.Input("fps", display_name="帧率 FPS｜每秒24张画面，原生固定", default=24, min=24, max=24),
                io.String.Input("parameter_help", display_name="参数说明（不参与生成）", default="时长：目标片段长度，支持4–15秒。模型会向上对齐帧数，成片可能多出不到1秒。\nFPS：每秒画面数量；当前H3原生只支持24，不是显示器刷新率。\nSeed：随机种子；相同参考图、提示词、参数和种子便于对比结果，不保证完全一致。\n分辨率：固定1344×768。采样：固定20步。\n参考音频：仍使用4秒音频作为声音参考，不决定输出长度。\n本机已实测4秒与15秒生成播放；15秒本次约73分钟。生成无总时长硬截止，可在计算边界取消。", multiline=True, dynamic_prompts=False)],
            outputs=[io.Video.Output("video", display_name="生成的视频")], is_output_node=True)

    @classmethod
    def validate_inputs(cls, reference_image, reference_audio, prompt, seed=42, duration=4, fps=24, parameter_help=""):
        from .policy import validate_ref2va_duration_inputs
        try: validate_ref2va_duration_inputs(prompt, seed, duration, fps)
        except InputError as exc: return str(exc)
        return True

    @classmethod
    def fingerprint_inputs(cls, **kwargs): return float("nan")

    @classmethod
    def execute(cls, reference_image, reference_audio, prompt, seed=42, duration=4, fps=24, parameter_help=""):
        from .policy import validate_ref2va_duration_inputs
        request = validate_ref2va_duration_inputs(prompt, seed, duration, fps)
        from dataclasses import replace
        contract = replace(load_runtime_contract(expected_profile=EXPECTED_PROFILE), total_timeout_seconds=None)
        from functools import partial
        from comfy.model_management import throw_exception_if_processing_interrupted
        from .client import H3GatewayClient
        output_directory = folder_paths.get_output_directory()
        cleanup_stale_staging(output_directory)
        published = pathlib.Path(execute_ref2va_request(request, reference_image, reference_audio,
            output_directory=output_directory, contract=contract, required_profile="heretic-ref2va-hmnsfw-v1",
            client_factory=partial(H3GatewayClient, interrupt_check=throw_exception_if_processing_interrupted))).resolve(strict=True)
        if published.parent != pathlib.Path(output_directory).resolve():
            raise RuntimeError("Generated video was not published in the ComfyUI output directory.")
        return io.NodeOutput(InputImpl.VideoFromFile(str(published)), ui=ui.PreviewVideo([ui.SavedResult(published.name,"",io.FolderType.output)]))


class DGXRemoteH3Extension(ComfyExtension):
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [DGXImageModelBatch, DGXZImageBatch, DGXDramaShotDraft, DGXRemoteMiniMaxH3HMNSFWControls, DGXQwenImageEditBatch, DGXQwenImageEditHuihui, DGXQwenImageEdit, DGXH3Video, DGXH3Ring4Ref2VA, DGXH3Ring4T2VA, DGXH3Ring4Status, DGXCreativeTool, DGXRemoteMiniMaxH3ProfileManager, DGXRemoteMiniMaxH3T2VA, DGXRemoteMiniMaxH3FL2VA, DGXRemoteMiniMaxH3Ref2VA]
