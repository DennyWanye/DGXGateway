"""Actual aiohttp routes and HTTP image responses; no Gateway/model operations."""
import base64
import importlib
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC')
JOB = 'a' * 32
FILENAME = 'candidate-0.png'


class CreativeImageBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        source = Path(os.environ.get('DGX_CREATIVE_NODE_DIR', str(Path(__file__).resolve().parents[1] / 'ComfyUI/custom_nodes/dgx_remote_h3')))
        package = ModuleType('_creative_image_test_pkg'); package.__path__ = [str(source)]
        routes = web.RouteTableDef()
        fake_server = ModuleType('server'); fake_server.PromptServer = SimpleNamespace(instance=SimpleNamespace(routes=routes))
        self.modules = patch.dict(sys.modules, {'_creative_image_test_pkg': package, 'server': fake_server})
        self.modules.start(); self.addCleanup(self.modules.stop)
        for name in ('_creative_image_test_pkg.creative_client', '_creative_image_test_pkg.creative_routes'):
            sys.modules.pop(name, None)
        self.bridge = importlib.import_module('_creative_image_test_pkg.creative_routes')
        self.status = 200; self.body = PNG; self.headers = {}; self.hits = []
        upstream = web.Application(); upstream.router.add_get('/{tail:.*}', self.serve_image)
        self.upstream = TestServer(upstream); await self.upstream.start_server()
        self.origin = str(self.upstream.make_url('')).rstrip('/')
        self.bridge.creative_client.GATEWAY = self.origin + '/mcp'
        self.target = self.origin + '/api/v1/artifacts/' + JOB + '/' + FILENAME
        self.artifact = {'kind': 'images', 'url': self.target, 'path': '/state/creative-artifacts/' + JOB + '/' + FILENAME}
        self.job = {'job_id': JOB, 'artifacts': [self.artifact]}
        self.forward = Mock(side_effect=lambda payload: {'result': {'structuredContent': self.job, 'isError': False}})
        self.bridge.forward = self.forward
        app = web.Application(); app.add_routes(routes)
        self.client = TestClient(TestServer(app)); await self.client.start_server()
        self.route = '/dgx-creative/artifacts/' + JOB + '/' + FILENAME

    async def asyncTearDown(self):
        await self.client.close(); await self.upstream.close()

    async def serve_image(self, request):
        self.hits.append(request.path)
        if self.body == 'chunked-oversize':
            response = web.StreamResponse(); response.enable_chunked_encoding(); await response.prepare(request)
            try:
                for _ in range(129): await response.write(b'x' * (64 * 1024))
                await response.write_eof()
            except ConnectionResetError:
                pass
            return response
        return web.Response(body=self.body, status=self.status, headers=self.headers)

    async def test_real_png_is_same_origin_with_correct_type_and_only_readonly_query(self):
        response = await self.client.get(self.route)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), PNG)
        self.assertEqual(response.headers['Content-Type'], 'image/png')
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(self.hits, ['/api/v1/artifacts/' + JOB + '/' + FILENAME])
        self.forward.assert_called_once_with({'jsonrpc': '2.0', 'id': 'image-read', 'method': 'tools/call',
            'params': {'name': 'comfy_job', 'arguments': {'job_id': JOB}}})

    async def test_invalid_identity_never_queries_gateway(self):
        for route in ('/dgx-creative/artifacts/not-hex/image.png',
                      '/dgx-creative/artifacts/' + JOB + '/bad%2Ffile.png',
                      '/dgx-creative/artifacts/' + JOB + '/bad%5Cfile.png'):
            with self.subTest(route=route):
                self.assertEqual((await self.client.get(route)).status, 400)
        self.forward.assert_not_called(); self.assertEqual(self.hits, [])

    async def test_unowned_url_path_kind_or_job_never_fetches_image(self):
        originals = dict(self.artifact)
        for changes in ({'url': 'https://attacker.example/image.png'}, {'url': self.target + '?other=1'},
                        {'path': '/other/' + FILENAME}, {'path': '/state/' + JOB + '/other.png'}, {'kind': 'videos'}):
            with self.subTest(changes=changes):
                self.artifact.clear(); self.artifact.update(originals); self.artifact.update(changes)
                self.assertEqual((await self.client.get(self.route)).status, 404)
        self.artifact.clear(); self.artifact.update(originals); self.job['job_id'] = 'b' * 32
        self.assertEqual((await self.client.get(self.route)).status, 404)
        self.assertEqual(self.hits, [])

    async def test_redirect_is_rejected_without_following(self):
        self.status = 302; self.headers = {'Location': self.origin + '/forbidden'}
        self.assertEqual((await self.client.get(self.route)).status, 503)
        self.assertEqual(len(self.hits), 1)

    async def test_declared_oversize_rejected(self):
        self.body = b'x' * (self.bridge.MAX_IMAGE_BYTES + 1)
        self.assertEqual((await self.client.get(self.route)).status, 413)

    async def test_chunked_oversize_rejected(self):
        self.body = 'chunked-oversize'
        self.assertEqual((await self.client.get(self.route)).status, 413)

    async def test_missing_artifact_and_upstream_missing_are_404(self):
        self.job['artifacts'] = []
        self.assertEqual((await self.client.get(self.route)).status, 404)
        self.assertEqual(self.hits, [])
        self.job['artifacts'] = [self.artifact]; self.status = 404
        self.assertEqual((await self.client.get(self.route)).status, 404)

    async def test_network_unavailable_is_503(self):
        await self.upstream.close()
        self.assertEqual((await self.client.get(self.route)).status, 503)
        self.forward.side_effect = OSError('Gateway unavailable')
        self.assertEqual((await self.client.get(self.route)).status, 503)

    async def test_nonimage_bytes_are_not_served_even_with_image_metadata(self):
        self.body = b'<html>not an image</html>'; self.headers = {'Content-Type': 'image/png'}
        self.assertEqual((await self.client.get(self.route)).status, 502)
