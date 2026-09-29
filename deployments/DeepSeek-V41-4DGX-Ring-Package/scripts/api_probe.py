#!/usr/bin/env python3
"""Authenticated API smoke and real-token-count needle probes; never executes generated code/tools."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

MODEL = 'deepseek-v4.1-flash-ring'

def key_from_file(path: str) -> str:
    lines = Path(path).read_text().splitlines()
    values = [x.split('=', 1)[1] for x in lines if x.startswith('VLLM_API_KEY=')]
    if len(values) != 1 or not values[0]:
        raise ValueError('Missing/duplicate VLLM_API_KEY in env file')
    return values[0]

def request_json(base: str, key: str, path: str, payload: dict | None = None, timeout: int = 600) -> dict:
    req = urllib.request.Request(base.rstrip('/') + path,
          data=json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None,
          headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        body = exc.read(2000).decode(errors='replace')
        raise RuntimeError(f'HTTP {exc.code}: {body}') from exc

def chat(base: str, key: str, text: str, *, max_tokens=512, timeout=600, extra=None) -> tuple[dict, float]:
    body = {'model': MODEL, 'messages': [{'role': 'user', 'content': text}],
            'temperature': 0, 'max_tokens': max_tokens, 'reasoning_effort': 'none'}
    if extra:
        body.update(extra)
    t = time.perf_counter()
    result = request_json(base, key, '/v1/chat/completions', body, timeout)
    return result, time.perf_counter() - t

def answer(result: dict) -> str:
    return result['choices'][0]['message'].get('content') or ''

def tokenize(base: str, key: str, text: str) -> int:
    obj = request_json(base, key, '/tokenize', {'model': MODEL, 'prompt': text, 'add_special_tokens': False})
    count = obj.get('count')
    if not isinstance(count, int):
        if isinstance(obj.get('tokens'), list):
            count = len(obj['tokens'])
        else:
            raise RuntimeError('/tokenize returned no usable token count; stop, do not claim a long-context pass')
    return count

def make_needle_text(base: str, key: str, target: int, depth: float, secret: str) -> tuple[str, int]:
    prefix = 'Read this archive. A single record gives the passphrase for project ORBIT.\n'
    filler = 'Ordinary archive record: the sample is stored in cabinet seven.\n'
    needle = f'\nPROJECT ORBIT PASSPHRASE = {secret}\n'
    suffix = '\nReturn only the exact PROJECT ORBIT PASSPHRASE. Do not include punctuation or explanations.'
    def build(n: int):
        left = int(n * depth)
        return prefix + filler * left + needle + filler * (n-left) + suffix
    lo, hi = 0, max(1, target // 8)
    # Tokenizer-only calls. No model generation during budget fitting.
    while tokenize(base, key, build(hi)) < target:
        hi *= 2
        if hi > 2000000:
            raise RuntimeError('Abnormal tokenizer response')
    best, best_count = build(0), tokenize(base, key, build(0))
    while lo <= hi:
        mid = (lo + hi) // 2
        txt = build(mid)
        n = tokenize(base, key, txt)
        if n <= target:
            best, best_count, lo = txt, n, mid + 1
        else:
            hi = mid - 1
    return best, best_count

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['models', 'smoke', 'tools', 'needle'])
    p.add_argument('--base-url', default='http://127.0.0.1:8891')
    p.add_argument('--key-file', default='/srv/dsv41-ring/secrets/api.env')
    p.add_argument('--output', default='api-probe-result.json')
    p.add_argument('--target-tokens', type=int, default=28000, help='Plain prompt tokens; leave >=4096 room below server context')
    p.add_argument('--depth', type=float, default=0.5)
    p.add_argument('--timeout', type=int, default=1800)
    a = p.parse_args()
    u = urllib.parse.urlparse(a.base_url)
    if u.scheme not in ('http','https') or not u.hostname:
        p.error('Invalid base URL')
    key = key_from_file(a.key_file)
    out = {'mode': a.mode, 'model': MODEL, 'results': []}
    if a.mode == 'models':
        out['response'] = request_json(a.base_url, key, '/v1/models')
        if not any(x.get('id') == MODEL for x in out['response'].get('data', [])):
            raise RuntimeError('Expected model ID missing')
    elif a.mode == 'smoke':
        cases = [('arithmetic', 'Compute 17 * 19. Reply only with the integer.', '323'),
                 ('literal', '只输出这三个汉字，不要输出其他内容：环形网', '环形网'),
                 ('code', 'Write a Python function is_even(n: int) -> bool. Return only source code.', None)]
        for name, text, expected in cases:
            r, elapsed = chat(a.base_url, key, text, timeout=a.timeout)
            content = answer(r).strip()
            passed = content == expected if expected else bool(content) and '\ufffd' not in content
            out['results'].append({'case': name, 'pass': passed, 'wall_seconds': elapsed,
                                   'response': r, 'note': 'Code case checks nonempty text, NOT code correctness'})
            if not passed:
                Path(a.output).write_text(json.dumps(out, ensure_ascii=False, indent=2))
                raise RuntimeError(f'Smoke mismatch: {name}; result saved. Never execute returned code automatically')
    elif a.mode == 'tools':
        tool = {'type': 'function', 'function': {'name': 'lookup_test_record', 'description': 'Look up a test record.',
                'parameters': {'type': 'object', 'properties': {'record_id': {'type': 'string'}},
                               'required': ['record_id']}}}
        r, elapsed = chat(a.base_url, key, 'Use lookup_test_record with record_id RING_TEST_17.', timeout=a.timeout,
                          extra={'tools': [tool], 'tool_choice': {'type': 'function','function': {'name': 'lookup_test_record'}}})
        calls = r['choices'][0]['message'].get('tool_calls') or []
        passed = len(calls) >= 1 and calls[0]['function']['name'] == 'lookup_test_record' and \
                 json.loads(calls[0]['function']['arguments']).get('record_id') == 'RING_TEST_17'
        out['results'].append({'pass': passed, 'wall_seconds': elapsed, 'response': r,
                               'note': 'Only validates call parsing. No external function was executed.'})
        if not passed:
            Path(a.output).write_text(json.dumps(out, ensure_ascii=False, indent=2))
            raise RuntimeError('Tool-call schema mismatch')
    else:
        if not 1000 <= a.target_tokens <= 1044000 or not 0.05 <= a.depth <= 0.95:
            p.error('Use target_tokens=1000..1044000, depth=0.05..0.95; leave context headroom')
        secret = secrets.token_hex(16)
        text, plain_tokens = make_needle_text(a.base_url, key, a.target_tokens, a.depth, secret)
        r, elapsed = chat(a.base_url, key, text, max_tokens=128, timeout=a.timeout)
        passed = answer(r).strip() == secret
        actual = r.get('usage', {}).get('prompt_tokens')
        out['results'].append({'pass': passed, 'needle': secret, 'depth': a.depth,
                               'plain_prompt_tokens': plain_tokens, 'actual_prompt_tokens': actual,
                               'wall_seconds': elapsed, 'response': r})
        if not isinstance(actual, int) or actual < plain_tokens * 0.95 or not passed:
            Path(a.output).write_text(json.dumps(out, ensure_ascii=False, indent=2))
            raise RuntimeError('Needle answer/token-count verification failed; no long-context pass claimed')
    Path(a.output).write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print('API_PROBE_PASS (limited to the explicit cases above; not a general quality benchmark)')

if __name__ == '__main__':
    main()
