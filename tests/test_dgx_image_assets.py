import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from DGXGateway.dgx_gateway.image_assets import ImageAssets, MAX_IMAGE_BYTES


class ImageAssetsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "assets"
        self.assets = ImageAssets(self.root)
        self.data = b"\x89PNG\r\n\x1a\nreference fixture"
        self.encoded = base64.b64encode(self.data).decode()

    def test_register_get_deduplicate_and_private_files(self):
        registered = self.assets.register_base64("参考.png", self.encoded)
        self.assertEqual(registered["asset_id"], hashlib.sha256(self.data).hexdigest())
        self.assertEqual(Path(registered["path"]).read_bytes(), self.data)
        self.assertEqual(registered["content_type"], "image/png")
        self.assertEqual(self.assets.get(registered["asset_id"]), registered)
        self.assertEqual(self.assets.register_base64("different.png", self.encoded), registered)
        self.assertEqual(len(list(self.root.iterdir())), 1)
        for path in (self.root, Path(registered["path"]).parent):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertEqual(Path(registered["path"]).stat().st_mode & 0o777, 0o600)

    def test_other_magic_and_extension_from_content(self):
        for data, ext, mime in [(b"\xff\xd8\xfffixture", "jpg", "image/jpeg"),
                                (b"RIFF\x04\x00\x00\x00WEBP", "webp", "image/webp")]:
            result = self.assets.register_base64("wrong.png", base64.b64encode(data).decode())
            self.assertTrue(result["filename"].endswith("." + ext))
            self.assertEqual(result["content_type"], mime)

    def test_invalid_encoding_magic_and_size(self):
        for encoded in ("", None, "%%%%", "data:image/png;base64," + self.encoded,
                        self.encoded + "\n", self.encoded + "=", "aGVsbG8=", "é"):
            with self.subTest(encoded=str(encoded)[:30]), self.assertRaises(ValueError):
                self.assets.register_base64("x.png", encoded)
        oversized = self.data + b"x" * MAX_IMAGE_BYTES
        with self.assertRaises(ValueError):
            self.assets.register_base64("x.png", base64.b64encode(oversized).decode())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_traversal_and_invalid_ids(self):
        for filename in ("../x.png", "/tmp/x.png", "a/b.png", "a\\b.png", ".", "..", "a\x00.png", None):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                self.assets.register_base64(filename, self.encoded)
        for asset_id in ("../outside", "a" * 63, "A" * 64, "g" * 64, None):
            with self.assertRaises(ValueError):
                self.assets.get(asset_id)

    def test_exact_size_limit_is_accepted(self):
        data = self.data + b"x" * (MAX_IMAGE_BYTES - len(self.data))
        result = self.assets.register_base64("limit.png", base64.b64encode(data).decode())
        self.assertEqual(result["size_bytes"], MAX_IMAGE_BYTES)

    def test_noncanonical_pad_bits_rejected(self):
        # Both encodings decode to the same bytes; strict transport accepts only one.
        canonical = base64.b64encode(b"\xff\xd8\xffx").decode()
        alternate = canonical[:-3] + "B=="
        self.assertEqual(base64.b64decode(canonical), base64.b64decode(alternate))
        with self.assertRaisesRegex(ValueError, "canonical"):
            self.assets.register_base64("x.jpg", alternate)

    def test_integrity_failure_is_not_silently_repaired(self):
        result = self.assets.register_base64("x.png", self.encoded)
        Path(result["path"]).write_bytes(self.data + b"modified")
        for action in (lambda: self.assets.get(result["asset_id"]),
                       lambda: self.assets.register_base64("x.png", self.encoded)):
            with self.assertRaisesRegex(ValueError, "integrity"):
                action()

    def test_metadata_cannot_redirect_reads(self):
        result = self.assets.register_base64("x.png", self.encoded)
        metadata_path = Path(result["path"]).parent / "metadata.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["filename"] = "../../outside"
        metadata_path.write_text(json.dumps(metadata))
        with self.assertRaises(ValueError):
            self.assets.get(result["asset_id"])

    def test_symlink_rejected(self):
        result = self.assets.register_base64("x.png", self.encoded)
        path = Path(result["path"])
        outside = Path(self.tmp.name) / "outside.png"
        outside.write_bytes(self.data)
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.assets.get(result["asset_id"])

    def test_concurrent_dedup_is_complete_and_atomic(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.assets.register_base64("x.png", self.encoded), range(24)))
        self.assertTrue(all(row == results[0] for row in results))
        self.assertEqual([p.name for p in self.root.iterdir()], [results[0]["asset_id"]])


if __name__ == "__main__":
    unittest.main()

class QwenEncoderVariantTests(unittest.TestCase):
 def test_encoder_selection_changes_only_encoder_weights(self):
  from DGXGateway.dgx_gateway.image_workflows import qwen_image_edit
  standard=qwen_image_edit('same prompt',['reference.png'])
  variant=qwen_image_edit('same prompt',['reference.png'],encoder='huihui-abliterated-bf16')
  self.assertEqual(variant['1'],standard['1'])
  self.assertEqual(variant['2']['inputs']['clip_name'],'qwen_2.5_vl_7b_huihui_abliterated_bf16.safetensors')
  variant['2']=standard['2']
  self.assertEqual(variant,standard)
 def test_arbitrary_encoder_path_rejected(self):
  from DGXGateway.dgx_gateway.image_workflows import qwen_image_edit
  with self.assertRaises(ValueError):qwen_image_edit('x',['a.png'],encoder='../../other-model')


class QwenBatchTests(unittest.TestCase):
    def test_precision_and_batch_are_preserved_in_remote_graph(self):
        from DGXGateway.dgx_gateway.image_workflows import qwen_image_edit
        graph = qwen_image_edit("sunset", ["reference.png"], seed=42, model_precision="bf16", batch_count=3)
        self.assertEqual(graph['1']['inputs']['unet_name'], 'qwen_image_edit_2511_bf16.safetensors')
        samplers=[n for n in graph.values() if n['class_type']=='DGXSeedBatchKSampler']
        self.assertEqual([n['inputs']['seed'] for n in samplers], [42,42])
        self.assertEqual([n['inputs']['candidate_indices'] for n in samplers], ['[0, 1]', '[2]'])
        self.assertEqual(len([n for n in graph.values() if n['class_type']=='DGXBatchSaveImage']),2)
        for sampler in samplers:
            self.assertEqual(sampler['inputs']['latent_image'], ['11',0])
        for node in graph.values():
            for value in node['inputs'].values():
                if isinstance(value,list): self.assertIn(value[0],graph)

    def test_invalid_batch_or_precision_rejected(self):
        from DGXGateway.dgx_gateway.image_workflows import qwen_image_edit
        for count in (0,9,1.5,True):
            with self.assertRaises(ValueError): qwen_image_edit("x",["ref.png"],batch_count=count)
        with self.assertRaises(ValueError): qwen_image_edit("x",["ref.png"],model_precision="unknown")
