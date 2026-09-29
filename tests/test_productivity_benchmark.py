import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "productivity_benchmark", ROOT / "scripts" / "productivity_benchmark.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None


class ProductivityBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        SPEC.loader.exec_module(MODULE)

    def test_extracts_python_fence_without_surrounding_prose(self):
        text = "Here is the fix:\n```python\ndef solve(x):\n    return x + 1\n```\nDone."
        self.assertEqual(MODULE.extract_code(text), "def solve(x):\n    return x + 1")

    def test_reference_solutions_pass_all_hidden_tests(self):
        for task_id, task in MODULE.TASKS.items():
            result = MODULE.grade_solution(task_id, task.reference_solution)
            self.assertTrue(result["passed"], (task_id, result))
            self.assertGreaterEqual(result["tests_run"], 6)

    def test_incorrect_solution_fails_hidden_tests(self):
        result = MODULE.grade_solution(
            "coalesce_ranges", "def coalesce_ranges(ranges, gap=0):\n    return ranges"
        )
        self.assertFalse(result["passed"])
        self.assertGreater(result["tests_failed"], 0)

    def test_unsafe_import_is_rejected_before_execution(self):
        result = MODULE.grade_solution(
            "coalesce_ranges", "import os\ndef coalesce_ranges(ranges, gap=0):\n    return []"
        )
        self.assertFalse(result["passed"])
        self.assertIn("unsafe syntax", result["error"])

    def test_ranking_prefers_more_correct_tasks_before_speed(self):
        rows = [
            {"profile": "fast-wrong", "passed_tasks": 2, "total_seconds": 10.0},
            {"profile": "slow-correct", "passed_tasks": 3, "total_seconds": 100.0},
            {"profile": "fast-correct", "passed_tasks": 3, "total_seconds": 80.0},
        ]
        ranked = MODULE.rank_profiles(rows)
        self.assertEqual([row["profile"] for row in ranked], ["fast-correct", "slow-correct", "fast-wrong"])

    def test_request_uses_low_reasoning_and_deterministic_generation(self):
        request = MODULE.build_request("model-id", "task prompt")
        self.assertEqual(request["model"], "model-id")
        self.assertEqual(request["reasoning_effort"], "low")
        self.assertEqual(request["seed"], 424242)
        self.assertEqual(request["temperature"], 0)
        self.assertGreaterEqual(request["max_tokens"], 2048)

    def test_profile_summary_counts_correctness_before_latency(self):
        rows = [
            {"task_id": "a", "grade": {"passed": True}, "elapsed_seconds": 10.0},
            {"task_id": "b", "grade": {"passed": False}, "elapsed_seconds": 4.0},
            {"task_id": "c", "grade": {"passed": True}, "elapsed_seconds": 6.0},
        ]
        summary = MODULE.summarize_profile("candidate", rows)
        self.assertEqual(summary["passed_tasks"], 2)
        self.assertEqual(summary["total_tasks"], 3)
        self.assertEqual(summary["total_seconds"], 20.0)
        self.assertEqual(summary["seconds_per_passed_task"], 10.0)


if __name__ == "__main__":
    unittest.main()
