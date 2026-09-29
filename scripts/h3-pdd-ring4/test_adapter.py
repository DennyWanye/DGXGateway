"""Focused CPU checks: numerical mapping, PDD plans and request cache lifetime."""
import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest

import torch
import torch.nn.functional as F

sys.path.insert(0, str(Path(__file__).parent))
import h3_pdd as pdd
from h3_easycache import EasyCache


class AdapterTests(unittest.TestCase):
    def test_pdd_plan_matches_explicit_four_subinterval_displacement(self):
        torch.manual_seed(7)
        heads = torch.randn(32, 3, 5)
        x = torch.randn(2, 5)
        for shift in (12, 3):
            grid = pdd.time_grid(shift)
            for step in range(8):
                start, end = step * 4, (step + 1) * 4
                fused = F.linear(x, torch.einsum("n,noi->oi", pdd.sampling_plan(shift, step), heads))
                displacement = sum((grid[i + 1] - grid[i]).float() * F.linear(x, heads[i]) for i in range(start, end))
                torch.testing.assert_close(fused * (grid[end] - grid[start]).float(), displacement)

    def test_qkv_deltas_land_in_three_separate_slices(self):
        torch.manual_seed(9)
        x, base = torch.randn(5, 7), torch.randn(5, 12)
        adapters = [(torch.randn(2, 7), torch.randn(4, 2), i, 1.) for i in range(3)]
        actual, bias = pdd.add_delta((base.clone(), None), x, adapters)
        expected = base + torch.cat([F.linear(F.linear(x, a), b) for a, b, _, _ in adapters], -1)
        torch.testing.assert_close(actual, expected)
        self.assertIsNone(bias)

    def test_diffusers_swiglu_value_gate_conversion(self):
        torch.manual_seed(11)
        x = torch.randn(2, 5)
        down, up = torch.randn(3, 5), torch.randn(8, 3)
        value, gate = up.chunk(2, 0)
        native, _ = pdd.add_delta((torch.zeros(2, 8), None), x, [(down, torch.cat((gate, value)), None, 1.)])
        reference = F.linear(F.linear(x, down), up)
        rv, rg = reference.chunk(2, -1)
        ng, nv = native.chunk(2, -1)
        torch.testing.assert_close(F.silu(ng) * nv, F.silu(rg) * rv)

    def test_shape_mapping_and_reject_unknown(self):
        self.assertEqual(pdd.native_target("transformer_blocks.49.adaln_proj.linear"), ("blocks.49.adaln_proj.linear", None, False))
        self.assertEqual(pdd.native_target("token_refiner.refiner_blocks.1.attn.to_v"), ("token_refiner.blocks.1.attn.qkv_proj", 2, False))
        with self.assertRaises(ValueError):
            pdd.native_target("unknown.weight")

    def test_cache_reuses_residual_without_mutating_euler_state_and_resets(self):
        os.environ["H3_EASYCACHE"] = "1"
        cache = EasyCache(8, torch.device("cpu"))
        audio = torch.ones(16, 32)
        for step in range(3):
            x = torch.ones(16, 96) * (1 + step * .001)
            self.assertFalse(cache.skip(step, x))
            cache.remember(x, audio, x * 1.01, audio * 1.02)
        x = torch.ones(16, 96) * 1.003
        before = x.clone()
        self.assertTrue(cache.skip(3, x))
        y, a = cache.reuse(x, audio)
        torch.testing.assert_close(x, before)
        torch.testing.assert_close(y, x + cache.diff_v)
        torch.testing.assert_close(a, audio + cache.diff_a)
        self.assertFalse(cache.skip(7, x))
        fresh = EasyCache(8, torch.device("cpu"))
        self.assertFalse(fresh.skip(3, x))
        self.assertEqual(fresh.skipped, 0)

    def test_zero_change_and_disabled_cache_never_invent_a_hit(self):
        for enabled in ("0", "1"):
            os.environ["H3_EASYCACHE"] = enabled
            cache = EasyCache(8, torch.device("cpu"))
            x, a = torch.ones(16, 96), torch.ones(16, 32)
            for i in range(8):
                self.assertFalse(cache.skip(i, x))
                cache.remember(x, a, x, a)


if __name__ == "__main__":
    unittest.main()
