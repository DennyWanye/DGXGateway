import threading
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from DGXGateway.dgx_gateway.adapters.h3_dual_pair import (
    H3DualPairAdapter, MODEL_ID, PAIRS, container_name,
)
from DGXGateway.dgx_gateway.image_assets import ImageAssets
from DGXGateway.dgx_gateway.operations import OperationContext
from DGXGateway.dgx_gateway.ssh import CommandResult
from DGXGateway.dgx_gateway.video_contracts import LONG_PROFILE
from DGXGateway.dgx_gateway.video_jobs import VideoJobs


def ok(stdout=""):
    return CommandResult(0, stdout, "")


def absent():
    return CommandResult(1, "", "No such container")


class DualPairAdapterTests(unittest.TestCase):
    def setUp(self):
        self.ssh = Mock()
        self.adapter = H3DualPairAdapter(SimpleNamespace(), self.ssh)
        self.context = OperationContext("op", Path("/tmp/token"), "generation", lambda: None)

    def fake(self, running=True, api=True):
        def run(node, argv, timeout=10):
            if argv[:2] == ["docker", "inspect"]:
                if not running:
                    return absent()
                name = argv[-1]
                pair = next(pair for pair in PAIRS if name.startswith(pair.container_prefix))
                return ok(f"true ref2va-official dual-{pair.pair_id}-ref-op\n")
            if argv[:2] == ["sh", "-c"] and "hostname" in argv[-1]:
                return ok(f"{node.hostname}\n{node.user}\n")
            if argv[:2] == ["sh", "-c"] and "30021" in argv[-1]:
                return ok("LISTEN 127.0.0.1:30021\n" if api else "")
            if argv and argv[0] == "python3" and "profile_node.py" in argv[1]:
                pair = next(pair for pair in PAIRS if pair.config_name in " ".join(argv))
                rank = next(rank for rank in pair.ranks if rank.node.address == node.address)
                return ok(f"docker run --rm --name {container_name(pair, rank.rank)} img sglang serve\n")
            if argv[:2] == ["docker", "run"]:
                return ok("cid\n")
            if argv and argv[0] == "docker" and argv[1] in {"stop", "rm"}:
                return ok()
            if argv[:2] == ["docker", "logs"]:
                return ok("log")
            raise AssertionError(argv)
        self.ssh.run.side_effect = run

    def test_new_identity_and_two_pairs(self):
        self.assertEqual(MODEL_ID, "h3-dual-pair")
        self.assertEqual([pair.pair_id for pair in PAIRS], ["pair-a", "pair-b"])
        self.assertEqual({rank.rank for pair in PAIRS for rank in pair.ranks}, {0, 1})

    def test_all_four_pair_ranks_and_both_apis_are_required(self):
        self.fake(running=True, api=True)
        self.assertEqual(self.adapter.observe().state, "HEALTHY")
        self.fake(running=True, api=False)
        self.assertEqual(self.adapter.observe().state, "PARTIAL")

    def test_start_uses_world_two_distinct_names_and_workers_first(self):
        self.fake(running=False)
        self.adapter.start(self.context, "ref2va-official")
        renders = [call for call in self.ssh.run.call_args_list
                   if call.args[1] and call.args[1][0] == "python3"]
        runs = [call for call in self.ssh.run.call_args_list
                if call.args[1][:2] == ["docker", "run"]]
        self.assertEqual(len(renders), 4)
        self.assertEqual(len(runs), 4)
        self.assertEqual([call.args[0].role for call in runs], ["worker", "rank3", "head", "rank2"])
        self.assertTrue(all(call.args[1][call.args[1].index("--world") + 1] == "2" for call in renders))
        prefixes = {call.args[1][call.args[1].index("--container-prefix") + 1] for call in renders}
        self.assertEqual(prefixes, {"h3-pair-a-r", "h3-pair-b-r"})

    def test_stop_only_targets_pair_names(self):
        self.fake(running=True)
        self.adapter.stop(self.context)
        names = [call.args[1][-1] for call in self.ssh.run.call_args_list
                 if call.args[1][:3] == ["docker", "rm", "-f"]]
        self.assertEqual(set(names), {"h3-pair-a-r0", "h3-pair-a-r1", "h3-pair-b-r0", "h3-pair-b-r1"})
        self.assertTrue(all(not name.startswith("h3-ring-serve") for name in names))


class DualPairQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        images = ImageAssets(root / "creative-assets")
        adapter = SimpleNamespace(observe=Mock(return_value=SimpleNamespace(
            model_id=MODEL_ID, state="HEALTHY", profile="ref2va-official")))
        self.app = SimpleNamespace(
            config=SimpleNamespace(
                state_dir=root,
                repo_root=root,
                nodes=[SimpleNamespace(address="127.0.0.1")],
                bind="127.0.0.1",
                port=18082,
            ),
            _submit_lock=threading.RLock(),
            creative_jobs=SimpleNamespace(active=None, assets=images),
            operations=SimpleNamespace(adapters={MODEL_ID: adapter}),
            store=SimpleNamespace(active_operation=lambda: None, list_operations=lambda limit=-1: []),
        )
        self.jobs = VideoJobs(self.app)
        self.app.video_jobs = self.jobs

    @staticmethod
    def row(job_id):
        return {
            "job_id": job_id,
            "idempotency_key": "dual-pair-test-" + job_id[:8],
            "profile": LONG_PROFILE,
            "runtime_model": MODEL_ID,
            "runtime_profile": "ref2va-official",
            "runtime_task": "ref2va",
            "state": "QUEUED",
            "memory_released": False,
            "shots": [],
        }

    def test_two_slots_run_and_third_waits_fifo(self):
        rows = [self.row(char * 32) for char in "abc"]
        acquired = []
        self.jobs.save = Mock()

        def acquire(row):
            acquired.append((row["job_id"], self.jobs._acquire_slot(row)))

        threads = [threading.Thread(target=acquire, args=(row,)) for row in rows]
        for thread in threads[:2]:
            thread.start()
        deadline = time.monotonic() + 2
        while len(acquired) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual({slot for _job, slot in acquired}, {"pair-a", "pair-b"})
        threads[2].start()
        time.sleep(0.05)
        self.assertTrue(threads[2].is_alive())
        self.jobs._release_slot(rows[0])
        threads[2].join(2)
        self.assertFalse(threads[2].is_alive())
        self.assertEqual(acquired[2][1], rows[0]["executor_slot"])
        self.jobs._release_slot(rows[1])
        self.jobs._release_slot(rows[2])
        for thread in threads:
            thread.join(1)

    def test_reconcile_queries_original_pair_job_and_never_submits(self):
        row = self.row("d" * 32)
        row.update(state="SUBMISSION_UNKNOWN", executor_slot="pair-b")
        row["shots"] = [{
            "name": "shot-a", "index": 0, "state": "SUBMISSION_UNKNOWN",
            "submission_phase": "UNKNOWN", "pair_id": "pair-b",
            "pair_job_id": "pair-job-original", "request_id": "e" * 32,
        }]
        self.jobs.save(row)
        self.jobs._slots["pair-b"] = row["job_id"]
        self.jobs.active = row["job_id"]
        self.jobs.active_jobs.add(row["job_id"])
        transport = SimpleNamespace(
            poll=Mock(return_value={"status": "completed"}),
            download=Mock(return_value=b"fixture-mp4"),
            submit=Mock(),
        )
        self.jobs.pair_transports = {"pair-b": transport}
        self.jobs._publish_result = Mock(return_value={"path": "original.mp4"})
        result = self.jobs.reconcile(row["job_id"])
        self.assertEqual(result["state"], "SUCCEEDED")
        self.assertTrue(result["memory_released"])
        transport.poll.assert_called_once_with("pair-job-original")
        transport.download.assert_called_once_with("pair-job-original")
        transport.submit.assert_not_called()
        self.assertIsNone(self.jobs._slots["pair-b"])

    def test_restart_rehydrates_original_pair_slot(self):
        row = self.row("f" * 32)
        row.update(state="RUNNING", executor_slot="pair-a")
        row["shots"] = [{
            "name": "shot-a", "index": 0, "state": "RUNNING",
            "submission_phase": "REQUEST_STARTED", "pair_id": "pair-a",
            "pair_job_id": "pair-job-running", "request_id": "1" * 32,
        }]
        self.jobs.save(row)
        restarted = VideoJobs(self.app)
        loaded = restarted.get(row["job_id"])
        self.assertEqual(loaded["state"], "SUBMISSION_UNKNOWN")
        self.assertEqual(restarted._slots["pair-a"], row["job_id"])
        self.assertEqual(loaded["shots"][0]["pair_job_id"], "pair-job-running")


if __name__ == "__main__":
    unittest.main()
