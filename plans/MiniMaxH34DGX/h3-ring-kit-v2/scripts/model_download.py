#!/usr/bin/env python3
"""Plan first; pin a Hub commit; download only the requested original partition."""
import argparse
import datetime
import fnmatch
import json
import re
from pathlib import Path

REPO = "MiniMaxAI/MiniMax-H3"


def main() -> None:
    from huggingface_hub import HfApi, snapshot_download
    p = argparse.ArgumentParser()
    p.add_argument("--dest", required=True, type=Path)
    p.add_argument("--lock", required=True, type=Path)
    p.add_argument("--variant", choices=["FL2VA", "Ref2VA"], default="FL2VA")
    p.add_argument("--revision", help="Optional exact Hub commit, not a mutable branch")
    p.add_argument("--download", action="store_true")
    a = p.parse_args()
    if a.revision and not re.fullmatch(r"[0-9a-fA-F]{40}", a.revision):
        p.error("--revision must be an exact 40-character commit")
    patterns = ["model_index.json", f"{a.variant}/*", "LICENSE*", "README.md", ".gitattributes"]
    api = HfApi()
    if a.lock.exists():
        lock = json.loads(a.lock.read_text())
        if lock["repo"] != REPO or lock["variant"] != a.variant:
            raise RuntimeError("Existing lock belongs to a different model/variant")
        revision = lock["revision"]
        if a.revision and revision != a.revision:
            raise RuntimeError("Do not overwrite an existing revision lock")
        if lock["allow_patterns"] != patterns:
            raise RuntimeError("Existing download scope differs; use a separate reviewed lock")
    else:
        info = api.model_info(REPO, revision=a.revision or "main", files_metadata=True)
        revision = info.sha
        selected = [f for f in info.siblings if any(fnmatch.fnmatchcase(f.rfilename, pat) for pat in patterns)]
        names = {f.rfilename for f in selected}
        if "model_index.json" not in names:
            raise RuntimeError("Repository layout changed: missing public model_index.json")
        for component in ("transformer", "text_encoder", "video_vae", "audio_vae", "tokenizer", "processor"):
            if not any(n.startswith(f"{a.variant}/{component}/") for n in names):
                raise RuntimeError(f"Missing required component: {component}")
        if any(f.size is None for f in selected):
            raise RuntimeError("Hub did not return all file sizes; cannot establish a complete plan")
        lock = {"repo": REPO, "revision": revision, "variant": a.variant,
                "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "allow_patterns": patterns, "total_bytes": sum(f.size for f in selected),
                "files": [{"path": f.rfilename, "size": f.size} for f in selected]}
        a.lock.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive create prevents silent replacement of another plan.
        with a.lock.open("x", encoding="utf-8") as f:
            json.dump(lock, f, ensure_ascii=False, indent=2)
    print(json.dumps({"revision": revision, "variant": a.variant,
                      "files": len(lock["files"]), "scope_GiB": round(lock["total_bytes"] / 2**30, 2),
                      "dest": str(a.dest.resolve()), "download": a.download}, ensure_ascii=False, indent=2))
    if not a.download:
        return
    a.dest.mkdir(parents=True, exist_ok=True)
    snapshot_download(REPO, revision=revision, allow_patterns=patterns,
                      local_dir=str(a.dest), max_workers=4)
    for item in lock["files"]:
        path = a.dest / item["path"]
        if not path.is_file() or path.stat().st_size != item["size"]:
            raise RuntimeError(f"Downloaded file missing or size mismatch: {item['path']}")
    print("PASS_DOWNLOAD_SIZE_CHECK; run manifest.py for your cross-node SHA256 inventory")


if __name__ == "__main__":
    main()
