#!/usr/bin/env python3
"""Pinned, scoped H3 downloads. Standard library except download/plan uses huggingface_hub.

Never replace an existing file with different bytes. Download manifests live outside model roots.
A receipt verifies bytes/provenance only; it is NOT a GPU compatibility or license approval.
"""
from __future__ import annotations
import argparse
import fnmatch
import hashlib
import json
import shutil
from pathlib import Path, PurePosixPath

ASSETS = {
    "official-fl": ("MiniMaxAI/MiniMax-H3", ["model_index.json", "FL2VA/*", "LICENSE*", "README.md"], "FL2VA"),
    "official-ref": ("MiniMaxAI/MiniMax-H3", ["model_index.json", "Ref2VA/*", "LICENSE*", "README.md"], "Ref2VA"),
    "encoder-heretic": ("llmfan46/Qwen3-VL-32B-Instruct-ultra-uncensored-heretic", ["config.json", "model*.safetensors", "model*.safetensors.index.json", "README.md", "LICENSE*"], None),
    "hybrid-singularity": ("WarmBloodAban/Minimax-h3_Singularity", ["Minimax-h3_Singularity_ref2va_v1.3_int8.safetensors", "README.md", "LICENSE*", "MiniMax_H3_Singularity_Prompt_Writing_Specification_Enhanced_EN.md"], None),
}
HYBRID_FILE = "Minimax-h3_Singularity_ref2va_v1.3_int8.safetensors"


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    p = PurePosixPath(relative)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise ValueError(f"Unsafe path: {relative!r}")
    return root / str(p)


