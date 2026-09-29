"""ComfyUI same-origin bridge to the single persistent creative MCP authority."""
import asyncio
from pathlib import PurePosixPath
import re
from urllib.parse import quote, urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, web
from server import PromptServer

from . import creative_client
from .creative_client import forward


MAX_IMAGE_BYTES = 8 * 1024 * 1024


@PromptServer.instance.routes.get("/dgx-creative/artifacts/{job_id}/{filename}")
async def creative_image(request):
    """Read one attributed image from the fixed Gateway; never proxy arbitrary URLs."""
    job_id, filename = request.match_info['job_id'], request.match_info['filename']
    if (not re.fullmatch(r'[0-9a-f]{32}', job_id) or not filename or filename in {'.', '..'}
            or len(filename.encode('utf-8')) > 255 or any(c in filename for c in '/\\')
            or any(ord(c) < 32 or ord(c) == 127 for c in filename)):
        return web.json_response({'error': 'invalid_artifact_identity'}, status=400)
    gateway = urlsplit(creative_client.GATEWAY)
    if gateway.scheme not in {'http', 'https'} or not gateway.netloc or gateway.username or gateway.password:
        return web.json_response({'error': 'gateway_unavailable'}, status=503)
    origin = f'{gateway.scheme}://{gateway.netloc}'
    target = origin + '/api/v1/artifacts/' + job_id + '/' + quote(filename, safe='')
    try:
        response = await asyncio.to_thread(forward, {'jsonrpc': '2.0', 'id': 'image-read',
            'method': 'tools/call', 'params': {'name': 'comfy_job', 'arguments': {'job_id': job_id}}})
        result = response.get('result', {})
        job = result.get('structuredContent', {})
        if result.get('isError') or job.get('job_id') != job_id:
            return web.json_response({'error': 'artifact_not_found'}, status=404)
        belongs = any(isinstance(a, dict) and a.get('kind') == 'images' and a.get('url') == target
            and isinstance(a.get('path'), str) and PurePosixPath(a['path']).name == filename
            and PurePosixPath(a['path']).parent.name == job_id for a in job.get('artifacts', []))
        if not belongs:
            return web.json_response({'error': 'artifact_not_found'}, status=404)
        async with ClientSession(timeout=ClientTimeout(total=30), trust_env=False) as session:
            async with session.get(target, allow_redirects=False) as upstream:
                if upstream.status == 404:
                    return web.json_response({'error': 'artifact_not_found'}, status=404)
                if upstream.status != 200:
                    return web.json_response({'error': 'gateway_image_unavailable'}, status=503)
                if upstream.content_length is not None and upstream.content_length > MAX_IMAGE_BYTES:
                    return web.json_response({'error': 'image_too_large'}, status=413)
                data = bytearray()
                async for chunk in upstream.content.iter_chunked(64 * 1024):
                    data.extend(chunk)
                    if len(data) > MAX_IMAGE_BYTES:
                        return web.json_response({'error': 'image_too_large'}, status=413)
        body = bytes(data)
        if body.startswith(b'\x89PNG\r\n\x1a\n'):
            content_type = 'image/png'
        elif body.startswith(b'\xff\xd8\xff'):
            content_type = 'image/jpeg'
        elif len(body) >= 12 and body[:4] == b'RIFF' and body[8:12] == b'WEBP':
            content_type = 'image/webp'
        else:
            return web.json_response({'error': 'invalid_image'}, status=502)
        return web.Response(body=body, content_type=content_type,
            headers={'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store'})
    except (ClientError, asyncio.TimeoutError, OSError, ValueError, TypeError, AttributeError):
        return web.json_response({'error': 'gateway_image_unavailable'}, status=503)


@PromptServer.instance.routes.post("/dgx-creative/mcp")
async def creative_mcp(request):
    if request.headers.get("X-DGX-Creative-Action") != "explicit-user-action":
        return web.json_response({"error": "action_header_required"}, status=403)
    if request.content_length is None or not 0 < request.content_length <= 16 * 1024 * 1024:
        return web.json_response({"error": "invalid_request_size"}, status=413)
    try:
        payload = await request.json()
        if not isinstance(payload, dict) or payload.get("method") not in {"tools/list", "tools/call"}:
            return web.json_response({"error": "invalid_method"}, status=400)
        return web.json_response(await asyncio.to_thread(forward, payload))
    except (ValueError, TypeError):
        return web.json_response({"error": "invalid_json"}, status=400)
    except Exception:
        # Never retry a potentially accepted generation request automatically.
        return web.json_response({"error": "gateway_unavailable_query_original_job_or_reuse_original_key"}, status=503)
