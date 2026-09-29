"""Real isolated business service and HTTP. No GPU calls or inherited test counts."""
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from DGXGateway.dgx_gateway.drama.service import DramaService
from DGXGateway.dgx_gateway.drama.business_mcp import call, rpc, tools_list
from DGXGateway.dgx_gateway.drama.contracts import DramaError
from DGXGateway.dgx_gateway.server import GatewayHTTPServer


class BusinessMCPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.s = DramaService(root, root); self.addCleanup(self.s.close)
        self.pid = self.s.create_project({'title': 'P6 tools', 'requestKey': 'p'})['project']['id']
        self.eid = self.s.create_episode(self.pid, {'title': 'e', 'requestKey': 'e'})['episode']['id']
    def call(self, name, body, pid=None):
        return call(self.s, name, {'projectId': pid or self.pid, 'arguments': body})
    def test_tools_cannot_approve_raw_call_or_forge_actor(self):
        names = [tool['name'] for tool in tools_list()]
        self.assertEqual(len(names), 39)
        for name in ('drama_save_timeline', 'drama_create_export', 'drama_import_audio', 'drama_import_video',
                     'drama_import_image', 'drama_create_entity', 'drama_bind_song', 'drama_create_shot', 'drama_revise_shot'):
            self.assertIn(name, names)
        with self.assertRaises(DramaError) as bind:
            self.call('drama_bind_song', {'episodeId': self.eid, 'songAssetId': 'a' * 64,
                                          'expectedRevision': 1, 'requestKey': 'song'})
        self.assertEqual(bind.exception.code, 'not_mv')
        self.assertIsNone(self.s.episode(self.eid)['timeline'])
        for name in ('drama_approve', 'comfy_submit', 'dgx_switch_model', 'drama_publish'):
            with self.assertRaises(DramaError): self.call(name, {})
        entity = self.s.create_entity(self.pid, {'kind': 'character', 'name': '甲', 'requestKey': 'hero'})['entity']
        body = {'entityId': entity['id'], 'expectedRevision': 1, 'name': '乙', 'requestKey': 'propose'}
        with self.assertRaises(DramaError): self.call('drama_propose_entity_revision', {**body, 'actor': 'human'})
        first = self.call('drama_propose_entity_revision', body)
        self.assertEqual(first, self.call('drama_propose_entity_revision', body))
        self.assertIsNone(first['entity']['approvedRevisionId'])
        with self.s.store.connect() as db:
            events = [json.loads(r[0]) for r in db.execute("SELECT payload FROM dr_events WHERE scope=?", ('entity-revisions:'+entity['id'],))]
        self.assertEqual(len(events), 1); self.assertEqual(events[0]['actor'], 'business_mcp')
    def test_other_project_episode_or_entity_rejected_before_service_dispatch(self):
        other = self.s.create_project({'title': 'Other', 'requestKey': 'q'})['project']['id']
        eid = self.s.create_episode(other, {'title': 'Other e', 'requestKey': 'e'})['episode']['id']
        with patch.object(self.s, 'start_text_job') as submit:
            with self.assertRaisesRegex(DramaError, '不属于'):
                self.call('drama_draft_beats', {'episodeId': eid, 'sourceRevisionIds': [], 'requestKey': 'x'})
            submit.assert_not_called()
    def test_text_payload_is_same_as_ui_and_gateway_disabled_is_explicit(self):
        body = {'episodeId': self.eid, 'sourceRevisionIds': [], 'requestKey': 'x'}
        with self.assertRaises(DramaError) as error:
            self.call('drama_draft_script', body)
        self.assertEqual(error.exception.code, 'text_unavailable')
        with patch.object(self.s, 'start_text_job', return_value={'job': 'sentinel'}) as submit:
            result = self.call('drama_draft_script', {**body, 'beatRevisionId': 'a'*32})
            submit.assert_called_once_with(self.eid, {'sourceRevisionIds': [], 'requestKey': 'x', 'beatRevisionId': 'a'*32, 'kind': 'draft_script'})
            self.assertEqual(result, {'job': 'sentinel'})
    def test_video_gate_error_matches_service_without_job_creation(self):
        self.s.video_adapter = SimpleNamespace()
        body = {'shots': [], 'requestKey': 'same'}
        with self.assertRaises(DramaError) as api:
            self.s.submit_video_batch(self.eid, body)
        with self.assertRaises(DramaError) as mcp:
            self.call('drama_request_video_take', {'episodeId': self.eid, **body})
        self.assertEqual(api.exception.code, mcp.exception.code)
        self.assertEqual(self.s.video_jobs(self.pid)['jobs'], [])
    def test_entity_image_request_scopes_revision_and_forwards_slot(self):
        entity = self.s.create_entity(self.pid, {'kind': 'character', 'name': '甲', 'requestKey': 'slot-entity'})['entity']
        body = {'entityRevisionId': entity['currentRevisionId'], 'tool': 'edit_image',
                'parameters': {'prompt': '侧面'}, 'requestKey': 'slot', 'referenceRole': 'identity_3q'}
        with patch.object(self.s, 'submit_reference_job', return_value={'job': 'sentinel'}) as submit:
            result = self.call('drama_request_entity_image', body)
            submit.assert_called_once_with('entity', entity['currentRevisionId'], {
                'tool': 'edit_image', 'parameters': {'prompt': '侧面'}, 'requestKey': 'slot', 'referenceRole': 'identity_3q'})
            self.assertEqual(result, {'job': 'sentinel'})
        other = self.s.create_project({'title': 'Other', 'requestKey': 'other-slot'})['project']['id']
        foreign = self.s.create_entity(other, {'kind': 'character', 'name': '乙', 'requestKey': 'foreign'})['entity']
        with patch.object(self.s, 'submit_reference_job') as submit:
            with self.assertRaisesRegex(DramaError, '不属于'):
                self.call('drama_request_entity_image', {**body, 'entityRevisionId': foreign['currentRevisionId']})
            submit.assert_not_called()
    def test_rough_draft_requires_approved_material_and_does_not_save(self):
        with self.assertRaises(DramaError):
            self.call('drama_build_rough_timeline', {'episodeId': self.eid})
        self.assertIsNone(self.s.episode(self.eid)['timeline'])
    def test_rpc_read_only_surface_and_unknown_method(self):
        listing = rpc(self.s, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        self.assertEqual(len(listing['result']['tools']), 39)
        result = rpc(self.s, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'dgx_switch_model', 'arguments': {}}})
        self.assertTrue(result['result']['isError'])
        self.assertEqual(rpc(self.s, {'jsonrpc': '2.0', 'id': 3, 'method': 'other'})['error']['code'], -32601)
    def test_real_http_strict_story_json_and_mcp_share_service(self):
        app = SimpleNamespace(drama=self.s, authorized=lambda _: True,
                              config=SimpleNamespace(lan_no_auth=True, allowed_origins=('http://localhost',)))
        server = GatewayHTTPServer(('127.0.0.1', 0), app)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}/api/v1/drama'
            def request(path, raw):
                req = Request(base+path, data=raw.encode(), headers={'Content-Type': 'application/json'})
                try:
                    with urlopen(req, timeout=5) as response: return response.status, json.load(response)
                except HTTPError as exc: return exc.code, json.load(exc)
            sourcepath = f'/episodes/{self.eid}/source-revisions'
            bad = '{"title":"x","text":"original","text":"replace","expectedRevision":0,"requestKey":"dup"}'
            status, body = request(sourcepath, bad)
            self.assertEqual(status, 422); self.assertEqual(body['error']['code'], 'invalid_json')
            self.assertEqual(self.s.story_state(self.eid)['heads']['source']['revision'], 0)
            good = {'title': '来源', 'text': '林舟😀\r\n到码头', 'expectedRevision': 0, 'requestKey': 'http'}
            status, body = request(sourcepath, json.dumps(good))
            self.assertEqual(status, 201); self.assertEqual(body['revision']['content']['text'], good['text'])
            status, body = request('/mcp', json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}))
            self.assertEqual(status, 200); self.assertEqual(len(body['result']['tools']), 39)
        finally:
            server.shutdown(); server.server_close(); thread.join(5)

