"""Offline contract checks; GPU correctness/performance require the real probe."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

P=Path(__file__).resolve().parents[1]/'plans/MiniMaxH34DGX/h3-ring-kit-v2/patches/performance/fp8_probe.py'
spec=importlib.util.spec_from_file_location('fp8_probe_test',P)
probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
DEV=SimpleNamespace(type='cuda')

def tensor(shape,dtype,stride):
    return SimpleNamespace(shape=shape,ndim=len(shape),dtype=dtype,stride=lambda axis=None:stride if axis is None else stride[axis],
        device=DEV,numel=lambda:shape[0]*shape[1],element_size=lambda:2)

class FP8ProbeTests(unittest.TestCase):
    def operands(self,k=7168):
        n=5376
        return (tensor((62496,k),'fp8',(k,1)),tensor((k,n),'fp8',(1,k)),
                tensor((62496,1),'f32',(1,1)),tensor((n,1),'f32',(1,1)))

    def test_scopes_actual_fp8_large_row_column_scaled_bias_free_operands(self):
        t=SimpleNamespace(float8_e4m3fn='fp8',bfloat16='bf16',float32='f32')
        args=self.operands();self.assertTrue(probe.eligible(*args,'bf16',None,t))
        self.assertFalse(probe.eligible(*args,'bf16',object(),t))
        self.assertFalse(probe.eligible(*args,'f32',None,t))
        args[2].numel=lambda:1
        self.assertFalse(probe.eligible(*args,'bf16',None,t))

    def test_bounded_probe_returns_baseline_and_restores_original_even_after_error(self):
        for fail in (False,True):
            baselines=[];candidates=[]
            def original(a,b,sa,sb,out_dtype,bias=None):
                result=tensor((a.shape[0],b.shape[1]),'bf16',(b.shape[1],1));baselines.append(result);return result
            def candidate(*args,**kwargs):
                candidates.append(kwargs)
                self.assertEqual(kwargs,probe.CONFIG)
                if fail:raise RuntimeError('compile refused')
                return tensor((62496,args[1].shape[1]),'bf16',(args[1].shape[1],1))
            fp8=SimpleNamespace(fp8_scaled_mm=original)
            event=lambda **kw:SimpleNamespace(record=lambda:None,synchronize=lambda:None,elapsed_time=lambda e:1.0)
            torch=SimpleNamespace(float8_e4m3fn='fp8',bfloat16='bf16',float32='f32',cuda=SimpleNamespace(Event=event,synchronize=lambda:None))
            modules={'torch':torch,'sglang.srt.layers.quantization':SimpleNamespace(fp8_utils=fp8),
                     'sglang.multimodal_gen.runtime.utils.h3_fp8_aligned':SimpleNamespace(triton_scaled_mm=candidate)}
            with patch.dict(sys.modules,modules),tempfile.TemporaryDirectory() as d,patch.object(Path,'read_text',return_value='MemAvailable: 67108864 kB\n'),patch.object(probe,'compare',return_value={'bitwise_equal':False}):
                restore=probe.install(Path(d))
                if fail:
                    with self.assertRaisesRegex(RuntimeError,'compile refused'):
                        fp8.fp8_scaled_mm(*self.operands(),'bf16')
                else:
                    for n in (7168,14336):
                        offset=len(baselines);out=fp8.fp8_scaled_mm(*self.operands(n),'bf16');self.assertIs(out,baselines[offset])
                    fp8.fp8_scaled_mm(*self.operands(),'bf16')
                    self.assertEqual(len(candidates),6)
                restore();self.assertIs(fp8.fp8_scaled_mm,original)
                with (Path(d)/'fp8-probe.jsonl').open() as f:rows=[json.loads(s) for s in f]
                self.assertEqual(len(rows),1 if fail else 2)
                self.assertEqual('error' in rows[0],fail)
