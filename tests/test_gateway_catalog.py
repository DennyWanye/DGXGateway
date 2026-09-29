"""Gateway catalog drops retired two-node models and keeps Ring / H3 identities."""
import tempfile
import unittest
from pathlib import Path

from DGXGateway.dgx_gateway.adapters.catalog import CATALOG, RETIRED_MODEL_IDS
from DGXGateway.dgx_gateway.text_jobs import UnavailableFence
from tests.test_dgx_gateway_vision import config_text


class GatewayCatalogTests(unittest.TestCase):
    def test_retired_two_node_models_are_not_in_catalog(self):
        ids = {cls.model_id for cls in CATALOG}
        self.assertEqual(
            set(RETIRED_MODEL_IDS),
            {
                "deepseek-v4-flash",
                "deepseek-v4-flash-vision-exp",
                "qwen38-flash-next",
            },
        )
        self.assertTrue(set(RETIRED_MODEL_IDS).isdisjoint(ids))
        self.assertEqual(
            ids,
            {
                "mia-dsv41-ablit",
                "longcat-video-avatar-1.5",
                "minimax-h3",
                "comfyui",
                "personaplex",
                "dsv41-ring-tp4",
                "h3-ring4",
                "h3-dual-pair",
            },
        )

    def test_application_omits_retired_models_and_text_jobs_start(self):
        from DGXGateway.dgx_gateway.__main__ import build_application

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "admin.token").write_text("x" * 40)
            (root / "authority.json").write_text("{}")
            (root / "authority.credential").write_text("y" * 40)
            (root / "deepseek.token").write_text("z" * 40)
            (root / "vision.token").write_text("w" * 40)
            config_path = root / "gateway.toml"
            config_path.write_text(config_text(root))
            app = build_application(config_path, recover_incomplete=False)
            try:
                self.assertEqual(set(app.operations.adapters), {cls.model_id for cls in CATALOG})
                self.assertEqual(set(app.snapshot.adapters), set(app.operations.adapters))
                for model_id in RETIRED_MODEL_IDS:
                    self.assertNotIn(model_id, app.operations.adapters)
                self.assertIsInstance(app.text_jobs.fence, UnavailableFence)
            finally:
                app.store.close()


if __name__ == "__main__":
    unittest.main()