if __name__ == '__main__': unittest.main()


class ProjectLifecycleMCPTests(unittest.TestCase):
    """drama_create_project / drama_create_episode: the two envelope-free tools.

    They are the only business tools that do not take {projectId, arguments},
    because they are what later project-scoped calls point at.
    """
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.s = DramaService(root, root); self.addCleanup(self.s.close)

    def test_create_project_needs_no_project_id_and_is_idempotent(self):
        result = call(self.s, 'drama_create_project', {'arguments': {
            'title': '古风舞姬', 'width': 1080, 'height': 1920, 'fps': 24,
            'kind': 'drama', 'requestKey': 'guofeng-p1'}})
        pid = result['project']['id']
        self.assertEqual((result['project']['title'], result['project']['width'], result['project']['height']),
                         ('古风舞姬', 1080, 1920))
        # Same requestKey must not create a second project.
        again = call(self.s, 'drama_create_project', {'arguments': {
            'title': '古风舞姬', 'width': 1080, 'height': 1920, 'fps': 24,
            'kind': 'drama', 'requestKey': 'guofeng-p1'}})
        self.assertEqual(again['project']['id'], pid)
        self.assertEqual(len(self.s.projects()['projects']), 1)

    def test_create_episode_under_created_project(self):
        pid = call(self.s, 'drama_create_project', {'arguments': {
            'title': '古风舞姬', 'requestKey': 'guofeng-p2'}})['project']['id']
        ep = call(self.s, 'drama_create_episode', {
            'projectId': pid, 'arguments': {'title': '第1集', 'requestKey': 'guofeng-e1'}})
        self.assertEqual(ep['episode']['title'], '第1集')
        # The new project must be readable through the normal scoped read tool.
        opened = call(self.s, 'drama_get_project', {'projectId': pid, 'arguments': {}})
        self.assertEqual([e['title'] for e in opened['episodes']], ['第1集'])

    def test_create_episode_rejects_unknown_project(self):
        with self.assertRaises(DramaError) as ctx:
            call(self.s, 'drama_create_episode', {
                'projectId': 'a' * 32, 'arguments': {'title': 'x', 'requestKey': 'nope'}})
        self.assertIn(ctx.exception.code, ('not_found', 'invalid_identifier'))

    def test_advertised_schema_has_no_project_id_for_create_project(self):
        tools = {t['name']: t for t in tools_list()}
        project_schema = tools['drama_create_project']['inputSchema']
        self.assertNotIn('projectId', project_schema['properties'])
        self.assertEqual(project_schema['required'], ['arguments'])
        episode_schema = tools['drama_create_episode']['inputSchema']
        self.assertIn('projectId', episode_schema['properties'])
        self.assertFalse(tools['drama_create_project']['annotations']['readOnlyHint'])


