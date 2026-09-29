#!/usr/bin/env python3
"""Content-addressed acquisition and two-node release for the pinned vLLM image.

No command uses a mutable registry tag, disables TLS, or starts a container.
Network and remote mutations happen only in the explicit ``probe``, ``acquire``,
``transfer`` and ``load-pair`` subcommands.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import pwd
import re
import shlex
import socket
import stat
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit


SOURCE = (
    "docker://docker.io/vllm/vllm-openai@"
    "sha256:8568b4bbc821903d93a0a9c17dd80382fdc0ba78eaa128e3eb5cb71c3bf06b79"
)
MANIFEST_DIGEST = "sha256:8568b4bbc821903d93a0a9c17dd80382fdc0ba78eaa128e3eb5cb71c3bf06b79"
CONFIG_DIGEST = "sha256:b12d77aecd6ca64639f55ab946f90342ba108ae1c32e4c545242722eff3b04d1"
CONFIG_SIZE = 42901
PLATFORM = ("linux", "arm64")
ENTRYPOINT = ["vllm", "serve"]
COMPRESSED_BYTES = 9_675_672_165
LAYER_COUNT = 37
DIRECT_UID = 996
DIRECT_USER = "dgx-hf-direct"
DIRECT_INTERFACE = "wlP9s9"
SKOPEO = Path("/usr/bin/skopeo")
SKOPEO_PACKAGE_VERSION = "1.13.3+ds1-2ubuntu0.24.04.3"
SKOPEO_SHA256 = "17ab973ac49639564a9f736cf7b78ad931c6a688a7a2492497bca992c850f560"
SKOPEO_LDD_SHA256 = "95bcffffce0b2b454140902d7b9664e1f94bf44a7108ce457bb2c6a49c2a5df4"
POLICY = Path("/etc/containers/policy.json")
POLICY_SHA256 = "e7b7d97048a60f2e26943f2e307caa6f2000e5836cc1c7cf48ff74452521f084"
PROXY_ENV = "DSV4_OCI_SOCKS_PROXY"
WORKER = "operator@192.0.2.34"
WORKER_IDENTITY = "/home/operator/.ssh/deepseek-v4-flash-worker"
WORKER_KNOWN_HOSTS = "/home/operator/.ssh/deepseek-v4-flash-worker-known_hosts"
REMOTE_RELEASE_ROOT = "/home/operator/deepseek-v4-flash-vision-exp/oci"

LAYERS = (
    ("sha256:8fd756343d87359dd6c58de9ce70f6d021bed0f9268fd272a7d270964b85034e", 29942850),
    ("sha256:c7ba38867e8d046b206969939a5d345ebd47819cebd0cab2ebc03588a122bdbc", 4532539),
    ("sha256:c88eadd066162cb15485ec40863657a4c067ba748b3edd900c2538f45c76aeb5", 99730896),
    ("sha256:fd2e70db770294d16caaf4d4d3ff37c95a6984755ba22b3bb82ffb2d54216847", 185),
    ("sha256:c03b8ec8dd332435fbc4c1fe6eeb6f0e8e8da2dd9d5ddcb50ad14c4a7ebf8939", 6886),
    ("sha256:a64256fd816f142460fdcbf8d47af9d63f7ff11a0071f1a6a9bcd60efd2ec3dd", 104),
    ("sha256:974b4a642f8ba906188bf3f520f1db0dcf0b1324cad67e4b33569c25a1e5ff39", 229),
    ("sha256:c86073faa8343f1a8cbf10a7ba5338dddd328a76f027db84e3e38c4022347d52", 226528993),
    ("sha256:9680fe44b08769ba21ebe858188ab04ec6e05a1bf85dfd7a3748736e07a3ddbf", 1561008469),
    ("sha256:4f4fb700ef54461cfa02571ae0db9a0dc1e0cdb5577484a6d75e68dc38e8acc1", 32),
    ("sha256:d0765867a03c0bdd26b7558e226c73924061f8ca0f3a9ba5e9966d64683e3d2e", 39658043),
    ("sha256:747ed60938235f9a844bd41b4149611d88ae42c3b2758d754ea3d8d9a758c81e", 150),
    ("sha256:982c4dee390d5a7edcbc0dde1ed0a1058d2aba5632fc5ba826716d98c867e177", 164),
    ("sha256:c52577cd045a3e5771b20cff5563af878457c732dd695bcf50b5582cebe5ab10", 3481),
    ("sha256:38eb57bc07c341deec10360f80ce555caf831d779238f2b77e3618f484b2bee4", 2083),
    ("sha256:4f4fb700ef54461cfa02571ae0db9a0dc1e0cdb5577484a6d75e68dc38e8acc1", 32),
    ("sha256:810b2af1821c4a4da8751d30a8d8cabceeb911da8941b35c292661b0f2f415d6", 1673),
    ("sha256:96f11480260568a917c8e0bf14c599a40d27ecdf91399fb963cac255788e0c2a", 875),
    ("sha256:59968349701f9324b045957ea66093e98212f12a68190ec50d2b1de3f6722adc", 5209466934),
    ("sha256:041283c2231deaf4aefc1daaac48d6fe379cb820a305276472a0cf684d7be5be", 1131484650),
    ("sha256:b014c792312218d4945a2766768a736848662d64514bf28b7e523fd617686dd3", 897),
    ("sha256:e09cfc46e39c5c9170b14296f905b5df14a3d4f9532d73c2e39b4f28b2a3abb3", 323321),
    ("sha256:21c8a67d18e0cede9d10fc1dfd586c09c1d13337f3abee5d743bd6b0915f54c1", 284281311),
    ("sha256:25e215fac05134987b66cf9f51b79d93777ab8d77aa4cfa1dcaa185fbd7e4e0c", 201),
    ("sha256:14b2db2db0c17eb03861c90f8bc6634165a001af07bbd5d37d5cd7964e971d3e", 239),
    ("sha256:b8e368fd9963d3f3866b8631ab691682282f49b38b6f6907be031dcb741e90a4", 521253074),
    ("sha256:4f4fb700ef54461cfa02571ae0db9a0dc1e0cdb5577484a6d75e68dc38e8acc1", 32),
    ("sha256:6f89afe9c275eef25bf6e43bb4aeff42232c7e2df074e8db0a0ab741f53032c7", 253),
    ("sha256:8149dbd8ba7fc46f9a7a78b5d2832868d05f1b162a6905a5cac5a8f805edfed0", 14310093),
    ("sha256:160edbbc0343103bbbb97c4ceb1d7d9543bc82c1415d6453712ce3637763b86d", 274644),
    ("sha256:c005a117177925a6111f77aa19e6f79f4d643d40beaec19ea870178f7461f14f", 291785),
    ("sha256:17343ce0fa5a82428ac901d3ff2f1c81c3849b44c8485283b289acc9f2c48118", 9841),
    ("sha256:4fe841bbe81cdc7dd22c08a2f26b3141d32f044debfcbd5fde86dbea2d36a655", 552555964),
    ("sha256:4f4fb700ef54461cfa02571ae0db9a0dc1e0cdb5577484a6d75e68dc38e8acc1", 32),
    ("sha256:bdf973a8e05e8f517f0ec04b104ebe969fb2435a281c58bcaa436b0d149ddff3", 212),
    ("sha256:aace0d36c9c721c5192a8215ec5dffdcfd95e244739f15bdea2ece94238ad8bd", 824),
    ("sha256:81aff31d322896e206754302266a7047cbc5c1f3171f3bb3d92c60721617f5dc", 174),
)

ROOTFS_DIFF_IDS = (
    "sha256:187d2314ef56af033ce427b9fe84a085a232fb54127a75999f5173c057b6e225",
    "sha256:a52c02546417da6990102dd006e31e35ae9238840989fa04968d43e17793b20b",
    "sha256:b60fd1af0a9d4e3622a9c3eda0769a14a3ddb27d8ef01a5d749891066bbc66be",
    "sha256:cc18467866bca6611f7c0fcd500e1295d2a420869310b4d2a3cc7238a7b0909b",
    "sha256:495bbaf4cc5ca53d8286fe305d2fc2fd3ecb2c8791cfdd4473e984d2eb0f3ea1",
    "sha256:52c5691af40c4a0132e5f30842c9920b4a230a0b670bf6319f91969e1677adbf",
    "sha256:827462bf4c37bc06b23fa59e008d9ad5e1b0c812f98b30e51e0565c3d9b7aa49",
    "sha256:eaf821734357b4b233f078009f57c16c2a823531e6f7e8cd1f78e796a60e213f",
    "sha256:85af90cd7d07d005dcd357f9cd55c6ddd8340b4db83fac2abe9bdfa120d699bc",
    "sha256:5f70bf18a086007016e948b04aed3b82103a36bea41755b6cddfaf10ace3c6ef",
    "sha256:d93d8a6fd5ef9a6e5a2bc815e5b35e580cde1cd5e52c6f94441dc70a973a0cc1",
    "sha256:f6e2bf4620c0cdd9dcba8e2ea05419a4a00d9d2cc338a43fa28d770587af8580",
    "sha256:afe3a8acb75471e9bc48ebe09d56014d6784f9948c856e7d48bba0fa53775de8",
    "sha256:88fb83395dbff6c2197315a86d942491ab88fc15fde39acf72bda4f86395abe7",
    "sha256:68ddc2b72bca928da62ef0e6af8e0ff822a01cb7f45d1368167b21977e2fe145",
    "sha256:5f70bf18a086007016e948b04aed3b82103a36bea41755b6cddfaf10ace3c6ef",
    "sha256:830c99c09c586643c015c6b59af40c7e6c9ff93d48b6a438cfbf94e32092202e",
    "sha256:de73563f4bdfef62e9a50513bb1cc8b965853e65198dc1b13f593bbe5fe37725",
    "sha256:c938433b07c29d703ae0117761d55e9f7e1e9e32829f0b74fc5541a8efe816e2",
    "sha256:c1c7dbb0746fdba948f6de6897fb4468b2c4a8ad247894d976a6eaa2f4df75aa",
    "sha256:0463f8dbe011c5d88ce88091fbee58c3284d4dd03b149125b4d3a8dc7fb3625f",
    "sha256:5c3c0dad3f52836c9453bf8093aa50e40f63b910547451d2bacca817c53fce59",
    "sha256:a1800fe18585dbf954f3aa1cef2f55fd00f70d5ce9340098ea607a1ac6aa698f",
    "sha256:6f75866c19818a0f7f6f21fb89f6fd1c8ac6d8d3d11f35a40a043eadf6a992df",
    "sha256:97a4477058900718e17928df859a8ed3df48c50f975d854918953c937b286978",
    "sha256:15c6659653237e9f3346e07cb934a132a8b164a993f7dc2981f1c6e64b930742",
    "sha256:5f70bf18a086007016e948b04aed3b82103a36bea41755b6cddfaf10ace3c6ef",
    "sha256:f06a346a2e00887f939f54b8cac185cfc723040a5bcbc48f41a895c2d209f4ba",
    "sha256:8972efcc9c32dd0ce820cee2f43dc5a9c8a79b892bcdc5ff8d2c9f8767557b52",
    "sha256:7bd733123c07d3a60f9364071b792faa8c306df3eb24accad88375ed5e3142a8",
    "sha256:a365c7e11f7f2f99946c2280f982944d2982ec99ae500f04ecece54deedbfece",
    "sha256:4f4510168ab57b3d5d713cdefef86979db82f37fdc26bcdf4300d2c6f2946047",
    "sha256:64c22b5b8ddce98cf0e4fe4c48d3662ec2539c3783fd24adc5591d3cd32053b9",
    "sha256:5f70bf18a086007016e948b04aed3b82103a36bea41755b6cddfaf10ace3c6ef",
    "sha256:98ecb54ca827515297fcdc300a55c84c2649c8d7adb3dd35d70376d9909afac5",
    "sha256:342762acd38d1fcac78cc2779688dd23d0a2e1086d0205e773ce86d11cebd8dd",
    "sha256:f97904b0fcefdf17e3744d8883f7b74502d61506a91b5bf0ed265c529e053d24",
)


class ReleaseError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def validate_absolute_path(path: Path, *, allow_missing_leaf: bool = False) -> Path:
    if not path.is_absolute() or path == Path("/") or ".." in path.parts:
        raise ReleaseError("path must be absolute, narrow, and traversal-free")
    probe = path.parent if allow_missing_leaf else path
    while probe != probe.parent and not probe.exists():
        probe = probe.parent
    if probe.is_symlink():
        raise ReleaseError("symlink path components are forbidden")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.exists() and current.is_symlink():
            raise ReleaseError("symlink path components are forbidden")
    return path


def ensure_release_root(path: Path) -> None:
    validate_absolute_path(path)
    path.mkdir(mode=0o755, parents=True, exist_ok=True)
    details = path.stat()
    if path.is_symlink() or details.st_uid != 0 or details.st_mode & 0o022:
        raise ReleaseError("release root must be root-owned and not group/world writable")


def validate_source(source: str) -> None:
    if source != SOURCE:
        raise ReleaseError("only the pinned child-manifest digest source is allowed")
    lowered = source.lower()
    if "--tls-verify=false" in lowered or "--insecure-policy" in lowered:
        raise ReleaseError("TLS or signature-policy weakening is forbidden")


def proxy_environment(raw: str | None) -> dict[str, str]:
    base = {"HOME": "/home/dgx-hf-direct", "PATH": "/usr/bin:/bin"}
    if not raw:
        return base
    parsed = urlsplit(raw)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ReleaseError(f"{PROXY_ENV} has an invalid port") from exc
    if (
        parsed.scheme != "socks5"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or port is None
        or not 1024 <= port <= 65535
    ):
        raise ReleaseError(
            f"{PROXY_ENV} must be an uncredentialed loopback socks5 URL with an explicit port"
        )
    canonical = f"socks5://127.0.0.1:{port}"
    return {
        **base,
        "HTTP_PROXY": canonical,
        "HTTPS_PROXY": canonical,
        "ALL_PROXY": canonical,
        "NO_PROXY": "127.0.0.1,localhost",
    }


def expected_manifest() -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {
            "mediaType": "application/vnd.docker.container.image.v1+json",
            "size": CONFIG_SIZE,
            "digest": CONFIG_DIGEST,
        },
        "layers": [
            {
                "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
                "size": size,
                "digest": digest,
            }
            for digest, size in LAYERS
        ],
    }


def verify_manifest(raw: bytes) -> dict[str, object]:
    if "sha256:" + hashlib.sha256(raw).hexdigest() != MANIFEST_DIGEST:
        raise ReleaseError("child manifest byte digest mismatch")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReleaseError("child manifest is not JSON") from exc
    if value != expected_manifest():
        raise ReleaseError("child manifest exact descriptor closure mismatch")
    if len(LAYERS) != LAYER_COUNT or sum(size for _, size in LAYERS) != COMPRESSED_BYTES:
        raise ReleaseError("embedded 37-layer closure is internally inconsistent")
    return value


def verify_config(raw: bytes) -> dict[str, object]:
    if "sha256:" + hashlib.sha256(raw).hexdigest() != CONFIG_DIGEST:
        raise ReleaseError("config blob digest mismatch")
    try:
        value = json.loads(raw)
        observed = (
            value["os"],
            value["architecture"],
            value["rootfs"]["type"],
            tuple(value["rootfs"]["diff_ids"]),
            value["config"].get("Entrypoint"),
        )
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ReleaseError("config blob schema is invalid") from exc
    expected = (PLATFORM[0], PLATFORM[1], "layers", ROOTFS_DIFF_IDS, ENTRYPOINT)
    if observed != expected:
        raise ReleaseError("config platform, RootFS DiffIDs, or entrypoint mismatch")
    return value


def _blob_name(digest: str) -> str:
    return digest.removeprefix("sha256:")


def verify_dir(path: Path, *, expected_uid: int | None = 0, fsync: bool = False) -> dict[str, object]:
    validate_absolute_path(path)
    if not path.is_dir() or path.is_symlink():
        raise ReleaseError("OCI dir transport path is absent or unsafe")
    if expected_uid is not None and path.stat().st_uid != expected_uid:
        raise ReleaseError(f"OCI dir transport is not owned by required UID {expected_uid}")
    unique_layers = dict(LAYERS)
    expected_names = {
        "manifest.json",
        "version",
        _blob_name(CONFIG_DIGEST),
        *(_blob_name(item) for item in unique_layers),
    }
    names = {item.name for item in path.iterdir()}
    if names != expected_names:
        raise ReleaseError("OCI dir transport contains missing or extra entries")
    for item in path.iterdir():
        mode = item.lstat().st_mode
        if not stat.S_ISREG(mode) or stat.S_ISLNK(mode) or item.stat().st_nlink != 1:
            raise ReleaseError("OCI dir entries must be regular, non-hardlinked files")
        if expected_uid is not None and item.stat().st_uid != expected_uid:
            raise ReleaseError("OCI dir entry ownership mismatch")
    manifest = (path / "manifest.json").read_bytes()
    verify_manifest(manifest)
    if (path / "version").read_text(encoding="ascii") != "Directory Transport Version: 1.1\n":
        raise ReleaseError("OCI dir transport version mismatch")
    config = path / _blob_name(CONFIG_DIGEST)
    if config.stat().st_size != CONFIG_SIZE:
        raise ReleaseError("config blob size mismatch")
    verify_config(config.read_bytes())
    for digest, size in unique_layers.items():
        blob = path / _blob_name(digest)
        if blob.stat().st_size != size or sha256_file(blob) != _blob_name(digest):
            raise ReleaseError(f"layer closure mismatch: {digest}")
    if fsync:
        for name in sorted(expected_names):
            descriptor = os.open(path / name, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return {
        "manifest_digest": MANIFEST_DIGEST,
        "config_digest": CONFIG_DIGEST,
        "layer_count": len(LAYERS),
        "unique_layer_blobs": len(unique_layers),
        "compressed_bytes": COMPRESSED_BYTES,
    }


def no_replace(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise ReleaseError("renameat2 no-replace primitive is unavailable")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    if renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise FileExistsError(f"refusing to replace published path: {destination}")
        raise OSError(error, os.strerror(error), str(destination))
    descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def direct_command(arguments: list[str], proxy: str | None) -> list[str]:
    environment = proxy_environment(proxy)
    assignments = [f"{key}={value}" for key, value in sorted(environment.items())]
    return ["sudo", "-n", "-u", f"#{DIRECT_UID}", "env", "-i", *assignments, *arguments]


def skopeo_base(proxy: str | None) -> list[str]:
    return direct_command(
        [str(SKOPEO), "--policy", str(POLICY), "--override-os", "linux", "--override-arch", "arm64"],
        proxy,
    )


def run_checked(command: list[str], *, stdout=None) -> subprocess.CompletedProcess:
    forbidden = {"--tls-verify=false", "--insecure-policy"}
    if any(argument.lower() in forbidden for argument in command):
        raise ReleaseError("refusing TLS or policy weakening")
    return subprocess.run(command, check=True, stdout=stdout)


def verify_toolchain() -> dict[str, object]:
    if socket.gethostname() != "dgx-head":
        raise ReleaseError("OCI acquisition tooling must run on the verified head dgx-head")
    skopeo_stat = SKOPEO.stat()
    if (
        not SKOPEO.is_file()
        or SKOPEO.is_symlink()
        or skopeo_stat.st_uid != 0
        or skopeo_stat.st_mode & 0o022
        or sha256_file(SKOPEO) != SKOPEO_SHA256
    ):
        raise ReleaseError("pinned /usr/bin/skopeo binary mismatch")
    package = subprocess.run(
        ["dpkg-query", "-W", "-f=${Version}", "skopeo"], check=True, text=True, capture_output=True
    ).stdout
    if package != SKOPEO_PACKAGE_VERSION:
        raise ReleaseError("pinned skopeo package version mismatch")
    version = subprocess.run([str(SKOPEO), "--version"], check=True, text=True, capture_output=True).stdout.strip()
    if version != "skopeo version 1.13.3":
        raise ReleaseError("skopeo runtime version mismatch")
    linked_raw = subprocess.run(
        ["ldd", str(SKOPEO)], check=True, text=True, capture_output=True
    ).stdout
    linked = sorted(
        re.sub(r"\s+\(0x[0-9a-f]+\)$", "", line.strip())
        for line in linked_raw.splitlines()
    )
    linked_digest = hashlib.sha256(("\n".join(linked) + "\n").encode()).hexdigest()
    if linked_digest != SKOPEO_LDD_SHA256:
        raise ReleaseError("skopeo linked-library closure mismatch")
    policy_stat = POLICY.stat()
    if (
        not POLICY.is_file()
        or POLICY.is_symlink()
        or policy_stat.st_uid != 0
        or policy_stat.st_mode & 0o022
        or sha256_file(POLICY) != POLICY_SHA256
    ):
        raise ReleaseError("containers policy hash mismatch")
    direct_account = pwd.getpwuid(DIRECT_UID)
    if (
        direct_account.pw_name != DIRECT_USER
        or direct_account.pw_dir != "/home/dgx-hf-direct"
        or direct_account.pw_shell != "/usr/sbin/nologin"
    ):
        raise ReleaseError("UID 996 identity mismatch")
    uid = subprocess.run(
        ["sudo", "-n", "-u", f"#{DIRECT_UID}", "id", "-u"], check=True, text=True, capture_output=True
    ).stdout.strip()
    route = subprocess.run(
        ["sudo", "-n", "-u", f"#{DIRECT_UID}", "ip", "route", "get", "1.1.1.1"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    if uid != str(DIRECT_UID) or not re.search(rf"\bdev {re.escape(DIRECT_INTERFACE)}\b", route):
        raise ReleaseError("UID 996 direct-route gate failed")
    return {
        "skopeo_package": package,
        "skopeo_sha256": SKOPEO_SHA256,
        "skopeo_ldd_sha256": SKOPEO_LDD_SHA256,
        "policy_sha256": POLICY_SHA256,
        "direct_uid": DIRECT_UID,
        "direct_interface": DIRECT_INTERFACE,
        "linked_libraries": linked,
    }


def cmd_probe(args: argparse.Namespace) -> None:
    validate_source(args.source)
    verify_toolchain()
    base = skopeo_base(os.environ.get(PROXY_ENV))
    manifest = subprocess.run(base + ["inspect", "--raw", args.source], check=True, capture_output=True).stdout
    config = subprocess.run(
        base + ["inspect", "--config", "--raw", args.source], check=True, capture_output=True
    ).stdout
    verify_manifest(manifest)
    verify_config(config)
    print(json.dumps({"status": "PASS", "manifest": MANIFEST_DIGEST, "config": CONFIG_DIGEST}))


def release_paths(root: Path) -> tuple[Path, Path]:
    validate_absolute_path(root)
    token = _blob_name(MANIFEST_DIGEST)
    return root / f"sha256-{token}.partial", root / f"sha256-{token}"


def cmd_acquire(args: argparse.Namespace) -> None:
    validate_source(args.source)
    verify_toolchain()
    staging, published = release_paths(args.root)
    if published.exists():
        verify_dir(published, fsync=False)
        print(json.dumps({"status": "ALREADY_PUBLISHED", "path": str(published)}))
        return
    if os.geteuid() != 0:
        raise ReleaseError("acquire must run through sudo -n for deterministic ownership")
    ensure_release_root(args.root)
    if staging.exists() and (not staging.is_dir() or staging.is_symlink()):
        raise ReleaseError("digest-specific staging path is unsafe")
    if not staging.exists():
        run_checked(["sudo", "-n", "install", "-d", "-o", DIRECT_USER, "-g", DIRECT_USER, "-m", "0700", str(staging)])
    command = skopeo_base(os.environ.get(PROXY_ENV)) + [
        "copy", "--retry-times", str(args.retry_times), "--preserve-digests", args.source, f"dir:{staging}"
    ]
    run_checked(command)
    run_checked(["chown", "-R", "root:root", str(staging)])
    verify_dir(staging, expected_uid=0, fsync=True)
    for item in staging.iterdir():
        item.chmod(0o444)
    staging.chmod(0o555)
    no_replace(staging, published)
    print(json.dumps({"status": "PUBLISHED", "path": str(published)}))


def write_exclusive(path: Path, payload: bytes, mode: int = 0o600) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(descriptor)


def fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def receipt_for(archive: Path) -> dict[str, object]:
    return {
        "schema": "deepseek-v4-flash-vision-exp-oci-archive-v1",
        "source": SOURCE,
        "manifest_digest": MANIFEST_DIGEST,
        "config_digest": CONFIG_DIGEST,
        "platform": "/".join(PLATFORM),
        "layer_count": len(LAYERS),
        "compressed_bytes": COMPRESSED_BYTES,
        "rootfs_diff_ids": list(ROOTFS_DIFF_IDS),
        "entrypoint": ENTRYPOINT,
        "archive_file": archive.name,
        "archive_size": archive.stat().st_size,
        "archive_sha256": sha256_file(archive),
    }


def verify_archive(release: Path) -> dict[str, object]:
    validate_absolute_path(release)
    if not release.is_dir() or release.is_symlink():
        raise ReleaseError("archive release directory is absent or unsafe")
    expected = {"base.docker-archive.tar", "manifest.digest", "receipt.json"}
    if {item.name for item in release.iterdir()} != expected:
        raise ReleaseError("archive release exact-set mismatch")
    archive = release / "base.docker-archive.tar"
    digestfile = release / "manifest.digest"
    receipt_path = release / "receipt.json"
    for item in (archive, digestfile, receipt_path):
        if not item.is_file() or item.is_symlink() or item.stat().st_nlink != 1:
            raise ReleaseError("archive release contains an unsafe file")
    if digestfile.read_text(encoding="ascii") not in {MANIFEST_DIGEST, MANIFEST_DIGEST + "\n"}:
        raise ReleaseError("skopeo digestfile mismatch")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt != receipt_for(archive):
        raise ReleaseError("archive receipt size/hash/identity mismatch")
    return receipt


def cmd_archive(args: argparse.Namespace) -> None:
    verify_toolchain()
    if os.geteuid() != 0:
        raise ReleaseError("archive must run through sudo -n so the UID 996 OCI dir remains protected")
    verify_dir(args.verified_dir, fsync=False)
    validate_absolute_path(args.release_root)
    ensure_release_root(args.release_root)
    token = _blob_name(MANIFEST_DIGEST)
    final = args.release_root / f"sha256-{token}.release"
    temporary = args.release_root / f".sha256-{token}.release.{os.getpid()}"
    if final.exists():
        verify_archive(final)
        print(json.dumps({"status": "ALREADY_PUBLISHED", "path": str(final)}))
        return
    run_checked([
        "sudo", "-n", "install", "-d", "-o", DIRECT_USER, "-g", DIRECT_USER,
        "-m", "0700", str(temporary),
    ])
    archive = temporary / "base.docker-archive.tar"
    digestfile = temporary / "manifest.digest"
    destination = f"docker-archive:{archive}:vllm/vllm-openai:vision-base-{token[:12]}"
    command = skopeo_base(None) + [
        "copy", "--preserve-digests", "--digestfile", str(digestfile), f"dir:{args.verified_dir}", destination
    ]
    try:
        run_checked(command)
        if digestfile.read_text(encoding="ascii").strip() != MANIFEST_DIGEST:
            raise ReleaseError("skopeo archive digestfile mismatch")
        run_checked(["chown", "-R", "root:root", str(temporary)])
        receipt = receipt_for(archive)
        write_exclusive(temporary / "receipt.json", json_bytes(receipt))
        fsync_file(archive)
        fsync_file(digestfile)
        descriptor = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        verify_archive(temporary)
        no_replace(temporary, final)
    finally:
        if temporary.exists():
            for item in temporary.iterdir():
                item.unlink()
            temporary.rmdir()
    print(json.dumps({"status": "PUBLISHED", "path": str(final), **receipt}))


def ssh_base() -> list[str]:
    return [
        "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "IdentitiesOnly=yes", "-o", f"UserKnownHostsFile={WORKER_KNOWN_HOSTS}",
        "-i", WORKER_IDENTITY, WORKER,
    ]


def verify_worker_identity() -> None:
    observed = subprocess.run(
        ssh_base() + ["hostname; whoami"], check=True, text=True, capture_output=True
    ).stdout.splitlines()
    if observed != ["dgx-worker", "operator"]:
        raise ReleaseError("fabric SSH target identity mismatch")


def safe_remote_path(path: str) -> str:
    candidate = Path(path)
    if not candidate.is_absolute() or not re.fullmatch(r"/[A-Za-z0-9._/-]+", path) or ".." in candidate.parts:
        raise ReleaseError("worker path is unsafe")
    return path


def cmd_transfer(args: argparse.Namespace) -> None:
    receipt = verify_archive(args.release)
    if socket.gethostname() != "dgx-head":
        raise ReleaseError("fabric transfer must run on the verified head dgx-head")
    verify_worker_identity()
    remote_root = safe_remote_path(args.remote_root)
    remote_release = f"{remote_root}/{args.release.name}"
    remote_staging = f"{remote_root}/.{args.release.name}.partial"
    ssh = ssh_base()
    local_hashes = {
        name: sha256_file(args.release / name)
        for name in ("base.docker-archive.tar", "manifest.digest", "receipt.json")
    }
    check = " && ".join(
        f"test $(sha256sum {shlex.quote(remote_release + '/' + name)} | cut -d' ' -f1) = {digest}"
        for name, digest in local_hashes.items()
    )
    existing = subprocess.run(ssh + [check], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if existing.returncode == 0:
        print(json.dumps({"status": "ALREADY_TRANSFERRED", "worker": WORKER, **local_hashes}))
        return
    absent = subprocess.run(ssh + [f"test ! -e {shlex.quote(remote_release)}"])
    if absent.returncode:
        raise ReleaseError("worker release exists but does not match the exact head closure")
    root_q = shlex.quote(remote_root)
    staging_q = shlex.quote(remote_staging)
    run_checked(ssh + [f"install -d -m 0700 {root_q} && test $(realpath -e {root_q}) = {root_q}"])
    run_checked(
        ssh + [f"install -d -m 0700 {staging_q} && test $(realpath -e {staging_q}) = {staging_q}"]
    )
    run_checked([
        "rsync", "--recursive", "--times", "--partial", "--append-verify", "--fsync", "--protect-args",
        "-e", shlex.join(ssh[:-1]), f"{args.release}/", f"{WORKER}:{remote_staging}/",
    ])
    staging_check = " && ".join(
        f"test $(sha256sum {shlex.quote(remote_staging + '/' + name)} | cut -d' ' -f1) = {digest}"
        for name, digest in local_hashes.items()
    )
    run_checked(ssh + [staging_check])
    promote = (
        f"mv -T --no-clobber {shlex.quote(remote_staging)} {shlex.quote(remote_release)}"
        f" && test ! -e {shlex.quote(remote_staging)}"
    )
    run_checked(ssh + [promote])
    print(json.dumps({"status": "TRANSFERRED", "worker": WORKER, "archive_sha256": receipt["archive_sha256"]}))


def inspect_identity(value: dict[str, object]) -> None:
    observed = (
        value.get("Id"), value.get("Os"), value.get("Architecture"),
        tuple(value.get("RootFS", {}).get("Layers", [])), value.get("Config", {}).get("Entrypoint"),
    )
    expected = (CONFIG_DIGEST, PLATFORM[0], PLATFORM[1], ROOTFS_DIFF_IDS, ENTRYPOINT)
    if observed != expected:
        raise ReleaseError("loaded image ID/platform/RootFS/entrypoint mismatch")


def load_and_inspect(command_prefix: list[str], archive: str) -> None:
    run_checked(command_prefix + ["docker", "image", "load", "--input", archive])
    result = subprocess.run(
        command_prefix + ["docker", "image", "inspect", CONFIG_DIGEST],
        check=True,
        text=True,
        capture_output=True,
    )
    values = json.loads(result.stdout)
    if not isinstance(values, list) or len(values) != 1:
        raise ReleaseError("docker inspect returned an invalid image set")
    inspect_identity(values[0])


def cmd_load_pair(args: argparse.Namespace) -> None:
    receipt = verify_archive(args.release)
    if socket.gethostname() != "dgx-head":
        raise ReleaseError("paired load must run on the verified head dgx-head")
    verify_worker_identity()
    local_archive = str(args.release / "base.docker-archive.tar")
    remote_release = f"{safe_remote_path(args.remote_root)}/{args.release.name}"
    remote_archive = f"{remote_release}/base.docker-archive.tar"
    remote_hash = subprocess.run(
        ssh_base() + [f"sha256sum {shlex.quote(remote_archive)}"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.split()[0]
    if remote_hash != receipt["archive_sha256"]:
        raise ReleaseError("worker archive SHA-256 differs before docker load")
    load_and_inspect([], local_archive)
    quoted_archive = shlex.quote(remote_archive)
    remote = ssh_base()
    run_checked(remote + [f"sudo -n docker image load --input {quoted_archive}"])
    result = subprocess.run(
        remote + [f"sudo -n docker image inspect {shlex.quote(CONFIG_DIGEST)}"],
        check=True,
        text=True,
        capture_output=True,
    )
    values = json.loads(result.stdout)
    if not isinstance(values, list) or len(values) != 1:
        raise ReleaseError("worker docker inspect returned an invalid image set")
    inspect_identity(values[0])
    print(json.dumps({"status": "LOADED_BOTH", "config_digest": CONFIG_DIGEST}))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("toolchain")
    probe = sub.add_parser("probe")
    probe.add_argument("--source", default=SOURCE)
    acquire = sub.add_parser("acquire")
    acquire.add_argument("--source", default=SOURCE)
    acquire.add_argument("--root", type=Path, required=True)
    acquire.add_argument("--retry-times", type=int, choices=range(1, 6), default=3)
    verify = sub.add_parser("verify-dir")
    verify.add_argument("--path", type=Path, required=True)
    archive = sub.add_parser("archive")
    archive.add_argument("--verified-dir", type=Path, required=True)
    archive.add_argument("--release-root", type=Path, required=True)
    check_archive = sub.add_parser("verify-archive")
    check_archive.add_argument("--release", type=Path, required=True)
    transfer = sub.add_parser("transfer")
    transfer.add_argument("--release", type=Path, required=True)
    transfer.add_argument("--remote-root", default=REMOTE_RELEASE_ROOT)
    load = sub.add_parser("load-pair")
    load.add_argument("--release", type=Path, required=True)
    load.add_argument("--remote-root", default=REMOTE_RELEASE_ROOT)
    return result


def main() -> int:
    args = parser().parse_args()
    if args.command == "toolchain":
        print(json.dumps({"status": "PASS", **verify_toolchain()}, sort_keys=True))
    elif args.command == "probe":
        cmd_probe(args)
    elif args.command == "acquire":
        cmd_acquire(args)
    elif args.command == "verify-dir":
        print(json.dumps({"status": "PASS", **verify_dir(args.path)}, sort_keys=True))
    elif args.command == "archive":
        cmd_archive(args)
    elif args.command == "verify-archive":
        print(json.dumps({"status": "PASS", **verify_archive(args.release)}, sort_keys=True))
    elif args.command == "transfer":
        cmd_transfer(args)
    elif args.command == "load-pair":
        cmd_load_pair(args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ReleaseError, FileExistsError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
