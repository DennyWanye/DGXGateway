import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).parents[1] / "scripts" / "test-q8-256k.py"
SPEC = importlib.util.spec_from_file_location("q8_256k", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class Q8256KTests(unittest.TestCase):
    def test_extracts_output_text(self):
        payload = {
            "output": [
                {"content": [{"type": "output_text", "text": "TOKEN"}]}
            ]
        }
        self.assertEqual(MODULE.extract_output_text(payload), "TOKEN")

    def test_result_requires_completed_status_and_sentinel(self):
        payload = {
            "status": "completed",
            "usage": {"input_tokens": 250047},
            "output": [],
        }
        result = MODULE.build_result(payload, "SENTINEL", 250047, 12.5)
        self.assertFalse(result["sentinel_recalled"])
        self.assertEqual(result["status"], "completed")


if __name__ == "__main__":
    unittest.main()
