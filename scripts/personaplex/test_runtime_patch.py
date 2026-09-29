"""Test patched native admission without loading torch or model weights."""
import ast
import asyncio
from pathlib import Path
import sys
import tempfile
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        source = ast.parse(Path(sys.argv[1]).read_text())
        original = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == 'ServerState')
        selected = [n for n in original.body if isinstance(n, ast.AsyncFunctionDef) and n.name in ('healthz', 'drain_requests', 'handle_chat')]
        cls = ast.ClassDef(name='State', bases=[], keywords=[], body=selected, decorator_list=[])
        module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
        scope = {'web': web, 'Path': Path}
        exec(compile(module, '<extracted-admission>', 'exec'), scope)
        self.state = scope['State']()
        self.directory = tempfile.TemporaryDirectory()
        Path(self.directory.name, 'NATF0.pt').touch()
        self.state.voice_prompt_dir = self.directory.name
        self.state.lock = asyncio.Lock()
        self.state.draining = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        async def chat(request):
            self.entered.set()
            await self.release.wait()
            return web.json_response({'seed': int(request.query.get('seed', '1'))})
        self.state._handle_chat = chat
        app = web.Application()
        app.router.add_get('/healthz', self.state.healthz)
        app.router.add_post('/drain', self.state.drain_requests)
        app.router.add_get('/api/chat', self.state.handle_chat)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        self.release.set()
        await self.client.close()
        self.directory.cleanup()

    async def test_busy_drain_finishes_existing(self):
        query = '/api/chat?voice_prompt=NATF0.pt&text_prompt=hello&seed=42'
        first = asyncio.create_task(self.client.get(query))
        await asyncio.wait_for(self.entered.wait(), 2)
        self.assertEqual((await self.client.get(query)).status, 409)
        response = await self.client.post('/drain')
        self.assertEqual(await response.json(), {'model': 'personaplex', 'ready': True, 'draining': True, 'active_sessions': 1})
        self.assertEqual((await self.client.get(query)).status, 503)
        self.release.set()
        self.assertEqual((await first).status, 200)
        self.assertEqual((await (await self.client.get('/healthz')).json())['active_sessions'], 0)

    async def test_invalid_inputs_do_not_take_lock(self):
        for query in ['voice_prompt=../other.pt&text_prompt=x', 'voice_prompt=NATF0.pt', 'voice_prompt=NATF0.pt&text_prompt=x&seed=bad', 'voice_prompt=NATF0.pt&text_prompt=x&seed=4294967296']:
            self.assertEqual((await self.client.get('/api/chat?' + query)).status, 400)
            self.assertFalse(self.state.lock.locked())


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]])
