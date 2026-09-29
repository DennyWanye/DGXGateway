from __future__ import annotations

import ipaddress
import tomllib
from dataclasses import dataclass
from pathlib import Path


RESERVED_PORTS = {8080, 8188, 11434, 18081}
NODE_ROLES = ("head", "worker", "rank2", "rank3")
PAIRED_ROLES = NODE_ROLES[:2]
EXPECTED_HOSTNAMES = {
    "head": "dgx-head",
    "worker": "dgx-worker",
    "rank2": "dgx-rank2",
    "rank3": "dgx-rank3",
}


@dataclass(frozen=True)
class NodeConfig:
    role: str
    hostname: str
    address: str
    user: str = "operator"


def paired_nodes(nodes: tuple[NodeConfig, ...]) -> tuple[NodeConfig, ...]:
    return tuple(node for node in nodes if node.role in PAIRED_ROLES)


@dataclass(frozen=True)
class GatewayConfig:
    bind: str
    port: int
    allowed_origins: tuple[str, ...]
    state_dir: Path
    admin_token_file: Path
    repo_root: Path
    lock_root: Path
    lock_cli: Path
    token_dir: Path
    deepseek_journal: Path
    deepseek_gateway_secret: Path
    deepseek_drain_timeout_seconds: int
    deepseek_vision_journal: Path
    deepseek_vision_gateway_secret: Path
    deepseek_vision_drain_timeout_seconds: int
    deepseek_vision_internal_port: int
    h3_queue_url: str
    music3_root: Path
    music3_python: Path
    music3_port: int
    personaplex_root: Path
    authority_id: str
    authority_record: Path
    authority_credential: Path
    authority_remote_fence_path: Path
    authority_remote_fence_enabled: bool
    mutation_enabled: bool
    acceptance_failure_model: str | None
    nodes: tuple[NodeConfig, ...]
    lan_no_auth: bool = False
    drama_enabled: bool = False
    text_enabled: bool = False
    text_qualification_file: Path | None = None
    text_qualification_sha256: str | None = None


