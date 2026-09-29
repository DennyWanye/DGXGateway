"""P3 video transport through the sole Gateway authority, with bounded outputs."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import re
import urllib.request

from .. import mcp
from ..audio_assets import MAX_AUDIO_BYTES, _validate_wav
from ..image_assets import _read_regular
from .gateway_adapter import GatewayReferenceAdapter, GatewayRejected, GatewayBusy, MAX_IMAGE_BYTES, RPC_ORIGIN
from .references import decode_image
from .contracts import DramaError
from ..video_contracts import STABLE_PROFILE, gateway_generation
from ..video_reference_assets import VideoReferenceAssets, MAX_VIDEO_REFERENCE_BYTES


MAX_VIDEO_BYTES = 256 * 1024 * 1024


class GatewayVideoAdapter(GatewayReferenceAdapter):
    def _call(self, name, arguments):
        request = {"jsonrpc": "2.0", "id": "drama-video", "method": "tools/call",
                   "params": {"name": name, "arguments": arguments}}
        if self.app is not None:
            response = mcp.dispatch(self.app, request)
        else:
            req = urllib.request.Request(RPC_ORIGIN + "/mcp", data=json.dumps(request, allow_nan=False).encode(),
                                         headers={"Content-Type": "application/json"})
            with self.http.open(req, timeout=30) as result:
                payload = result.read(2 * 1024 * 1024 + 1)
            if len(payload) > 2 * 1024 * 1024:
                raise RuntimeError("Gateway response exceeds the size limit")
            response = json.loads(payload)
        if response.get("error", {}).get("code") == -32602:
            raise GatewayRejected("Gateway rejected video arguments")
        result = response.get("result", {})
        structured = result.get("structuredContent")
        if result.get("isError") and isinstance(structured, dict):
            code = structured.get("error", {}).get("code")
            if code == "DGX_BUSY":
                raise GatewayBusy("DGX busy; existing resources were not preempted")
            if code == "VIDEO_REJECTED":
                raise GatewayRejected("Gateway rejected the video request")
        if response.get("error") or result.get("isError") or not isinstance(structured, dict):
            raise RuntimeError("Gateway did not return a confirmed video result")
        return structured

    @staticmethod
    def _inputs(items, expected, limit):
        if not isinstance(items, list) or not 0 <= len(items) <= 32:
            raise GatewayRejected("Video input registrations must contain 1–32 items")
        result = []
        for item in items:
            if not isinstance(item, dict) or set(item) != {"assetId", "filename", "data"}:
                raise GatewayRejected("Invalid video input registration")
            data = item["data"]
            if (not isinstance(data, bytes) or not 0 < len(data) <= limit
                    or not isinstance(item["assetId"], str) or hashlib.sha256(data).hexdigest() != item["assetId"]):
                raise GatewayRejected("Video input hash or size is invalid")
            if (not isinstance(item["filename"], str) or not 1 <= len(item["filename"]) <= 180
                    or any(c in item["filename"] for c in "/\\")
                    or any(ord(c) < 32 for c in item["filename"])):
                raise GatewayRejected("Video registration requires a plain filename")
            result.append(item)
        if {item["assetId"] for item in result} != set(expected):
            raise GatewayRejected("Registered inputs do not match the shot references")
        return result

    def submit(self, shots, dispatch_key, images, audios, before_submit, *, profile=STABLE_PROFILE, videos=None):
        videos = videos or []
        parameters = {"shots": shots, "idempotency_key": dispatch_key,
                      "profile": profile, "non_preemptive": True}
        try:
            for shot in shots:
                gateway_generation(profile, shot)
            schema = next(tool["inputSchema"] for tool in mcp.TOOLS if tool["name"] == "video_batch_submit")
            mcp._validate(schema, parameters)
            expected_images = [asset_id for shot in shots for asset_id in
                               [*(shot.get("reference_image_ids") or []), shot.get("image_asset_id", ""), shot.get("action_image_asset_id", "")] if asset_id]
            images = self._inputs(images, expected_images, MAX_IMAGE_BYTES)
            audios = self._inputs(audios, [shot["audio_asset_id"] for shot in shots if shot["audio_asset_id"]], MAX_AUDIO_BYTES)
            videos = self._inputs(videos, [shot.get("video_asset_id", "") for shot in shots if shot.get("video_asset_id")], MAX_VIDEO_REFERENCE_BYTES)
            for image in images:
                if decode_image(image["data"])["extension"] not in {"png", "jpg"}:
                    raise ValueError("H3 requires a PNG or JPEG reference")
            for audio in audios:
                _validate_wav(audio["data"])
        except (ValueError, TypeError, DramaError) as exc:
            raise GatewayRejected("Invalid versioned H3 video inputs") from exc
        try:
            for tool, field, items in (("comfy_register_image", "image_base64", images),
                                       ("video_register_audio", "audio_base64", audios)):
                for item in items:
                    registered = self._call(tool, {"filename": item["filename"], field: base64.b64encode(item["data"]).decode("ascii")})
                    if registered.get("asset_id") != item["assetId"]:
                        raise GatewayRejected("Gateway did not preserve the reference hash")
            # The business service and Gateway share the same Mac state root.
            # Register large MP4 references directly into the Gateway's
            # content-addressed store; generation still goes through MCP and
            # the sole lifecycle authority.
            registry = VideoReferenceAssets(self.state_dir / "creative-video-reference-assets")
            for item in videos:
                registered = registry.register_bytes(item["filename"], item["data"])
                if registered.get("asset_id") != item["assetId"]:
                    raise GatewayRejected("Gateway did not preserve the video reference hash")
        except Exception as exc:
            # A registration reply can be lost, but generation has not been
            # called at all. Do not strand this as an unknown VIDEO submission.
            raise GatewayRejected(
                f"Input registration failed; no video request was submitted ({exc})"
            ) from exc
        before_submit()
        return self._call("video_batch_submit", parameters)

    def get(self, job_id):
        return self._call("video_job", {"job_id": job_id})

    def find(self, key):
        return self._call("video_job_find", {"idempotency_key": key})["job"]

    def cancel(self, job_id):
        return self._call("video_cancel", {"job_id": job_id})

    def reconcile(self, job_id):
        return self._call("video_reconcile", {"job_id": job_id})

    def outputs(self, record):
        """Yield one bounded local file at a time, including partial-batch results."""
        jid, shots = record.get("job_id", ""), record.get("shots", [])
        if not re.fullmatch(r"[a-f0-9]{32}", jid) or not isinstance(shots, list) or not 1 <= len(shots) <= 32:
            raise ValueError("Invalid Gateway video batch identity")
        root = self.state_dir / "creative-artifacts"
        directory = root / jid
        if root.is_symlink() or directory.is_symlink():
            raise ValueError("Video output directory cannot be a symlink")
        for ordinal, shot in enumerate(shots):
            index, name = shot.get("index"), shot.get("name", "")
            if (type(index) is not int or index != ordinal or not isinstance(name, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", name)):
                raise ValueError("Invalid ordered video shot identity")
            # A running synchronous request has no promised output yet. Read
            # only completed or ambiguous requests; an ambiguous request may
            # have published its file before saving the artifact pointer.
            phase = shot.get("submission_phase", "UNKNOWN")
            ambiguous_started = phase == "REQUEST_STARTED" and record.get("state") in {
                "FAILED", "INTERRUPTED", "SUBMISSION_UNKNOWN", "CANCELED"}
            if not (shot.get("artifact") or shot.get("state") == "SUCCEEDED"
                    or phase in {"RECEIVED", "UNKNOWN"} or ambiguous_started):
                continue
            filename = f"{index:03d}-{name}.mp4"
            identity = {"index": index, "sourceKey": f"{jid}/{filename}", "filename": filename}
            try:
                data = _read_regular(directory / filename, MAX_VIDEO_BYTES)
                if not data:
                    raise ValueError("Empty video artifact")
            except (OSError, ValueError):
                yield {**identity, "error": "artifact_unavailable"}
            else:
                yield {**identity, "data": data}
