"""Same-origin, backend-only routes for the H3 profile manager widget."""

from __future__ import annotations

from aiohttp import web
from server import PromptServer

from .profile_control import PUBLIC_PROFILES, ProfileControlError, profile_action


def sanitized(value: dict) -> dict:
    allowed = {"terminal", "state", "profile", "healthy", "profile_identity"}
    return {key: value[key] for key in allowed if key in value}


@PromptServer.instance.routes.get("/dgx-h3/profile/status")
async def get_profile_status(_request):
    try:
        return web.json_response(sanitized(profile_action("status", PUBLIC_PROFILES[0])))
    except ProfileControlError:
        return web.json_response({"error": "controller_unavailable"}, status=503)


@PromptServer.instance.routes.post("/dgx-h3/profile/action")
async def post_profile_action(request):
    if request.headers.get("X-DGX-H3-Action") != "explicit-user-action":
        return web.json_response({"error": "action_header_required"}, status=403)
    if request.content_length is None or not 0 < request.content_length <= 1024:
        return web.json_response({"error": "invalid_request"}, status=422)
    try:
        value = await request.json()
        if not isinstance(value, dict) or set(value) != {"action", "profile"}:
            raise ValueError
        action, profile = value["action"], value["profile"]
        if action not in {"switch", "stop"} or profile not in PUBLIC_PROFILES:
            raise ValueError
        return web.json_response(sanitized(profile_action(action, profile)))
    except ValueError:
        return web.json_response({"error": "invalid_request"}, status=422)
    except ProfileControlError as exc:
        if "BUSY" in str(exc):
            return web.json_response({"error": "busy"}, status=409)
        return web.json_response({"error": "operation_failed"}, status=503)
