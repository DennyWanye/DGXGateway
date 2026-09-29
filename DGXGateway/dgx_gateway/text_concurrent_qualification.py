"""Explicit QA only: one ledgered HTTP attempt overlaps a parent-owned fence.

The monitor opens TCP only; it never submits a second inference. All mutations
remain on the parent worker, with its existing epoch guard and lease heartbeat.
"""
from __future__ import annotations
import errno
import hashlib
import math
import socket
import threading
import time
from .text_jobs import MODEL, FixedTextHTTP, InvalidTextResponse, SubmissionUnknown, canonical, strict_json
from .text_store import TextFenced


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class RuntimeProbe:
    simulation = False
    def __init__(self, adapter, transport):
        self.adapter, self.transport = adapter, transport
    def metrics(self):
        values = self.adapter._request_metrics()
        if len(values) != 2 or any(not math.isfinite(x) or x < 0 for x in values):
            raise TextFenced('Concurrent QA metrics are invalid')
        return list(values)
    def identity(self, *, closed):
        return self.adapter._text_runtime_identity(admission_open=False if closed else True)
    def refused(self):
        try:
            with socket.create_connection((self.transport.host, 11434), timeout=1): return False
        except OSError as exc:
            # A timeout, routing error or reset is not proof of closed admission.
            if exc.errno == errno.ECONNREFUSED: return True
            raise TextFenced('Concurrent QA admission check is inconclusive') from exc


class ConcurrentProbeTransport:
    """Trusted runner wrapper, never exposed through HTTP/MCP/config inputs."""
    qualified = True
    def __init__(self, jobs, transport, job_id, *, driver=None, observe_timeout=30, interval=.05):
        self.jobs, self.transport, self.job_id = jobs, transport, job_id
        self.driver = driver or RuntimeProbe(jobs.app.operations.adapters[MODEL], transport)
        self.simulation = bool(getattr(self.driver, 'simulation', True) or getattr(transport, 'simulation', True))
        self.observe_timeout, self.interval = observe_timeout, interval
    def payload(self, kind, inputs): return self.transport.payload(kind, inputs)
    def generate(self, payload, guard, timeout=240):
        jobs = self.jobs
        row = jobs.persistence.get(self.job_id)
        context = jobs._context(self.job_id, row, jobs._leases[self.job_id])
        identity = row['runtime_identity']
        if row['dispatch_state'] != 'SENT' or not row['attempt_id'] or not identity:
            raise TextFenced('Concurrent QA requires the exact durable SENT attempt')
        evidence = {'schema':'dgx-text-concurrent-v1', 'simulation':self.simulation, 'status':'NOT_QUALIFIED',
                    'job_id':self.job_id, 'owner_id':row['owner_id'], 'lease_epoch':row['lease_epoch'],
                    'lease_generation':row['lease_generation'], 'attempt_id':row['attempt_id'],
                    'resource_operation_id':context.operation_id, 'runtime_sha256':digest(identity),
                    'input_hash':row['input_hash'], 'payload_sha256':digest(payload), 'created_at':time.time()}
        def save():
            guard()
            jobs._update(row, {'external_concurrent_proof':dict(evidence)}, 'external-concurrent-qa', recovery=True)
        guard()
        if self.driver.identity(closed=False) != identity or self.driver.metrics() != [0,0]:
            evidence['reason']='Baseline identity or idle metrics mismatch'; save()
            raise TextFenced(evidence['reason'])
        evidence['baseline_idle']=True
        sent, done, monitor_stop = threading.Event(), threading.Event(), threading.Event()
        result, witness, monitor_errors = {}, {}, []
        deadline = time.monotonic() + timeout
        def on_sent():
            evidence['sent_at']=time.monotonic(); sent.set()
        def client():
            try: result['output']=self.transport.generate(payload, guard, timeout=timeout, sent_callback=on_sent)
            except BaseException as exc: result['error']=exc
            finally:
                result['completed_at']=time.monotonic(); done.set()
        request_thread = threading.Thread(target=client, name='text-qualification-http', daemon=True)
        request_thread.start()
        def completed_output():
            if not done.wait(max(0,deadline-time.monotonic()) + .2):
                raise SubmissionUnknown('Concurrent QA original HTTP deadline exceeded; never retry')
            guard()
            if 'error' in result: raise result['error']
            evidence['completed_at']=result['completed_at']
            evidence['output_sha256']=hashlib.sha256(result['output'].encode()).hexdigest()
            return result['output']
        def monitor():
            try:
                while not monitor_stop.is_set() and not done.is_set():
                    guard(); start=time.monotonic()
                    if sent.is_set() and self.driver.refused() and not done.is_set():
                        metrics=self.driver.metrics()
                        # Sandwich the metric sample with the same client pending;
                        # no sequential "refused later, running earlier" shortcut.
                        if metrics[0] > 0 and not done.is_set() and not monitor_stop.is_set():
                            witness.update({'started_at':start,'observed_at':time.monotonic(),
                                            'metrics':metrics,'client_pending':True,'connection_refused':True})
                            return
                    monitor_stop.wait(self.interval)
            except BaseException as exc: monitor_errors.append(exc)
        monitor_thread = None; failed=False; failure_exception=None
        try:
            observation_deadline=min(deadline, time.monotonic()+self.observe_timeout)
            while time.monotonic() < observation_deadline and not done.is_set():
                guard()
                if sent.is_set():
                    metrics=self.driver.metrics()
                    if metrics[0] > 0 and not done.is_set():
                        if self.driver.identity(closed=False) != identity: raise TextFenced('Concurrent QA instance changed')
                        if not done.is_set():
                            evidence['accepted']={'observed_at':time.monotonic(),'metrics':metrics,'client_pending':True}
                            break
                done.wait(self.interval)
            if 'accepted' not in evidence:
                output=completed_output()
                evidence['reason']='Original request completed without a witnessed in-flight fence window'
                save(); return output
            monitor_thread=threading.Thread(target=monitor,name='text-qualification-admission',daemon=True)
            evidence['fence_started_at']=time.monotonic()
            monitor_thread.start()
            # Synchronous existing controller: no detached lifecycle mutation.
            proof=jobs._fence(row,context)
            evidence['fence_completed_at']=time.monotonic()
            monitor_stop.set(); monitor_thread.join(timeout=20)
            output=completed_output()
            guard()
            if self.driver.identity(closed=True) != dict(identity,gateway_container_id=None):
                raise TextFenced('Concurrent QA post-fence strong runtime identity changed')
            if self.driver.metrics() != [0,0] or not self.driver.refused():
                raise TextFenced('Concurrent QA final admission/idle evidence missing')
            evidence['final_idle']=True; evidence['fence_proof']=proof
            if witness and not monitor_errors and not monitor_thread.is_alive():
                evidence['overlap']=dict(witness)
                evidence['status']='SIMULATED_PASS' if self.simulation else 'PASS'
            else: evidence['reason']='No overlapping refusal plus original pending request and running metric was witnessed'
            save(); return output
        except BaseException as exc:
            failed=True; failure_exception=exc
            evidence['reason']=type(exc).__name__ + ': ' + str(exc)[:240]
            try: save()
            except TextFenced: pass  # A new epoch alone may write subsequent evidence.
            raise
        finally:
            monitor_stop.set()
            if monitor_thread: monitor_thread.join(timeout=20)
            # FixedTextHTTP has its own hard socket shutdown deadline. Never
            # release resources merely because a background client is delayed.
            if not done.is_set(): done.wait(max(0,deadline-time.monotonic()) + .2)
            if failed and done.is_set() and not isinstance(failure_exception,InvalidTextResponse):
                # Fence failure must not throw away an already complete model
                # receipt. Preserve it under the original attempt; old epochs
                # can only append quarantine evidence, never replace a draft.
                error=result.get('error'); raw=result.get('output')
                if isinstance(error,InvalidTextResponse): raw=error.raw
                if isinstance(raw,str):
                    sha=hashlib.sha256(raw.encode()).hexdigest()
                    try:
                        parsed=strict_json(raw)
                        if not isinstance(parsed,dict) or isinstance(error,InvalidTextResponse): raise ValueError()
                        status='valid_draft'
                    except (ValueError,TypeError,RecursionError): parsed=None;status='invalid_draft'
                    try:
                        guard()
                        jobs.persistence.archive(self.job_id,row['owner_id'],row['lease_epoch'],row['attempt_id'],
                            {'output_text':raw,'output_sha256':sha,'output_json':parsed,'output_status':status,'error':evidence.get('reason')})
                    except TextFenced:
                        jobs.persistence.quarantine(self.job_id,row['lease_epoch'],{'attempt_id':row['attempt_id'],'sha256':sha,'output_text':raw})


