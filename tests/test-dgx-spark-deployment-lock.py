#!/usr/bin/env python3
import json
import hashlib
import hmac
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "dgx-spark-deployment-lock.py"


class DeploymentLockTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / "lock"
        self.h3_token = base / "h3.token"
        self.ds_token = base / "ds.token"
        self.verifier_key = base / "verifier.key"
        self.verifier_key.write_text("v" * 64 + "\n")
        self.verifier_key.chmod(0o600)

    def tearDown(self):
        self.temp.cleanup()

    def run_lock(self, *args, expected=0):
        command = [str(SCRIPT), "--root", str(self.root), *args]
        result = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(result.returncode, expected, result.stderr)
        return result

    def run_lock_env(self, *args, expected=0, env=None):
        command = [str(SCRIPT), "--root", str(self.root), *args]
        merged = dict(__import__("os").environ)
        merged.update(env or {})
        result = subprocess.run(command, text=True, capture_output=True, env=merged)
        self.assertEqual(result.returncode, expected, result.stderr)
        return result

    def acquire_h3(self, lease="300"):
        return self.run_lock(
            "acquire", "--owner", "minimax-h3", "--task-id", "h3-thread",
            "--purpose", "nccl qualification", "--lease-seconds", lease,
            "--token-file", str(self.h3_token),
        )

    def test_single_owner_guard_heartbeat_and_release(self):
        acquired = json.loads(self.acquire_h3().stdout)
        guarded = json.loads(
            self.run_lock("guard", "--owner", "minimax-h3", "--token-file", str(self.h3_token)).stdout
        )
        heartbeat = json.loads(
            self.run_lock("heartbeat", "--owner", "minimax-h3", "--token-file", str(self.h3_token)).stdout
        )
        self.assertEqual(guarded["lease_generation"], acquired["lease_generation"])
        self.assertEqual(heartbeat["lease_generation"], acquired["lease_generation"])
        release = json.loads(
            self.run_lock("release", "--owner", "minimax-h3", "--token-file", str(self.h3_token)).stdout
        )
        status = json.loads(self.run_lock("status").stdout)
        self.assertEqual(status["state"], "UNLOCKED")
        receipt = Path(release["release_receipt"])
        self.assertTrue(receipt.exists())
        completion = json.loads(receipt.read_text())
        self.assertEqual(completion["owner"], "minimax-h3")
        self.assertEqual(completion["task_id"], "h3-thread")
        self.assertEqual(completion["terminal_state"], "UNLOCKED")
        self.assertEqual(completion["operation_id"], release["release_operation_id"])
        self.assertTrue(Path(completion["intent_path"]).exists())

    def test_competing_owner_cannot_acquire_or_release(self):
        self.acquire_h3()
        self.run_lock(
            "acquire", "--owner", "deepseek-v4", "--task-id", "ds-thread",
            "--purpose", "runtime", "--lease-seconds", "300",
            "--token-file", str(self.ds_token), expected=75,
        )
        self.ds_token.write_text("wrong\n")
        self.run_lock("release", "--owner", "deepseek-v4", "--token-file", str(self.ds_token), expected=75)

    def test_acquire_does_not_delete_a_preexisting_token_file(self):
        self.h3_token.write_text("preexisting-token\n")
        self.run_lock(
            "acquire", "--owner", "minimax-h3", "--task-id", "h3-thread",
            "--purpose", "runtime", "--lease-seconds", "300",
            "--token-file", str(self.h3_token), expected=75,
        )
        self.assertEqual(self.h3_token.read_text(), "preexisting-token\n")
        self.assertFalse(self.root.exists())

    def test_stale_lock_is_fail_closed(self):
        self.acquire_h3()
        metadata_path = self.root / "owner.json"
        metadata = json.loads(metadata_path.read_text())
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        metadata["heartbeat_at"] = old.isoformat().replace("+00:00", "Z")
        metadata_path.write_text(json.dumps(metadata))
        status = json.loads(self.run_lock("status").stdout)
        self.assertEqual(status["state"], "STALE")
        self.run_lock("heartbeat", "--owner", "minimax-h3", "--token-file", str(self.h3_token), expected=75)
        self.run_lock("release", "--owner", "minimax-h3", "--token-file", str(self.h3_token), expected=75)
        self.run_lock(
            "acquire", "--owner", "deepseek-v4", "--task-id", "ds-thread",
            "--purpose", "runtime", "--lease-seconds", "300",
            "--token-file", str(self.ds_token), expected=75,
        )

    def test_stale_retirement_requires_original_capability_and_bound_safe_proof(self):
        acquired = json.loads(self.acquire_h3().stdout)
        metadata_path = self.root / "owner.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["heartbeat_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat().replace("+00:00", "Z")
        metadata_path.write_text(json.dumps(metadata))
        proof = self.root.parent / "stale-proof.json"
        evidence = self.root.parent / "stale-evidence.json"
        evidence.write_text(json.dumps({
            "schema": "dgx-stale-retirement-evidence-v1",
            "cluster_id": "dgx-head+dgx-worker",
            "owner": "minimax-h3",
            "lease_generation": acquired["lease_generation"],
            "observed_local_model_state": "ACTIVE_EXCLUSIVE",
            "remote_attestation_absent": True,
            "sampled_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }))
        evidence.chmod(0o600)
        evidence_sha = hashlib.sha256(evidence.read_bytes()).hexdigest()
        signature = hmac.new(
            ("v" * 64).encode(),
            f"{evidence_sha}:minimax-h3:{acquired['lease_generation']}".encode(),
            hashlib.sha256,
        ).hexdigest()
        proof.write_text(json.dumps({
            "schema": "dgx-stale-retirement-proof-v1",
            "cluster_id": "dgx-head+dgx-worker",
            "owner": "minimax-h3",
            "lease_generation": acquired["lease_generation"],
            "evidence_path": str(evidence),
            "evidence_sha256": evidence_sha,
            "verifier_hmac_sha256": signature,
        }))
        proof.chmod(0o600)
        self.ds_token.write_text("wrong\n")
        self.run_lock(
            "release", "--owner", "minimax-h3", "--token-file", str(self.ds_token),
            "--operation-id", "stale-explicit", "--stale-retirement-proof", str(proof),
            "--stale-retirement-verifier-key", str(self.verifier_key),
            expected=75,
        )
        result = json.loads(self.run_lock(
            "release", "--owner", "minimax-h3", "--token-file", str(self.h3_token),
            "--operation-id", "stale-explicit", "--stale-retirement-proof", str(proof),
            "--stale-retirement-verifier-key", str(self.verifier_key),
        ).stdout)
        self.assertEqual(result["state"], "UNLOCKED")
        receipt = json.loads(Path(result["release_receipt"]).read_text())
        self.assertTrue(receipt["stale_retirement"])
        self.assertEqual(receipt["stale_retirement_proof_sha256"], __import__("hashlib").sha256(proof.read_bytes()).hexdigest())

    def test_unknown_metadata_is_fail_closed(self):
        self.root.mkdir()
        (self.root / "owner.json").write_text("not-json")
        self.run_lock("status", expected=75)
        self.run_lock(
            "acquire", "--owner", "minimax-h3", "--task-id", "h3-thread",
            "--purpose", "runtime", "--lease-seconds", "300",
            "--token-file", str(self.h3_token), expected=75,
        )

    def test_heartbeat_release_are_serialized(self):
        for _ in range(25):
            self.acquire_h3()
            heartbeat = subprocess.Popen(
                [str(SCRIPT), "--root", str(self.root), "heartbeat", "--owner", "minimax-h3",
                 "--token-file", str(self.h3_token)],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            release = subprocess.Popen(
                [str(SCRIPT), "--root", str(self.root), "release", "--owner", "minimax-h3",
                 "--token-file", str(self.h3_token)],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            heartbeat.communicate(timeout=5)
            release.communicate(timeout=5)
            self.assertIn(release.returncode, (0, 75))
            self.assertIn(heartbeat.returncode, (0, 75))
            self.assertFalse(self.root.exists())
            self.assertFalse(self.h3_token.exists())

    def test_release_is_resumable_at_every_destructive_boundary(self):
        for fault in (
            "after-intent",
            "after-rename",
            "after-commit",
            "after-tombstone-cleanup",
            "after-completion",
            "after-token-cleanup",
        ):
            with self.subTest(fault=fault):
                self.acquire_h3()
                operation_id = f"release-{fault}"
                self.run_lock_env(
                    "release", "--owner", "minimax-h3", "--token-file", str(self.h3_token),
                    "--operation-id", operation_id, "--test-fault-after", fault,
                    expected=97, env={"DGX_LOCK_TEST_MODE": "1"},
                )
                status = json.loads(self.run_lock("status").stdout)
                if fault in ("after-completion", "after-token-cleanup"):
                    self.assertEqual(status["state"], "UNLOCKED")
                else:
                    self.assertEqual(status["state"], "RECOVERY_REQUIRED")
                    self.run_lock(
                        "acquire", "--owner", "deepseek-v4", "--task-id", "ds-thread",
                        "--purpose", "runtime", "--lease-seconds", "300",
                        "--token-file", str(self.ds_token), expected=75,
                    )
                resumed = json.loads(self.run_lock(
                    "release", "--owner", "minimax-h3", "--token-file", str(self.h3_token),
                    "--operation-id", operation_id,
                ).stdout)
                self.assertEqual(resumed["state"], "UNLOCKED")
                self.assertEqual(resumed["release_operation_id"], operation_id)
                self.assertFalse(self.h3_token.exists())

    def test_release_resume_rejects_wrong_capability(self):
        self.acquire_h3()
        self.run_lock_env(
            "release", "--owner", "minimax-h3", "--token-file", str(self.h3_token),
            "--operation-id", "resume-auth", "--test-fault-after", "after-rename",
            expected=97, env={"DGX_LOCK_TEST_MODE": "1"},
        )
        self.ds_token.write_text("wrong\n")
        self.run_lock(
            "release", "--owner", "minimax-h3", "--token-file", str(self.ds_token),
            "--operation-id", "resume-auth", expected=75,
        )


if __name__ == "__main__":
    unittest.main()
