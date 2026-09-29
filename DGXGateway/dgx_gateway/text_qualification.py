"""Explicit, version-bound text qualification. Loading never enables by default."""
from __future__ import annotations
import argparse
import hashlib
import hmac
import json
import sqlite3
import re
from pathlib import Path
import time
from .admission import MutationAdmission
from .recovery import _atomic_private_json
from .ssh import FixedSSH
from .text_jobs import MODEL, CONTRACT, KINDS, FixedTextHTTP, AdapterTextFence, canonical

REMOTE = {
    '/usr/local/libexec/run-deepseek-v4-flash-dspark.sh': 'scripts/run-deepseek-v4-flash-dspark.sh',
    '/usr/local/libexec/recover-deepseek-v4-flash': 'scripts/recover-deepseek-v4-flash.py',
}
REMOTE_BY_ROLE = {'head':dict(REMOTE), 'worker':{p:s for p,s in REMOTE.items() if p.endswith('run-deepseek-v4-flash-dspark.sh')}}
SCRIPTS = tuple(REMOTE.values()) + ('scripts/control-deepseek-v4-flash-dspark.py', 'scripts/dgx-spark-deployment-lock.py')


def source_hashes(config):
    root = Path(config.repo_root)
    files = sorted(p for p in (root/'DGXGateway/dgx_gateway').rglob('*.py') if 'drama' not in p.relative_to(root/'DGXGateway/dgx_gateway').parts) + [root/x for x in SCRIPTS]
    if not files: raise RuntimeError('Text qualification source files unavailable')
    result = {}
    for path in files:
        if path.is_symlink() or not path.is_file(): raise RuntimeError('Qualification source is unsafe')
        result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def runtime_hashes(config, guard=lambda: None):
    result = {}
    for node in config.nodes:
        guard()
        expected_paths = REMOTE_BY_ROLE[node.role]
        probe = FixedSSH().run(node, ['sh', '-c', 'hostname; whoami; sudo -n sha256sum -- ' + ' '.join(expected_paths)], timeout=15)
        guard()
        lines = probe.stdout.splitlines()
        if probe.returncode or len(lines) != 2 + len(expected_paths) or lines[:2] != [node.hostname, node.user]:
            raise RuntimeError('Actual runtime helper hash unavailable')
        files = {}
        for line in lines[2:]:
            digest, path = line.split(maxsplit=1)
            if path not in expected_paths or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise RuntimeError('Actual runtime helper identity mismatch')
            files[path] = digest
        if set(files) != set(expected_paths): raise RuntimeError('Incomplete runtime helper inventory')
        result[node.role] = files
    return result


def validate_runtime(config, sources, actual):
    if set(actual) != {'head', 'worker'}: raise RuntimeError('Both runtime nodes must be qualified')
    for role in actual:
        expected = {remote:sources[local] for remote,local in REMOTE_BY_ROLE[role].items()}
        if actual[role] != expected: raise RuntimeError('Deployed runtime helper differs from qualified source')


def _signed(record, config):
    secret = MutationAdmission._read_secret(config.authority_credential)
    return hmac.new(secret.encode(), canonical(record).encode(), hashlib.sha256).hexdigest()


