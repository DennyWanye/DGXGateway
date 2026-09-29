import importlib.util
import pathlib
import unittest


MODULE_PATH = pathlib.Path(__file__).parents[1] / "scripts" / "fastmtp_strict.py"


def load_module():
    spec = importlib.util.spec_from_file_location("fastmtp_strict", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load fastmtp_strict")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FastMTPStrictTests(unittest.TestCase):
    def test_cases_freeze_sampler_and_cover_document_and_reasoning(self):
        module = load_module()
        cases = module.build_cases()
        self.assertEqual(set(cases), {"document", "reasoning"})
        for body in cases.values():
            self.assertEqual(body["seed"], 424242)
            self.assertEqual(body["temperature"], 1.0)
            self.assertEqual(body["top_k"], 20)
            self.assertEqual(body["top_p"], 0.95)
            self.assertEqual(body["max_output_tokens"], 512)
            self.assertGreater(len(body["input"]), 40_000)
        self.assertEqual(cases["document"]["reasoning"], {"effort": "none"})
        self.assertEqual(cases["reasoning"]["reasoning"], {"effort": "xhigh"})

    def test_digest_is_stable_and_sensitive_to_output(self):
        module = load_module()
        first = {"output": [{"type": "message", "content": [{"text": "a"}]}]}
        same = {"output": [{"content": [{"text": "a"}], "type": "message"}]}
        different = {"output": [{"type": "message", "content": [{"text": "b"}]}]}
        self.assertEqual(module.output_digest(first), module.output_digest(same))
        self.assertNotEqual(module.output_digest(first), module.output_digest(different))

    def test_summary_uses_medians_and_acceptance_ratio(self):
        module = load_module()
        runs = [
            {"prompt_tps": 700.0, "generation_tps": 10.0, "draft_accepted": 60, "draft_generated": 100, "output_sha256": "x"},
            {"prompt_tps": 720.0, "generation_tps": 12.0, "draft_accepted": 70, "draft_generated": 100, "output_sha256": "x"},
            {"prompt_tps": 710.0, "generation_tps": 11.0, "draft_accepted": 80, "draft_generated": 100, "output_sha256": "x"},
        ]
        summary = module.summarize(runs)
        self.assertEqual(summary["prompt_tps_median"], 710.0)
        self.assertEqual(summary["generation_tps_median"], 11.0)
        self.assertEqual(summary["draft_acceptance"], 0.7)
        self.assertTrue(summary["outputs_identical"])


if __name__ == "__main__":
    unittest.main()
