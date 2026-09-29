#!/usr/bin/env python3
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCORER = (
    ROOT
    / "plans/minimax-h3-two-node/spikes/fabric-network-transaction/score-nccl-candidate.py"
)
TARGET_SIZES = (268435456, 536870912, 1073741824)


class ScoreNcclCandidateTests(unittest.TestCase):
    def make_candidate(self, root: Path, *, retrans_delta=0, bad_exit=False, bad_seal=False):
        candidate = root / "single-a"
        candidate.mkdir()
        for index in range(6):
            rows = []
            for size in TARGET_SIZES:
                rows.append(
                    f"{size} 1 float sum -1 1.0 1.0 10.0 0 1.0 1.0 10.0 0"
                )
            (candidate / f"run-{index}.log").write_text("\n".join(rows) + "\n")
            exit_code = 9 if bad_exit and index == 3 else 0
            (candidate / f"run-{index}.exit").write_text(
                "candidate=single-a\n"
                f"index={index}\n"
                f"start=2026-08-30T00:0{index}:00Z\n"
                f"end=2026-08-30T00:0{index}:30Z\n"
                f"exit={exit_code}\n"
            )

        for node in ("head", "worker"):
            before = (
                f"node={node} phase=before\n"
                "/sys/class/infiniband/rocep1s0f0/ports/1/hw_counters/"
                "roce_adp_retrans:10\n"
                "/sys/class/infiniband/rocep1s0f0/ports/1/counters/port_xmit_data:100\n"
            )
            after_retrans = 10 + (retrans_delta if node == "worker" else 0)
            after = (
                f"node={node} phase=after\n"
                "/sys/class/infiniband/rocep1s0f0/ports/1/hw_counters/"
                f"roce_adp_retrans:{after_retrans}\n"
                "/sys/class/infiniband/rocep1s0f0/ports/1/counters/port_xmit_data:200\n"
            )
            (candidate / f"counters-before-{node}.txt").write_text(before)
            (candidate / f"counters-after-{node}.txt").write_text(after)
            hostname = "dgx-head" if node == "head" else "dgx-worker"
            for phase in ("before", "after"):
                (candidate / f"node-state-{phase}-{node}.txt").write_text(
                    f"node={node}\n"
                    f"phase={phase}\n"
                    f"hostname={hostname}\n"
                    "user=operator\n"
                    f"boot_id={node}-boot\n"
                    "kernel=6.17.0-1031-nvidia\n"
                    "dgx_ota=7.5.0\n"
                    "driver=580.173.02\n"
                )

        (candidate / "candidate-metadata.txt").write_text(
            "candidate=single-a\n"
            "bootstrap_ip=192.0.2.34\n"
            "iface=enp1s0f0np0\n"
            "hca=rocep1s0f0:1\n"
            f"binary_sha256={'1' * 64}\n"
            f"nccl_sha256={'2' * 64}\n"
        )

        sealed = [
            *[candidate / f"run-{index}.log" for index in range(6)],
            *[candidate / f"run-{index}.exit" for index in range(6)],
            *[
                candidate / f"counters-{phase}-{node}.txt"
                for phase in ("before", "after")
                for node in ("head", "worker")
            ],
            *[
                candidate / f"node-state-{phase}-{node}.txt"
                for phase in ("before", "after")
                for node in ("head", "worker")
            ],
            candidate / "candidate-metadata.txt",
        ]
        lines = []
        for path in sorted(sealed):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{digest}  {path.name}")
        if bad_seal:
            lines[0] = f"{'0' * 64}  {lines[0].split('  ', 1)[1]}"
        (candidate / "SHA256SUMS").write_text("\n".join(lines) + "\n")
        return candidate

    def score(self, candidate: Path):
        completed = subprocess.run(
            ["python3", str(SCORER), str(candidate)],
            text=True,
            capture_output=True,
            check=False,
        )
        return completed, json.loads(completed.stdout)

    def test_complete_clean_candidate_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            completed, result = self.score(self.make_candidate(Path(tmp)))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(result["passed"])
        self.assertTrue(result["all_run_exits_zero"])
        self.assertTrue(result["evidence_sha256_valid"])
        self.assertTrue(result["counter_gate_passed"])
        self.assertTrue(result["release_correlation_passed"])
        self.assertEqual(len(result["runs"]), 6)

    def test_retrans_increment_fails_counter_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            completed, result = self.score(
                self.make_candidate(Path(tmp), retrans_delta=1)
            )
        self.assertEqual(completed.returncode, 1)
        self.assertFalse(result["counter_gate_passed"])
        self.assertEqual(result["counter_violations"][0]["delta"], 1)

    def test_nonzero_exit_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            completed, result = self.score(self.make_candidate(Path(tmp), bad_exit=True))
        self.assertEqual(completed.returncode, 1)
        self.assertFalse(result["all_run_exits_zero"])

    def test_bad_sha256_seal_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            completed, result = self.score(self.make_candidate(Path(tmp), bad_seal=True))
        self.assertEqual(completed.returncode, 1)
        self.assertFalse(result["evidence_sha256_valid"])


if __name__ == "__main__":
    unittest.main()
