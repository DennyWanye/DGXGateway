import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "model_matrix", ROOT / "scripts" / "model_matrix.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)

BENCH_SPEC = importlib.util.spec_from_file_location(
    "benchmark_model_matrix", ROOT / "scripts" / "benchmark-model-matrix.py"
)
BENCH = importlib.util.module_from_spec(BENCH_SPEC)
assert BENCH_SPEC.loader is not None
BENCH_SPEC.loader.exec_module(BENCH)

REPORT_SPEC = importlib.util.spec_from_file_location(
    "report_model_matrix", ROOT / "scripts" / "report-model-matrix.py"
)
REPORT = importlib.util.module_from_spec(REPORT_SPEC)
assert REPORT_SPEC.loader is not None
REPORT_SPEC.loader.exec_module(REPORT)


class ModelMatrixTests(unittest.TestCase):
    def test_smoke_allows_low_reasoning_to_finish_before_visible_marker(self):
        smoke = (ROOT / "scripts" / "smoke-model-matrix.sh").read_text()
        self.assertIn(r'\"max_tokens\":512', smoke)

    def test_five_profiles_and_exact_new_payload(self):
        profiles = MODULE.profiles()
        self.assertEqual(
            set(profiles),
            {"qwen38-q6", "qwen38-q5", "qwen38-q4", "qwen36-a3b-q4", "gemma4-a4b-q4"},
        )
        self.assertEqual(MODULE.missing_payload_bytes(), 86_614_819_040)

    def test_speculative_profiles_use_the_correct_draft(self):
        profiles = MODULE.profiles()
        for name in ("qwen38-q6", "qwen38-q5", "qwen38-q4"):
            self.assertEqual(profiles[name].runtime, "fastmtp")
            self.assertEqual(profiles[name].draft_id, "qwen38-fastmtp")
            self.assertIn("--spec-draft-n-max", profiles[name].server_args)
        self.assertIsNone(profiles["qwen36-a3b-q4"].draft_id)
        self.assertNotIn("--spec-type", profiles["qwen36-a3b-q4"].server_args)
        self.assertEqual(profiles["gemma4-a4b-q4"].draft_id, "gemma4-mtp")

    def test_qwen36_low_reasoning_has_a_bounded_native_budget(self):
        args = MODULE.profiles()["qwen36-a3b-q4"].server_args
        index = args.index("--reasoning-budget")
        self.assertEqual(args[index + 1], "64")

    def test_gemma4_low_reasoning_has_a_bounded_native_budget(self):
        args = MODULE.profiles()["gemma4-a4b-q4"].server_args
        index = args.index("--reasoning-budget")
        self.assertEqual(args[index + 1], "64")

    def test_all_profiles_are_200k_loopback_and_uncached(self):
        for profile in MODULE.profiles().values():
            args = profile.server_args
            self.assertIn("204800", args)
            self.assertIn("127.0.0.1", args)
            self.assertIn("--no-cache-prompt", args)
            self.assertIn("--no-mmap", args)

    def test_artifact_metadata_is_unique_and_sha256(self):
        artifacts = MODULE.artifacts()
        self.assertEqual(len(artifacts), 7)
        self.assertEqual(len({item.filename for item in artifacts.values()}), 7)
        for artifact in artifacts.values():
            self.assertGreater(artifact.size, 0)
            self.assertEqual(len(artifact.sha256), 64)
            int(artifact.sha256, 16)

    def test_summary_rejects_short_or_cached_runs(self):
        valid = {
            "status": "pass",
            "actual_input_tokens": 200_100,
            "output_tokens": 256,
            "prompt_tps": 100.0,
            "generation_tps": 10.0,
            "sentinel_recalled": True,
            "full_prompt_evaluated": True,
        }
        runs = [dict(valid, generation_tps=value) for value in (9.0, 10.0, 12.0)]
        summary = MODULE.summarize_runs(runs)
        self.assertEqual(summary["status"], "pass")
        self.assertEqual(summary["generation_tps_median"], 10.0)
        runs[0]["full_prompt_evaluated"] = False
        self.assertEqual(MODULE.summarize_runs(runs)["status"], "fail")

    def test_cli_json_is_machine_readable(self):
        payload = MODULE.manifest_dict()
        self.assertEqual(len(json.loads(json.dumps(payload))["profiles"]), 5)

    def test_benchmark_log_parser_requires_full_timing(self):
        log = """
prompt eval time =  222000.00 ms / 200123 tokens ( 901.45 tokens per second)
       eval time =   42000.00 ms /   512 tokens ( 12.19 tokens per second)
draft acceptance = 77.1% ( 394 accepted / 511 generated)
"""
        parsed = BENCH.parse_server_timings(log)
        self.assertEqual(parsed["prompt_tokens"], 200123)
        self.assertEqual(parsed["generation_tokens"], 512)
        self.assertEqual(parsed["draft_accepted"], 394)
        with self.assertRaises(RuntimeError):
            BENCH.parse_server_timings("server started")

    def test_prompt_contains_fixed_sentinel_and_coding_workload(self):
        prompt = BENCH.build_prompt(10)
        self.assertIn(BENCH.SENTINEL, prompt)
        self.assertEqual(prompt.count("async def process_item"), 10)
        self.assertIn("at least 300 tokens", prompt)

    def test_report_renders_measured_medians_and_failure(self):
        aggregate = {
            "profiles": {
                "qwen38-q4": {
                    "artifact_bytes": 123,
                    "runtime": "fastmtp",
                    "summary": {"status": "pass", "prompt_tps_median": 800.1,
                                "generation_tps_median": 20.2, "ttft_seconds_median": 250.0,
                                "peak_rss_bytes": 10_000},
                },
                "gemma4-a4b-q4": {
                    "artifact_bytes": 456,
                    "runtime": "upstream",
                    "summary": {"status": "fail", "valid_runs": 1},
                },
            }
        }
        report = REPORT.render_markdown(aggregate)
        self.assertIn("20.200", report)
        self.assertIn("FAIL", report)
        self.assertIn("实测吞吐", report)


if __name__ == "__main__":
    unittest.main()
