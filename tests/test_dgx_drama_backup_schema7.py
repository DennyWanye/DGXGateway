"""Schema6 import compatibility and schema7 resource responsibility boundaries."""
import copy
import json
import sqlite3
import time
import unittest
from unittest.mock import Mock
import zipfile

from tests import test_dgx_drama_backups as fixtures
from DGXGateway.dgx_gateway.drama.backups import SCHEMA6_HASH, encode, sha, projection, snapshot, validate_rows, recovery_obligations
from DGXGateway.dgx_gateway.drama.contracts import DramaError
from DGXGateway.dgx_gateway.drama.restore import restore_archive, inspect_archive
from DGXGateway.dgx_gateway.drama.service import DramaService


class BackupSchema7Tests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.BackupTests(); self.f.setUp(); self.jid='7'*32
        with self.f.s.store.connect(write=True) as db:
            db.execute("INSERT INTO dr_video_jobs(id,project_id,episode_id,dispatch_key,gateway_job_id,state,memory_released,resource_settled,resource_status,reserved_shots,created_at,updated_at) VALUES(?,?,?,?,?,'FAILED',1,1,'released',0,?,?)",
                (self.jid,self.f.pid,self.f.eid,'old-key','8'*32,time.time(),time.time()))
        snap=snapshot(self.f.s.store)
        try:self.rows=projection(snap,self.f.pid)
        finally:snap.close()

    def tearDown(self):self.f.tearDown()

    def legacy_archive(self, *, injected=False, bad_hash=False):
        self.f.pack()
        with zipfile.ZipFile(self.f.package) as archive:members={n:archive.read(n) for n in archive.namelist()}
        manifest=json.loads(members['manifest.json']);manifest['schemaVersion']=6
        manifest['schemaFingerprint']='0'*64 if bad_hash else SCHEMA6_HASH
        members.pop('rows/dr_reference_candidate_reviews.jsonl')
        manifest['tables'].pop('dr_reference_candidate_reviews')
        manifest['files']=[item for item in manifest['files'] if item['path']!='rows/dr_reference_candidate_reviews.jsonl']
        jobs=[json.loads(line) for line in members['rows/dr_video_jobs.jsonl'].splitlines()]
        for job in jobs:
            job.pop('resource_settled');job.pop('resource_status')
            if injected:job['resource_settled']=1
        members['rows/dr_video_jobs.jsonl']=b''.join((encode(job)+'\n').encode() for job in jobs)
        projects=[json.loads(line) for line in members['rows/dr_projects.jsonl'].splitlines()]
        for row in projects:
            row.pop('kind', None)
        members['rows/dr_projects.jsonl']=b''.join((encode(row)+'\n').encode() for row in projects)
        episodes=[json.loads(line) for line in members['rows/dr_episodes.jsonl'].splitlines()]
        for row in episodes:
            row.pop('song_asset_id', None)
        members['rows/dr_episodes.jsonl']=b''.join((encode(row)+'\n').encode() for row in episodes)
        for item in manifest['files']:
            data=members[item['path']];item.update(bytes=len(data),sha256=sha(data))
        manifest['totalBytes']=sum(item['bytes'] for item in manifest['files'])
        members['manifest.json']=encode(manifest).encode()
        path=self.f.root/'legacy.zip'
        with zipfile.ZipFile(path,'w') as archive:
            for name,data in members.items():archive.writestr(name,data)
        return path

    def test_legacy6_import_rebuilds8_preserves_identity_and_sets_safe_defaults(self):
        package=self.legacy_archive();target=self.f.root/'legacy-restored';restore_archive(package,target)
        with sqlite3.connect(target/'drama/drama.sqlite3') as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],9)
            self.assertEqual(db.execute('SELECT id,state,memory_released,resource_settled,resource_status FROM dr_video_jobs').fetchone(),
                (self.jid,'FAILED',1,1,'released'))
        provenance=json.loads((target/'drama/restore-provenance.json').read_text())
        self.assertEqual(provenance['sourceSchemaVersion'],6)
        self.assertEqual(provenance['sourceSchemaFingerprint'],SCHEMA6_HASH)

    def test_legacy6_cannot_smuggle_new_resource_columns_or_unknown_fingerprint(self):
        package=self.legacy_archive(injected=True)
        with self.assertRaisesRegex(DramaError,'schema6归档列'):restore_archive(package,self.f.root/'bad')
        self.assertFalse((self.f.root/'bad').exists())
        package.unlink();self.f.package.unlink()
        with self.assertRaisesRegex(DramaError,'版本'):inspect_archive(self.legacy_archive(bad_hash=True))

    def test_unsettled_legacy_resources_remain_recovery_required(self):
        with self.f.s.store.connect(write=True) as db:
            db.execute("UPDATE dr_video_jobs SET memory_released=0,resource_settled=0,resource_status='unsettled' WHERE id=?",(self.jid,))
        package=self.legacy_archive();target=self.f.root/'unsettled';result=restore_archive(package,target)
        self.assertEqual([r['id'] for r in result['recovery']['tasks']],[self.jid])
        with sqlite3.connect(target/'drama/drama.sqlite3') as db:
            self.assertEqual(db.execute('SELECT resource_settled,resource_status FROM dr_video_jobs').fetchone(),(0,'unsettled'))

    def test_resident_history_does_not_create_recovery_release_or_model_calls(self):
        with self.f.s.store.connect(write=True) as db:
            db.execute("UPDATE dr_video_jobs SET memory_released=0,resource_status='resident' WHERE id=?",(self.jid,))
        self.f.pack();target=self.f.root/'resident';result=restore_archive(self.f.package,target)
        self.assertEqual(result['recovery']['tasks'],[])
        adapter=Mock(); child=DramaService(target,self.f.root,video_adapter=adapter)
        try:
            with child.store.connect() as db:
                row=db.execute('SELECT resource_settled,resource_status,memory_released FROM dr_video_jobs').fetchone()
                self.assertEqual(tuple(row),(1,'resident',0))
            for action in (lambda:child.cancel_video_job(self.jid),lambda:child.refresh_video_job(self.jid),lambda:child.submit_video_batch(self.f.eid,{})):
                with self.assertRaisesRegex(DramaError,'隔离'):action()
            self.assertFalse(adapter.mock_calls)
        finally:child.close()

    def test_inconsistent_settlement_cannot_hide_cleanup_or_active_task(self):
        for changes in ({'cleanup_required':1},{'state':'RUNNING'},{'resource_status':'resident'},
                        {'resource_status':'unsettled'},{'resource_settled':2},{'resource_status':'arbitrary'}):
            rows=copy.deepcopy(self.rows);rows['dr_video_jobs'][0].update(changes)
            with self.subTest(changes=changes),self.assertRaises(DramaError):validate_rows(rows,self.f.pid)
        for changes in ({'cleanup_required':1},{'state':'RUNNING'}):
            rows=copy.deepcopy(self.rows);rows['dr_video_jobs'][0].update(memory_released=0,resource_status='resident',**changes)
            self.assertTrue(recovery_obligations(rows))
