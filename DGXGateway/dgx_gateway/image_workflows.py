"""Pinned image workflows. Agents call these only through the typed MCP tools."""
from .image_batches import batch_graph

QUALITY_GENERATE_MODEL = 'flux2-dev'
PREVIEW_GENERATE_MODEL = 'z-image-turbo'
QUALITY_EDIT_MODEL = 'qwen-image-edit-2511'
_REJECTED_GENERATE_MODELS = frozenset({'flux2-dev-abliterated', 'llada-image-turbo'})
_IMAGE_WORKFLOW_CLASS_TYPES = frozenset({
    'LLaDAImageLoader', 'DGXLLaDATurboBatch', 'TextEncodeQwenImageEditPlus',
    'EmptyFlux2LatentImage', 'Flux2Scheduler', 'FluxKontextImageScale',
    'FluxKontextMultiReferenceLatentMethod', 'ReferenceLatent',
})
FLUX2_EDIT_MODEL = 'flux2-dev'
FLUX2_REFERENCE_LIMIT = 8
_IMAGE_WORKFLOW_MARKERS = (
    'flux2_dev_fp8mixed.safetensors', 'z_image_turbo_bf16.safetensors',
    'qwen_image_edit_2511_fp8mixed.safetensors', 'qwen_image_edit_2511_bf16.safetensors',
    'qwen_image_vae.safetensors', 'flux2-vae.safetensors', 'LLaDA-Image-Turbo',
    'LLaDa_VAE.safetensors', 'mistral_3_small_flux2', 'mistral_small_3_abliterated',
)
_IMAGE_CLIP_TYPES = frozenset({'flux2', 'qwen_image', 'lumina2'})
_IMAGE_PATH_ERROR = 'image generation must use generate_image or edit_image; do not submit a raw image workflow'


def resolve_generate_model(model=None):
    return QUALITY_GENERATE_MODEL if not model else model


def assert_image_policy(tool, parameters):
    """Quality-first contract for new MCP and drama image requests. Graph builders stay available for historical retries."""
    if tool == 'generate_image':
        model = parameters.get('model', QUALITY_GENERATE_MODEL)
        if model in _REJECTED_GENERATE_MODELS:
            raise ValueError('not a quality image model; use flux2-dev for a new image or edit_image to change an existing one')
        if model == PREVIEW_GENERATE_MODEL and parameters.get('preview') is not True:
            raise ValueError('z-image-turbo is preview-only; set preview=true only after the user explicitly asked for a quick look')
    elif tool == 'edit_image' and parameters.get('model', QUALITY_EDIT_MODEL) not in {QUALITY_EDIT_MODEL, FLUX2_EDIT_MODEL}:
        raise ValueError('not the quality edit path; use qwen-image-edit-2511 or flux2-dev')
    if tool == 'generate_image' and parameters.get('asset_ids') and parameters.get('model', QUALITY_GENERATE_MODEL) != FLUX2_EDIT_MODEL:
        raise ValueError('multi-reference generation requires flux2-dev')


def assert_typed_image_path(workflow):
    if not isinstance(workflow, dict):
        return
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        if node.get('class_type') in _IMAGE_WORKFLOW_CLASS_TYPES:
            raise ValueError(_IMAGE_PATH_ERROR)
        inputs = node.get('inputs')
        if not isinstance(inputs, dict):
            continue
        if inputs.get('type') in _IMAGE_CLIP_TYPES:
            raise ValueError(_IMAGE_PATH_ERROR)
        if any(marker in ' '.join(str(value) for value in inputs.values()) for marker in _IMAGE_WORKFLOW_MARKERS):
            raise ValueError(_IMAGE_PATH_ERROR)


