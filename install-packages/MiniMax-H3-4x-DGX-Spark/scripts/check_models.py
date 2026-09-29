#!/usr/bin/env python3
"""Verify locked file presence/size before launching. No automatic downloads."""
import argparse
import hashlib
import json
from pathlib import Path

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--profile", choices=["q", "u-q", "p", "u-p"], required=True)
    p.add_argument("--sha256", action="store_true", help="Also verify the available Hub LFS hashes")
    a = p.parse_args()
    lock_path = a.root / "manifests/model-lock.json"
    lock = json.loads(lock_path.read_text())
    keys = ["base"]
    if a.profile.startswith("u-"):
        keys.append("heretic")
    if a.profile in {"p", "u-p"}:
        keys.append("turbo")
    for key in keys:
        record = lock["models"][key]
        base = a.root / "models" / record["directory"]
        for entry in record["files"]:
            path = base / entry["path"]
            if not path.is_file() or (entry["size"] is not None and path.stat().st_size != entry["size"]):
                raise RuntimeError(f"Missing or incomplete model file: {path}")
            if a.sha256 and entry.get("sha256"):
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(16 * 1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != entry["sha256"]:
                    raise RuntimeError(f"SHA256 mismatch: {path}")
    print("MODEL_LOCK_SHA256=" + hashlib.sha256(lock_path.read_bytes()).hexdigest())
    print(f"Verified file presence/size for {a.profile}; available LFS hashes checked={a.sha256}.")

if __name__ == "__main__":
    main()
