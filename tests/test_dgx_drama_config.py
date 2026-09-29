"""Feature flag integration through the real Gateway application factory."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from DGXGateway.dgx_gateway.config import load_config
from DGXGateway.dgx_gateway.__main__ import build_application


class DramaConfigTests(unittest.TestCase):
    def test_absent_flag_defaults_off_and_non_boolean_is_rejected(self):
        example = Path(__file__).parents[1] / "DGXGateway/config/gateway.example.toml"
        value = example.read_text()
        start, end = value.index("[drama]"), value.index("[cluster]")
        without = value[:start] + value[end:]
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "gateway.toml"
            config.write_text(without)
            self.assertFalse(load_config(config).drama_enabled)
            config.write_text(without + '\n[drama]\nenabled="false"\n')
            with self.assertRaisesRegex(ValueError, "drama.enabled"):
                load_config(config)

    def test_factory_only_initializes_drama_when_enabled(self):
        example = Path(__file__).parents[1] / "DGXGateway/config/gateway.example.toml"
        base = load_config(example)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for enabled in (False, True):
                state = root / str(enabled)
                config = replace(base, state_dir=state, drama_enabled=enabled, mutation_enabled=False)
                with patch("DGXGateway.dgx_gateway.__main__.load_config", return_value=config), \
                     patch("DGXGateway.dgx_gateway.recording.load_recording", return_value=None):
                    app = build_application(example)
                self.assertEqual(hasattr(app, "drama"), enabled)
                self.assertEqual((state / "drama/drama.sqlite3").exists(), enabled)
                if enabled:
                    self.assertEqual(app.drama.projects(), {"projects": []})
                    app.drama.close()


if __name__ == "__main__":
    unittest.main()
