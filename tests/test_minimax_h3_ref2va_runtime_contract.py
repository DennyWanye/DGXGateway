from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONTROL_UPDATE = ROOT / "scripts/minimax-h3-two-node/update-ref2va-control-layer.sh"
LOCK = ROOT / "manifests/minimax-h3-ref2va-runtime-lock.json"
ACTIVE_RUNTIME_LOCK = ROOT / "manifests/minimax-h3-runtime-lock.json"
BUILD_SCRIPT = ROOT / "scripts/minimax-h3-two-node/build-ref2va-runtime.sh"
WORKER_OVERLAY = ROOT / "overlays/minimax-h3-two-node/worker.py"
SHARED_COMPONENT_PATCH = ROOT / "patches/minimax-h3-ref2va/0001-share-heretic-fl2va-components.patch"
HEX64 = re.compile(r"[0-9a-f]{64}")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")


class Ref2VARuntimeContractTests(unittest.TestCase):
    def test_worker_overlay_serializes_reference_audio_content_not_api_tmp_path(self) -> None:
        source = WORKER_OVERLAY.read_text(encoding="utf-8")
        for required in (
            "class _PortableAudioPath",
            "def __fspath__",
            "def __reduce__",
            "wave.open",
            "_restore_pcm16_audio",
            "_install_portable_audio_decode_patch",
            "video_api_utils.decode_audio_url",
            '"EXECUTE_ERROR"',
            "traceback.print_exc()",
        ):
            self.assertIn(required, source)
        self.assertIn("audio must be mono PCM16 WAV", source)

    def test_active_and_sealed_runtime_locks_pin_the_same_overlay(self) -> None:
        active = json.loads(ACTIVE_RUNTIME_LOCK.read_text(encoding="utf-8"))
        sealed = json.loads(LOCK.read_text(encoding="utf-8"))
        self.assertEqual(active["deployment_overlays"], sealed["deployment_overlays"])

    def test_control_update_is_atomic_recoverable_and_does_not_touch_networking(self):
        source = CONTROL_UPDATE.read_text(encoding="utf-8")
        self.assertIn("rollback_update", source)
        self.assertIn("preimage", source)
        self.assertIn("mv -f --", source)
        self.assertIn("systemctl daemon-reload", source)
        self.assertIn("H3_RESTORE_LLM_ON_STOP 0", source)
        self.assertIn(
            "set_key H3_MAC_LOCK_ATTESTATION_PATH /run/minimax-h3-two-node/mac-lock-attestation.json",
            source,
        )
        self.assertNotIn("ufw ", source)
        self.assertNotIn("nft ", source)
        self.assertNotIn("ip route", source)

    def setUp(self) -> None:
        self.document = json.loads(LOCK.read_text(encoding="utf-8"))

    def test_pins_candidate_source_and_arm64_base(self) -> None:
        self.assertEqual(1, self.document["schema_version"])
        source = self.document["vllm_omni_source"]
        self.assertEqual("https://github.com/vllm-project/vllm-omni.git", source["repository"])
        self.assertEqual("a65e89cbd47fe7c9f39dba558200c0bc16a758f3", source["commit"])
        base = self.document["base_image"]
        self.assertEqual(
            "vllm/vllm-omni:minimax-h3@sha256:e930db8e225162d01e17a49dddc43fd0e844208908d8356a028e5c4e7357696e",
            base["reference"],
        )
        self.assertEqual("arm64", base["architecture"])
        self.assertEqual(
            "sha256:474d635dc5dc2cf66f0df692ddef19967a920253826f431689af2b0e010afc4e",
            base["child_digest"],
        )

    def test_all_local_inputs_exist_and_match_their_hashes(self) -> None:
        inputs = self.document["local_inputs"]
        self.assertGreaterEqual(len(inputs), 5)
        paths = [row["path"] for row in inputs]
        self.assertIn(
            "patches/minimax-h3-ref2va/0001-share-heretic-fl2va-components.patch",
            paths,
        )
        self.assertEqual(len(paths), len(set(paths)))
        for row in inputs:
            self.assertRegex(row["sha256"], HEX64)
            path = ROOT / row["path"]
            self.assertTrue(path.is_file(), path)
            self.assertEqual(row["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_candidate_is_sealed_to_the_observed_remote_build(self) -> None:
        candidate = self.document["candidate"]
        self.assertEqual("minimax-h3-two-node:ref2va-candidate", candidate["tag"])
        self.assertEqual(
            "sha256:b0d338b9637963f94774bfba6b90f5d5dfe45b90063386d012ee46c948fd6e69",
            candidate["final_image_id"],
        )
        self.assertEqual("2026-09-01T11:22:51.530960Z", candidate["sealed_at"])
        self.assertEqual("resolved", candidate["status"])

    def test_shared_component_patch_is_explicit_and_fail_closed(self) -> None:
        source = SHARED_COMPONENT_PATCH.read_text(encoding="utf-8")
        self.assertIn('"ref2va": ["FL2VA/text_encoder/**"]', source)
        self.assertIn('shared_path = model_root / "FL2VA"', source)
        self.assertIn('"Ref2VA/transformer/**"', source)
        self.assertIn('os.path.join(shared_path, "video_vae")', source)
        self.assertIn('os.path.join(shared_path, "audio_vae")', source)
        self.assertNotIn('+    "ref2va": ["Ref2VA/**"]', source)

    def test_build_is_isolated_verified_and_seals_observed_image(self) -> None:
        source = BUILD_SCRIPT.read_text(encoding="utf-8")
        for required in (
            "a65e89cbd47fe7c9f39dba558200c0bc16a758f3",
            "minimax-h3-two-node:ref2va-candidate",
            "minimax-h3-two-node:production",
            "H3_ALLOW_LOCAL_DOCKER_BUILD",
            "apply --check",
            "--no-deps",
            "--no-build-isolation",
            "VLLM_OMNI_VERSION_OVERRIDE",
            "seal-existing",
            "_DIFFUSION_MODELS",
            "locate_file",
            "docker image inspect",
            "os.replace",
            "--task-type",
            "ref2va",
        ):
            self.assertIn(required, source)
        self.assertNotIn("docker rmi", source)
        self.assertNotIn("docker rm", source)
        self.assertNotIn("git clone", source)
        self.assertIn("git -C \"$source_dir\" ls-files -z", source)
        self.assertIn("--null -T - -cf -", source)

    def test_production_rollback_identity_is_immutable(self) -> None:
        rollback = self.document["rollback"]
        self.assertRegex(rollback["final_image_id"], IMAGE_ID)
        self.assertEqual(
            "sha256:2c8b61d92a7f1205e8445e6ea6ba27b9922fde308b9daf121a8b4dcd50727d11",
            rollback["final_image_id"],
        )
        self.assertEqual("minimax-h3-two-node:production", rollback["tag"])


if __name__ == "__main__":
    unittest.main()
