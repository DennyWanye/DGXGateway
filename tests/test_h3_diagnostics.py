import unittest
from DGXGateway.dgx_gateway.h3_diagnostics import should_stop, validate_diagnostic


class DiagnosticTests(unittest.TestCase):
    def test_all_ranks_must_finish_and_time_limit_is_independent(self):
        row = {"diagnostic": {"trace_step": 3, "max_steps": 6, "max_seconds": 3000},
               "diagnostic_started_at": 100,
               "diagnostic_progress": {str(r): {"completed_steps": 6} for r in range(3)}}
        self.assertIsNone(should_stop(row, 200))
        row["diagnostic_progress"]["3"] = {"completed_steps": 6}
        self.assertEqual(should_stop(row, 200), "step_budget")
        row["diagnostic_progress"] = {}
        self.assertEqual(should_stop(row, 3100), "time_budget")

    def test_validation_rejects_unbounded_or_unreachable_trace(self):
        valid = {"trace_step": 3, "max_steps": 6, "max_seconds": 3000}
        self.assertEqual(validate_diagnostic(valid), valid)
        self.assertIsNone(validate_diagnostic(None))
        for change in ({"max_steps": 49}, {"max_seconds": 50000}, {"trace_step": 6},
                       {"trace_step": True}, {"extra": "unknown"}):
            with self.assertRaises(ValueError):
                validate_diagnostic({**valid, **change})

    def test_attention_probe_is_bounded_and_cannot_overlap_heavy_trace(self):
        probe = {"trace_step": -1, "max_steps": 1, "max_seconds": 1200, "attention_probe": True}
        self.assertEqual(validate_diagnostic(probe), probe)
        for change in ({"trace_step": 0}, {"max_steps": 2}, {"attention_probe": "true"}):
            with self.assertRaises(ValueError):
                validate_diagnostic({**probe, **change})
