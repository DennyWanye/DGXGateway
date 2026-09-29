#!/usr/bin/env python3
"""Bounded real API checks on head loopback. Never execute model-produced code/tools."""
import base64
import hashlib
import json
from pathlib import Path
import struct
import time
import urllib.request
import zlib

ROOT = Path('/home/operator/dgx-models/qwen38-flash-next/control/runtime')
OUT = ROOT / 'functional-results-20260910'
MODEL = 'qwen38-flash-next'
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def metrics():
    names = ('vllm:request_prefill_time_seconds_sum', 'vllm:request_prefill_time_seconds_count',
             'vllm:request_decode_time_seconds_sum', 'vllm:request_decode_time_seconds_count')
    with HTTP.open('http://127.0.0.1:18890/metrics', timeout=10) as response:
        raw = response.read(4 * 1024 * 1024).decode()
    values = {}
    for line in raw.splitlines():
        if line.startswith('#') or not line.strip():
            continue
        sample, value, *_ = line.split()
        name = sample.split('{')[0]
        if name in names:
            values[name] = values.get(name, 0) + float(value)
    return values


def request(path, payload, timeout=180):
    req = urllib.request.Request('http://127.0.0.1:18890' + path,
        data=json.dumps(payload, ensure_ascii=False).encode(), headers={'Content-Type': 'application/json'})
    return HTTP.open(req, timeout=timeout)


def run_case(case):
    payload = {'model': MODEL, 'temperature': 0, **case['request']}
    before = metrics()
    started = time.perf_counter()
    with request('/v1/chat/completions', payload) as response:
        if payload.get('stream'):
            events, first, done = [], None, False
            for raw in response:
                if not raw.startswith(b'data: '):
                    continue
                data = raw[6:].strip()
                if data == b'[DONE]':
                    done = True
                    break
                row = json.loads(data)
                now = time.perf_counter()
                if first is None and any(any(c.get('delta', {}).get(k) for k in ('content', 'reasoning', 'reasoning_content')) for c in row.get('choices', [])):
                    first = now
                events.append(row)
            elapsed = time.perf_counter() - started
            usage = next((r['usage'] for r in reversed(events) if r.get('usage')), None)
            finishes = [c.get('finish_reason') for r in events for c in r.get('choices', []) if c.get('finish_reason')]
            result = {'events': events, 'done': done, 'usage': usage, 'finish_reasons': finishes,
                      'elapsed_seconds': elapsed, 'ttft_seconds': None if first is None else first - started,
                      'completion_tokens_per_total_second': usage['completion_tokens'] / elapsed if usage else None}
            assert done and finishes and usage, 'stream termination or usage missing'
        else:
            result = json.load(response)
            result['elapsed_seconds'] = time.perf_counter() - started
            assert result.get('choices'), 'completion choices missing'
    after = metrics()
    count_key = 'vllm:request_prefill_time_seconds_count'
    for _ in range(24):
        if count_key not in before or after.get(count_key, 0) > before[count_key]:
            break
        time.sleep(0.25)
        after = metrics()
    delta = {k: after[k] - before[k] for k in before if k in after}
    result['server_metric_delta'] = delta
    usage = result.get('usage')
    prefill = delta.get('vllm:request_prefill_time_seconds_sum', 0)
    decode = delta.get('vllm:request_decode_time_seconds_sum', 0)
    if usage and delta.get(count_key) == 1 and prefill > 0:
        result['server_prefill_tokens_per_second'] = usage['prompt_tokens'] / prefill
    if usage and delta.get('vllm:request_decode_time_seconds_count') == 1 and decode > 0:
        result['server_decode_tokens_per_second'] = max(0, usage['completion_tokens'] - 1) / decode
    (OUT / (case['id'] + '.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return {'id': case['id'], 'transport_passed': True, 'elapsed_seconds': result['elapsed_seconds'],
            'usage': result.get('usage'), 'semantic_review_required': True}


def png():
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data) & 0xffffffff)
    pixels = b''.join(b'\0' + bytes([255, 0, 0]) * 128 + bytes([0, 0, 255]) * 128 for _ in range(128))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 256, 128, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b'')


def long_case():
    def message(count):
        filler = '这是长上下文测试使用的普通背景记录。\n'
        return [{'role': 'user', 'content': filler * (count // 2) + '\n唯一校验代号是：琥珀松鼠4729。\n' + filler * (count - count // 2) + '\n请只输出上文的唯一校验代号。'}]
    low, high, selected, count = 1, 3200, None, None
    trace = []
    for _ in range(12):
        middle = (low + high) // 2
        messages = message(middle)
        with request('/tokenize', {'model': MODEL, 'messages': messages, 'add_generation_prompt': True}, timeout=20) as response:
            tokens = json.load(response)['count']
        trace.append({'repetitions': middle, 'tokens': tokens})
        (OUT / 'long-tokenization-trace.json').write_text(json.dumps(trace, indent=2))
        if tokens <= 24000:
            selected, count, low = messages, tokens, middle + 1
        else:
            high = middle - 1
        if low > high:
            break
    assert selected and 22000 <= count <= 24000, 'long fixture did not reach its token budget; see trace'
    (OUT / 'long-input.json').write_text(json.dumps({'tokenized_input_tokens': count, 'messages': selected}, ensure_ascii=False))
    return {'id': 'long', 'request': {'messages': selected, 'max_tokens': 256}}


def main():
    OUT.mkdir(exist_ok=False)
    cases = json.loads((ROOT / 'functional-cases.json').read_text())['cases']
    fixture = png()
    (OUT / 'vision-fixture.png').write_bytes(fixture)
    cases.append({'id': 'vision', 'request': {'messages': [{'role': 'user', 'content': [
        {'type': 'text', 'text': '图像左半边和右半边分别是什么颜色？请简短用中文回答。'},
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(fixture).decode()}}
    ]}], 'max_tokens': 512}})
    summary = []
    for case in cases:
        try:
            row = run_case(case)
        except Exception as error:
            row = {'id': case['id'], 'transport_passed': False, 'error_type': type(error).__name__, 'http_status': getattr(error, 'code', None)}
        summary.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        (OUT / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    try:
        row = run_case(long_case())
    except Exception as error:
        row = {'id': 'long', 'transport_passed': False, 'error_type': type(error).__name__, 'http_status': getattr(error, 'code', None)}
    summary.append(row)
    (OUT / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(row, ensure_ascii=False), flush=True)
    raise SystemExit(0 if all(x['transport_passed'] for x in summary) else 1)


if __name__ == '__main__':
    main()
