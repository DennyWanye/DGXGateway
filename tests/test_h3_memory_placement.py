"""Targeted offline checks; do not claim GPU correctness or speed."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "plans/MiniMaxH34DGX/h3-ring-kit-v2"
sys.path.insert(0, str(KIT / "scripts"))
import profile_node

spec = importlib.util.spec_from_file_location("memory_placement", KIT / "patches/performance/memory_placement.py")
memory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(memory)


class MemoryPlacementTests(unittest.TestCase):
    def build(self, enabled=True, **overrides):
        profile = json.loads((KIT / "profiles/ref2va-official.json").read_text())
        profile.pop("memory_placement", None)
        profile.pop("attention_probe_candidate", None)
        profile.pop("fp8_probe_candidate", None)
        if enabled:
            profile["memory_placement"] = memory.CANDIDATE
        profile.update(overrides)
        cluster = json.loads((KIT / "config/cluster.example.json").read_text())
        cluster.update(image_id="sha256:test", master_addr="192.0.2.2")
        node = dict(rank=0, control_iface="eth0", network_mode="socket", hcas="")
        return profile_node.build(cluster, node, profile, {}, 4, "memory-test", Path("/tmp/logs"))

    def test_candidate_scopes_residency_and_preserves_other_compute_flags(self):
        baseline, _ = self.build(False)
        candidate, identity = self.build()
        self.assertNotIn("--layerwise-resident-layers", baseline)
        self.assertEqual(candidate[candidate.index("--layerwise-resident-layers") + 1],
                         "transformer=0.5,text_encoder=0,image_encoder=0,vae=0")
        for option in ("--performance-mode", "--dit-offload-prefetch-size", "--dit-layerwise-resident-layers",
                       "--enable-torch-compile", "--use-fsdp-inference", "--quantization",
                       "--tp-size", "--ring-degree"):
            self.assertEqual(baseline[baseline.index(option) + 1], candidate[candidate.index(option) + 1])
        self.assertIn("H3_MEMORY_PLACEMENT=" + memory.CANDIDATE, candidate)

    def test_rejects_mixed_candidate_parameters(self):
        for overrides in (dict(performance_mode="speed"), dict(dit_layerwise_resident_layers=25),
                          dict(dit_offload_prefetch_size=2), dict(memory_placement="typo")):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.build(**overrides)

    def test_next_attention_probe_is_explicit_and_cannot_mix_with_placement(self):
        baseline, _ = self.build(False)
        candidate, _ = self.build(False, attention_probe_candidate="m256n64-t256-v1")
        self.assertFalse(any("H3_ATTENTION_PROBE=" in item for item in baseline))
        self.assertIn("H3_ATTENTION_PROBE=m256n64-t256-v1", candidate)
        with self.assertRaises(ValueError):
            self.build(attention_probe_candidate="m256n64-t256-v1")

    def test_fp8_probe_is_explicit_and_cannot_mix_with_other_candidates(self):
        args, _ = self.build(False,fp8_probe_candidate="triton-m128n256k64-scalealigned-v2")
        self.assertIn("H3_FP8_PROBE=triton-m128n256k64-scalealigned-v2",args)
        for extra in ({"memory_placement":"memory-dit-resident-v1"},
                      {"attention_probe_candidate":"m256n64-t256-v1"},{"performance_mode":"speed"}):
            with self.assertRaises(ValueError):
                self.build(False,fp8_probe_candidate="triton-m128n256k64-scalealigned-v2",**extra)

    def test_both_host_and_cgroup_limits_must_pass(self):
        g = memory.GIB
        args = dict(selected_bytes=16*g, current_bytes=50*g, limit_bytes=104*g,
                    host_available_bytes=60*g, cuda_allocated_bytes=24*g)
        self.assertTrue(memory.budget_check(**args)["ok"])
        self.assertFalse(memory.budget_check(**{**args, "limit_bytes": 80*g})["ok"])
        self.assertFalse(memory.budget_check(**{**args, "host_available_bytes": 30*g})["ok"])
        self.assertFalse(memory.budget_check(**{**args, "current_bytes": 90*g})["ok"])

    def test_inventory_uses_actual_fp8_and_strided_buffer_bytes(self):
        def tensor(count, size):
            return SimpleNamespace(numel=lambda: count, element_size=lambda: size)
        manager = SimpleNamespace(num_layers=2, layers_attr_str="blocks", prefetch_size=1,
            resident_layers=1, residency_policy="leading", _resident_set={0},
            _consolidated_cpu_weights={0: {"fp8": tensor(100, 1)}, 1: {"bf16": tensor(100, 2)}},
            _strided_cpu_weights={0: {"scale": tensor(4, 4)}}, _mapped_cpu_weights={},
            _weight_metadata={0: {"w": {}}, 1: {"w": {}}})
        groups = memory.inventory(SimpleNamespace(layerwise_offload_managers=[manager]))
        self.assertEqual(groups[0]["selected_device_bytes"], 116)
        self.assertEqual(groups[0]["host_copy_bytes"], 316)


    def test_whole_resident_is_a_distinct_component_strategy(self):
        cmd, _ = self.build(memory_placement="memory-dit-resident-v1")
        self.assertEqual(cmd[cmd.index("--component-residency") + 1], "transformer=resident")
        self.assertNotIn("--layerwise-resident-layers", cmd)
        self.assertEqual(cmd[cmd.index("--performance-mode") + 1], "memory")
        self.assertIn("H3_MEMORY_PLACEMENT=memory-dit-resident-v1", cmd)

    def test_whole_budget_rejects_duplicate_stores_and_insufficient_host_margin(self):
        g = memory.GIB
        inv = dict(unique_storage_bytes={"cuda":31*g}, non_cuda_parameters=[],
                   layerwise_managers=0, host_store_entries=0,
                   parameter_dtypes={"torch.float8_e4m3fn":50})
        args = dict(inventory=inv, host_available_bytes=70*g, current_bytes=45*g,
                    limit_bytes=104*g, cuda_allocated_bytes=32*g)
        self.assertTrue(memory.resident_budget(**args)["ok"])
        self.assertFalse(memory.resident_budget(**{**args,"host_available_bytes":50*g})["ok"])
        for change in (dict(layerwise_managers=1), dict(host_store_entries=10),
                       dict(non_cuda_parameters=["weight"]), dict(unique_storage_bytes={"cuda":31*g,"cpu":2*g})):
            self.assertFalse(memory.resident_budget(**{**args,"inventory":{**inv,**change}})["ok"])

    def test_resident_inventory_deduplicates_actual_storage(self):
        class T:
            dtype = "torch.float8_e4m3fn"
            device = SimpleNamespace(type="cuda")
            def untyped_storage(self):
                return SimpleNamespace(data_ptr=lambda:1234,nbytes=lambda:4096)
        model = SimpleNamespace(named_parameters=lambda:[("a",T()),("b",T())],
                                named_buffers=lambda:[],modules=lambda:[],layerwise_offload_managers=[])
        inv = memory.resident_inventory(model)
        self.assertEqual(inv["unique_storage_bytes"], {"cuda":4096})
        self.assertEqual(inv["parameter_count"], 2)

    def test_disabled_audit_is_noop_and_finish_clears_references(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"H3_MEMORY_PLACEMENT": ""}):
            memory.start(None, None, None)
        with tempfile.TemporaryDirectory() as d:
            batch = SimpleNamespace(extra={"_h3_memory_root": Path(d),
                "_h3_memory_model": SimpleNamespace(layerwise_offload_managers=[])})
            memory.finish(batch)
            self.assertEqual(batch.extra, {})
            self.assertEqual(json.loads((Path(d)/"placement.jsonl").read_text())["phase"], "after_stage_release")


if __name__ == "__main__":
    unittest.main()
