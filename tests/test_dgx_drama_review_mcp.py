"""Review MCP on isolated SQLite/media, including actual HTTP writes. No GPU."""
import json
import threading
import unittest
from types import SimpleNamespace
from urllib.request import Request, urlopen
from tests import test_dgx_drama_production as fixtures
from DGXGateway.dgx_gateway.drama.business_mcp import call, rpc
from DGXGateway.dgx_gateway.drama.contracts import DramaError
from DGXGateway.dgx_gateway.drama.store import new_id
from DGXGateway.dgx_gateway.server import GatewayHTTPServer


class ReviewMCPTests(unittest.TestCase):
    setUp = fixtures.ProductionTests.setUp
    tearDown = fixtures.ProductionTests.tearDown
    key = fixtures.ProductionTests.key
    entity = fixtures.ProductionTests.entity
    shot = fixtures.ProductionTests.shot
    candidate = fixtures.ProductionTests.candidate

    def invoke(self, name, body, pid=None):
        return call(self.service, name, {'projectId': pid or self.pid, 'arguments': body})

    def fixture(self):
        entity = self.entity(approved=False)
        cid = self.candidate('entity', entity['id'], entity['currentRevisionId'], self.image)
        return entity, cid

    def test_reads_match_workbench_and_batch_review_notes_optional(self):
        entity, cid = self.fixture()
        self.assertEqual(self.invoke('drama_get_project', {}), self.service.project(self.pid))
        self.assertEqual(self.invoke('drama_get_episode', {'episodeId': self.eid}), self.service.episode(self.eid))
        self.assertEqual(self.invoke('drama_list_entities', {}), self.service.entities(self.pid))
        self.assertEqual(call(self.service, 'drama_list_projects', {}), self.service.projects())
        body = {'candidateIds': [cid], 'decision': 'approved', 'requestKey': self.key()}
        result = self.invoke('drama_review_images', body)
        self.assertEqual(result, self.invoke('drama_review_images', body))
        queue = self.invoke('drama_image_review_queue', {'scope': 'episode', 'episodeId': self.eid})
        self.assertEqual(queue['items'][0]['reviewStatus'], 'approved')
        self.assertIsNone(self.service.entity(entity['id'])['entity']['approvedRevisionId'])

    def test_entity_selection_replay_and_explicit_approval(self):
        entity, cid = self.fixture()
        body = {'entityId': entity['id'], 'candidateId': cid, 'referenceAssetId': self.image,
                'expectedRevision': entity['revision'], 'requestKey': self.key()}
        first = self.invoke('drama_select_entity_image', body)
        self.assertEqual(first, self.invoke('drama_select_entity_image', body))
        self.assertIsNone(first['entity']['approvedRevisionId'])
        current = first['entity']
        approved = self.invoke('drama_approve_entity', {'entityId': entity['id'], 'revisionId': current['currentRevisionId'],
                              'expectedRevision': current['revision'], 'requestKey': self.key()})
        self.assertEqual(approved['entity']['approvedRevisionId'], current['currentRevisionId'])
        self.assertEqual(approved['entity']['current']['name'], entity['current']['name'])

    def test_batch_returns_partial_and_replays_success_without_extra_revision(self):
        entities = [self.fixture(), self.fixture()]
        selections = [{'targetType': 'entity', 'targetId': e['id'], 'candidateId': c, 'assetId': self.image,
                       'expectedRevision': e['revision'] if i == 0 else 999, 'requestKey': self.key()}
                      for i, (e,c) in enumerate(entities)]
        result = self.invoke('drama_select_images', {'selections': selections})
        self.assertFalse(result['complete'])
        self.assertTrue(result['results'][0]['ok'])
        self.assertEqual(result['results'][1]['error']['code'], 'revision_conflict')
        self.assertEqual(result, self.invoke('drama_select_images', {'selections': selections}))
        with self.assertRaisesRegex(DramaError, '同一参考槽位'):
            self.invoke('drama_select_images', {'selections': [selections[0], selections[0]]})

    def test_cross_project_bulk_is_rejected_without_partial_quality_writes(self):
        entity, cid = self.fixture()
        other = self.service.create_project({'title': 'other', 'requestKey': self.key()})['project']['id']
        for name, body in [('drama_get_shot', {'shotId': self.shot()}),
                           ('drama_review_images', {'candidateIds': [cid], 'decision': 'approved', 'requestKey': self.key()}),
                           ('drama_select_entity_image', {'entityId': entity['id'], 'candidateId': cid, 'referenceAssetId': self.image, 'expectedRevision': entity['revision'], 'requestKey': self.key()})]:
            with self.assertRaises(DramaError): self.invoke(name, body, other)
        self.assertEqual(self.service.image_review_queue(self.pid, scope='project')['items'][0]['reviewStatus'], 'pending')

    def test_shot_candidate_selection_and_stale_conflict(self):
        sid = self.shot()
        shot = self.invoke('drama_get_shot', {'shotId': sid})
        cid = self.candidate('shot', sid, shot['currentRevisionId'], self.image)
        body = {'shotId': sid, 'candidateId': cid, 'assetId': self.image, 'expectedRevision': shot['revision'], 'requestKey': self.key()}
        selected = self.invoke('drama_select_shot_image', body)
        self.assertEqual(selected, self.invoke('drama_select_shot_image', body))
        with self.assertRaises(DramaError) as exc:
            self.invoke('drama_select_shot_image', {**body, 'requestKey': self.key()})
        self.assertEqual(exc.exception.code, 'revision_conflict')
        with self.assertRaises(DramaError) as exc:
            self.invoke('drama_select_shot_image', {**body, 'expectedRevision': selected['revision'], 'requestKey': self.key()})
        self.assertEqual(exc.exception.code, 'stale_candidate')
        self.assertIsNone(self.service.episode(self.eid)['timeline'])

    def test_video_review_selection_no_notes_and_no_timeline_write(self):
        sid = self.shot(); shot = self.service.shot_production(sid); tid = new_id()
        with self.service.store.connect(write=True) as db:
            db.execute('INSERT INTO dr_takes(id,shot_id,asset_id,source,created_at,source_revision_id) VALUES(?,?,?,?,?,?)',
                       (tid, sid, self.image, 'h3_video', 1, shot['currentRevisionId']))
        body = {'shotId': sid, 'takeId': tid, 'expectedRevision': shot['revision'], 'requestKey': self.key()}
        with self.assertRaises(DramaError) as exc: self.invoke('drama_select_take', body)
        self.assertEqual(exc.exception.code, 'review_required')
        self.invoke('drama_review_take', {'takeId': tid, 'decision': 'approved', 'requestKey': self.key()})
        first = self.invoke('drama_select_take', body)
        self.assertEqual(first, self.invoke('drama_select_take', body))
        self.assertIsNone(self.service.episode(self.eid)['timeline'])

    def test_shot_review_can_omit_reason(self):
        face = self.entity()
        sid = self.shot(characterRevisionIds=[face['currentRevisionId']])
        changed = fixtures.ProductionTests.revise_entity(self, face)
        fixtures.ProductionTests.approve(self, changed)
        shot = self.service.shot_production(sid)
        result = self.invoke('drama_review_shot', {'shotId': sid, 'expectedRevision': shot['revision'],
                             'invalidationIds': [shot['invalidations'][0]['id']], 'requestKey': self.key()})
        self.assertTrue(result['invalidations'][0]['resolved'])

    def test_empty_candidate_cannot_bypass_candidate_validation(self):
        sid = self.shot()
        with self.assertRaises(DramaError):
            self.invoke('drama_select_shot_image', {'shotId': sid, 'candidateId': '', 'assetId': self.image,
                        'expectedRevision': 1, 'requestKey': self.key()})

    def test_document_catalog_matches_advertised_contracts(self):
        from pathlib import Path
        from DGXGateway.dgx_gateway.drama.business_mcp import tools_list
        root = Path(__file__).resolve().parents[1]
        saved = json.loads((root/'DGXGateway/static/drama-mcp-tools.json').read_text())
        self.assertEqual(saved['tools'], tools_list())
        page = (root/'DGXGateway/static/mcp-guide.html').read_text()
        for tool in tools_list(): self.assertIn('id="'+tool['name']+'"', page)

    def test_invalid_shapes_are_mcp_errors_not_server_exceptions(self):
        for name, body in [('drama_image_review_queue', {'scope': []}),
                           ('drama_review_take', {'takeId': 'a'*32, 'decision': [], 'requestKey': 'x'}),
                           ('drama_select_images', {'selections': [None]}),
                           ('drama_review_images', {'candidateIds': [], 'decision': 'approved', 'requestKey': 'x'})]:
            result = rpc(self.service, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
                'name': name, 'arguments': {'projectId': self.pid, 'arguments': body}}})
            self.assertTrue(result['result']['isError'])

    def test_real_http_mcp_review_persists_with_business_actor(self):
        _, cid = self.fixture()
        app = SimpleNamespace(drama=self.service, authorized=lambda _: True,
                              config=SimpleNamespace(lan_no_auth=True, allowed_origins=('http://localhost',)))
        server = GatewayHTTPServer(('127.0.0.1', 0), app)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            message = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'drama_review_images',
                       'arguments': {'projectId': self.pid, 'arguments': {'candidateIds': [cid], 'decision': 'approved', 'requestKey': 'http-review'}}}}
            req = Request(f'http://127.0.0.1:{server.server_port}/api/v1/drama/mcp', data=json.dumps(message).encode(), headers={'Content-Type': 'application/json'})
            with urlopen(req, timeout=5) as response: result = json.load(response)
            self.assertFalse(result['result']['isError'])
            with self.service.store.connect() as db:
                self.assertEqual(db.execute('SELECT decision FROM dr_reference_candidate_reviews WHERE candidate_id=?', (cid,)).fetchone()[0], 'approved')
                event = db.execute('SELECT payload FROM dr_events WHERE scope=?', ('image-reviews:'+self.pid,)).fetchone()[0]
                self.assertEqual(json.loads(event)['actor'], 'business_mcp')
        finally:
            server.shutdown(); server.server_close(); thread.join(5)