def generate_image(prompt, width=1024, height=1024, seed=42, batch_count=1,
                   parallelism=2, candidate_indices=None, model=QUALITY_GENERATE_MODEL, *, image_filenames=None):
    """Omitted model is the quality default. Preview and historical graphs pass an explicit model."""
    args = (prompt, width, height, seed, batch_count, parallelism, candidate_indices)
    if model == 'z-image-turbo':
        if image_filenames:
            raise ValueError('multi-reference generation requires flux2-dev')
        return z_image(*args)
    if model in {'flux2-dev', 'flux2-dev-abliterated'}:
        return flux2_image(*args, abliterated=model.endswith('-abliterated'), image_filenames=image_filenames)
    if model == 'llada-image-turbo':
        if image_filenames:
            raise ValueError('multi-reference generation requires flux2-dev')
        return llada_turbo_image(*args)
    raise ValueError('unsupported image model')


def _new_image_inputs(prompt, width, height):
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError('prompt required')
    if any(type(v) is not int or not 256 <= v <= 1536 or v % 32 for v in (width, height)):
        raise ValueError('new model dimensions must be 256–1536 and multiples of 32')


def _flux2_reference_names(image_filenames):
    if not image_filenames:
        return []
    if not isinstance(image_filenames, (list, tuple)) or not 1 <= len(image_filenames) <= FLUX2_REFERENCE_LIMIT:
        raise ValueError(f'FLUX.2 accepts 1–{FLUX2_REFERENCE_LIMIT} reference images')
    if any(not isinstance(name, str) or not name for name in image_filenames):
        raise ValueError('FLUX.2 reference filenames must be non-empty strings')
    return list(image_filenames)


def _attach_flux2_references(graph, filenames, *, derive_size):
    """Chain official ReferenceLatent conditioning. Edit size follows the first scaled reference."""
    conditioning = ['7', 0]
    for index, filename in enumerate(filenames):
        load_id, scale_id, encode_id, ref_id = (str(20 + index), str(40 + index), str(50 + index), str(60 + index))
        graph[load_id] = {'class_type': 'LoadImage', 'inputs': {'image': filename}}
        graph[scale_id] = {'class_type': 'ImageScaleToTotalPixels', 'inputs': {
            'image': [load_id, 0], 'upscale_method': 'lanczos', 'megapixels': 1.0, 'resolution_steps': 16}}
        graph[encode_id] = {'class_type': 'VAEEncode', 'inputs': {'pixels': [scale_id, 0], 'vae': ['3', 0]}}
        graph[ref_id] = {'class_type': 'ReferenceLatent', 'inputs': {'conditioning': conditioning, 'latent': [encode_id, 0]}}
        conditioning = [ref_id, 0]
    graph['8']['inputs']['positive'] = conditioning
    if derive_size:
        graph['68'] = {'class_type': 'GetImageSize', 'inputs': {'image': ['40', 0]}}
        graph['6']['inputs']['width'] = ['68', 0]
        graph['6']['inputs']['height'] = ['68', 1]
        graph['11']['inputs']['width'] = ['68', 0]
        graph['11']['inputs']['height'] = ['68', 1]
    return graph


