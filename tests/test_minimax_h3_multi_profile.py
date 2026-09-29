import base64
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class MultiProfileCatalogTests(unittest.TestCase):
    def test_catalog_exposes_three_public_profiles_and_retains_internal_control(self):
        catalog = json.loads((ROOT / "manifests/minimax-h3-profiles-v1.json").read_text())
        self.assertEqual(catalog["schema_version"], 2)
        profiles = catalog["profiles"]
        self.assertEqual(
            {name for name, value in profiles.items() if value["visibility"] == "public"},
            {"heretic-ref2va-v1", "heretic-ref2va-hmnsfw-v1", "10eros-fl2va-v1"},
        )
        self.assertEqual(profiles["heretic-hmnsfw-v1"]["visibility"], "internal")
        self.assertEqual(
            {name: profiles[name]["qualification"] for name in (
                "heretic-ref2va-v1",
                "heretic-ref2va-hmnsfw-v1",
                "10eros-fl2va-v1",
            )},
            {
                "heretic-ref2va-v1": "VALUE_QUALIFIED",
                "heretic-ref2va-hmnsfw-v1": "VALUE_QUALIFIED",
                "10eros-fl2va-v1": "VALUE_QUALIFIED",
            },
        )
        self.assertEqual(profiles["10eros-fl2va-v1"]["supported_tasks"], ["t2va", "fl2va"])
        self.assertEqual(
            profiles["10eros-fl2va-v1"]["model_path"],
            profiles["10eros-fl2va-v1"]["model_root"],
        )
        self.assertEqual(profiles["heretic-ref2va-hmnsfw-v1"]["supported_tasks"], ["ref2va"])

    def test_10eros_runtime_lock_seals_the_required_worker_overlay(self):
        runtime = json.loads(
            (ROOT / "manifests/minimax-h3-10eros-runtime-lock-v1.json").read_text()
        )
        self.assertEqual(
            runtime["deployment_overlays"],
            [
                {
                    "path": "overlays/minimax-h3-two-node/worker.py",
                    "target": "/opt/h3-multinode/h3_multinode/worker.py",
                    "sha256": "f17239d6a5fc361e1e482f25e02273de5285c902f5a36340a10e3d9a9a901b93",
                }
            ],
        )

    def test_10eros_bf16_source_is_not_dynamically_requantized(self):
        source = (ROOT / "scripts/minimax-h3-two-node/minimax-h3-runtime-action").read_text()
        branch_start = source.index("elif [[ $H3_SELECTED_PROFILE == 10eros-fl2va-v1 ]]")
        branch_end = source.index("\n  fi", branch_start)
        branch = source[branch_start:branch_end]
        self.assertIn("quantization_args=()", branch)
        self.assertIn("profile_args+=(--text-encoder-tp-size 2)", branch)
        self.assertNotIn("ignored_layers", branch)
        self.assertIn('"${quantization_args[@]}"', source)

    def test_identity_is_canonical_and_round_trips_through_header(self):
        identity_module = load_module(
            "h3_profile_identity",
            ROOT / "scripts/minimax-h3-two-node/profile_identity.py",
        )
        value = {
            "schema_version": 1,
            "name": "heretic-ref2va-v1",
            "task_family": "ref2va",
            "supported_tasks": ["ref2va"],
            "transformer": {"kind": "official", "file_sha256": "a" * 64},
            "modifier": {"kind": "none"},
            "curve": {"kind": "none"},
            "model_tree_sha256": "b" * 64,
            "runtime_image_id": "sha256:" + "c" * 64,
            "runtime_overlay_sha256": "d" * 64,
            "rank_identities": [0, 1],
        }
        sealed = identity_module.seal_identity(value)
        canonical = identity_module.canonical_identity_bytes(sealed)
        self.assertEqual(sealed["contract_sha256"], hashlib.sha256(canonical).hexdigest())
        encoded = identity_module.encode_identity_header(sealed)
        self.assertNotIn("=", encoded)
        self.assertEqual(identity_module.decode_identity_header(encoded), sealed)


