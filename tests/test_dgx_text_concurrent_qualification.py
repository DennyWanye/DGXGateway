"""Isolated CPU qualification oracles. No model, SSH, deployment or real PASS."""
import copy
import errno
import hashlib
import http.client
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from DGXGateway.dgx_gateway.text_jobs import MODEL, SERVED_MODEL, FixedTextHTTP, InvalidTextResponse, SubmissionUnknown
from DGXGateway.dgx_gateway.text_store import TextFenced
from DGXGateway.dgx_gateway.text_concurrent_qualification import ConcurrentProbeTransport, RuntimeProbe, digest, validate_concurrent_evidence


def complete_fixture_proof(row):
    """Synthetic signed-artifact validator fixture, never a qualification run."""
    row.setdefault('task_kind','draft_script');row.setdefault('inputs',{})
    identity={'nodes':{'head':{'id':'a'},'worker':{'id':'b'}},'gateway_container_id':'c'}
    row.update(owner_id='fixture',lease_epoch=1,lease_generation='generation',attempt_id='attempt',runtime_identity=identity)
    fence={'runtime_identity':identity,'lease_generation':'generation','admission_closed':True,'inflight_zero':True,'receipt_sha256':'f'*64}
    return dict(schema='dgx-text-concurrent-v1',simulation=False,status='PASS',**{k:row[k] for k in ('job_id','owner_id','lease_epoch','lease_generation','attempt_id','input_hash','output_sha256')},
        resource_operation_id='text-'+row['job_id'],runtime_sha256=digest(identity),payload_sha256=digest(FixedTextHTTP.payload(None,row['task_kind'],row['inputs'])),baseline_idle=True,final_idle=True,fence_proof=fence,
        sent_at=1,accepted={'observed_at':2,'metrics':[1,0],'client_pending':True},fence_started_at=3,
        overlap={'started_at':4,'observed_at':5,'metrics':[1,0],'client_pending':True,'connection_refused':True},completed_at=6,fence_completed_at=7)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.identity={'nodes':{'head':{'id':'a'},'worker':{'id':'b'}},'gateway_container_id':'c'}
        self.row={'job_id':'a'*32,'owner_id':'owner','lease_epoch':1,'lease_generation':'gen','attempt_id':'attempt','input_hash':'i',
                  'dispatch_state':'SENT','runtime_identity':self.identity}
        self.epoch=1; self.closed=False; self.active=False; self.calls=0; self.release=threading.Event();self.done=threading.Event()
        self.context=SimpleNamespace(operation_id='text-'+self.row['job_id'],lease_generation='gen',guard=self.guard)
        self.quarantine=[]
        self.jobs=SimpleNamespace(persistence=SimpleNamespace(get=lambda jid:copy.deepcopy(self.row),archive=lambda jid,owner,epoch,attempt,changes:self.update(None,changes),quarantine=lambda *args:self.quarantine.append(args)),_context=lambda *a:self.context,
                                 _leases={self.row['job_id']:None},_update=self.update,_fence=self.fence)
        self.transport=SimpleNamespace(simulation=True,generate=self.generate,payload=lambda *a:{'model':MODEL})
        self.driver=SimpleNamespace(simulation=True,metrics=lambda:[int(self.active),0],identity=self.get_identity,refused=lambda:self.closed)
        self.probe=ConcurrentProbeTransport(self.jobs,self.transport,self.row['job_id'],driver=self.driver,observe_timeout=.1,interval=.002)
    def guard(self):
        if self.epoch!=1: raise TextFenced('epoch replaced')
    def update(self,row,changes,*args,**kw): self.guard();self.row.update(changes)
    def get_identity(self,closed): return dict(self.identity,gateway_container_id=None) if closed else self.identity
    def generate(self,payload,guard,timeout,sent_callback):
        self.calls+=1;sent_callback();self.active=True
        try:
            if not self.release.wait(timeout):raise SubmissionUnknown('fixture lost response')
            guard();return '{}'
        finally:self.active=False;self.done.set()
    def fence(self,row,context):
        context.guard();self.closed=True
        timer=threading.Timer(.06,self.release.set);timer.start()
        self.done.wait(.5);timer.join();context.guard()
        return {'runtime_identity':self.identity,'lease_generation':'gen','admission_closed':True,'inflight_zero':True,'receipt_sha256':'r'*64}
    def tearDown(self): self.release.set()
    def test_simulated_overlap_never_produces_production_pass(self):
        self.assertEqual(self.probe.generate({},self.guard,timeout=1),'{}')
        proof=self.row['external_concurrent_proof'];self.assertEqual(proof['status'],'SIMULATED_PASS');self.assertTrue(proof['overlap']['client_pending']);self.assertEqual(self.calls,1)
        with self.assertRaises(RuntimeError):validate_concurrent_evidence(self.row,digest(proof))
    def test_fast_request_is_not_qualified_and_is_never_retried(self):
        self.release.set();self.assertEqual(self.probe.generate({},self.guard,timeout=1),'{}')
        self.assertEqual(self.row['external_concurrent_proof']['status'],'NOT_QUALIFIED');self.assertFalse(self.closed);self.assertEqual(self.calls,1)
    def test_running_before_refusal_but_zero_after_refusal_is_not_overlap(self):
        self.driver.metrics=lambda:[int(self.active and not self.closed),0]
        self.probe.generate({},self.guard,timeout=1)
        self.assertEqual(self.row['external_concurrent_proof']['status'],'NOT_QUALIFIED');self.assertEqual(self.calls,1)
    def test_request_completing_during_refusal_sample_is_not_overlap(self):
        def refusal():
            if self.closed:self.release.set();self.done.wait(.5)
            return self.closed
        self.driver.refused=refusal;self.probe.generate({},self.guard,timeout=1)
        self.assertEqual(self.row['external_concurrent_proof']['status'],'NOT_QUALIFIED')
    def test_changed_instance_after_fence_is_not_qualified(self):
        self.driver.identity=lambda closed: {'nodes':{}} if closed else self.identity
        with self.assertRaises(TextFenced):self.probe.generate({},self.guard,timeout=1)
        self.assertEqual(self.row['external_concurrent_proof']['status'],'NOT_QUALIFIED');self.assertEqual(self.row['output_text'],'{}')
    def test_expired_epoch_cannot_write_late_gate_evidence(self):
        original=self.fence
        def fence(*args):
            result=original(*args);self.epoch=2;return result
        self.jobs._fence=fence
        with self.assertRaises(TextFenced):self.probe.generate({},self.guard,timeout=1)
        self.assertNotIn('external_concurrent_proof',self.row);self.assertEqual(self.calls,1);self.assertEqual(len(self.quarantine),1)
    def test_original_unknown_remains_unknown_no_second_http(self):
        self.jobs._fence=lambda *args: (time.sleep(.15) or {})
        with self.assertRaises(SubmissionUnknown):self.probe.generate({},self.guard,timeout=.1)
        self.assertEqual(self.calls,1);self.assertEqual(self.row['external_concurrent_proof']['status'],'NOT_QUALIFIED')
    def test_complete_invalid_http_response_is_left_for_normal_draft_archive(self):
        def invalid(payload,guard,timeout,sent_callback):
            sent_callback();raise InvalidTextResponse('{"bad":"wrapper"}','fixture invalid model')
        self.transport.generate=invalid
        with self.assertRaises(InvalidTextResponse):self.probe.generate({},self.guard,timeout=1)
        self.assertNotIn('output_text',self.row)
    def test_wrong_final_metrics_or_admission_refuses_proof(self):
        for value in ('metrics','admission'):
            with self.subTest(value=value):
                self.setUp()
                if value=='metrics':self.driver.metrics=lambda:[1,0] if self.closed else [int(self.active),0]
                else:self.driver.refused=lambda:False
                with self.assertRaises(TextFenced):self.probe.generate({},self.guard,timeout=1)
                self.assertEqual(self.calls,1)


