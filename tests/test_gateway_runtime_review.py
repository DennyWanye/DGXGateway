"""Independent FU05 review: source attribution and release authorization boundaries."""
from email.message import Message
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from DGXGateway.dgx_gateway.runtime_view import runtime_view
from DGXGateway.dgx_gateway.server import GatewayHandler
from tests import test_model_residency as fixtures


class RuntimeAttributionReviewTests(unittest.TestCase):
    def test_foreign_lifecycle_invalidates_idle_residency_attribution(self):
        snapshot={'active_local_model':{'state':'ACTIVE','model_id':'minimax-h3'},
            'current_operation':None,
            'latest_operation':{'operation_id':'foreign-operation','phase':'RELEASED','outcome':'COMMITTED'},
            'residency':{'state':'IDLE','model_id':'minimax-h3','owner_job_id':'a'*32,'start_operation_id':'old-start'}}
        view=runtime_view(snapshot)
        self.assertEqual(view['phase'],'UNKNOWN')
        self.assertNotEqual(view['origin']['kind'],'automatic_job')

    def test_active_foreign_stop_is_not_reported_as_idle_automatic_start(self):
        snapshot={'active_local_model':{'state':'ACTIVE','model_id':'minimax-h3'},
            'current_operation':{'operation_id':'foreign-stop','phase':'SOURCE_DRAINING'},
            'residency':{'state':'IDLE','model_id':'minimax-h3','owner_job_id':'a'*32,'start_operation_id':'old-start'}}
        view=runtime_view(snapshot)
        self.assertNotEqual(view['phase'],'IDLE')
        self.assertNotEqual(view['origin']['kind'],'automatic_job')

    def request(self, body, *, auth=True, origin='http://trusted.test', release=None):
        handler=GatewayHandler.__new__(GatewayHandler)
        handler.path='/api/v1/residency/release';handler.command='POST'
        encoded=json.dumps(body).encode();headers=Message()
        headers['Content-Length']=str(len(encoded));headers['Content-Type']='application/json'
        if origin:headers['Origin']=origin
        handler.headers=headers;handler.rfile=io.BytesIO(encoded)
        release=release or Mock(return_value={'state':'NONE'})
        handler.server=SimpleNamespace(application=SimpleNamespace(
            config=SimpleNamespace(allowed_origins=['http://trusted.test'],lan_no_auth=False),
            authorized=lambda _:auth,residency=SimpleNamespace(release=release)))
        handler._json=Mock();handler.do_POST()
        return handler._json.call_args.args[0],release

    def test_release_auth_and_origin_required_before_mutation(self):
        for auth,origin in ((False,'http://trusted.test'),(True,'http://evil.test'),(True,None)):
            with self.subTest(auth=auth,origin=origin):
                status,release=self.request({'owner_job_id':'a'*32},auth=auth,origin=origin)
                self.assertEqual(status,403);release.assert_not_called()

    def test_release_exact_body_and_stale_owner_conflict(self):
        for body in ({},{'owner_job_id':None},{'owner_job_id':'a'*32,'target_model':'minimax-h3'}):
            status,release=self.request(body);self.assertEqual(status,422);release.assert_not_called()
        release=Mock(side_effect=BlockingIOError('Retained model owner changed'))
        status,release=self.request({'owner_job_id':'a'*32},release=release)
        self.assertEqual(status,409);release.assert_called_once_with('a'*32)

    def test_expired_owner_cannot_release_new_claimed_task_real_controller(self):
        f=fixtures.ResidencyTests();f.setUp()
        try:
            a=f.row('a');f.start(a);f.finish(a)
            b=f.row('b');f.start(b)
            status,_=self.request({'owner_job_id':a['job_id']},release=f.r.release)
            self.assertEqual(status,409)
            self.assertEqual(f.h3.state,'HEALTHY')
        finally:f.doCleanups()
