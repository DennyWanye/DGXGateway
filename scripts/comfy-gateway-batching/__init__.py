"""Bounded native candidate sampling and owner-scoped incremental output evidence."""
from collections import OrderedDict
from copy import deepcopy
import json
import threading
import time

from aiohttp import web
from server import PromptServer

VERSION = 1
MAX_JOBS = 64
MAX_TRACE = 32
UINT64 = 1 << 64


def candidate_seeds(seed, candidate_indices):
    if type(seed) is not int or not 0 <= seed < UINT64:
        raise ValueError('seed must be an unsigned 64-bit integer')
    if not isinstance(candidate_indices, str):
        raise ValueError('candidate_indices must be a JSON array')
    try:
        indices = json.loads(candidate_indices)
    except (ValueError, TypeError) as exc:
        raise ValueError('candidate_indices must be JSON') from exc
    if (not isinstance(indices, list) or not indices
            or any(type(i) is not int or i < 0 for i in indices)
            or len(set(indices)) != len(indices)):
        raise ValueError('one group requires unique nonnegative integer candidate indices')
    return indices, [(seed + i) % UINT64 for i in indices]


def identity(value):
    return isinstance(value, str) and 1 <= len(value) <= 128 and not any(ord(c) < 32 for c in value)


class BatchProgress:
    def __init__(self, server):
        self.server = server
        self.lock = threading.RLock()
        self.jobs = OrderedDict()

    def owner(self, node_id, node_class):
        """Bind only to the unique currently executing queue entry containing this node."""
        node_id = str(node_id)
        if not identity(node_id):
            raise ValueError('missing execution node identity')
        queue = self.server.prompt_queue
        with queue.mutex:
            running, _ = queue.get_current_queue_volatile()
            matching = [r for r in running if len(r) > 3 and isinstance(r[2], dict)
                        and isinstance(r[2].get(node_id), dict)
                        and r[2][node_id].get('class_type') == node_class]
            if len(matching) != 1:
                raise RuntimeError('cannot uniquely bind batch node to an executing prompt')
            row = matching[0]
            client = row[3].get('client_id') if isinstance(row[3], dict) else None
            if not identity(row[1]) or not identity(client):
                raise RuntimeError('executing prompt requires explicit prompt_id and client_id')
            return row[1], client

    def begin(self, prompt_id, client, sampler_id, indices, seeds, steps):
        with self.lock:
            job = self.jobs.get(prompt_id)
            if job is None:
                job = {'version': VERSION, 'prompt_id': prompt_id, 'client_id': client,
                       'groups': {}, 'outputs': {}}
                self.jobs[prompt_id] = job
                while len(self.jobs) > MAX_JOBS:
                    self.jobs.popitem(last=False)
            if job['client_id'] != client:
                raise RuntimeError('prompt owner changed')
            if sampler_id in job['groups']:
                raise RuntimeError('this candidate group already executed; automatic retry is forbidden')
            used = {i for g in job['groups'].values() for i in g['indices']}
            if used.intersection(indices):
                raise ValueError('candidate indices must be unique across this prompt')
            job['groups'][sampler_id] = {
                'indices': list(indices), 'seeds': list(seeds), 'state': 'SAMPLING',
                'step': 0, 'total_steps': steps, 'actual_candidate_parallelism': 0,
                'forward_count': 0, 'trace': [], 'apply_forward_count': 0, 'apply_trace': [], 'duration_seconds': 0,
                'peak_memory_allocated': 0, 'peak_memory_reserved': 0,
            }

    def verify_info(self, info, prompt_id, client, state):
        if not isinstance(info, dict) or info.get('prompt_id') != prompt_id or info.get('client_id') != client:
            raise ValueError('batch dependency belongs to another prompt')
        with self.lock:
            job = self.jobs.get(prompt_id)
            group = job['groups'].get(info.get('sampler_id')) if job and job['client_id'] == client else None
            if (group is None or group['state'] != state or group['indices'] != info.get('indices')
                    or group['seeds'] != info.get('seeds')):
                raise ValueError('batch dependency does not match recorded execution')

    def update(self, info, **changes):
        with self.lock:
            job = self.jobs.get(info['prompt_id'])
            if job is None or job['client_id'] != info['client_id']:
                raise RuntimeError('batch progress identity unavailable')
            job['groups'][info['sampler_id']].update(changes)

    def forward(self, info, tensor, cond_or_uncond, *, source='diffusion_forward'):
        shape = list(tensor.shape)
        device = str(tensor.device)
        conds = list(cond_or_uncond) if isinstance(cond_or_uncond, (list, tuple)) else []
        count = len(conds)
        candidate_batch = shape[0] // count if shape and count and shape[0] % count == 0 else 0
        valid = device.startswith('cuda') and candidate_batch == len(info['indices'])
        evidence = {'source': source, 'input_shape': shape, 'device': device, 'dtype': str(tensor.dtype),
                    'cond_or_uncond': conds, 'candidate_batch': candidate_batch}
        with self.lock:
            group = self.jobs[info['prompt_id']]['groups'][info['sampler_id']]
            if source != 'diffusion_forward':
                group['apply_forward_count'] += 1
                if len(group['apply_trace']) < MAX_TRACE:
                    group['apply_trace'].append(evidence)
                return
            previous = group['forward_count']
            group['forward_count'] += 1
            actual = candidate_batch if valid else 0
            group['actual_candidate_parallelism'] = min(group['actual_candidate_parallelism'], actual) if previous else actual
            if len(group['trace']) < MAX_TRACE:
                group['trace'].append(evidence)

    def saved(self, info, node_id, images):
        if not isinstance(images, list) or len(images) != len(info['indices']):
            raise RuntimeError('saved image count differs from candidate group')
        safe = []
        for image in images:
            if not isinstance(image, dict) or not all(isinstance(image.get(k), str) for k in ('filename', 'subfolder', 'type')):
                raise RuntimeError('invalid saved image receipt')
            safe.append({k: image[k] for k in ('filename', 'subfolder', 'type')})
        with self.lock:
            job = self.jobs[info['prompt_id']]
            job['outputs'][str(node_id)] = {'images': safe, 'sampler_id': info['sampler_id'],
                                         'indices': list(info['indices']), 'seeds': list(info['seeds'])}
            job['groups'][info['sampler_id']]['state'] = 'SAVED'

    async def get(self, request):
        pid, client = request.match_info.get('prompt_id'), request.query.get('client_id')
        if (not identity(pid) or not identity(client) or set(request.query) != {'client_id'}
                or len(request.query.getall('client_id')) != 1):
            return web.json_response({'error': 'invalid_identity'}, status=400)
        with self.lock:
            job = self.jobs.get(pid)
            if job is None:
                return web.json_response({'error': 'progress_unavailable'}, status=404)
            if job['client_id'] != client:
                return web.json_response({'error': 'owner_mismatch'}, status=403)
            result = deepcopy(job)
        return web.json_response(result)


