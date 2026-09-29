from __future__ import annotations

import hashlib
import json
import re
import shlex
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from ..config import NodeConfig, paired_nodes
from ..ssh import FixedSSH


_IDENTITY = re.compile(r"[A-Za-z0-9._-]+")


@dataclass(frozen=True)
class PairedComposeProbeSpec:
    project: str
    service: str
    model_id: str
    unit: str
    internal_port: int

    def __post_init__(self) -> None:
        for value in (self.project, self.service, self.model_id, self.unit):
            if not _IDENTITY.fullmatch(value):
                raise ValueError("invalid paired probe identity")
        if not 1024 <= self.internal_port <= 65535:
            raise ValueError("invalid paired probe port")


def probe_paired_compose_target(
    ssh: FixedSSH,
    nodes: tuple[NodeConfig, ...],
    spec: PairedComposeProbeSpec,
    *,
    timeout: int = 12,
) -> dict[str, dict]:
    """Return an exact, structured two-rank probe without substring matching."""

    def probe_node(node: NodeConfig) -> tuple[str, dict]:
        filters = (
            f"label=com.docker.compose.project={spec.project}",
            f"label=com.docker.compose.service={spec.service}",
            f"label=com.dgx-spark.model-id={spec.model_id}",
            f"label=com.dgx-spark.role={node.role}",
        )
        docker_filters = " ".join(
            f"--filter {shlex.quote(value)}" for value in filters
        )
        unit_command = ""
        if node.role == "head":
            unit_command = (
                "(systemctl show "
                f"{shlex.quote(spec.unit)} --property=LoadState --property=ActiveState "
                "--value 2>/dev/null || true) | sed 's/^/UNIT\\t/'"
            )
        script = "\n".join(
            (
                "set -eu",
                "printf 'HOST\\t'; hostname",
                "printf 'USER\\t'; whoami",
                "sudo -n docker ps "
                f"{docker_filters} --format 'CONTAINER\\t{{{{json .}}}}'",
                "ss -H -ltn "
                f"'( sport = :{spec.internal_port} )' | sed 's/^/LISTENER\\t/'",
                unit_command,
            )
        )
        result = ssh.run(node, ["sh", "-c", script], timeout=timeout)
        containers: list[dict] = []
        listeners: list[str] = []
        unit_values: list[str] = []
        hostname = None
        user = None
        parse_ok = True
        for line in result.stdout.splitlines():
            kind, separator, value = line.partition("\t")
            if not separator:
                parse_ok = False
                continue
            if kind == "HOST":
                hostname = value.strip()
            elif kind == "USER":
                user = value.strip()
            elif kind == "CONTAINER":
                try:
                    item = json.loads(value)
                except json.JSONDecodeError:
                    parse_ok = False
                else:
                    if not isinstance(item, dict):
                        parse_ok = False
                    else:
                        containers.append(item)
            elif kind == "LISTENER":
                listeners.append(value)
            elif kind == "UNIT":
                if value.strip():
                    unit_values.append(value.strip())
            else:
                parse_ok = False
        identity_ok = hostname == node.hostname and user == node.user
        return node.role, {
            "ok": result.returncode == 0 and parse_ok and identity_ok,
            "identity_ok": identity_ok,
            "containers": containers,
            "listeners": listeners,
            "unit_values": unit_values,
            "stderr_sha256": hashlib.sha256(result.stderr.encode()).hexdigest(),
        }

    targets = paired_nodes(nodes)
    with ThreadPoolExecutor(max_workers=len(targets)) as executor:
        return dict(executor.map(probe_node, targets))


def paired_probe_state(probe: dict[str, dict]) -> str:
    if set(probe) != {"head", "worker"} or not all(
        item.get("ok") is True for item in probe.values()
    ):
        return "UNKNOWN"
    head = probe["head"]
    worker = probe["worker"]
    containers = head["containers"] + worker["containers"]
    exact_ranks = (
        len(head["containers"]) == 1
        and len(worker["containers"]) == 1
        and all(str(item.get("Status", "")).startswith("Up ") for item in containers)
    )
    unit_active = "active" in head["unit_values"]
    if exact_ranks and unit_active and len(head["listeners"]) == 1 and not worker["listeners"]:
        return "RUNNING"
    if not containers and not head["listeners"] and not worker["listeners"] and not unit_active:
        return "STOPPED"
    return "PARTIAL"
