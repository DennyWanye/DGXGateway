"""Narrow image-tool adapter to the existing Gateway, without a new resource lock."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import re
import urllib.parse
import urllib.request

from .. import mcp
from ..image_assets import _read_regular


IMAGE_TOOLS = frozenset({"generate_image", "edit_image", "compose_image_layers"})
MAX_IMAGE_BYTES = 8 * 1024 * 1024
RPC_ORIGIN = "http://192.0.2.37:18082"
ARTIFACT_ORIGINS = frozenset({RPC_ORIGIN, "http://127.0.0.1:18082"})


class GatewayRejected(Exception):
    """A definite pre-dispatch rejection; other errors remain outcome-unknown."""


class GatewayBusy(GatewayRejected):
    """Gateway explicitly proved rejection before allocating a model task."""
    code = "DGX_BUSY"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Gateway redirects are not permitted")


def validate_parameters(tool: str, parameters: dict) -> dict:
    if tool not in IMAGE_TOOLS or not isinstance(parameters, dict) or "idempotency_key" in parameters:
        raise ValueError("Unsupported image tool or parameters")
    normalized = json.loads(json.dumps(parameters, allow_nan=False))
    schema = next(item["inputSchema"] for item in mcp.TOOLS if item["name"] == tool)
    mcp._validate(schema, {**normalized, "idempotency_key": "drama-validation"})
    if tool in {"generate_image", "edit_image"}:
        from ..image_batches import candidate_indices
        candidate_indices(normalized.get("batch_count", 1), normalized.get("parallelism", 2), normalized.get("candidate_indices"))
        seed = normalized.get("seed", 42)
        if type(seed) is not int or not 0 <= seed < 2**64:
            raise ValueError("seed must be an unsigned 64-bit integer")
        from ..image_workflows import assert_image_policy, QUALITY_GENERATE_MODEL, _new_image_inputs
        assert_image_policy(tool, normalized)
        if tool == 'generate_image' and normalized.get('model', QUALITY_GENERATE_MODEL) != 'z-image-turbo':
            _new_image_inputs(normalized['prompt'], normalized.get('width', 1024), normalized.get('height', 1024))
    if tool == 'edit_image':
        from ..image_workflows import validate_edit_parameters
        validate_edit_parameters(normalized)
    return normalized


def input_asset_ids(tool: str, parameters: dict) -> list[str]:
    if tool == "edit_image":
        return list(parameters["asset_ids"])
    if tool == "generate_image" and parameters.get("asset_ids"):
        return list(parameters["asset_ids"])
    if tool == "compose_image_layers":
        return [parameters["background_asset_id"], *(layer["asset_id"] for layer in parameters["layers"])]
    return []


class GatewayReferenceAdapter:
    """Use ``app=`` in production, or known local ``state_dir=`` for preview RPC.

    Neither origin nor state directory is accepted from a browser request.
    Recovery scans only the two known job directories and never submits a job.
    """

    def __init__(self, *, app=None, state_dir: Path | None = None):
        if app is None and state_dir is None:
            raise ValueError("A configured Gateway state directory is required")
        self.app = app
        self.state_dir = Path(app.config.state_dir if app is not None else state_dir).resolve()
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def _call(self, name: str, arguments: dict) -> dict:
        request = {"jsonrpc": "2.0", "id": "drama-image", "method": "tools/call",
                   "params": {"name": name, "arguments": arguments}}
        if self.app is not None:
            response = mcp.dispatch(self.app, request)
        else:
            payload = json.dumps(request, allow_nan=False).encode()
            req = urllib.request.Request(RPC_ORIGIN + "/mcp", data=payload,
                                         headers={"Content-Type": "application/json"})
            with self.http.open(req, timeout=30) as result:
                raw = result.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("Gateway response exceeds limit")
            response = json.loads(raw)
        if response.get("error", {}).get("code") == -32602:
            raise GatewayRejected("Gateway rejected image arguments")
        result = response.get("result", {})
        if result.get("isError") and isinstance(result.get("structuredContent"), dict):
            if result["structuredContent"].get("error", {}).get("code") == "DGX_BUSY":
                raise GatewayBusy("DGX资源繁忙，任务保留在等待队列")
        if response.get("error") or result.get("isError") or not isinstance(result.get("structuredContent"), dict):
            # The upstream wrapper can mask exceptions after job persistence.
            # Do not infer that no submission happened from its generic error.
            raise RuntimeError("Gateway did not return a confirmed image result")
        return result["structuredContent"]

    def recover(self, job_id: str) -> dict:
        return self._call("comfy_reconcile", {"job_id": job_id})

    def submit(self, tool: str, parameters: dict, dispatch_key: str, images: list[dict]) -> dict:
        return self.submit_with_gate(tool, parameters, dispatch_key, images, lambda: None)

    def submit_with_gate(self, tool: str, parameters: dict, dispatch_key: str,
                         images: list[dict], before_submit) -> dict:
        """Register inputs, then run the business gate immediately before generation."""
        parameters = validate_parameters(tool, parameters)
        expected = input_asset_ids(tool, parameters)
        if [image["assetId"] for image in images] != expected:
            raise GatewayRejected("Ordered image inputs do not match the request")
        for image in images:
            if hashlib.sha256(image["data"]).hexdigest() != image["assetId"]:
                raise GatewayRejected("Source image hash changed")
            registered = self._call("comfy_register_image", {
                "filename": image["filename"], "image_base64": base64.b64encode(image["data"]).decode("ascii")})
            if registered.get("asset_id") != image["assetId"]:
                raise GatewayRejected("Gateway did not preserve the registered image identity")
        before_submit()
        return self._call(tool, {**parameters, "idempotency_key": dispatch_key})

    def get(self, tool: str, job_id: str) -> dict:
        return self._call("media_job" if tool == "compose_image_layers" else "comfy_job", {"job_id": job_id})

    def find(self, tool: str, dispatch_key: str) -> dict | None:
        root = self.state_dir / ("media-jobs" if tool == "compose_image_layers" else "creative-jobs")
        if not root.exists():
            return None
        if root.is_symlink():
            raise ValueError("Gateway job directory cannot be a symlink")
        matches = []
        for path in root.glob("*.json"):
            if not re.fullmatch(r"[a-f0-9]{32}\.json", path.name):
                continue
            try:
                row = json.loads(_read_regular(path, 2 * 1024 * 1024))
            except (OSError, ValueError):
                continue
            if row.get("idempotency_key") == dispatch_key:
                matches.append(row)
        if len(matches) > 1:
            raise ValueError("More than one Gateway task matches the saved identity")
        return self.get(tool, matches[0]["job_id"]) if matches else None

    def cancel(self, tool: str, job_id: str) -> dict:
        if tool == "compose_image_layers":
            raise GatewayRejected("Existing local composition jobs do not support cancellation")
        return self._call("comfy_cancel", {"job_id": job_id})

    def outputs(self, tool: str, record: dict) -> list[dict]:
        jid = record.get("job_id", "")
        if not re.fullmatch(r"[a-f0-9]{32}", jid):
            raise ValueError("Invalid Gateway job identity")
        if tool == "compose_image_layers":
            asset = record.get("output_asset")
            if not isinstance(asset, dict) or not re.fullmatch(r"[a-f0-9]{64}", asset.get("asset_id", "")):
                return []
            aid = asset["asset_id"]
            directory = self.state_dir / "creative-assets" / aid
            if directory.is_symlink() or directory.parent.is_symlink():
                raise ValueError("Gateway asset directory cannot be a symlink")
            metadata = json.loads(_read_regular(directory / "metadata.json", 8192))
            filename = metadata.get("filename", "")
            if filename not in {f"dgx-asset-{aid}.{ext}" for ext in ("png", "jpg", "webp")}:
                raise ValueError("Invalid Gateway image filename")
            data = _read_regular(directory / filename, MAX_IMAGE_BYTES)
            if hashlib.sha256(data).hexdigest() != aid:
                raise ValueError("Gateway image hash mismatch")
            return [{"sourceKey": aid, "filename": filename, "data": data}]
        artifacts = record.get("artifacts", [])
        if not isinstance(artifacts, list) or len(artifacts) > 8:
            raise ValueError("Image job must contain no more than eight outputs")
        outputs = []
        for artifact in artifacts:
            url = urllib.parse.urlsplit(artifact.get("url", ""))
            origin = f"{url.scheme}://{url.netloc}"
            if self.app is not None:
                allowed = ARTIFACT_ORIGINS | {f"http://{self.app.config.bind}:{self.app.config.port}"}
            else:
                allowed = ARTIFACT_ORIGINS
            decoded = urllib.parse.unquote(url.path)
            prefix = f"/api/v1/artifacts/{jid}/"
            name = decoded.removeprefix(prefix)
            if (origin not in allowed or not decoded.startswith(prefix) or url.query or url.fragment
                    or not name or name in {".", ".."} or "/" in name or "\\" in name
                    or any(ord(char) < 32 for char in name)):
                raise ValueError("Image artifact URL is outside the configured Gateway")
            # Fixed local archive avoids exposing or following artifact['path'].
            directory = self.state_dir / "creative-artifacts" / jid
            if directory.is_symlink() or directory.parent.is_symlink():
                raise ValueError("Gateway artifact directory cannot be a symlink")
            try:
                data = _read_regular(directory / name, MAX_IMAGE_BYTES)
            except (OSError, ValueError):
                # Missing/oversized members do not discard earlier valid images.
                # Keep their stable identity so a read-only refresh can recover.
                outputs.append({"sourceKey": name, "filename": name, "error": "artifact_unavailable"})
            else:
                outputs.append({"sourceKey": name, "filename": name, "data": data})
        return outputs
