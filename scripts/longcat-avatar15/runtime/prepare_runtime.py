#!/usr/bin/env python3
"""Generate an idempotent clean-speech entry, preserving pinned upstream files."""
import argparse
import ast
import difflib
import hashlib
import json
from pathlib import Path

SOURCE_COMMIT = "6b3f4b8582a8bc3f20f795735f5383716c4ba794"
ENTRY = "run_demo_avatar_single_audio_to_video.py"
SOURCE_HASHES = json.loads((Path(__file__).resolve().parent / "source-hashes.json").read_text())
OUTPUT = "run_avatar15_clean_speech.py"


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise ValueError(f"Patch anchor did not match exactly once: {old[:100]!r}")
    return source.replace(old, new, 1)


def transform(source):
    source = replace_once(source, "from audio_separator.separator import Separator", "from runtime_guards import require_dmd, require_clean_audio, verify_audio_unchanged, load_required_dmd")
    begin = source.index("def extract_vocal_from_speech(")
    end = source.index("def generate(args):", begin)
    source = source[:begin] + source[end:]
    source = replace_once(source, "    # set up default inference params", '''    if model_type != "avatar-v1.5" or not use_distill:
        raise ValueError("This audited entry requires avatar-v1.5 and --use_distill")
    # Verify before initializing CUDA or allocating models. No missing-adapter fallback.
    distill_checkpoint_path = require_dmd(checkpoint_dir)

    # set up default inference params''')
    source = replace_once(source, "    raw_speech_path = input_data['cond_audio']['person1']", "    raw_speech_path, raw_speech_sha256 = require_clean_audio(input_data['cond_audio']['person1'])")
    source = replace_once(source, "timeout=datetime.timedelta(seconds=3600*24)", "timeout=datetime.timedelta(seconds=600)")
    old = '''            distill_checkpoint_path = os.path.join(checkpoint_dir, 'lora', f'dmd_lora.safetensors')
            if os.path.exists(distill_checkpoint_path):
                dit.load_lora(distill_checkpoint_path, "dmd", multiplier=1.0, lora_network_dim=128, lora_network_alpha=64)
                dit.enable_loras(["dmd"])'''
    source = replace_once(source, old, '            load_required_dmd(dit, distill_checkpoint_path)')
    begin = source.index("    vocal_separator_path = ")
    end = source.index("    # initialize pipeline", begin)
    source = source[:begin] + source[end:]
    old = '''        # extract vocal
        temp_vocal_path = extract_vocal_from_speech(raw_speech_path, f"/tmp/temp_speech_{generate_random_uid()}_{global_rank}_vocal.wav", vocal_separator, audio_output_dir_temp)
        assert temp_vocal_path is not None and os.path.exists(temp_vocal_path), f"No vocal detected"    '''
    source = replace_once(source, old, "        # Clean speech is read directly; no separator or temporary input ownership.")
    source = replace_once(source, "librosa.load(temp_vocal_path, sr=16000)", "librosa.load(raw_speech_path, sr=16000)")
    source = replace_once(source, '''        if os.path.exists(temp_vocal_path):
            os.remove(temp_vocal_path)''', "        verify_audio_unchanged(raw_speech_path, raw_speech_sha256)")
    # Optional reviewed candidate; original loader remains default until candidate passes real validation.
    source = replace_once(source, "from longcat_video.modules.quantization import load_quantized_dit", '''from longcat_video.modules.quantization import load_quantized_dit
if os.environ.get("LONGCAT_META_LOAD") == "1":
    from meta_loader import load_quantized_dit''')
    ast.parse(source)
    return source


def generate(source_dir):
    source_dir = Path(source_dir)
    for name, expected in SOURCE_HASHES.items():
        actual = hashlib.sha256((source_dir / name).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"Pinned upstream identity mismatch: {name}")
    original = (source_dir / ENTRY).read_text()
    generated = transform(original)
    helper_dir = Path(__file__).resolve().parent
    outputs = {OUTPUT: generated.encode()}
    for name in ("runtime_guards.py", "meta_loader.py"):
        outputs[name] = (helper_dir / name).read_bytes()
    outputs["avatar15-clean-speech.patch"] = ''.join(difflib.unified_diff(original.splitlines(True), generated.splitlines(True), fromfile=ENTRY, tofile=OUTPUT)).encode()
    # Refuse to replace different pre-existing generated state; identical reapply is a no-op.
    for name, content in outputs.items():
        target = source_dir / name
        if target.exists() and target.read_bytes() != content:
            raise ValueError(f"Refusing to overwrite different generated file: {target}")
    for name, content in outputs.items():
        target = source_dir / name
        if not target.exists():
            with target.open("xb") as stream:
                stream.write(content)
    return {"source_commit": SOURCE_COMMIT, "original_hashes": SOURCE_HASHES,
            "generated_hashes": {name: hashlib.sha256(content).hexdigest() for name, content in outputs.items()}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source_dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(generate(args.source_dir), indent=2))
