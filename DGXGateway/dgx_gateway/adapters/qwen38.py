"""Qwen TP2 context profiles using the existing Gateway resource authority."""
from dataclasses import replace
import json
import time
import urllib.request
from .deepseek_vision import DeepSeekVisionAdapter, VisionAdapterSpec
from .loader import load_module


class Qwen38Adapter(DeepSeekVisionAdapter):
    model_id = 'qwen38-flash-next'
    context_profiles = {'32K': 32768, '256K': 262144}
    profiles = tuple(context_profiles)
    default_profile = '256K'

    def __init__(self, config, ssh=None):
        # Reuse only paired observation/drain/proof transport. Qwen owns its recipe.
        adapted = replace(config, deepseek_vision_internal_port=18890,
                          deepseek_vision_gateway_secret=config.deepseek_gateway_secret,
                          deepseek_vision_drain_timeout_seconds=900)
        super().__init__(adapted, ssh, spec=VisionAdapterSpec(
            model_id=self.model_id, compose_project=self.model_id, compose_service='qwen',
            systemd_unit=self.model_id + '.service', controller_module='dgx_qwen38_controller',
            controller_path='scripts/control-qwen38-flash-next.py'))

    def _controller(self, context):
        if self._module is None:
            self._module = load_module(self.spec.controller_module, self.config.repo_root / self.spec.controller_path)
        module = self._module
        runner = module.SubprocessRunner()
        authority = module.LockAuthority(runner=runner, token_file=context.token_path,
            owner='dgx-cluster-gateway', lock_root=self.config.lock_root, lock_script=self.config.lock_cli,
            lock_history=self.config.lock_root.parent / (self.config.lock_root.name + '-history'))
        return module.ClusterController(authority=authority, runner=runner,
            journal=module.Journal(self.config.state_dir / 'qwen38-lifecycle.jsonl'),
            task_id=context.operation_id, gateway_secret=self.config.deepseek_gateway_secret)

    def start(self, context, profile=None):
        profile = profile or self.default_profile
        if profile not in self.context_profiles:
            raise ValueError('invalid Qwen context profile')
        context.guard()
        controller = self._controller(context)
        controller.start(context.operation_id, profile=profile)
        controller.production_activate_gateway(context.operation_id)
        observed = self.observe()
        if observed.state != 'HEALTHY' or observed.profile != profile:
            raise RuntimeError('Qwen paired API identity/profile verification failed')

    def drain(self, context):
        context.guard()
        state = self.observe().state
        if state == 'STOPPED':
            return
        if state != 'HEALTHY':
            raise RuntimeError('Qwen drain requires a healthy paired runtime')
        self._controller(context).drain_admission(context.operation_id)
        # Admission is now closed. Do not call observe() again (models returns 503).
        deadline, consecutive_idle = time.monotonic() + 900, 0
        while time.monotonic() < deadline:
            context.guard()
            running, waiting = self._request_metrics()
            consecutive_idle = consecutive_idle + 1 if running == waiting == 0 else 0
            if consecutive_idle >= 3:
                return
            time.sleep(1)
        raise TimeoutError('Qwen active requests did not drain')

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