def load_qualification(config):
    path = getattr(config, 'text_qualification_file', None)
    expected = getattr(config, 'text_qualification_sha256', None)
    if path is None or expected is None: raise RuntimeError('No text qualification artifact configured')
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077 or path.stat().st_size > 1024*1024:
        raise RuntimeError('Qualification artifact is unsafe')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected: raise RuntimeError('Qualification artifact hash mismatch')
    record = json.loads(raw); signature = record.pop('hmac_sha256', None)
    if not hmac.compare_digest(str(signature), _signed(record, config)): raise RuntimeError('Qualification signature mismatch')
    if (record.get('schema') != 'dgx-text-qualification-v1' or record.get('model') != MODEL
            or record.get('contract') != CONTRACT or record.get('simulation') is not False
            or record.get('authority_id') != config.authority_id
            or record.get('gates') != {'http': 'PASS', 'fence': 'PASS', 'release': 'PASS', 'external_concurrent': 'PASS'}):
        raise RuntimeError('Text qualification is incomplete or simulated')
    if record.get('sources') != source_hashes(config): raise RuntimeError('Text qualification source version changed')
    validate_runtime(config, record['sources'], record.get('runtime_helpers', {}))
    evidence = record.get('jobs')
    if not isinstance(evidence, list) or len(evidence) != len(KINDS) or {x.get('task_kind') for x in evidence} != set(KINDS):
        raise RuntimeError('Qualification task evidence is incomplete')
    verify_job_evidence(config, evidence, record.get('external_concurrent'))
    return record


def verify_job_evidence(config, evidence, concurrent):
    database = Path(config.state_dir)/'gateway.sqlite3'
    if database.is_symlink() or not database.is_file(): raise RuntimeError('Qualification job database unavailable')
    connection = sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)
    try:
        seen=set(); concurrent_verified=False
        if not isinstance(concurrent,dict): raise RuntimeError('Concurrent qualification evidence is missing')
        for item in evidence:
            job_id=item.get('job_id')
            if not isinstance(job_id,str) or len(job_id)!=32 or any(c not in '0123456789abcdef' for c in job_id) or job_id in seen: raise RuntimeError('Qualification job identity invalid')
            seen.add(job_id)
            saved=connection.execute('SELECT payload FROM text_jobs WHERE job_id=?',(job_id,)).fetchone()
            if saved is None: raise RuntimeError('Qualification job evidence is missing')
            row=json.loads(saved[0])
            if (row.get('state')!='SUCCEEDED' or row.get('resource_status')!='released' or row.get('memory_released') is not True
                    or row.get('output_status')!='valid_draft' or row.get('task_kind')!=item.get('task_kind')
                    or row.get('input_hash')!=item.get('input_hash') or row.get('output_sha256')!=item.get('output_sha256')
                    or not isinstance(row.get('output_text'),str) or hashlib.sha256(row['output_text'].encode()).hexdigest()!=item.get('output_sha256')
                    or not row.get('fence_proof') or hashlib.sha256(canonical(row['fence_proof']).encode()).hexdigest()!=item.get('fence_proof_sha256')
                    or not row.get('release_receipt') or row['release_receipt'].get('sha256')!=item.get('release_receipt_sha256')):
                raise RuntimeError('Qualification job evidence does not match archived results and release')
            if job_id == concurrent.get('job_id'):
                from .text_concurrent_qualification import validate_concurrent_evidence
                validate_concurrent_evidence(row, concurrent.get('sha256'))
                attempt=connection.execute('SELECT job_id,epoch,state,payload FROM text_attempts WHERE attempt_id=?',(row['attempt_id'],)).fetchone()
                if (attempt is None or attempt[:3]!=(job_id,row['lease_epoch'],'RESULT_ARCHIVED')
                        or json.loads(attempt[3]).get('output_sha256')!=row['output_sha256']):
                    raise RuntimeError('Concurrent qualification original attempt is not archived under the same epoch')
                concurrent_verified=True
        if not concurrent_verified: raise RuntimeError('Concurrent evidence must belong to one of the qualified jobs')
    except (sqlite3.Error,ValueError,KeyError) as exc: raise RuntimeError('Qualification job evidence is invalid') from exc
    finally: connection.close()


class QualifiedFence(AdapterTextFence):
    def __init__(self, adapter, record): super().__init__(adapter); self.record = record
    def check(self, context):
        context.guard()
        if source_hashes(self.adapter.config) != self.record['sources']: raise RuntimeError('Qualified code changed')
        validate_runtime(self.adapter.config, self.record['sources'], runtime_hashes(self.adapter.config, context.guard))
    def capture(self, context): self.check(context); return super().capture(context)
    def drain(self, context, identity): self.check(context); return super().drain(context, identity)


