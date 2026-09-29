from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from DGXGateway.dgx_gateway.snapshot import SnapshotService


class SnapshotCoalescingTests(unittest.TestCase):
    def make_service(self):
        return SnapshotService(SimpleNamespace(), Mock(), {}, telemetry=Mock())

    def test_concurrent_http_retries_share_one_build_and_isolate_results(self):
        service = self.make_service()
        started, release = threading.Event(), threading.Event()
        def collect():
            started.set()
            self.assertTrue(release.wait(2))
            return {'nodes': [{'value': 1}], 'active_connection': {'type': 'test'}}
        service._build_uncached = Mock(side_effect=collect)
        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(service.build) for _ in range(16)]
            self.assertTrue(started.wait(1))
            self.assertEqual(service._build_uncached.call_count, 1)
            release.set()
            results = [future.result(2) for future in futures]
        self.assertEqual(service._build_uncached.call_count, 1)
        results[0]['nodes'][0]['value'] = 9
        results[0].pop('active_connection')
        self.assertEqual(results[1]['nodes'][0]['value'], 1)
        self.assertIn('active_connection', service.build())

    def test_ttl_expires_from_completion(self):
        service = self.make_service()
        clock = [100.0]
        def collect():
            clock[0] += 10
            return {'build': service._build_uncached.call_count}
        service._build_uncached = Mock(side_effect=collect)
        with patch('DGXGateway.dgx_gateway.snapshot.time.monotonic', side_effect=lambda: clock[0]):
            self.assertEqual(service.build()['build'], 1)
            clock[0] += 1.9
            self.assertEqual(service.build()['build'], 1)
            clock[0] += 0.2
            self.assertEqual(service.build()['build'], 2)

    def test_deadline_does_not_detach_running_probe_and_start_another(self):
        started, release = threading.Event(), threading.Event()
        def observe():
            started.set()
            release.wait(2)
            return SimpleNamespace(model_id='test', state='STOPPED', profile=None)
        adapter = SimpleNamespace(observe=Mock(side_effect=observe))
        store = SimpleNamespace(active_operation=lambda: None, list_operations=lambda **kw: [], last_committed_profile=lambda model: None)
        service = SnapshotService(SimpleNamespace(nodes=[]), store, {'test': adapter}, telemetry=SimpleNamespace(collect=lambda: []))
        service.lock_status = lambda: {'state': 'UNLOCKED'}
        def deadline(futures, timeout):
            self.assertTrue(started.wait(1))
            return set(), set(futures)
        with patch('DGXGateway.dgx_gateway.snapshot.wait', side_effect=deadline), patch('DGXGateway.dgx_gateway.snapshot.active_connection_view', return_value=None):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(service.build)
                self.assertTrue(started.wait(1))
                second = pool.submit(service.build)
                self.assertFalse(first.done())
                self.assertFalse(second.done())
                release.set()
                self.assertEqual(first.result(2)['models'][0]['state'], 'UNKNOWN')
                second.result(2)
        self.assertEqual(adapter.observe.call_count, 1)

    def test_failed_build_does_not_poison_next_attempt(self):
        service = self.make_service()
        service._build_uncached = Mock(side_effect=[RuntimeError('probe failed'), {'ok': True}])
        with self.assertRaisesRegex(RuntimeError, 'probe failed'):
            service.build()
        self.assertEqual(service.build(), {'ok': True})
        self.assertEqual(service._build_uncached.call_count, 2)

    def test_expired_cache_is_not_served_while_refresh_waits(self):
        service = self.make_service()
        service._cached = {'old': True}
        service._cached_at = 0
        started, release = threading.Event(), threading.Event()
        def collect():
            started.set()
            release.wait(2)
            return {'new': True}
        service._build_uncached = Mock(side_effect=collect)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(service.build)
            self.assertTrue(started.wait(1))
            second = pool.submit(service.build)
            self.assertFalse(second.done())
            release.set()
            self.assertEqual(first.result(2), {'new': True})
            self.assertEqual(second.result(2), {'new': True})
        self.assertEqual(service._build_uncached.call_count, 1)


if __name__ == '__main__':
    unittest.main()
