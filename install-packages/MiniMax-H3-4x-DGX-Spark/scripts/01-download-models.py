#!/usr/bin/env python3
"""Pin exact Hub revisions and download only FL2VA, one Heretic encoder, one Turbo.
Default is metadata-only planning. --download explicitly downloads selected weights.
The first run writes model-lock.json; later runs reuse its immutable revisions.
"""
from __future__ import annotations
import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path
from typing import Any

SPECS = {
    "base": {"repo": "MiniMaxAI/MiniMax-H3", "directory": "MiniMax-H3",
             "patterns": ["model_index.json", "FL2VA/*", "LICENSE*", "README*"]},
    "heretic": {"repo": "llmfan46/Qwen3-VL-32B-Instruct-ultra-uncensored-heretic",
                "directory": "H3-Heretic-Encoder", "patterns": None},
    "turbo": {"repo": "larryvrh/MiniMax-H3-Turbo-Lora", "directory": "H3-Turbo-v4",
              "patterns": ["minimax_h3_turbo_v4_step600_ema.safetensors",
                           "adapter_config.json", "README*", "LICENSE*"]},
}

def atomic_json(path: Path, value: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)

def wanted(key: str, name: str) -> bool:
    if Path(name).is_absolute() or ".." in Path(name).parts:
        raise ValueError(f"Unsafe Hub filename: {name}")
    if key == "heretic":
        # Full Transformers-format checkpoint, not optimizer/training subdirectories.
        return "/" not in name and (name.endswith((".json", ".safetensors", ".model", ".txt", ".jinja"))
                                    or name.startswith(("LICENSE", "README")))
    return any(fnmatch.fnmatchcase(name, p) for p in SPECS[key]["patterns"])

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(16 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/srv/h3"))
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--base-only", action="store_true", help="Stage only the original baseline now")
    parser.add_argument("--verify-sha256", action="store_true", help="Read downloaded LFS files and verify Hub hashes")
    args = parser.parse_args()
    from huggingface_hub import HfApi, snapshot_download
    root = args.root.resolve()
    manifests = root / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    lock_path = manifests / "model-lock.json"
    lock = json.loads(lock_path.read_text()) if lock_path.exists() else {"schema": 1, "models": {}}
    api = HfApi(token=os.environ.get("HF_TOKEN"))
    keys = ["base"] if args.base_only else list(SPECS)
    for key in keys:
        spec = SPECS[key]
        existing = lock["models"].get(key)
        if existing and existing["repo"] != spec["repo"]:
            raise RuntimeError(f"Unexpected existing repository in lock: {key}")
        if existing:
            record = existing
        else:
            info = api.model_info(spec["repo"], files_metadata=True)
            if not info.sha:
                raise RuntimeError(f"Cannot resolve immutable revision for {spec['repo']}")
            files = []
            for f in info.siblings or []:
                if not wanted(key, f.rfilename):
                    continue
                lfs = getattr(f, "lfs", None)
                digest = (lfs.get("sha256") if isinstance(lfs, dict)
                          else getattr(lfs, "sha256", None)) if lfs else None
                files.append({"path": f.rfilename, "size": f.size, "sha256": digest})
            if not files or not any(f["path"].endswith(".safetensors") for f in files):
                raise RuntimeError(f"No checkpoint files matched {key}; inspect the upstream layout")
            if key == "base" and not any(f["path"] == "model_index.json" for f in files):
                raise RuntimeError("Official root model_index.json is missing")
            record = {"repo": spec["repo"], "revision": info.sha,
                      "directory": spec["directory"], "files": files}
            lock["models"][key] = record
            atomic_json(lock_path, lock)
        known = sum(f["size"] or 0 for f in record["files"])
        unknown = sum(f["size"] is None for f in record["files"])
        print(f"{key}: {record['repo']} @ {record['revision']}\n"
              f"  {len(record['files'])} files, {known / 2**30:.2f} GiB known; unknown sizes={unknown}", flush=True)
        if not args.download:
            continue
        destination = root / "models" / record["directory"]
        snapshot_download(repo_id=record["repo"], revision=record["revision"],
                          local_dir=str(destination),
                          allow_patterns=[f["path"] for f in record["files"]],
                          token=os.environ.get("HF_TOKEN"), max_workers=4)
        for item in record["files"]:
            path = destination / item["path"]
            if not path.is_file():
                raise RuntimeError(f"Missing downloaded file: {path}")
            if item["size"] is not None and path.stat().st_size != item["size"]:
                raise RuntimeError(f"Size mismatch: {path}")
            if args.verify_sha256 and item["sha256"] and sha256(path) != item["sha256"]:
                raise RuntimeError(f"SHA256 mismatch: {path}")
        print(f"  Verified presence/size{' and available LFS hashes' if args.verify_sha256 else ''}", flush=True)
    print(f"Revision lock: {lock_path}")
    print("No weights downloaded." if not args.download else "Selected downloads complete.")
    print("Keep this lock and identical model bytes on all nodes. Planning does not prove disk capacity or model compatibility.")

if __name__ == "__main__":
    main()
