"""Real Gateway localhost routes plus actual remote Drama text adapter, no model."""
import json
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tests import test_dgx_gateway_text_jobs as fixtures
from DGXGateway.dgx_gateway.server import GatewayHTTPServer
from DGXGateway.dgx_gateway.drama import gateway_text_adapter


class TextOriginReviewTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.TextJobTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.server = GatewayHTTPServer(('127.0.0.1', 0), self.f.app)
        self.origin = 'http://127.0.0.1:' + str(self.server.server_port)
        self.f.app.config.allowed_origins = (self.origin,)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), thread.join(5)))
        patcher = patch.object(gateway_text_adapter, 'RPC_ORIGIN', self.origin)
        patcher.start(); self.addCleanup(patcher.stop)
        self.adapter = gateway_text_adapter.GatewayTextAdapter(timeout=3)

    def test_real_route_adapter_submit_and_same_key_find_work_without_starting_model(self):
        self.assertTrue(self.adapter.capabilities()['generation_qualified'])
        row = self.adapter.submit('draft_script', {'source': 'isolated local source'}, 'origin-submit')
        self.assertEqual(row['state'], 'QUEUED')
        self.assertEqual(self.adapter.find('origin-submit')['job_id'], row['job_id'])
        self.assertEqual(self.adapter.get(row['job_id'])['job_id'], row['job_id'])
        self.assertEqual(self.adapter.submit('draft_script', {'source': 'isolated local source'}, 'origin-submit')['job_id'], row['job_id'])
        self.assertEqual(len(self.f.jobs.list()), 1)
        self.assertEqual(self.f.transport.calls, 0)
        self.assertEqual(self.f.adapter.calls, [])

    def test_complete_busy_http_response_is_known_not_sent(self):
        from DGXGateway.dgx_gateway.text_jobs import TextJobError
        with patch.object(self.f.app.text_jobs, 'submit', side_effect=TextJobError('TEXT_BUSY', 'occupied', 409)):
            with self.assertRaises(gateway_text_adapter.GatewayTextBusy) as error:
                self.adapter.submit('draft_script', {'source': 'fixture'}, 'busy')
        self.assertEqual(error.exception.evidence['status'], 409)
        self.assertEqual(error.exception.evidence['body']['error']['code'], 'TEXT_BUSY')
        self.assertIsNone(self.adapter.find('busy'))
        self.assertEqual(self.f.transport.calls, 0)

    def test_real_route_adapter_cancel_has_fixed_origin_but_missing_or_foreign_origin_stays_denied(self):
        row = self.f.jobs.submit('draft_script', {'source': 'queued fixture'}, 'origin-cancel')
        raw = json.dumps({'task_kind': 'draft_script', 'inputs': {'source': 'forbidden'}, 'idempotency_key': 'bad-origin'}).encode()
        for origin in (None, 'http://foreign.invalid'):
            headers = {'Content-Type': 'application/json'}
            if origin:
                headers['Origin'] = origin
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(self.origin + '/api/v1/text-jobs', data=raw, headers=headers), timeout=3)
            self.assertEqual(error.exception.code, 403)
            self.assertEqual(json.load(error.exception)['error']['code'], 'FORBIDDEN')
            error.exception.close()
        canceled = self.adapter.cancel(row['job_id'])
        self.assertEqual(canceled['state'], 'CANCELED')
        self.assertEqual(len(self.f.jobs.list()), 1)
        self.assertEqual(self.f.transport.calls, 0)
        self.assertEqual(self.f.adapter.calls, [])
