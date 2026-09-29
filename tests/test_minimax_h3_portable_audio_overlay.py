from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import pickle
import sys
import tempfile
import unittest
import wave


ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "overlays/minimax-h3-two-node/worker.py"

try:
    import torch
except ImportError:
    torch = None


@unittest.skipIf(torch is None, "runtime-only test requires torch")
class PortableAudioOverlayTests(unittest.TestCase):
    def test_pickle_transports_pcm_content_after_source_path_is_removed(self) -> None:
        spec = importlib.util.spec_from_file_location("h3_test_worker_overlay", OVERLAY)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(module)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            path = handle.name
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        with wave.open(path, "wb") as destination:
            destination.setnchannels(1)
            destination.setsampwidth(2)
            destination.setframerate(24_000)
            destination.writeframes(b"\x00\x00\xff\x7f\x00\x80")

        payload = pickle.dumps(module._PortableAudioPath(path))
        os.unlink(path)
        waveform, sample_rate = pickle.loads(payload)

        self.assertEqual(24_000, sample_rate)
        self.assertEqual((1, 3), tuple(waveform.shape))
        self.assertEqual(torch.float32, waveform.dtype)
        self.assertAlmostEqual(0.0, waveform[0, 0].item())
        self.assertGreater(waveform[0, 1].item(), 0.99)
        self.assertEqual(-1.0, waveform[0, 2].item())


if __name__ == "__main__":
    unittest.main()
