import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from DGXGateway.dgx_gateway.ssh import CommandResult
from DGXGateway.dgx_gateway.video_ring_transport import Ring4Unknown, Ring4VideoTransport


class FakeSSH:
    def __init__(self):
        self.files = {}
        self.puts = []
        self.posts = 0
        self.deletes = 0

    def run(self, node, argv, timeout=10):
        if argv[:2] == ["mkdir", "-p"]:
            return CommandResult(0, "", "")
        if argv[0] == "sha256sum":
            data = self.files.get((node.address, argv[1]))
            if data is None:
                return CommandResult(1, "", "missing")
            return CommandResult(0, hashlib.sha256(data).hexdigest() + "  " + argv[1] + "\n", "")
        if argv[0] == "python3" and argv[3].endswith("/v1/videos"):
            self.posts += 1
            return CommandResult(0, json.dumps({"id": "ring-job-1"}), "")
        if argv[0] == "python3" and "method=\"DELETE\"" in argv[2]:
            self.deletes += 1
            return CommandResult(0, json.dumps({"id": "ring-job-1", "status": "deleted"}), "")
        if argv[0] == "python3" and "/content" in argv[3]:
            return CommandResult(0, "4\n", "")
        if argv[0] == "python3" and "/v1/videos/" in argv[3]:
            return CommandResult(0, json.dumps({"status": "completed"}), "")
        if argv[:2] == ["rm", "-f"]:
            return CommandResult(0, "", "")
        return CommandResult(1, "", "unexpected")

    def put(self, node, local_path, remote_path, timeout=60):
        self.puts.append(node.address)
        self.files[(node.address, remote_path)] = Path(local_path).read_bytes()
        return CommandResult(0, "", "")

    def get(self, node, remote_path, local_path, timeout=60):
        Path(local_path).write_bytes(b"ftypmp4x")
        return CommandResult(0, "", "")


class Ring4TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ssh = FakeSSH()
        self.transport = Ring4VideoTransport(self.ssh)

    def test_stage_syncs_all_ranks_and_submit_posts_once(self):
        local = Path(self.tmp.name) / "ref.png"
        local.write_bytes(b"png-bytes")
        rel = "workbench/" + "a" * 32 + "/" + "b" * 32 + ".png"
        self.transport.stage(rel, str(local))
        self.assertEqual(len(self.ssh.puts), 4)
        job_id = self.transport.submit(self.transport.payload("ref2va", "prompt", 17, [
            {"type": "image", "uri": self.transport.uri(rel), "role": "reference"}
        ]))
        self.assertEqual(job_id, "ring-job-1")
        self.assertEqual(self.ssh.posts, 1)
        picture = "workbench/" + "a" * 32 + "/" + "b" * 32 + "-picture-1.png"
        self.assertTrue(picture.startswith("workbench/"))
        self.transport.uri(picture)

    def test_lost_post_receipt_is_unknown_not_retryable(self):
        def fail_run(node, argv, timeout=10):
            if argv[0] == "python3" and argv[3].endswith("/v1/videos"):
                self.ssh.posts += 1
                return CommandResult(255, "", "ssh dropped")
            return FakeSSH.run(self.ssh, node, argv, timeout)
        self.ssh.run = fail_run
        local = Path(self.tmp.name) / "req.json"
        local.write_text("{}")
        with self.assertRaises(Ring4Unknown):
            self.transport.submit(self.transport.payload("t2va", "prompt", 1, []))
        self.assertEqual(self.ssh.posts, 1)

    def test_stage_failure_before_post_is_not_unknown(self):
        def fail_mkdir(node, argv, timeout=10):
            if argv[:2] == ["mkdir", "-p"]:
                return CommandResult(1, "", "mkdir failed")
            return FakeSSH.run(self.ssh, node, argv, timeout)
        self.ssh.run = fail_mkdir
        with self.assertRaises(RuntimeError):
            self.transport.submit(self.transport.payload("t2va", "prompt", 1, []))
        self.assertEqual(self.ssh.posts, 0)

    def test_rejects_path_escape(self):
        with self.assertRaises(ValueError):
            self.transport.uri("../secret.png")
        with self.assertRaises(ValueError):
            self.transport.remote_asset("workbench/not-hex/x.png")

    def test_cancel_uses_one_shot_remote_delete(self):
        receipt = self.transport.cancel("ring-job-1")
        self.assertEqual(receipt["status"], "deleted")
        self.assertEqual(self.ssh.deletes, 1)

    def test_cancel_connection_refused_is_already_gone(self):
        def refuse(node, argv, timeout=10):
            if argv[0] == "python3" and "method=\"DELETE\"" in argv[2]:
                self.ssh.deletes += 1
                return CommandResult(1, "", "ConnectionRefusedError: [Errno 111] Connection refused")
            return FakeSSH.run(self.ssh, node, argv, timeout)
        self.ssh.run = refuse
        receipt = self.transport.cancel("ring-job-1")
        self.assertEqual(receipt["status"], "not_found")
        self.assertEqual(receipt["reason"], "connection_refused")
        self.assertEqual(self.ssh.deletes, 1)

    def test_delete_snippet_treats_connection_refused_as_not_found(self):
        import json
        import subprocess

        from DGXGateway.dgx_gateway.video_ring_transport import _DELETE

        proc = subprocess.run(
            ["python3", "-c", _DELETE, "http://127.0.0.1:1/v1/videos/ring-job-1"],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        receipt = json.loads(proc.stdout)
        self.assertEqual(receipt["status"], "not_found")
        self.assertEqual(receipt["reason"], "connection_refused")


if __name__ == "__main__":
    unittest.main()
