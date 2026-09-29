"""P6 D27/D28 decisive gaps; real isolated service, no model calls."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock

from tests import test_dgx_drama_scripts as fixtures
from DGXGateway.dgx_gateway.drama.business_mcp import call, CALL_SOURCE
from DGXGateway.dgx_gateway.drama.contracts import DramaError
from DGXGateway.dgx_gateway.drama.service import DramaService


class P6CoverageReviewTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ScriptTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)

    def test_d27_invalid_speaker_references_do_not_create_revisions_or_approvals(self):
        f = self.f
        _, _, place, _, script, _ = f.flow()
        other = f.s.create_project({'title': 'foreign', 'requestKey': 'foreign'})['project']
        foreign = f.s.create_entity(other['id'], {'kind': 'character', 'name': 'foreign secret', 'requestKey': 'foreign'})['entity']
        before = f.s.story_state(f.eid)
        for speaker in ('f' * 32, place['id'], foreign['id']):
            with self.subTest(speaker=speaker):
                content = deepcopy(script['content'])
                content['scenes'][0]['lines'][0]['speakerId'] = speaker
                with self.assertRaises(DramaError) as error:
                    f.save('script', content)
                self.assertEqual(error.exception.code, 'entity_reference_invalid')
                self.assertNotIn('foreign secret', error.exception.message)
                self.assertEqual(f.s.story_state(f.eid), before)
        self.assertEqual(f.s.episode(f.eid)['shots'], [])

    def test_d28_real_nonempty_stale_shot_mcp_matches_service_zero_dispatch(self):
        f = self.f
        *_, script, proposal = f.flow()
        saved = f.save('storyboard', proposal)
        shot = f.s.import_storyboard(saved['id'], {'candidateIds': ['shot1'], 'expectedRevision': 1, 'requestKey': 'import'})['shots'][0]
        content = deepcopy(script['content'])
        content['scenes'][0]['lines'][0]['text'] = '新台词。'
        f.approve(f.save('script', content))
        production = f.s.shot_production(shot['id'])
        adapter = Mock()
        f.s.video_adapter = adapter
        self.addCleanup(setattr, f.s, 'video_adapter', None)
        body = {'requestKey': 'blocked', 'shots': [{'shotId': shot['id'], 'shotRevisionId': production['currentRevisionId'], 'audioAssetId': 'f' * 64, 'prompt': '拍摄', 'seed': 1}]}
        with f.s.store.connect() as db:
            counts = {t: db.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0] for t in ['dr_events', 'dr_video_jobs', 'dr_video_items', 'dr_production_approvals', 'dr_takes']}
        for fn in (
            lambda: f.s.submit_video_batch(f.eid, body),
            lambda: call(f.s, 'drama_request_video_take', {'projectId': f.pid, 'arguments': {'episodeId': f.eid, **body}}),
        ):
            with self.assertRaises(DramaError) as error:
                fn()
            self.assertEqual(error.exception.code, 'dependencies_stale')
        self.assertEqual(adapter.mock_calls, [])
        self.assertEqual(CALL_SOURCE.get(), 'ui_api')
        with f.s.store.connect() as db:
            self.assertEqual({t: db.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0] for t in counts}, counts)

    def test_m11_successful_repair_and_lost_repair_receipt_never_third_post(self):
        f = self.f
        for lost in (False, True):
            with self.subTest(lost=lost):
                adapter, body = f.text_setup('{}')
                body['requestKey'] = f.key()
                original = f.s.create_text_job(f.eid, body)['job']
                f.s.dispatch_text_job(original['id'])
                source = f.s.story_state(f.eid)['revisions']['source'][0]
                adapter.output = json.dumps(f.candidate_content(source), ensure_ascii=False)
                adapter.lost = lost
                repair_body = {**body, 'requestKey': f.key(), 'repairOf': original['id']}
                repair = f.s.create_text_job(f.eid, repair_body)['job']
                done = f.s.dispatch_text_job(repair['id'])['job']
                if lost:
                    self.assertEqual(done['state'], 'SUBMISSION_UNKNOWN')
                    self.assertEqual(f.s.create_text_job(f.eid, repair_body)['job']['id'], repair['id'])
                    f.s.dispatch_text_job(repair['id'])
                    done = f.s.reconcile_text_job(repair['id'])['job']
                self.assertEqual(done['outputStatus'], 'valid_draft')
                self.assertEqual(adapter.calls, 2)
                with self.assertRaises(DramaError):
                    f.s.create_text_job(f.eid, {**repair_body, 'requestKey': f.key(), 'repairOf': repair['id']})
                self.assertEqual(adapter.calls, 2)
                state = f.s.story_state(f.eid)
                self.assertEqual(state['revisions']['entities'], [])
                self.assertEqual(f.s.entities(f.pid)['entities'], [])

    def test_o04_real_process_kill_at_four_business_dispatch_boundaries(self):
        # Real SIGKILL and SQLite recovery; durable fake upstream receipts are
        # a controlled transport oracle, not evidence of a DGX model request.
        f = self.f
        child_code = r'''
import json,sys,time,hashlib
from pathlib import Path
from DGXGateway.dgx_gateway.drama.service import DramaService
base,repo,job,stage,marker,receipt_path,count_path,raw_path=sys.argv[1:]
marker,receipt_path,count_path,raw_path=map(Path,(marker,receipt_path,count_path,raw_path))
def pause():
 marker.write_text(stage)
 while True: time.sleep(.05)
class Adapter:
 def capabilities(self):return {'generation_qualified':True,'simulation':True}
 def submit(self,kind,inputs,key):
  if stage=='after_intent':pause()
  count_path.write_text(str(int(count_path.read_text())+1))
  raw=raw_path.read_text()
  value={'job_id':hashlib.sha256(key.encode()).hexdigest()[:32],'idempotency_key':key,'task_kind':kind,'state':'SUCCEEDED','resource_status':'released','output_text':raw,'output_sha256':hashlib.sha256(raw.encode()).hexdigest()}
  receipt_path.write_text(json.dumps(value))
  if stage=='after_post':pause()
  return value
s=DramaService(Path(base),Path(repo),text_adapter=Adapter())
if stage=='before_intent':pause()
if stage=='after_receipt':s.record_text_receipt=lambda *args:pause()
s.dispatch_text_job(job)
'''
        for stage in ('before_intent', 'after_intent', 'after_post', 'after_receipt'):
            with self.subTest(stage=stage):
                adapter, body = f.text_setup()
                body['requestKey'] = f.key()
                job = f.s.create_text_job(f.eid, body)['job']
                base, repo = f.s.root.parent, f.s.repo_root
                marker, receipt_path, count_path, raw_path = [base / (stage + suffix) for suffix in ('.marker', '.receipt', '.count', '.raw')]
                count_path.write_text('0'); raw_path.write_text(adapter.output)
                f.s.close()
                child = subprocess.Popen([sys.executable, '-c', child_code, str(base), str(repo), job['id'], stage,
                                          str(marker), str(receipt_path), str(count_path), str(raw_path)],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 8
                    while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertTrue(marker.exists(), 'Child must reach the exact frozen kill boundary')
                    child.kill(); child.communicate(timeout=5)
                finally:
                    if child.poll() is None:
                        child.kill(); child.communicate(timeout=5)
                class ReadOnlyRecoveryAdapter:
                    def capabilities(self): return {'generation_qualified': True, 'simulation': True}
                    def find(self, key):
                        return json.loads(receipt_path.read_text()) if receipt_path.exists() else None
                    def get(self, jid): return self.find(None)
                    def submit(self, *args):
                        raise AssertionError('Recovery must not re-POST an ambiguous intent')
                f.s = DramaService(base, repo, text_adapter=ReadOnlyRecoveryAdapter())
                current = f.s.text_job(job['id'])['job']
                if stage == 'before_intent':
                    self.assertEqual(current['state'], 'QUEUED')
                    self.assertEqual(current['attempts'], [])
                    self.assertEqual(count_path.read_text(), '0')
                    f.s.text_adapter = adapter
                    self.assertEqual(f.s.dispatch_text_job(job['id'])['job']['outputStatus'], 'valid_draft')
                    self.assertEqual(adapter.calls, 1)
                else:
                    self.assertEqual(current['state'], 'SUBMISSION_UNKNOWN')
                    self.assertEqual(len(current['attempts']), 1)
                    f.s.dispatch_text_job(job['id'])
                    final = f.s.reconcile_text_job(job['id'])['job']
                    if stage == 'after_intent':
                        self.assertEqual(final['state'], 'SUBMISSION_UNKNOWN')
                        self.assertEqual(count_path.read_text(), '0')
                    else:
                        self.assertEqual(final['outputStatus'], 'valid_draft')
                        self.assertEqual(count_path.read_text(), '1')
                        f.s.reconcile_text_job(job['id'])
                    self.assertEqual(len(final['attempts']), 1)
                self.assertEqual(f.s.story_state(f.eid)['revisions']['entities'], [])
