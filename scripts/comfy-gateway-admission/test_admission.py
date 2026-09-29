"""Exercise actual middleware with in-flight native handler scheduling."""
import asyncio
import threading
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


def load_module(server):
    old = sys.modules.get('server')
    sys.modules['server'] = SimpleNamespace(PromptServer=SimpleNamespace(instance=server))
    try:
        spec = importlib.util.spec_from_file_location('dgx_admission_test', Path(__file__).with_name('__init__.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if old is None:
            del sys.modules['server']
        else:
            sys.modules['server'] = old


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.running, self.pending = [], []
        queue = SimpleNamespace(mutex=threading.RLock(), get_current_queue_volatile=lambda: (list(self.running), list(self.pending)), delete_queue_item=lambda predicate: self.pending.__setitem__(slice(None), [row for row in self.pending if not predicate(row)]))
        self.server = SimpleNamespace(app=web.Application(), routes=web.RouteTableDef(), prompt_queue=queue)
        self.module = load_module(self.server)
        self.admission = self.server._dgx_gateway_admission
        self.started, self.complete = asyncio.Event(), asyncio.Event()
        self.enqueue = True
        async def prompt(request):
            self.started.set()
            await self.complete.wait()
            if self.enqueue:
                self.pending.append('accepted-job')
                return web.json_response({'prompt_id': 'accepted-job'})
            return web.json_response({'error': 'invalid'}, status=400)
        self.server.routes.post('/prompt')(prompt)
        self.server.routes.post('/api/prompt')(prompt)
        self.server.app.add_routes(self.server.routes)
        self.client = TestClient(TestServer(self.server.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        self.complete.set()
        await self.client.close()

    async def start_drain(self):
        task = asyncio.create_task(self.client.post('/dgx/drain'))
        for _ in range(100):
            if self.admission.draining:
                return task
            await asyncio.sleep(0.001)
        self.fail('drain never entered')

    async def test_inflight_validation_can_enqueue_after_drain_begins(self):
        submit = asyncio.create_task(self.client.post('/prompt'))
        await asyncio.wait_for(self.started.wait(), 1)
        drain = await self.start_drain()
        self.assertFalse(drain.done())
        self.assertEqual((await self.client.post('/api/prompt')).status, 503)
        self.complete.set()
        self.assertEqual((await submit).status, 200)
        response = await drain
        self.assertEqual(response.status, 409)
        state = await response.json()
        self.assertEqual(state['queue_pending'], 1)
        self.assertFalse(state['draining'])
        self.assertEqual(self.pending, ['accepted-job'])

    async def test_rejected_inflight_request_allows_fenced_idle_stop(self):
        self.enqueue = False
        submit = asyncio.create_task(self.client.post('/api/prompt'))
        await asyncio.wait_for(self.started.wait(), 1)
        drain = await self.start_drain()
        self.complete.set()
        self.assertEqual((await submit).status, 400)
        response = await drain
        self.assertTrue((await response.json())['drained'])
        self.assertEqual((await self.client.post('/prompt')).status, 503)
        self.assertEqual((await self.client.post('/api/prompt')).status, 503)

    async def test_timeout_resumes_admission_without_touching_queue(self):
        self.admission.timeout = 0.02
        submit = asyncio.create_task(self.client.post('/prompt'))
        await asyncio.wait_for(self.started.wait(), 1)
        response = await self.client.post('/dgx/drain')
        self.assertEqual(response.status, 409)
        self.assertFalse(self.admission.draining)
        self.complete.set()
        self.assertEqual((await submit).status, 200)
        self.assertEqual(self.pending, ['accepted-job'])

    async def test_cancel_refuses_wrong_owner_and_preserves_other_jobs(self):
        self.running.append([0, 'p1', {}, {'client_id': 'someone-else'}])
        response = await self.client.post('/dgx/cancel', json={'prompt_id': 'p1', 'client_id': 'mine'})
        self.assertEqual(response.status, 409)
        self.assertEqual(len(self.running), 1)

    async def test_cancel_pending_deletes_only_matching_prompt(self):
        self.pending.extend([[0, 'p1', {}, {'client_id': 'mine'}], [1, 'p2', {}, {'client_id': 'other'}]])
        response = await self.client.post('/dgx/cancel', json={'prompt_id': 'p1', 'client_id': 'mine'})
        self.assertEqual((await response.json())['state'], 'canceled_pending')
        self.assertEqual([row[1] for row in self.pending], ['p2'])

    async def test_cancel_running_interrupts_only_verified_owner(self):
        self.running.append([0, 'p1', {}, {'client_id': 'mine'}])
        management = SimpleNamespace(interrupt_current_processing=Mock())
        with patch.dict(sys.modules, {'comfy': SimpleNamespace(model_management=management), 'comfy.model_management': management}):
            response = await self.client.post('/dgx/cancel', json={'prompt_id': 'p1', 'client_id': 'mine'})
        self.assertEqual((await response.json())['state'], 'interrupt_requested')
        management.interrupt_current_processing.assert_called_once_with(True)

    async def test_existing_running_job_is_preserved(self):
        self.running.append('external-job')
        response = await self.client.post('/dgx/drain')
        self.assertEqual(response.status, 409)
        self.assertFalse(self.admission.draining)
        self.assertEqual(self.running, ['external-job'])
        health = await (await self.client.get('/dgx/health')).json()
        self.assertEqual(health['queue_running'], 1)


if __name__ == '__main__':
    unittest.main()
