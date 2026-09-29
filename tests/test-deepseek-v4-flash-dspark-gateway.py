#!/usr/bin/env python3
import importlib.util
import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_harness():
    path = ROOT / "scripts/test-deepseek-v4-flash-dspark.py"
    spec = importlib.util.spec_from_file_location("deepseek_harness", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def controller_module():
    path = ROOT / "scripts/control-deepseek-v4-flash-dspark.py"
    spec = importlib.util.spec_from_file_location("deepseek_controller_gateway", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class GatewayConfigTests(unittest.TestCase):
    def setUp(self):
        self.conf = (ROOT / "gateway/deepseek-v4-flash-nginx.conf").read_text()
        self.unit = (ROOT / "systemd/deepseek-v4-flash-gateway.service").read_text()

    def test_public_listener_and_loopback_upstream_are_exact(self):
        self.assertIn("listen 192.0.2.30:11434", self.conf)
        self.assertNotIn("listen 0.0.0.0", self.conf)
        self.assertNotIn("listen 18888", self.conf)
        self.assertIn("server 127.0.0.1:18888", self.conf)

    def test_route_and_method_allowlist_is_closed(self):
        for route in (
            "/health",
            "/v1/models",
            "/v1/chat/completions",
            "/v1/responses",
        ):
            self.assertIn(f"location = {route}", self.conf)
        self.assertIn("location /", self.conf)
        self.assertIn("return 404", self.conf)
        self.assertGreaterEqual(self.conf.count("return 405"), 4)

    def test_bearer_is_external_and_logs_cannot_capture_it(self):
        self.assertIn("map_hash_bucket_size 128", self.conf)
        self.assertIn(
            "include /run/secrets/deepseek-v4-flash-gateway-auth.conf", self.conf
        )
        self.assertNotIn("$http_authorization", self.conf)
        self.assertNotIn("$request_header", self.conf)
        self.assertIn("if ($deepseek_auth_ok = 0)", self.conf)
        self.assertIn("gateway-secret.conf:/run/secrets/", self.unit)
        self.assertIn("test \"$(stat -c %%a", self.unit)
        self.assertIn("test \"$(stat -c %%u", self.unit)
        self.assertIn('= \"600\"', self.unit)

    def test_sse_and_resource_bounds_are_explicit(self):
        for directive in (
            "proxy_buffering off",
            "proxy_cache off",
            "proxy_request_buffering off",
            "proxy_http_version 1.1",
            "client_max_body_size 32m",
            "proxy_read_timeout 1200s",
        ):
            self.assertIn(directive, self.conf)

    def test_rejection_body_health_json_and_core_method_gate_are_explicit(self):
        staging = (ROOT / "gateway/deepseek-v4-flash-nginx-staging.conf").read_text()
        for conf in (self.conf, staging):
            self.assertIn("error_page 401 = @deepseek_unauthorized;", conf)
            self.assertIn("location @deepseek_unauthorized", conf)
            self.assertIn("return 401 '{\"error\":\"unauthenticated\"}';", conf)
            self.assertIn("error_page 405 = @deepseek_method_gate;", conf)
            self.assertIn("location @deepseek_method_gate", conf)
            self.assertGreaterEqual(
                conf.count("return 401 '{\"error\":\"unauthenticated\"}';"), 2
            )
            self.assertIn("proxy_hide_header Content-Type;", conf)
            self.assertIn("add_header Content-Type application/json always;", conf)

    def test_nginx_accepts_rendered_syntax(self):
        nginx = shutil.which("nginx")
        if not nginx:
            self.skipTest("nginx is unavailable for a local syntax check")
        rendered = self.conf.replace(
            "include /run/secrets/deepseek-v4-flash-gateway-auth.conf;",
            'map $http_authorization $deepseek_auth_ok { default 0; "Bearer syntax-only-fixture" 1; }',
        ).replace(
            "pid /var/run/nginx.pid;", "pid logs/nginx.pid;"
        ).replace(
            "listen 192.0.2.30:11434;", "listen 127.0.0.1:19434;"
        ).replace(
            "access_log /var/log/nginx/access.log deepseek;", "access_log off;"
        ).replace(
            "error_log /var/log/nginx/error.log warn;", "error_log stderr warn;"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / "logs").mkdir()
            config = root / "nginx.conf"
            config.write_text(rendered)
            result = subprocess.run(
                [nginx, "-t", "-p", str(root), "-c", str(config), "-e", "stderr"],
                text=True,
                capture_output=True,
                timeout=10,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_gateway_unit_is_manual_host_network_and_digest_pinned(self):
        self.assertIn("--network host", self.unit)
        self.assertIn("--read-only", self.unit)
        self.assertIn(
            "docker.io/library/nginx@sha256:77d740efa8f9c4753f2a7212d8422b8c77411681971f400ea03d07fe38476cac",
            self.unit,
        )
        self.assertIn("Restart=no", self.unit)
        self.assertNotIn("WantedBy=", self.unit)
        self.assertIn(
            "ConditionPathExists=/run/deepseek-v4-flash-dspark/gateway-authorized.pending",
            self.unit,
        )
        self.assertIn(
            "gateway-authorized.pending /run/deepseek-v4-flash-dspark/gateway-authorized.running",
            self.unit,
        )

    def test_staging_config_diff_is_only_the_loopback_listener(self):
        staging = (ROOT / "gateway/deepseek-v4-flash-nginx-staging.conf").read_text()
        normalized = staging.replace(
            "listen 127.0.0.1:11435;", "listen 192.0.2.30:11434;"
        )
        self.assertEqual(normalized, self.conf)


class ConsumerAndHarnessContractTests(unittest.TestCase):
    def test_gateway_matrix_uses_exact_negative_statuses_and_root_base_url(self):
        harness = load_harness()
        route_matrix, route_digest = harness._locked_json(harness.API_ROUTE_MATRIX)
        status_tables = {
            "allowed": route_matrix["allowed_status"],
            "denied": route_matrix["denied_status"],
            "method-denied": route_matrix["method_denied_status"],
        }

        class MatrixClient:
            def __init__(self):
                self.calls = []

            def request(self, method, path, body=None, *, token=harness._DEFAULT_TOKEN, accept=None, request_id=None):
                credential = "correct" if token is harness._DEFAULT_TOKEN else "missing" if token is None else "wrong"
                matching = [row for row in route_matrix["rows"] if row["method"] == method and row["path"] == path]
                self.assert_unique(matching, method, path)
                row = next(
                    (item for item in matching if item["mode"] == ("sse" if accept == "text/event-stream" else "json")),
                    matching[0],
                )
                self.calls.append({"row": row, "credential": credential, "body": body, "accept": accept, "request_id": request_id})
                content_type = "text/event-stream" if row["mode"] == "sse" else "application/json"
                return status_tables[row["category"]][credential], b"{}", content_type

            @staticmethod
            def assert_unique(rows, method, path):
                if not rows:
                    raise AssertionError(f"unfrozen request {method} {path}")
                categories = {row["category"] for row in rows}
                modes = {row["mode"] for row in rows}
                if len(categories) != 1 or (len(rows) > 1 and modes != {"json", "sse"}):
                    raise AssertionError(f"ambiguous frozen request {method} {path}")

        client = MatrixClient()
        result = harness.gateway_matrix(client, "model")
        self.assertTrue(result["passed"])
        self.assertEqual(result["route_matrix_sha256"], route_digest)
        self.assertEqual(result["route_rows_executed"], len(route_matrix["rows"]))
        self.assertEqual(result["requests_executed"], len(route_matrix["rows"]) * 3)
        self.assertEqual(len(client.calls), len(route_matrix["rows"]) * 3)
        self.assertEqual(
            set(route_matrix.get("method_universe", [])),
            {"HEAD", "OPTIONS", "GET", "POST", "PUT", "PATCH", "DELETE", "TRACE"},
        )
        self.assertEqual(
            {row["method"] for row in route_matrix["rows"]},
            {"HEAD", "OPTIONS", "GET", "POST", "PUT", "PATCH", "DELETE", "TRACE"},
        )
        negative = [row for row in result["matrix"] if "expected_status" in row]
        self.assertTrue(all(row["status"] == row["expected_status"] for row in negative))
        rejected = [row for row in negative if row["expected_status"] != 200]
        self.assertTrue(all(row["expected_upstream_hits"] == 0 for row in rejected))
        self.assertEqual(len({row["request_id"] for row in rejected}), len(rejected))
        for call in client.calls:
            if call["row"]["mode"] == "sse" and call["row"]["category"] == "allowed":
                self.assertEqual(call["accept"], "text/event-stream")
                self.assertIs(call["body"]["stream"], True)
            if call["row"]["method"] in {"POST", "PUT", "PATCH"}:
                self.assertIsInstance(call["body"], dict)
            else:
                self.assertIsNone(call["body"])
        logs = "\n".join(
            f'GET / denied rid="{row["request_id"]}" upstream="-/-"'
            for row in rejected
        )
        verified = controller_module().validate_gateway_rejection_logs(result, logs)
        self.assertTrue(all(row["actual_upstream_hits"] == 0 for row in verified["matrix"] if "expected_upstream_hits" in row))
        source = (ROOT / "scripts/test-deepseek-v4-flash-dspark.py").read_text()
        self.assertIn('default="http://192.0.2.30:11434"', source)
        self.assertNotIn('default="http://192.0.2.30:11434/v1"', source)
        self.assertIn('rid="$http_x_request_id"', (ROOT / "gateway/deepseek-v4-flash-nginx.conf").read_text())
        self.assertNotIn("$http_authorization", (ROOT / "gateway/deepseek-v4-flash-nginx.conf").read_text().split("log_format deepseek", 1)[1].split(";", 1)[0])

    def test_consumers_are_finite_and_secrets_stay_external(self):
        manifest = json.loads(
            (ROOT / "manifests/deepseek-v4-flash-consumers.json").read_text()
        )
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["base_url"], "http://192.0.2.30:11434/v1")
        consumers = {item["id"]: item for item in manifest["consumers"]}
        self.assertEqual(set(consumers), {"mac-codex", "repo-smoke", "windows-codex"})
        self.assertEqual(consumers["windows-codex"]["management"], "observed-only")
        for name in ("mac-codex", "repo-smoke"):
            ref = consumers[name]["secret_file"]
            self.assertTrue(ref.startswith("/Users/operator/.config/"))
            self.assertEqual(consumers[name]["secret_mode"], "0600")
        self.assertNotIn("secret_file", consumers["windows-codex"])

    def test_frozen_scenarios_context_and_performance_gates(self):
        harness = load_harness()
        self.assertEqual(tuple(harness.SCENARIOS), ("DS-S1", "DS-S2", "DS-S3", "DS-S4", "DS-S5"))
        self.assertEqual(harness.CONTEXT_TARGETS, (32768, 131072, 262144, 524288, 1048000))
        self.assertEqual(harness.MIN_OUTPUT_HEADROOM, 256)
        self.assertEqual(harness.SINGLE_STREAM_MIN_TPS, 55.0)
        self.assertEqual(harness.SIX_STREAM_MIN_TPS, 140.0)

    def test_context_recall_accepts_output_cap_after_sentinel_was_recalled(self):
        harness = load_harness()
        self.assertTrue(
            harness.context_result_passed(
                {
                    "status": "incomplete",
                    "incomplete_reason": "max_output_tokens",
                    "sentinel_recalled": True,
                }
            )
        )

    def test_context_capacity_probe_uses_deterministic_sampling(self):
        harness = load_harness()

        class Client:
            timeout = 30
            request_body = None

            def json(self, path, body):
                self.request_body = body
                return {
                    "status": "completed",
                    "output_text": "DEEPSEEK_CONTEXT_32768_A91F",
                    "usage": {"input_tokens": 32299},
                }

        client = Client()
        with mock.patch.object(harness, "CONTEXT_TARGETS", (32768,)), mock.patch.object(
            harness,
            "render_context",
            return_value=("content", "DEEPSEEK_CONTEXT_32768_A91F", 32295),
        ):
            harness.context_gate(client, "model", "http://internal")
        self.assertEqual(client.request_body["temperature"], 0)
        self.assertFalse(
            harness.context_result_passed(
                {
                    "status": "incomplete",
                    "incomplete_reason": "max_output_tokens",
                    "sentinel_recalled": False,
                }
            )
        )
        self.assertFalse(
            harness.context_result_passed(
                {
                    "status": "incomplete",
                    "incomplete_reason": "unknown",
                    "sentinel_recalled": True,
                }
            )
        )

    def test_missing_credential_really_omits_authorization_header(self):
        harness = load_harness()
        observed = []

        class Response:
            status = 200
            headers = {"Content-Type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return b"{}"

        def fake_urlopen(request, timeout):
            observed.append(request)
            return Response()

        client = harness.Client("http://127.0.0.1:1", "real-secret", 1)
        with mock.patch.object(harness.urllib.request, "urlopen", fake_urlopen):
            client.request("GET", "/health", token=None)
        self.assertNotIn("Authorization", observed[0].headers)

    def test_secret_reader_requires_exact_mode_0600(self):
        harness = load_harness()
        with tempfile.TemporaryDirectory() as temporary:
            secret = pathlib.Path(temporary) / "token"
            secret.write_text("syntax-only-token\n")
            secret.chmod(0o644)
            with self.assertRaises(ValueError):
                harness.read_secret(secret)
            secret.chmod(0o600)
            self.assertEqual(harness.read_secret(secret), "syntax-only-token")

    def test_cutover_history_cannot_disarm_before_all_gates(self):
        harness = load_harness()
        history = list(harness.PRODUCTION_STATES)
        self.assertLess(history.index("PRODUCTION_MONITOR_PASSED"), history.index("COMMITTING"))
        self.assertLess(history.index("WORKER_REBOOT_PASSED"), history.index("COMMITTING"))
        self.assertLess(history.index("HEAD_REBOOT_PASSED"), history.index("COMMITTING"))
        self.assertLess(history.index("CLIENTS_VERIFIED"), history.index("COMMITTING"))
        self.assertEqual(history[-2:], ["FALLBACK_DISARMED", "VERIFIED"])
        harness.validate_state_history(history)
        with self.assertRaises(ValueError):
            harness.validate_state_history(
                ["PREPARED", "FALLBACK_ARMED", "FALLBACK_DISARMED", "VERIFIED"]
            )

    def test_testcase_inventory_preserves_required_obligations(self):
        inventory = json.loads(
            (ROOT / "testcase/deepseek-v4-flash-dspark/index.json").read_text()
        )
        obligations = {
            obligation
            for case in inventory["testcases"]
            for obligation in case["obligations"]
        }
        self.assertTrue(
            {
                "TO-DS-D5",
                "TO-DS-D6",
                "TO-DS-D7",
                "TO-DS-D8",
                "TO-DS-R3",
                "TO-DS-R4",
                "TO-DS-R5",
            }.issubset(obligations)
        )
        spec = json.loads(
            (
                ROOT
                / "plans/deepseek-v4-flash-dspark-2node/verification-prep/verification-spec.json"
            ).read_text()
        )
        scenarios = {row["scenario_id"]: row for row in spec["scenarios"]}
        staged_contract = spec["manifest"]["live_finalization_contract"]
        stage_a_required = {
            scenario_id
            for scenario_id, scenario in scenarios.items()
            if scenario["required"]
        }
        stage_a_allowed = set(staged_contract["stage_a"]["allowed_scenarios"])
        stage_b_required = set(staged_contract["stage_b"]["required_scenarios"])
        self.assertEqual(stage_a_required, {"DS-V-LOCK", "DS-V-ARTIFACT"})
        self.assertEqual(stage_a_allowed, stage_a_required)
        self.assertEqual(stage_b_required, set(scenarios) - stage_a_required)
        self.assertEqual(len(stage_b_required), 10)
        combined = "\n".join(
            path.read_text()
            for path in sorted((ROOT / "testcase/deepseek-v4-flash-dspark").glob("*.md"))
        )
        for scenario_id in ("DS-S1", "DS-S2", "DS-S3", "DS-S4", "DS-S5"):
            self.assertIn(scenario_id, scenarios)
            self.assertIn(scenario_id, stage_b_required)
            self.assertTrue(scenarios[scenario_id]["testcase_ids"])
            for testcase_id in scenarios[scenario_id]["testcase_ids"]:
                self.assertIn(f"id: {testcase_id}", combined)
            self.assertIn(scenario_id, combined)


if __name__ == "__main__":
    unittest.main()
