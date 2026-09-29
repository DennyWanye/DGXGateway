"""Versioned story validators. Source text and model output are data, never instructions."""
from __future__ import annotations

import hashlib
import json
import math
import re

from .contracts import DramaError

SCHEMA_VERSION = 1
TEMPLATE_VERSION = "drama-story-v1"
SOURCE_BYTES = 256 * 1024
OUTPUT_BYTES = 2 * 1024 * 1024
KINDS = ("source", "entities", "beats", "script", "storyboard")
TASK_KINDS = {"extract_entities": "entities", "draft_beats": "beats", "draft_script": "script", "draft_storyboard": "storyboard", "draft_video_prompt": "video_prompt"}


def fail(path, message, code="story_validation"):
    error = DramaError(code, f"{path}: {message}")
    error.errors = [{"path": path, "code": code, "message": message}]
    raise error


def obj(value, required, optional=(), path="content"):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        fail(path, "字段缺失或包含未知字段")
    return value


def string(value, path, maximum=2000, empty=False):
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value or (not empty and not value.strip()):
        fail(path, f"须为{'非空' if not empty else ''}文本，最多{maximum}字符")
    try:
        value.encode("utf-8")
    except UnicodeError:
        fail(path, "包含无效Unicode字符")
    return value


def number(value, path, low, high):
    if type(value) is not int or not low <= value <= high:
        fail(path, f"须为{low}至{high}整数")
    return value


def array(value, path, maximum, minimum=0):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        fail(path, f"须为{minimum}至{maximum}项列表")
    return value


def local_id(value, path):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
        fail(path, "局部ID须为1至64位字母、数字、下划线或连字符")
    return value


def unique(values, path):
    if len(values) != len(set(values)):
        fail(path, "ID重复")


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def bounded_json(raw):
    if isinstance(raw, str) and len(raw) > OUTPUT_BYTES:
        fail("rawText", "响应超过2MiB", "story_output_limit")
    string(raw, "rawText", OUTPUT_BYTES, empty=True)
    if len(raw.encode("utf-8")) > OUTPUT_BYTES:
        fail("rawText", "响应超过2MiB", "story_output_limit")
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                fail("content", "重复JSON字段", "story_json_invalid")
            out[key] = value
        return out
    def floating(value):
        val = float(value)
        if not math.isfinite(val):
            raise ValueError("nonfinite")
        return val
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_float=floating,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
    except (ValueError, RecursionError) as exc:
        fail("content", "须为单个严格JSON文档", "story_json_invalid")
    check_tree(value)
    if not isinstance(value, dict):
        fail("content", "须为对象")
    return value


def check_tree(value, depth=1):
    if depth > 32:
        fail("content", "JSON深度超过32", "story_depth_limit")
    if isinstance(value, dict):
        for key, child in value.items():
            string(key, "key", OUTPUT_BYTES, empty=True)
            check_tree(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            check_tree(child, depth + 1)
    elif isinstance(value, str):
        string(value, "text", OUTPUT_BYTES, empty=True)
    elif value is not None and type(value) not in (bool, int, float):
        fail("content", "不是JSON类型")
    elif type(value) is float and not math.isfinite(value):
        fail("content", "数值必须有限")


def content_copy(value):
    check_tree(value)
    try:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, OverflowError):
        fail("content", "不是有限JSON内容")
    return bounded_json(raw)