class EvidenceTests(unittest.TestCase):
    def test_binding_and_overlap_cannot_be_replaced_by_a_signed_pass_label(self):
        row={'job_id':'a'*32,'input_hash':'i','output_sha256':'o'};proof=complete_fixture_proof(row);row['external_concurrent_proof']=proof
        validate_concurrent_evidence(row,digest(proof))
        for field,value in [('attempt_id','other'),('lease_generation','other'),('lease_epoch',2),('owner_id','other'),('runtime_sha256','other'),('simulation',True),('output_sha256','other')]:
            changed=copy.deepcopy(row);changed['external_concurrent_proof'][field]=value
            with self.subTest(field=field),self.assertRaises(RuntimeError):validate_concurrent_evidence(changed,digest(changed['external_concurrent_proof']))
        changed=copy.deepcopy(row);changed['external_concurrent_proof']['overlap']['observed_at']=8
        with self.assertRaises(RuntimeError):validate_concurrent_evidence(changed,digest(changed['external_concurrent_proof']))
    def test_only_connection_refused_qualifies_not_timeout_reset_or_unreachable(self):
        probe=RuntimeProbe(None,SimpleNamespace(host='127.0.0.1'))
        with patch('socket.create_connection',side_effect=ConnectionRefusedError(errno.ECONNREFUSED,'refused')):self.assertTrue(probe.refused())
        for error in (TimeoutError(),ConnectionResetError(errno.ECONNRESET,'reset'),OSError(errno.ENETUNREACH,'unreachable')):
            with patch('socket.create_connection',side_effect=error),self.assertRaises(TextFenced):probe.refused()
    def test_nonfinite_metrics_refused(self):
        for values in ((float('nan'),0),(float('inf'),0),(-1,0)):
            with self.subTest(values=values),self.assertRaises(TextFenced):RuntimeProbe(SimpleNamespace(_request_metrics=lambda:values),None).metrics()


