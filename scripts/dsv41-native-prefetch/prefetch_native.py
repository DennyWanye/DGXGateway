#!/usr/bin/env python3
"""固定 revision 的预下载/校验；不启动模型、不删除旧文件。默认只审计。"""
from __future__ import annotations
import argparse
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import time
import urllib.request

REPO = "deepseek-ai/DeepSeek-V4.1-Flash"
REVISION = "fb2764a5cf321eaa5070ca8f9e892818f477c16d"
ENGRAM_FILES = {f"model-{i:05d}-of-00048.safetensors" for i in (47, 48)}
GIB = 2**30


def save_json(path: Path, data: object) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def network_gate() -> None:
    if os.environ.get("DIRECT_NETWORK_CONFIRMED") != "1":
        raise RuntimeError("先核实系统路由、透明代理和 Docker 出口，再设置 DIRECT_NETWORK_CONFIRMED=1。")
    proxies = [k for k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY") if os.environ.get(k)]
    if proxies:
        raise RuntimeError("当前仍设置代理变量：" + ", ".join(proxies))
    endpoint = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
    if endpoint != "https://huggingface.co":
        raise RuntimeError("HF_ENDPOINT 不是本方案固定的官方站点。")


def entries_from(raw: dict) -> list[dict]:
    if raw.get("sha") != REVISION:
        raise RuntimeError("远端/保存清单 revision 不一致。")
    entries = []
    seen = set()
    for row in raw["siblings"]:
        name = row["rfilename"]
        p = PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts or not p.parts or p.parts[0] == ".cache" or str(p) != name:
            raise RuntimeError(f"不安全的仓库路径：{name!r}")
        if name in seen:
            raise RuntimeError(f"重复条目：{name}")
        seen.add(name)
        lfs = row.get("lfs") or {}
        size = row.get("size", lfs.get("size"))
        digest = lfs.get("sha256") or row.get("blobId")
        algo = "sha256" if lfs else "git-sha1"
        if not isinstance(size, int) or size < 0 or not isinstance(digest, str):
            raise RuntimeError(f"远端缺少 size/hash 元数据：{name}")
        if not re.fullmatch(r"[0-9a-f]{64}" if lfs else r"[0-9a-f]{40}", digest):
            raise RuntimeError(f"无效的哈希：{name}")
        entries.append({"name": name, "size": size, "hash": digest, "algo": algo})
    shards = {e["name"] for e in entries if re.fullmatch(r"model-\d{5}-of-00048\.safetensors", e["name"])}
    expected = {f"model-{i:05d}-of-00048.safetensors" for i in range(1, 49)}
    if shards != expected:
        raise RuntimeError("该 revision 不符合已审阅的 48 分片布局；停止，不套用旧方案。")
    if not {"config.json", "model.safetensors.index.json"}.issubset(seen):
        raise RuntimeError("缺少配置或权重索引。")
    return sorted(entries, key=lambda e: e["name"])


def destination(root: Path, name: str) -> Path:
    p = root / name
    for part in [p, *p.parents]:
        if part == root:
            break
        if part.is_symlink():
            raise RuntimeError(f"新 checkpoint 内不接受符号链接：{part}")
    if not p.resolve().is_relative_to(root):
        raise RuntimeError(f"目标越出 checkpoint：{p}")
    return p


def identity(path: Path) -> tuple[int, int, int, int]:
    s = path.stat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns


def matches(path: Path, entry: dict) -> bool:
    if not path.is_file() or path.stat().st_size != entry["size"]:
        return False
    before = identity(path)
    h = hashlib.sha256() if entry["algo"] == "sha256" else hashlib.sha1()
    if entry["algo"] == "git-sha1":
        h.update(f"blob {entry['size']}\0".encode())
    with path.open("rb") as f:
        while chunk := f.read(8 * 1024 * 1024):
            h.update(chunk)
    if identity(path) != before:
        raise RuntimeError(f"校验期间文件被修改：{path}")
    return h.hexdigest() == entry["hash"]


def reuse(src: Path, dst: Path, entry: dict, reserve: int) -> str:
    # src 的真实内容已经和目标 revision 哈希匹配；新路径不使用软链接。
    src = src.resolve(strict=True)
    if not matches(src, entry):
        raise RuntimeError(f"复用前再次校验失败：{src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        raise RuntimeError(f"拒绝覆盖已有目标：{dst}")
    try:
        os.link(src, dst)
        return "hardlink"
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    if shutil.disk_usage(dst.parent).free < entry["size"] + reserve:
        raise RuntimeError("跨文件系统复制 Engram 的可用空间不足。")
    tmp = dst.with_name(dst.name + ".reuse-partial")
    if tmp.exists():
        raise RuntimeError(f"存在上次复制临时文件，请人工核查：{tmp}")
    with src.open("rb") as inp, tmp.open("xb") as out:
        shutil.copyfileobj(inp, out, 8 * 1024 * 1024)
        out.flush()
        os.fsync(out.fileno())
    if not matches(tmp, entry):
        raise RuntimeError(f"本地复制后哈希不符，保留临时文件供排查：{tmp}")
    os.link(tmp, dst)  # 同一目标文件系统；目标已存在则报错，绝不覆盖。
    tmp.unlink()
    return "local-copy"


def verify_layout(root: Path, entries: list[dict]) -> None:
    index = json.loads((root / "model.safetensors.index.json").read_text())
    names = {e["name"] for e in entries}
    for name in set(index["weight_map"].values()):
        if name not in names or not destination(root, name).is_file():
            raise RuntimeError(f"索引指向未下载/未校验分片：{name}")
    config = json.loads((root / "config.json").read_text())
    if config.get("quantization_config", {}).get("quant_method") == "exl3":
        raise RuntimeError("目标目录仍是 EXL3，不是原生 checkpoint。")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model-dir", required=True, type=Path)
    ap.add_argument("--state-dir", required=True, type=Path)
    ap.add_argument("--source-engram", type=Path)
    modes = ap.add_mutually_exclusive_group()
    modes.add_argument("--apply", action="store_true", help="复用已校验文件，并下载缺失文件")
    modes.add_argument("--offline-verify", action="store_true", help="仅用保存清单逐文件校验，不访问网络")
    ap.add_argument("--reserve-gib", type=int, default=30)
    args = ap.parse_args()
    if args.reserve_gib < 0:
        ap.error("--reserve-gib 不能为负数")
    root = args.model_dir.expanduser().resolve()
    state = args.state_dir.expanduser().resolve()
    source = args.source_engram.expanduser().resolve() if args.source_engram else None
    if root == state or root.is_relative_to(state) or state.is_relative_to(root):
        raise RuntimeError("state-dir 与 model-dir 必须是独立目录，不能互相包含。")
    if source and (source == root or source.is_relative_to(root) or root.is_relative_to(source)):
        raise RuntimeError("旧 Engram 来源不能与新 checkpoint 重叠。")
    root.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    lock = (state / "prefetch.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    receipt = state / "checkpoint-verified.json"
    # 清除的仅是本工具先前的成功标记，绝不删除用户模型文件。
    if receipt.exists():
        receipt.unlink()
    metadata = state / "hf-metadata.json"
    if args.offline_verify:
        raw = json.loads(metadata.read_text())
    else:
        network_gate()
        headers = {"User-Agent": "dsv41-migration-prefetch/1"}
        if token := os.environ.get("HF_TOKEN"):
            headers["Authorization"] = "Bearer " + token
        url = f"https://huggingface.co/api/models/{REPO}/revision/{REVISION}?blobs=true"
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=90) as r:
            raw = json.load(r)
        entries_from(raw)
        save_json(metadata, raw)
    entries = entries_from(raw)
    tasks = []
    for e in entries:
        dst = destination(root, e["name"])
        if dst.exists():
            if not matches(dst, e):
                raise RuntimeError(f"目标已有文件但校验失败，不自动覆盖：{dst}")
            print("OK", e["name"], flush=True)
            continue
        if args.offline_verify:
            raise RuntimeError(f"离线校验发现缺失文件：{dst}")
        src = source / e["name"] if source and e["name"] in ENGRAM_FILES else None
        if src and matches(src, e):
            print("REUSE", e["name"], flush=True)
            tasks.append((e, src))
        else:
            if src and src.exists():
                print("旧文件不匹配目标 revision；保留旧文件，计划下载新文件：", e["name"], flush=True)
            tasks.append((e, None))
    total = sum(e["size"] for e in entries)
    net = sum(e["size"] for e, src in tasks if src is None)
    reusable = sum(e["size"] for e, src in tasks if src is not None)
    disk = sum(e["size"] for e, src in tasks if src is None or src.stat().st_dev != root.stat().st_dev)
    report = {"repo": REPO, "revision": REVISION, "model_dir": str(root),
              "total_bytes": total, "remaining_content_bytes": net,
              "reusable_bytes": reusable, "additional_file_bytes": disk,
              "note": "流量不含元数据、协议开销与重试；剩余内容大小不是实际网卡流量。",
              "files": [{"name": e["name"], "size": e["size"], "source": str(src) if src else "HuggingFace"} for e, src in tasks]}
    save_json(state / "download-plan.json", report)
    print(f"总计 {total/GIB:.2f} GiB；待下载内容 {net/GIB:.2f} GiB；可本地复用 {reusable/GIB:.2f} GiB；新增文件空间约 {disk/GIB:.2f} GiB", flush=True)
    if args.apply:
        reserve = args.reserve_gib * GIB
        if shutil.disk_usage(root).free < disk + reserve:
            raise RuntimeError("空间不足：先按文档迁出/清理确认不再依赖的旧文件，不继续下载。")
        for e, src in tasks:
            dst = destination(root, e["name"])
            if src:
                print(reuse(src, dst, e, reserve), e["name"], flush=True)
            else:
                from huggingface_hub import hf_hub_download
                print("DOWNLOAD", e["name"], flush=True)
                downloaded = Path(hf_hub_download(REPO, e["name"], revision=REVISION, local_dir=str(root)))
                if downloaded.resolve() != dst.resolve() or not matches(dst, e):
                    raise RuntimeError(f"下载返回路径或哈希不符：{e['name']}")
    elif tasks:
        print("仅审计；没有下载模型权重，也没有修改旧文件。加 --apply 才执行。")
        return 0
    verify_layout(root, entries)
    save_json(receipt, {"repo": REPO, "revision": REVISION, "verified_at": time.time(),
                       "model_dir": str(root), "files": entries,
                       "warning": "此标记不证明文件以后未被修改；删除旧数据前再执行 --offline-verify。"})
    print("VERIFIED", receipt)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Exception, KeyboardInterrupt) as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        raise SystemExit(1)
