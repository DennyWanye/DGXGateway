import tempfile
import unittest
from pathlib import Path


class GatewayLeaseTest(unittest.TestCase):
    def test_single_outer_lease_guards_and_releases_with_operation_identity(self):
        from DGXGateway.dgx_gateway.authority import GatewayLease

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            lease = GatewayLease(
                lock_cli=Path(__file__).parents[1] / "scripts" / "dgx-spark-deployment-lock.py",
                lock_root=base / "lock",
                token_dir=base / "tokens",
                operation_id="op-switch-1",
                purpose="exclusive model switch",
                lease_seconds=300,
            )
            acquired = lease.acquire()
            self.assertEqual(acquired["owner"], "dgx-cluster-gateway")
            self.assertEqual(lease.guard()["lease_generation"], acquired["lease_generation"])
            released = lease.release()
            self.assertEqual(released["state"], "UNLOCKED")
            self.assertEqual(released["release_operation_id"], "op-switch-1")
            self.assertFalse(lease.token_path.exists())

    def test_second_gateway_lease_is_refused(self):
        from DGXGateway.dgx_gateway.authority import GatewayLease, LeaseError

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            common = dict(
                lock_cli=Path(__file__).parents[1] / "scripts" / "dgx-spark-deployment-lock.py",
                lock_root=base / "lock",
                token_dir=base / "tokens",
                purpose="exclusive model switch",
                lease_seconds=300,
            )
            first = GatewayLease(operation_id="op-1", **common)
            second = GatewayLease(operation_id="op-2", **common)
            first.acquire()
            with self.assertRaises(LeaseError):
                second.acquire()
            first.release()


if __name__ == "__main__":
    unittest.main()
