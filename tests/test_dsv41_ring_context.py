import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "scripts" / "test-dsv41-ring-context.py"
SPEC = importlib.util.spec_from_file_location("dsv41_ring_context", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class Dsv41RingContextTests(unittest.TestCase):
    def test_extracts_chat_message_text(self):
        payload = {"choices": [{"message": {"content": "DGX_DSV41_RING_SENTINEL_RECALL"}}]}
        self.assertEqual(MODULE.extract_message_text(payload), "DGX_DSV41_RING_SENTINEL_RECALL")

    def test_prompt_keeps_sentinel_at_both_ends(self):
        prompt = MODULE.build_prompt("DGX_DSV41_RING_SENTINEL_01_00", 12)
        self.assertTrue(prompt.startswith("开头验证码是 DGX_DSV41_RING_SENTINEL_01_00"))
        self.assertIn("现在只输出开头验证码", prompt)
        self.assertEqual(prompt.count("的"), 12)

    def test_concurrency_marks_stable_only_when_all_recall(self):
        replies = iter(
            [
                {"ok": True, "sentinel": "A", "sentinel_recalled": True, "response": "A"},
                {"ok": True, "sentinel": "B", "sentinel_recalled": False, "response": "nope"},
            ]
        )
        with patch.object(MODULE, "one_request", side_effect=lambda *args, **kwargs: next(replies)):
            result = MODULE.run_concurrency("http://127.0.0.1:8891/v1", "k", MODULE.MODEL, 2, 8, 16, 10)
        self.assertFalse(result["stable"])
        self.assertEqual(result["recalled"], 1)
        self.assertEqual(result["concurrency"], 2)

    def test_ramp_stops_at_first_unstable_level(self):
        def fake_run(_url, _key, _model, concurrency, *_rest):
            return {"concurrency": concurrency, "stable": concurrency < 4, "recalled": concurrency if concurrency < 4 else 0, "total": concurrency, "failures": [], "results": []}

        with patch.object(MODULE, "run_concurrency", side_effect=fake_run):
            result = MODULE.ramp("http://127.0.0.1:8891/v1", "k", MODULE.MODEL, 2, 6, 8, 16, 10)
        self.assertEqual(result["max_stable_concurrency"], 3)
        self.assertEqual([row["concurrency"] for row in result["runs"]], [2, 3, 4])


if __name__ == "__main__":
    unittest.main()
