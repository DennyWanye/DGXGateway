"""Frozen historical review event: preserve evidence, reject foreign ownership."""
import copy
import io
import json
import sqlite3
import subprocess
import time
import unittest
import zipfile

from tests import test_dgx_drama_backups as fixtures
from DGXGateway.dgx_gateway.drama.backups import encode, pack_project, projection, snapshot, validate_rows
from DGXGateway.dgx_gateway.drama.contracts import DramaError
from DGXGateway.dgx_gateway.drama.restore import restore_archive


class HistoricalReviewFixtureTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.BackupTests(); self.f.setUp()
        video = self.f.root / 'fixture.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=blue:size=96x64:rate=24',
                        '-t', '8', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(video)], check=True)
        data = video.read_bytes()
        self.aid = self.f.s.upload(self.f.pid, io.BytesIO(data), len(data), 'fixture.mp4', 'v')['asset']['id']
        shot = self.f.s.create_shot(self.f.eid, {'title': 'review fixture', 'requestKey': 's'})['shot']
        self.sid = shot['id']
        revised = self.f.s.revise_shot(self.sid, {'expectedRevision': shot['revision'],
            'baseRevisionId': shot['currentRevisionId'], 'snapshot': {'title': 'fixture v2'}, 'requestKey': 'v2'})
        revisions = [revised['currentRevisionId'], shot['currentRevisionId']]
        self.payload = dict(fixture='shadcn-review-fixture-v1', created=True, projectId=self.f.pid,
            episodeId=self.f.eid, shotId=self.sid, currentJobId='a'*32, staleJobId='b'*32,
            currentTakeId='c'*32, staleTakeId='d'*32, source='ui_review_fixture', assetId=self.aid,
            gpuGenerated=False, mediaSeconds=8, note='UI-only historical evidence')
        with self.f.s.store.connect(write=True) as db:
            for role, rid in zip(('current', 'stale'), revisions):
                jid = self.payload[role+'JobId']; tid = self.payload[role+'TakeId']
                db.execute('INSERT INTO dr_takes(id,shot_id,asset_id,source,created_at,source_revision_id) VALUES(?,?,?,?,?,?)',
                    (tid,self.sid,self.aid,'ui_review_fixture',time.time(),rid))
                db.execute("INSERT INTO dr_video_jobs(id,project_id,episode_id,dispatch_key,state,memory_released,reserved_shots,created_at,updated_at) VALUES(?,?,?,?,'SUCCEEDED',1,0,?,?)",
                    (jid,self.f.pid,self.f.eid,'fixture-no-gpu-'+jid,time.time(),time.time()))
                inputs = encode(dict(source='ui_review_fixture',fixture=self.payload['fixture'],mediaSourceAssetId=self.aid))
                db.execute("INSERT INTO dr_video_items(id,job_id,ordinal,shot_id,shot_revision_id,input_snapshot,state,submission_phase,take_id,output_hash) VALUES(?,?,0,?,?,?,'SUCCEEDED','RECEIVED',?,?)",
                    (('e' if role=='current' else 'f')*32,jid,self.sid,rid,inputs,tid,self.aid))
            db.execute('INSERT INTO dr_events VALUES(?,?,?,?,?)',
                ('1'*32,'fixture:'+self.f.pid,'ui_review_fixture',encode(self.payload),time.time()))
        snap = snapshot(self.f.s.store)
        try: self.rows = projection(snap,self.f.pid)
        finally: snap.close()

    def tearDown(self): self.f.tearDown()

    def changed(self, **kwargs):
        rows = copy.deepcopy(self.rows)
        event = next(r for r in rows['dr_events'] if r['id']=='1'*32)
        payload = json.loads(event['payload']); payload.update(kwargs); event['payload']=encode(payload)
        return rows

    def test_archive_restore_preserves_event_and_all_candidate_relations(self):
        self.f.pack(); dest=self.f.root/'restored'; restore_archive(self.f.package,dest)
        with sqlite3.connect(dest/'drama/drama.sqlite3') as db:
            self.assertEqual(json.loads(db.execute("SELECT payload FROM dr_events WHERE id=?",('1'*32,)).fetchone()[0]),self.payload)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM dr_video_items WHERE take_id IN (?,?)",('c'*32,'d'*32)).fetchone()[0],2)
        package = self.f.root/'other.zip'; pack_project(self.f.s,self.f.qid,package,'9'*32)
        with zipfile.ZipFile(package) as archive:
            self.assertNotIn(b'ui_review_fixture',archive.read('rows/dr_events.jsonl'))

    def test_reject_foreign_and_wrong_typed_references(self):
        for field in ('projectId','episodeId','shotId','assetId','currentJobId','staleJobId','currentTakeId','staleTakeId'):
            for value in (self.f.qid, [], None):
                with self.subTest(field=field,value=value), self.assertRaises(DramaError):
                    validate_rows(self.changed(**{field:value}),self.f.pid)

    def test_reject_same_project_job_take_swap_and_duplicate_candidates(self):
        for values in ({'currentTakeId':'d'*32,'staleTakeId':'c'*32}, {'currentJobId':'b'*32}, {'staleTakeId':'c'*32}):
            with self.subTest(values=values),self.assertRaises(DramaError):
                validate_rows(self.changed(**values),self.f.pid)

    def test_reject_unknown_contract_and_fake_gpu_origin(self):
        for values in ({'fixture':'future-v2'}, {'unknown':'foreign'}, {'source':'gpu'}, {'gpuGenerated':True},
                       {'created':1}, {'mediaSeconds':8.0}, {'note':{}}):
            with self.subTest(values=values),self.assertRaises(DramaError):
                validate_rows(self.changed(**values),self.f.pid)
        rows=copy.deepcopy(self.rows); rows['dr_video_jobs'][0]['gateway_job_id']='9'*32
        with self.assertRaises(DramaError):validate_rows(rows,self.f.pid)

    def test_fixture_kind_scope_pair_and_request_are_closed(self):
        for scope,kind in (('fixture:'+self.f.pid,'mutation'),('project:'+self.f.pid,'ui_review_fixture'),('fixture:'+self.f.eid,'ui_review_fixture')):
            rows=copy.deepcopy(self.rows); event=next(r for r in rows['dr_events'] if r['id']=='1'*32)
            event.update(scope=scope,kind=kind)
            with self.subTest(scope=scope,kind=kind),self.assertRaises(DramaError):validate_rows(rows,self.f.pid)
        rows=copy.deepcopy(self.rows)
        rows['dr_requests'].append(dict(scope='fixture:'+self.f.pid,request_key='fake',input_hash='0'*64,response='{}',created_at=time.time()))
        with self.assertRaises(DramaError):validate_rows(rows,self.f.pid)
