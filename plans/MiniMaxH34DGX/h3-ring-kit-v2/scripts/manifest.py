#!/usr/bin/env python3
"""Hash a read-only model snapshot; verify identical bytes on the other nodes."""
import argparse
import hashlib
import json
from pathlib import Path


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(16 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["make", "verify"])
    p.add_argument("root", type=Path)
    p.add_argument("manifest", type=Path)
    a = p.parse_args()
    root = a.root.resolve()
    if not root.is_dir():
        raise RuntimeError("Model root is missing")
    if a.manifest.resolve().is_relative_to(root):
        raise RuntimeError("Store the manifest outside the model directory")
    paths = sorted(p for p in root.rglob("*") if p.is_file() and ".cache" not in p.relative_to(root).parts)
    if not paths:
        raise RuntimeError("Refusing an empty model manifest")
    actual = {str(p.relative_to(root)): {"bytes": p.stat().st_size, "sha256": digest(p)} for p in paths}
    if a.mode == "make":
        a.manifest.parent.mkdir(parents=True, exist_ok=True)
        with a.manifest.open("x", encoding="utf-8") as f:
            json.dump(actual, f, indent=2)
    else:
        expected = json.loads(a.manifest.read_text())
        if actual != expected:
            keys = set(actual) | set(expected)
            bad = sorted(k for k in keys if actual.get(k) != expected.get(k))
            raise RuntimeError(f"Manifest mismatch ({len(bad)} files): {bad[:20]}")
    print(json.dumps({"status": "PASS", "mode": a.mode, "files": len(actual)}))


if __name__ == "__main__":
    main()
