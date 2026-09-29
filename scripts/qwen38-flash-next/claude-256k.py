#!/usr/bin/env python3
"""Launch the installed modern client through the active Qwen CC Switch provider."""
from pathlib import Path
import importlib.util
import json
import os
import sqlite3
import sys

native = Path.home()/'Library/Application Support/Claude-3p/claude-code/2.1.260/claude.app/Contents/MacOS/claude'
if not native.is_file():
    raise SystemExit('未找到已验收的 Claude Code 2.1.260；请先检查 Desktop 安装版本。')
settings = json.loads((Path.home()/'.claude/settings.json').read_text())
if settings.get('env',{}).get('ANTHROPIC_BASE_URL') != 'http://127.0.0.1:15721':
    raise SystemExit('请先在 CC Switch 的 Claude Code 页面启用 Qwen 本地供应商。')
with sqlite3.connect('file:'+str(Path.home()/'.cc-switch/cc-switch.db')+'?mode=ro',uri=True) as database:
    current = database.execute("select id from providers where app_type='claude' and is_current=1").fetchone()
    if current != ('qwen38-flash-next-dgx-local',):
        raise SystemExit('CC Switch 当前 Claude Code 供应商不是 Qwen，请先切换。')
spec = importlib.util.spec_from_file_location('qwen_gateway_client',Path(__file__).with_name('gateway-client.py'))
module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
active = module.client()('/api/v1/snapshot')['active_local_model']
if active.get('state') != 'ACTIVE' or active.get('model_id') != 'qwen38-flash-next' or active.get('profile') != '256K':
    raise SystemExit('请先在 DGX Gateway 启动 Qwen 256K，并等待健康检查通过。')
environment = {k:v for k,v in os.environ.items() if not k.startswith('ANTHROPIC_')}
os.execve(native,[str(native),'--model','qwen38-flash-next',*sys.argv[1:]],environment)