def flux2_image(prompt, width=1024, height=1024, seed=42, batch_count=1,
                parallelism=2, candidate_indices=None, *, abliterated=False, image_filenames=None, derive_size=False):
    filenames = _flux2_reference_names(image_filenames)
    if derive_size:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError('prompt required')
        if not filenames:
            raise ValueError('FLUX.2 editing requires a reference image')
    else:
        _new_image_inputs(prompt, width, height)
    encoder = ('mistral_small_3_abliterated_v2_fp8mixed.safetensors' if abliterated
               else 'mistral_3_small_flux2_bf16.safetensors')
    graph = {
        '1': {'class_type': 'UNETLoader', 'inputs': {'unet_name': 'flux2_dev_fp8mixed.safetensors', 'weight_dtype': 'default'}},
        '2': {'class_type': 'CLIPLoader', 'inputs': {'clip_name': encoder, 'type': 'flux2', 'device': 'default'}},
        '3': {'class_type': 'VAELoader', 'inputs': {'vae_name': 'flux2-vae.safetensors'}},
        '4': {'class_type': 'CLIPTextEncode', 'inputs': {'clip': ['2', 0], 'text': prompt}},
        '5': {'class_type': 'ConditioningZeroOut', 'inputs': {'conditioning': ['4', 0]}},
        '6': {'class_type': 'EmptyFlux2LatentImage', 'inputs': {'width': width, 'height': height, 'batch_size': 1}},
        '7': {'class_type': 'FluxGuidance', 'inputs': {'conditioning': ['4', 0], 'guidance': 4.0}},
        '8': {'class_type': 'KSampler', 'inputs': {'model': ['1', 0], 'positive': ['7', 0], 'negative': ['5', 0],
              'latent_image': ['6', 0], 'seed': seed, 'steps': 20, 'cfg': 1.0, 'sampler_name': 'euler',
              'scheduler': 'simple', 'denoise': 1.0, 'sigmas': ['11', 0]}},
        '9': {'class_type': 'VAEDecode', 'inputs': {'samples': ['8', 0], 'vae': ['3', 0]}},
        '10': {'class_type': 'SaveImage', 'inputs': {'images': ['9', 0], 'filename_prefix': 'dgx-flux2'}},
        '11': {'class_type': 'Flux2Scheduler', 'inputs': {'steps': 20, 'width': width, 'height': height}},
    }
    if filenames:
        _attach_flux2_references(graph, filenames, derive_size=derive_size)
    return batch_graph(graph, '8', '9', '10', batch_count, parallelism, candidate_indices)


def llada_turbo_image(prompt, width=1024, height=1024, seed=42, batch_count=1,
                      parallelism=2, candidate_indices=None, *, source_filename=None):
    import json
    from .image_batches import candidate_indices as validate_indices
    _new_image_inputs(prompt, width, height)
    indices = validate_indices(batch_count, parallelism, candidate_indices)
    graph = {'1': {'class_type': 'LLaDAImageLoader', 'inputs': {
        'diffusion_model': 'LLaDA-Image-Turbo-INT8.safetensors',
        'text_encoder': 'LLaDA-Image-Turbo-text_encoder-Q4_K_M.gguf', 'vae': 'LLaDa_VAE.safetensors',
        'dtype': 'bfloat16', 'offload': 'cuda', 'vae_tiling': 'Off'}}}
    if source_filename is not None:
        graph['2'] = {'class_type': 'LoadImage', 'inputs': {'image': source_filename}}
    previous = None
    for group, start in enumerate(range(0, len(indices), parallelism)):
        sid, oid = str(10 + group*2), str(11 + group*2)
        inputs = {'pipeline': ['1', 0], 'prompt': prompt, 'width': width, 'height': height,
                  'seed': seed, 'steps': 4, 'candidate_indices': json.dumps(indices[start:start+parallelism])}
        if source_filename is not None:
            inputs['source_image'] = ['2', 0]
        if previous:
            inputs['after_batch'] = [previous, 0]
        graph[sid] = {'class_type': 'DGXLLaDATurboBatch', 'inputs': inputs}
        graph[oid] = {'class_type': 'DGXBatchSaveImage', 'inputs': {
            'images': [sid, 0], 'batch_info': [sid, 1], 'filename_prefix': f'dgx-llada-turbo-g{group+1:02d}'}}
        previous = oid
    return graph