class ShotLifecycleMCPTests(unittest.TestCase):
    """drama_create_shot / drama_revise_shot: draft adapters over the UI contracts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.s = DramaService(root, root); self.addCleanup(self.s.close)
        self.pid = self.s.create_project({'title': 'Shot MCP', 'requestKey': 'p'})['project']['id']
        self.eid = self.s.create_episode(self.pid, {'title': 'e', 'requestKey': 'e'})['episode']['id']

    def call(self, name, body, pid=None):
        return call(self.s, name, {'projectId': pid or self.pid, 'arguments': body})

    def test_create_shot_is_idempotent_and_does_not_approve(self):
        first = self.call('drama_create_shot', {'episodeId': self.eid, 'title': 'S01 · 对视', 'requestKey': 'shot-1'})
        sid = first['shot']['id']
        self.assertEqual(first['shot']['title'], 'S01 · 对视')
        again = self.call('drama_create_shot', {'episodeId': self.eid, 'title': 'S01 · 对视', 'requestKey': 'shot-1'})
        self.assertEqual(again['shot']['id'], sid)
        production = self.s.shot_production(sid)
        self.assertFalse(production['current']['approved'])
        self.assertEqual(len(self.s.episode(self.eid)['shots']), 1)

    def test_revise_shot_saves_draft_without_approval_or_generation(self):
        sid = self.call('drama_create_shot', {'episodeId': self.eid, 'title': '空白', 'requestKey': 'blank'})['shot']['id']
        current = self.s.shot_production(sid)
        snapshot = {**current['current']['snapshot'], 'title': 'S02 · 抚摸',
                    'prompt': '明亮教室，银辉短发礼服同一张脸。',
                    'action': '0-4秒对视；4-10秒被抚摸时眼神放荡；10-15秒喘息。',
                    'camera': '缓慢前推近到半身，一条连续运镜，不切换。',
                    'framing': 'vertical 9:16 medium close-up',
                    'targetEditFrames': 360,
                    'generation': {'contractVersion': 'h3-ref2va-v1', 'profile': 'heretic-ref2va-hmnsfw-v1',
                                   'requestDurationSeconds': 15}}
        result = self.call('drama_revise_shot', {
            'shotId': sid, 'expectedRevision': current['revision'], 'baseRevisionId': current['currentRevisionId'],
            'snapshot': snapshot, 'requestKey': 'revise-1'})
        self.assertEqual(result['current']['snapshot']['title'], 'S02 · 抚摸')
        self.assertEqual(result['current']['snapshot']['generation']['profile'], 'heretic-ref2va-hmnsfw-v1')
        self.assertEqual(result['current']['snapshot']['targetEditFrames'], 360)
        self.assertFalse(result['current']['approved'])
        opened = self.call('drama_get_shot', {'shotId': sid})
        self.assertEqual(opened['currentRevisionId'], result['currentRevisionId'])
        self.assertEqual(opened['current']['snapshot']['action'][:6], '0-4秒对视')
        self.assertEqual(self.s.video_jobs(self.pid)['jobs'], [])

    def test_revise_shot_rejects_foreign_project_before_dispatch(self):
        sid = self.call('drama_create_shot', {'episodeId': self.eid, 'title': '本项目', 'requestKey': 'local'})['shot']['id']
        other = self.s.create_project({'title': 'Other', 'requestKey': 'other'})['project']['id']
        current = self.s.shot_production(sid)
        with patch.object(self.s, 'revise_shot') as revise:
            with self.assertRaisesRegex(DramaError, '不属于'):
                self.call('drama_revise_shot', {
                    'shotId': sid, 'expectedRevision': current['revision'],
                    'baseRevisionId': current['currentRevisionId'],
                    'snapshot': {'title': '偷改'}, 'requestKey': 'steal'}, pid=other)
            revise.assert_not_called()

    def test_reference_node_snapshot_round_trip_keeps_order_roles_and_null_slots(self):
        from PIL import Image
        path = Path(self.tmp.name) / 'reference.png'
        Image.new('RGB', (32, 24), 'wheat').save(path)
        aid = self.call('drama_import_image', {'path': str(path), 'requestKey': 'node-image'})['asset']['id']
        sid = self.call('drama_create_shot', {'episodeId': self.eid, 'title': '光影', 'requestKey': 'nodes'})['shot']['id']
        current = self.call('drama_get_shot', {'shotId': sid})
        snapshot = {**current['current']['snapshot'], 'prompt': 'light ' * 1400,
                    'referenceNodes': [
                        {'id': 'face', 'media': 'image', 'assetId': aid, 'role': 'character', 'description': '身份'},
                        {'id': 'light', 'media': 'image', 'assetId': aid, 'role': 'custom', 'customRoleId': 'lighting', 'description': '光影'}],
                    'customReferenceRoles': [{'id': 'lighting', 'label': '光影', 'token': 'Lighting'}]}
        schema = next(t for t in tools_list() if t['name'] == 'drama_revise_shot')['inputSchema']['properties']['arguments']['properties']['snapshot']['properties']
        self.assertLessEqual(len(snapshot['prompt']), schema['prompt']['maxLength'])
        self.assertTrue(set(snapshot) <= set(schema))
        for field, value in snapshot.items():
            if value is None:
                self.assertIn('null', schema[field]['type'], field)
        body = {'shotId': sid, 'expectedRevision': current['revision'], 'baseRevisionId': current['currentRevisionId'],
                'snapshot': snapshot, 'requestKey': 'nodes-revise'}
        saved = self.call('drama_revise_shot', body)
        self.assertEqual(saved, self.call('drama_revise_shot', body))
        opened = self.call('drama_get_shot', {'shotId': sid})['current']
        for field in ('referenceNodes', 'customReferenceRoles', 'prompt', 'locationRevisionId', 'firstFrameAssetId'):
            self.assertEqual(opened['snapshot'][field], snapshot[field])
        self.assertFalse(opened['approved'])
        self.assertEqual(self.s.video_jobs(self.pid)['jobs'], [])

    def test_revise_shot_rejects_unapproved_entity_bind(self):
        entity = self.s.create_entity(self.pid, {'kind': 'character', 'name': '未批', 'requestKey': 'hero'})['entity']
        sid = self.call('drama_create_shot', {'episodeId': self.eid, 'title': '绑人', 'requestKey': 'bind'})['shot']['id']
        current = self.s.shot_production(sid)
        with self.assertRaises(DramaError) as ctx:
            self.call('drama_revise_shot', {
                'shotId': sid, 'expectedRevision': current['revision'],
                'baseRevisionId': current['currentRevisionId'],
                'snapshot': {**current['current']['snapshot'],
                             'characterRevisionIds': [entity['currentRevisionId']]},
                'requestKey': 'bind-unapproved'})
        self.assertEqual(ctx.exception.code, 'dependency_not_approved')
        self.assertFalse(self.s.shot_production(sid)['current']['approved'])

    def test_advertised_schema_is_project_scoped_and_writable(self):
        tools = {t['name']: t for t in tools_list()}
        create = tools['drama_create_shot']
        revise = tools['drama_revise_shot']
        self.assertIn('projectId', create['inputSchema']['properties'])
        self.assertEqual(set(create['inputSchema']['properties']['arguments']['required']),
                         {'episodeId', 'title', 'requestKey'})
        self.assertEqual(set(revise['inputSchema']['properties']['arguments']['required']),
                         {'shotId', 'expectedRevision', 'baseRevisionId', 'snapshot', 'requestKey'})
        self.assertFalse(create['annotations']['readOnlyHint'])
        self.assertFalse(revise['annotations']['readOnlyHint'])
        select = tools['drama_select_shot_image']
        self.assertNotIn('candidateId', select['inputSchema']['properties']['arguments']['required'])
        video = tools['drama_import_video']
        self.assertEqual(video['inputSchema']['properties']['arguments']['required'], ['requestKey'])
        with self.assertRaises(DramaError) as escaped:
            self.call('drama_import_video', {'path': '/etc/passwd', 'requestKey': 'escape'})
        self.assertEqual(escaped.exception.code, 'path_not_allowed')
        with self.assertRaises(DramaError) as missing:
            self.call('drama_import_video', {'requestKey': 'none'})
        self.assertEqual(missing.exception.code, 'invalid_fields')


class EntityImportMCPTests(unittest.TestCase):
    """drama_import_image / drama_create_entity wrap the workbench contracts and do not approve."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.s = DramaService(root, root); self.addCleanup(self.s.close)
        self.pid = self.s.create_project({'title': 'Import', 'requestKey': 'p'})['project']['id']

    def call(self, name, body, pid=None):
        return call(self.s, name, {'projectId': pid or self.pid, 'arguments': body})

    def test_import_image_rejects_escape_and_missing_source(self):
        with self.assertRaises(DramaError) as escaped:
            self.call('drama_import_image', {'path': '/etc/passwd', 'requestKey': 'escape'})
        self.assertEqual(escaped.exception.code, 'path_not_allowed')
        with self.assertRaises(DramaError) as missing:
            self.call('drama_import_image', {'requestKey': 'none'})
        self.assertEqual(missing.exception.code, 'invalid_fields')

    def test_import_image_and_create_entity_keep_draft(self):
        from PIL import Image
        import io
        path = Path(self.tmp.name) / 'face.png'
        Image.new('RGB', (32, 24), 'wheat').save(path, format='PNG')
        imported = self.call('drama_import_image', {'path': str(path), 'filename': '舞姬正面.png', 'requestKey': 'img-1'})
        asset_id = imported['asset']['id']
        self.assertEqual(imported['asset']['kind'], 'image')
        again = self.call('drama_import_image', {'path': str(path), 'filename': '舞姬正面.png', 'requestKey': 'img-1'})
        self.assertEqual(again['asset']['id'], asset_id)
        created = self.call('drama_create_entity', {
            'kind': 'character', 'name': '古风舞姬02', 'description': '红金云上舞姬。',
            'references': [{'assetId': asset_id, 'role': 'identity_front'}], 'requestKey': 'entity-1'})
        entity = created['entity']
        self.assertEqual(entity['kind'], 'character')
        self.assertEqual(entity['current']['name'], '古风舞姬02')
        self.assertFalse(entity['current']['approved'])
        self.assertIsNone(entity['approvedRevisionId'])
        self.assertEqual(entity['current']['references'], [{'assetId': asset_id, 'role': 'identity_front'}])
        repeated = self.call('drama_create_entity', {
            'kind': 'character', 'name': '古风舞姬02', 'description': '红金云上舞姬。',
            'references': [{'assetId': asset_id, 'role': 'identity_front'}], 'requestKey': 'entity-1'})
        self.assertEqual(repeated['entity']['id'], entity['id'])
        self.assertEqual(len(self.s.entities(self.pid)['entities']), 1)

    def test_entity_description_revision_can_preserve_all_reference_slots(self):
        from PIL import Image
        refs = []
        for index, role in enumerate(('identity_front', 'identity_3q')):
            path = Path(self.tmp.name) / (role + '.png')
            Image.new('RGB', (32, 24), ('wheat', 'red')[index]).save(path)
            aid = self.call('drama_import_image', {'path': str(path), 'requestKey': role})['asset']['id']
            refs.append({'assetId': aid, 'role': role})
        original = self.call('drama_create_entity', {'kind': 'character', 'name': '舞姬',
            'references': refs, 'requestKey': 'hero-refs'})['entity']
        body = {'entityId': original['id'], 'expectedRevision': original['revision'], 'name': '舞姬',
                'description': '身份不变，统一右后方夕阳。', 'references': refs,
                'referenceAssetId': refs[0]['assetId'], 'referenceRole': 'identity_front', 'requestKey': 'relight'}
        result = self.call('drama_propose_entity_revision', body)
        self.assertEqual(result, self.call('drama_propose_entity_revision', body))
        entity = result['entity']
        self.assertEqual(entity['current']['references'], refs)
        self.assertEqual(entity['current']['description'], body['description'])
        self.assertIsNone(entity['approvedRevisionId'])
        self.assertFalse(entity['current']['approved'])
        self.assertEqual(entity['revisions'][1]['references'], refs)
        with self.assertRaises(DramaError):
            self.call('drama_propose_entity_revision', {**body, 'expectedRevision': entity['revision'],
                'references': [{'assetId': 'f' * 64, 'role': 'identity_front'}],
                'referenceAssetId': 'f' * 64, 'requestKey': 'unbound'})


