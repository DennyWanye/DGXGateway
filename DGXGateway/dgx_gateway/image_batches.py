"""Versioned candidate identities and observed GPU batching, never inferred from CFG."""
from __future__ import annotations
import copy
import json


def candidate_indices(count, parallelism=2, indices=None):
    if type(count) is not int or count < 1:
        raise ValueError('batch_count must be a positive integer')
    if type(parallelism) is not int or parallelism < 1:
        raise ValueError('parallelism must be a positive integer')
    indices = list(range(count)) if indices is None else indices
    if (not isinstance(indices, list) or len(indices) != count or
            any(type(i) is not int or i < 0 for i in indices) or len(set(indices)) != count):
        raise ValueError('candidate_indices must be unique original indices matching batch_count')
    return indices


def batch_graph(graph, sampler_id, decoder_id, save_id, count=1, parallelism=2, indices=None):
    """Replace independent sampler branches with bounded tensor batches, preserving seeds."""
    indices = candidate_indices(count, parallelism, indices)
    sampler = copy.deepcopy(graph.pop(sampler_id))
    decoder = graph.pop(decoder_id)
    save = graph.pop(save_id)
    previous = None
    for group, start in enumerate(range(0, len(indices), parallelism)):
        sid, did, oid = ((sampler_id, decoder_id, save_id) if group == 0
                         else tuple(str(100 + group * 3 + x) for x in range(3)))
        node = copy.deepcopy(sampler)
        node['class_type'] = 'DGXSeedBatchKSampler'
        node['inputs']['candidate_indices'] = json.dumps(indices[start:start + parallelism])
        if previous is not None:
            node['inputs']['after_batch'] = [previous, 0]
        graph[sid] = node
        graph[did] = copy.deepcopy(decoder)
        graph[did]['inputs']['samples'] = [sid, 0]
        graph[oid] = {'class_type': 'DGXBatchSaveImage', 'inputs': {
            'images': [did, 0], 'batch_info': [sid, 1],
            'filename_prefix': save['inputs']['filename_prefix'] + (f'-g{group + 1:02d}' if group else '')}}
        previous = oid
    return graph


def manifest(workflow):
    groups, candidates = [], []
    for node_id, node in workflow.items():
        if node['class_type'] not in {'DGXSeedBatchKSampler', 'DGXLLaDATurboBatch'}:
            continue
        inputs = node['inputs']
        indices = json.loads(inputs.get('candidate_indices', '[0,1]'))
        candidate_indices(len(indices), 2, indices)
        seed = inputs.get('seed', 42)
        if type(seed) is not int or not 0 <= seed < 2**64:
            raise ValueError('invalid batch seed')
        saves = [sid for sid, save in workflow.items() if save['class_type'] == 'DGXBatchSaveImage'
                 and save['inputs'].get('batch_info') == [node_id, 1]]
        if len(saves) != 1:
            raise ValueError('each GPU batch needs one attributed save node')
        seeds = [(seed + i) % 2**64 for i in indices]
        groups.append({'samplerNode': node_id, 'saveNode': saves[0], 'indices': indices,
                       'seeds': seeds, 'batchSize': len(indices), 'state': 'QUEUED'})
        candidates.extend({'index': i, 'seed': s, 'seedText': str(s), 'state': 'QUEUED'} for i, s in zip(indices, seeds))
    if not groups:
        return None
    if len({c['index'] for c in candidates}) != len(candidates):
        raise ValueError('duplicate image candidates')
    return {'version': 1, 'executionMode': 'gpu_batch', 'candidateCount': len(candidates),
            'parallelLimit': max(g['batchSize'] for g in groups), 'observedParallelism': 0,
            'groups': groups, 'candidates': candidates}


def update_progress(batch, progress):
    """Only evidence from the identity-checked runtime can establish observed parallelism."""
    by_index = {c['index']: c for c in batch['candidates']}
    for group in batch['groups']:
        live = progress.get('groups', {}).get(group['samplerNode'])
        if not isinstance(live, dict):
            continue
        if live.get('indices') != group['indices'] or live.get('seeds') != group['seeds']:
            raise ValueError('runtime candidate identity changed')
        group['state'] = live.get('state', 'UNKNOWN')
        group['runtime'] = live
        actual = live.get('actual_candidate_parallelism', 0)
        if type(actual) is not int or not 0 <= actual <= group['batchSize']:
            raise ValueError('invalid runtime parallelism evidence')
        group['actualParallelism'] = actual
        batch['observedParallelism'] = max(g.get('actualParallelism', 0) for g in batch['groups'])
        for index in group['indices']:
            candidate = by_index[index]
            if candidate['state'] == 'SUCCEEDED':
                continue
            candidate['state'] = {'SAMPLING': 'RUNNING', 'SAMPLED': 'RUNNING',
                                  'SAVED': 'COLLECTING', 'FAILED': 'FAILED'}.get(group['state'], 'UNKNOWN')


def attribution(batch, save_node, output_index):
    if not batch:
        return {}
    group = next((g for g in batch['groups'] if g['saveNode'] == str(save_node)), None)
    if group is None or type(output_index) is not int or not 0 <= output_index < len(group['indices']):
        raise ValueError('unattributed batch output')
    return {'candidate_index': group['indices'][output_index], 'seed': group['seeds'][output_index], 'seedText': str(group['seeds'][output_index])}


def finish_candidates(batch, state, known_terminal):
    if not batch:
        return
    for candidate in batch['candidates']:
        if candidate['state'] in {'SUCCEEDED', 'UNKNOWN', 'FAILED', 'CANCELED'}:
            continue
        # A saved result whose download failed must be recovered, never regenerated.
        if candidate['state'] == 'COLLECTING':
            candidate['state'] = 'UNKNOWN'
        elif known_terminal and state in {'FAILED', 'CANCELED'}:
            candidate['state'] = state
        else:
            candidate['state'] = 'UNKNOWN'
