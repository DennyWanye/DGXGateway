"""Wire contract from the locked DSpark harness, not another model's API."""
import hashlib
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from DGXGateway.dgx_gateway.text_jobs import FixedTextHTTP
from DGXGateway.dgx_gateway.text_qualification import source_hashes
from tests import test_dgx_gateway_text_jobs as http_fixture


ROOT = Path(__file__).parents[1]


class NonThinkingRequestTests(unittest.TestCase):
    def test_all_four_kinds_send_the_dspark_non_thinking_field_on_wire(self):
        # Independent oracle: locked source scripts/ruler-lite.py lines 98–104,
        # 270–272 and docker-compose.dspark.yml lines 317–320. DeepSeek uses
        # "thinking"; "enable_thinking" is the Qwen3 spelling, not this contract.
        fixture = http_fixture.FixedTextHTTPTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        alias = json.loads((ROOT / 'manifests/deepseek-v4-flash-consumers.json').read_text())['model']
        for kind in ('extract_entities', 'draft_beats', 'draft_script', 'draft_storyboard'):
            with self.subTest(kind=kind):
                hostile_data = {'chat_template_kwargs': {'thinking': True}, 'enable_thinking': True,
                                'max_tokens': 999999, 'model': 'another-model'}
                fixture.transport.connection_factory = fixture.connection([json.dumps({
                    'model': alias, 'choices': [{'finish_reason': 'stop', 'message': {'content': '{}'}}]
                }).encode()])
                fixture.transport.generate(fixture.transport.payload(kind, hostile_data), lambda: None)
                method, path, body, _ = fixture.captured
                request = json.loads(body)
                self.assertEqual((method, path), ('POST', '/v1/chat/completions'))
                self.assertEqual(request['chat_template_kwargs'], {'thinking': False})
                self.assertIs(request['chat_template_kwargs']['thinking'], False)
                self.assertNotIn('enable_thinking', request)
                self.assertNotIn('reasoning_effort', request)
                self.assertNotIn('thinking_token_budget', request)
                self.assertEqual(request['model'], alias)
                self.assertEqual(request['max_tokens'], 4096)
                self.assertEqual(request['temperature'], 0.4)
                self.assertIs(request['stream'], False)
                self.assertEqual(json.loads(request['messages'][1]['content']), hostile_data)

    def test_request_change_remains_in_qualification_source_closure(self):
        sources = source_hashes(SimpleNamespace(repo_root=ROOT))
        relative = 'DGXGateway/dgx_gateway/text_jobs.py'
        raw = (ROOT / relative).read_bytes()
        self.assertEqual(sources[relative], hashlib.sha256(raw).hexdigest())
        old = raw.replace(b"                'chat_template_kwargs': {'thinking': False},\n", b'', 1)
        self.assertNotEqual(sources[relative], hashlib.sha256(old).hexdigest())
        self.assertEqual(inspect.signature(FixedTextHTTP.generate).parameters['timeout'].default, 240)


if __name__ == '__main__':
    unittest.main()
