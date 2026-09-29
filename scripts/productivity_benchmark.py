#!/usr/bin/env python3
"""Executable, deterministic coding-productivity benchmark primitives."""

from __future__ import annotations

import ast
import argparse
import json
import multiprocessing
from pathlib import Path
import re
import time
from typing import NamedTuple
import urllib.request


class Task(NamedTuple):
    prompt: str
    function_name: str
    reference_solution: str


TASKS = {
    "coalesce_ranges": Task(
        prompt="""Implement `coalesce_ranges(ranges, gap=0)` in Python.

Requirements:
- Each input pair is an inclusive integer range; endpoints may be reversed.
- Return normalized `[start, end]` lists sorted by start.
- Merge overlapping ranges and ranges separated by at most `gap` integers.
- Reject a negative gap with ValueError.
- Do not mutate the input.
- Handle empty input and duplicate ranges.

Return only one Python code fence containing the complete function. Do not import modules.""",
        function_name="coalesce_ranges",
        reference_solution="""def coalesce_ranges(ranges, gap=0):
    if gap < 0:
        raise ValueError("gap must be non-negative")
    normalized = sorted([min(a, b), max(a, b)] for a, b in ranges)
    result = []
    for start, end in normalized:
        if not result or start > result[-1][1] + gap + 1:
            result.append([start, end])
        else:
            result[-1][1] = max(result[-1][1], end)
    return result""",
    ),
    "schedule_dag": Task(
        prompt="""Implement `schedule_dag(tasks, limit)` in Python.

`tasks` maps a string task id to an iterable of dependency ids. Return a list
of execution waves, where each wave is a lexicographically sorted list with at
most `limit` tasks. A task may only appear after all dependencies appeared in
earlier waves. When more tasks are ready than fit, choose lexicographically.

Requirements:
- Return [] for an empty mapping.
- Raise ValueError when limit is not positive.
- Raise KeyError for a dependency not present in tasks.
- Raise ValueError for cycles, including self-cycles.
- Do not mutate the mapping or dependency collections.

Return only one Python code fence containing the complete function. Do not import modules.""",
        function_name="schedule_dag",
        reference_solution="""def schedule_dag(tasks, limit):
    if limit <= 0:
        raise ValueError("limit must be positive")
    deps = {name: set(values) for name, values in tasks.items()}
    names = set(deps)
    for values in deps.values():
        for dependency in values:
            if dependency not in names:
                raise KeyError(dependency)
    done = set()
    waves = []
    while len(done) < len(names):
        ready = sorted(name for name in names - done if deps[name] <= done)
        if not ready:
            raise ValueError("cycle")
        wave = ready[:limit]
        waves.append(wave)
        done.update(wave)
    return waves""",
    ),
    "apply_json_patch": Task(
        prompt="""Implement `apply_json_patch(document, operations)` in Python.

Support RFC-6902-style `add`, `remove`, and `replace` operations on dictionaries
and lists. Paths use JSON Pointer: `/` separates segments, `~1` decodes to `/`,
and `~0` decodes to `~`. For list `add`, an integer inserts before that index
and `-` appends. The root path `""` replaces/adds the whole document; removing
the root is invalid.

Requirements:
- Return a modified deep copy; never mutate the input document or operations.
- Raise KeyError for missing dictionary keys or traversal paths.
- Raise IndexError for invalid list indexes.
- Raise ValueError for unsupported operations, malformed paths, or invalid `-` use.

Return only one Python code fence containing the complete function. Do not import modules.""",
        function_name="apply_json_patch",
        reference_solution="""def apply_json_patch(document, operations):
    def clone(value):
        if isinstance(value, dict):
            return {key: clone(item) for key, item in value.items()}
        if isinstance(value, list):
            return [clone(item) for item in value]
        return value
    def parts(path):
        if path == "":
            return []
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("malformed path")
        return [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]
    def index_for(container, token, allow_end=False):
        if token == "-":
            if allow_end:
                return len(container)
            raise ValueError("invalid dash")
        try:
            index = int(token)
        except Exception:
            raise IndexError(token)
        if str(index) != token or index < 0 or index > len(container) - (0 if allow_end else 1):
            raise IndexError(index)
        return index
    result = clone(document)
    for operation in operations:
        kind = operation.get("op")
        path = parts(operation.get("path"))
        if not path:
            if kind in ("add", "replace"):
                if "value" not in operation:
                    raise KeyError("value")
                result = clone(operation["value"])
                continue
            raise ValueError("cannot remove root")
        parent = result
        for token in path[:-1]:
            if isinstance(parent, dict):
                if token not in parent:
                    raise KeyError(token)
                parent = parent[token]
            elif isinstance(parent, list):
                parent = parent[index_for(parent, token)]
            else:
                raise KeyError(token)
        token = path[-1]
        if kind == "add":
            if "value" not in operation:
                raise KeyError("value")
            value = clone(operation["value"])
            if isinstance(parent, dict):
                parent[token] = value
            elif isinstance(parent, list):
                parent.insert(index_for(parent, token, True), value)
            else:
                raise KeyError(token)
        elif kind == "replace":
            if "value" not in operation:
                raise KeyError("value")
            value = clone(operation["value"])
            if isinstance(parent, dict):
                if token not in parent:
                    raise KeyError(token)
                parent[token] = value
            elif isinstance(parent, list):
                parent[index_for(parent, token)] = value
            else:
                raise KeyError(token)
        elif kind == "remove":
            if isinstance(parent, dict):
                if token not in parent:
                    raise KeyError(token)
                del parent[token]
            elif isinstance(parent, list):
                del parent[index_for(parent, token)]
            else:
                raise KeyError(token)
        else:
            raise ValueError("unsupported operation")
    return result""",
    ),
}


