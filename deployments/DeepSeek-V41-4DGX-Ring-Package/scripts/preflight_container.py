"""GPU/container probe; no model load, no downloads."""
import ctypes
import hashlib
import importlib.metadata
import json
import platform
import subprocess
from pathlib import Path

def main():
    import torch
    import flashinfer
    assert platform.machine() in ('aarch64','arm64'), platform.machine()
    assert torch.cuda.is_available() and torch.cuda.device_count()==1
    assert torch.cuda.get_device_capability(0)==(12,1)
    x=torch.ones((128,128),device='cuda',dtype=torch.bfloat16)
    assert (x@x)[0,0].item()==128
    torch.cuda.synchronize()
    nccl=ctypes.CDLL('/opt/nccl-mesh/lib/libnccl.so.2');ver=ctypes.c_int()
    assert nccl.ncclGetVersion(ctypes.byref(ver))==0 and ver.value==22907,ver.value
    ctypes.CDLL('/opt/nccl-mesh/lib/libnccl-net-mesh.so')
    patches=json.loads(Path('/opt/dsv41-ring/patch-manifest.json').read_text())
    for item in patches:
        assert hashlib.sha256(Path(item['target']).read_bytes()).hexdigest()==item['installed_sha256'],item['target']
    import vllm.models.deepseek_v4_1.common.engram as engram
    assert callable(engram.gather_engram_hashes)
    from flashinfer.mla import supported_sparse_mla_sm120_configs
    assert supported_sparse_mla_sm120_configs()['dsv4'].supports_decode(num_heads=16,topk=1152)
    print(json.dumps({'platform':platform.machine(),'torch':torch.__version__,'cuda':torch.version.cuda,
        'vllm':importlib.metadata.version('vllm'),'flashinfer':flashinfer.__version__,
        'nccl':ver.value,'patch_count':len(patches)},indent=2))
    for so in ('libnccl.so.2.29.7','libnccl-net.so'):
        lib=Path('/opt/nccl-mesh/lib')/so
        out=subprocess.check_output(['ldd',str(lib)],text=True)
        if 'not found' in out:raise RuntimeError(out)
        print(out)
    print('CONTAINER_PREFLIGHT_PASS (not a full-model inference pass)')

if __name__=='__main__':main()