def remote_hash(path: Path, kind: str) -> str:
    if kind == "sha256":
        return sha256(path)
    if kind != "git-sha1":
        raise ValueError("Unsupported upstream hash")
    h = hashlib.sha1()
    h.update(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as f:
        for b in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def matches(path: Path, spec: dict) -> bool:
    return path.is_file() and path.stat().st_size == spec["bytes"] and remote_hash(path, spec["hash_kind"]) == spec["upstream_hash"]


def write_once(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if json.loads(path.read_text()) != value:
            raise RuntimeError(f"Refusing to replace different manifest: {path}")
        return
    with path.open("x", encoding="utf-8") as f:
        json.dump(value, f, indent=2, ensure_ascii=False)
        f.write("\n")


def scope_validate(asset: str, files: list[dict]) -> None:
    names = {x["path"] for x in files}
    partition = ASSETS[asset][2]
    if partition:
        for needed in ("model_index.json", f"{partition}/model_index.json"):
            if needed not in names:
                raise RuntimeError(f"Missing official entry: {needed}")
        for component in ("transformer", "text_encoder", "video_vae", "audio_vae", "tokenizer", "processor"):
            if not any(x.startswith(f"{partition}/{component}/") for x in names):
                raise RuntimeError(f"Missing component: {component}; do not guess a different layout")
    elif asset == "encoder-heretic":
        if "config.json" not in names or not any(x.endswith(".safetensors") for x in names):
            raise RuntimeError("Missing BF16 encoder config/weights")
    elif HYBRID_FILE not in names:
        raise RuntimeError(f"The exact reviewed hybrid file is unavailable: {HYBRID_FILE}")


def verify_receipt(root: Path, receipt: dict, full_hash: bool = True) -> None:
    if not receipt.get("files"):
        raise RuntimeError("Empty receipt")
    for spec in receipt["files"]:
        p = safe_path(root, spec["path"])
        if not p.is_file() or p.stat().st_size != spec["bytes"]:
            raise RuntimeError(f"Missing/size mismatch: {p}")
        if full_hash and sha256(p) != spec["sha256"]:
            raise RuntimeError(f"SHA256 mismatch: {p}")


def make_plan(asset: str, revision: str | None) -> dict:
    from huggingface_hub import HfApi
    repo, patterns, _ = ASSETS[asset]
    info = HfApi().model_info(repo, revision=revision or "main", files_metadata=True)
    import re
    if not re.fullmatch(r"[0-9a-f]{40}", info.sha):
        raise RuntimeError("Hub did not return a full commit SHA")
    result = []
    for f in info.siblings:
        if not any(fnmatch.fnmatchcase(f.rfilename, p) for p in patterns):
            continue
        lfs = getattr(f, "lfs", None)
        digest = (lfs.get("sha256") if isinstance(lfs, dict) else getattr(lfs, "sha256", None)) if lfs else None
        kind = "sha256" if digest else "git-sha1"
        digest = digest or getattr(f, "blob_id", None)
        if f.size is None or not digest:
            raise RuntimeError(f"Missing upstream size/hash: {f.rfilename}")
        result.append({"path": f.rfilename, "bytes": f.size, "hash_kind": kind, "upstream_hash": digest})
    scope_validate(asset, result)
    return {"schema": 1, "asset": asset, "repo": repo, "revision": info.sha,
            "patterns": patterns, "files": sorted(result, key=lambda x: x["path"])}


def run_download(lock: dict, dest: Path, receipt_path: Path, reuse_official: bool) -> dict:
    from huggingface_hub import hf_hub_download
    dest.mkdir(parents=True, exist_ok=True)
    rows, counters = [], {"existing": 0, "local_copy": 0, "hub_fetch": 0}
    for spec in lock["files"]:
        p = safe_path(dest, spec["path"])
        if p.exists():
            if not matches(p, spec):
                raise RuntimeError(f"Existing bytes differ from pinned source: {p}. Keep this deployment; use a NEW snapshot directory.")
            counters["existing"] += 1
        else:
            reused = False
            # Same component name is not enough: require upstream content hash equality.
            if reuse_official and spec["path"].startswith("Ref2VA/"):
                candidate = dest / ("FL2VA/" + spec["path"].removeprefix("Ref2VA/"))
                if matches(candidate, spec):
                    p.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(candidate, p)  # Separate inode; never edit the official source.
                    reused = True
                    counters["local_copy"] += 1
            if not reused:
                hf_hub_download(lock["repo"], filename=spec["path"], revision=lock["revision"], local_dir=str(dest))
                counters["hub_fetch"] += 1
            if not matches(p, spec):
                raise RuntimeError(f"Downloaded/copied content failed upstream hash: {p}")
        rows.append({"path": spec["path"], "bytes": spec["bytes"], "sha256": sha256(p)})
        print(json.dumps({"verified_file": spec["path"]}, ensure_ascii=False), flush=True)
    receipt = {"schema": 1, "asset": lock["asset"], "repo": lock["repo"], "revision": lock["revision"],
               "lock_sha256": hashlib.sha256(canonical(lock)).hexdigest(), "files": rows}
    write_once(receipt_path, receipt)
    return counters


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)
    q = sub.add_parser("plan")
    q.add_argument("asset", choices=ASSETS)
    q.add_argument("--lock", type=Path, required=True)
    q.add_argument("--revision", help="Full SHA. Initial planning may resolve main once; download never uses main.")
    q.add_argument("--same-revision-as", type=Path, help="Existing official v1/v2 lock/receipt; requires exact official repo")
    q = sub.add_parser("download")
    q.add_argument("--lock", type=Path, required=True)
    q.add_argument("--dest", type=Path, required=True)
    q.add_argument("--receipt", type=Path, required=True)
    q.add_argument("--reuse-official", action="store_true")
    q = sub.add_parser("verify")
    q.add_argument("--dest", type=Path, required=True)
    q.add_argument("--receipt", type=Path, required=True)
    a = p.parse_args()
    if a.action == "plan":
        revision = a.revision
        if a.same_revision_as:
            old = json.loads(a.same_revision_as.read_text())
            if old["repo"] != "MiniMaxAI/MiniMax-H3" or not a.asset.startswith("official-"):
                raise RuntimeError("Revision reuse applies only to the same official H3 repository")
            if revision and revision != old["revision"]:
                raise RuntimeError("Conflicting revision arguments")
            revision = old["revision"]
        import re
        if revision and not re.fullmatch(r"[0-9a-f]{40}", revision):
            p.error("revision must be a full commit SHA")
        if a.lock.exists():
            lock = json.loads(a.lock.read_text())
            if lock["asset"] != a.asset or (revision and lock["revision"] != revision):
                raise RuntimeError("Existing lock identity mismatch")
        else:
            lock = make_plan(a.asset, revision)
            write_once(a.lock, lock)
        print(json.dumps({"asset": a.asset, "repo": lock["repo"], "revision": lock["revision"],
                          "files": len(lock["files"]), "scope_GiB": round(sum(x["bytes"] for x in lock["files"])/2**30, 2),
                          "note": "Logical selected size, not additional WAN traffic or free-space guarantee; no weights downloaded."}, ensure_ascii=False, indent=2))
    elif a.action == "download":
        lock = json.loads(a.lock.read_text())
        if lock["asset"] not in ASSETS or lock["repo"] != ASSETS[lock["asset"]][0]:
            raise RuntimeError("Unrecognized asset lock")
        if a.receipt.resolve().is_relative_to(a.dest.resolve()):
            raise RuntimeError("Keep receipts outside weights")
        import re
        if not re.fullmatch(r"[0-9a-f]{40}",lock.get("revision","")) or lock.get("patterns") != ASSETS[lock["asset"]][1]:
            raise RuntimeError("Unpinned or modified download scope")
        scope_validate(lock["asset"], lock["files"])
        print(json.dumps(run_download(lock, a.dest, a.receipt, a.reuse_official), indent=2))
    else:
        receipt = json.loads(a.receipt.read_text())
        verify_receipt(a.dest, receipt)
        print("PASS_ASSET_BYTES_ONLY")

if __name__ == "__main__":
    main()
