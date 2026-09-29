"""Invoked inside the new container. Does not download or mutate models."""
import argparse
import json
import os
from pathlib import Path
from common import STAGES, model_args

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--stage',choices=STAGES,required=True)
    p.add_argument('--rank',type=int,choices=range(4),required=True)
    p.add_argument('--master',required=True)
    p.add_argument('--port',type=int,required=True)
    p.add_argument('--master-port',type=int,required=True)
    p.add_argument('--dry-run',action='store_true')
    a=p.parse_args()
    if not os.environ.get('VLLM_API_KEY'):
        raise SystemExit('VLLM_API_KEY is required')
    model=Path('/models/DeepSeek-V4.1-Flash')
    config=json.loads((model/'config.json').read_text())
    if config.get('model_type')!='deepseek_v41' or config.get('quantization_config',{}).get('expert_dtype')!='fp4':
        raise SystemExit('Expected official native FP4/FP8 DeepSeek V4.1, not EXL3')
    for k in ('model.safetensors.index.json','model-00047-of-00048.safetensors','model-00048-of-00048.safetensors'):
        if not (model/k).is_file():raise SystemExit(f'Missing {k}')
    cmd=model_args(a.stage,a.rank,a.master,a.port,a.master_port)
    print(json.dumps({'stage':a.stage,'rank':a.rank,'argv':cmd},ensure_ascii=False),flush=True)
    if not a.dry_run:os.execvp(cmd[0],cmd)

if __name__=='__main__':main()
