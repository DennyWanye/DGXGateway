import importlib.util
import pathlib
import unittest


MODULE_PATH = pathlib.Path(__file__).parents[1] / "scripts" / "q8_tuning.py"


def load_module():
    spec = importlib.util.spec_from_file_location("q8_tuning", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load q8_tuning")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Q8TuningTests(unittest.TestCase):
    def test_benchmark_cli_freezes_three_runs_and_json_fields(self):
        benchmark = pathlib.Path(__file__).parents[1] / "scripts" / "benchmark-q8-tuning.py"
        source = benchmark.read_text()
        self.assertIn("range(3)", source)
        self.assertIn('"candidate"', source)
        self.assertIn('"runs"', source)
        self.assertIn('"aggregate"', source)
        self.assertIn('"cache_probe"', source)
        self.assertIn('"spec_types"', source)

    def test_build_request_freezes_quality_and_workload(self):
        tuning = load_module()
        body = tuning.build_request()
        self.assertEqual(body["model"], "qwen38-q8-256k:latest")
        self.assertEqual(body["reasoning"], {"effort": "none"})
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["max_output_tokens"], 256)
        self.assertIn("Python", body["input"])
        self.assertGreater(len(body["input"]), 40_000)

    def test_aggregate_uses_medians_and_rejects_incomplete_run(self):
        tuning = load_module()
        runs = [
            {"valid": True, "generation_tps": 10.0, "prompt_tps": 400.0, "draft_accepted": 100, "draft_generated": 120},
            {"valid": True, "generation_tps": 12.0, "prompt_tps": 420.0, "draft_accepted": 110, "draft_generated": 130},
            {"valid": False, "generation_tps": 99.0, "prompt_tps": 999.0, "draft_accepted": 0, "draft_generated": 0},
        ]
        result = tuning.aggregate_runs("candidate", runs)
        self.assertFalse(result["valid"])
        self.assertEqual(result["valid_runs"], 2)
        self.assertEqual(result["generation_tps"], 11.0)
        self.assertEqual(result["prompt_tps"], 410.0)

    def test_aggregate_uses_full_prompt_run_not_cached_suffix(self):
        tuning = load_module()
        runs = [
            {"valid": True, "generation_tps": 15.0, "prompt_tps": 700.0, "prompt_eval_tokens": 10_000, "draft_accepted": 80, "draft_generated": 100},
            {"valid": True, "generation_tps": 15.5, "prompt_tps": 18.0, "prompt_eval_tokens": 4, "draft_accepted": 80, "draft_generated": 100},
            {"valid": True, "generation_tps": 15.3, "prompt_tps": 17.0, "prompt_eval_tokens": 4, "draft_accepted": 80, "draft_generated": 100},
        ]
        result = tuning.aggregate_runs("cached", runs)
        self.assertEqual(result["prompt_tps"], 700.0)
        self.assertEqual(result["generation_tps"], 15.3)

    def test_normalize_accepts_server_log_timings(self):
        tuning = load_module()
        response = {
            "status": "completed",
            "output": [{"content": [{"type": "output_text", "text": "x" * 100}]}],
            "usage": {"input_tokens": 10_050, "output_tokens": 256},
        }
        timings = {"prompt_per_second": 719.4, "predicted_per_second": 15.55, "prompt_n": 10_057, "draft_n": 190, "draft_n_accepted": 159}
        run = tuning.normalize_run(response, 30.0, timings)
        self.assertTrue(run["valid"])
        self.assertEqual(run["prompt_eval_tokens"], 10_057)
        self.assertEqual(run["draft_accepted"], 159)

    def test_winner_requires_five_percent_generation_gain(self):
        tuning = load_module()
        baseline = {"name": "baseline", "generation_tps": 10.0, "prompt_tps": 400.0, "valid": True}
        noisy = {"name": "noisy", "generation_tps": 10.4, "prompt_tps": 410.0, "valid": True}
        winner = {"name": "winner", "generation_tps": 10.6, "prompt_tps": 380.0, "valid": True}
        self.assertEqual(tuning.choose_winner(baseline, [noisy, winner])["name"], "winner")

    def test_winner_rejects_prompt_regression(self):
        tuning = load_module()
        baseline = {"name": "baseline", "generation_tps": 10.0, "prompt_tps": 400.0, "valid": True}
        regressed = {"name": "regressed", "generation_tps": 12.0, "prompt_tps": 350.0, "valid": True}
        self.assertEqual(tuning.choose_winner(baseline, [regressed])["name"], "baseline")

    def test_winner_supports_explicit_three_percent_threshold(self):
        tuning = load_module()
        baseline = {"name": "baseline", "generation_tps": 18.08, "prompt_tps": 846.03, "valid": True}
        candidate = {"name": "candidate", "generation_tps": 18.70, "prompt_tps": 800.0, "valid": True}
        self.assertEqual(tuning.choose_winner(baseline, [candidate])["name"], "baseline")
        self.assertEqual(
            tuning.choose_winner(baseline, [candidate], minimum_gain=0.03)["name"],
            "candidate",
        )

    def test_nearest_cutoff_has_deterministic_lower_tie_break(self):
        tuning = load_module()
        choices = [0.3, 0.4, 0.5, 0.6]
        self.assertEqual(tuning.nearest_cutoff(0.5, choices), 0.4)
        self.assertEqual(tuning.nearest_cutoff(0.6, choices), 0.5)

    def test_ngram_selection_uses_cold_run_not_repeated_prompt_median(self):
        tuning = load_module()
        candidate = {
            "settings": {"spec_types": "draft-mtp,ngram-mod"},
            "aggregate": {"name": "hybrid", "valid": True, "generation_tps": 140.52, "prompt_tps": 845.4},
            "runs": [{"generation_tps": 19.57}, {"generation_tps": 140.52}, {"generation_tps": 149.78}],
        }
        view = tuning.selection_aggregate(candidate)
        self.assertEqual(view["generation_tps"], 19.57)
        self.assertEqual(view["selection_basis"], "cold_first_run")


if __name__ == "__main__":
    unittest.main()
