import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from DGXGateway.dgx_gateway.maintenance import creative_admission, set_mutation_fence
from DGXGateway.dgx_gateway.store import OperationStore


class CreativeMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = SimpleNamespace(state_dir=Path(self.temp.name))
        self.config_path = Path(self.temp.name) / 'gateway.toml'
        store = OperationStore(self.config.state_dir / 'gateway.sqlite3')
        store.close()
        self.patch = patch('DGXGateway.dgx_gateway.maintenance.load_config', return_value=self.config)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def job(self, state='RUNNING', cleanup=False):
        root = self.config.state_dir / 'creative-jobs'
        root.mkdir(exist_ok=True)
        (root / 'job.json').write_text(json.dumps({'job_id': 'job', 'state': state, 'cleanup_required': cleanup}))

    def test_running_job_blocks_restart_without_fencing_its_stop(self):
        self.job()
        with self.assertRaisesRegex(RuntimeError, 'creative job'):
            set_mutation_fence(self.config_path, True)
        with creative_admission(self.config):
            pass  # The refused restart did not strand active job's future stop.

    def test_recovery_job_blocks_restart(self):
        self.job('INTERRUPTED', cleanup=True)
        with self.assertRaises(RuntimeError):
            set_mutation_fence(self.config_path, True)

    def test_running_video_batch_blocks_restart(self):
        root = self.config.state_dir / 'video-jobs'
        root.mkdir(exist_ok=True)
        (root / 'video.json').write_text(json.dumps({'job_id': 'video', 'state': 'RUNNING', 'cleanup_required': False}))
        with self.assertRaisesRegex(RuntimeError, 'creative job video'):
            set_mutation_fence(self.config_path, True)

    def test_cancel_ack_is_not_a_job_but_parent_ownership_still_blocks(self):
        root = self.config.state_dir / 'video-jobs'
        root.mkdir()
        jid = 'a' * 32
        parent = root / (jid + '.json')
        ack = root / (jid + '.cooperative-cancel.json')
        ack.write_text(json.dumps({'request_id': 'b' * 32, 'acknowledged': True, 'status': 200}))
        for state, cleanup in [('RUNNING', False), ('CANCELED', True)]:
            parent.write_text(json.dumps({'job_id': jid, 'state': state, 'cleanup_required': cleanup}))
            with self.assertRaisesRegex(RuntimeError, 'creative job ' + jid):
                set_mutation_fence(self.config_path, True)
        parent.write_text(json.dumps({'job_id': jid, 'state': 'CANCELED', 'cleanup_required': False, 'memory_released': True}))
        before = ack.read_bytes()
        self.assertTrue(set_mutation_fence(self.config_path, True)['mutation_fenced'])
        self.assertEqual(ack.read_bytes(), before)

    def test_orphan_ack_and_unknown_json_do_not_bypass_maintenance(self):
        root = self.config.state_dir / 'video-jobs'
        root.mkdir()
        ack = root / ('a' * 32 + '.cooperative-cancel.json')
        ack.write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'no regular parent job'):
            set_mutation_fence(self.config_path, True)
        ack.unlink()
        (root / 'unexpected.json').write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'state unreadable'):
            set_mutation_fence(self.config_path, True)

    def test_terminal_job_allows_restart_and_new_admission_is_fenced(self):
        self.job('SUCCEEDED')
        self.assertTrue(set_mutation_fence(self.config_path, True)['mutation_fenced'])
        with self.assertRaises(BlockingIOError):
            with creative_admission(self.config):
                self.fail('new job admitted under maintenance')
        set_mutation_fence(self.config_path, False)
        with creative_admission(self.config):
            pass

    def test_admitted_job_persists_before_maintenance_checks(self):
        entered = threading.Event()
        finished = threading.Event()
        errors = []
        def maintain():
            entered.set()
            try:
                set_mutation_fence(self.config_path, True)
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()
        with creative_admission(self.config):
            thread = threading.Thread(target=maintain)
            thread.start()
            self.assertTrue(entered.wait(1))
            self.assertFalse(finished.wait(0.03))
            self.job('QUEUED')
        thread.join(2)
        self.assertTrue(finished.is_set())
        self.assertEqual(len(errors), 1)
        self.assertIn('creative job', str(errors[0]))

    def test_cold_start_can_restore_recovery_owner_without_changing_journal(self):
        self.job('INTERRUPTED', cleanup=True)
        before = (self.config.state_dir / 'creative-jobs/job.json').read_text()
        result = set_mutation_fence(self.config_path, True, allow_creative_recovery=True)
        self.assertTrue(result['mutation_fenced'])
        self.assertEqual((self.config.state_dir / 'creative-jobs/job.json').read_text(), before)

    def test_explicit_recovery_restart_accepts_failed_cleanup_owner(self):
        for state in ('FAILED', 'INTERRUPTED'):
            self.job(state, cleanup=True)
            self.assertTrue(set_mutation_fence(self.config_path, True, recovery_restart=True)['mutation_fenced'])
            set_mutation_fence(self.config_path, False)

    def test_explicit_recovery_restart_rejects_live_creative_states(self):
        for state in ('QUEUED', 'LOADING', 'SUBMITTING', 'RUNNING', 'RELEASING', 'RECONCILING'):
            self.job(state, cleanup=True)
            with self.assertRaises(RuntimeError):
                set_mutation_fence(self.config_path, True, recovery_restart=True)

    def test_explicit_recovery_restart_rejects_live_lifecycle(self):
        self.job('FAILED', cleanup=True)
        store = OperationStore(self.config.state_dir / 'gateway.sqlite3')
        store.create_operation('live-operation', None, 'personaplex', 'live-key')
        store.close()
        with self.assertRaisesRegex(RuntimeError, 'operation live-operation'):
            set_mutation_fence(self.config_path, True, recovery_restart=True)

    def video_job(self, *, state='SUBMISSION_UNKNOWN', **updates):
        root = self.config.state_dir / 'video-jobs'
        root.mkdir(exist_ok=True)
        path = root / 'video.json'
        document = {'job_id': 'video', 'state': state, 'cleanup_required': True, 'memory_released': False,
                    'shots': [{'request_id': 'a' * 32, 'state': 'SUBMISSION_UNKNOWN', 'submission_phase': 'UNKNOWN'},
                              {'request_id': 'b' * 32, 'state': 'QUEUED', 'submission_phase': 'NOT_SUBMITTED'}]}
        document.update(updates)
        path.write_text(json.dumps(document))
        return path, document

    def test_explicit_recovery_restart_accepts_settled_unknown_video_without_changing_identity(self):
        path, _document = self.video_job()
        before = path.read_bytes()
        with self.assertRaises(RuntimeError):
            set_mutation_fence(self.config_path, True)
        with creative_admission(self.config):
            pass
        self.assertTrue(set_mutation_fence(self.config_path, True, recovery_restart=True)['mutation_fenced'])
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaises(BlockingIOError):
            with creative_admission(self.config):
                self.fail('recovery restart did not fence new generation')

    def test_unknown_video_recovery_restart_requires_explicit_settled_request_records(self):
        path, valid = self.video_job()
        cases = [
            {'cleanup_required': False}, {'cleanup_required': 1},
            {'shots': []}, {'shots': None}, {'shots': ['invalid']},
            {'shots': [{'state': 'SUBMISSION_UNKNOWN', 'submission_phase': 'UNKNOWN'}]},
            {'shots': [{**valid['shots'][0], 'submission_phase': 'REQUEST_STARTED'}]},
            {'shots': [{**valid['shots'][0], 'submission_phase': None}]},
            {'shots': [{**valid['shots'][0], 'state': 'RUNNING'}]},
            {'shots': [{**valid['shots'][0], 'state': 'LOADING'}]},
        ]
        for updates in cases:
            with self.subTest(updates=updates):
                path.write_text(json.dumps({**valid, **updates}))
                before = path.read_bytes()
                with self.assertRaisesRegex(RuntimeError, 'creative job video'):
                    set_mutation_fence(self.config_path, True, recovery_restart=True)
                self.assertEqual(path.read_bytes(), before)
                with creative_admission(self.config):
                    pass

    def test_unknown_recovery_exception_is_video_only_and_never_allows_live_jobs(self):
        path, document = self.video_job()
        path.unlink()
        for directory in ('creative-jobs', 'media-jobs'):
            root = self.config.state_dir / directory
            root.mkdir(exist_ok=True)
            other = root / 'other.json'
            other.write_text(json.dumps(document))
            with self.assertRaises(RuntimeError):
                set_mutation_fence(self.config_path, True, recovery_restart=True)
            other.unlink()
        for state in ('RUNNING', 'LOADING', 'QUEUED', 'SUBMITTING', 'RELEASING', 'RECONCILING'):
            path.write_text(json.dumps({**document, 'state': state}))
            for recovery in (False, True):
                with self.subTest(state=state, recovery=recovery), self.assertRaises(RuntimeError):
                    set_mutation_fence(self.config_path, True, recovery_restart=recovery)

    def test_only_explicitly_unacquired_blocked_video_allows_normal_restart(self):
        path, document = self.video_job(state='BLOCKED', cleanup_required=False, memory_released=True,
            resource_not_acquired=True, shots=[{'request_id': 'a' * 32, 'state': 'QUEUED', 'submission_phase': 'NOT_SUBMITTED'}])
        before = path.read_bytes()
        self.assertTrue(set_mutation_fence(self.config_path, True)['mutation_fenced'])
        self.assertEqual(path.read_bytes(), before)
        set_mutation_fence(self.config_path, False)
        missing = {key: value for key, value in document.items() if key != 'resource_not_acquired'}
        path.write_text(json.dumps(missing))
        self.assertTrue(set_mutation_fence(self.config_path, True)['mutation_fenced'])
        set_mutation_fence(self.config_path, False)
        path.write_text(json.dumps({**missing, 'start_operation_id': 'c' * 32}))
        with self.assertRaises(RuntimeError):
            set_mutation_fence(self.config_path, True)
        for updates in ({'cleanup_required': True}, {'cleanup_required': 0}, {'memory_released': False},
                        {'resource_not_acquired': False}, {'resource_not_acquired': 1},
                        {'shots': []}, {'shots': [{**document['shots'][0], 'submission_phase': 'UNKNOWN'}]},
                        {'shots': [{**document['shots'][0], 'state': 'RUNNING'}]}):
            path.write_text(json.dumps({**document, **updates}))
            for recovery in (False, True):
                with self.subTest(updates=updates, recovery=recovery), self.assertRaises(RuntimeError):
                    set_mutation_fence(self.config_path, True, recovery_restart=recovery)
        path.unlink()
        root = self.config.state_dir / 'creative-jobs'
        root.mkdir()
        (root / 'blocked.json').write_text(json.dumps(document))
        with self.assertRaises(RuntimeError):
            set_mutation_fence(self.config_path, True)

    def test_unknown_video_recovery_restart_still_refuses_live_lifecycle(self):
        self.video_job()
        store = OperationStore(self.config.state_dir / 'gateway.sqlite3')
        store.create_operation('live-operation', None, 'personaplex', 'live-key')
        store.close()
        with self.assertRaisesRegex(RuntimeError, 'operation live-operation'):
            set_mutation_fence(self.config_path, True, recovery_restart=True)

    def test_ring4_cancel_requested_unknown_allows_control_plane_recovery_restart(self):
        path, document = self.video_job(
            cleanup_required=False,
            start_operation_id='c' * 32,
            runtime_model='h3-ring4',
            execution_mode='ring4_four_node',
            shots=[{'request_id': 'a' * 32, 'state': 'SUBMISSION_UNKNOWN',
                    'submission_phase': 'REQUEST_STARTED', 'ring_job_id': 'remote-job-123'}],
        )
        path.with_suffix('.cancel').write_text(json.dumps({'requested_at': 1}))
        before = path.read_bytes()
        with self.assertRaises(RuntimeError):
            set_mutation_fence(self.config_path, True)
        result = set_mutation_fence(self.config_path, True, recovery_restart=True)
        self.assertTrue(result['mutation_fenced'])
        self.assertEqual(path.read_bytes(), before)

    def test_known_ring4_unknown_allows_observer_reload_only(self):
        path, document = self.video_job(
            cleanup_required=False, start_operation_id='c' * 32,
            runtime_model='h3-ring4', execution_mode='ring4_four_node',
            shots=[{'request_id': 'a' * 32, 'state': 'SUBMISSION_UNKNOWN',
                    'submission_phase': 'UNKNOWN', 'ring_job_id': 'original-ring-id'}])
        before = path.read_bytes()
        with self.assertRaises(RuntimeError):
            set_mutation_fence(self.config_path, True)
        self.assertTrue(set_mutation_fence(self.config_path, True, recovery_restart=True)['mutation_fenced'])
        self.assertEqual(path.read_bytes(), before)
        for change in ({'ring_job_id': None}, {'submission_phase': 'REQUEST_STARTED'}, {'state': 'RUNNING'}):
            changed = json.loads(before)
            changed['shots'][0].update(change)
            path.write_text(json.dumps(changed))
            with self.assertRaises(RuntimeError):
                set_mutation_fence(self.config_path, True, recovery_restart=True)

    def test_corrupt_journal_refuses_restart(self):
        self.job()
        (self.config.state_dir / 'creative-jobs/job.json').write_text('{')
        with self.assertRaisesRegex(RuntimeError, 'unreadable'):
            set_mutation_fence(self.config_path, True)


if __name__ == '__main__':
    unittest.main()
