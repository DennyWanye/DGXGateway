"""Opt-in low-peak candidate; validate on pinned runtime before actual model use.

Avoid constructing full FP32 storage and then allocating INT8 replacement buffers.
All state keys/shapes/dtypes are checked. This never calls to_empty(), which would
leave non-checkpoint state uninitialized. RoPE caches in this source are lazy.
"""
import json
from pathlib import Path
import torch
from safetensors import safe_open
from safetensors.torch import load_file
from longcat_video.modules.quantization import QuantizedLinear, DEFAULT_SKIP_PATTERNS


def build_quantized_meta(model_class, config):
    with torch.device("meta"):
        model = model_class(**config)
        replacements = []
        for name, module in model.named_modules():
            if isinstance(module, torch.nn.Linear) and not any(p in name for p in DEFAULT_SKIP_PATTERNS):
                replacements.append((name, QuantizedLinear(module.in_features, module.out_features, module.bias is not None)))
        for name, replacement in replacements:
            parent_name, _, leaf = name.rpartition(".")
            parent = model.get_submodule(parent_name) if parent_name else model
            setattr(parent, leaf, replacement)
    # The audited DiT has no nonpersistent initialization buffers. Fail closed if this changes.
    for name, module in model.named_modules():
        if module._non_persistent_buffers_set:
            raise ValueError(f"Unspecified nonpersistent buffers: {name}")
        for attr, value in vars(module).items():
            if isinstance(value, torch.Tensor) and value.is_meta:
                raise ValueError(f"Unregistered meta tensor: {name}.{attr}")
    return model


def load_shards_into_meta(model, quantized_dir):
    root = Path(quantized_dir).resolve()
    index = json.loads((root / "quantized_model.safetensors.index.json").read_text())
    weight_map = index["weight_map"]
    expected = model.state_dict()
    if set(weight_map) != set(expected):
        raise ValueError("Checkpoint index keys differ from model state keys")
    shard_names = sorted(set(weight_map.values()))
    if not shard_names:
        raise ValueError("No indexed shards")
    # Header preflight avoids materializing the model until the entire closure is consistent.
    observed = set()
    for name in shard_names:
        if Path(name).name != name or not name.endswith(".safetensors"):
            raise ValueError("Shard must be a plain safetensors basename")
        path = (root / name).resolve(strict=True)
        if path.parent != root:
            raise ValueError("Shard escapes the model directory")
        with safe_open(path, framework="pt", device="cpu") as stream:
            for key in stream.keys():
                if key in observed or weight_map.get(key) != name:
                    raise ValueError(f"Unexpected or duplicate shard key: {key}")
                observed.add(key)
                tensor_slice = stream.get_slice(key)
                if list(expected[key].shape) != tensor_slice.get_shape():
                    raise ValueError(f"Shape mismatch: {key}")
                actual_dtype = tensor_slice.get_dtype()
                if key.endswith("weight_int8"):
                    allowed = {"I8"}
                elif key.endswith("weight_scale"):
                    allowed = {"F32"}
                else:
                    allowed = {"BF16", "F32"}
                if actual_dtype not in allowed:
                    raise ValueError(f"Unexpected checkpoint dtype for {key}: {actual_dtype}")
    if observed != set(expected):
        raise ValueError("Some indexed tensors were absent from shard headers")
    del expected
    for name in shard_names:
        state = load_file(str(root / name), device="cpu")
        # Match upstream final parameter BF16 cast without making full FP32 buffers.
        for key in state:
            if not key.endswith(("weight_int8", "weight_scale")):
                state[key] = state[key].to(torch.bfloat16)
        result = model.load_state_dict(state, strict=False, assign=True)
        if result.unexpected_keys:
            raise ValueError(f"Unexpected keys while assigning shard: {result.unexpected_keys}")
        del state
    for name, value in list(model.named_parameters()) + list(model.named_buffers()):
        if value.is_meta:
            raise ValueError(f"Unmaterialized tensor after load: {name}")
        if name.endswith("weight_int8") and value.dtype != torch.int8:
            raise ValueError(f"Wrong INT8 buffer dtype: {name}")
        if name.endswith("weight_scale") and value.dtype != torch.float32:
            raise ValueError(f"Wrong scale buffer dtype: {name}")
    return model.eval()


def load_quantized_dit(checkpoint_dir, subfolder="base_model_int8", **kwargs):
    from longcat_video.modules.avatar.longcat_video_dit_avatar import LongCatVideoAvatarTransformer3DModel
    root = Path(checkpoint_dir) / subfolder
    config = json.loads((root / "config.json").read_text())
    for key in ("_class_name", "architectures", "_diffusers_version", "model_max_length"):
        config.pop(key, None)
    config.update(kwargs)
    model = build_quantized_meta(LongCatVideoAvatarTransformer3DModel, config)
    return load_shards_into_meta(model, root)
