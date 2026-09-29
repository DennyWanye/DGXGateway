import ast
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from typing import Any, Callable
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]

class OverlayProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.overlay = Path(self.temp.name) / 'overlay'
        shutil.copytree(ROOT/'scripts/minimax-h3-two-node/long-video-overlay', self.overlay)

    def args_for(self, profile):
        return subprocess.run(['bash', '-c',
            'source "$1"; H3_SELECTED_PROFILE=$2; h3_long_video_args "$3" || exit $?; printf "%s\\n" "${H3_LONG_VIDEO_ARGS[@]}"',
            'overlay-test', str(ROOT/'scripts/minimax-h3-two-node/long-video-mounts.sh'),
            profile, str(self.overlay)], capture_output=True, text=True)

    def test_h3_one_only_receives_executor_health_overlay(self):
        result = self.args_for('heretic-ref2va-v1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['-v',
            f'{self.overlay}/executor.py:/opt/h3-multinode/h3_multinode/executor.py:ro'])

    def test_h3_two_retains_four_mounts_and_original_timeout(self):
        result = self.args_for('heretic-ref2va-hmnsfw-v1')
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((self.overlay/'manifest.json').read_text())
        targets = {row['file']: row['target'] for row in manifest['files']}
        expected = ['-e', 'VLLM_OMNI_VIDEO_SYNC_TIMEOUT=0']
        for name in ['executor.py', 'api_server.py', 'pipeline_minimax_h3.py', 'vae.py']:
            expected += ['-v', f'{self.overlay}/{name}:{targets[name]}:ro']
        self.assertEqual(result.stdout.splitlines(), expected)

    def test_other_profiles_receive_no_overlay(self):
        for profile in ['10eros-fl2va-v1', 'unknown']:
            result = self.args_for(profile)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), '')

    def test_bad_executor_hash_rejects_before_returning_any_mount(self):
        with (self.overlay/'executor.py').open('a') as handle:
            handle.write('\n# corrupted\n')
        for profile in ['heretic-ref2va-v1', 'heretic-ref2va-hmnsfw-v1']:
            result = self.args_for(profile)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '')

    def test_h3_one_requires_the_qualified_original_executor_identity(self):
        path = self.overlay/'manifest.json'
        manifest = json.loads(path.read_text())
        next(row for row in manifest['files'] if row['file']=='executor.py')['source_sha256'] = '0'*64
        path.write_text(json.dumps(manifest))
        result = self.args_for('heretic-ref2va-v1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')

class ExecutorHealthTests(unittest.TestCase):
    def make_executor(self):
        source = (ROOT/'scripts/minimax-h3-two-node/long-video-overlay/executor.py').read_text()
        node = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == 'RayDiffusionExecutor')
        namespace = {'DiffusionExecutor': object, 'EngineDeadError': RuntimeError, 'Any': Any, 'Callable': Callable}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'executor.py', 'exec'), namespace)
        executor = namespace['RayDiffusionExecutor']()
        executor._closed = executor._is_failed = False
        executor._failure_callbacks = []
        executor._actors = [Mock(), Mock()]
        executor._ray = Mock()
        executor._rpc_lock = threading.RLock()
        return executor

    def test_busy_health_does_not_ping_or_destroy_serial_workers(self):
        executor = self.make_executor()
        held, release = threading.Event(), threading.Event()
        def work():
            with executor._rpc_lock:
                held.set()
                release.wait(3)
        worker = threading.Thread(target=work)
        worker.start()
        self.assertTrue(held.wait(1))
        try:
            executor.check_health()
            executor._ray.get.assert_not_called()
            for actor in executor._actors: actor.ping.remote.assert_not_called()
            self.assertFalse(executor.is_dead)
        finally:
            release.set()
            worker.join()
        executor._ray.get.return_value = [{'rank':0}, {'rank':1}]
        executor.check_health()
        executor._ray.get.assert_called_once()

    def test_idle_dead_worker_still_fails_closed(self):
        executor = self.make_executor()
        executor._ray.get.side_effect = RuntimeError('actor dead')
        with self.assertRaises(RuntimeError): executor.check_health()
        self.assertTrue(executor.is_dead)

    def test_cooperative_cancel_does_not_poison_executor(self):
        executor = self.make_executor()
        executor._ray.get.return_value = [{'rank':n,'ok':False,'error':'H3 request canceled at a collective safe boundary'} for n in (0,1)]
        with self.assertRaisesRegex(RuntimeError, 'canceled'):
            executor.collective_rpc('execute_model')
        self.assertFalse(executor.is_dead)

    def test_other_rank_errors_still_poison_executor(self):
        executor = self.make_executor()
        executor._ray.get.return_value = [{'rank':0,'ok':False,'error':'CUDA failure'}, {'rank':1,'ok':True}]
        with self.assertRaises(RuntimeError): executor.collective_rpc('execute_model')
        self.assertTrue(executor.is_dead)

class RecoveryAdmissionTests(unittest.TestCase):
    def test_partial_drain_requires_explicit_empty_successful_queue_response(self):
        import json
        from types import SimpleNamespace
        from DGXGateway.dgx_gateway.adapters.h3 import H3Adapter
        from DGXGateway.dgx_gateway.domain import ObservedModel
        for payload, code, accepted in [({},0,False), ({'queue_running':[]},0,False),
                ({'queue_running':[], 'queue_pending':[]},1,False),
                ({'queue_running':[1], 'queue_pending':[]},0,False),
                ({'queue_running':[], 'queue_pending':[]},0,True)]:
            with self.subTest(payload=payload, code=code):
                adapter = H3Adapter(SimpleNamespace(nodes=[SimpleNamespace(role='head')], h3_queue_url='http://comfy/queue'), Mock())
                adapter.observe = Mock(return_value=ObservedModel('minimax-h3','PARTIAL','heretic-ref2va-hmnsfw-v1'))
                adapter.ssh.run.return_value = SimpleNamespace(returncode=0,stdout='{"api":"STOPPED"}')
                authority = Mock()
                authority.run.return_value = SimpleNamespace(returncode=code,stdout=json.dumps(payload))
                adapter._authority = Mock(return_value=authority)
                if accepted: adapter.drain(Mock())
                else:
                    with self.assertRaises(RuntimeError): adapter.drain(Mock())

    def test_only_isolated_partial_h3_can_be_stopped(self):
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch
        from DGXGateway.dgx_gateway.server import GatewayApplication
        from DGXGateway.dgx_gateway.store import OperationStore
        from DGXGateway.dgx_gateway.domain import ObservedModel
        for other, target, accepted in [('STOPPED',None,True),('UNKNOWN',None,False),('HEALTHY',None,False),('STOPPED','minimax-h3',False)]:
            with self.subTest(other=other,target=target), tempfile.TemporaryDirectory() as directory:
                store=OperationStore(Path(directory)/'db')
                adapters={name:SimpleNamespace(observe=Mock(return_value=ObservedModel(name,state,'heretic-ref2va-hmnsfw-v1'))) for name,state in [('minimax-h3','PARTIAL'),('other',other)]}
                app=GatewayApplication(config=object(),store=store,operations=SimpleNamespace(adapters=adapters),snapshot=object(),static_dir=Path(directory),admission=SimpleNamespace(assert_allowed=lambda:None))
                try:
                    with patch('DGXGateway.dgx_gateway.server.threading.Thread'):
                        if accepted:
                            op,_=app.submit_switch(target,None,'key')
                            self.assertEqual(op.source_model,'minimax-h3')
                        else:
                            with self.assertRaises(RuntimeError): app.submit_switch(target,None,'key')
                finally: store.close()