def extract_code(text: str) -> str:
    match = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    return (match.group(1) if match else text).strip()


_BLOCKED_NODES = (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal)
_BLOCKED_CALLS = {
    "breakpoint", "compile", "eval", "exec", "getattr", "globals", "input",
    "locals", "open", "setattr", "vars", "__import__",
}


def _validate_ast(code: str) -> ast.AST:
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, _BLOCKED_NODES):
            raise ValueError(f"unsafe syntax: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id in _BLOCKED_CALLS:
            raise ValueError(f"unsafe syntax: {node.id}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ValueError(f"unsafe syntax: {node.attr}")
    return tree


def _cases(task_id: str):
    if task_id == "coalesce_ranges":
        return [
            ([], 0, []),
            ([[5, 1]], 0, [[1, 5]]),
            ([[1, 2], [3, 4]], 0, [[1, 4]]),
            ([[1, 2], [4, 5]], 0, [[1, 2], [4, 5]]),
            ([[10, 8], [1, 3], [5, 4]], 1, [[1, 5], [8, 10]]),
            ([[1, 1], [1, 1], [2, 2]], 0, [[1, 2]]),
        ]
    if task_id == "schedule_dag":
        return [
            ({}, 2, []),
            ({"a": []}, 1, [["a"]]),
            ({"b": [], "a": []}, 1, [["a"], ["b"]]),
            ({"a": [], "b": ["a"], "c": ["a"]}, 2, [["a"], ["b", "c"]]),
            ({"a": [], "b": [], "c": ["a"], "d": ["b"]}, 1,
             [["a"], ["b"], ["c"], ["d"]]),
            ({"build": ["lint", "test"], "lint": [], "test": []}, 3,
             [["lint", "test"], ["build"]]),
        ]
    if task_id == "apply_json_patch":
        return [
            ({"a": 1}, [{"op": "replace", "path": "/a", "value": 2}], {"a": 2}),
            ({"a": [1, 3]}, [{"op": "add", "path": "/a/1", "value": 2}], {"a": [1, 2, 3]}),
            ({"a": [1]}, [{"op": "add", "path": "/a/-", "value": 2}], {"a": [1, 2]}),
            ({"a/b": {"~x": 3}}, [{"op": "remove", "path": "/a~1b/~0x"}], {"a/b": {}}),
            ({"a": 1}, [{"op": "add", "path": "", "value": [1, 2]}], [1, 2]),
            ({"a": {"b": 1}}, [
                {"op": "add", "path": "/a/c", "value": [1]},
                {"op": "replace", "path": "/a/b", "value": 2},
            ], {"a": {"b": 2, "c": [1]}}),
        ]
    raise KeyError(task_id)


def _grade_worker(task_id: str, code: str, queue) -> None:
    try:
        tree = _validate_ast(code)
        safe_builtins = {
            "Exception": Exception, "IndexError": IndexError, "KeyError": KeyError,
            "TypeError": TypeError, "ValueError": ValueError, "all": all, "any": any,
            "bool": bool, "dict": dict, "enumerate": enumerate, "float": float,
            "int": int, "isinstance": isinstance, "len": len, "list": list,
            "max": max, "min": min, "range": range, "reversed": reversed,
            "set": set, "sorted": sorted, "str": str, "sum": sum, "tuple": tuple,
            "zip": zip,
        }
        namespace = {"__builtins__": safe_builtins}
        exec(compile(tree, "<candidate>", "exec"), namespace)
        function = namespace[TASKS[task_id].function_name]
        failures = []
        cases = _cases(task_id)
        for index, case in enumerate(cases):
            expected = case[-1]
            args = case[:-1]
            before = repr(args)
            try:
                actual = function(*args)
                if actual != expected or repr(args) != before:
                    failures.append(index)
            except Exception as exc:
                failures.append({"case": index, "error": type(exc).__name__})
        if task_id == "coalesce_ranges":
            try:
                function([], -1)
                failures.append("negative-gap")
            except ValueError:
                pass
        elif task_id == "schedule_dag":
            for label, args, error in [
                ("limit", ({}, 0), ValueError),
                ("missing", ({"a": ["x"]}, 1), KeyError),
                ("cycle", ({"a": ["b"], "b": ["a"]}, 2), ValueError),
            ]:
                try:
                    function(*args)
                    failures.append(label)
                except error:
                    pass
        else:
            original = {"a": [1]}
            operations = [{"op": "add", "path": "/a/-", "value": {"x": 1}}]
            function(original, operations)
            if original != {"a": [1]} or operations != [{"op": "add", "path": "/a/-", "value": {"x": 1}}]:
                failures.append("mutation")
            for label, args, error in [
                ("missing", ({}, [{"op": "remove", "path": "/x"}]), KeyError),
                ("index", ([], [{"op": "remove", "path": "/0"}]), IndexError),
                ("unsupported", ({}, [{"op": "move", "path": "/x"}]), ValueError),
            ]:
                try:
                    function(*args)
                    failures.append(label)
                except error:
                    pass
        queue.put({
            "passed": not failures,
            "tests_run": len(cases) + (1 if task_id == "coalesce_ranges" else 3),
            "tests_failed": len(failures),
            "failures": failures,
            "error": None,
        })
    except Exception as exc:
        queue.put({
            "passed": False, "tests_run": 0, "tests_failed": 0,
            "failures": [], "error": f"{type(exc).__name__}: {exc}",
        })


def grade_solution(task_id: str, code: str, timeout: float = 3.0) -> dict:
    if task_id not in TASKS:
        raise KeyError(task_id)
    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    process = context.Process(target=_grade_worker, args=(task_id, code, queue))
    process.start()
    process.join(timeout)
    if process.is_alive():
        process.terminate()
        process.join()
        return {
            "passed": False, "tests_run": 0, "tests_failed": 0,
            "failures": [], "error": "TimeoutError: candidate exceeded execution limit",
        }
    if queue.empty():
        return {
            "passed": False, "tests_run": 0, "tests_failed": 0,
            "failures": [], "error": f"candidate exited with code {process.exitcode}",
        }
    return queue.get()


def rank_profiles(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda row: (-int(row["passed_tasks"]), float(row["total_seconds"])))


def build_request(model: str, prompt: str) -> dict:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": "Solve the coding task exactly. Return only the requested Python code fence."},
            {"role": "user", "content": prompt},
        ],
        "reasoning_effort": "low",
        "max_tokens": 4096,
        "seed": 424242,
        "temperature": 0,
        "stream": False,
    }