def z_image(prompt, width=1024, height=1024, seed=42, batch_count=1, parallelism=2, candidate_indices=None):
    if not isinstance(prompt,str) or not prompt.strip(): raise ValueError('prompt required')
    if width%16 or height%16: raise ValueError('dimensions must be multiples of 16')
    graph = {
        '1':{'class_type':'UNETLoader','inputs':{'unet_name':'z_image_turbo_bf16.safetensors','weight_dtype':'default'}},
        '2':{'class_type':'CLIPLoader','inputs':{'clip_name':'qwen_3_4b.safetensors','type':'lumina2','device':'default'}},
        '3':{'class_type':'VAELoader','inputs':{'vae_name':'ae.safetensors'}},
        '4':{'class_type':'CLIPTextEncode','inputs':{'clip':['2',0],'text':prompt}},
        '5':{'class_type':'ConditioningZeroOut','inputs':{'conditioning':['4',0]}},
        '6':{'class_type':'EmptySD3LatentImage','inputs':{'width':width,'height':height,'batch_size':1}},
        '7':{'class_type':'ModelSamplingAuraFlow','inputs':{'model':['1',0],'shift':3.0}},
        '8':{'class_type':'KSampler','inputs':{'model':['7',0],'positive':['4',0],'negative':['5',0],'latent_image':['6',0],'seed':seed,'steps':8,'cfg':1.0,'sampler_name':'res_multistep','scheduler':'simple','denoise':1.0}},
        '9':{'class_type':'VAEDecode','inputs':{'samples':['8',0],'vae':['3',0]}},
        '10':{'class_type':'SaveImage','inputs':{'images':['9',0],'filename_prefix':'dgx-z-image'}},
    }
    return batch_graph(graph, "8", "9", "10", batch_count, parallelism, candidate_indices)


