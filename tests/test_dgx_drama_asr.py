"""ASR output boundary oracles using a real local subprocess, no model inference."""
import json
from pathlib import Path
import tempfile
import unittest

from DGXGateway.dgx_gateway.drama.asr import transcribe
from DGXGateway.dgx_gateway.drama.contracts import DramaError


class AsrTests(unittest.TestCase):
    def run_worker(self, rows, timeout=10, code=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = root / 'worker'
            worker.write_text('#!/usr/bin/env python3\nimport sys,json,time\nfrom pathlib import Path\n' + (code or 'Path(sys.argv[sys.argv.index("-of")+1]+".json").write_text(' + repr(json.dumps({'transcription': rows})) + ')\n'))
            worker.chmod(0o700)
            model, audio = root / 'model.bin', root / 'actual.wav'
            model.write_bytes(b'fake-local-model'); audio.write_bytes(b'fixture')
            return transcribe(audio, worker=str(worker), model=str(model), total_frames=48, timeout=timeout)

    def test_shared_nonframe_boundary_quantizes_consistently(self):
        segments, source = self.run_worker([{'offsets': {'from': 0, 'to': 1001}, 'text': '第一句'}, {'offsets': {'from': 1001, 'to': 2000}, 'text': '第二句'}])
        self.assertEqual([(s['startFrame'], s['endFrame']) for s in segments], [(0, 24), (24, 48)])
        self.assertEqual(source['rawSegments'][0]['offsets']['to'], 1001)

    def test_raw_overlap_reversed_and_oob_rejected(self):
        for start, end in [(999, 1900), (1500, 1400), (1001, 2200), (1001, 2019), (-1, 1200)]:
            with self.subTest(start=start, end=end), self.assertRaises(DramaError) as error:
                self.run_worker([{'offsets': {'from': 0, 'to': 1001}, 'text': 'a'}, {'offsets': {'from': start, 'to': end}, 'text': 'b'}])
            self.assertEqual(error.exception.code, 'ASR_OUTPUT_INVALID')
            self.assertTrue(error.exception.asr_result['requiresCorrection'])
            self.assertEqual(error.exception.asr_result['raw']['transcription'][1]['offsets']['to'], end)

    def test_silence_empty_draft(self):
        self.assertEqual(self.run_worker([])[0], [])

    def test_timeout_and_process_failure(self):
        for code, expected in [('time.sleep(10)', 'ASR_TIMEOUT'), ('sys.exit(2)', 'ASR_FAILED')]:
            with self.subTest(expected=expected), self.assertRaises(DramaError) as error:
                self.run_worker([], timeout=.1 if expected == 'ASR_TIMEOUT' else 2, code=code)
            self.assertEqual(error.exception.code, expected)

    def test_output_limit(self):
        with self.assertRaises(DramaError) as error:
            self.run_worker([], code='Path(sys.argv[sys.argv.index("-of")+1]+".json").write_bytes(b"x"*(4*1024*1024+1))')
        self.assertIn(error.exception.code, ('ASR_OUTPUT_LIMIT', 'ASR_OUTPUT_INVALID'))

    def test_nonfinite_numeric_literal_and_malformed_json_preserved(self):
        for raw in ('{"transcription":[{"offsets":{"from":0,"to":1e309},"text":"bad"}]}', '{malformed', '{"transcription":[{"offsets":{"from":0,"to":' + '9' * 400 + '},"text":"bad"}]}'):
            with self.subTest(raw=raw), self.assertRaises(DramaError) as error:
                self.run_worker([], code='Path(sys.argv[sys.argv.index("-of")+1]+".json").write_text(' + repr(raw) + ')')
            self.assertEqual(error.exception.code, 'ASR_OUTPUT_INVALID')
            self.assertEqual(error.exception.asr_result['rawText'], raw)
            json.dumps(error.exception.asr_result, allow_nan=False)
