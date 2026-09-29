#!/usr/bin/env python3
"""Fixed offline TP2 recipe qualified by the Qwen KV8 smoke test."""
import os
import json
from pathlib import Path

role = os.environ['QWEN_ROLE']
if role not in ('head', 'worker'):
    raise SystemExit('invalid node role')
expected_ip = '192.0.2.33' if role == 'head' else '192.0.2.34'
if os.environ.get('VLLM_HOST_IP') != expected_ip:
    raise SystemExit('role/fabric address mismatch')
profile = json.loads(Path('/opt/qwen/profile.json').read_text())['profile']
contexts = {'32K': 32768, '256K': 262144}
if profile not in contexts:
    raise SystemExit('invalid Qwen context profile')

args = ['vllm', 'serve', '/model', '--served-model-name', 'qwen38-flash-next',
        '--distributed-executor-backend', 'mp', '--nnodes', '2',
        '--node-rank', '0' if role == 'head' else '1',
        '--master-addr', '192.0.2.33', '--master-port', '29542',
        '--tensor-parallel-size', '2', '--pipeline-parallel-size', '1',
        '--max-model-len', str(contexts[profile]), '--max-num-seqs', '2', '--max-num-batched-tokens', '4096',
        '--gpu-memory-utilization', '0.70', '--kv-cache-memory-bytes', '8589934592',
        '--enforce-eager', '--no-enable-flashinfer-autotune', '--compilation-config', '{"mode":0}',
        '--moe-backend', 'marlin', '--load-format', 'safetensors', '--safetensors-load-strategy', 'lazy',
        '--limit-mm-per-prompt', '{"image":1,"video":0}',
        '--reasoning-parser', 'qwen3', '--tool-call-parser', 'qwen3_coder', '--enable-auto-tool-choice']
args += ['--host', '127.0.0.1', '--port', '18890'] if role == 'head' else ['--headless']
os.execvp(args[0], args)
