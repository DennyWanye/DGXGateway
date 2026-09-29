"""Native Comfy prompt admission fence for DGX Gateway model retirement."""
import asyncio

from aiohttp import web
from server import PromptServer

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}


class GatewayAdmission:
    def __init__(self, server, timeout=10.0):
        self.server = server
        self.timeout = timeout
        self.draining = False
        self.inflight = 0
        self.idle = asyncio.Event()
        self.idle.set()
        self.drain_lock = asyncio.Lock()

    @web.middleware
    async def middleware(self, request, handler):
        # Comfy registers both original routes and /api-prefixed aliases.
        if request.method != 'POST' or request.path not in {'/prompt', '/api/prompt'}:
            return await handler(request)
        # No await between admission decision and counter increment.
        if self.draining:
            return web.json_response({'error': 'dgx_draining'}, status=503)
        self.inflight += 1
        self.idle.clear()
        try:
            return await handler(request)
        finally:
            self.inflight -= 1
            if self.inflight == 0:
                self.idle.set()

    def status(self):
        running, pending = self.server.prompt_queue.get_current_queue_volatile()
        return {'component': 'dgx_gateway_admission', 'version': 1,
                'draining': self.draining, 'inflight_submissions': self.inflight,
                'queue_running': len(running), 'queue_pending': len(pending)}

    async def health(self, request):
        return web.json_response(self.status())

    async def cancel(self, request):
        """Target one Gateway-owned prompt; never use the global /interrupt blindly."""
        value = await request.json()
        if not isinstance(value, dict) or set(value) != {'prompt_id', 'client_id'}:
            return web.json_response({'error': 'invalid_identity'}, status=400)
        prompt_id, client_id = value['prompt_id'], value['client_id']
        if not all(isinstance(v, str) and 1 <= len(v) <= 128 for v in (prompt_id, client_id)):
            return web.json_response({'error': 'invalid_identity'}, status=400)
        queue = self.server.prompt_queue
        # Comfy's task_done and get take this same RLock. Identity check and
        # interrupt must be atomic with respect to advancing to the next prompt.
        with queue.mutex:
            running, pending = queue.get_current_queue_volatile()
            matching = [row for row in running + pending if row[1] == prompt_id]
            if any(len(row) <= 3 or row[3].get('client_id') != client_id for row in matching):
                return web.json_response({'error': 'owner_mismatch'}, status=409)
            if any(row[1] == prompt_id for row in pending):
                queue.delete_queue_item(lambda row: row[1] == prompt_id)
                return web.json_response({'state': 'canceled_pending'})
            if any(row[1] == prompt_id for row in running):
                import comfy.model_management
                comfy.model_management.interrupt_current_processing(True)
                return web.json_response({'state': 'interrupt_requested'})
        return web.json_response({'state': 'not_queued'})

    async def drain(self, request):
        async with self.drain_lock:
            self.draining = True
            try:
                # A request accepted before drain can still be validating before
                # enqueueing. Wait for every accepted handler to return first.
                await asyncio.wait_for(self.idle.wait(), timeout=self.timeout)
                state = self.status()
                if state['queue_running'] or state['queue_pending']:
                    self.draining = False
                    return web.json_response({**state, 'draining': False,
                                              'drained': False, 'error': 'comfy_busy'}, status=409)
                return web.json_response({**state, 'drained': True})
            except asyncio.TimeoutError:
                self.draining = False
                return web.json_response({**self.status(), 'drained': False,
                                          'error': 'submission_drain_timeout'}, status=409)
            except BaseException:
                # Do not strand normal users after a failed queue inspection or
                # canceled drain. A successful response intentionally stays fenced.
                self.draining = False
                raise


def install(server):
    existing = getattr(server, '_dgx_gateway_admission', None)
    if existing is not None:
        return existing
    admission = GatewayAdmission(server)
    server.app.middlewares.insert(0, admission.middleware)
    server.routes.get('/dgx/health')(admission.health)
    server.routes.post('/dgx/drain')(admission.drain)
    server.routes.post('/dgx/cancel')(admission.cancel)
    server._dgx_gateway_admission = admission
    return admission


install(PromptServer.instance)
