import copy, json, unittest
from DGXGateway.dgx_gateway.h3_prompt import compile_prompt
from DGXGateway.dgx_gateway.drama.contracts import DramaError
import test_dgx_drama_video as fixture
from test_dgx_drama_scripts import FakeText

VALUE = dict(subject_definitions='<Subject 1> is the left fighter in <Picture 1>.', summary='[reference generation] One guarded punch.', retention_analysis='Keep identity, clothing, left/right positions and background.', detailed_description='[Shot 1] Medium side view. <Subject 1> extends one punch, meets the guard, then retracts. Locked camera, daylight.', overall_soundscape='No dialogue, no speech, no voices. Wind and one brief contact sound.', non_diegetic_music='None.')
class PromptTests(unittest.TestCase):
    setUp = fixture.DramaVideoTests.setUp
    tearDown = fixture.DramaVideoTests.tearDown
    stop_poller = fixture.DramaVideoTests.stop_poller
    key = fixture.DramaVideoTests.key
    shot = fixture.DramaVideoTests.shot
    def test_exact_dialogue_and_no_extra_speech(self):
        value = copy.deepcopy(VALUE)
        value['overall_soundscape'] = '<d>[Chinese] 我回来了。</d> (S1), no other speech.'
        self.assertIn('我回来了。', compile_prompt(value, {'soundMode':'native','dialogue':'我回来了。'}))
        for bad in ['我回来啦。','我回来了。你好。']:
            with self.assertRaises(ValueError): compile_prompt(value, {'soundMode':'native','dialogue':bad})
        with self.assertRaises(ValueError): compile_prompt(value, {'soundMode':'silent'})
        with self.assertRaises(ValueError): compile_prompt({**value, 'summary': value['summary'] + '<d>[English] Surprise!</d>'}, {'soundMode':'native','dialogue':'我回来了。'})
    def test_unknown_reference_and_utf8_overflow_rejected(self):
        for key, text in [('subject_definitions','<Subject 1> in <Picture 2>.'),('summary','中'*1400)]:
            value={**VALUE,key:text}
            with self.assertRaises(ValueError): compile_prompt(value, {'soundMode':'silent'})
        with self.assertRaises(ValueError): compile_prompt({**VALUE,'extra':'oops'}, {})
    def test_chinese_silence_is_rendered_explicitly_without_losing_raw_or_bypassing_limits(self):
        from DGXGateway.dgx_gateway.drama.prompts import prepare_video_prompt
        value={**VALUE,'overall_soundscape':'轻微风声，无对白，无人声，无背景音乐。'}
        original=copy.deepcopy(value)
        compiled, notes=prepare_video_prompt(value,{'soundMode':'silent'})
        self.assertIn('No dialogue, no speech, no voices.',compiled)
        self.assertEqual(value,original)
        self.assertEqual(len(notes),1)
        omitted={**VALUE,'overall_soundscape':'轻微风声，挥拳破空声，一次格挡撞击。'}
        self.assertIn('No dialogue, no speech, no voices.',prepare_video_prompt(omitted,{'soundMode':'silent'})[0])
        for patch in ({'summary': '<d>[Chinese] 不该出现</d>'}, {'summary':'中'*1400}, {'overall_soundscape':'悟空说话，弗利萨回答。'}):
            with self.assertRaises(ValueError): prepare_video_prompt({**value,**patch},{'soundMode':'silent'})
        self.service.text_adapter=FakeText(json.dumps(value)); shot=self.shot()
        job=self.service.create_text_job(self.eid,{'kind':'draft_video_prompt','sourceRevisionIds':[],'shotRevisionId':shot['shotRevisionId'],'brief':'一次动作','soundMode':'silent','requestKey':self.key()})['job']
        job=self.service.dispatch_text_job(job['id'])['job']
        self.assertEqual(job['compiledPrompt'],compiled)
        self.assertEqual(json.loads(job['attempts'][-1]['rawText']),original)
        result=self.service.submit_video_batch(self.eid,{'shots':[{**shot,'prompt':compiled,'promptJobId':job['id']}],'requestKey':self.key()})
        self.assertEqual(result['job']['items'][0]['prompt'],compiled)
    def test_freeze_validate_apply_and_changed_prompt_cannot_impersonate_writer(self):
        self.service.text_adapter = FakeText(json.dumps(VALUE))
        shot = self.shot()
        body={'kind':'draft_video_prompt','sourceRevisionIds':[], 'shotRevisionId':shot['shotRevisionId'],'brief':'左侧人物打一拳，保持衣服和位置','soundMode':'silent','dialogue':'','requestKey':self.key()}
        job=self.service.create_text_job(self.eid,body)['job']
        job=self.service.dispatch_text_job(job['id'])['job']
        self.assertEqual(job['outputStatus'],'valid_draft')
        self.assertEqual(job['inputRevisions']['imageAssetId'],self.image)
        task=next(t for t in self.service.task_index()['tasks'] if t['id']==job['id'])
        self.assertEqual((task['taskKind'],task['targetId']),('draft_video_prompt',shot['shotId']))
        shot.update(prompt=compile_prompt(VALUE,{'soundMode':'silent'}),promptJobId=job['id'])
        batch=self.service.submit_video_batch(self.eid,{'shots':[shot],'requestKey':self.key()})['job']
        self.assertEqual(batch['items'][0]['prompt'],shot['prompt'])
        from DGXGateway.dgx_gateway.drama.backups import pack_project
        from DGXGateway.dgx_gateway.drama.restore import restore_archive
        from pathlib import Path
        import tempfile, sqlite3
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory); archive=path/'prompt.zip'
            pack_project(self.service,self.pid,archive,'a'*32)
            restore_archive(archive,path/'restored')
            with sqlite3.connect(path/'restored/drama/drama.sqlite3') as db:
                self.assertEqual(db.execute('SELECT kind FROM dr_text_jobs WHERE id=?',(job['id'],)).fetchone()[0],'draft_video_prompt')
                self.assertIn(job['id'],db.execute('SELECT input_snapshot FROM dr_video_items WHERE job_id=?',(batch['id'],)).fetchone()[0])
        with self.assertRaises(DramaError): self.service.submit_video_batch(self.eid,{'shots':[{**shot,'prompt':shot['prompt']+' changed'}],'requestKey':self.key()})
        with self.service.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM dr_story_drafts').fetchone()[0],0)
    def test_new_kind_rejects_cross_episode_and_story_fields(self):
        self.service.text_adapter=FakeText(json.dumps(VALUE));shot=self.shot()
        other=self.service.create_episode(self.pid,{'title':'other','requestKey':self.key()})['episode']['id']
        body={'kind':'draft_video_prompt','sourceRevisionIds':[],'shotRevisionId':shot['shotRevisionId'],'brief':'punch','requestKey':self.key()}
        with self.assertRaises(DramaError):self.service.create_text_job(other,body)
        with self.assertRaises(DramaError):self.service.create_text_job(self.eid,{**body,'entityRevisionIds':[]})
