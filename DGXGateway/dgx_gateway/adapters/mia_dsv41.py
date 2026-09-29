"""Mia Abliterated TP2 context profiles using the existing Gateway resource authority."""
from dataclasses import replace
import hashlib
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from ..config import paired_nodes
from .deepseek_vision import DeepSeekVisionAdapter, VisionAdapterSpec
from .loader import load_module


class MiaDsv41Adapter(DeepSeekVisionAdapter):
    model_id = 'mia-dsv41-ablit'
    context_profiles = {'256K': 262144, '512K': 524288}
    profiles = tuple(context_profiles)
    default_profile = '256K'
    RANK = {'head': 'dsv41-exl3-head', 'worker': 'dsv41-exl3-worker'}

    def __init__(self, config, ssh=None):
        adapted = replace(config, deepseek_vision_internal_port=8888,
                          deepseek_vision_gateway_secret=config.deepseek_gateway_secret,
                          deepseek_vision_drain_timeout_seconds=900)
        super().__init__(adapted, ssh, spec=VisionAdapterSpec(
            model_id=self.model_id, compose_project=self.model_id, compose_service='mia',
            systemd_unit=self.model_id + '-gateway.service', controller_module='dgx_mia_dsv41_controller',
            controller_path='scripts/control-mia-dsv41-ablit.py'))

    def _controller(self, context):
        if self._module is None:
            self._module = load_module(self.spec.controller_module, self.config.repo_root / self.spec.controller_path)
        module = self._module
        runner = module.SubprocessRunner()
        authority = module.LockAuthority(runner=runner, token_file=context.token_path,
            owner='dgx-cluster-gateway', lock_root=self.config.lock_root, lock_script=self.config.lock_cli,
            lock_history=self.config.lock_root.parent / (self.config.lock_root.name + '-history'))
        return module.ClusterController(authority=authority, runner=runner,
            journal=module.Journal(self.config.state_dir / 'mia-dsv41-lifecycle.jsonl'),
            task_id=context.operation_id, gateway_secret=self.config.deepseek_gateway_secret)

    def _probe(self):
        def probe_node(node):
            name = self.RANK[node.role]
            unit_command = ''
            if node.role == 'head':
                unit_command = (
                    '(systemctl show mia-dsv41-ablit-gateway.service '
                    '--property=LoadState --property=ActiveState --value 2>/dev/null || true) | sed "s/^/UNIT\\t/"'
                )
            script = '\n'.join((
                'set -eu',
                "printf 'HOST\\t'; hostname",
                "printf 'USER\\t'; whoami",
                f"sudo -n docker ps --filter name=^/{name}$ --format 'CONTAINER\\t{{{{json .}}}}'",
                "ss -H -ltn '( sport = :8888 )' | sed 's/^/LISTENER\\t/'",
                unit_command,
            ))
            result = self.ssh.run(node, ['sh', '-c', script], timeout=12)
            containers, listeners, unit_values = [], [], []
            hostname = user = None
            parse_ok = True
            for line in result.stdout.splitlines():
                kind, separator, value = line.partition('\t')
                if not separator:
                    parse_ok = False
                    continue
                if kind == 'HOST':
                    hostname = value.strip()
                elif kind == 'USER':
                    user = value.strip()
                elif kind == 'CONTAINER':
                    try:
                        item = json.loads(value)
                    except json.JSONDecodeError:
                        parse_ok = False
                    else:
                        if not isinstance(item, dict):
                            parse_ok = False
                        else:
                            containers.append(item)
                elif kind == 'LISTENER':
                    listeners.append(value)
                elif kind == 'UNIT':
                    if value.strip():
                        unit_values.append(value.strip())
                else:
                    parse_ok = False
            identity_ok = hostname == node.hostname and user == node.user
            return node.role, {
                'ok': result.returncode == 0 and parse_ok and identity_ok,
                'identity_ok': identity_ok,
                'containers': containers,
                'listeners': listeners,
                'unit_values': unit_values,
                'stderr_sha256': hashlib.sha256(result.stderr.encode()).hexdigest(),
            }

        targets = paired_nodes(self.config.nodes)
        with ThreadPoolExecutor(max_workers=len(targets)) as executor:
            return dict(executor.map(probe_node, targets))

    def start(self, context, profile=None):
        profile = profile or self.default_profile
        if profile not in self.context_profiles:
            raise ValueError('invalid Mia context profile')
        context.guard()
        controller = self._controller(context)
        controller.start(context.operation_id, profile=profile)
        controller.production_activate_gateway(context.operation_id)
        observed = self.observe()
        if observed.state != 'HEALTHY' or observed.profile != profile:
            raise RuntimeError('Mia paired API identity/profile verification failed')

    def drain(self, context):
        context.guard()
        state = self.observe().state
        if state == 'STOPPED':
            return
        if state != 'HEALTHY':
            raise RuntimeError('Mia drain requires a healthy paired runtime')
        self._controller(context).drain_admission(context.operation_id)
        deadline, consecutive_idle = time.monotonic() + 900, 0
        while time.monotonic() < deadline:
            context.guard()
            running, waiting = self._request_metrics()
            consecutive_idle = consecutive_idle + 1 if running == waiting == 0 else 0
            if consecutive_idle >= 3:
                return
            time.sleep(1)
        raise TimeoutError('Mia active requests did not drain')

    def health(self, context):
        context.guard()
        observed = self.observe()
        if observed.state != 'HEALTHY':
            return {'healthy': False, 'model_id': self.model_id}
        token = self._read_token()
        head = next(node for node in self.config.nodes if node.role == 'head')
        request = urllib.request.Request(f'http://{head.address}:11434/v1/chat/completions',
            data=json.dumps({'model': self.model_id, 'messages': [{'role': 'user', 'content': '17乘以19等于多少？只输出数字。'}],
                             'max_tokens': 256, 'temperature': 0}).encode(),
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=90) as response:
            value = json.loads(response.read(1024 * 1024))
        context.guard()
        choice = value['choices'][0]
        passed = choice['message']['content'].strip() == '323' and choice['finish_reason'] == 'stop'
        return {'healthy': passed, 'model_id': self.model_id, 'canary_passed': passed,
                'context': self.context_profiles[observed.profile], 'tp': 2, 'profile': observed.profile}
