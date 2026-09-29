#!/usr/bin/env python3
"""CPU-only numerical/negative tests of the optional loader in the target image."""
import argparse
import json
from pathlib import Path
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-dir', required=True, type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source_dir.resolve()))
    import torch
    from safetensors.torch import save_file
    from longcat_video.modules.quantization import quantize_model, QuantizedLinear
    from meta_loader import build_quantized_meta, load_shards_into_meta

    class Tiny(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.conv = torch.nn.Conv3d(2, 4, 1)
            self.linear = torch.nn.Linear(4, 8)
            self.final_layer = torch.nn.Module()
            self.final_layer.linear = torch.nn.Linear(8, 3)
            self.norm = torch.nn.LayerNorm(3)

        def forward(self, x):
            x = self.conv(x).flatten(2).transpose(1, 2)
            return self.norm(self.final_layer.linear(self.linear(x)))

    torch.manual_seed(1949)
    baseline = quantize_model(Tiny()).eval()
    for module in baseline.modules():
        if not isinstance(module, QuantizedLinear):
            for parameter in module.parameters(recurse=False):
                parameter.data = parameter.data.to(torch.bfloat16)
    original_state = baseline.state_dict()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        keys = list(original_state)
        parts = [keys[::2], keys[1::2]]
        index = {'weight_map': {}}
        for i, keys_part in enumerate(parts):
            name = f'shard-{i}.safetensors'
            save_file({key: original_state[key].contiguous() for key in keys_part}, str(root / name))
            index['weight_map'].update({key: name for key in keys_part})
        index_path = root / 'quantized_model.safetensors.index.json'
        index_path.write_text(json.dumps(index))
        candidate = load_shards_into_meta(build_quantized_meta(Tiny, {}), root)
        for key, value in candidate.state_dict().items():
            assert value.dtype == original_state[key].dtype, key
            torch.testing.assert_close(value, original_state[key], rtol=0, atol=0)
        x = torch.randn(1, 2, 2, 2, 2, dtype=torch.bfloat16)
        with torch.inference_mode():
            result, reference = candidate(x), baseline(x)
        assert torch.isfinite(result).all()
        torch.testing.assert_close(result, reference, rtol=0, atol=0)
        bad = dict(index['weight_map'])
        bad.pop(next(iter(bad)))
        index_path.write_text(json.dumps({'weight_map': bad}))
        try:
            load_shards_into_meta(build_quantized_meta(Tiny, {}), root)
        except ValueError:
            pass
        else:
            raise AssertionError('Missing-index-key gate did not fail')
    print(json.dumps({'status': 'PASS', 'scope': 'CPU toy INT8 roundtrip / exact state+dtypes / BF16 forward / missing-index-key rejection', 'max_abs_error': (result.float()-reference.float()).abs().max().item(), 'full_avatar_load_tested': False, 'torch': torch.__version__}, indent=2))


if __name__ == '__main__':
    main()