def qwen_image_edit(prompt, image_filenames, seed=42, steps=40, encoder="standard", model_precision="fp8", batch_count=1, parallelism=2, candidate_indices=None):
    """Native Comfy Qwen Edit 2511 graph; image 1 defines output aspect ratio."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError('prompt required')
    if not 1 <= len(image_filenames) <= 3:
        raise ValueError('provide one to three registered images')
    if not 20 <= steps <= 40:
        raise ValueError('steps must be between 20 and 40')
    encoders = {"standard": "qwen_2.5_vl_7b_fp8_scaled.safetensors", "huihui-abliterated-bf16": "qwen_2.5_vl_7b_huihui_abliterated_bf16.safetensors"}
    if encoder not in encoders: raise ValueError("unsupported Qwen encoder")
    models = {"fp8": "qwen_image_edit_2511_fp8mixed.safetensors", "bf16": "qwen_image_edit_2511_bf16.safetensors"}
    if model_precision not in models: raise ValueError("unsupported Qwen precision")
    if type(batch_count) is not int or batch_count < 1: raise ValueError("batch_count must be a positive integer")
    graph = {
        '1': {'class_type': 'UNETLoader', 'inputs': {'unet_name': models[model_precision], 'weight_dtype': 'default'}},
        '2': {'class_type': 'CLIPLoader', 'inputs': {'clip_name': encoders[encoder], 'type': 'qwen_image', 'device': 'default'}},
        '3': {'class_type': 'VAELoader', 'inputs': {'vae_name': 'qwen_image_vae.safetensors'}},
        '4': {'class_type': 'ModelSamplingAuraFlow', 'inputs': {'model': ['1', 0], 'shift': 3.1}},
        '5': {'class_type': 'CFGNorm', 'inputs': {'model': ['4', 0], 'strength': 1.0}},
        '6': {'class_type': 'FluxKontextImageScale', 'inputs': {'image': ['20', 0]}},
        '7': {'class_type': 'TextEncodeQwenImageEditPlus', 'inputs': {'clip': ['2', 0], 'vae': ['3', 0], 'image1': ['6', 0], 'prompt': prompt}},
        '8': {'class_type': 'TextEncodeQwenImageEditPlus', 'inputs': {'clip': ['2', 0], 'vae': ['3', 0], 'image1': ['6', 0], 'prompt': ''}},
        '9': {'class_type': 'FluxKontextMultiReferenceLatentMethod', 'inputs': {'conditioning': ['7', 0], 'reference_latents_method': 'index_timestep_zero'}},
        '10': {'class_type': 'FluxKontextMultiReferenceLatentMethod', 'inputs': {'conditioning': ['8', 0], 'reference_latents_method': 'index_timestep_zero'}},
        '11': {'class_type': 'VAEEncode', 'inputs': {'pixels': ['6', 0], 'vae': ['3', 0]}},
        '12': {'class_type': 'KSampler', 'inputs': {'model': ['5', 0], 'positive': ['9', 0], 'negative': ['10', 0], 'latent_image': ['11', 0], 'seed': seed, 'steps': steps, 'cfg': 4.0, 'sampler_name': 'euler', 'scheduler': 'simple', 'denoise': 1.0}},
        '13': {'class_type': 'VAEDecode', 'inputs': {'samples': ['12', 0], 'vae': ['3', 0]}},
        '14': {'class_type': 'SaveImage', 'inputs': {'images': ['13', 0], 'filename_prefix': 'dgx-qwen-edit'}},
    }
    for index, filename in enumerate(image_filenames):
        node_id = str(20 + index)
        graph[node_id] = {'class_type': 'LoadImage', 'inputs': {'image': filename}}
        if index:
            for encoder in ('7', '8'):
                graph[encoder]['inputs'][f'image{index + 1}'] = [node_id, 0]
    return batch_graph(graph, "12", "13", "14", batch_count, parallelism, candidate_indices)


def validate_edit_parameters(parameters):
    """Validate model-specific fields before creating or dispatching a task."""
    model = parameters.get('model', 'qwen-image-edit-2511')
    if model == 'llada-image-turbo':
        if len(parameters['asset_ids']) != 1:
            raise ValueError('LLaDA reference editing requires exactly one complete reference image; use Qwen for multiple references')
        if any(k in parameters for k in ('encoder', 'model_precision')):
            raise ValueError('Qwen encoder/precision fields cannot be used with LLaDA')
        if parameters.get('steps', 4) != 4:
            raise ValueError('LLaDA Turbo editing requires exactly 4 steps')
        _new_image_inputs(parameters['prompt'], parameters.get('width', 1024), parameters.get('height', 1024))
    elif model == 'qwen-image-edit-2511':
        if len(parameters['asset_ids']) > 3:
            raise ValueError('Qwen editing accepts at most 3 reference images')
        if any(k in parameters for k in ('width', 'height')):
            raise ValueError('Qwen editing derives dimensions from its first reference image')
        if not 20 <= parameters.get('steps', 40) <= 40:
            raise ValueError('Qwen editing requires 20–40 steps')
    elif model == FLUX2_EDIT_MODEL:
        if not 1 <= len(parameters['asset_ids']) <= FLUX2_REFERENCE_LIMIT:
            raise ValueError(f'FLUX.2 editing accepts 1–{FLUX2_REFERENCE_LIMIT} reference images')
        if any(k in parameters for k in ('width', 'height', 'encoder', 'model_precision')):
            raise ValueError('FLUX.2 editing derives size from image 1 and does not use Qwen encoder fields')
        if parameters.get('steps', 20) != 20:
            raise ValueError('FLUX.2 editing uses the pinned 20-step schedule')
    else:
        raise ValueError('unsupported reference editing model')


def edit_image(parameters, image_filenames):
    validate_edit_parameters(parameters)
    p = parameters
    if len(image_filenames) != len(p['asset_ids']):
        raise ValueError('Reference filenames must match registered assets')
    if p.get('model') == 'llada-image-turbo':
        return llada_turbo_image(p['prompt'], p.get('width', 1024), p.get('height', 1024),
            p.get('seed', 42), p.get('batch_count', 1), p.get('parallelism', 2), p.get('candidate_indices'),
            source_filename=image_filenames[0])
    if p.get('model') == FLUX2_EDIT_MODEL:
        return flux2_image(p['prompt'], seed=p.get('seed', 42), batch_count=p.get('batch_count', 1),
            parallelism=p.get('parallelism', 2), candidate_indices=p.get('candidate_indices'),
            image_filenames=image_filenames, derive_size=True)
    return qwen_image_edit(p['prompt'], image_filenames, p.get('seed', 42), p.get('steps', 40),
        p.get('encoder', 'standard'), p.get('model_precision', 'fp8'), p.get('batch_count', 1),
        p.get('parallelism', 2), p.get('candidate_indices'))
