import base64
import io
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from DGXGateway.dgx_gateway.image_assets import ImageAssets
from DGXGateway.dgx_gateway.media_jobs import MediaJobs


def png_bytes(size, color, transparent_corner=False):
    image = Image.new("RGBA", size, color)
    if transparent_corner:
        image.putpixel((0, 0), (0, 0, 0, 0))
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


class MediaJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        assets = ImageAssets(root / "creative-assets")
        self.background = assets.register_base64("background.png", base64.b64encode(png_bytes((100, 60), (20, 30, 40, 255))).decode())
        self.layer = assets.register_base64("layer.png", base64.b64encode(png_bytes((10, 10), (255, 0, 0, 255), True)).decode())
        app = SimpleNamespace(config=SimpleNamespace(state_dir=root), creative_jobs=SimpleNamespace(assets=assets))
        self.jobs = MediaJobs(app)

    def test_composite_returns_registered_png(self):
        row = {"job_id": "a" * 32, "kind": "COMPOSITE", "state": "QUEUED", "spec": {"background_asset_id": self.background["asset_id"], "layers": [{"name": "B01", "asset_id": self.layer["asset_id"], "x": 5, "y": 6, "width": 20, "height": 20, "opacity": 100, "flip_x": True}]}}
        self.jobs.active = row["job_id"]
        self.jobs.run(row)
        self.assertEqual(row["state"], "SUCCEEDED")
        output = self.jobs.assets.get(row["output_asset"]["asset_id"])
        with Image.open(output["path"]) as image:
            self.assertEqual(image.size, (100, 60))
            self.assertEqual(image.convert("RGB").getpixel((10, 10)), (255, 0, 0))

    def test_opaque_layer_rejected_with_actionable_error(self):
        opaque = self.jobs.assets.register_base64("opaque.png", base64.b64encode(png_bytes((10, 10), (255, 0, 0, 255))).decode())
        row = {"job_id": "b" * 32, "kind": "COMPOSITE", "state": "QUEUED", "spec": {"background_asset_id": self.background["asset_id"], "layers": [{"name": "B01", "asset_id": opaque["asset_id"], "x": 0, "y": 0, "width": 10, "height": 10, "opacity": 100}]}}
        self.jobs.active = row["job_id"]
        self.jobs.run(row)
        self.assertEqual(row["state"], "FAILED")
        self.assertIn("prepare_image_cutout", row["error"])

    def test_cutout_uses_pinned_runtime_and_registers_alpha(self):
        self.jobs.npx = Path(self.tmp.name) / "npx"
        self.jobs.npx.write_text("stub")
        row = {"job_id": "c" * 32, "kind": "CUTOUT", "state": "QUEUED", "spec": {"asset_id": self.background["asset_id"]}}
        self.jobs.active = row["job_id"]
        def fake_run(command, **kwargs):
            output = Path(command[command.index("-o") + 1])
            output.write_bytes(png_bytes((100, 60), (255, 0, 0, 255), True))
            return SimpleNamespace(returncode=0)
        with patch("subprocess.run", side_effect=fake_run) as run:
            self.jobs.run(row)
        self.assertEqual(row["state"], "SUCCEEDED")
        self.assertIn("hyperframes@0.8.29", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
