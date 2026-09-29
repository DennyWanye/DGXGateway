#!/usr/bin/env python3
"""Build the long-input fixture using the pinned offline tokenizer, without a GPU."""
import json
from transformers import AutoTokenizer


def message(count):
    filler = '这是长上下文测试使用的普通背景记录。\n'
    return [{'role': 'user', 'content': filler * (count // 2)
             + '\n唯一校验代号是：琥珀松鼠4729。\n'
             + filler * (count - count // 2)
             + '\n请只输出上文的唯一校验代号。'}]


def main():
    tokenizer = AutoTokenizer.from_pretrained('/model', local_files_only=True,
                                               trust_remote_code=False)
    low, high, selected, token_count = 1, 3200, None, None
    trace = []
    while low <= high:
        middle = (low + high) // 2
        messages = message(middle)
        encoded = tokenizer.apply_chat_template(messages, tokenize=True,
                                                 add_generation_prompt=True)
        # Transformers 5.15 returns a BatchEncoding by default.
        ids = encoded['input_ids'] if hasattr(encoded, 'keys') else encoded
        assert isinstance(ids, list) and ids and isinstance(ids[0], int)
        count = len(ids)
        trace.append({'repetitions': middle, 'tokens': count})
        if count <= 24000:
            selected, token_count, low = messages, count, middle + 1
        else:
            high = middle - 1
    assert selected and 22000 <= token_count <= 24000
    print(json.dumps({'tokenized_input_tokens': token_count, 'trace': trace,
                      'messages': selected, 'expected': '琥珀松鼠4729',
                      'max_tokens': 256, 'inference_verified': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()
