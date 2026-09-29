#!/usr/bin/env python3
"""Bounded local Claude Code acceptance after selecting the Qwen CC Switch provider."""
from pathlib import Path
import argparse,json,os,sqlite3,subprocess,tempfile,time
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'plans/qwen38-flash-next/evidence/claude-code'
CLI=Path.home()/'.nvm/versions/node/v24.14.0/bin/claude'
parser=argparse.ArgumentParser();parser.add_argument('--tool',action='store_true')
parser.add_argument('--cli',type=Path,default=CLI)
parser.add_argument('--model');parser.add_argument('--output',type=Path,default=OUT)
parser.add_argument('--expected-context',type=int)
args=parser.parse_args();CLI=args.cli;OUT=args.output
version=subprocess.check_output([str(CLI),'--version'],text=True).strip()
settings=json.loads((Path.home()/'.claude/settings.json').read_text())
assert settings['env']['ANTHROPIC_BASE_URL']=='http://127.0.0.1:15721'
c=sqlite3.connect('file:'+str(Path.home()/'.cc-switch/cc-switch.db')+'?mode=ro',uri=True)
current=c.execute("select id from providers where app_type='claude' and is_current=1").fetchone();c.close();assert current==('qwen38-flash-next-dgx-local',),current
with tempfile.TemporaryDirectory(prefix='qwen38-claude-acceptance-') as folder:
    work=Path(folder);(work/'fixture.txt').write_text('verification_code=QWEN-DGX-4729\n')
    command=[str(CLI),'-p','--output-format',('stream-json' if args.tool else 'json'),'--no-session-persistence','--strict-mcp-config','--mcp-config','{"mcpServers":{}}','--setting-sources','user','--disable-slash-commands','--no-chrome']
    if args.model:command+=['--model',args.model]
    if args.tool:
        command+=['--verbose','--tools','Read','--allowedTools','Read','--','请使用 Read 工具读取当前目录的 fixture.txt，回复其中的 verification_code。不要读取其他文件。']
    else:
        command+=['--tools','','--','只回答 17×19 的结果，不要解释。']
    environment=dict(os.environ)
    for key in list(environment):
        if key.startswith('ANTHROPIC_'):environment.pop(key)
    start=time.monotonic();result=subprocess.run(command,cwd=work,env=environment,text=True,capture_output=True,timeout=180)
    name='tool' if args.tool else 'answer'
    try:
        if args.tool:
            events=[json.loads(line) for line in result.stdout.splitlines() if line.strip()]
            payload=next(event for event in reversed(events) if event.get('type')=='result')
            tool_blocks=[block for event in events for block in event.get('message',{}).get('content',[]) if isinstance(block,dict) and block.get('type') in ('tool_use','tool_result')]
        else:payload=json.loads(result.stdout);tool_blocks=[]
    except (json.JSONDecodeError,StopIteration):payload={'raw_output':result.stdout[:2000]};tool_blocks=[]
    evidence={'exit_code':result.returncode,'elapsed_seconds':time.monotonic()-start,'claude_code_version':version,'settings_source':'actual user settings via CC Switch','simulation':False,'result':payload,'tool_blocks':tool_blocks,'stderr':result.stderr[-2000:]}
    OUT.mkdir(parents=True,exist_ok=True);(OUT/(name+'.json')).write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
    print(json.dumps(evidence,ensure_ascii=False),flush=True)
    assert result.returncode==0 and not payload.get('is_error'), 'Claude Code request failed'
    if args.tool:assert any(block.get('name')=='Read' and Path(block.get('input',{}).get('file_path','')).name=='fixture.txt' for block in tool_blocks),'No actual Read call observed'
    answer=payload.get('result','')
    assert ('QWEN-DGX-4729' if args.tool else '323') in answer, 'Unexpected model answer'
    if args.expected_context:
        assert any(row.get('contextWindow')==args.expected_context for row in payload.get('modelUsage',{}).values()), 'Client effective context mismatch'
