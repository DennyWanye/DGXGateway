import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from DGXGateway.dgx_gateway.ssh import CommandResult
from DGXGateway.dgx_gateway.video_pair_transport import PairRequestUnknown, PairVideoTransport


class FakeSSH:
    def __init__(self):
        self.files = {}
        self.puts = []
        self.posts = []

    def run(self, node, argv, timeout=10):
        if argv[:2] == ["mkdir", "-p"]:
            return CommandResult(0, "", "")
        if argv[0] == "sha256sum":
            data = self.files.get((node.address, argv[1]))
            if data is None:
                return CommandResult(1, "", "missing")
            return CommandResult(0, hashlib.sha256(data).hexdigest() + "  " + argv[1] + "\n", "")
        if argv[0] == "python3" and argv[3].endswith("/v1/videos"):
            self.posts.append(node.address)
            return CommandResult(0, json.dumps({"id": "pair-job-1"}), "")
        if argv[0] == "python3" and "/content" in argv[3]:
            return CommandResult(0, "8\n", "")
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


class PairTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ssh = FakeSSH()

    def test_stage_is_limited_to_selected_pair_and_post_uses_its_head(self):
        transport = PairVideoTransport("pair-b", self.ssh)
        local = Path(self.tmp.name) / "ref.png"
        local.write_bytes(b"png-bytes")
        rel = "workbench/" + "a" * 32 + "/" + "b" * 32 + "-primary.png"
        transport.stage(rel, str(local))
        self.assertEqual(set(self.ssh.puts), {"192.0.2.39", "192.0.2.40"})
        job_id = transport.submit(transport.payload("ref2va", "prompt", 17, [
            {"type": "image", "uri": transport.uri(rel), "role": "reference"}
        ]))
        self.assertEqual(job_id, "pair-job-1")
        self.assertEqual(self.ssh.posts, ["192.0.2.39"])

    def test_before_post_receipt_and_lost_receipt_are_non_retryable(self):
        transport = PairVideoTransport("pair-a", self.ssh)
        marks = []

        def fail_post(node, argv, timeout=10):
            if argv[0] == "python3" and argv[3].endswith("/v1/videos"):
                self.ssh.posts.append(node.address)
                return CommandResult(255, "", "ssh dropped")
            return FakeSSH.run(self.ssh, node, argv, timeout)

        self.ssh.run = fail_post
        with self.assertRaises(PairRequestUnknown):
            transport.submit(transport.payload("t2va", "prompt", 1, []), before_post=lambda: marks.append("started"))
        self.assertEqual(marks, ["started"])
        self.assertEqual(self.ssh.posts, ["192.0.2.41"])

    def test_stage_failure_happens_before_request_receipt(self):
        transport = PairVideoTransport("pair-a", self.ssh)
        marks = []

        def fail_mkdir(node, argv, timeout=10):
            if argv[:2] == ["mkdir", "-p"]:
                return CommandResult(1, "", "mkdir failed")
            return FakeSSH.run(self.ssh, node, argv, timeout)

        self.ssh.run = fail_mkdir
        with self.assertRaises(RuntimeError):
            transport.submit(transport.payload("t2va", "prompt", 1, []), before_post=lambda: marks.append("started"))
        self.assertEqual(marks, [])
        self.assertEqual(self.ssh.posts, [])

    def test_rejects_escape_and_wrong_pair(self):
        with self.assertRaises(ValueError):
            PairVideoTransport("pair-c", self.ssh)
        transport = PairVideoTransport("pair-a", self.ssh)
        with self.assertRaises(ValueError):
            transport.uri("../secret.png")


if __name__ == "__main__":
    unittest.main()
