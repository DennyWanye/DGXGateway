"""Project reorganization preserves live job identity and archive ownership."""
import io
import json
from pathlib import Path
import tempfile
import time
import unittest

from PIL import Image

from DGXGateway.dgx_gateway.drama.service import DramaService
from DGXGateway.dgx_gateway.drama.business_mcp import call, tools_list
from DGXGateway.dgx_gateway.drama.backups import projection
from DGXGateway.dgx_gateway.drama.contracts import DramaError
from DGXGateway.dgx_gateway.drama.store import new_id, encode


class ProjectMergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.s = DramaService(root, root)
        self.addCleanup(self.s.close)
        self.src = self.s.create_project({'title': 'source', 'requestKey': 'source'})['project']['id']
        self.dst = self.s.create_project({'title': 'practice', 'requestKey': 'target'})['project']['id']
        self.ep = self.s.create_episode(self.src, {'title': 'lunch', 'requestKey': 'episode-source'})['episode']['id']
        buf = io.BytesIO(); Image.new('RGB', (64, 64)).save(buf, 'PNG'); data = buf.getvalue()
        self.asset = self.s.upload_image(self.src, io.BytesIO(data), len(data), 'face.png', 'image')['asset']['id']
        self.entity = self.s.create_entity(self.src, {'kind': 'character', 'name': 'hero',
            'referenceAssetId': self.asset, 'requestKey': 'entity-source'})['entity']
        self.s.approve_entity(self.entity['id'], {'revisionId': self.entity['currentRevisionId'], 'expectedRevision': 1, 'requestKey': 'approve'})
        self.shot = self.s.create_shot(self.ep, {'title': 'shot', 'requestKey': 'shot'})['shot']
        self.body = {'sourceProjectId': self.src, 'sourceExpectedRevision': 1, 'expectedRevision': 1,
                     'episodeIds': [self.ep], 'requestKey': 'merge'}

    def merge(self, body=None):
        return call(self.s, 'drama_merge_project', {'projectId': self.dst, 'arguments': body or self.body})

    def test_mcp_schema_idempotency_and_readback_preserve_ids(self):
        schema = next(t for t in tools_list() if t['name'] == 'drama_merge_project')
        self.assertIn('sourceProjectId', schema['inputSchema']['properties']['arguments']['required'])
        before = self.s.shot_production(self.shot['id'])
        result = self.merge()
        self.assertEqual(result, self.merge())
        self.assertEqual(self.s.shot_production(self.shot['id']), before)
        self.assertEqual([e['id'] for e in self.s.project(self.dst)['episodes']], [self.ep])
        self.assertEqual(self.s.project(self.src)['episodes'], [])
        self.assertEqual(self.s.entities(self.dst)['entities'][0]['id'], self.entity['id'])
        self.assertEqual(self.s.entities(self.dst)['entities'][0]['approvedRevisionId'], self.entity['currentRevisionId'])
        self.assertEqual(self.s.media_path(self.src, self.asset), self.s.media_path(self.dst, self.asset))
        with self.s.store.connect() as db:
            for pid in (self.src, self.dst):
                projection(db, pid)

    def test_running_video_ownership_moves_without_changing_frozen_request(self):
        jid, iid, gateway = new_id(), new_id(), new_id()
        frozen = encode({'shotId': self.shot['id'], 'shotRevisionId': self.shot['currentRevisionId'],
                         'prompt': 'unchanged prompt '+self.src, 'seed': 42})
        with self.s.store.connect(write=True) as db:
            db.execute("INSERT INTO dr_video_jobs(id,project_id,episode_id,dispatch_key,gateway_job_id,state,reserved_shots,created_at,updated_at) VALUES (?,?,?,?,?,'RUNNING',1,?,?)",
                       (jid,self.src,self.ep,'original-dispatch',gateway,time.time(),time.time()))
            db.execute("INSERT INTO dr_video_items(id,job_id,ordinal,shot_id,shot_revision_id,input_snapshot,state,submission_phase) VALUES (?,?,0,?,?,?,'RUNNING','RECEIVED')",
                       (iid,jid,self.shot['id'],self.shot['currentRevisionId'],frozen))
        self.merge()
        with self.s.store.connect() as db:
            job = dict(db.execute('SELECT * FROM dr_video_jobs WHERE id=?',(jid,)).fetchone())
            item = dict(db.execute('SELECT * FROM dr_video_items WHERE id=?',(iid,)).fetchone())
            self.assertEqual((job['project_id'],job['gateway_job_id'],job['dispatch_key'],job['state']),
                             (self.dst,gateway,'original-dispatch','RUNNING'))
            self.assertEqual(item['input_snapshot'], frozen)
            projection(db, self.dst)

    def test_stale_or_incomplete_request_rolls_back(self):
        for body in ({**self.body,'expectedRevision':2}, {**self.body,'episodeIds':[new_id()]},
                     {**self.body,'sourceProjectId':self.dst}):
            with self.assertRaises(DramaError): self.merge(body)
        self.assertEqual(self.s.project(self.src)['episodes'][0]['id'],self.ep)
        self.assertEqual(self.s.project(self.dst)['episodes'],[])

    def test_collection_request_collision_does_not_partially_move(self):
        self.s.create_episode(self.dst, {'title':'existing','requestKey':'episode-source'})
        with self.assertRaises(DramaError) as e: self.merge()
        self.assertEqual(e.exception.code,'merge_key_conflict')
        self.assertEqual(self.s.entities(self.src)['entities'][0]['id'],self.entity['id'])

    def test_pause_and_canvas_mismatch_rejected(self):
        with self.s.store.connect(write=True) as db:
            db.execute('UPDATE dr_projects SET width=1080 WHERE id=?',(self.dst,))
        with self.assertRaises(DramaError) as e: self.merge()
        self.assertEqual(e.exception.code,'project_format_mismatch')
        with self.s.store.connect(write=True) as db:
            db.execute('UPDATE dr_projects SET width=1344 WHERE id=?',(self.dst,))
            db.execute('INSERT INTO dr_production_state VALUES (?,1,1)',(self.src,))
        with self.assertRaises(DramaError) as e: self.merge()
        self.assertEqual(e.exception.code,'pause_mismatch')


if __name__ == '__main__':
    unittest.main()
