#!/usr/bin/env python3
"""Real near-limit inference; records token counts, response and memory, never keys."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from DGXGateway.dgx_gateway.config import load_config
from DGXGateway.dgx_gateway.connection_view import _read_shared_api_key

OUT = Path(__file__).resolve().parents[2] / 'plans/qwen38-flash-next/evidence/context256k'
MODEL = 'qwen38-flash-next'
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def main():
    config = load_config(Path.home()/'.config/dgx-cluster-gateway/gateway.toml')
    token = _read_shared_api_key(config.deepseek_gateway_secret)
    def request(path, body=None):
        req = urllib.request.Request('http://192.0.2.30:11434'+path,
            data=None if body is None else json.dumps(body, ensure_ascii=False).encode(),
            headers={'Authorization': 'Bearer '+token, 'Content-Type': 'application/json'})
        with HTTP.open(req, timeout=900) as response:
            return json.load(response)
    models = request('/v1/models')
    assert models['data'][0]['max_model_len'] == 262144
    expected = '琥珀雪松826493'
    background = '这是长上下文测试使用的普通背景记录。\n'
    content = background*13000 + '\n唯一校验码：'+expected+'。\n' + background*13000
    content += '\n请找出上文的唯一校验码，仅回复校验码，不要解释。'
    payload = {'model': MODEL, 'messages': [{'role': 'user', 'content': content}], 'max_tokens': 512, 'temperature': 0}
    raw = json.dumps(payload, ensure_ascii=False).encode()
    started = time.monotonic()
    result = request('/v1/chat/completions', payload)
    evidence = {'simulation': False, 'max_model_len': 262144, 'request_sha256': hashlib.sha256(raw).hexdigest(),
                'needle_position': 'middle', 'expected': expected, 'elapsed_seconds': time.monotonic()-started,
                'result': result}
    (OUT/'near-limit-result.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2)+'\n')
    usage = result['usage']; choice = result['choices'][0]
    assert 250000 <= usage['prompt_tokens'] < 262144
    assert usage['prompt_tokens'] + usage['completion_tokens'] <= 262144
    assert expected in choice['message']['content'] and choice['finish_reason'] == 'stop'
    print(json.dumps({'near_limit_pass': True, 'usage': usage, 'elapsed_seconds': evidence['elapsed_seconds']},ensure_ascii=False),flush=True)
    memory = []
    for ip, host in [('192.0.2.30','dgx-head'), ('192.0.2.31','dgx-worker')]:
        r = subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8','operator@'+ip,
                            'hostname; whoami; cat /proc/meminfo'],text=True,capture_output=True,check=True)
        lines = r.stdout.splitlines(); assert lines[:2] == [host, 'operator']
        available = int(next(x.split()[1] for x in lines if x.startswith('MemAvailable:'))) * 1024
        memory.append({'host':host,'available_bytes':available,'guard_pass':available >= 16*1024**3})
    (OUT/'post-long-memory.json').write_text(json.dumps(memory,indent=2)+'\n')
    assert all(r['guard_pass'] for r in memory)


if __name__ == '__main__':
    main()