def _absolute(value: str, name: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{name} must be absolute")
    return path


def load_config(path: Path) -> GatewayConfig:
    document = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    drama_enabled = document.get("drama", {}).get("enabled", False)
    if type(drama_enabled) is not bool:
        raise ValueError("drama.enabled must be a boolean")
    text = document.get("text", {})
    if not isinstance(text, dict) or type(text.get("enabled", False)) is not bool:
        raise ValueError("text.enabled must be a boolean")
    qualification_file = text.get("qualification_file")
    qualification_sha = text.get("qualification_sha256")
    if qualification_file is not None: qualification_file = _absolute(qualification_file, "text qualification file")
    if qualification_sha is not None and (not isinstance(qualification_sha, str) or len(qualification_sha) != 64 or any(c not in '0123456789abcdef' for c in qualification_sha)):
        raise ValueError("text qualification hash is invalid")
    if text.get("enabled", False) and (qualification_file is None or qualification_sha is None):
        raise ValueError("enabled text requires an explicit qualification artifact and hash")
    gateway = document.get("gateway", {})
    cluster = document.get("cluster", {})
    models = document.get("models", {})
    bind = str(gateway.get("bind", ""))
    ipaddress.ip_address(bind)
    port = int(gateway.get("port", 0))
    if not 1024 <= port <= 65535 or port in RESERVED_PORTS:
        raise ValueError("gateway port is invalid or reserved")
    origins = tuple(gateway.get("allowed_origins", ()))
    if not origins or any(not isinstance(item, str) or "*" in item for item in origins):
        raise ValueError("allowed_origins must be an exact non-empty list")
    node_rows = document.get("nodes", {})
    if "rank2" in node_rows or "rank3" in node_rows:
        if "rank2" not in node_rows or "rank3" not in node_rows:
            raise ValueError("rank2 and rank3 must be configured together")
        roles = NODE_ROLES
    else:
        roles = NODE_ROLES[:2]
    nodes = tuple(
        NodeConfig(
            role=role,
            hostname=str(node_rows[role]["hostname"]),
            address=str(ipaddress.ip_address(node_rows[role]["address"])),
            user=str(node_rows[role].get("user", "operator")),
        )
        for role in roles
    )
    if any(node.hostname != EXPECTED_HOSTNAMES[node.role] for node in nodes):
        raise ValueError("cluster role mapping is invalid")
    repo_root = _absolute(cluster["repo_root"], "repo_root")
    drain_timeout = int(models["deepseek"].get("drain_timeout_seconds", 120))
    if not 5 <= drain_timeout <= 900:
        raise ValueError("DeepSeek drain timeout is invalid")
    vision = models.get("deepseek_vision")
    if not isinstance(vision, dict):
        raise ValueError("models.deepseek_vision is required")
    vision_drain_timeout = int(vision.get("drain_timeout_seconds", 120))
    if not 5 <= vision_drain_timeout <= 900:
        raise ValueError("DeepSeek Vision drain timeout is invalid")
    vision_internal_port = int(vision.get("internal_port", 18889))
    if not 1024 <= vision_internal_port <= 65535:
        raise ValueError("DeepSeek Vision internal port is invalid")
    h3_queue_url = str(models["h3"]["queue_url"])
    if h3_queue_url != "http://192.0.2.41:8188/queue":
        raise ValueError("H3 queue URL must use the fixed ComfyUI LAN endpoint")
    music = models.get("music3", {})
    persona = models.get("personaplex", {})
    authority = document.get("authority", {})
    testing = document.get("acceptance_testing", {})
    failure_model = testing.get("fail_target_health_model")
    if failure_model is not None and failure_model not in {
        "deepseek-v4-flash",
        "deepseek-v4-flash-vision-exp",
        "minimax-h3",
        "minimax-music3",
        "comfyui",
    }:
        raise ValueError("invalid acceptance failure model")
    music_port = int(music.get("port", 8188))
    if music_port != 8188:
        raise ValueError("Music 3 must use the fixed head port 8188")
    authority_id = str(authority["id"])
    remote_fence_enabled = bool(authority.get("remote_fence_enabled", True))
    if not remote_fence_enabled and not (
        bind in {"127.0.0.1", "::1"} and authority_id.startswith("test-")
    ):
        raise ValueError("remote authority fence may only be disabled by loopback test authorities")
    deepseek_journal = _absolute(models["deepseek"]["journal"], "deepseek journal")
    deepseek_secret = _absolute(models["deepseek"]["gateway_secret"], "deepseek secret")
    vision_journal = _absolute(vision["journal"], "deepseek vision journal")
    vision_secret = _absolute(vision["gateway_secret"], "deepseek vision secret")
    if vision_journal.resolve(strict=False) == deepseek_journal.resolve(strict=False):
        raise ValueError("DeepSeek and DeepSeek Vision journals must be distinct")
    if vision_secret.resolve(strict=False) == deepseek_secret.resolve(strict=False):
        raise ValueError("DeepSeek and DeepSeek Vision secrets must be distinct")
    if vision_internal_port in RESERVED_PORTS | {8000, 18888, port}:
        raise ValueError("DeepSeek Vision internal port conflicts with an existing port")
    return GatewayConfig(
        bind=bind,
        port=port,
        allowed_origins=origins,
        state_dir=_absolute(gateway["state_dir"], "state_dir"),
        admin_token_file=_absolute(gateway["admin_token_file"], "admin_token_file"),
        repo_root=repo_root,
        lock_root=_absolute(cluster["lock_root"], "lock_root"),
        lock_cli=_absolute(cluster.get("lock_cli", str(repo_root / "scripts/dgx-spark-deployment-lock.py")), "lock_cli"),
        token_dir=_absolute(cluster["token_dir"], "token_dir"),
        deepseek_journal=deepseek_journal,
        deepseek_gateway_secret=deepseek_secret,
        deepseek_drain_timeout_seconds=drain_timeout,
        deepseek_vision_journal=vision_journal,
        deepseek_vision_gateway_secret=vision_secret,
        deepseek_vision_drain_timeout_seconds=vision_drain_timeout,
        deepseek_vision_internal_port=vision_internal_port,
        h3_queue_url=h3_queue_url,
        music3_root=_absolute(music["root"], "Music 3 root"),
        music3_python=_absolute(music["python"], "Music 3 python"),
        music3_port=music_port,
        personaplex_root=_absolute(persona["root"], "PersonaPlex root"),
        authority_id=authority_id,
        authority_record=_absolute(authority["record"], "authority record"),
        authority_credential=_absolute(authority["credential"], "authority credential"),
        authority_remote_fence_path=_absolute(
            authority.get("remote_fence_path", "/home/operator/.local/state/dgx-cluster-gateway/authority.json"),
            "remote authority fence path",
        ),
        authority_remote_fence_enabled=remote_fence_enabled,
        mutation_enabled=bool(authority.get("mutation_enabled", False)),
        acceptance_failure_model=str(failure_model) if failure_model is not None else None,
        nodes=nodes,
        lan_no_auth=bool(gateway.get("lan_no_auth", False)),
        drama_enabled=drama_enabled,
        text_enabled=text.get("enabled", False),
        text_qualification_file=qualification_file,
        text_qualification_sha256=qualification_sha,
    )
