import json
import pathlib
import sys
import tempfile
import unittest


CUSTOM_NODES = pathlib.Path(__file__).resolve().parents[2] / "custom_nodes"
sys.path.insert(0, str(CUSTOM_NODES))

from dgx_remote_h3.contract import ContractError, load_runtime_contract


PRODUCTION = {
    "schema_id": "dgx-h3-production-v1",
    "profile": "production",
    "endpoint_id": "h3-head-lan-v1",
    "endpoint": "http://192.0.2.30:18081/v1/videos/sync",
    "secret_path": "/run/dgx-h3-secret/token",
    "connect_timeout_seconds": 10,
    "total_timeout_seconds": 3700,
    "max_response_bytes": 268435456,
}


class ContractTests(unittest.TestCase):
    def _load(self, document, *, expected_profile="production"):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "runtime.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            return load_runtime_contract(path, expected_profile=expected_profile)

    def test_accepts_only_exact_production_contract(self):
        contract = self._load(PRODUCTION)
        self.assertEqual(contract.endpoint_host, "192.0.2.30")
        self.assertEqual(contract.endpoint_port, 18081)
        self.assertEqual(contract.endpoint_path, "/v1/videos/sync")
        self.assertEqual(contract.secret_path, pathlib.Path("/run/dgx-h3-secret/token"))
        self.assertEqual(contract.max_response_bytes, 256 * 1024 * 1024)

    def test_accepts_exact_noninteroperable_test_contract_only_when_explicit(self):
        test_contract = dict(
            PRODUCTION,
            schema_id="dgx-h3-test-v1",
            profile="test",
            endpoint_id="fake-gateway-v1",
            endpoint="http://fake-gateway:19081/v1/videos/sync",
            connect_timeout_seconds=2,
            total_timeout_seconds=5,
        )
        contract = self._load(test_contract, expected_profile="test")
        self.assertEqual((contract.endpoint_host, contract.endpoint_port), ("fake-gateway", 19081))
        self.assertEqual(contract.connect_timeout_seconds, 2)
        self.assertEqual(contract.total_timeout_seconds, 5)
        with self.assertRaises(ContractError):
            self._load(test_contract, expected_profile="production")

    def test_rejects_missing_extra_drifted_or_malformed_contract(self):
        mutations = []
        for key in PRODUCTION:
            changed = dict(PRODUCTION)
            del changed[key]
            mutations.append(changed)
        mutations.extend(
            [
                dict(PRODUCTION, extra="not-allowed"),
                dict(PRODUCTION, endpoint="http://127.0.0.1:18081/v1/videos/sync"),
                dict(PRODUCTION, endpoint="https://192.0.2.30:18081/v1/videos/sync"),
                dict(PRODUCTION, secret_path="/tmp/token"),
                dict(PRODUCTION, connect_timeout_seconds=True),
                dict(PRODUCTION, total_timeout_seconds=3699),
                dict(PRODUCTION, max_response_bytes=1),
            ]
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(ContractError):
                self._load(mutation)

    def test_rejects_invalid_json_without_returning_source_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "runtime.json"
            path.write_text('{"secret":"do-not-echo"', encoding="utf-8")
            with self.assertRaises(ContractError) as raised:
                load_runtime_contract(path)
            self.assertNotIn("do-not-echo", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
