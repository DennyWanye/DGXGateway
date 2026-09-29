"""An editable projection of a saved shot. Network writes require the UI button."""
import json

from comfy_api.latest import io
from .drama_client import build_canvas_draft, TEMPLATE_VERSION


class DGXDramaShotDraft(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="DGXDramaShotDraft", display_name="短剧镜头草稿 · 显式回写",
            category="DGX/Drama",
            description="编辑镜头制作字段，通过节点按钮回写新修订。执行节点只输出草稿JSON，不生成图片、不调用模型、不保存修订。",
            inputs=[
                io.String.Input("shotId", default="", dynamic_prompts=False),
                io.Int.Input("expectedRevision", default=1, min=1),
                io.String.Input("baseRevisionId", default="", dynamic_prompts=False),
                io.String.Input("templateVersion", default=TEMPLATE_VERSION, dynamic_prompts=False),
                io.String.Input("title", default="镜头", dynamic_prompts=False),
                io.String.Input("action", default="", multiline=True, dynamic_prompts=False),
                io.String.Input("framing", default="medium", dynamic_prompts=False),
                io.String.Input("camera", default="locked", dynamic_prompts=False),
                io.String.Input("prompt", default="", multiline=True, dynamic_prompts=False),
                io.Int.Input("targetEditFrames", default=96, min=1, max=100000),
                io.String.Input("snapshot_json", default="{}", multiline=True, dynamic_prompts=False),
            ],
            outputs=[io.String.Output("draft_json")], is_output_node=True,
        )

    @classmethod
    def execute(cls, **kwargs):
        text = json.dumps(build_canvas_draft(**kwargs), ensure_ascii=False, indent=2)
        return io.NodeOutput(text, ui={"text": [text]})