def install(server):
    existing = getattr(server, '_dgx_batch_progress', None)
    if existing is not None:
        return existing
    progress = BatchProgress(server)
    server.routes.get('/dgx/batch-progress/{prompt_id}')(progress.get)
    server._dgx_batch_progress = progress
    return progress


def current_progress():
    return install(PromptServer.instance)


class DGXSeedBatchKSampler:
    CATEGORY = 'DGX/candidate batches'
    RETURN_TYPES = ('LATENT', 'DGX_BATCH_INFO')
    RETURN_NAMES = ('samples', 'batch_info')
    FUNCTION = 'sample'

    @classmethod
    def INPUT_TYPES(cls):
        import nodes
        schema = deepcopy(nodes.KSampler.INPUT_TYPES())
        schema['required']['candidate_indices'] = ('STRING', {'default': '[0,1]'})
        schema.setdefault('optional', {})['after_batch'] = ('DGX_BATCH_INFO',)
        schema['optional']['sigmas'] = ('SIGMAS',)
        schema['hidden'] = {'unique_id': 'UNIQUE_ID'}
        return schema

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float('nan')  # Every prompt needs its own owner-bound evidence.

    def sample(self, model, seed, steps, cfg, sampler_name, scheduler, positive,
               negative, latent_image, denoise=1.0, candidate_indices='[0,1]',
               after_batch=None, unique_id=None, sigmas=None):
        indices, seeds = candidate_seeds(seed, candidate_indices)
        progress = current_progress()
        pid, client = progress.owner(unique_id, 'DGXSeedBatchKSampler')
        sid = str(unique_id)
        if after_batch is not None and (not isinstance(after_batch, dict)
                or after_batch.get('prompt_id') != pid or after_batch.get('client_id') != client
                or after_batch.get('saved') is not True):
            raise ValueError('after_batch must be a saved group from this same prompt')
        if after_batch is not None:
            progress.verify_info(after_batch, pid, client, 'SAVED')
        info = {'version': VERSION, 'prompt_id': pid, 'client_id': client,
                'sampler_id': sid, 'indices': indices, 'seeds': seeds, 'saved': False}
        progress.begin(pid, client, sid, indices, seeds, steps)
        started = time.perf_counter()
        cuda = None
        device = None
        diffusion_hook = None
        try:
            import torch
            import comfy.sample
            import comfy.utils
            import latent_preview
            latent = latent_image['samples']
            if latent.shape[0] != 1:
                raise ValueError('candidate batching requires exactly one source latent')
            latent = comfy.sample.fix_empty_latent_channels(model, latent,
                latent_image.get('downscale_ratio_spacial'), latent_image.get('downscale_ratio_temporal'))
            # Seed each single candidate before concatenating: grouping never changes its RNG stream.
            noise = torch.cat([comfy.sample.prepare_noise(latent, value) for value in seeds], dim=0)
            batch = latent.repeat((len(indices),) + (1,) * (latent.ndim - 1))
            mask = latent_image.get('noise_mask')
            if mask is not None and mask.shape[0] != 1:
                raise ValueError('candidate batching requires a single shared noise mask')
            clone = model.clone()
            existing = clone.model_options.get('model_function_wrapper')
            context = threading.local()
            diffusion = clone.get_model_object('diffusion_model')
            def on_diffusion(_module, args, kwargs):
                x = args[0] if args else kwargs.get('x', kwargs.get('hidden_states'))
                if x is None:
                    raise RuntimeError('diffusion forward did not expose its input tensor')
                progress.forward(info, x, getattr(context, 'conds', None), source='diffusion_forward')
            # ModelPatcher clones share their underlying module; remove this task-scoped hook in finally.
            diffusion_hook = diffusion.register_forward_pre_hook(on_diffusion, with_kwargs=True)

            def wrapper(apply_model, args):
                previous = getattr(context, 'conds', None)
                context.conds = args.get('cond_or_uncond')
                def actual_forward(*call_args, **call_kwargs):
                    x = call_args[0] if call_args else call_kwargs.get('x', call_kwargs.get('input'))
                    progress.forward(info, x, context.conds, source='apply_model_input')
                    return apply_model(*call_args, **call_kwargs)
                try:
                    if existing is not None:
                        return existing(actual_forward, args)
                    return actual_forward(args['input'], args['timestep'], **args['c'])
                finally:
                    context.conds = previous

            # Comfy inspects optional wrapper-owned models and moves them with .to().
            # Keep these lifecycle hooks while retaining our tracing adapter.
            if existing is not None and hasattr(existing, 'models'):
                wrapper.models = lambda: existing.models()
            if existing is not None and hasattr(existing, 'to'):
                def move_wrapper(*args, **kwargs):
                    nonlocal existing
                    moved = existing.to(*args, **kwargs)
                    if moved is not None:
                        existing = moved
                    return wrapper
                wrapper.to = move_wrapper
            clone.set_model_unet_function_wrapper(wrapper)
            preview = latent_preview.prepare_callback(clone, steps)
            def callback(step, x0, x, total_steps):
                progress.update(info, step=step + 1, total_steps=total_steps)
                if preview:
                    preview(step, x0, x, total_steps)
            device = clone.load_device
            if str(device).startswith('cuda'):
                cuda = torch.cuda
                cuda.synchronize(device)
                cuda.reset_peak_memory_stats(device)
            if sigmas is None:
                output = comfy.sample.sample(clone, noise, steps, cfg, sampler_name, scheduler,
                    positive, negative, batch, denoise=denoise, noise_mask=mask,
                    callback=callback, disable_pbar=not comfy.utils.PROGRESS_BAR_ENABLED, seed=seed)
            else:
                import comfy.samplers
                if len(sigmas) != steps + 1 or denoise != 1.0:
                    raise ValueError('custom schedule must match the full requested steps')
                output = comfy.sample.sample_custom(clone, noise, cfg,
                    comfy.samplers.sampler_object(sampler_name), sigmas, positive, negative,
                    batch, noise_mask=mask, callback=callback,
                    disable_pbar=not comfy.utils.PROGRESS_BAR_ENABLED, seed=seed)
            if cuda is not None:
                cuda.synchronize(device)
            result = latent_image.copy()
            for key in ('downscale_ratio_spacial', 'downscale_ratio_temporal', 'batch_index'):
                result.pop(key, None)
            result['samples'] = output
            progress.update(info, state='SAMPLED')
            return result, info
        except BaseException as exc:
            # Keep Comfy's cancellation exception and normal error handling intact.
            progress.update(info, state='FAILED', error_type=type(exc).__name__)
            raise
        finally:
            if diffusion_hook is not None:
                diffusion_hook.remove()
            metrics = {'duration_seconds': time.perf_counter() - started}
            if cuda is not None:
                try:
                    metrics.update(peak_memory_allocated=int(cuda.max_memory_allocated(device)),
                                   peak_memory_reserved=int(cuda.max_memory_reserved(device)))
                except Exception:
                    pass
            progress.update(info, **metrics)


