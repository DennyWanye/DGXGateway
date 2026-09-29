import importlib.util
import json
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/deepseek-v4-flash-vision-exp/replicate-official-model.py"
SPEC = importlib.util.spec_from_file_location("replicate_official", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class ReplicationContractTests(unittest.TestCase):
    def test_plan_is_exact_nul_allowlist_over_pinned_fabric_ssh(self):
        _, paths = MODULE.load_manifest(
            ROOT / "manifests/deepseek-v4-flash-vision-exp-official-exact.json"
        )
        plan = MODULE.plan(paths)
        command = plan["rsync"]
        self.assertEqual(plan["paths"], 84)
        for option in ("--partial", "--relative", "--from0", "--files-from=-", "--protect-args"):
            self.assertIn(option, command)
        transport = command[command.index("-e") + 1]
        self.assertIn("/home/operator/.ssh/deepseek-v4-flash-worker", transport)
        self.assertIn("StrictHostKeyChecking=yes", transport)
        self.assertIn("UserKnownHostsFile=/home/operator/.ssh/deepseek-v4-flash-worker-known_hosts", transport)
        self.assertEqual(command[-1], "operator@192.0.2.34:" + MODULE.WORKER_STAGING + "/")
        self.assertEqual(len(plan["allowlist_sha256"]), 64)

    def test_emit_plan_is_read_only_and_deterministic(self):
        first = subprocess.run(["python3", str(SCRIPT), "--emit-plan"], check=True, text=True,
                               stdout=subprocess.PIPE).stdout
        second = subprocess.run(["python3", str(SCRIPT), "--emit-plan"], check=True, text=True,
                                stdout=subprocess.PIPE).stdout
        self.assertEqual(first, second)
        self.assertEqual(json.loads(first)["paths"], 84)

    def test_closure_parser_requires_machine_readable_hash(self):
        digest = "a" * 64
        self.assertEqual(MODULE.parse_closure(json.dumps({"root_closure_sha256": digest})), digest)
        with self.assertRaises(MODULE.ReplicationError):
            MODULE.parse_closure("VERIFY_PASS without closure")


if __name__ == "__main__":
    unittest.main()