class ShotDeletionMCPTests(unittest.TestCase):
    """drama_delete_shot shares the workbench method and keeps the generation audit."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.s = DramaService(root, root); self.addCleanup(self.s.close)
        self.s._video_stop.set()
        self.pid = self.s.create_project({'title': '删除合同', 'requestKey': 'p'})['project']['id']
        self.eid = self.s.create_episode(self.pid, {'title': 'e', 'requestKey': 'e'})['episode']['id']

    def call(self, name, body, pid=None):
        return call(self.s, name, {'projectId': pid or self.pid, 'arguments': body})

    def test_delete_is_one_method_and_audit_survives_projection(self):
        from DGXGateway.dgx_gateway.drama.backups import projection, snapshot
        from DGXGateway.dgx_gateway.drama.store import encode, new_id
        tools = {tool['name']: tool for tool in tools_list()}
        self.assertIn('drama_delete_shot', tools)
        self.assertIn('drama_list_shot_audit', tools)
        self.assertFalse(tools['drama_delete_shot']['annotations']['readOnlyHint'])
        self.assertTrue(tools['drama_list_shot_audit']['annotations']['readOnlyHint'])
        self.assertEqual(set(tools['drama_delete_shot']['inputSchema']['properties']['arguments']['required']),
                         {'shotId', 'expectedRevision', 'requestKey'})
        prompt = '审计提示词。' + ('袖' * 240)
        sid = self.call('drama_create_shot', {'episodeId': self.eid, 'title': '要删', 'requestKey': 'gone'})['shot']['id']
        current = self.s.shot_production(sid)
        self.call('drama_revise_shot', {
            'shotId': sid, 'expectedRevision': current['revision'], 'baseRevisionId': current['currentRevisionId'],
            'snapshot': {**current['current']['snapshot'], 'prompt': prompt}, 'requestKey': 'prompt'})
        kept = self.call('drama_create_shot', {'episodeId': self.eid, 'title': '保留', 'requestKey': 'kept'})['shot']['id']
        other = self.s.create_project({'title': '别的项目', 'requestKey': 'other'})['project']['id']
        other_eid = self.s.create_episode(other, {'title': '别的集', 'requestKey': 'other-e'})['episode']['id']
        other_sid = self.s.create_shot(other_eid, {'title': '别人的镜头', 'requestKey': 'other-shot'})['shot']['id']
        shot = self.s.shot_production(sid)
        jid, iid, now = new_id(), new_id(), 1_700_000_000.0
        batch = {'job': {'id': jid, 'episodeId': self.eid, 'state': 'SUCCEEDED', 'items': [
            {'id': iid, 'shotId': sid, 'shotRevisionId': shot['currentRevisionId']}]}}
        with self.s.store.connect(write=True) as db:
            db.execute("INSERT INTO dr_video_jobs(id,project_id,episode_id,dispatch_key,state,memory_released,resource_settled,resource_status,cleanup_required,reserved_shots,created_at,updated_at) VALUES(?,?,?,?,'SUCCEEDED',1,1,'released',0,1,?,?)",
                       (jid, self.pid, self.eid, 'drama-video-' + jid, now, now))
            db.execute("INSERT INTO dr_video_items(id,job_id,ordinal,shot_id,shot_revision_id,input_snapshot,state,submission_phase) VALUES(?,?,0,?,?,?,'SUCCEEDED','RECEIVED')",
                       (iid, jid, sid, shot['currentRevisionId'], encode({
                           'shotId': sid, 'shotRevisionId': shot['currentRevisionId'], 'prompt': prompt,
                           'imageAssetId': '', 'audioAssetId': '', 'seed': 7})))
            db.execute("INSERT INTO dr_requests VALUES (?,?,?,?,?)",
                       (f'video-batches:{self.eid}', 'batch-1', 'h', encode(batch), now))
            db.execute("INSERT INTO dr_events VALUES (?,?,?,?,?)",
                       (new_id(), f'video-batches:{self.eid}', 'mutation', encode({'requestKey': 'batch-1', 'actor': 'ui_api'}), now))
            db.execute("INSERT INTO dr_requests VALUES (?,?,?,?,?)",
                       (f'reference-submit:{other}', 'foreign-ref', 'h', encode({'jobId': 'ab' * 32}), now))
        body = {'shotId': sid, 'expectedRevision': shot['revision'], 'requestKey': 'delete-1'}
        first = self.call('drama_delete_shot', body)
        again = self.s.delete_shot(sid, {'expectedRevision': shot['revision'], 'requestKey': 'delete-1'})
        self.assertEqual(first, again)
        self.assertTrue(first['deleted'])
        self.assertEqual([item['id'] for item in self.s.episode(self.eid)['shots']], [kept])
        self.assertIsNone(self.s.episode(self.eid)['timeline'])
        self.assertEqual(self.s.episode(other_eid)['shots'][0]['id'], other_sid)
        audit = self.call('drama_list_shot_audit', {})
        kinds = [event['kind'] for event in audit['events']]
        self.assertEqual(kinds, ['video_generation', 'shot_deleted'])
        self.assertEqual(audit['events'][0]['payload']['prompt'], prompt)
        self.assertEqual(audit['events'][0]['payload']['actor'], 'ui_api')
        self.assertEqual(audit['events'][1]['payload']['prompt'], prompt)
        self.assertEqual(audit['events'][1]['payload']['actor'], 'business_mcp')
        self.assertNotIn('shotId', audit['events'][0]['payload'])
        with self.s.store.connect() as db:
            foreign = db.execute("SELECT response FROM dr_requests WHERE scope=? AND request_key=?",
                                 (f'reference-submit:{other}', 'foreign-ref')).fetchone()
            self.assertEqual(json.loads(foreign[0])['jobId'], 'ab' * 32)
            snap = snapshot(self.s.store)
        try:
            rows = projection(snap, self.pid)
        finally:
            snap.close()
        self.assertNotIn(sid, {row['id'] for row in rows['dr_shots']})
        self.assertIn('video_generation', {row['kind'] for row in rows['dr_events']})
        self.assertIn('shot_deleted', {row['kind'] for row in rows['dr_events']})

    def test_unknown_submission_and_foreign_project_are_refused(self):
        from DGXGateway.dgx_gateway.drama.store import encode, new_id
        sid = self.call('drama_create_shot', {'episodeId': self.eid, 'title': '回执不明', 'requestKey': 'busy'})['shot']['id']
        shot = self.s.shot_production(sid)
        jid, now = new_id(), 1_700_000_001.0
        with self.s.store.connect(write=True) as db:
            db.execute("INSERT INTO dr_video_jobs(id,project_id,episode_id,dispatch_key,state,memory_released,resource_settled,resource_status,reserved_shots,created_at,updated_at) VALUES(?,?,?,?,'CANCELED',0,0,'unsettled',1,?,?)",
                       (jid, self.pid, self.eid, 'drama-video-' + jid, now, now))
            db.execute("INSERT INTO dr_video_items(id,job_id,ordinal,shot_id,shot_revision_id,input_snapshot,state,submission_phase) VALUES(?,?,0,?,?,?,'SUBMISSION_UNKNOWN','UNKNOWN')",
                       (new_id(), jid, sid, shot['currentRevisionId'], encode({'shotId': sid, 'prompt': '不能丢'})))
        with self.assertRaises(DramaError) as busy:
            self.call('drama_delete_shot', {'shotId': sid, 'expectedRevision': shot['revision'], 'requestKey': 'busy-delete'})
        self.assertEqual(busy.exception.code, 'shot_busy')
        self.assertEqual(self.s.episode(self.eid)['shots'][0]['id'], sid)
        self.assertEqual(self.call('drama_list_shot_audit', {})['events'], [])
        other = self.s.create_project({'title': '别的', 'requestKey': 'other'})['project']['id']
        with patch.object(self.s, 'delete_shot') as delete:
            with self.assertRaisesRegex(DramaError, '不属于'):
                self.call('drama_delete_shot', {'shotId': sid, 'expectedRevision': shot['revision'], 'requestKey': 'steal'}, pid=other)
            delete.assert_not_called()