class DGXBatchSaveImage:
    CATEGORY = 'DGX/candidate batches'
    RETURN_TYPES = ('DGX_BATCH_INFO',)
    RETURN_NAMES = ('after_batch',)
    FUNCTION = 'save_images'
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {'required': {'images': ('IMAGE',), 'batch_info': ('DGX_BATCH_INFO',),
                             'filename_prefix': ('STRING', {'default': 'dgx-batch'})},
                'hidden': {'unique_id': 'UNIQUE_ID', 'prompt': 'PROMPT', 'extra_pnginfo': 'EXTRA_PNGINFO'}}

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float('nan')

    def save_images(self, images, batch_info, filename_prefix='dgx-batch',
                    unique_id=None, prompt=None, extra_pnginfo=None):
        progress = current_progress()
        pid, client = progress.owner(unique_id, 'DGXBatchSaveImage')
        if (not isinstance(batch_info, dict) or batch_info.get('prompt_id') != pid
                or batch_info.get('client_id') != client or batch_info.get('saved') is not False):
            raise ValueError('batch_info must come from this prompt sampler')
        progress.verify_info(batch_info, pid, client, 'SAMPLED')
        try:
            if len(images) != len(batch_info['indices']):
                raise ValueError('decoded image count differs from candidate indices')
            import nodes
            response = nodes.SaveImage().save_images(images, filename_prefix, prompt, extra_pnginfo)
            receipts = response['ui']['images']
            progress.saved(batch_info, unique_id, receipts)
            saved = {**batch_info, 'saved': True}
            return {'ui': {'images': receipts}, 'result': (saved,)}
        except BaseException as exc:
            progress.update(batch_info, state='FAILED', error_type=type(exc).__name__)
            raise


NODE_CLASS_MAPPINGS = {'DGXSeedBatchKSampler': DGXSeedBatchKSampler,
                       'DGXBatchSaveImage': DGXBatchSaveImage}
NODE_DISPLAY_NAME_MAPPINGS = {'DGXSeedBatchKSampler': 'DGX · Seeded candidate batch',
                              'DGXBatchSaveImage': 'DGX · Save candidate group'}
install(PromptServer.instance)
