#!/usr/bin/env python3
"""Local fixtures only: no SSH, remote writes, or large model downloads."""
import argparse
import copy
import hashlib
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("queue", HERE / "download-queue.py")
queue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue)
PLAN = queue.PROJECT / "plans/qwen38-flash-next/download-plan.json"


class DownloadTests(unittest.TestCase):
    def controller(self):
        return queue.Controller(argparse.Namespace(plan=PLAN, small_only=False, interface="en1", host="unused", limit="32M"))

    def test_pinned_plan_and_blob_count(self):
        plan = queue.verify.load_plan(PLAN)
        self.assertEqual(len(plan["files"]), 29)
        self.assertEqual(sum(not f["lfs_sha256"] for f in plan["files"]), 10)
        self.assertEqual(sum(f["size"] for f in plan["files"]), 135234133076)

    def test_hashes_reject_truncation_corruption_and_symlink(self):
        blob = b"fixture for atomic verified model download\n"
        sha = hashlib.sha256(blob).hexdigest()
        git = hashlib.sha1(f"blob {len(blob)}\0".encode() + blob).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = str(Path(tmp).resolve())
            path = Path(tmp) / "artifact"
            path.write_bytes(blob)
            self.assertTrue(queue.verify.verify_file(path, len(blob), sha))
            self.assertTrue(queue.verify.verify_file(path, len(blob), "gitsha1:" + git))
            path.write_bytes(blob[:-1])
            self.assertFalse(queue.verify.verify_file(path, len(blob), sha))
            path.write_bytes(b"X" + blob[1:])
            self.assertFalse(queue.verify.verify_file(path, len(blob), sha))
            path.write_bytes(blob)
            link = Path(tmp) / "link"
            link.symlink_to(path)
            self.assertFalse(queue.verify.verify_file(link, len(blob), sha))
            parent_link = Path(tmp) / "parent-link"
            parent_link.symlink_to(Path(tmp), target_is_directory=True)
            self.assertFalse(queue.verify.verify_file(parent_link / "artifact", len(blob), sha))

    def test_metadata_checks_revision_size_and_hash(self):
        plan = queue.verify.load_plan(PLAN)
        metadata = {r: {"sha": v, "siblings": []} for r, v in queue.verify.REVISIONS.items()}
        for f in plan["files"]:
            row = {"rfilename": f["path"], "size": f["size"], "blobId": f["git_blob_id"]}
            if f["lfs_sha256"]:
                row["lfs"] = {"size": f["size"], "sha256": f["lfs_sha256"]}
            metadata[f["repo"]]["siblings"].append(row)
        queue.validate_metadata(plan, metadata)
        for field, value in (("size", 1), ("blobId", "0" * 40)):
            changed = copy.deepcopy(metadata)
            changed[plan["files"][0]["repo"]]["siblings"][0][field] = value
            with self.assertRaises(queue.DownloadError):
                queue.validate_metadata(plan, changed)
        changed = copy.deepcopy(metadata)
        changed[plan["files"][0]["repo"]]["sha"] = "0" * 40
        with self.assertRaises(queue.DownloadError):
            queue.validate_metadata(plan, changed)

    def test_signed_url_validation(self):
        self.assertEqual(queue.validate_signed_url("https://us.aws.cdn.hf.co/file?Signature=secret"), "us.aws.cdn.hf.co")
        for url in ("http://huggingface.co/file", "https://attacker.example/file",
                    "https://huggingface.co/file\nheader=secret", "https://user:pass@huggingface.co/file"):
            with self.assertRaises(queue.DownloadError):
                queue.validate_signed_url(url)

    def test_http_401_403_hard_stop_no_retry_and_no_secret_argv(self):
        ctrl = self.controller()
        for code in (401, 403):
            completed = subprocess.CompletedProcess([], 0, f"{code}\thttps://huggingface.co/?Signature=secret".encode(), b"")
            with mock.patch.object(queue.subprocess, "run", return_value=completed) as run, mock.patch.object(queue.time, "sleep") as sleep:
                with self.assertRaises(queue.Gated):
                    ctrl.http("https://huggingface.co/?Signature=secret", head=True)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[0][:2], ["curl", "-q"])
                self.assertNotIn("secret", " ".join(run.call_args.args[0]))
                sleep.assert_not_called()

    def test_http_transient_retry_is_bounded(self):
        ctrl = self.controller()
        completed = subprocess.CompletedProcess([], 0, b"503\thttps://huggingface.co/", b"")
        with mock.patch.object(queue.subprocess, "run", return_value=completed) as run, mock.patch.object(queue.time, "sleep"):
            with self.assertRaises(queue.DownloadError):
                ctrl.http("https://huggingface.co/")
            self.assertEqual(run.call_count, 3)

    def test_lfs_redirect_is_validated_without_mac_cdn_connection(self):
        ctrl = self.controller()
        url = "https://us.aws.cdn.hf.co/file?Signature=secret"
        result = subprocess.CompletedProcess([], 0, ("302\t" + url).encode(), b"")
        with mock.patch.object(queue.subprocess, "run", return_value=result) as run:
            self.assertEqual(ctrl.http("https://huggingface.co/model", head=True), url)
            self.assertNotIn("--location", run.call_args.args[0])
            self.assertNotIn("secret", " ".join(run.call_args.args[0]))
        bad = subprocess.CompletedProcess([], 0, b"302\thttps://attacker.example/file", b"")
        with mock.patch.object(queue.subprocess, "run", return_value=bad):
            with self.assertRaises(queue.DownloadError):
                ctrl.http("https://huggingface.co/model", head=True)

    def test_existing_corruption_does_not_start_download(self):
        ctrl = self.controller()
        result = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch.object(ctrl, "ssh", side_effect=[result, queue.DownloadError("invalid file")]):
            with self.assertRaises(queue.DownloadError):
                ctrl.existing(ctrl.plan["files"][0])

    def test_transient_ssh_after_running_does_not_abandon_worker(self):
        ctrl = self.controller()
        item = next(f for f in ctrl.plan["files"] if f["lfs_sha256"])
        replies = iter([
            subprocess.CompletedProcess([], 0, b"RUNNING\n", b""),
            subprocess.CompletedProcess([], 255, b"", b"connection failed"),
            subprocess.CompletedProcess([], 0, b"PASS\n", b""),
        ])
        def ssh(command, *args, **kwargs):
            if command.startswith("sudo -n cat "):
                return next(replies)
            return b""
        with mock.patch.object(ctrl, "preflight"), mock.patch.object(ctrl, "http", return_value="https://us.aws.cdn.hf.co/file?Signature=secret"), mock.patch.object(ctrl, "resolve_ipv4", return_value="1.1.1.1"), mock.patch.object(ctrl, "ssh", side_effect=ssh), mock.patch.object(ctrl, "existing", return_value=True) as verified, mock.patch.object(queue.time, "sleep"), mock.patch.object(queue.time, "monotonic", side_effect=[0, 0, 1, 120, 130]):
            ctrl.payload(7, item)
            verified.assert_called_once_with(item)


if __name__ == "__main__":
    unittest.main()
