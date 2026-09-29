from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from .config import NODE_ROLES, GatewayConfig, NodeConfig
from .ssh import FixedSSH


REMOTE_PROBE = r'''
import json, os, pathlib, pwd, socket, subprocess, sys, time
role, expected_host, expected_user = sys.argv[1:]
if socket.gethostname() != expected_host or pwd.getpwuid(os.getuid()).pw_name != expected_user:
    raise SystemExit(75)
def text(path, default=""):
    try: return pathlib.Path(path).read_text().strip()
    except Exception: return default
def run(argv):
    return subprocess.run(argv, text=True, capture_output=True, timeout=5).stdout.strip()
mem = {}
for line in text('/proc/meminfo').splitlines():
    if ':' in line:
        key, value = line.split(':', 1)
        try: mem[key] = int(value.strip().split()[0]) * 1024
        except Exception: pass
stat = os.statvfs('/')
gpu = run(['nvidia-smi','--query-gpu=temperature.gpu,utilization.gpu,power.draw','--format=csv,noheader,nounits']).split(',')
gpu = [item.strip() for item in gpu]
# Interfaces are enumerated instead of hardcoded: rank hosts differ, and the
# host address is what tells an operator which rail is the model fabric.
try:
    addresses = json.loads(run(['ip','-j','addr']) or '[]')
except Exception:
    addresses = []
FABRIC_MTU = 9000
rails = {}
for device in addresses:
    interface = device.get('ifname') or ''
    if not interface or interface == 'lo':
        continue
    base = pathlib.Path('/sys/class/net') / interface
    ipv4 = [info.get('local') for info in device.get('addr_info', [])
            if info.get('family') == 'inet' and info.get('local')]
    mtu = int(text(base / 'mtu', '0') or 0)
    # Virtual interfaces must never be labelled fabric: a VPN or bridge with a
    # high MTU would otherwise be mistaken for the model interconnect.
    virtual = interface.startswith(('wl', 'docker', 'br-', 'veth', 'tun', 'tap', 'virbr', 'dgx-', 'wg'))
    if interface.startswith('wl'):
        kind = 'management'
    elif interface.startswith('dgx-'):
        kind = 'vpn'
    elif virtual:
        kind = 'virtual'
    elif ipv4 and mtu >= FABRIC_MTU:
        kind = 'fabric'
    elif device.get('link_type') == 'ether':
        kind = 'ethernet'
    else:
        kind = 'other'
    counters = {}
    for name in ('rx_errors','tx_errors','rx_dropped','tx_dropped'):
        try: counters[name] = int(text(base / 'statistics' / name, '0'))
        except Exception: counters[name] = None
    rails[interface] = {
        'kind': kind,
        'ipv4': ipv4,
        'operstate': text(base / 'operstate', 'missing'),
        'mtu': mtu,
        'speed_mbps': int(text(base / 'speed', '0') or 0),
        'risk_counters': counters,
    }
load = os.getloadavg()
print(json.dumps({
    'role': role, 'hostname': socket.gethostname(), 'online': True,
    'boot_id': text('/proc/sys/kernel/random/boot_id'),
    'uptime_seconds': float(text('/proc/uptime','0').split()[0]),
    'cpu_load': {'one': load[0], 'five': load[1], 'fifteen': load[2]},
    'unified_memory': {'total_bytes': mem.get('MemTotal'), 'available_bytes': mem.get('MemAvailable')},
    'disk': {'total_bytes': stat.f_blocks * stat.f_frsize, 'available_bytes': stat.f_bavail * stat.f_frsize},
    'gpu': {
        'temperature_c': float(gpu[0]) if len(gpu)>0 and gpu[0] not in ('','[N/A]','N/A') else None,
        'utilization_percent': float(gpu[1]) if len(gpu)>1 and gpu[1] not in ('','[N/A]','N/A') else None,
        'power_watts': float(gpu[2]) if len(gpu)>2 and gpu[2] not in ('','[N/A]','N/A') else None,
        'memory_kind': 'unified',
    },
    'rails': rails,
}, sort_keys=True))
'''


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class TelemetryCollector:
    def __init__(self, config: GatewayConfig, ssh: FixedSSH | None = None):
        self.config = config
        self.ssh = ssh or FixedSSH()

    def collect_node(self, node: NodeConfig) -> dict[str, Any]:
        sampled_at = utc_now()
        try:
            result = self.ssh.run(
                node,
                ["python3", "-c", REMOTE_PROBE, node.role, node.hostname, node.user],
                timeout=9,
            )
            if result.returncode:
                raise RuntimeError(f"probe exited {result.returncode}")
            value = json.loads(result.stdout)
            value.update({"address": node.address, "sampled_at": sampled_at, "stale": False})
            return value
        except Exception as exc:
            return {
                "role": node.role,
                "hostname": node.hostname,
                "address": node.address,
                "online": False,
                "sampled_at": sampled_at,
                "stale": True,
                "error": type(exc).__name__,
            }

    def collect(self) -> list[dict[str, Any]]:
        values = []
        with ThreadPoolExecutor(max_workers=len(self.config.nodes)) as executor:
            futures = {executor.submit(self.collect_node, node): node for node in self.config.nodes}
            for future in as_completed(futures):
                values.append(future.result())
        return sorted(values, key=lambda value: NODE_ROLES.index(value["role"]) if value["role"] in NODE_ROLES else 99)
