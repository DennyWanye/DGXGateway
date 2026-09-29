"""Independent adversarial P7 projection/restore review; isolated tiny SQLite only."""
import json
import time
import unittest
from tests import test_dgx_drama_backups as fixtures
from DGXGateway.dgx_gateway.drama.backups import projection, snapshot, encode
from DGXGateway.dgx_gateway.drama.contracts import DramaError

class BackupReviewTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.BackupTests()
        self.f.setUp()
    def tearDown(self):
        self.f.tearDown()

    def test_owned_text_request_cannot_carry_foreign_job_response(self):
        with self.f.s.store.connect(write=True) as db:
            db.execute('INSERT INTO dr_requests VALUES(?,?,?,?,?)',
                       ('text-jobs:'+self.f.eid,'corrupt-response','0'*64,encode({'job':{'id':self.f.qid,'raw':'Q-PRIVATE-RAW-SENTINEL'}}),time.time()))
        snap=snapshot(self.f.s.store)
        try:
            with self.assertRaises(DramaError):
                projection(snap,self.f.pid)
        finally: snap.close()

    def test_owned_request_cannot_carry_foreign_id_list(self):
        with self.f.s.store.connect(write=True) as db:
            db.execute('INSERT INTO dr_requests VALUES(?,?,?,?,?)',
                       ('shots:'+self.f.eid,'corrupt-response','0'*64,encode({'shotIds':[self.f.qid]}),time.time()))
        snap=snapshot(self.f.s.store)
        try:
            with self.assertRaises(DramaError):
                projection(snap,self.f.pid)
        finally: snap.close()

    def test_story_head_cannot_bind_other_episode_revision_inside_same_project(self):
        other = self.f.s.create_episode(self.f.pid, {'title':'other episode','requestKey':'other-e'})['episode']['id']
        source = self.f.s.save_source(other, {'title':'other source','text':'other episode only','expectedRevision':0,'requestKey':'other-source'})['revision']
        with self.f.s.store.connect(write=True) as db:
            db.execute('UPDATE dr_story_heads SET current_revision_id=? WHERE episode_id=? AND kind=?', (source['id'],self.f.eid,'source'))
        snap=snapshot(self.f.s.store)
        try:
            with self.assertRaises(DramaError):
                projection(snap,self.f.pid)
        finally: snap.close()

    def test_restored_workspace_restart_interrupts_new_cpu_job_but_preserves_original(self):
        import subprocess, io
        from unittest.mock import patch
        from DGXGateway.dgx_gateway.drama.restore import restore_archive
        from DGXGateway.dgx_gateway.drama.service import DramaService
        video=self.f.root/'tiny.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=blue:size=96x64:rate=24','-t','1','-c:v','libx264','-pix_fmt','yuv420p',str(video)],check=True)
        data=video.read_bytes()
        asset=self.f.s.upload(self.f.pid,io.BytesIO(data),len(data),'tiny.mp4','video')['asset']
        shot=self.f.s.create_shot(self.f.eid,{'title':'tiny','assetId':asset['id'],'requestKey':'shot'})['shot']
        take=shot['takes'][0]['id']
        self.f.s.select_take(shot['id'],{'takeId':take,'expectedRevision':1,'requestKey':'choose'})
        tl=self.f.s.save_timeline(self.f.eid,{'clips':[{'clipId':'a','takeId':take,'inFrame':0,'frames':24,'keepSourceAudio':False,'fit':'contain'}],'audioClips':[],'expectedRevision':0,'requestKey':'timeline'})['timeline']
        with patch.object(self.f.s._executor,'submit'):
            old=self.f.s.create_export(tl['id'],{'outputs':['proxy','report'],'requestKey':'original'})['export']
        self.f.pack(); destination=self.f.root/'restored-cpu'
        restore_archive(self.f.package,destination)
        child=DramaService(destination,self.f.root)
        try:
            with patch.object(child._executor,'submit'):
                new=child.create_export(tl['id'],{'outputs':['proxy','report'],'requestKey':'new-local-cpu'})['export']
            with child.store.connect(write=True) as db:
                db.execute("UPDATE dr_exports SET state='RUNNING' WHERE id=?",(new['id'],))
        finally: child.close()
        child=DramaService(destination,self.f.root)
        try:
            self.assertEqual(child.export(old['id'])['export']['state'],'QUEUED')
            self.assertEqual(child.export(new['id'])['export']['state'],'INTERRUPTED')
        finally: child.close()

    def test_terminal_video_unreleased_or_unarchived_is_recovery_required(self):
        from DGXGateway.dgx_gateway.drama.backups import recovery_obligations,TABLES
        for unreleased in (True,False):
            with self.subTest(unreleased=unreleased):
                rows={table:[] for table in TABLES}
                rows['dr_video_jobs']=[{'id':'f'*32,'state':'FAILED','gateway_job_id':'a'*32,'dispatch_key':'original','cleanup_required':0,'memory_released':0 if unreleased else 1,'cancel_requested':0}]
                rows['dr_video_items']=[] if unreleased else [{'job_id':'f'*32,'id':'d'*32,'state':'OUTPUT_UNAVAILABLE','submission_phase':'REQUEST_STARTED','take_id':None}]
                obligations=recovery_obligations(rows)
                self.assertEqual([r['id'] for r in obligations],['f'*32])

    def test_project_dimensions_require_valid_typed_business_values(self):
        for value in ('not-an-integer', 0):
            with self.subTest(width=value):
                with self.f.s.store.connect(write=True) as db:
                    db.execute('UPDATE dr_projects SET width=? WHERE id=?',(value,self.f.pid))
                snap=snapshot(self.f.s.store)
                try:
                    with self.assertRaises(DramaError):
                        projection(snap,self.f.pid)
                finally:snap.close()

    def test_restore_uploaded_package_must_match_original_upload_receipt_hash(self):
        import io
        from DGXGateway.dgx_gateway.drama.backups import pack_project
        self.f.pack(); data=self.f.package.read_bytes()
        upload=self.f.s.upload_restore(io.BytesIO(data),len(data),'bound-upload')['upload']
        other=self.f.root/'other-project.zip'
        pack_project(self.f.s,self.f.qid,other,'f'*32)
        (self.f.s._backup_root/(upload['id']+'.upload.zip')).write_bytes(other.read_bytes())
        with self.assertRaises(DramaError):
            self.f.s.create_restore({'uploadId':upload['id'],'requestKey':'restore-original-preview'})

    def test_sanitized_shared_asset_transform_is_declared_without_original_name(self):
        from DGXGateway.dgx_gateway.drama.backups import sanitized
        snap=snapshot(self.f.s.store)
        try: rows=projection(snap,self.f.pid)
        finally:snap.close()
        cleaned,changes=sanitized(rows)
        self.assertEqual(cleaned['dr_assets'][0]['filename'], self.f.aid+'.png')
        self.assertTrue(any(c.get('table')=='dr_assets' and c.get('field')=='filename' for c in changes))
        self.assertNotIn('Q-SECRET',encode(changes))

    def test_unknown_attempt_receipt_evidence_is_preserved_only_as_redacted_digest(self):
        import zipfile
        with self.f.s.store.connect(write=True) as db:
            now=time.time();jid='e'*32
            db.execute('INSERT INTO dr_text_jobs(id,episode_id,kind,request_key,input_hash,payload,state,output_status,resource_status,epoch,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (jid,self.f.eid,'extract_entities','historical-text','0'*64,'{}','FAILED','none','not_acquired',0,now,now))
            db.execute('INSERT INTO dr_text_receipt_evidence VALUES(?,?,?,?,?,?)',
                       ('f'*32,jid,'d'*32,encode({'output_text':'Q-EXTERNAL-SECRET-SENTINEL','job_id':'c'*32}),'unknown attempt identity',now))
        self.f.pack()
        with zipfile.ZipFile(self.f.package) as archive:
            row=json.loads(archive.read('rows/dr_text_receipt_evidence.jsonl').splitlines()[0])
            receipt=json.loads(row['receipt'])
            self.assertEqual(row['attempt_id'],'d'*32)
            self.assertTrue(receipt['redacted'])
            self.assertEqual(len(receipt['originalSha256']),64)
            self.assertNotIn('Q-EXTERNAL-SECRET-SENTINEL',archive.read('rows/dr_text_receipt_evidence.jsonl').decode())

    def test_real_production_pause_response_is_backed_up(self):
        self.f.s.set_production_state(self.f.pid,{'paused':True,'expectedRevision':1,'requestKey':'pause'})
        result=self.f.pack()
        self.assertGreater(result['manifest']['tables']['dr_requests'],0)

    def test_restored_workspace_persists_verified_whole_archive_hash(self):
        import hashlib
        from DGXGateway.dgx_gateway.drama.restore import restore_archive
        self.f.pack(); expected=hashlib.sha256(self.f.package.read_bytes()).hexdigest()
        target=self.f.root/'provenance-restored'
        result=restore_archive(self.f.package,target)
        self.assertEqual(result.get('backupSha256'),expected)
        for name in ('restore-state.json','restore-provenance.json'):
            marker=json.loads((target/'drama'/name).read_text())
            self.assertEqual(marker.get('backupSha256'),expected)
