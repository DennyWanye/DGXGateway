"""ComfyUI lifecycle identity is not MiniMax Music 3."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from DGXGateway.dgx_gateway.adapters.catalog import CATALOG
from DGXGateway.dgx_gateway.adapters.music3 import ComfyAdapter, Music3Adapter
from DGXGateway.dgx_gateway.comfy_identity import (
    COMFY_SERVICE,
    LEGACY_COMFY_SERVICE,
    canonical_service,
    is_comfy_service,
    same_service,
)
from DGXGateway.dgx_gateway.residency import ModelResidency
from DGXGateway.dgx_gateway.runtime_view import job_runtime_view, runtime_view, service_name


class ComfyServiceIdentityTests(unittest.TestCase):
    def test_canonical_id_is_comfyui_not_music3(self):
        self.assertEqual(COMFY_SERVICE, "comfyui")
        self.assertEqual(canonical_service("minimax-music3"), "comfyui")
        self.assertEqual(canonical_service("comfyui"), "comfyui")
        self.assertEqual(canonical_service("minimax-h3"), "minimax-h3")
        self.assertTrue(is_comfy_service("minimax-music3"))
        self.assertTrue(same_service("minimax-music3", "comfyui"))
        self.assertFalse(same_service("comfyui", "minimax-h3"))

    def test_catalog_and_adapter_use_comfyui(self):
        self.assertEqual(ComfyAdapter.model_id, "comfyui")
        self.assertIs(Music3Adapter, ComfyAdapter)
        self.assertIn("comfyui", {cls.model_id for cls in CATALOG})
        self.assertNotIn("minimax-music3", {cls.model_id for cls in CATALOG})

    def test_image_residency_and_runtime_are_comfyui(self):
        row = {
            "job_id": "a" * 32,
            "image_request": {"tool": "generate_image"},
            "workflow": {"1": {"class_type": "LLaDAImageLoader", "inputs": {"diffusion_model": "LLaDA-Image-Turbo-INT8.safetensors"}}},
        }
        self.assertEqual(ModelResidency.key("creative_jobs", row)[0], "comfyui")
        view = job_runtime_view(row, "creative")
        self.assertEqual(view["service_id"], "comfyui")
        self.assertEqual(view["model_name"], "LLaDA Image Turbo · INT8")
        snapshot = {
            "active_local_model": {"state": "ACTIVE", "model_id": "comfyui"},
            "current_operation": None,
            "latest_operation": {
                "operation_id": "start",
                "target_model": "comfyui",
                "phase": "RELEASED",
                "outcome": "COMMITTED",
            },
        }
        runtime = runtime_view(snapshot, [{**row, "kind": "creative", "state": "RUNNING", "start_operation_id": "start"}])
        self.assertEqual(runtime["service_id"], "comfyui")
        self.assertEqual(runtime["service_name"], "ComfyUI 制作服务")
        self.assertNotIn("music3", runtime["service_name"])

    def test_legacy_music3_labels_and_residency_still_match_comfyui(self):
        self.assertEqual(service_name("minimax-music3"), "ComfyUI 制作服务")
        self.assertEqual(service_name("comfyui"), "ComfyUI 制作服务")
        snapshot = {
            "active_local_model": {"state": "ACTIVE", "model_id": "comfyui"},
            "current_operation": None,
            "latest_operation": {
                "operation_id": "start",
                "target_model": "minimax-music3",
                "phase": "RELEASED",
                "outcome": "COMMITTED",
            },
            "residency": {
                "state": "IDLE",
                "model_id": "minimax-music3",
                "owner_job_id": "b" * 32,
                "start_operation_id": "start",
            },
        }
        self.assertEqual(runtime_view(snapshot)["phase"], "IDLE")


class ComfyHealthDoesNotRequireMusicWeightsTests(unittest.TestCase):
    def _adapter(self, run):
        config = SimpleNamespace(
            nodes=[SimpleNamespace(role="head", address="192.0.2.41")],
            music3_root="/home/operator/ai/ComfyUI",
            music3_python="/home/operator/ai/comfyui-env/bin/python",
            music3_port=8188,
        )
        adapter = ComfyAdapter(config, SimpleNamespace(run=run))
        adapter._http_ok = lambda path, timeout=5: True
        return adapter

    def test_healthy_observe_ignores_music_weight_files(self):
        def run(node, argv, timeout):
            del node, timeout
            if argv[0] == "test":
                self.fail("Music 3 weight files must not gate ComfyUI health")
            if argv[0] == "systemctl":
                return SimpleNamespace(returncode=0, stdout="loaded\nactive\n")
            if argv[0] == "pgrep":
                return SimpleNamespace(returncode=0, stdout="1\n")
            return SimpleNamespace(returncode=0, stdout="LISTEN 8188")

        adapter = self._adapter(run)
        self.assertEqual(adapter.observe().state, "HEALTHY")
        self.assertEqual(adapter.model_id, "comfyui")

    def test_start_uses_comfyui_unit_without_music_weights(self):
        seen = []

        def run(node, argv, timeout):
            del node, timeout
            seen.append(argv)
            if argv[0] == "test":
                self.fail("Music 3 weight files must not gate ComfyUI start")
            return SimpleNamespace(returncode=0, stdout="")

        adapter = self._adapter(run)
        adapter.start(SimpleNamespace(guard=Mock()))
        command = next(argv for argv in seen if argv[0] == "systemd-run")
        self.assertIn("--unit=dgx-gateway-comfyui", command)
        self.assertNotIn("--unit=dgx-gateway-music3", command)

    def test_stop_retires_legacy_and_current_units(self):
        seen = []

        def run(node, argv, timeout):
            del node, timeout
            seen.append(argv)
            return SimpleNamespace(returncode=0, stdout="")

        adapter = self._adapter(run)
        adapter.stop(SimpleNamespace(guard=Mock()))
        stopped = [tuple(argv) for argv in seen if argv[:3] == ["systemctl", "--user", "stop"]]
        self.assertEqual(
            stopped,
            [
                ("systemctl", "--user", "stop", "dgx-gateway-comfyui.service"),
                ("systemctl", "--user", "stop", "dgx-gateway-music3.service"),
            ],
        )

    def test_legacy_unit_alone_is_still_healthy(self):
        def run(node, argv, timeout):
            del node, timeout
            if argv[0] == "systemctl":
                unit = argv[3]
                if unit == "dgx-gateway-music3.service":
                    return SimpleNamespace(returncode=0, stdout="loaded\nactive\n")
                return SimpleNamespace(returncode=4, stdout="not-found\ninactive\n")
            if argv[0] == "pgrep":
                return SimpleNamespace(returncode=0, stdout="1\n")
            return SimpleNamespace(returncode=0, stdout="LISTEN 8188")

        adapter = self._adapter(run)
        self.assertEqual(adapter.observe().state, "HEALTHY")


if __name__ == "__main__":
    unittest.main()
