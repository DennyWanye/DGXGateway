"""Real single-rank bootstrap/NCCL probe; no model weights."""
import datetime
import json
import os
import torch
import torch.distributed as dist

assert os.environ['MASTER_ADDR'] == '127.0.0.1'
print('PHASE cuda_set_device', flush=True)
torch.cuda.set_device(0)
print('PHASE init_process_group', flush=True)
dist.init_process_group('nccl', timeout=datetime.timedelta(seconds=30))
try:
    print('PHASE all_reduce', flush=True)
    assert dist.get_world_size() == 1
    value = torch.ones(1, device='cuda')
    dist.all_reduce(value)
    torch.cuda.synchronize()
    assert value.item() == 1
    print(json.dumps({'status': 'PASS', 'master_addr': os.environ['MASTER_ADDR'],
                      'world_size': dist.get_world_size(), 'scope': 'single_rank_nccl_no_model'}))
finally:
    print('PHASE destroy_process_group', flush=True)
    dist.destroy_process_group()