class LocalHTTPTests(ProbeTests):
    # Execute just this concrete local HTTP gate test, not inherited test copies.
    def test_actual_local_socket_accept_response_and_refusal_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            secret=Path(directory)/'secret';secret.write_text('q'*40);secret.chmod(0o600)
            fixture=self; auth=[]
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*args):pass
                def do_POST(self):
                    auth.append(self.headers.get('Authorization')=='Bearer '+'q'*40)
                    self.rfile.read(int(self.headers['Content-Length']));fixture.active=True;fixture.calls+=1
                    fixture.release.wait(1)
                    body=json.dumps({'model':SERVED_MODEL,'choices':[{'finish_reason':'stop','message':{'content':'{}'}}]}).encode()
                    self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);self.wfile.flush()
                    fixture.active=False;fixture.done.set()
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
            serving=threading.Thread(target=lambda:server.serve_forever(poll_interval=.002),daemon=True);serving.start()
            config=SimpleNamespace(nodes=[SimpleNamespace(role='head',address='127.0.0.1')],deepseek_gateway_secret=secret)
            def connection(host,port,timeout):
                self.assertEqual((host,port),('127.0.0.1',11434))
                return http.client.HTTPConnection(host,server.server_port,timeout=timeout)
            transport=FixedTextHTTP(config,connection_factory=connection)
            def refused():
                try:
                    with socket.create_connection(('127.0.0.1',server.server_port),timeout=.1):return False
                except ConnectionRefusedError:return True
            self.driver.refused=refused
            def fence(row,context):
                context.guard();server.shutdown();server.server_close();self.closed=True
                timer=threading.Timer(.08,self.release.set);timer.start();self.done.wait(1);timer.join()
                return {'runtime_identity':self.identity,'lease_generation':'gen','admission_closed':True,'inflight_zero':True,'receipt_sha256':'r'*64}
            self.jobs._fence=fence
            probe=ConcurrentProbeTransport(self.jobs,transport,self.row['job_id'],driver=self.driver,observe_timeout=.5,interval=.002)
            try:
                self.assertEqual(probe.generate({},self.guard,timeout=2),'{}')
                self.assertEqual(self.calls,1);self.assertEqual(auth,[True]);self.assertEqual(self.row['external_concurrent_proof']['status'],'SIMULATED_PASS')
            finally:self.release.set();server.shutdown();server.server_close();serving.join(1)

