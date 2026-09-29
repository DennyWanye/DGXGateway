"""Image-build-only installer; writes inside the new image, never the host."""
import ast
import hashlib
import importlib.util
import json
import shutil
from pathlib import Path

PATCHES = {
    'engram.py':'models/deepseek_v4_1/common/engram.py',
    'model_state.py':'models/deepseek_v4_1/nvidia/model_state.py',
    'weight_utils.py':'model_executor/model_loader/weight_utils.py',
    'attention.py':'models/deepseek_v4_1/attention.py',
    'flashinfer_sparse.py':'models/deepseek_v4_1/nvidia/flashinfer_sparse.py',
    'sparse_swa.py':'v1/attention/backends/mla/sparse_swa.py',
    'sparse_attn_indexer.py':'model_executor/layers/sparse_attn_indexer.py',
}

def main():
    spec = importlib.util.find_spec('vllm')
    if not spec or not spec.origin:
        raise RuntimeError('vLLM package missing')
    site = Path(spec.origin).parent
    src = Path('/opt/dsv41-sources')
    jobs = []
    for filename, target in PATCHES.items():
        source = src / ('ring/images/runtime/deepseek41' if filename=='engram.py' else 'recipe/patch') / filename
        jobs.append((source,site/target))
    jobs.append((src/'ring/images/runtime/deepseek41/deepseek_v41_tokenizer.py',site/'tokenizers/deepseek_v41.py'))
    record=[]
    for source,target in jobs:
        if not source.is_file() or not target.is_file():
            raise RuntimeError(f'Missing patch/source target: {source} -> {target}')
        text=source.read_text(); ast.parse(text)
        if source.name=='engram.py' and 'def gather_engram_hashes' not in text:
            raise RuntimeError('Missing day-0 Engram compatibility helper')
        record.append({'source':str(source),'target':str(target),
            'original_sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
            'installed_sha256':hashlib.sha256(source.read_bytes()).hexdigest()})
    for source,target in jobs:
        shutil.copyfile(source,target)
    connector=src/'ring/images/runtime/deepseek41/dsv41_kv_nvme.py'
    ast.parse(connector.read_text())
    out=Path('/opt/dsv41-ring/runtime');out.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(connector,out/connector.name)
    record.append({'source':str(connector),'target':str(out/connector.name),
                  'installed_sha256':hashlib.sha256(connector.read_bytes()).hexdigest()})
    Path('/opt/dsv41-ring/patch-manifest.json').write_text(json.dumps(record,indent=2)+'\n')
    print(f'PATCHES_INSTALLED: {len(jobs)} replacements + one optional connector; vLLM={site}')

if __name__=='__main__':main()