def validate_concurrent_evidence(row, expected_hash):
    proof=row.get('external_concurrent_proof')
    if not isinstance(proof,dict) or digest(proof) != expected_hash:
        raise RuntimeError('Concurrent qualification evidence hash mismatch')
    try: payload_hash=digest(FixedTextHTTP.payload(None,row['task_kind'],row['inputs']))
    except (KeyError,TypeError): raise RuntimeError('Concurrent qualification frozen input is unavailable')
    identity=row.get('runtime_identity'); overlap=proof.get('overlap',{}); accepted=proof.get('accepted',{})
    if (proof.get('schema')!='dgx-text-concurrent-v1' or proof.get('simulation') is not False or proof.get('status')!='PASS'
            or any(proof.get(key)!=row.get(key) for key in ('job_id','owner_id','lease_epoch','lease_generation','attempt_id','input_hash','output_sha256'))
            or proof.get('runtime_sha256')!=digest(identity) or proof.get('payload_sha256')!=payload_hash
            or proof.get('resource_operation_id')!=(row.get('resource_operation_id') or 'text-'+row['job_id'])
            or proof.get('baseline_idle') is not True or proof.get('final_idle') is not True
            or overlap.get('connection_refused') is not True or overlap.get('client_pending') is not True
            or accepted.get('client_pending') is not True):
        raise RuntimeError('Concurrent qualification binding or observations are invalid')
    fence=proof.get('fence_proof',{})
    if (fence.get('admission_closed') is not True or fence.get('inflight_zero') is not True
            or fence.get('lease_generation')!=row.get('lease_generation') or fence.get('runtime_identity')!=identity
            or not fence.get('receipt_sha256')):
        raise RuntimeError('Concurrent qualification parent fence proof is invalid')
    try:
        times=[proof['sent_at'],accepted['observed_at'],proof['fence_started_at'],overlap['started_at'],overlap['observed_at'],proof['completed_at']]
        if any(not math.isfinite(x) for x in times) or times != sorted(times): raise ValueError()
        if not math.isfinite(proof['fence_completed_at']) or proof['fence_completed_at'] < overlap['observed_at']: raise ValueError()
        for sample in (accepted,overlap):
            metrics=sample['metrics']
            if len(metrics)!=2 or any(not math.isfinite(x) or x<0 for x in metrics) or metrics[0]<=0: raise ValueError()
    except (KeyError,TypeError,ValueError): raise RuntimeError('Concurrent qualification overlap timing is invalid')
    return proof
