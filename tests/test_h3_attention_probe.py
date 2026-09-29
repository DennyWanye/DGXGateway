import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


class AttentionProbeWrapperTests(unittest.TestCase):
    def test_next_probe_keeps_production_output_cache_and_restores_on_error(self):
        path = Path(__file__).resolve().parents[1] / 'plans/MiniMaxH34DGX/h3-ring-kit-v2/patches/performance/diagnostic.py'
        spec = importlib.util.spec_from_file_location('h3_next_probe_test', path)
        diagnostic = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(diagnostic)
        for fail in (False, True):
            interface = ModuleType('interface')
            kernel = type('Kernel', (), {'get_fwd_num_threads': staticmethod(lambda *a, **k: 128)})
            seen = []
            def original(q, k, v, return_lse=True, tile_mn=None, out=None, lse=None):
                self.assertIs(interface._flash_attn_fwd.compile_cache, original.compile_cache)
                self.assertEqual(kernel.get_fwd_num_threads(128,128,*tile_mn),256)
                if fail and tile_mn == (256,64):
                    raise RuntimeError('candidate rejected')
                value = tuple([None,None])
                seen.append((tile_mn,value))
                return value
            original.compile_cache = {}
            interface._flash_attn_fwd = original
            event = lambda **kw: SimpleNamespace(record=lambda:None, synchronize=lambda:None, elapsed_time=lambda end:1.0)
            modules = {'torch': SimpleNamespace(bfloat16='bf16',cuda=SimpleNamespace(Event=event,synchronize=lambda:None)),
                'sglang.kernels.ops.attention.flash_attn.cute': SimpleNamespace(interface=interface),
                'sglang.kernels.ops.attention.fa4_sm120.flash_fwd': SimpleNamespace(FlashAttentionForwardSm120=kernel)}
            q = SimpleNamespace(dtype='bf16',shape=(62496,56,128),stride=lambda:(21504,128,1))
            k = SimpleNamespace(dtype='bf16',shape=(62475,56,128))
            with patch.dict(sys.modules,modules), tempfile.TemporaryDirectory() as directory:
                restore_current = diagnostic.install_attention_candidate()
                current = interface._flash_attn_fwd
                restore_probe = diagnostic.install_next_attention_probe(Path(directory))
                if fail:
                    with self.assertRaisesRegex(RuntimeError,'candidate rejected'):
                        interface._flash_attn_fwd(q,k,k,return_lse=True)
                else:
                    value=interface._flash_attn_fwd(q,k,k,return_lse=True)
                    self.assertIs(value,seen[0][1])
                    self.assertEqual([x[0] for x in seen],[(128,64),(256,64),(128,64),(256,64),(128,64),(256,64)])
                self.assertEqual(kernel.get_fwd_num_threads(),128)
                row=json.loads((Path(directory)/'attention-next-probe.jsonl').read_text())
                self.assertEqual('error' in row,fail)
                restore_probe()
                self.assertIs(interface._flash_attn_fwd,current)
                restore_current()
                self.assertIs(interface._flash_attn_fwd,original)

    def test_candidate_uses_measured_shape_and_restores_threads_on_failure(self):
        path = Path(__file__).resolve().parents[1] / 'plans/MiniMaxH34DGX/h3-ring-kit-v2/patches/performance/diagnostic.py'
        spec = importlib.util.spec_from_file_location('h3_candidate_test', path)
        diagnostic = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(diagnostic)
        interface = ModuleType('interface')
        kernel = type('Kernel', (), {'get_fwd_num_threads': staticmethod(lambda *a, **k: 128)})
        def original(q, k, v, return_lse=True, tile_mn=None, fail=False):
            if fail:
                raise RuntimeError('kernel failure')
            return tile_mn, kernel.get_fwd_num_threads(128,128,*(tile_mn or (64,64))), interface._flash_attn_fwd.compile_cache
        original.compile_cache = {}
        interface._flash_attn_fwd = original
        modules = {'torch': SimpleNamespace(bfloat16='bf16'),
            'sglang.kernels.ops.attention.flash_attn.cute': SimpleNamespace(interface=interface),
            'sglang.kernels.ops.attention.fa4_sm120.flash_fwd': SimpleNamespace(FlashAttentionForwardSm120=kernel)}
        q = SimpleNamespace(dtype='bf16', shape=(62496,56,128), stride=lambda:(21504,128,1))
        k = SimpleNamespace(dtype='bf16', shape=(62475,56,128))
        with patch.dict(sys.modules, modules):
            restore = diagnostic.install_attention_candidate()
            result = interface._flash_attn_fwd(q,k,k)
            # bind_partial omits defaults: explicit return_lse is required.
            self.assertEqual(result[:2], (None,128))
            self.assertEqual(interface._flash_attn_fwd(q,k,k,return_lse=True)[:2], ((128,64),256))
            self.assertEqual(kernel.get_fwd_num_threads(),128)
            with self.assertRaises(RuntimeError):
                interface._flash_attn_fwd(q,k,k,return_lse=True,fail=True)
            self.assertEqual(kernel.get_fwd_num_threads(),128)
            q.shape=(31248,56,128)
            self.assertEqual(interface._flash_attn_fwd(q,k,k,return_lse=True)[:2], (None,128))
            restore()
            self.assertIs(interface._flash_attn_fwd, original)

    def test_preserves_self_referenced_compile_cache_and_restores_function(self):
        path = Path(__file__).resolve().parents[1] / 'plans/MiniMaxH34DGX/h3-ring-kit-v2/patches/performance/diagnostic.py'
        spec = importlib.util.spec_from_file_location('h3_probe_test', path)
        diagnostic = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(diagnostic)
        interface = ModuleType('interface')
        def original(q=None, k=None):
            # Upstream accesses its cache through the module-global name.
            return interface._flash_attn_fwd.compile_cache
        original.compile_cache = {'existing_kernel': object()}
        interface._flash_attn_fwd = original
        kernel = type('Kernel', (), {'get_fwd_num_threads': staticmethod(lambda *a, **k: 128)})
        modules = {
            'torch': SimpleNamespace(),
            'sglang.kernels.ops.attention.flash_attn.cute': SimpleNamespace(interface=interface),
            'sglang.kernels.ops.attention.fa4_sm120.flash_fwd': SimpleNamespace(FlashAttentionForwardSm120=kernel),
        }
        with patch.dict(sys.modules, modules), tempfile.TemporaryDirectory() as directory:
            restore = diagnostic.install_attention_probe(Path(directory))
            self.assertIs(interface._flash_attn_fwd(), original.compile_cache)
            self.assertIs(interface._flash_attn_fwd.__wrapped__, original)
            restore()
            self.assertIs(interface._flash_attn_fwd, original)
            self.assertEqual(kernel.get_fwd_num_threads(), 128)
