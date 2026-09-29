"""Independent service oracle: real persistence/leases and FixedTextHTTP, fake IO."""
import json
from types import SimpleNamespace
import unittest

from tests import test_dgx_gateway_text_jobs as fixtures
from DGXGateway.dgx_gateway.text_jobs import FixedTextHTTP, TextJobs


class TextDiagnosticReviewTests(unittest.TestCase):
    def test_unknown_safe_diagnostic_is_durable_public_and_never_replayed(self):
        for status in (404, 200):
            with self.subTest(status=status):
                fixture = fixtures.TextJobTests()
                fixture.setUp()
                try:
                    secret = fixture.root / 'http-secret'
                    token = 'private-credential-sentinel-' + 'x' * 40
                    secret.write_text(token)
                    secret.chmod(0o600)
                    requests = []
                    alias = json.loads((fixtures.REPO / 'manifests/deepseek-v4-flash-consumers.json').read_text())['model']

                    class Connection:
                        def __init__(self, *args, **kwargs):
                            self.sock = SimpleNamespace(settimeout=lambda _: None)
                        def connect(self): pass
                        def close(self): pass
                        def request(self, method, path, body, headers):
                            requests.append(json.loads(body)['model'])
                        def getresponse(self): return self
                        def read1(self, _):
                            if status == 404:
                                raise AssertionError('Rejected private response body must not be read')
                            raise OSError('private-response-and-exception-sentinel')

                    Connection.status = status
                    transport = FixedTextHTTP(SimpleNamespace(
                        nodes=[SimpleNamespace(role='head', address='192.0.2.1')],
                        deepseek_gateway_secret=secret), connection_factory=Connection)
                    transport.qualified = True
                    fixture.jobs.transport = transport
                    row = fixture.submit(inputs={'source': 'private-input-sentinel'})
                    job_id = row['job_id']
                    fixture.jobs.run(job_id)
                    expected = {'phase': 'response_status' if status == 404 else 'response_body',
                                'http_status': status,
                                'exception_type': 'ValueError' if status == 404 else 'OSError'}
                    public = fixture.jobs.get(job_id)
                    self.assertEqual(public['state'], 'SUBMISSION_UNKNOWN')
                    self.assertEqual(public['failure_diagnostic'], expected)
                    self.assertEqual(public['resource_status'], 'recovery_required')
                    self.assertTrue(public['cleanup_required'])
                    self.assertFalse(public['memory_released'])
                    self.assertEqual(requests, [alias])
                    self.assertNotIn('private-', json.dumps(public))
                    self.assertEqual(fixture.adapter.calls, ['start'])
                    saved = json.loads(fixture.store._db.execute(
                        'SELECT payload FROM text_jobs WHERE job_id=?', (job_id,)).fetchone()[0])
                    self.assertEqual(saved['failure_diagnostic'], expected)
                    attempts = list(fixture.store._db.execute(
                        'SELECT * FROM text_attempts WHERE job_id=?', (job_id,)))
                    self.assertEqual(len(attempts), 1)
                    reopened = TextJobs(fixture.app, transport=transport, fence=fixture.fence,
                                        allow_generation=True, autostart=False,
                                        clock=lambda: fixture.now[0])
                    self.assertEqual(reopened.get(job_id)['failure_diagnostic'], expected)
                    self.assertEqual(reopened.find('text-test-key')['job_id'], job_id)
                    self.assertEqual(reopened.submit('draft_script', {'source': 'private-input-sentinel'},
                                                     'text-test-key')['job_id'], job_id)
                    reopened.run(job_id)  # A direct accidental run cannot claim an old UNKNOWN.
                    self.assertEqual(requests, [alias])
                    self.assertEqual(list(fixture.store._db.execute(
                        'SELECT * FROM text_attempts WHERE job_id=?', (job_id,))), attempts)
                    self.assertEqual(fixture.store._db.execute(
                        "SELECT COUNT(*) FROM text_events WHERE job_id=? AND kind='dispatch-sent'",
                        (job_id,)).fetchone()[0], 1)
                finally:
                    fixture.doCleanups()