def apply_qualification(jobs):
    jobs.allow_generation = False
    jobs.transport.qualified = False
    adapter = jobs.app.operations.adapters.get(MODEL)
    jobs.qualification_error = None
    if adapter is None:
        jobs.qualification_error = '文本资格不可用：DeepSeek V4 Flash 已从 Gateway 目录下线'
        return False
    adapter.text_fence_qualified = False
    if not getattr(jobs.app.config, 'text_enabled', False) and getattr(jobs.app.config, 'text_qualification_file', None) is None: return False
    try:
        record = load_qualification(jobs.app.config)
        if not isinstance(jobs.transport, FixedTextHTTP): raise RuntimeError('Production text transport is not fixed HTTP')
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as exc:
        # Invalid text evidence must not take down manual authoring or other APIs.
        jobs.qualification_error = '文本资格不可用：' + str(exc)[:300]
        return False
    adapter.text_fence_qualified = True
    jobs.transport.qualified = True
    jobs.fence = QualifiedFence(adapter, record)
    jobs.allow_generation = getattr(jobs.app.config, 'text_enabled', False) is True
    jobs.qualification_id = record['qualification_id']
    return True


def validate_qa_inputs(values):
    from .text_jobs import KINDS, strict_json
    if not isinstance(values,dict) or set(values) != set(KINDS): raise RuntimeError('QA inputs require exactly the supported fixed task kinds')
    for kind,value in values.items():
        if not isinstance(value,dict): raise RuntimeError('QA task input must be an object')
        serialized=canonical(value)
        if len(serialized)>32000 or len(serialized.encode())>256*1024 or re.search(r'(?:https?|ftp|file)://',serialized,re.I):
            raise RuntimeError('QA input is oversized or contains a URL')
        strict_json(serialized)
    return values


def read_qa_inputs(path):
    from .text_jobs import strict_json
    path=Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o022 or path.stat().st_size>1024*1024:
        raise RuntimeError('QA input file is unsafe or oversized')
    return validate_qa_inputs(strict_json(path.read_text()))


