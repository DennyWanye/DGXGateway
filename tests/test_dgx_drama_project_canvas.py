"""Canvas changes preserve project identity and reject stale/unsafe edits."""
from pathlib import Path
import tempfile
import unittest
from DGXGateway.dgx_gateway.drama.service import DramaService
from DGXGateway.dgx_gateway.drama.contracts import DramaError

class ProjectCanvasTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = DramaService(Path(self.temp.name), Path(self.temp.name))
        self.project = self.service.create_project({'title':'MV','width':1080,'height':1920,'requestKey':'create'})['project']
    def tearDown(self):
        self.service.close()
        self.temp.cleanup()
    def test_idempotent_canvas_preserves_identity_and_detects_stale_revision(self):
        body={'width':1920,'height':1080,'expectedRevision':1,'requestKey':'landscape'}
        result=self.service.update_project_canvas(self.project['id'],body)
        self.assertEqual(result,self.service.update_project_canvas(self.project['id'],body))
        self.assertEqual(result['project'],{**self.project,'width':1920,'height':1080,'revision':2})
        with self.assertRaises(DramaError) as error:
            self.service.update_project_canvas(self.project['id'],{**body,'requestKey':'stale'})
        self.assertEqual(error.exception.code,'revision_conflict')
    def test_invalid_size_does_not_change_project(self):
        with self.assertRaises(DramaError):
            self.service.update_project_canvas(self.project['id'],{'width':1081,'height':1920,'expectedRevision':1,'requestKey':'invalid'})
        self.assertEqual(self.service.project(self.project['id'])['project'],self.project)
    def test_existing_timeline_blocks_canvas_change(self):
        eid=self.service.create_episode(self.project['id'],{'title':'episode','requestKey':'episode'})['episode']['id']
        with self.service.store.connect(write=True) as db:
            db.execute('INSERT INTO dr_timeline_revisions VALUES (?,?,?,?,?)',('a'*32,eid,1,'{}',0))
        with self.assertRaises(DramaError) as error:
            self.service.update_project_canvas(self.project['id'],{'width':1920,'height':1080,'expectedRevision':1,'requestKey':'with-timeline'})
        self.assertEqual(error.exception.code,'canvas_has_timeline')
        self.assertEqual(self.service.project(self.project['id'])['project'],self.project)
