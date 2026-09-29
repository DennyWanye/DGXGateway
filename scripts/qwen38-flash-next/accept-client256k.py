#!/usr/bin/env python3
"""Actual installed Claude Code client, CC Switch, and Qwen long-context gate."""
from pathlib import Path
import json
import os
import sqlite3
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT/'plans/qwen38-flash-next/evidence/context256k'
CLI = Path.home()/'Library/Application Support/Claude-3p/claude-code/2.1.260/claude.app/Contents/MacOS/claude'
config = json.loads((Path.home()/'.claude/settings.json').read_text())
assert config['env']['ANTHROPIC_BASE_URL'] == 'http://127.0.0.1:15721'
assert config['env']['CLAUDE_CODE_MAX_CONTEXT_TOKENS'] == '262144'
assert not config['env'].get('DISABLE_COMPACT')
with sqlite3.connect(Path.home()/'.cc-switch/cc-switch.db') as connection:
    assert connection.execute("select id from providers where app_type='claude' and is_current=1").fetchone()[0] == 'qwen38-flash-next-dgx-local'

expected = '苍鹭松林591783'
background = '这是长上下文测试使用的普通背景记录。\n'
prompt = background*10000 + '\n唯一校验码：'+expected+'。\n' + background*10000
prompt += '\n请仅回复上文唯一校验码。不要调用工具。'
env = {k:v for k,v in os.environ.items() if not k.startswith('ANTHROPIC_')}
with tempfile.TemporaryDirectory(prefix='qwen-client256k-') as work:
    command = [str(CLI), '-p', '--output-format', 'json', '--no-session-persistence',
               '--model', 'qwen38-flash-next', '--tools', '', '--strict-mcp-config',
               '--mcp-config', '{"mcpServers":{}}', '--setting-sources', 'user',
               '--disable-slash-commands', '--no-chrome']
    started = time.monotonic()
    result = subprocess.run(command,input=prompt,cwd=work,env=env,text=True,capture_output=True,timeout=600)
    body = json.loads(result.stdout)
    record = {'simulation':False,'client_version':subprocess.check_output([str(CLI),'--version'],text=True).strip(),
              'settings_source':'actual CC Switch user settings','elapsed_seconds':time.monotonic()-started,
              'exit_code':result.returncode,'result':body,'stderr':result.stderr[-2000:]}
    (OUT/'client-long-result.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
    assert result.returncode == 0 and not body.get('is_error')
    assert expected in body.get('result','')
    assert any(v.get('contextWindow') == 262144 for v in body.get('modelUsage',{}).values())
    usage = body['usage']
    actual_input = sum(usage.get(k,0) for k in ['input_tokens','cache_read_input_tokens','cache_creation_input_tokens'])
    assert 200000 < actual_input < 262144
    print(json.dumps({'client_256k_pass':True,'input_tokens':actual_input,'elapsed_seconds':record['elapsed_seconds'],
                      'effective_context':262144},ensure_ascii=False),flush=True)
