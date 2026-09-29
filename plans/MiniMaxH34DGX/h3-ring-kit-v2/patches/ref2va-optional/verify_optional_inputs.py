"""CPU-only checks using installed SGLang plus this pinned overlay; no model loads."""
import importlib
import importlib.util
import itertools
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
PREFIX = 'sglang.multimodal_gen.runtime.pipelines_core.stages.model_specific_stages.minimax_h3'
for relative in ('request_validation.py', 'presentation.py', 'stages/denoising.py'):
    name = PREFIX + '.' + relative[:-3].replace('/', '.')
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
validation = sys.modules[PREFIX + '.request_validation']
presentation = sys.modules[PREFIX + '.presentation']
denoising = sys.modules[PREFIX + '.stages.denoising']
plans = importlib.import_module(PREFIX + '.resolved_plan')
packing = importlib.import_module(PREFIX + '.packed_sequence')
constants = importlib.import_module(PREFIX + '.constants')

class Tokenizer:
    eos_token_id = 151643
    def __call__(self, text, **kwargs):
        return {'input_ids': [ord(c) for c in text]}
    def convert_tokens_to_ids(self, token):
        return 100 + len(token)

class OptionalInputs(unittest.TestCase):
    def test_all_sixteen_text_image_video_audio_presence_combinations(self):
        for text, image, video, audio in itertools.product((False, True), repeat=4):
            with self.subTest(text=text, image=image, video=video, audio=audio):
                refs = [{'type': t, 'role': 'reference', 'uri': 'file:///data/example.' + ext}
                        for enabled, t, ext in ((image, 'image', 'png'), (video, 'video', 'mp4'), (audio, 'audio', 'wav')) if enabled]
                prompt = 'A dancer moves.' if text else ''
                request = validation.minimax_h3_validate_canonical_request(task='ref2va', prompt=prompt,
                    conditions=refs, target={'short_edge': 768, 'aspect_ratio': '16:9', 'duration_seconds': 7})
                plan = plans.minimax_h3_resolve_plan(request)
                self.assertEqual(plan.task, 'ref2va')
                self.assertEqual(plan.prompt, prompt)
                self.assertEqual(len(plan.materials), len(refs))
                labels = [(r['type'], 1) for r in refs]
                ids, tags = presentation.minimax_h3_ref2va_video_presentation(Tokenizer(), prompt=prompt,
                    condition_labels=labels, image_token_count=4 if image else None,
                    video_block_token_counts=[4] if video else None,
                    video_block_timestamps=[0.0] if video else None)
                self.assertGreater(len(ids), 0)
                self.assertEqual(len(ids), len(tags))
                if not any((text, image, video, audio)):
                    self.assertEqual(ids.tolist(), [Tokenizer.eos_token_id])

    def test_zero_reference_uses_ref2va_denoising_and_empty_reference_blocks(self):
        plan = SimpleNamespace(task='ref2va', materials=())
        batch = SimpleNamespace(extra={constants.MINIMAX_H3_TEXT_EMBEDDINGS_EXTRA_KEY: {},
            constants.MINIMAX_H3_DENOISE_STATE_EXTRA_KEY: dict(latent_t=5, latent_h=4, latent_w=4, audio_t=8),
            constants.MINIMAX_H3_SIGMAS_EXTRA_KEY: {}})
        with patch.object(plans, 'minimax_h3_plan_from_batch', return_value=plan):
            ctx = denoising._resolve_full_loop_context(batch)
        self.assertTrue(ctx.is_ref2va)
        denoising._assemble_condition_rows(ctx)
        self.assertEqual(ctx.ref2va_positive_blocks, [])
        self.assertIsNone(ctx.cond_rows)
        self.assertIsNone(ctx.audio_ref_rows)
        packed = packing.minimax_h3_packed_sequence_ref2va_blocks(text_len=1,
            latent_t=5, latent_h=4, latent_w=4, audio_t=8, ref_blocks=[])
        self.assertEqual(packed['stream_layout']['cond_image_shapes'], ())
        self.assertEqual(packed['stream_layout']['cond_audio_stream_lens'], ())
        self.assertTrue(packed['update_mask'].any())
        self.assertTrue(packed['audio_update_mask'].any())

    def test_reference_text_tokens_unchanged_and_no_fabricated_reference(self):
        ids, tags = presentation.minimax_h3_ref2va_presentation(Tokenizer(), prompt='exact text',
            condition_labels=[], image_token_count=None)
        self.assertEqual(ids.tolist(), [ord(c) for c in 'exact text'])
        self.assertEqual(tags.tolist(), [1] * len('exact text'))

    def test_other_task_validation_not_relaxed(self):
        target = {'short_edge': 768, 'aspect_ratio': '16:9', 'duration_seconds': 4}
        for task, prompt in (('fl2va', 'x'), ('t2va', '')):
            with self.assertRaises(ValueError):
                validation.minimax_h3_validate_canonical_request(task=task, prompt=prompt, conditions=[], target=target)
        with self.assertRaises(ValueError):
            validation.minimax_h3_validate_canonical_request(task='ref2va', prompt=None, conditions=[], target=target)

if __name__ == '__main__':
    unittest.main()
