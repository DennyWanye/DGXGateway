"""Same-origin streaming bridge. All destinations come from deployment config."""
from __future__ import annotations

import asyncio
import ipaddress
import os
import re
from urllib.parse import urlsplit, urlencode

import aiohttp
from aiohttp import web
from server import PromptServer

from .creative_client import GATEWAY


MAX_BODY = 512 * 1024 * 1024


def drama_upstream(gateway, override):
    if override is None:
        base = urlsplit(gateway)
        return f"{base.scheme}://{base.netloc}/api/v1/drama"
    # Deployment-only override, frozen at import. Requests cannot choose a host.
    # Docker's host alias and a LAN-bound Gateway need not share a hostname.
    try:
        value = urlsplit(override)
        host = value.hostname
        trusted = host in {"localhost", "host.docker.internal"}
        if not trusted:
            address = ipaddress.ip_address(host or "")
            trusted = address.is_loopback or any(address in network for network in (
                ipaddress.ip_network("192.0.2.1/8"),
                ipaddress.ip_network("192.0.2.24/12"),
                ipaddress.ip_network("192.0.2.26/16"),
            ))
        if (not trusted or value.scheme not in {"http", "https"}
                or value.port is None or not 1 <= value.port <= 65535
                or value.username is not None or value.password is not None
                or value.path != "/api/v1/drama" or "?" in override or "#" in override
                or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in override) or "\\" in override):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("DGX_DRAMA_GATEWAY_URL must be a fixed trusted LAN/loopback drama endpoint") from None
    return override


UPSTREAM = drama_upstream(GATEWAY, os.environ.get("DGX_DRAMA_GATEWAY_URL"))


async def proxy(request):
    tail = request.match_info["tail"]
    if not re.fullmatch(r"[a-zA-Z0-9/-]{1,400}", tail) or "//" in tail:
        return web.json_response({"error": {"code": "invalid_route", "message": "无效接口路径"}}, status=404)
    route_tail = re.sub(r"^(?:restored/[a-f0-9]{32}/){1,4}", "", tail)
    query = ""
    if request.query:
        is_queue = bool(re.fullmatch(r"projects/[a-f0-9]{32}/image-review-queue", route_tail))
        key = "requestKey" if re.fullmatch(r"episodes/[a-f0-9]{32}/text-jobs", route_tail) else "before" if re.fullmatch(r"projects/[a-f0-9]{32}/backups", route_tail) else "projectId" if route_tail == "tasks" else None
        if is_queue:
            if (set(request.query) - {"scope", "episodeId"}
                    or any(len(request.query.getall(name)) != 1 for name in request.query)
                    or request.query.get("scope", "episode") not in {"episode", "project"}
                    or ("episodeId" in request.query and not re.fullmatch(r"[a-f0-9]{32}", request.query["episodeId"]))):
                return web.json_response({"error": {"code": "invalid_query", "message": "查询参数不受支持"}}, status=422)
            query = "?" + urlencode({name: request.query[name] for name in ("scope", "episodeId") if name in request.query})
        elif (not key or set(request.query) != {key} or len(request.query.getall(key)) != 1
                or not 1 <= len(request.query[key]) <= 128
                or (key in {"before", "projectId"} and not re.fullmatch(r"[a-f0-9]{32}", request.query[key]))):
            return web.json_response({"error": {"code": "invalid_query", "message": "查询参数不受支持"}}, status=422)
        elif not is_queue:
            query = "?" + urlencode({key: request.query[key]})
    headers = {}
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if (request.headers.get("X-DGX-Drama-Action") != "explicit-user-action"
                or (origin and origin != f"{request.scheme}://{request.host}")):
            return web.json_response({"error": {"code": "forbidden", "message": "请求来源不受信任"}}, status=403)
        length = request.content_length
        limit = 256 * 1024 * 1024 if tail.endswith("/audio-assets") else MAX_BODY if tail.endswith("/assets") else 8 * 1024 * 1024 if tail.endswith("/images") else 256 * 1024 if tail.endswith("/reference-audio") else 1024 * 1024
        if tail.endswith("restore-uploads"):
            limit = 2 * 1024 * 1024 * 1024
        if re.fullmatch(r"episodes/[a-f0-9]{32}/(?:source|entities|beats|script|storyboard)-(?:revisions|drafts)", route_tail):
            limit = 16 * 1024 * 1024
        if length is None or not 0 < length <= limit:
            return web.json_response({"error": {"code": "upload_size", "message": "请求大小不受支持"}}, status=413)
        for name in ("Content-Type", "Content-Length", "X-Request-Key", "X-Filename", "X-Content-SHA256"):
            if name in request.headers:
                headers[name] = request.headers[name]
    for name in ("Range",):
        if name in request.headers:
            headers[name] = request.headers[name]
    response = None
    try:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=90)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False, auto_decompress=False) as client:
            async with client.request(request.method, UPSTREAM + "/" + tail + query, headers=headers,
                                      data=request.content.iter_chunked(256 * 1024) if request.method == "POST" else None,
                                      allow_redirects=False) as upstream:
                if 300 <= upstream.status < 400:
                    raise ValueError("upstream redirect refused")
                copied = {key: value for key, value in upstream.headers.items() if key.lower() in {
                    "content-type", "content-length", "content-range", "accept-ranges", "content-disposition", "cache-control"}}
                copied["X-Content-Type-Options"] = "nosniff"
                response = web.StreamResponse(status=upstream.status, headers=copied)
                await response.prepare(request)
                if request.method != "HEAD":
                    async for chunk in upstream.content.iter_chunked(256 * 1024):
                        await response.write(chunk)
                await response.write_eof()
                return response
    except (aiohttp.ClientError, OSError, ValueError, asyncio.TimeoutError):
        if response is not None and response.prepared:
            response.force_close()
            return response
        return web.json_response({"error": {"code": "gateway_unavailable", "message": "Gateway暂不可用，请先查询原任务；不会自动重复提交"}}, status=503)


@PromptServer.instance.routes.get("/dgx-drama/{tail:.*}")
async def drama_get(request):
    return await proxy(request)


@PromptServer.instance.routes.post("/dgx-drama/{tail:.*}")
async def drama_post(request):
    return await proxy(request)
