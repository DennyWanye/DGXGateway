#!/usr/bin/env python3
import argparse
import hashlib
import json
import pathlib

QUANTS = ("Q8_K_P", "Q6_K_P", "Q5_K_P", "Q4_K_P")
PREFIX = "Qwen3.8-27B-Uncensored-HauhauCS-Aggressive"

parser = argparse.ArgumentParser(description="Verify a GGUF against the signed release manifest")
parser.add_argument("--quant", choices=QUANTS, default="Q8_K_P")
parser.add_argument(
    "--root",
    type=pathlib.Path,
    default=pathlib.Path.home() / "dgx-models/qwen38-27b/models",
    help="directory containing the manifest and GGUF",
)
args = parser.parse_args()

root = args.root
name = f"{PREFIX}-{args.quant}.gguf"
manifest_path = root / "HauhauCS-RELEASE-MANIFEST.json"
model_path = root / name

manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
expected = manifest["artifacts"][name]
actual_size = model_path.stat().st_size
if actual_size != expected["bytes"]:
    raise SystemExit(f"size mismatch: expected {expected['bytes']}, got {actual_size}")

digest = hashlib.sha256()
with model_path.open("rb") as stream:
    for block in iter(lambda: stream.read(16 * 1024 * 1024), b""):
        digest.update(block)
actual_hash = digest.hexdigest()
if actual_hash != expected["sha256"]:
    raise SystemExit(f"sha256 mismatch: expected {expected['sha256']}, got {actual_hash}")

print(f"verified {name}")
print(f"bytes={actual_size}")
print(f"sha256={actual_hash}")