def qualify(jobs, output_path, qa_inputs=None, *, external_concurrent=False, concurrent_kind='draft_script'):
    """Explicit administrator QA run; never called by the service or downloads."""
    from .text_jobs import KINDS
    if (type(jobs.transport) is not FixedTextHTTP or type(jobs.fence) is not AdapterTextFence
            or getattr(jobs.transport, 'simulation', True) or getattr(jobs.fence, 'simulation', True)):
        raise RuntimeError('Simulation cannot qualify production')
    if type(external_concurrent) is not bool or concurrent_kind not in KINDS: raise RuntimeError('Invalid concurrent qualification selection')
    qa_inputs = validate_qa_inputs(qa_inputs)
    jobs.app.admission.assert_allowed()
    if jobs.active or jobs._other_busy(): raise RuntimeError('Qualification requires an idle Gateway')
    output_path = Path(output_path)
    if output_path.exists() or output_path.is_symlink(): raise RuntimeError('Qualification output already exists')
    sources = source_hashes(jobs.app.config)
    actual = runtime_hashes(jobs.app.config, jobs.app.admission.assert_allowed)
    validate_runtime(jobs.app.config, sources, actual)
    adapter = jobs.app.operations.adapters.get(MODEL)
    if adapter is None:
        raise RuntimeError('文本资格不可用：DeepSeek V4 Flash 已从 Gateway 目录下线')
    original = (jobs.allow_generation, jobs.transport.qualified, adapter.text_fence_qualified, jobs.autostart)
    original_transport=jobs.transport; concurrent_evidence=None
    results = []; qualification_id = __import__('uuid').uuid4().hex
    try:
        jobs.allow_generation = True; jobs.transport.qualified = True; adapter.text_fence_qualified = True; jobs.autostart = False
        for kind in KINDS:
            inputs = qa_inputs[kind]
            row = jobs.submit(kind, inputs, 'text-qualification-'+qualification_id+'-'+kind)
            if external_concurrent and kind == concurrent_kind:
                from .text_concurrent_qualification import ConcurrentProbeTransport
                jobs.transport=ConcurrentProbeTransport(jobs, original_transport, row['job_id'])
            try: jobs.run(row['job_id'])
            finally: jobs.transport=original_transport
            row = jobs.persistence.get(row['job_id'])
            if row['state'] != 'SUCCEEDED' or row['output_status'] != 'valid_draft' or not row['memory_released'] or not row.get('fence_proof') or not row.get('release_receipt'):
                raise RuntimeError('Real qualification task failed; original task and cleanup obligation retained')
            if external_concurrent and kind == concurrent_kind:
                from .text_concurrent_qualification import validate_concurrent_evidence, digest
                proof_hash=digest(row.get('external_concurrent_proof'))
                validate_concurrent_evidence(row,proof_hash)
                concurrent_evidence={'job_id':row['job_id'],'sha256':proof_hash}
            results.append({'job_id':row['job_id'],'task_kind':kind,'input_hash':row['input_hash'], 'output_sha256':row['output_sha256'],
                            'fence_proof_sha256':hashlib.sha256(canonical(row['fence_proof']).encode()).hexdigest(), 'release_receipt_sha256':row['release_receipt']['sha256']})
        if source_hashes(jobs.app.config) != sources: raise RuntimeError('Code changed during qualification')
        after = runtime_hashes(jobs.app.config, jobs.app.admission.assert_allowed)
        if after != actual: raise RuntimeError('Runtime helpers changed during qualification')
        record = {'schema':'dgx-text-qualification-v1','qualification_id':qualification_id,'model':MODEL,'contract':CONTRACT,
                  'authority_id':jobs.app.config.authority_id, 'simulation':False,'created_at':time.time(),
                  'gates':{'http':'PASS','fence':'PASS','release':'PASS','external_concurrent':'PASS' if concurrent_evidence else 'NOT_RUN'},
                  'scope':'Fixed authenticated HTTP, parent-owned fence and release; business semantics require separate UI acceptance',
                  'external_concurrent':concurrent_evidence,
                  'qa_inputs_sha256':hashlib.sha256(canonical(qa_inputs).encode()).hexdigest(),'sources':sources,'runtime_helpers':actual,'jobs':results}
        record['hmac_sha256'] = _signed(record, jobs.app.config)
        _atomic_private_json(output_path, record)
        return {'qualification_file':str(output_path), 'qualification_sha256':hashlib.sha256(output_path.read_bytes()).hexdigest(), 'enabled':False, 'production_qualified':bool(concurrent_evidence), 'remaining_gate':None if concurrent_evidence else 'external_concurrent'}
    finally:
        jobs.transport=original_transport
        jobs.allow_generation, jobs.transport.qualified, adapter.text_fence_qualified, jobs.autostart = original


def main():
    parser = argparse.ArgumentParser(description='Explicit local administrator text QA qualification; performs real model work only when invoked')
    parser.add_argument('--config', type=Path, required=True); parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--inputs',type=Path,required=True,help='Frozen local JSON mapping for all supported task kinds; no URLs')
    parser.add_argument('--external-concurrent',action='store_true',help='Require real in-flight HTTP overlap with graceful admission closure; never retries automatically')
    parser.add_argument('--concurrent-kind',choices=('extract_entities','draft_beats','draft_script','draft_storyboard'),default='draft_script')
    args = parser.parse_args()
    from .text_admin_recovery import build_admin_jobs
    jobs = build_admin_jobs(args.config, apply=False)
    try: print(json.dumps(qualify(jobs, args.output, read_qa_inputs(args.inputs), external_concurrent=args.external_concurrent, concurrent_kind=args.concurrent_kind), ensure_ascii=False))
    finally: jobs.app.store.close()

if __name__ == '__main__': main()
