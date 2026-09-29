"""Graph entry point for exactly the same tools as the creative panel and MCP."""
import json
from comfy_api.latest import io
from .creative_client import forward


class DGXCreativeTool(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="DGXCreativeTool", display_name="DGX 创作工具（共用 MCP 任务）",
            category="DGX/Creative",
            description="Submit or query a persistent Gateway task. Generation returns job_id immediately; inspect and cancel it in the DGX 创作 panel. Comfy's queue stop does not cancel a submitted Gateway job.",
            inputs=[io.String.Input("tool", default="dgx_status", dynamic_prompts=False),
                    io.String.Input("arguments_json", default="{}", multiline=True, dynamic_prompts=False)],
            outputs=[io.String.Output("result_json")], is_output_node=True)

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        return float("nan")

    @classmethod
    def execute(cls, tool, arguments_json):
        args = json.loads(arguments_json)
        if not isinstance(args, dict):
            raise ValueError("arguments_json must be a JSON object")
        result = forward({"jsonrpc": "2.0", "id": "comfy-node", "method": "tools/call", "params": {"name": tool, "arguments": args}})
        if result.get("error") or result.get("result", {}).get("isError"):
            raise ValueError(json.dumps(result, ensure_ascii=False))
        text = json.dumps(result["result"]["structuredContent"], ensure_ascii=False, indent=2)
        return io.NodeOutput(text, ui={"text": [text]})
