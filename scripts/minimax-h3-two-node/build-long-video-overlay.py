from pathlib import Path
import hashlib,json
root=Path(__file__).resolve().parents[2]
src=root/'evidence/h3-timeout-memory-repair/source'
out=root/'scripts/minimax-h3-two-node/long-video-overlay'
out.mkdir(exist_ok=True)
rows=[]
def emit(name,original,text,target):
 compile(text,name,'exec'); (out/name).write_text(text)
 rows.append(dict(file=name,target=target,source_sha256=hashlib.sha256(original.encode()).hexdigest(),sha256=hashlib.sha256(text.encode()).hexdigest()))
p=src/'api_server.py'; orig=p.read_text(); s=orig.replace('timeout=VIDEO_SYNC_TIMEOUT_S,','timeout=None if VIDEO_SYNC_TIMEOUT_S <= 0 else VIDEO_SYNC_TIMEOUT_S,')
s=s.replace('    request_id = f"video_sync-{random_uuid()}"','''    client_id = raw_request.headers.get("X-H3-Request-ID", "")
    if client_id and not __import__("re").fullmatch(r"[0-9a-f]{32}", client_id):
        raise HTTPException(status_code=400, detail="invalid H3 request ID")
    request_id = f"video_sync-{client_id or random_uuid()}"
    _h3_active_requests.add(request_id)''')
# route only acknowledges live requests, markers are consumed at collective safe boundaries.
pos=s.index('@router.post(\n    "/v1/videos/sync"')
s=s[:pos]+'''_h3_active_requests = set()

@router.post("/v1/videos/cancel")
async def h3_cancel_video(raw_request: Request):
    client_id = raw_request.headers.get("X-H3-Request-ID", "")
    if not __import__("re").fullmatch(r"[0-9a-f]{32}", client_id):
        raise HTTPException(status_code=400, detail="invalid H3 request ID")
    request_id = "video_sync-" + client_id
    if request_id not in _h3_active_requests:
        raise HTTPException(status_code=404, detail="request is not active")
    from pathlib import Path
    marker = Path("/tmp/ray/h3-cancel") / request_id
    marker.parent.mkdir(exist_ok=True)
    marker.touch()
    return {"cancel_requested": True, "boundary": "next sampling or decode boundary"}


'''+s[pos:]
# lifecycle cleanup after all sync handler exception branches, before successful response
a=s.index('async def create_video_sync')
b=s.index('    finally:',a)
s=s[:b]+s[b:].replace('    finally:\n', '    finally:\n        _h3_active_requests.discard(request_id)\n',1)
emit('api_server.py',orig,s,'/usr/local/lib/python3.12/dist-packages/vllm_omni/entrypoints/openai/api_server.py')
p=src/'minimax_h3/pipeline_minimax_h3.py';orig=p.read_text();s=orig
pos=s.index('    def decode(\n')
s=s[:pos]+'''    def _h3_cancel_boundary(self):
        from pathlib import Path
        group, rank, _ = _dit_rank_world()
        marker = Path("/tmp/ray/h3-cancel") / self._h3_request_id
        canceled = torch.tensor([int(rank == 0 and marker.is_file())], device=self.device)
        dist.broadcast(canceled, src=0, group=group)
        if canceled.item():
            if rank == 0:
                marker.unlink(missing_ok=True)
            raise RuntimeError("H3 request canceled at a collective safe boundary")

    def _h3_memory(self, phase):
        logger.info("H3_MEMORY phase=%s rank=%s allocated_mb=%.1f reserved_mb=%.1f peak_mb=%.1f",
                    phase, self._dit_rank, torch.cuda.memory_allocated()/2**20,
                    torch.cuda.memory_reserved()/2**20, torch.cuda.max_memory_allocated()/2**20)

'''+s[pos:]
s=s.replace('        if len(request.prompts) != 1:', '        self._h3_request_id = __import__("re").match(r"video_sync-[0-9a-f]{32}", request.request_id).group(0)\n        self._h3_cancel_boundary()\n        if len(request.prompts) != 1:')
s=s.replace('on_step=lambda step, video, audio: progress.update(),','on_step=lambda step, video, audio: (progress.update(), self._h3_cancel_boundary()),')
s=s.replace('        video, audio = self.decode(\n','''        self._h3_cancel_boundary()
        self._h3_memory("before_cache_release")
        torch.cuda.empty_cache()
        self._h3_memory("before_decode")
        video, audio = self.decode(
''')
s=s.replace('        return DiffusionOutput(\n            output=(video, audio),','        self._h3_memory("after_decode")\n        return DiffusionOutput(\n            output=(video, audio),')
# Synthetic longest-length VAE check before first real sampling, under full resident model weights.
s=s.replace('        seed = int(sampling.seed if sampling.seed is not None else 42)', '''        if not getattr(self, "_h3_decode_preflight_done", False):
            self._h3_memory("decode_preflight_start")
            torch.cuda.empty_cache()
            probe_t = MINIMAX_H3_SHAPE_PLANNER.video_latent_t(362)
            probe = torch.zeros((1, 24, probe_t, 48, 84), device=self.device)
            with current_omni_platform.create_autocast_context(device_type=self.device.type, dtype=torch.float16, enabled=True):
                decoded_probe = self.video_vae.decode_latent(probe)
            if tuple(decoded_probe.shape) != (1, 3, 362, 768, 1344):
                raise RuntimeError(f"H3 decode preflight shape mismatch: {tuple(decoded_probe.shape)}")
            self._h3_memory("decode_preflight_pass")
            del decoded_probe, probe
            torch.cuda.empty_cache()
            self._h3_decode_preflight_done = True
        self._h3_cancel_boundary()
        seed = int(sampling.seed if sampling.seed is not None else 42)''')
emit('pipeline_minimax_h3.py',orig,s,'/usr/local/lib/python3.12/dist-packages/vllm_omni/diffusion/models/minimax_h3/pipeline_minimax_h3.py')
p=src/'minimax_h3/vae.py';orig=p.read_text();s=orig.replace('        self.model = self.remote.model','''        self.model = self.remote.model
        # Bound decoder activation batch size; preserve native spatial/temporal blending.
        self.model.stack_tiling = False''',1)
emit('vae.py',orig,s,'/usr/local/lib/python3.12/dist-packages/vllm_omni/diffusion/models/minimax_h3/vae.py')
p=src/'executor.py';orig=p.read_text();s=orig
s=s.replace('        self._ray = ray','        self._ray = ray\n        self._rpc_lock = __import__("threading").RLock()',1)
s=s.replace('    def collective_rpc(\n','    def collective_rpc(self, *args, **kwargs):\n        with self._rpc_lock:\n            return self._collective_rpc(*args, **kwargs)\n\n    def _collective_rpc(\n',1)
s=s.replace('        if failures:\n            self._mark_failed()', '        if failures:\n            if not all(item.get("error") == "H3 request canceled at a collective safe boundary" for item in failures):\n                self._mark_failed()',1)
s=s.replace('    def check_health(self) -> None:\n', '    def check_health(self) -> None:\n        self._ensure_open()\n        if not self._rpc_lock.acquire(blocking=False):\n            return  # RPC owns the actors; its result reports failures.\n        try:\n            self._check_idle_health()\n        finally:\n            self._rpc_lock.release()\n\n    def _check_idle_health(self) -> None:\n',1)
emit('executor.py',orig,s,'/opt/h3-multinode/h3_multinode/executor.py')
(out/'manifest.json').write_text(json.dumps({'profile':'heretic-ref2va-hmnsfw-v1','files':rows},indent=2)+'\n')
