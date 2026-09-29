"""Build a reproducible native overlay; does not deploy or start services."""
from pathlib import Path
import hashlib
import json
import shutil

ROOT = Path(__file__).resolve().parents[2]
TASK = ROOT / "plans/h3-ring4-performance/pdd-easycache-20260929"
SOURCE = TASK / "source"
OUT = TASK / "overlay"
BASE = "runtime/pipelines_core/stages/model_specific_stages/minimax_h3/"


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f"Unexpected source anchor count for {old[:80]!r}")
    return text.replace(old, new)


def main():
    OUT.mkdir(exist_ok=True)
    entries = {}
    def write(name, text, target):
        (OUT / name).write_text(text)
        entries[name] = {"target": target, "sha256": hashlib.sha256(text.encode()).hexdigest()}
    for name in ("h3_pdd.py", "h3_easycache.py"):
        write(name, (Path(__file__).parent / name).read_text(), "runtime/utils/" + name)
    stage = (SOURCE / "denoising.py").read_text()
    stage = replace_once(stage, "            build_vsa_h3_step_metadata = _maybe_prepare_vsa_h3_step_metadata(",
        "            from sglang.multimodal_gen.runtime.utils.h3_pdd import attach_pdd\n"
        "            attach_pdd(model)\n"
        "            build_vsa_h3_step_metadata = _maybe_prepare_vsa_h3_step_metadata(")
    write("denoising.py", stage, BASE + "stages/denoising.py")
    timestep = (SOURCE / (BASE + "stages/timestep_preparation.py")).read_text()
    timestep = replace_once(timestep, '        sigmas: dict[str, list[float]] = {}',
        '        from sglang.multimodal_gen.runtime.utils.h3_pdd import prepare_pdd_schedule\n'
        '        pdd_sigmas = prepare_pdd_schedule(batch, scales)\n'
        '        if pdd_sigmas is not None:\n'
        '            batch.extra[MINIMAX_H3_SIGMAS_EXTRA_KEY] = pdd_sigmas\n'
        '            return\n'
        '        sigmas: dict[str, list[float]] = {}')
    write("timestep_preparation.py", timestep, BASE + "stages/timestep_preparation.py")
    loop = (SOURCE / "denoise_loop.py").read_text()
    loop = replace_once(loop, '    for step in range(num_steps):',
        '    from sglang.multimodal_gen.runtime.utils.h3_easycache import EasyCache\n'
        '    import time\n'
        '    cache = EasyCache(num_steps, device)\n'
        '    pdd = getattr(model, "_h3_pdd", None)\n'
        '    if pdd is not None and num_steps != 8:\n'
        '        raise ValueError("PDD must execute exactly eight intervals")\n'
        '    if cache.enabled and pdd is None:\n'
        '        raise ValueError("This EasyCache experiment requires PDD")\n'
        '    for step in range(num_steps):')
    loop = replace_once(loop, '            s_v = sigmas_video[step]',
        '            torch.cuda.synchronize(device) if device.type == "cuda" else None\n'
        '            step_started = time.monotonic()\n'
        '            if pdd is not None:\n'
        '                pdd.arm(step)\n'
        '            s_v = sigmas_video[step]')
    old = '''                if model_forward is None:
                    v_video, v_audio = model(**fk)
                else:
                    v_video, v_audio = model_forward(model, fk, step)'''
    new = '''                video_target = video_rows[video_target_slice]
                skipped = cache.skip(step, video_target)
                if skipped:
                    v_video, v_audio = cache.reuse(video_target, audio_rows)
                else:
                    if model_forward is None:
                        v_video, v_audio = model(**fk)
                    else:
                        v_video, v_audio = model_forward(model, fk, step)
                    cache.remember(video_target, audio_rows, v_video, v_audio)'''
    loop = replace_once(loop, old, new)
    loop = replace_once(loop, '            if on_step is not None:',
        '            torch.cuda.synchronize(device) if device.type == "cuda" else None\n'
        '            cache.log_step(step, skipped, time.monotonic() - step_started)\n'
        '            if on_step is not None:')
    loop = replace_once(loop, '    return video_rows, audio_rows',
        '    cache.finish()\n    return video_rows, audio_rows')
    write("denoise_loop.py", loop, BASE + "denoise_loop.py")
    (OUT / "manifest.json").write_text(json.dumps({"source_commit": "e970453b433e1467e22f6d925f270563417540b5", "contract": "h3-pdd-easycache-v1", "files": entries}, indent=2) + "\n")
    renderer = (SOURCE / "profile_node.py").read_text()
    renderer = replace_once(renderer, '    identity = {"h3_performance": performance_manifest,', '''    pdd_root = KIT / "patches" / "pdd-easycache"
    pdd_config_path = pdd_root / "config.json"
    pdd_config = load(pdd_config_path) if pdd_config_path.is_file() else {"enabled": False}
    pdd_manifest = None
    pdd_targets = set()
    if pdd_config.get("enabled"):
        if p["id"] != "ref2va-official" or world != 4:
            raise ValueError("PDD experiment requires official Ref2VA Ring4")
        if type(pdd_config.get("easycache")) is not bool:
            raise ValueError("PDD EasyCache switch must be boolean")
        pdd_manifest = load(pdd_root / "manifest.json")
        if pdd_manifest["source_commit"] != c["source_commit"]:
            raise ValueError("PDD source version mismatch")
        for rel, entry in pdd_manifest["files"].items():
            if hashlib.sha256((pdd_root / rel).read_bytes()).hexdigest() != entry["sha256"]:
                raise ValueError(f"PDD overlay checksum mismatch: {rel}")
            pdd_targets.add(entry["target"])
        pdd_weight = KIT / "weights" / "MiniMax-H3-Ref2VA-Acc-8Step.safetensors"
        if pdd_weight.stat().st_size != 1372450680:
            raise ValueError("PDD weight missing or incomplete")
    identity = {"h3_pdd": {"config": pdd_config, "manifest": pdd_manifest}, "h3_performance": performance_manifest,''')
    renderer = replace_once(renderer, '            mount(performance_root / rel, "/opt/sglang-src/python/sglang/multimodal_gen/" + entry["target"])',
        '            if entry["target"] not in pdd_targets:\n'
        '                mount(performance_root / rel, "/opt/sglang-src/python/sglang/multimodal_gen/" + entry["target"])\n'
        '    if pdd_manifest:\n'
        '        for rel, entry in pdd_manifest["files"].items():\n'
        '            mount(pdd_root / rel, "/opt/sglang-src/python/sglang/multimodal_gen/" + entry["target"])\n'
        '        mount(pdd_weight, "/models/h3-pdd.safetensors")\n'
        '        args.extend(["-e", "H3_PDD_PATH=/models/h3-pdd.safetensors", "-e", "H3_EASYCACHE=" + ("1" if pdd_config["easycache"] else "0"), "-e", "H3_EASYCACHE_THRESHOLD=0.2"])')
    (TASK / "profile_node.py").write_text(renderer)
    print(json.dumps({"output": str(OUT), "files": list(entries)}))


if __name__ == "__main__":
    main()
