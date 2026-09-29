#!/usr/bin/env python3
"""Apply the reviewed lifecycle patch without starting PersonaPlex."""
import ast
import hashlib
from pathlib import Path
import shutil
import sys

path = Path(sys.argv[1])
source = path.read_text()
marker = 'DGX_GATEWAY_PERSONAPLEX_V1'
if marker in source:
    print('already patched')
    raise SystemExit(0)
wrapper = '''    # DGX_GATEWAY_PERSONAPLEX_V1: admission precedes every shared model mutation.
    async def healthz(self, request):
        return web.json_response({"model": "personaplex", "ready": True,
                                  "draining": self.draining,
                                  "active_sessions": int(self.lock.locked())})

    async def drain_requests(self, request):
        self.draining = True
        return await self.healthz(request)

    async def handle_chat(self, request):
        if self.draining:
            raise web.HTTPServiceUnavailable(text="model is draining")
        if self.lock.locked():
            raise web.HTTPConflict(text="one active session is supported")
        voice = request.query.get("voice_prompt", "")
        allowed = {p.name for p in Path(self.voice_prompt_dir).glob("*.pt")}
        if voice not in allowed or len(request.query.get("text_prompt", "")) > 16000:
            raise web.HTTPBadRequest(text="invalid voice or text prompt")
        if "text_prompt" not in request.query:
            raise web.HTTPBadRequest(text="text_prompt is required")
        if "seed" in request.query:
            try:
                seed = int(request.query["seed"])
                if not -1 <= seed <= 4294967295:
                    raise ValueError()
            except ValueError:
                raise web.HTTPBadRequest(text="invalid seed")
        async with self.lock:
            return await self._handle_chat(request)

    async def _handle_chat(self, request):'''
changes = [
 ('    async def handle_chat(self, request):', wrapper),
 ('        self.lock = asyncio.Lock()', '        self.lock = asyncio.Lock()\n        self.draining = False'),
 ('        async with self.lock:\n            if seed', '        if True:  # Session lock is held by handle_chat.\n            if seed'),
 ('int(request["seed"])', 'int(request.query["seed"])'),
 ('    app.router.add_get("/api/chat", state.handle_chat)', '    app.router.add_get("/healthz", state.healthz)\n    app.router.add_post("/drain", state.drain_requests)\n    app.router.add_get("/api/chat", state.handle_chat)'),
 ('web.run_app(app, port=args.port, ssl_context=ssl_context)', 'web.run_app(app, host=args.host, port=args.port, ssl_context=ssl_context)'),
]
for old, new in changes:
    if source.count(old) != 1:
        raise RuntimeError(f'unexpected upstream source for {old[:70]!r}')
    source = source.replace(old, new)
ast.parse(source)
backup = path.with_name(path.name + '.pre-gateway-' + hashlib.sha256(path.read_bytes()).hexdigest()[:12])
if not backup.exists():
    shutil.copy2(path, backup)
path.write_text(source)
print(f'patched {path}; backup {backup}')
