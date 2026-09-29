"""Small deterministic four-rank CUDA collectives, no model load."""
import datetime
import json
import os
import time
from pathlib import Path

def main():
    import torch
    import torch.distributed as dist
    if os.environ.get('NCCL_NET')!='Mesh' or os.environ.get('NCCL_ALGO')!='Ring':
        raise RuntimeError('Mesh/Ring must be explicitly selected')
    torch.cuda.set_device(0)
    if torch.cuda.get_device_capability(0)!=(12,1):raise RuntimeError('Expected GB10 SM121')
    rank=int(os.environ['RANK']);world=int(os.environ['WORLD_SIZE'])
    if world!=4:raise RuntimeError('Expected WORLD_SIZE=4')
    dist.init_process_group('nccl',timeout=datetime.timedelta(seconds=180),device_id=torch.device('cuda',0))
    if torch.cuda.nccl.version()!=(2,29,7):raise RuntimeError(f'Unexpected loaded NCCL: {torch.cuda.nccl.version()}')
    maps=Path('/proc/self/maps').read_text()
    paths=sorted({x.split()[-1] for x in maps.splitlines() if '/libnccl.so' in x})
    if not paths or any('/opt/nccl-mesh/lib/' not in x for x in paths):
        raise RuntimeError(f'Wrong/duplicate NCCL mapping: {paths}')
    for mib in (1,16,128):
        count=mib*1024*1024//4
        x=torch.full((count,),float(rank+1),device='cuda')
        dist.all_reduce(x);torch.cuda.synchronize()
        assert torch.all(x==10).item(), 'all_reduce corruption'
        inp=torch.full((count,),float(rank),device='cuda')
        out=torch.empty((count*world,),device='cuda')
        dist.all_gather_into_tensor(out,inp);torch.cuda.synchronize()
        for r in range(world):assert torch.all(out[r*count:(r+1)*count]==r).item(),'all_gather corruption'
        inp2=torch.cat([torch.full((count,),float(r+rank),device='cuda') for r in range(world)])
        out2=torch.empty((count,),device='cuda')
        dist.reduce_scatter_tensor(out2,inp2);torch.cuda.synchronize()
        assert torch.all(out2==(4*rank+6)).item(),'reduce_scatter corruption'
        for src in range(world):
            x.fill_(src+11 if rank==src else 0)
            dist.broadcast(x,src);torch.cuda.synchronize()
            assert torch.all(x==src+11).item(),'broadcast corruption'
        elapsed=[]
        for _ in range(5):
            x.fill_(rank+1);torch.cuda.synchronize();t=time.perf_counter()
            dist.all_reduce(x);torch.cuda.synchronize();elapsed.append(time.perf_counter()-t)
            assert torch.all(x==10).item()
        record={'rank':rank,'MiB':mib,'all_reduce_seconds_mean':sum(elapsed)/len(elapsed),'correct':True}
        print(json.dumps(record),flush=True)
        del x,inp,out,inp2,out2
    dist.barrier();torch.cuda.synchronize()
    print(f'COLLECTIVES_PASS rank={rank} world_size={world} nccl=2.29.7',flush=True)
    dist.destroy_process_group()

if __name__=='__main__':main()
