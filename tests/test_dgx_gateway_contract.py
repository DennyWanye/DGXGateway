import http.client
import json
import os
import subprocess
import tempfile
import threading
import unittest
from types import SimpleNamespace
from pathlib import Path


class GatewayDomainContractTest(unittest.TestCase):
    def test_active_connection_uses_legacy_deepseek_key_and_exact_protocol(self):
        from DGXGateway.dgx_gateway.connection_view import active_connection_view
        from DGXGateway.dgx_gateway.domain import ActiveModelClassification

        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "gateway.token"
            secret.write_text("fixture-" + "a" * 56 + "\n", encoding="utf-8")
            os.chmod(secret, 0o600)
            config = SimpleNamespace(
                deepseek_gateway_secret=secret,
                bind="192.0.2.37",
                music3_port=8188,
                nodes=(SimpleNamespace(role="head", address="192.0.2.30"),),
            )
            vision = active_connection_view(
                config,
                ActiveModelClassification("ACTIVE", "deepseek-v4-flash-vision-exp"),
            )
            self.assertEqual(vision["base_url"], "http://192.0.2.30:11434/v1")
            self.assertEqual(vision["model_name"], "deepseek-v4-flash-vision-exp")
            self.assertEqual(vision["request_path"], "/chat/completions")
            self.assertEqual(vision["api_key"], "fixture-" + "a" * 56)

            h3 = active_connection_view(
                config,
                ActiveModelClassification("ACTIVE", "minimax-h3", "10eros-fl2va-v1"),
            )
            self.assertEqual(h3["base_url"], "http://192.0.2.30:18081/v1")
            self.assertTrue(h3["model_name"].endswith("minimax-h3-10eros-fl2va-v1"))
            self.assertEqual(h3["request_path"], "/videos/sync")
            self.assertEqual(h3["api_key"], vision["api_key"])

            for invalid_profile in (None, "future-profile", "../injected"):
                invalid_h3 = active_connection_view(
                    config,
                    ActiveModelClassification("ACTIVE", "minimax-h3", invalid_profile),
                )
                self.assertFalse(invalid_h3["available"])
                self.assertNotIn("api_key", invalid_h3)

            starting = active_connection_view(
                config,
                ActiveModelClassification("ACTIVE", "deepseek-v4-flash"),
                observed_state="STARTING",
            )
            self.assertFalse(starting["available"])
            self.assertNotIn("api_key", starting)

            music = active_connection_view(
                config,
                ActiveModelClassification("ACTIVE", "minimax-music3"),
            )
            self.assertEqual(music["base_url"], "http://192.0.2.30:8188")
            self.assertFalse(music["api_key_required"])
            self.assertEqual(music["api_key"], "")

            os.chmod(secret, 0o644)
            unavailable = active_connection_view(
                config,
                ActiveModelClassification("ACTIVE", "deepseek-v4-flash"),
            )
            self.assertFalse(unavailable["available"])
            self.assertNotIn("api_key", unavailable)

            os.chmod(secret, 0o600)
            link = Path(directory) / "linked.token"
            link.symlink_to(secret)
            config.deepseek_gateway_secret = link
            linked = active_connection_view(
                config,
                ActiveModelClassification("ACTIVE", "deepseek-v4-flash"),
            )
            self.assertFalse(linked["available"])
            self.assertNotIn("api_key", linked)

    def test_ui_core_node_contract(self):
        root = Path(__file__).parents[1]
        result = subprocess.run(
            ["node", str(root / "tests/test-dgx-gateway-ui.mjs")],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_gateway_start_keeps_mutations_fenced_until_http_readiness(self):
        root = Path(__file__).parents[1]
        script = (root / "DGXGateway/manage.sh").read_text(encoding="utf-8")
        fence_on = script.index('set-mutation-fence --config "$CONFIG_PATH" --enabled true')
        readiness = script.index('curl -fsS --max-time 12 "$READY_URL"')
        fence_off = script.index('set-mutation-fence --config "$CONFIG_PATH" --enabled false', readiness)
        self.assertLess(fence_on, readiness)
        self.assertLess(readiness, fence_off)
        self.assertIn("mutation remains fenced", script)

    def test_gateway_already_running_readiness_failure_stays_fenced(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            fake_bin = temporary / "bin"
            fake_bin.mkdir()
            calls = temporary / "python-calls.txt"
            launchctl = fake_bin / "launchctl"
            launchctl.write_text(
                "#!/bin/sh\n[ \"$1\" = print ] && { echo 'state = running'; exit 0; }\nexit 0\n",
                encoding="utf-8",
            )
            curl = fake_bin / "curl"
            curl.write_text("#!/bin/sh\nexit 22\n", encoding="utf-8")
            python = fake_bin / "python3"
            python.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CALL_LOG\"\nexit 0\n", encoding="utf-8")
            for executable in (launchctl, curl, python):
                executable.chmod(0o755)
            config = temporary / "gateway.toml"
            token = temporary / "admin.token"
            state = temporary / "state"
            config.write_text("fixture", encoding="utf-8")
            token.write_text("x" * 40, encoding="utf-8")
            environment = dict(os.environ)
            environment.update({
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "CALL_LOG": str(calls),
                "DGX_GATEWAY_CONFIG": str(config),
                "DGX_GATEWAY_TOKEN_PATH": str(token),
                "DGX_GATEWAY_STATE_DIR": str(state),
                "DGX_GATEWAY_READY_URL": "http://127.0.0.1:9/api/v1/snapshot",
            })
            result = subprocess.run(
                ["/bin/sh", str(root / "DGXGateway/manage.sh"), "start"],
                text=True, capture_output=True, env=environment,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("mutation remains fenced", result.stderr)
            logged = calls.read_text(encoding="utf-8")
            self.assertIn("set-mutation-fence", logged)
            self.assertIn("--enabled true", logged)
            self.assertNotIn("--enabled false", logged)

    def test_snapshot_session_flag_is_request_scoped_and_admin_secret_free(self):
        from DGXGateway.dgx_gateway.server import GatewayApplication, GatewayHTTPServer

        class Snapshot:
            providers = ()

            def build(self):
                return {"schema_version": 1, "lock": {"state": "UNLOCKED"}}

        class Store:
            pass

        class Operations:
            adapters = {}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = root / "admin.token"
            token.write_text("x" * 40, encoding="utf-8")
            os.chmod(token, 0o600)
            app = GatewayApplication(
                config=SimpleNamespace(
                    admin_token_file=token,
                    allowed_origins=("http://gateway.test",),
                ),
                store=Store(), operations=Operations(), snapshot=Snapshot(),
                static_dir=root, admission=object(),
            )
            server = GatewayHTTPServer(("127.0.0.1", 0), app)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                connection.request("GET", "/api/v1/snapshot")
                anonymous = json.loads(connection.getresponse().read())
                self.assertEqual(anonymous["session"], {"authenticated": False, "lan_no_auth": False})
                connection.close()

                body = json.dumps({"token": "x" * 40})
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                connection.request("POST", "/api/v1/session", body=body, headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                    "Origin": "http://gateway.test",
                })
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                cookie = response.getheader("Set-Cookie").split(";", 1)[0]
                response.read()
                connection.close()

                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                connection.request("GET", "/api/v1/snapshot", headers={"Cookie": cookie})
                authenticated = json.loads(connection.getresponse().read())
                self.assertEqual(authenticated["session"], {"authenticated": True, "lan_no_auth": False})
                serialized = json.dumps(authenticated)
                self.assertNotIn(cookie.split("=", 1)[1], serialized)
                self.assertNotIn("x" * 40, serialized)
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
    def test_operation_view_covers_canonical_branches_and_is_monotonic(self):
        from DGXGateway.dgx_gateway.operation_view import operation_view

        def view(phase, outcome=None, error=None):
            return operation_view(SimpleNamespace(
                operation_id="op-1",
                source_model="minimax-h3",
                target_model="deepseek-v4-flash",
                target_profile=None,
                idempotency_key="hidden",
                phase=phase,
                outcome=outcome,
                cancel_requested=False,
                created_at="2026-09-03T00:00:00Z",
                updated_at="2026-09-03T00:00:01Z",
                error=error,
                to_dict=lambda: {},
            ))

        success_trace = [
            ("REQUESTED", None), ("PREFLIGHT", None),
            ("LEASE_ACQUIRED", None), ("SOURCE_DRAINING", None),
            ("SOURCE_STOPPING", None), ("SOURCE_STOPPED_PROVEN", None),
            ("TARGET_STARTING", None), ("TARGET_HEALTH", None),
            ("TARGET_HEALTHY", "COMMITTED"), ("RELEASING", "COMMITTED"),
            ("RELEASED", "COMMITTED"),
        ]
        progress = [view(phase, outcome)["progress"]["percent"] for phase, outcome in success_trace]
        self.assertEqual(progress, sorted(progress))
        committed = view("RELEASED", "COMMITTED")
        self.assertEqual(committed["progress"]["tone"], "success")
        self.assertTrue(committed["progress"]["terminal"])
        self.assertEqual(committed["progress"]["percent"], 100)

        rollback_trace = [
            ("TARGET_STARTING", None), ("TARGET_HEALTH", None),
            ("TARGET_HEALTHY", None), ("TARGET_RETIRING", None),
            ("TARGET_STOPPED_PROVEN", None), ("ROLLBACK_STARTING", None),
            ("SOURCE_HEALTHY_AND_OTHERS_STOPPED_PROVEN", "ROLLED_BACK"),
            ("RELEASING", "ROLLED_BACK"), ("RELEASED", "ROLLED_BACK"),
        ]
        rollback_progress = [view(phase, outcome)["progress"]["percent"] for phase, outcome in rollback_trace]
        self.assertEqual(rollback_progress, sorted(rollback_progress))
        self.assertEqual(view("RELEASED", "ROLLED_BACK")["progress"]["tone"], "rollback")
        self.assertEqual(view("RELEASED", "FAILED_SAFE_STOPPED")["progress"]["tone"], "danger")
        self.assertEqual(view("CANCELED", "CANCELED")["progress"]["tone"], "neutral")

        for frozen_outcome in (None, "RECOVERY_REQUIRED", "COMMITTED", "ROLLED_BACK", "FAILED_SAFE_STOPPED", "CANCELED"):
            recovery = view("RECOVERY_REQUIRED", frozen_outcome, "private detail")
            self.assertEqual(recovery["error"], "operation_failed")
            self.assertEqual(recovery["progress"]["tone"], "critical")
            self.assertTrue(recovery["progress"]["terminal"])
            self.assertFalse(recovery["progress"]["known"] is False)

        unknown = view("FUTURE_PHASE", None)
        self.assertEqual(unknown["progress"]["tone"], "critical")
        self.assertFalse(unknown["progress"]["known"])
        self.assertFalse(unknown["progress"]["terminal"])

    def test_public_operation_never_exposes_idempotency_key_or_private_error(self):
        from DGXGateway.dgx_gateway.operation_view import operation_view

        item = SimpleNamespace(
            operation_id="op-secret",
            source_model="music-3",
            target_model="minimax-h3",
            target_profile="heretic-ref2va-v1",
            idempotency_key="must-not-leak",
            phase="TARGET_STARTING",
            outcome=None,
            cancel_requested=False,
            created_at="2026-09-03T00:00:00Z",
            updated_at="2026-09-03T00:00:01Z",
            error="private stack trace",
        )
        public = operation_view(item)
        self.assertNotIn("idempotency_key", public)
        self.assertEqual(public["error"], "operation_failed")
        self.assertEqual(public["progress"]["message"], "正在启动目标模型")

    def test_lan_ui_does_not_require_secure_context_random_uuid(self):
        root = Path(__file__).parents[1]
        script = (root / "DGXGateway/static/app.js").read_text()
        page = (root / "DGXGateway/static/index.html").read_text()
        self.assertNotIn("crypto.randomUUID", script)
        self.assertIn("crypto.getRandomValues", script)
        self.assertIn("/app.js?v=", page)
        self.assertIn("/ui-core.js?v=", page)
        self.assertNotIn("setInterval(refresh", script)
        self.assertIn("state.pollInFlight", script)
        self.assertIn("setTimeout(refresh, delay)", script)
        self.assertIn('role="progressbar"', script)
        self.assertIn('aria-live="polite"', page)
        self.assertIn('id="telemetry-chart"', page)
        self.assertNotIn("https://", page)

    def test_example_config_uses_fixed_non_conflicting_port_and_roles(self):
        from DGXGateway.dgx_gateway.config import load_config

        root = Path(__file__).parents[1]
        config = load_config(root / "DGXGateway/config/gateway.example.toml")
        self.assertEqual(config.port, 18082)
        self.assertEqual(config.h3_queue_url, "http://192.0.2.41:8188/queue")
        self.assertEqual(
            [node.hostname for node in config.nodes],
            ["dgx-head", "dgx-worker", "dgx-rank2", "dgx-rank3"],
        )
        self.assertEqual(
            [(node.role, node.address, node.user) for node in config.nodes],
            [
                ("head", "192.0.2.41", "operator"),
                ("worker", "192.0.2.42", "operator"),
                ("rank2", "192.0.2.39", "operator"),
                ("rank3", "192.0.2.40", "operator"),
            ],
        )
        from DGXGateway.dgx_gateway.config import paired_nodes

        self.assertEqual(
            [node.role for node in paired_nodes(config.nodes)],
            ["head", "worker"],
        )

    def test_active_model_classification_is_fail_closed(self):
        from DGXGateway.dgx_gateway.domain import ObservedModel, classify_local_models

        self.assertEqual(classify_local_models([]).state, "NONE")
        self.assertEqual(
            classify_local_models([ObservedModel("h3", "HEALTHY")]).model_id,
            "h3",
        )
        self.assertEqual(
            classify_local_models([
                ObservedModel("h3", "HEALTHY"),
                ObservedModel("deepseek-v4-flash", "HEALTHY"),
            ]).state,
            "CONFLICT",
        )
        self.assertEqual(
            classify_local_models([ObservedModel("h3", "UNKNOWN")]).state,
            "AMBIGUOUS",
        )

    def test_operation_outcome_is_separate_and_immutable(self):
        from DGXGateway.dgx_gateway.store import OperationStore

        with tempfile.TemporaryDirectory() as directory:
            store = OperationStore(Path(directory) / "gateway.sqlite3")
            op = store.create_operation("op-1", "deepseek-v4-flash", "h3", "key-1")
            self.assertEqual(op.phase, "REQUESTED")
            self.assertIsNone(op.outcome)
            store.set_phase("op-1", "TARGET_HEALTHY")
            store.freeze_outcome("op-1", "COMMITTED")
            store.set_phase("op-1", "RELEASED")
            final = store.get_operation("op-1")
            self.assertEqual(final.phase, "RELEASED")
            self.assertEqual(final.outcome, "COMMITTED")
            with self.assertRaises(ValueError):
                store.freeze_outcome("op-1", "ROLLED_BACK")

    def test_idempotency_key_returns_original_operation(self):
        from DGXGateway.dgx_gateway.store import OperationStore

        with tempfile.TemporaryDirectory() as directory:
            store = OperationStore(Path(directory) / "gateway.sqlite3")
            first = store.create_operation("op-1", "h3", "deepseek-v4-flash", "same-key")
            second = store.create_operation("op-2", "h3", "deepseek-v4-flash", "same-key")
            self.assertEqual(first.operation_id, second.operation_id)


if __name__ == "__main__":
    unittest.main()
