"""Independent review of root's P6 wiring; temporary business DB and localhost only."""
import json
import io
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener

from tests import test_dgx_drama_scripts as fixture
from DGXGateway.dgx_gateway.drama.business_mcp import call, rpc, tools_list, CALL_SOURCE
from DGXGateway.dgx_gateway.server import GatewayHTTPServer
from DGXGateway.dgx_gateway.drama.contracts import DramaError
from DGXGateway.dgx_gateway.drama import gateway_text_adapter as text_adapter_module


class WiringReviewTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.ScriptTests()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.s = self.case.s

    def imported_take(self):
        path = self.s.root / 'temp' / 'review-fixture.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=96x64:r=24:d=4',
                        '-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '1', '-pix_fmt', 'yuv420p', str(path)], check=True)
        data = path.read_bytes()
        asset = self.s.upload(self.case.pid, io.BytesIO(data), len(data), 'CPU审查夹具.mp4', 'review-upload')['asset']
        shot = self.s.create_shot(self.case.eid, {'title': '已有片段', 'assetId': asset['id'], 'requestKey': 'review-shot'})['shot']
        return shot, shot['takes'][0]['id']

    def test_imported_approved_take_builds_valid_unsaved_rough_draft(self):
        shot, tid = self.imported_take()
        self.s.review_take(tid, {'decision': 'approved', 'notes': '人工夹具批准', 'requestKey': 'review-take'})
        selected = self.s.select_take(shot['id'], {'takeId': tid, 'expectedRevision': shot['revision'], 'requestKey': 'review-select'})['shot']
        result = call(self.s, 'drama_build_rough_timeline', {'projectId': self.case.pid, 'arguments': {'episodeId': self.case.eid}})
        self.assertFalse(result['saved'])
        self.assertEqual(result['draft']['clips'][0]['frames'], 96)
        self.assertEqual(result['draft']['clips'][0]['takeId'], tid)
        self.assertIsNone(self.s.episode(self.case.eid)['timeline'])
        self.assertEqual(self.s.shot_production(shot['id'])['revision'], selected['revision'])
        draft = result['draft']
        self.s.save_timeline(self.case.eid, {k: v for k, v in {**draft, 'requestKey': 'human-save-rough'}.items() if k != 'episodeId'})

    def test_review_suggestion_is_idempotent_scoped_and_cannot_change_human_review(self):
        shot, tid = self.imported_take()
        self.s.review_take(tid, {'decision': 'rejected', 'notes': '人工拒绝', 'requestKey': 'human-reject'})
        body = {'takeId': tid, 'suggestion': '模型建议：可留作备用', 'requestKey': 'suggest-only'}
        envelope = {'projectId': self.case.pid, 'arguments': body}
        first = call(self.s, 'drama_record_review_suggestion', envelope)
        self.assertEqual(first, call(self.s, 'drama_record_review_suggestion', envelope))
        self.assertFalse(first['suggestion']['changesApproval'])
        other = self.s.create_project({'title': '另一个项目', 'requestKey': 'other-review-project'})['project']['id']
        with self.assertRaises(DramaError):
            call(self.s, 'drama_record_review_suggestion', {**envelope, 'projectId': other})
        with self.s.store.connect() as db:
            self.assertEqual(db.execute('SELECT decision FROM dr_take_reviews WHERE take_id=?', (tid,)).fetchone()[0], 'rejected')
            self.assertIsNone(db.execute('SELECT selected_take_id FROM dr_shots WHERE id=?', (shot['id'],)).fetchone()[0])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM dr_events WHERE kind='review_suggestion'").fetchone()[0], 1)

    def test_mcp_publishes_actionable_argument_schemas(self):
        expected = {'drama_extract_entities': {'episodeId', 'sourceRevisionIds', 'requestKey'},
                    'drama_draft_script': {'episodeId', 'sourceRevisionIds', 'requestKey'},
                    'drama_propose_entity_revision': {'entityId', 'expectedRevision', 'name', 'requestKey'},
                    'drama_request_keyframe': {'shotRevisionId', 'tool', 'parameters', 'requestKey'},
                    'drama_request_entity_image': {'entityRevisionId', 'tool', 'parameters', 'requestKey'},
                    'drama_request_video_take': {'episodeId', 'shots', 'requestKey'},
                    'drama_get_job': {'jobId', 'kind'},
                    'drama_create_shot': {'episodeId', 'title', 'requestKey'},
                    'drama_revise_shot': {'shotId', 'expectedRevision', 'baseRevisionId', 'snapshot', 'requestKey'}}
        for tool in tools_list():
            if tool['name'] not in expected: continue
            with self.subTest(tool=tool['name']):
                schema = tool['inputSchema']['properties']['arguments']
                self.assertTrue(expected[tool['name']] <= set(schema.get('properties', {})))
                self.assertTrue(expected[tool['name']] <= set(schema.get('required', [])))
        from copy import deepcopy
        from DGXGateway.dgx_gateway.mcp import TOOLS
        before = deepcopy(TOOLS)
        names = {'generate_image', 'edit_image', 'compose_image_layers'}
        for name in ('drama_request_keyframe', 'drama_request_entity_image'):
            arguments = next(t for t in tools_list() if t['name'] == name)['inputSchema']['properties']['arguments']
            self.assertEqual(set(arguments['properties']['tool']['enum']), names)
            branches = {b['properties']['tool']['const']: b['properties']['parameters'] for b in arguments['oneOf']}
            self.assertEqual(set(branches), names)
            for tool in TOOLS:
                if tool['name'] not in names: continue
                expected_schema = deepcopy(tool['inputSchema'])
                expected_schema['properties'].pop('idempotency_key')
                expected_schema['required'].remove('idempotency_key')
                self.assertEqual(branches[tool['name']], expected_schema)
        self.assertEqual(TOOLS, before)

    def test_invalid_tool_name_is_controlled_rpc_error_without_side_effects(self):
        for name in ([], {}, None, 42):
            with self.subTest(name=name):
                result = rpc(self.s, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                     'params': {'name': name, 'arguments': {}}})
                self.assertTrue('error' in result or result['result']['isError'])
        self.assertEqual(CALL_SOURCE.get(), 'ui_api')

    def test_remote_text_adapter_rejects_ambiguous_or_nonfinite_control_receipt(self):
        class Handler(BaseHTTPRequestHandler):
            payload = b'{}'
            def do_GET(self):
                self.send_response(200); self.send_header('Content-Length', str(len(self.payload)))
                self.end_headers(); self.wfile.write(self.payload)
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(3)))
        with patch.object(text_adapter_module, 'RPC_ORIGIN', f'http://127.0.0.1:{server.server_port}'):
            adapter = text_adapter_module.GatewayTextAdapter()
            for payload in (b'{"generation_qualified":false,"generation_qualified":true}', b'{"value":1e309}'):
                with self.subTest(payload=payload):
                    Handler.payload = payload
                    with self.assertRaises((ValueError, RuntimeError)):
                        adapter.capabilities()
            Handler.payload = b'{"generation_qualified":false}'
            self.assertEqual(adapter.capabilities(), {'generation_qualified': False})

    def test_remote_control_header_and_body_drips_obey_one_total_deadline(self):
        class Handler(BaseHTTPRequestHandler):
            mode = 'header'
            def do_GET(self):
                try:
                    if self.mode == 'header':
                        wire = b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n'
                    else:
                        self.send_response(200); self.send_header('Content-Length', '100')
                        self.end_headers(); wire = b'{' + b' '*99
                    for byte in wire:
                        self.wfile.write(bytes([byte])); self.wfile.flush(); time.sleep(.025)
                except OSError: pass
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(3)))
        with patch.object(text_adapter_module, 'RPC_ORIGIN', f'http://127.0.0.1:{server.server_port}'):
            for mode in ('header', 'body'):
                with self.subTest(mode=mode):
                    Handler.mode = mode
                    adapter = text_adapter_module.GatewayTextAdapter(timeout=.15)
                    started = time.monotonic()
                    with self.assertRaises(Exception): adapter.capabilities()
                    self.assertLess(time.monotonic()-started, .8)

    def test_complete_json_with_incomplete_http_receipt_is_not_confirmed(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                raw = b'{"generation_qualified":false}'
                self.send_response(200); self.send_header('Content-Length', str(len(raw)+10))
                self.end_headers(); self.wfile.write(raw)
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(3)))
        with patch.object(text_adapter_module, 'RPC_ORIGIN', f'http://127.0.0.1:{server.server_port}'):
            with self.assertRaisesRegex(RuntimeError, 'Incomplete'):
                text_adapter_module.GatewayTextAdapter(timeout=1).capabilities()

    def test_all_p6_approval_import_review_routes_reject_duplicate_fields_before_dispatch(self):
        app = SimpleNamespace(drama=self.s, authorized=lambda _: True,
                              config=SimpleNamespace(lan_no_auth=True, allowed_origins=()))
        server = GatewayHTTPServer(('127.0.0.1', 0), app)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(3)))
        http = build_opener(ProxyHandler({}))
        targets = [(f'/story-revisions/{"a"*32}/approve', 'approve_story_revision'),
                   (f'/story-revisions/{"a"*32}/confirm-entity', 'confirm_entity_candidate'),
                   (f'/story-revisions/{"a"*32}/import', 'import_storyboard'),
                   (f'/shots/{"a"*32}/script-review', 'review_script_invalidations')]
        raw = b'{"expectedRevision":0,"expectedRevision":1,"requestKey":"ambiguous"}'
        for path, method in targets:
            with self.subTest(path=path), patch.object(self.s, method, return_value={'unexpected': True}) as mutation:
                request = Request(f'http://127.0.0.1:{server.server_port}/api/v1/drama'+path,
                                  data=raw, headers={'Content-Type': 'application/json'})
                try:
                    with http.open(request, timeout=3) as response: status, body = response.status, json.load(response)
                except HTTPError as response: status, body = response.code, json.load(response)
                self.assertEqual(status, 422, body)
                self.assertEqual(body['error']['code'], 'invalid_json')
                mutation.assert_not_called()

    def test_async_text_mcp_and_repeat_schedule_do_not_duplicate_dispatch_or_actor(self):
        source = self.case.source()
        adapter = fixture.FakeText(json.dumps(self.case.candidate_content(source)))
        self.s.text_adapter = adapter
        body = {'episodeId': self.case.eid, 'sourceRevisionIds': [source['id']], 'requestKey': 'async-mcp'}
        result = call(self.s, 'drama_extract_entities', {'projectId': self.case.pid, 'arguments': body})
        self.s._text_executor.submit(lambda: None).result(timeout=5)
        jid = result['job']['id']
        self.assertEqual(self.s.text_job(jid)['job']['state'], 'SUCCEEDED')
        call(self.s, 'drama_extract_entities', {'projectId': self.case.pid, 'arguments': body})
        self.s._text_executor.submit(lambda: None).result(timeout=5)
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(CALL_SOURCE.get(), 'ui_api')
        with self.s.store.connect() as db:
            events = [json.loads(r[0]) for r in db.execute('SELECT payload FROM dr_events WHERE scope=? AND kind=?', ('text-jobs:'+self.case.eid, 'mutation'))]
        self.assertEqual(events, [{'actor': 'business_mcp', 'requestKey': 'async-mcp'}])

    def test_async_unknown_repeat_start_and_explicit_find_never_resubmit(self):
        source = self.case.source()
        adapter = fixture.FakeText(json.dumps(self.case.candidate_content(source)), lost=True)
        self.s.text_adapter = adapter
        body = {'kind': 'extract_entities', 'sourceRevisionIds': [source['id']], 'requestKey': 'lost-async'}
        result = self.s.start_text_job(self.case.eid, body)
        self.s._text_executor.submit(lambda: None).result(timeout=5)
        jid = result['job']['id']
        self.assertEqual(self.s.text_job(jid)['job']['state'], 'SUBMISSION_UNKNOWN')
        self.s.start_text_job(self.case.eid, body)
        self.s._text_executor.submit(lambda: None).result(timeout=5)
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(self.s.reconcile_text_job(jid)['job']['state'], 'SUCCEEDED')
        self.assertEqual(adapter.calls, 1)


if __name__ == '__main__':
    unittest.main()