# unittest treats inherited methods as tests; keep only the distinct real-HTTP case.
for _name in ProbeTests.__dict__:
    if _name.startswith('test_'):setattr(LocalHTTPTests,_name,None)


class LeaseIntegrationTests(unittest.TestCase):
    from tests.test_dgx_gateway_text_jobs import TextJobTests as Fixture
    setUp=Fixture.setUp;lease=Fixture.lease;submit=Fixture.submit
    def run_probe(self,fail_fence=False):
        identity={'nodes':{'head':{'id':'a'},'worker':{'id':'b'}},'gateway_container_id':'c'}
        release=threading.Event();done=threading.Event();state={'running':False,'closed':False,'calls':0}
        def generate(payload,guard,timeout,sent_callback):
            state['calls']+=1;sent_callback();state['running']=True
            release.wait(2);guard();state['running']=False;done.set();return '{}'
        self.transport.generate=generate
        self.fence.capture=lambda context:identity
        def drain(context,expected):
            context.guard();state['closed']=True
            timer=threading.Timer(.08,release.set);timer.start();done.wait(2);timer.join();context.guard()
            if fail_fence:raise TextFenced('fixture fence receipt rejected')
            return {'runtime_identity':identity,'lease_generation':context.lease_generation,'admission_closed':True,'inflight_zero':True,'receipt_sha256':'f'*64}
        self.fence.drain=drain
        driver=SimpleNamespace(simulation=True,metrics=lambda:[int(state['running']),0],refused=lambda:state['closed'],
            identity=lambda closed:dict(identity,gateway_container_id=None) if closed else identity)
        row=self.submit();self.jobs.transport=ConcurrentProbeTransport(self.jobs,self.transport,row['job_id'],driver=driver,interval=.002)
        try:self.jobs.run(row['job_id'])
        finally:release.set()
        return self.jobs.persistence.get(row['job_id']),state
    def test_real_parent_lease_and_sqlite_release_with_simulated_runtime(self):
        row,state=self.run_probe();self.assertEqual(row['state'],'SUCCEEDED',row);self.assertTrue(row['memory_released'])
        self.assertEqual(row['external_concurrent_proof']['status'],'SIMULATED_PASS');self.assertEqual(state['calls'],1)
        self.assertEqual(self.adapter.calls,['start','stop']);self.assertFalse(self.app.config.lock_root.exists())
        attempt=self.store._db.execute('SELECT state FROM text_attempts WHERE attempt_id=?',(row['attempt_id'],)).fetchone()
        self.assertEqual(attempt[0],'RESULT_ARCHIVED')
    def test_failed_fence_keeps_complete_output_and_parent_recovery_obligation(self):
        row,state=self.run_probe(fail_fence=True)
        self.assertEqual(row['state'],'RECOVERY_REQUIRED');self.assertEqual(row['output_text'],'{}');self.assertEqual(row['output_status'],'valid_draft')
        self.assertTrue(row['cleanup_required']);self.assertFalse(row['memory_released']);self.assertEqual(state['calls'],1)
        self.assertEqual(self.adapter.calls,['start']);self.assertTrue(self.app.config.lock_root.exists())

if __name__=='__main__':unittest.main()