def summarize_profile(profile: str, rows: list[dict]) -> dict:
    passed = sum(1 for row in rows if row["grade"]["passed"])
    elapsed = round(sum(float(row["elapsed_seconds"]) for row in rows), 3)
    return {
        "profile": profile,
        "passed_tasks": passed,
        "total_tasks": len(rows),
        "pass_rate": round(passed / len(rows), 6) if rows else 0.0,
        "total_seconds": elapsed,
        "seconds_per_passed_task": round(elapsed / passed, 3) if passed else None,
    }


def _post_json(url: str, payload: dict, timeout: int = 1800) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def run_profile(base_url: str, profile: str, alias: str) -> dict:
    rows = []
    for task_id, task in TASKS.items():
        started = time.monotonic()
        response = _post_json(
            base_url.rstrip("/") + "/v1/chat/completions",
            build_request(alias, task.prompt),
        )
        elapsed = time.monotonic() - started
        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        reasoning = message.get("reasoning_content") or ""
        code = extract_code(content)
        grade = grade_solution(task_id, code)
        rows.append({
            "task_id": task_id,
            "elapsed_seconds": round(elapsed, 3),
            "output_characters": len(content),
            "reasoning_characters": len(reasoning),
            "finish_reason": choice.get("finish_reason"),
            "usage": response.get("usage") or {},
            "grade": grade,
            "content": content,
            "reasoning_preview": reasoning[:1000],
        })
    return {"profile": profile, "alias": alias, "tasks": rows,
            "summary": summarize_profile(profile, rows)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--alias", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:18082")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = run_profile(args.base_url, args.profile, args.alias)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
