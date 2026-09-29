import io
import json
import unittest
from unittest import mock

from scripts.codex_compatibility_probe import (
    assert_completed_visible,
    collect_reasoning_text,
    count_tokens,
    find_function_call,
)


class CodexCompatibilityProbeTests(unittest.TestCase):
    def test_finds_exact_namespaced_function_call(self):
        response = {
            "output": [
                {"type": "message", "content": []},
                {
                    "type": "function_call",
                    "name": "mcp__diagnostic__echo",
                    "arguments": "{\"value\":\"NAMESPACE_OK\"}",
                },
            ]
        }
        call = find_function_call(response, "mcp__diagnostic__echo")
        self.assertEqual(json.loads(call["arguments"]), {"value": "NAMESPACE_OK"})

    def test_rejects_prose_only_response(self):
        response = {
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "I would call echo"}]}]
        }
        with self.assertRaisesRegex(AssertionError, "mcp__diagnostic__echo"):
            find_function_call(response, "mcp__diagnostic__echo")

    def test_collects_reasoning_across_items_and_parts(self):
        response = {
            "output": [
                {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "first"}]},
                {"type": "reasoning", "summary": [{"type": "summary_text", "text": "second"}]},
            ]
        }
        self.assertEqual(collect_reasoning_text(response), "first\nsecond")

    def test_requires_completed_status_and_visible_output(self):
        with self.assertRaisesRegex(AssertionError, "completed"):
            assert_completed_visible({"status": "incomplete", "output": []})
        with self.assertRaisesRegex(AssertionError, "visible"):
            assert_completed_visible({"status": "completed", "output": []})
        assert_completed_visible({
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "done"}]}],
        })

    @mock.patch("scripts.codex_compatibility_probe.urllib.request.urlopen")
    def test_counts_server_tokens(self, urlopen):
        urlopen.return_value.__enter__.return_value = io.BytesIO(b'{"tokens":[10,20,30,40]}')
        self.assertEqual(count_tokens("http://127.0.0.1:18084/v1", "hello"), 4)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:18084/tokenize")
        self.assertEqual(json.loads(request.data), {"content": "hello"})


if __name__ == "__main__":
    unittest.main()
