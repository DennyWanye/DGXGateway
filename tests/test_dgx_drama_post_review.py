"""Independent P5 review oracles: frozen font bytes cannot change during export.

Only private fixtures are changed. Renderers are mocked to place the replacement
at a deterministic boundary; these tests do not qualify a real font or libass.
"""
import hashlib
import shutil
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_dgx_drama_postproduction import PostproductionTests


class FrozenFontReviewTests(PostproductionTests):
    def test_review_frozen_font_change_before_render_and_between_render_phases(self):
        timeline, _ = self.setup_post()
        font = self.root / "isolated-font.fixture"
        original = b"review fixture font A"
        changed = b"review fixture font B"
        font.write_bytes(original)
        self.service.post_font_file = str(font)
        self.service.post_font_probe = shutil.which("python3")
        subtitle = self.service.save_subtitles(timeline["id"], {
            "segments": [{"startFrame": 0, "endFrame": 24, "text": "fixture"}],
            "expectedRevision": 0, "requestKey": "review-font-subtitle",
        })["subtitle"]
        self.service.approve_subtitles(subtitle["id"], {
            "timelineId": timeline["id"], "requestKey": "review-font-approve",
        })

        for boundary in ("before_render", "after_master"):
            with self.subTest(boundary=boundary):
                font.write_bytes(original)
                entered, proceed = threading.Event(), threading.Event()
                def block_executor():
                    entered.set()
                    if not proceed.wait(10):
                        raise RuntimeError("review barrier timeout")
                self.service._executor.submit(block_executor)
                self.assertTrue(entered.wait(10))

                def master(clips, output, **kwargs):
                    output.write_bytes(b"isolated fake master")
                    if boundary == "after_master":
                        font.write_bytes(changed)
                    return {"kind": "video", "frames": 192, "fps": 24}

                def burn(master_path, captions_path, output, **kwargs):
                    output.write_bytes(b"isolated fake burned result")
                    return {"kind": "video", "frames": 192, "fps": 24,
                            "fontSha256": hashlib.sha256(Path(kwargs["font_file"]).read_bytes()).hexdigest()}

                with patch("DGXGateway.dgx_gateway.drama.post_media.render_post_timeline", side_effect=master), \
                        patch("DGXGateway.dgx_gateway.drama.post_media.burn_subtitles", side_effect=burn) as burned:
                    try:
                        export = self.service.create_export(timeline["id"], {
                            "outputs": ["burned"], "subtitleRevisionId": subtitle["id"],
                            "requestKey": "review-font-" + boundary,
                        })["export"]
                        self.assertEqual(export["recipe"]["runtimeHashes"]["font_file"],
                                         hashlib.sha256(original).hexdigest())
                        if boundary == "before_render":
                            font.write_bytes(changed)
                    finally:
                        proceed.set()
                    result = self.wait_export(export["id"])
                    if result["state"] == "SUCCEEDED":
                        self.assertEqual(boundary, "after_master")
                        burned.assert_called_once()
                        self.assertEqual(result["outputs"][0]["metadata"]["fontSha256"],
                                         hashlib.sha256(original).hexdigest())
                    else:
                        self.assertEqual(result["state"], "FAILED", result)
                        self.assertEqual(result["outputs"], [])
                        burned.assert_not_called()


# Baseline scenarios are executed by their own suites; never count them twice.
for _name in vars(PostproductionTests):
    if _name.startswith("test_"):
        setattr(FrozenFontReviewTests, _name, None)
del PostproductionTests
