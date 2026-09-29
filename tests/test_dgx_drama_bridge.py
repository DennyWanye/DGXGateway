"""Actual ComfyUI route registration and HTTP streaming into an isolated Gateway.

Requires aiohttp (available in the qualified ComfyUI image). No running ComfyUI,
Gateway, model, network service, or FFmpeg installation is needed. Media decoding
has its own real-media suite; this suite stubs only that CPU validation boundary.
"""
import asyncio
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
import uuid
import wave

try:
    import aiohttp
    from aiohttp import web
except ImportError:
    aiohttp = web = None

from DGXGateway.dgx_gateway.drama.service import DramaService
from DGXGateway.dgx_gateway.server import GatewayHTTPServer


@unittest.skipUnless(aiohttp is not None, "aiohttp required; run in the ComfyUI runtime")
class DramaBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.service = DramaService(root, root)
        config = SimpleNamespace(lan_no_auth=True, allowed_origins=("http://localhost",))
        application = SimpleNamespace(drama=self.service, config=config, authorized=lambda cookie: True)
        self.gateway = GatewayHTTPServer(("127.0.0.1", 0), application)
        self.gateway_thread = threading.Thread(target=self.gateway.serve_forever, daemon=True)
        self.gateway_thread.start()
        self.runners = []

        # Execute the production registration decorators with only ComfyUI's
        # host object replaced. The handlers and aiohttp transport remain real.
        routes = web.RouteTableDef()
        package_name = "drama_bridge_test_" + uuid.uuid4().hex
        package = ModuleType(package_name)
        node_root = Path(__file__).resolve().parents[1] / "ComfyUI/custom_nodes/dgx_remote_h3"
        package.__path__ = [str(node_root)]
        transport = ModuleType(package_name + ".creative_client")
        transport.GATEWAY = f"http://127.0.0.1:{self.gateway.server_port}/mcp"
        server_module = ModuleType("server")
        server_module.PromptServer = SimpleNamespace(instance=SimpleNamespace(routes=routes))
        name = package_name + ".drama_routes"
        spec = importlib.util.spec_from_file_location(name, node_root / "drama_routes.py")
        self.bridge = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {
            package_name: package, transport.__name__: transport,
            "server": server_module, name: self.bridge,
        }):
            # Never inherit a container deployment override pointing at user data.
            with patch.dict(os.environ):
                os.environ.pop("DGX_DRAMA_GATEWAY_URL", None)
                spec.loader.exec_module(self.bridge)
        app = web.Application()
        app.add_routes(routes)
        self.base = await self.start_app(app)
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), trust_env=False)
        self.action = {"X-DGX-Drama-Action": "explicit-user-action", "Origin": self.base}

    async def asyncTearDown(self):
        await self.client.close()
        for runner in reversed(self.runners):
            await runner.cleanup()
        await asyncio.to_thread(self.gateway.shutdown)
        self.gateway.server_close()
        self.gateway_thread.join(timeout=5)
        self.service.close()
        self.temporary.cleanup()

    async def start_app(self, app):
        runner = web.AppRunner(app)
        await runner.setup()
        self.runners.append(runner)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def request(self, method, tail, **kwargs):
        async with self.client.request(method, self.base + "/dgx-drama/" + tail, **kwargs) as response:
            return response.status, response.headers, await response.read()

    async def create_project(self, key="project", title="桥接测试"):
        status, _, payload = await self.request("POST", "projects", headers=self.action,
                                              json={"title": title, "requestKey": key})
        self.assertEqual(status, 201, payload)
        return json.loads(payload)["project"]["id"]

    async def upload(self, pid):
        # Crosses several 256 KiB proxy chunks. Real bytes are persisted and
        # hashed; only codec decoding is outside this transport test's scope.
        data = bytes(range(256)) * 8193
        metadata = {"kind": "video", "frames": 96, "fps": 24.0, "duration": 4.0,
                    "width": 96, "height": 64, "hasAudio": False,
                    "codec": "h264", "contentType": "video/mp4"}
        headers = {**self.action, "Content-Type": "application/octet-stream",
                   "X-Request-Key": "binary-upload", "X-Filename": "clip.mp4"}
        with patch("DGXGateway.dgx_gateway.drama.media.probe_media", return_value=metadata), \
                patch.object(self.service, "upload", wraps=self.service.upload) as incoming:
            status, _, payload = await self.request("POST", f"projects/{pid}/assets", headers=headers,
                                                  data=io.BytesIO(data))
        self.assertEqual(status, 201, payload)
        self.assertEqual(incoming.call_count, 1)
        self.assertEqual(incoming.call_args.args[2], len(data))
        aid = json.loads(payload)["asset"]["id"]
        self.assertEqual(aid, hashlib.sha256(data).hexdigest())
        self.assertEqual(self.service.media_path(pid, aid).read_bytes(), data)
        return aid, data

    async def test_registered_json_routes_reach_real_gateway_and_preserve_errors(self):
        pid = await self.create_project()
        status, _, payload = await self.request("GET", "projects")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["projects"][0]["id"], pid)
        status, _, payload = await self.request("POST", "projects", headers=self.action,
                                              json={"requestKey": "invalid"})
        self.assertEqual(status, 422)
        self.assertEqual(json.loads(payload)["error"]["code"], "invalid_fields")

    async def test_text_job_query_preserves_exact_key_and_rejects_ambiguous_query(self):
        pid = await self.create_project()
        status, _, payload = await self.request("POST", f"projects/{pid}/episodes", headers=self.action,
                                              json={"title": "文本查询", "requestKey": "episode"})
        self.assertEqual(status, 201)
        eid = json.loads(payload)["episode"]["id"]
        from urllib.parse import urlencode
        key = "原键+&问号?"
        with patch.object(self.service, "find_text_job", return_value={"job": None}) as find:
            status, _, payload = await self.request("GET", f"episodes/{eid}/text-jobs?" + urlencode({"requestKey": key}))
            self.assertEqual(status, 200, payload)
            find.assert_called_once_with(eid, key)
        for query in ("requestKey=a&requestKey=b", "url=http://evil.invalid", "requestKey="):
            status, _, _ = await self.request("GET", f"episodes/{eid}/text-jobs?" + query)
            self.assertEqual(status, 422)

    async def test_image_review_queue_bridge_forwards_only_contract_query(self):
        pid = await self.create_project()
        status, _, payload = await self.request("POST", f"projects/{pid}/episodes", headers=self.action,
                                                json={"title": "审图分集", "requestKey": "review-episode"})
        self.assertEqual(status, 201, payload)
        eid = json.loads(payload)["episode"]["id"]
        status, _, payload = await self.request("GET", f"projects/{pid}/image-review-queue?scope=episode&episodeId={eid}")
        self.assertEqual(status, 200, payload)
        self.assertEqual((json.loads(payload)["scope"], json.loads(payload)["episodeId"]), ("episode", eid))
        for query in ("scope=invalid", "scope=project&scope=episode", "episodeId=invalid", "episodeId=" + eid + "&episodeId=" + eid,
                      "scope=project&unknown=x"):
            status, _, payload = await self.request("GET", f"projects/{pid}/image-review-queue?{query}")
            self.assertEqual(status, 422, payload)

    async def queue_image(self, pid, key):
        # Persist a real intent through HTTP, but do not invoke a model adapter.
        # The production scheduler itself is covered by main_flow's queue tests.
        from tests.test_dgx_drama_references import FakeGateway
        self.service._reference_stop.set()
        await asyncio.to_thread(self.service._reference_poller.join)
        if self.service._reference_adapter is None:
            self.service._reference_adapter = FakeGateway()
        status, _, payload = await self.request("POST", f"projects/{pid}/entities", headers=self.action,
            json={"kind": "character", "name": key, "requestKey": key + "-entity"})
        self.assertEqual(status, 201, payload)
        rid = json.loads(payload)["entity"]["currentRevisionId"]
        with patch.object(self.service._reference_executor, "submit"):
            status, _, payload = await self.request("POST", f"entity-revisions/{rid}/reference-jobs", headers=self.action,
                json={"tool": "generate_image", "parameters": {"prompt": "桥接任务占位，不运行模型"}, "requestKey": key + "-image"})
        self.assertEqual(status, 202, payload)
        self.assertEqual(self.service._reference_adapter.submissions, [])
        return json.loads(payload)["job"]["id"]

    async def wait_operation(self, oid):
        for _ in range(200):
            operation = self.service.backup_operation(oid)["operation"]
            if operation["state"] in {"SUCCEEDED", "FAILED"}:
                self.assertEqual(operation["state"], "SUCCEEDED", operation)
                return operation
            await asyncio.sleep(.025)
        self.fail("isolated backup/restore operation did not finish")

    async def test_task_index_queries_current_all_projects_and_rejects_ambiguous_scope(self):
        first = await self.create_project()
        second = await self.create_project("second", "另一项目")
        one = await self.queue_image(first, "first")
        two = await self.queue_image(second, "second")
        with patch.object(self.service, "task_index", wraps=self.service.task_index) as index:
            status, _, payload = await self.request("GET", f"tasks?projectId={first}")
            self.assertEqual(status, 200, payload)
            self.assertEqual([task["id"] for task in json.loads(payload)["tasks"]], [one])
            index.assert_called_once_with(first)
            status, _, payload = await self.request("GET", "tasks")
            self.assertEqual(status, 200, payload)
            self.assertEqual({task["id"] for task in json.loads(payload)["tasks"]}, {one, two})
            self.assertEqual(index.call_args.args, (None,))
            for query in (f"projectId={first}&projectId={second}", "projectId=", "projectId=not-an-id",
                          f"projectId={first}&url=http://evil.invalid", f"requestKey={first}"):
                with self.subTest(query=query):
                    before = index.call_count
                    status, _, payload = await self.request("GET", "tasks?" + query)
                    self.assertEqual(status, 422, payload)
                    self.assertEqual(json.loads(payload)["error"]["code"], "invalid_query")
                    self.assertEqual(index.call_count, before, "invalid queries must be rejected by the bridge")
        self.assertEqual(self.service._reference_adapter.submissions, [])

    async def test_task_index_restored_workspace_keeps_its_own_scope(self):
        original = await self.create_project()
        one = await self.queue_image(original, "restore-source")
        revision = self.service.project(original)["project"]["revision"]
        backup = self.service.create_backup(original, {"requestKey": "scope-backup", "expectedProjectRevision": revision})["operation"]
        await self.wait_operation(backup["id"])
        restored = self.service.create_restore({"requestKey": "scope-restore", "backupId": backup["id"]})["operation"]
        await self.wait_operation(restored["id"])
        other = await self.create_project("root-only", "只在原工作区")
        two = await self.queue_image(other, "root-only")
        prefix = f"restored/{restored['id']}/tasks"
        status, _, payload = await self.request("GET", prefix)
        self.assertEqual(status, 200, payload)
        tasks = json.loads(payload)["tasks"]
        self.assertEqual([task["id"] for task in tasks], [one])
        self.assertIsNone(tasks[0]["cancelPath"])
        self.assertIsNone(tasks[0]["refreshPath"])
        status, _, payload = await self.request("GET", prefix + f"?projectId={original}")
        self.assertEqual(status, 200, payload)
        self.assertEqual([task["id"] for task in json.loads(payload)["tasks"]], [one])
        status, _, payload = await self.request("GET", prefix + f"?projectId={other}")
        self.assertEqual(status, 404, payload)
        status, _, payload = await self.request("GET", "tasks")
        self.assertEqual(status, 200, payload)
        self.assertEqual({task["id"] for task in json.loads(payload)["tasks"]}, {one, two})
        self.assertEqual(self.service._reference_adapter.submissions, [])

    async def test_default_silence_http_is_idempotent_exact_wav_and_requires_explicit_action(self):
        pid = await self.create_project()
        tail = f"projects/{pid}/reference-audio/silence"
        body = {"requestKey": "default-silence"}
        with patch.object(self.service, "default_reference_audio", wraps=self.service.default_reference_audio) as create:
            status, _, payload = await self.request("POST", tail, headers={"Origin": self.base}, json=body)
            self.assertEqual(status, 403, payload)
            self.assertEqual(json.loads(payload)["error"]["code"], "forbidden")
            create.assert_not_called()
            self.assertEqual(self.service.project(pid)["assets"], [])
            status, _, payload = await self.request("POST", tail, headers=self.action, json=body)
            self.assertEqual(status, 200, payload)
            asset = json.loads(payload)["asset"]
            status, _, payload = await self.request("POST", tail, headers=self.action, json=body)
            self.assertEqual(status, 200, payload)
            self.assertEqual(json.loads(payload)["asset"]["id"], asset["id"])
            self.assertEqual(create.call_count, 2)
        status, headers, data = await self.request("GET", f"projects/{pid}/assets/{asset['id']}/media")
        self.assertEqual(status, 200, data[:200])
        self.assertEqual(headers["Content-Type"], "audio/wav")
        self.assertEqual(hashlib.sha256(data).hexdigest(), asset["id"])
        with wave.open(io.BytesIO(data), "rb") as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()), (1, 2, 24000, 96000))
            self.assertEqual(wav.readframes(96000), bytes(192000))
        self.assertEqual(len(self.service.project(pid)["assets"]), 1)

    async def test_binary_upload_and_get_head_single_ranges_are_lossless(self):
        pid = await self.create_project()
        aid, data = await self.upload(pid)
        tail = f"projects/{pid}/assets/{aid}/media"
        status, headers, body = await self.request("GET", tail)
        self.assertEqual((status, body), (200, data))
        self.assertEqual(headers["Content-Length"], str(len(data)))
        self.assertEqual(headers["Content-Type"], "video/mp4")
        status, headers, body = await self.request("HEAD", tail)
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(headers["Content-Length"], str(len(data)))
        for requested, start, end in (("bytes=262140-262150", 262140, 262150),
                                      ("bytes=-11", len(data) - 11, len(data) - 1)):
            status, headers, body = await self.request("GET", tail, headers={"Range": requested})
            self.assertEqual((status, body), (206, data[start:end + 1]))
            self.assertEqual(headers["Content-Range"], f"bytes {start}-{end}/{len(data)}")
            self.assertEqual(headers["Content-Length"], str(end - start + 1))
        status, headers, body = await self.request("HEAD", tail, headers={"Range": "bytes=0-10"})
        self.assertEqual((status, body, headers["Content-Length"]), (206, b"", "11"))
        status, headers, body = await self.request("GET", tail, headers={"Range": f"bytes={len(data)}-"})
        self.assertEqual((status, body), (416, b""))
        self.assertEqual(headers["Content-Range"], f"bytes */{len(data)}")

    async def test_untrusted_origin_and_missing_action_cannot_mutate(self):
        for headers in ({**self.action, "Origin": "https://untrusted.invalid"}, {"Origin": self.base}):
            status, _, payload = await self.request("POST", "projects", headers=headers,
                                                  json={"title": "must not exist", "requestKey": "bad-origin"})
            self.assertEqual(status, 403, payload)
        self.assertEqual(self.service.projects()["projects"], [])

    async def test_chunked_upload_without_declared_length_is_rejected(self):
        pid = await self.create_project()
        async def chunks():
            yield b"incomplete contract"
        status, _, payload = await self.request("POST", f"projects/{pid}/assets",
                                              headers={**self.action, "Content-Type": "application/octet-stream"},
                                              data=chunks())
        self.assertEqual(status, 413, payload)
        self.assertEqual(self.service.project(pid)["assets"], [])

    async def test_redirect_is_not_followed(self):
        hits = []
        async def redirect(request):
            hits.append("redirect")
            raise web.HTTPFound(location="/must-not-follow")
        async def destination(request):
            hits.append("destination")
            return web.json_response({"unexpected": True})
        app = web.Application()
        app.router.add_get("/api/v1/drama/redirect", redirect)
        app.router.add_get("/must-not-follow", destination)
        self.bridge.UPSTREAM = await self.start_app(app) + "/api/v1/drama"
        status, _, payload = await self.request("GET", "redirect")
        self.assertEqual(status, 503, payload)
        self.assertEqual(hits, ["redirect"])
        self.assertEqual(json.loads(payload)["error"]["code"], "gateway_unavailable")

    async def test_upstream_disconnect_before_headers_returns_503_without_replaying_post(self):
        calls = []
        async def disconnect(request):
            calls.append(await request.read())
            request.transport.abort()
            return web.Response()
        app = web.Application()
        app.router.add_post("/api/v1/drama/projects", disconnect)
        self.bridge.UPSTREAM = await self.start_app(app) + "/api/v1/drama"
        body = {"title": "possibly accepted", "requestKey": "original-key"}
        status, _, payload = await self.request("POST", "projects", headers=self.action, json=body)
        self.assertEqual(status, 503, payload)
        self.assertEqual(json.loads(payload)["error"]["code"], "gateway_unavailable")
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(calls[0]), body)


if __name__ == "__main__":
    unittest.main()