class CurveRuntimeTests(unittest.TestCase):
    def test_curve_runtime_matches_comfy_reference_precision_contract(self):
        source = (ROOT / "scripts/minimax-h3-two-node/patch-10eros-runtime.py").read_text()
        self.assertIn(
            "self.compute_dtype = _FP32_DTYPE if arch.use_adaln_curve else _BF16_DTYPE",
            source,
        )
        self.assertIn("params_dtype=self.compute_dtype", source)
        self.assertIn("quant_config=None if arch.use_adaln_curve else quant_config", source)
        self.assertIn("x.to(self.compute_dtype)", source)
        self.assertIn("item.to(_BF16_DTYPE) for item in x.chunk", source)
        self.assertIn("if arch.use_adaln_curve:\\n            return weight", source)
        self.assertIn("return _reorder_grouped_qkv_to_qkv", source)

    def test_10eros_schedule_patch_matches_comfy_simple_cardinality(self):
        schedule = load_module(
            "h3_10eros_schedule_patch",
            ROOT / "scripts/minimax-h3-two-node/patch-10eros-schedule.py",
        )
        self.assertEqual(
            [round(value, 9) for value in schedule.comfy_simple_sigmas(8, 12.0)],
            [
                1.0,
                0.988235294,
                0.972972973,
                0.952380952,
                0.923076923,
                0.878048780,
                0.8,
                0.631578947,
                0.0,
            ],
        )
        self.assertEqual(len(schedule.comfy_simple_sigmas(8, 3.0)), 9)

    def test_10eros_image_build_is_timezone_reproducible(self):
        source = (ROOT / "scripts/minimax-h3-two-node/build-10eros-runtime.sh").read_text()
        self.assertIn("TZ=UTC SOURCE_DATE_EPOCH=1725148800 docker buildx build", source)

    def test_curve_interpolation_clamps_and_uses_last_interval(self):
        curve = load_module(
            "h3_curve_runtime",
            ROOT / "scripts/minimax-h3-two-node/curve_runtime.py",
        )
        table = [[float(index), float(index * 10)] for index in range(5)]
        self.assertEqual(curve.interpolate_curve(table, -0.2), [0.0, 0.0])
        self.assertEqual(curve.interpolate_curve(table, 0.125), [0.5, 5.0])
        self.assertEqual(curve.interpolate_curve(table, 1.0), [4.0, 40.0])
        self.assertEqual(curve.interpolate_curve(table, 1.2), [4.0, 40.0])

    def test_curve_contract_rejects_dense_time_weights(self):
        curve = load_module(
            "h3_curve_runtime_contract",
            ROOT / "scripts/minimax-h3-two-node/curve_runtime.py",
        )
        keys = {
            "adaln_t_table",
            "adaln_basis",
            "adaln_mean",
            "time_embedder.linear_1.weight",
        }
        with self.assertRaisesRegex(ValueError, "dense time"):
            curve.validate_curve_keys(keys, projection_count=51)

class HmnsfwBuilderTests(unittest.TestCase):
    def test_merge_casts_scaled_delta_before_bf16_add(self):
        try:
            import torch
        except ImportError:
            self.skipTest("torch is available in the pinned deployment image")
        builder = load_module(
            "h3_hmnsfw_builder",
            ROOT / "scripts/minimax-h3-two-node/build-hmnsfw-profile.py",
        )
        weight = torch.tensor([[1.0, -2.0], [0.5, 3.0]], dtype=torch.bfloat16)
        a = torch.tensor([[0.25, -0.5]], dtype=torch.float16)
        b = torch.tensor([[2.0], [-1.0]], dtype=torch.float16)
        expected = weight + ((b.float() @ a.float()) * 0.5).to(torch.bfloat16)
        self.assertTrue(torch.equal(builder.merge_weight(weight, a, b, 0.5), expected))

    def test_lora_key_mapping_is_exact(self):
        builder = load_module(
            "h3_hmnsfw_builder_keys",
            ROOT / "scripts/minimax-h3-two-node/build-hmnsfw-profile.py",
        )
        self.assertEqual(
            builder.lora_target("diffusion_model.transformer_blocks.0.ff.net.0.proj.lora_A.weight"),
            ("transformer_blocks.0.ff.net.0.proj.weight", "A"),
        )
        with self.assertRaises(ValueError):
            builder.lora_target("unexpected.weight")


if __name__ == "__main__":
    unittest.main()
