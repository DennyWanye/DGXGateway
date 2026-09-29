import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import run_guarded as guard


class RunnerTests(unittest.TestCase):
    def test_low_memory_requires_three_consecutive_samples_and_timeout(self):
        budget = guard.Budget(100, 0)
        self.assertIsNone(budget.observe(guard.MIN_AVAILABLE - 1, 0))
        self.assertIsNone(budget.observe(guard.MIN_AVAILABLE - 1, 5))
        self.assertIsNone(budget.observe(guard.MIN_AVAILABLE, 10))
        self.assertIsNone(budget.observe(guard.MIN_AVAILABLE - 1, 15))
        self.assertIsNone(budget.observe(guard.MIN_AVAILABLE - 1, 20))
        self.assertEqual(budget.observe(guard.MIN_AVAILABLE - 1, 25), 'host_memavailable_below_20GiB_three_samples')
        self.assertEqual(budget.observe(guard.MIN_AVAILABLE, 100), 'timeout')

    def test_command_memory_readonly_fixed_image_and_cli(self):
        args = argparse.Namespace(source_dir=Path('/s'), weights_dir=Path('/w'), input_dir=Path('/i'), run_dir=Path('/r'), num_segments=19)
        command = guard.docker_create_args(args, 'our-name', 'our-id')
        self.assertEqual(command[command.index('--memory') + 1], str(100 * 1024**3))
        self.assertEqual(command[command.index('--memory-swap') + 1], str(100 * 1024**3))
        self.assertIn(guard.IMAGE, command)
        self.assertEqual(command[command.index('--entrypoint') + 1], '/opt/longcat-venv/bin/python')
        self.assertEqual(command[command.index(guard.IMAGE) + 1:command.index(guard.IMAGE) + 3], ['-m', 'torch.distributed.run'])
        self.assertNotIn('torchrun', command)
        self.assertNotIn('--standalone', command)
        for parameter in ['--rdzv_backend=static', '--master_addr=127.0.0.1', '--master_port=29500', '--node_rank=0', '--nnodes=1', '--nproc_per_node=1']:
            self.assertIn(parameter, command)
        for source, target in [('/s', '/workspace/src'), ('/w', '/workspace/weights'), ('/i', '/workspace/input')]:
            self.assertIn(f'type=bind,source={source},target={target},readonly', command)
        self.assertIn('--read-only', command)
        self.assertIn('LONGCAT_META_LOAD=1', command)
        self.assertIn('NCCL_NET_PLUGIN=none', command)
        self.assertIn('NCCL_IB_DISABLE=1', command)
        self.assertIn('NCCL_SOCKET_IFNAME==lo', command)
        self.assertIn('core=0:0', command)
        self.assertIn('--num_segments=19', command)
        self.assertIn('--use_int8', command)
        self.assertIn('--use_distill', command)
        self.assertIn('--nproc_per_node=1', command)
        self.assertIn('--network', command)
        self.assertNotIn('--privileged', command)

    def test_stop_only_owned_exact_id(self):
        docker = mock.Mock()
        docker.inspect.side_effect = [
            {'Id': 'exact-ours', 'Config': {'Labels': {guard.LABEL: 'token'}}, 'State': {'Running': True}},
            {'Id': 'exact-ours', 'Config': {'Labels': {guard.LABEL: 'token'}}, 'State': {'Running': False}}]
        guard.stop_owned(docker, 'name', 'token')
        docker.call.assert_called_once_with(['stop', '--time', '20', 'exact-ours'], timeout=35)
        docker.reset_mock()
        docker.inspect.side_effect = None
        docker.inspect.return_value = {'Config': {'Labels': {guard.LABEL: 'another-run'}}}
        with self.assertRaises(RuntimeError):
            guard.stop_owned(docker, 'name', 'token')
        docker.call.assert_not_called()

    def test_cgroup_values_and_oom_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'proc/123').mkdir(parents=True)
            (root / 'proc/123/cgroup').write_text('0::/docker/test\n')
            group = root / 'cgroup/docker/test'
            group.mkdir(parents=True)
            for name, data in {'memory.current': '100', 'memory.peak': '200', 'memory.max': str(guard.MEMORY_CAP), 'memory.swap.max': '0', 'memory.events': 'low 0\nhigh 0\nmax 4\noom 2\noom_kill 1\n'}.items():
                (group / name).write_text(data)
            sample = guard.cgroup_sample(123, root / 'proc', root / 'cgroup')
            self.assertEqual(sample['memory_current'], 100)
            self.assertEqual(sample['memory_peak'], 200)
            self.assertEqual(sample['memory_events']['oom_kill'], 1)

    def test_log_failure_does_not_skip_exact_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            docker = mock.Mock()
            docker.prefix = ['docker']
            state = {'Running': False, 'ExitCode': 0, 'OOMKilled': False}
            docker.inspect.return_value = {'Id': 'ours', 'Config': {'Labels': {guard.LABEL: 'token'}}, 'State': state}
            with mock.patch.object(guard.subprocess, 'run', side_effect=TimeoutError('logs timed out')):
                result = guard.cleanup_owned(docker, 'name', 'token', Path(directory) / 'model.log')
            docker.call.assert_called_once_with(['rm', 'ours'], timeout=30)
            self.assertTrue(result['cleanup_complete'])
            self.assertIn('log_error', result)

    def test_effective_settings_must_match(self):
        info = {'Image': guard.IMAGE, 'HostConfig': {'Memory': guard.MEMORY_CAP, 'MemorySwap': guard.MEMORY_CAP,
                'NetworkMode': 'none', 'ReadonlyRootfs': True},
                'Mounts': [{'Destination': path, 'RW': False} for path in ['/workspace/src', '/workspace/weights', '/workspace/input']]}
        guard.verify_effective_config(info)
        info['HostConfig']['MemorySwap'] += 1
        with self.assertRaises(RuntimeError):
            guard.verify_effective_config(info)

    def test_existing_output_rejected_and_input_hashes_detect_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ['src', 'weights', 'input', 'existing']:
                (root / name).mkdir()
            for name in ['run_avatar15_clean_speech.py', 'runtime_guards.py', 'meta_loader.py']:
                (root / 'src' / name).write_text('')
            for rel in ['LongCat-Video-Avatar-1.5/lora/dmd_lora.safetensors', 'LongCat-Video/text_encoder/config.json']:
                path = root / 'weights' / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('')
            for name in ['man.png', 'speech.wav']:
                (root / 'input' / name).write_bytes(b'input')
            (root / 'input/case.json').write_text(json.dumps({'cond_image': '/workspace/input/man.png', 'cond_audio': {'person1': '/workspace/input/speech.wav'}}))
            args = argparse.Namespace(source_dir=root/'src', weights_dir=root/'weights', input_dir=root/'input', run_dir=root/'existing', num_segments=1, timeout_seconds=None)
            with self.assertRaisesRegex(ValueError, 'new directory'):
                guard.validate(args)
            before = guard.input_hashes(root / 'input')
            (root / 'input/speech.wav').write_bytes(b'changed')
            self.assertNotEqual(before, guard.input_hashes(root / 'input'))
            (root / 'input/link').symlink_to(root / 'input/man.png')
            with self.assertRaises(ValueError):
                guard.input_hashes(root / 'input')


if __name__ == '__main__':
    unittest.main()
