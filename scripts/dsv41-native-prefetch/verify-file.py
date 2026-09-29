#!/usr/bin/env python3
"""Single-file size and digest check. Digest is sha256 hex or gitsha1:<blob>."""
import argparse
import hashlib
import os
from pathlib import Path


def verify_file(path: str, size: int, digest: str) -> bool:
    path = Path(path).absolute()
    if any(p.is_symlink() for p in [path, *path.parents]) or not path.is_file() or path.stat().st_size != size:
        return False
    git = digest.startswith("gitsha1:")
    expected = digest.removeprefix("gitsha1:")
    h = hashlib.sha1() if git else hashlib.sha256()
    if git:
        h.update(f"blob {size}\0".encode())
    with path.open("rb") as src:
        for chunk in iter(lambda: src.read(8 * 1024 * 1024), b""):
            h.update(chunk)
            if hasattr(os, "posix_fadvise"):
                os.posix_fadvise(src.fileno(), src.tell() - len(chunk), len(chunk), os.POSIX_FADV_DONTNEED)
    return h.hexdigest() == expected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True)
    parser.add_argument("--size", required=True, type=int)
    parser.add_argument("--digest", required=True)
    args = parser.parse_args()
    return 0 if verify_file(args.file, args.size, args.digest) else 1


if __name__ == "__main__":
    raise SystemExit(main())
