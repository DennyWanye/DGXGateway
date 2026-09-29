"""Small, explicit validators shared by the drama service and HTTP boundary."""
from __future__ import annotations

import re


class DramaError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def text(value, field: str, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise DramaError("invalid_input", f"{field} 不能为空且不能超过 {maximum} 个字符")
    return value.strip()


def integer(value, field: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise DramaError("invalid_input", f"{field} 必须是 {low}–{high} 的整数")
    return value


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}|[a-f0-9]{64}", value):
        raise DramaError("not_found", "找不到指定对象", 404)
    return value


def revision(actual: int, expected) -> None:
    if type(expected) is not int or expected != actual:
        raise DramaError("revision_conflict", "内容已更新，请刷新后再保存", 409)


def fields(body: dict, allowed: set[str], required: set[str] | None = None) -> None:
    if not isinstance(body, dict) or set(body) - allowed or (required or set()) - set(body):
        raise DramaError("invalid_fields", "请求字段缺失或包含不支持的字段")
