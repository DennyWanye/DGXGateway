"""P7 isolated tiny project archive/restore oracles; no GPU or user state."""
import io
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock,patch
import zipfile
from PIL import Image
from DGXGateway.dgx_gateway.drama.service import DramaService
from DGXGateway.dgx_gateway.drama.contracts import DramaError
from DGXGateway.dgx_gateway.drama.backups import pack_project,projection,snapshot,TABLES,sha,encode,no_replace
from DGXGateway.dgx_gateway.drama.restore import restore_archive,inspect_archive

class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.s=DramaService(self.root/'source',self.root)
        self.pid=self.s.create_project({'title':'P-owned','width':96,'height':64,'requestKey':'p'})['project']['id']
        self.qid=self.s.create_project({'title':'Q-SECRET-SENTINEL','requestKey':'q'})['project']['id']
        self.eid=self.s.create_episode(self.pid,{'title':'episode','requestKey':'e'})['episode']['id']
        self.source=self.s.save_source(self.eid,{'title':'原文','text':'保持 😀 原文','expectedRevision':0,'requestKey':'src'})['revision']
        self.buf=io.BytesIO(); Image.new('RGB',(64,64),'blue').save(self.buf,format='PNG'); self.data=self.buf.getvalue()
        self.aid=self.s.upload_image(self.qid,io.BytesIO(self.data),len(self.data),'Q-SECRET-FILENAME.png','q-image')['asset']['id']
        self.s.upload_image(self.pid,io.BytesIO(self.data),len(self.data),'P.png','p-image')
        self.package=self.root/'project.zip'
    def tearDown(self):
        self.s.close(); self.temp.cleanup()
    def pack(self): return pack_project(self.s,self.pid,self.package,'b'*32)
    def test_41_table_projection_and_shared_asset_no_other_project_bytes(self):
        result=self.pack(); self.assertEqual(len(result['manifest']['tables']),41)
        with zipfile.ZipFile(self.package) as z:
            for name in z.namelist():
                self.assertNotIn(b'Q-SECRET',z.read(name),name)
            rows=[json.loads(line) for line in z.read('rows/dr_projects.jsonl').splitlines()]
            self.assertEqual([r['id'] for r in rows],[self.pid])
            self.assertEqual(z.read('assets/'+self.aid+'.png'),self.data)
    def test_restore_fresh_sqlite_and_source_unicode_and_asset(self):
        self.pack(); dest=self.root/'new'; result=restore_archive(self.package,dest)
        self.assertEqual(result['mode'],'isolated')
        self.assertEqual(result['backupSha256'],sha(self.package.read_bytes()))
        self.assertEqual(json.loads((dest/'drama/restore-provenance.json').read_text())['backupSha256'],result['backupSha256'])
        child=DramaService(dest,self.root)
        try:
            self.assertEqual(child.project(self.pid)['project']['title'],'P-owned')
            self.assertEqual(child.media_path(self.pid,self.aid).read_bytes(),self.data)
            self.assertEqual(child.story_state(self.eid)['revisions']['source'][0]['content']['text'],'保持 😀 原文')
            self.assertTrue(child.production_state(self.pid)['paused'])
            self.assertEqual(child.capabilities()['restoreMode'],'isolated')
        finally: child.close()
    def test_nonempty_and_existing_empty_target_never_overwritten(self):
        self.pack()
        for populated in (False,True):
            dest=self.root/('full' if populated else 'empty'); dest.mkdir()
            if populated: (dest/'keep').write_text('keep')
            with self.assertRaises(DramaError): restore_archive(self.package,dest)
            self.assertEqual(list(dest.iterdir()),[dest/'keep'] if populated else [])
    def test_asset_tamper_and_symlink_rejected(self):
        p=self.s.media_path(self.pid,self.aid); p.write_bytes(b'bad')
        with self.assertRaises(DramaError): self.pack()
        self.assertFalse(self.package.exists()); p.unlink(); p.symlink_to(self.root/'outside')
        with self.assertRaises((DramaError,OSError)): self.pack()
    def test_archive_hash_tamper_fails_before_publish(self):
        self.pack(); altered=self.root/'bad.zip'
        with zipfile.ZipFile(self.package) as src,zipfile.ZipFile(altered,'w') as out:
            for name in src.namelist(): out.writestr(name,b'bad' if name.endswith('.png') else src.read(name))
        with self.assertRaises(DramaError): restore_archive(altered,self.root/'new')
        self.assertFalse((self.root/'new').exists())
    def test_zip_traversal_duplicate_compression_and_symlink_rejected(self):
        for index,(name,compression,mode) in enumerate([('../out',0,None),('/out',0,None),('manifest.json',8,None),('assets/'+'a'*64+'.png',0,0o120777)]):
            p=self.root/f'bad{index}.zip'
            with zipfile.ZipFile(p,'w',compression=compression) as z:
                info=zipfile.ZipInfo(name); info.compress_type=compression
                if mode: info.external_attr=mode<<16
                z.writestr(info,b'{}')
            with self.assertRaises(DramaError): inspect_archive(p)
    def test_snapshot_has_bound_and_cancellation(self):
        with self.assertRaises(DramaError): snapshot(self.s.store,maximum=1)
        with self.assertRaises(DramaError): snapshot(self.s.store,cancel=lambda:True)
    def test_low_space_refuses_without_removing_user_files(self):
        with patch('DGXGateway.dgx_gateway.drama.backups.shutil.disk_usage',return_value=type('Usage',(),{'free':128*1024*1024})()):
            with self.assertRaises(DramaError): self.pack()
        self.assertTrue(self.s.media_path(self.pid,self.aid).exists()); self.assertFalse(self.package.exists())
    def test_explicit_credentials_in_authored_content_reject_without_redaction(self):
        secret='Authorization: Bearer credential-sentinel'
        self.s.save_source(self.eid,{'title':'bad','text':secret,'expectedRevision':1,'requestKey':'bad'})
        with self.assertRaisesRegex(DramaError,'凭据'): self.pack()
        self.assertEqual(self.s.story_state(self.eid)['revisions']['source'][0]['content']['text'],secret)
    def test_restore_model_paths_guard_before_any_same_key_submission(self):
        self.pack(); dest=self.root/'new'; restore_archive(self.package,dest)
        adapter=Mock(); child=DramaService(dest,self.root,reference_adapter=adapter,video_adapter=adapter,text_adapter=adapter)
        try:
            for operation in (lambda:child.submit_video_batch(self.eid,{}),lambda:child.create_text_job(self.eid,{}),lambda:child.submit_reference_job('entity','a'*32,{}),lambda:child.refresh_video_job('a'*32),lambda:child.cancel_text_job('a'*32),lambda:child.create_transcription('a'*32,{})):
                with self.assertRaisesRegex(DramaError,'隔离'): operation()
            time.sleep(.02); self.assertFalse(adapter.mock_calls)
        finally: child.close()
    def test_async_receipt_same_key_and_restore(self):
        first=self.s.create_backup(self.pid,{'requestKey':'backup','expectedProjectRevision':1})['operation']
        second=self.s.create_backup(self.pid,{'requestKey':'backup','expectedProjectRevision':1})['operation']; self.assertEqual(first['id'],second['id'])
        for _ in range(100):
            row=self.s.backup_operation(first['id'])['operation']
            if row['state'] in ('SUCCEEDED','FAILED'):break
            time.sleep(.01)
        self.assertEqual(row['state'],'SUCCEEDED',row)
        self.assertTrue(self.s.backup_download(row['id']).is_file())
    def test_uploaded_package_idempotency_verifies_content_without_replay(self):
        self.pack(); data=self.package.read_bytes()
        first=self.s.upload_restore(io.BytesIO(data),len(data),'upload-key')['upload']
        second=self.s.upload_restore(io.BytesIO(data),len(data),'upload-key')['upload']; self.assertEqual(first,second)
        bad=data[:-1]+bytes([data[-1]^1])
        with self.assertRaises(DramaError): self.s.upload_restore(io.BytesIO(bad),len(bad),'upload-key')
        self.assertEqual(first['preview']['assetCount'],1)
        self.s._restore_depth=4
        with self.assertRaisesRegex(DramaError,'4层'):self.s.create_restore({'uploadId':first['id'],'requestKey':'too-deep'})
        self.assertFalse(list(self.s._restore_root.iterdir()))
    def test_restore_archive_sqlite_is_opaque_not_executed(self):
        self.pack(); alternate=self.root/'opaque.zip'
        with zipfile.ZipFile(self.package) as z:
            members={name:z.read(name) for name in z.namelist()}
        evil=b'SQLite format 3\x00 MALICIOUS TRIGGER SENTINEL'
        manifest=json.loads(members['manifest.json']); old=next(i for i in manifest['files'] if i['path']=='project.sqlite3'); manifest['totalBytes']+=len(evil)-old['bytes']; old.update(bytes=len(evil),sha256=sha(evil)); members['project.sqlite3']=evil; members['manifest.json']=encode(manifest).encode()
        with zipfile.ZipFile(alternate,'w') as z:
            for name,data in members.items(): z.writestr(name,data)
        result=restore_archive(alternate,self.root/'opaque-restored'); self.assertEqual(result['assetCount'],1)
        with closing(sqlite3.connect(self.root/'opaque-restored/drama/drama.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT title FROM dr_projects').fetchone()[0],'P-owned')
    def test_new_schema_column_is_refused(self):
        with self.s.store.connect(write=True) as db: db.execute('ALTER TABLE dr_projects ADD COLUMN surprise TEXT')
        with self.assertRaisesRegex(DramaError,'列结构'): self.pack()
    def test_cross_project_json_asset_reference_is_rejected(self):
        with self.s.store.connect(write=True) as db:
            db.execute('INSERT INTO dr_events VALUES(?,?,?,?,?)',('e'*32,'project:'+self.pid,'review_suggestion',encode({'takeId':'d'*32,'suggestion':'foreign'}),time.time()))
        with self.assertRaisesRegex(DramaError,'越出项目'): self.pack()
    def test_business_actor_and_project_scope_event_are_preserved(self):
        with self.s.store.connect(write=True) as db:
            db.execute('INSERT INTO dr_events VALUES(?,?,?,?,?)',('e'*32,'review-suggestion:'+self.pid,'mutation',encode({'actor':'business_mcp','requestKey':'agent'}),time.time()))
            db.execute('INSERT INTO dr_events VALUES(?,?,?,?,?)',('f'*32,'project:'+self.pid,'review_suggestion',encode({'suggestion':'文字建议','actor':'business_mcp'}),time.time()))
        self.pack()
        with zipfile.ZipFile(self.package) as z:
            events=[json.loads(x) for x in z.read('rows/dr_events.jsonl').splitlines()]
        self.assertTrue(any(r['scope']=='project:'+self.pid for r in events)); self.assertTrue(any('business_mcp' in r['payload'] for r in events))
    def test_restored_tasks_preserve_original_state_cancel_and_cleanup_and_no_auto_query(self):
        jid='f'*32
        with self.s.store.connect(write=True) as db:
            db.execute('INSERT INTO dr_video_jobs(id,project_id,episode_id,dispatch_key,gateway_job_id,state,memory_released,cleanup_required,cancel_requested,reserved_shots,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(jid,self.pid,self.eid,'original-dispatch','gateway-job','RUNNING',0,1,1,1,time.time(),time.time()))
        self.pack(); dest=self.root/'recover-jobs'; restored=restore_archive(self.package,dest)
        self.assertEqual(restored['recovery']['tasks'][0]['gatewayId'],'gateway-job')
        adapter=Mock(); child=DramaService(dest,self.root,video_adapter=adapter)
        try:
            with child.store.connect() as db:
                row=dict(db.execute('SELECT * FROM dr_video_jobs WHERE id=?',(jid,)).fetchone())
            self.assertEqual((row['state'],row['cancel_requested'],row['cleanup_required']),('RUNNING',1,1)); self.assertFalse(adapter.mock_calls)
        finally:child.close()
        (dest/'drama/restore-state.json').unlink()
        with self.assertRaises(DramaError): DramaService(dest,self.root)
    def test_query_only_preserves_cancel_intent_and_queries_original_identity_without_cancel(self):
        jid='f'*32; gid='a'*32
        with self.s.store.connect(write=True) as db:
            db.execute('INSERT INTO dr_video_jobs(id,project_id,episode_id,dispatch_key,gateway_job_id,state,memory_released,cleanup_required,cancel_requested,reserved_shots,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(jid,self.pid,self.eid,'original-dispatch',gid,'RUNNING',0,1,1,1,time.time(),time.time()))
        self.pack(); dest=self.root/'query-workspace'; restore_archive(self.package,dest)
        adapter=Mock(); adapter.get.return_value={'job_id':gid,'idempotency_key':'original-dispatch','state':'RUNNING','shots':[],'memory_released':False,'cleanup_required':True,'cancel_requested':False}; adapter.outputs.return_value=[]
        child=DramaService(dest,self.root,video_adapter=adapter)
        try:
            with self.assertRaises(DramaError): child.refresh_video_job(jid)
            child._restore_mode='query_only'
            result=child.refresh_video_job(jid)['job']
            adapter.get.assert_called_once_with(gid); adapter.cancel.assert_not_called(); adapter.submit.assert_not_called()
            self.assertTrue(result['cancelRequested']); self.assertTrue(result['cleanupRequired'])
            for action in (lambda:child.cancel_video_job(jid),lambda:child.dispatch_video_once(),lambda:child.submit_video_batch(self.eid,{})):
                with self.assertRaises(DramaError): action()
        finally:child.close()
    def test_disk_write_failure_removes_only_owned_partial(self):
        self.pack(); dest=self.root/'fault'; keep=self.root/'keep'; keep.write_text('user')
        with patch('DGXGateway.dgx_gateway.drama.restore.write_database',side_effect=OSError(28,'disk full')):
            with self.assertRaises(OSError): restore_archive(self.package,dest)
        self.assertFalse(dest.exists()); self.assertFalse((self.root/'.restore-fault.partial').exists()); self.assertEqual(keep.read_text(),'user')
    def test_unknown_owned_scope_and_response_fail_closed(self):
        with self.s.store.connect(write=True) as db:
            db.execute('INSERT INTO dr_requests VALUES(?,?,?,?,?)',('future:'+self.pid,'new','hash','{}',time.time()))
        with self.assertRaisesRegex(DramaError,'scope'): self.pack()
        with self.s.store.connect(write=True) as db:
            db.execute("DELETE FROM dr_requests WHERE request_key='new'")
            db.execute('INSERT INTO dr_requests VALUES(?,?,?,?,?)',('project:'+self.pid,'new','hash',encode({'unknownFutureRaw':'foreign bytes'}),time.time()))
        with self.assertRaisesRegex(DramaError,'响应字段'): self.pack()
    def test_archive_path_and_member_type_attack_matrix(self):
        cases=[('../escape',0,0,b''),('C:/escape',0,0,b''),('rows\\escape',0,0,b''),('rows/./a',0,0,b''),('Assets/'+'a'*64+'.png',0,0,b''),('assets/ä.png',0,0,b''),('manifest.json',0,0o020600,b''),('manifest.json',0,0o010600,b''),('manifest.json',8,0,b''),('manifest.json',0,0,b'\x01\x00\x00\x00')]
        for index,(name,method,mode,extra) in enumerate(cases):
            with self.subTest(name=name,mode=mode,method=method):
                package=self.root/f'attack-{index}.zip'
                with zipfile.ZipFile(package,'w') as archive:
                    info=zipfile.ZipInfo(name);info.compress_type=method;info.external_attr=mode<<16;info.extra=extra;archive.writestr(info,b'{}')
                with self.assertRaises(DramaError):inspect_archive(package)
                package.unlink()
        duplicate=self.root/'duplicate.zip'
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore',UserWarning)
            with zipfile.ZipFile(duplicate,'w') as archive:archive.writestr('manifest.json',b'{}');archive.writestr('manifest.json',b'{}')
        with self.assertRaises(DramaError):inspect_archive(duplicate)
    def test_publication_race_and_parent_symlink_preserve_original(self):
        from DGXGateway.dgx_gateway.drama.backups import open_regular
        parent=self.root/'safe';parent.mkdir();link=self.root/'link';link.symlink_to(parent,target_is_directory=True)
        (parent/'file').write_bytes(b'user')
        with self.assertRaises(DramaError):open_regular(link/'file')
        source=self.root/'staged';source.mkdir();(source/'new').write_text('new')
        target=self.root/'target';target.mkdir();(target/'keep').write_text('keep')
        with self.assertRaises(OSError):no_replace(source,target)
        self.assertEqual((target/'keep').read_text(),'keep');self.assertEqual((source/'new').read_text(),'new')
    def test_credentials_matrix_refuses_authored_text_without_rewriting(self):
        values=['-----BEGIN PRIVATE KEY-----','eyJabcdefghijk.abcdefghijk.abcdefghijk','password: abcdefghijk','https://example.invalid/file?X-Amz-Signature=abcdef','hf_'+'a'*30]
        for index,value in enumerate(values):
            with self.subTest(index=index):
                self.s.save_source(self.eid,{'title':'sensitive','text':value,'expectedRevision':index+1,'requestKey':'secret'+str(index)})
                with self.assertRaisesRegex(DramaError,'凭据'):self.pack()
                self.assertEqual(self.s.story_state(self.eid)['revisions']['source'][0]['content']['text'],value)
    def test_backup_history_pagination_does_not_include_other_project(self):
        for index in range(103):
            oid=f'{index:032x}'
            (self.s._backup_root/(oid+'.receipt.json')).write_text(encode({'id':oid,'projectId':self.pid,'createdAt':index,'state':'SUCCEEDED','kind':'backup'}))
        foreign='f'*32
        (self.s._backup_root/(foreign+'.receipt.json')).write_text(encode({'id':foreign,'projectId':self.qid,'createdAt':1000,'state':'SUCCEEDED','kind':'backup'}))
        first=self.s.project_backups(self.pid);self.assertEqual(len(first['operations']),100)
        second=self.s.project_backups(self.pid,first['nextCursor']);self.assertEqual(len(second['operations']),3);self.assertIsNone(second['nextCursor'])
        self.assertNotIn(foreign,[x['id'] for x in first['operations']+second['operations']])
        with self.assertRaises(DramaError):self.s.project_backups(self.pid,foreign)
    def test_original_queued_cpu_export_keys_cannot_replay_after_restore(self):
        tid='c'*32; xid='d'*32; old='e'*32
        body={'requestKey':'original-export'}; retry={'requestKey':'original-retry'}
        with self.s.store.connect(write=True) as db:
            db.execute('INSERT INTO dr_timeline_revisions(id,episode_id,revision,snapshot,created_at) VALUES(?,?,?,?,?)',(tid,self.eid,1,'{}',time.time()))
            for oid,state in ((xid,'QUEUED'),(old,'FAILED')):
                db.execute('INSERT INTO dr_exports VALUES(?,?,?,?,?,?,?,?,?)',(oid,self.pid,self.eid,tid,state,None,None,time.time(),time.time()))
            db.execute('INSERT INTO dr_export_recipes(export_id,recipe) VALUES(?,?)',(old,'{}'))
            for scope,key,payload,response in [('exports:'+tid,body['requestKey'],body,{'export':{'id':xid}}),('export-retry:'+old,retry['requestKey'],retry,{'exportId':xid})]:
                db.execute('INSERT INTO dr_requests VALUES(?,?,?,?,?)',(scope,key,sha(encode(payload).encode()),encode(response),time.time()))
        self.pack();dest=self.root/'queued-export';restore_archive(self.package,dest);child=DramaService(dest,self.root)
        try:
            with patch.object(child._executor,'submit') as submit:
                self.assertEqual(child.create_export(tid,body)['export']['state'],'QUEUED')
                self.assertEqual(child.retry_export(old,retry)['export']['state'],'QUEUED')
                submit.assert_not_called()
            with child.store.connect() as db:self.assertEqual(child.active_export_count(db),0)
        finally:child.close()
    def test_all_40_tables_have_owned_rows_and_survive_projection(self):
        # Real P6 source/entities/beats/script/storyboard history, plus remaining
        # structural task rows; the separate CPU test validates playable media.
        from tests.test_dgx_drama_scripts import ScriptTests
        flow=ScriptTests();flow.s=self.s;flow.pid=self.pid;flow.eid=self.eid;flow.seq=1000
        src,hero,place,beats,script,proposal=flow.flow()
        entities=flow.save('entities',flow.candidate_content(src))
        self.s.confirm_entity_candidate(entities['id'],{'candidateId':'hero','expectedRevision':1,'requestKey':'map-hero'})
        board=flow.save('storyboard',proposal)
        self.s.import_storyboard(board['id'],{'candidateIds':['shot1'],'expectedRevision':1,'requestKey':'map-shot'})
        story_ids={'entities':entities['id'],'storyboard':board['id'],'script':script['id']}
        generated={t:sha(t.encode())[:32] for t in TABLES};generated.update(dr_projects=self.pid,dr_episodes=self.eid,dr_assets=self.aid)
        json_fields={'metadata','snapshot','parameters','input_snapshot','response','payload','content','dependencies','validation','sources','dialogue','recipe','outputs','result','receipt'}
        with self.s.store.connect(write=True) as db:
            db.execute('PRAGMA defer_foreign_keys=ON')
            for table in TABLES:
                columns=[r['name'] for r in db.execute(f'PRAGMA table_info({table})')]
                if 'id' in columns:
                    row=db.execute(f'SELECT id FROM {table} LIMIT 1').fetchone()
                    if row:generated[table]=row['id']
            generated.update(dr_projects=self.pid,dr_episodes=self.eid,dr_assets=self.aid)
            generated['dr_entity_revisions']=db.execute('SELECT current_revision_id FROM dr_entities WHERE id=?',(generated['dr_entities'],)).fetchone()[0]
            generated['dr_shot_revisions']=db.execute('SELECT current_content_revision_id FROM dr_shots WHERE id=?',(generated['dr_shots'],)).fetchone()[0]
            existing={t:bool(db.execute(f'SELECT 1 FROM {t} LIMIT 1').fetchone()) for t in TABLES}
            for table in TABLES:
                if existing[table]:continue
                cols=list(db.execute(f'PRAGMA table_info({table})')); row={}
                for c in cols:
                    name,kind=c['name'],c['type']; row[name]=1 if kind=='INTEGER' else time.time() if kind=='REAL' else '{}' if name in json_fields else 'fixture'
                    if name=='id':row[name]=generated[table]
                    if not c['notnull'] and not c['pk']:row[name]=None
                if 'id' in row:row['id']=generated[table]
                for fk in db.execute(f'PRAGMA foreign_key_list({table})'):
                    row[fk['from']]=generated[fk['table']]
                if 'state' in row:row['state']='SUCCEEDED'
                if 'kind' in row:row['kind']='character' if table=='dr_entities' else 'source' if table in {'dr_story_revisions','dr_story_heads','dr_story_drafts'} else 'mutation' if table=='dr_events' else 'extract_entities'
                if 'target_type' in row:row.update(target_type='entity',target_id=generated['dr_entities'])
                for key in ('target_revision_id','revision_id'):
                    if key in row and table in {'dr_reference_jobs','dr_reference_candidates','dr_production_approvals'}:row[key]=generated['dr_entity_revisions']
                if table=='dr_story_heads':row.update(current_revision_id=self.source['id'],approved_revision_id=self.source['id'])
                if table=='dr_story_approvals':row['revision_id']=self.source['id']
                if table in {'dr_entity_proposal_mappings','dr_storyboard_mappings'}:row['proposal_revision_id']=story_ids['entities' if table=='dr_entity_proposal_mappings' else 'storyboard']
                if table=='dr_script_dependencies':row.update(script_revision_id=story_ids['script'],dialogue='[]')
                if table=='dr_script_invalidations':row.update(old_revision_id=story_ids['script'],new_revision_id=story_ids['script'])
                if table=='dr_text_jobs':row.update(current_attempt_id=generated['dr_text_attempts'],repair_of=None,resource_status='released',payload='{}')
                if table=='dr_text_receipt_evidence':row['attempt_id']=generated['dr_text_attempts']
                if table=='dr_text_attempts':row.update(receipt='{}',raw_text='draft',raw_hash=sha(b'draft'))
                if table=='dr_story_drafts':row.update(kind='entities',attempt_id=generated['dr_text_attempts'],raw_text='{}',raw_hash=sha(b'{}'),validation='{}')
                if table=='dr_video_jobs':row.update(cleanup_required=0,memory_released=1,cancel_requested=0,resource_settled=1,resource_status='released')
                if table=='dr_production_state':row['paused']=1
                if table=='dr_export_recipes':row.update(recipe='{}',outputs='[]')
                if table=='dr_reference_candidates':row['job_id']=generated['dr_reference_jobs']
                if table=='dr_reference_candidate_reviews':row.update(candidate_id=generated['dr_reference_candidates'],decision='approved',notes='')
                if table=='dr_timeline_revisions':row['snapshot']='{}'
                db.execute(f'INSERT INTO {table} ({",".join(row)}) VALUES ({",".join("?" for _ in row)})',list(row.values()))
            # Both ends of cyclic content/current/selected references now exist.
        result=self.pack()
        self.assertEqual(set(result['manifest']['tables']),set(TABLES))
        self.assertTrue(all(n>0 for n in result['manifest']['tables'].values()),result['manifest']['tables'])
        restored=restore_archive(self.package,self.root/'all-tables')
        self.assertEqual(restored['tables'],result['manifest']['tables'])
    def test_real_tiny_video_subtitles_and_cpu_reexport_after_restore(self):
        import subprocess
        from DGXGateway.dgx_gateway.drama.media import probe_media
        source=self.root/'one-second.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=blue:size=96x64:rate=24','-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','1','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(source)],check=True)
        data=source.read_bytes(); asset=self.s.upload(self.pid,io.BytesIO(data),len(data),'tiny.mp4','video')['asset']
        shot=self.s.create_shot(self.eid,{'title':'tiny','assetId':asset['id'],'requestKey':'shot'})['shot']
        take=shot['takes'][0]['id']; self.s.select_take(shot['id'],{'takeId':take,'expectedRevision':1,'requestKey':'choose'})
        tl=self.s.save_timeline(self.eid,{'clips':[{'clipId':'a','takeId':take,'inFrame':0,'frames':24,'keepSourceAudio':True,'fit':'contain'}],'audioClips':[],'expectedRevision':0,'requestKey':'timeline'})['timeline']
        sub=self.s.save_subtitles(tl['id'],{'segments':[{'startFrame':0,'endFrame':24,'text':'恢复测试'}],'expectedRevision':0,'requestKey':'sub'})['subtitle']
        self.s.approve_subtitles(sub['id'],{'timelineId':tl['id'],'requestKey':'approve-sub'})
        def export(service,key):
            row=service.create_export(tl['id'],{'outputs':['master','srt','ass','report'],'subtitleRevisionId':sub['id'],'requestKey':key})['export']
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                row=service.export(row['id'])['export']
                if row['state'] in ('SUCCEEDED','FAILED'):break
                time.sleep(.02)
            self.assertEqual(row['state'],'SUCCEEDED',row);return row
        original=export(self.s,'export-before'); self.pack()
        dest=self.root/'cpu-restored';restore_archive(self.package,dest); child=DramaService(dest,self.root)
        try:
            self.assertEqual(child.episode(self.eid)['timeline'],self.s.episode(self.eid)['timeline'])
            self.assertEqual(child.subtitles(tl['id'])['subtitles'][0]['segments'][0]['text'],'恢复测试')
            exported=export(child,'export-after')
            outputs=exported['outputs'];self.assertEqual({o['role'] for o in outputs},{'master','srt','ass','report'})
            master=next(o['assetId'] for o in outputs if o['role']=='master'); self.assertEqual(probe_media(child.media_path(self.pid,master))['frames'],24)
            self.assertEqual(child.capabilities()['restoreMode'],'isolated')
        finally:child.close()

class BackupHTTPTests(unittest.TestCase):
    def setUp(self):
        import threading
        from types import SimpleNamespace
        from DGXGateway.dgx_gateway.server import GatewayHTTPServer
        BackupTests.setUp(self)
        self.app=SimpleNamespace(drama=self.s,config=SimpleNamespace(lan_no_auth=True,allowed_origins=('http://localhost',)),authorized=lambda cookie:True)
        self.server=GatewayHTTPServer(('127.0.0.1',0),self.app)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();BackupTests.tearDown(self)
    def request(self,method,path,body=None,headers=None):
        import http.client
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=20)
        request_headers=headers or {}
        if isinstance(body,dict):body=encode(body).encode();request_headers={'Content-Type':'application/json',**request_headers}
        conn.request(method,'/api/v1/drama'+path,body,request_headers);response=conn.getresponse();result=(response.status,dict(response.getheaders()),response.read());conn.close();return result
    def wait(self,kind,oid):
        for _ in range(200):
            code,_,raw=self.request('GET',f'/{kind}/{oid}');self.assertEqual(code,200,raw);value=json.loads(raw)['operation']
            if value['state'] in ('SUCCEEDED','FAILED'):break
            time.sleep(.01)
        self.assertEqual(value['state'],'SUCCEEDED',value);return value
    def test_http_backup_download_upload_restore_and_isolated_namespace(self):
        code,_,raw=self.request('POST',f'/projects/{self.pid}/backups',{'requestKey':'http-backup','expectedProjectRevision':1});self.assertEqual(code,202,raw)
        bid=json.loads(raw)['operation']['id'];self.wait('backups',bid)
        code,headers,data=self.request('GET',f'/backups/{bid}/download');self.assertEqual(code,200);self.assertEqual(headers['Content-Type'],'application/zip')
        self.assertIn('attachment',headers['Content-Disposition'])
        code,headers,part=self.request('GET',f'/backups/{bid}/download',headers={'Range':'bytes=0-9'});self.assertEqual((code,part),(206,data[:10]))
        code,headers,empty=self.request('HEAD',f'/backups/{bid}/download');self.assertEqual((code,empty),(200,b''));self.assertEqual(int(headers['Content-Length']),len(data))
        code,_,raw=self.request('POST','/restore-uploads',data,{'Content-Type':'application/octet-stream','X-Request-Key':'http-upload'});self.assertEqual(code,201,raw);upload=json.loads(raw)['upload']
        self.assertEqual(upload['preview']['projectId'],self.pid)
        self.assertFalse(list(self.s._restore_root.iterdir()),'upload must not restore automatically')
        code,_,raw=self.request('POST','/restores',{'uploadId':upload['id'],'requestKey':'http-restore'});self.assertEqual(code,202,raw)
        rid=json.loads(raw)['operation']['id'];self.wait('restores',rid)
        prefix=f'/restored/{rid}'
        code,_,raw=self.request('GET',prefix+'/capabilities');self.assertEqual(code,200,raw);self.assertEqual(json.loads(raw)['restoreMode'],'isolated')
        code,_,raw=self.request('GET',prefix+f'/projects/{self.pid}/assets/{self.aid}/media');self.assertEqual((code,raw),(200,self.data))
        self.assertEqual(self.request('GET',prefix+f'/projects/{self.qid}')[0],404)
        code,_,raw=self.request('POST',f'/restores/{rid}/query-mode',{'requestKey':'allow-query','mode':'query_only'});self.assertEqual(code,200,raw)
        self.assertEqual(json.loads(self.request('GET',prefix+'/capabilities')[2])['restoreMode'],'query_only')
        # A restored project can itself be backed up/restored without losing its parent namespace.
        code,_,raw=self.request('POST',prefix+f'/projects/{self.pid}/backups',{'requestKey':'nested-backup','expectedProjectRevision':1});self.assertEqual(code,202,raw)
        nested_bid=json.loads(raw)['operation']['id'];self.wait(f'restored/{rid}/backups',nested_bid)
        code,_,raw=self.request('POST',prefix+'/restores',{'backupId':nested_bid,'requestKey':'nested-restore'});self.assertEqual(code,202,raw)
        nested_rid=json.loads(raw)['operation']['id'];self.wait(f'restored/{rid}/restores',nested_rid)
        code,_,raw=self.request('GET',prefix+f'/restored/{nested_rid}/capabilities');self.assertEqual(code,200,raw);self.assertEqual(json.loads(raw)['restoreMode'],'isolated')

    def test_http_image_review_queue_parses_only_single_whitelisted_parameters(self):
        code,_,raw=self.request('GET',f'/projects/{self.pid}/image-review-queue?scope=episode&episodeId={self.eid}')
        self.assertEqual(code,200,raw)
        payload=json.loads(raw)
        self.assertEqual((payload['scope'], payload['episodeId'], payload['items']), ('episode', self.eid, []))
        for suffix in ('?scope=project&scope=episode', '?unknown=value', '?episodeId='+self.eid+'&episodeId='+self.eid):
            code,_,raw=self.request('GET',f'/projects/{self.pid}/image-review-queue{suffix}')
            self.assertEqual(code,422,raw)

    def test_http_unknown_key_recovery_conflict_and_access_checks(self):
        body={'requestKey':'original-http','expectedProjectRevision':1}
        first=self.request('POST',f'/projects/{self.pid}/backups',body);self.assertEqual(first[0],202)
        second=self.request('POST',f'/projects/{self.pid}/backups',body);self.assertEqual(json.loads(first[2])['operation']['id'],json.loads(second[2])['operation']['id'])
        self.assertEqual(self.request('POST',f'/projects/{self.pid}/backups',{**body,'expectedProjectRevision':2})[0],409)
        self.assertEqual(self.request('POST',f'/projects/{self.pid}/backups',body,{'Origin':'https://untrusted.test'})[0],403)
        self.app.authorized=lambda cookie:False
        self.assertEqual(self.request('GET',f'/projects/{self.pid}/backups')[0],403)
