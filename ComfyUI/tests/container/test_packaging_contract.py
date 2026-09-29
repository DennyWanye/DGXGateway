from __future__ import annotations

import json
import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]


def required_text(relative: str) -> str:
    path = ROOT / relative
    if not path.is_file():
        raise AssertionError(f"required packaging file is missing: {relative}")
    return path.read_text()


class PackagingContractTests(unittest.TestCase):
    def test_dockerfile_pins_upstream_and_base_digest(self) -> None:
        text = required_text("Dockerfile")
        self.assertIn(
            "python:3.13-slim-bookworm@sha256:"
            "ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e",
            text,
        )
        self.assertIn("169fcf35a2fc163fec31338b816503ddac0d3fcf", text)
        self.assertRegex(text, r"--require-hashes")
        self.assertRegex(text, r"AS production\b")
        self.assertRegex(text, r"AS test\b")

    def test_production_compose_is_loopback_by_default_cpu_only_and_hardened(self) -> None:
        text = required_text("compose.yaml")
        self.assertIn('"${COMFYUI_BIND_ADDRESS:-127.0.0.1}:8188:8188"', text)
        self.assertIn("read_only: true", text)
        self.assertIn('cpus: "4.0"', text)
        self.assertIn("mem_limit: 6g", text)
        self.assertIn("no-new-privileges:true", text)
        self.assertIn("cap_drop:\n      - ALL", text)
        for cap in ("CHOWN", "SETGID", "SETUID", "SETPCAP"):
            self.assertRegex(text, rf"(?m)^\s+- {cap}$")
        for mutable in ("./data/input", "./data/output", "./data/user", "./data/temp"):
            self.assertIn(mutable, text)
        self.assertIn("./secrets/h3_gateway.token", text)
        self.assertIn("./secrets/h3_profile_control.token", text)
        self.assertIn("/run/secrets-source:rw,noexec,nosuid,nodev,size=64k,mode=0700", text)
        self.assertNotRegex(text.lower(), r"\b(gpu|nvidia|device_requests|runtime:)\b")
        self.assertNotIn("192.0.2.30", text)

    def test_test_profile_is_separate_and_internal(self) -> None:
        text = required_text("compose.test.yaml")
        self.assertRegex(text, r"(?m)^\s+target: test$")
        self.assertIn("internal: true", text)
        self.assertIn("fake-gateway", text)
        self.assertIn("/run/secrets-source:rw,noexec,nosuid,nodev,size=64k,mode=0700", text)
        self.assertNotIn("192.0.2.30", text)
        self.assertNotIn("h3_gateway.token", text)
        self.assertIn('${FAKE_GATEWAY_MODE:-success}', text)
        for path in ("input", "output", "user", "temp"):
            self.assertIn(
                f"/data/{path}:rw,noexec,nosuid,nodev,uid=65532,gid=65532",
                text,
            )

    def test_runtime_contracts_are_non_interoperable(self) -> None:
        production = json.loads(required_text("contracts/production-runtime.json"))
        test = json.loads(required_text("contracts/test-runtime.json"))
        self.assertEqual(production["schema_id"], "dgx-h3-production-v1")
        self.assertEqual(production["profile"], "production")
        self.assertEqual(production["endpoint_id"], "h3-head-lan-v1")
        self.assertEqual(
            production["endpoint"],
            "http://192.0.2.30:18081/v1/videos/sync",
        )
        self.assertEqual(production["secret_path"], "/run/dgx-h3-secret/token")
        self.assertEqual(test["schema_id"], "dgx-h3-test-v1")
        self.assertEqual(test["profile"], "test")
        self.assertEqual(test["endpoint_id"], "fake-gateway-v1")
        self.assertEqual(
            test["endpoint"],
            "http://fake-gateway:19081/v1/videos/sync",
        )
        self.assertEqual(test["secret_path"], "/run/dgx-h3-secret/token")
        self.assertNotEqual(production["schema_id"], test["schema_id"])

    def test_test_image_replaces_only_the_immutable_profile_literal(self) -> None:
        dockerfile = required_text("Dockerfile")
        test_profile = required_text("contracts/test-deployment.py").strip()
        self.assertEqual(test_profile, 'EXPECTED_PROFILE = "test"')
        self.assertIn(
            "COPY contracts/test-deployment.py /opt/ComfyUI/custom_nodes/dgx_remote_h3/deployment.py",
            dockerfile,
        )

    def test_profile_manager_routes_and_frontend_are_packaged(self) -> None:
        package = required_text("custom_nodes/dgx_remote_h3/__init__.py")
        routes = required_text("custom_nodes/dgx_remote_h3/profile_routes.py")
        frontend = required_text("custom_nodes/dgx_remote_h3/web/profile_manager.js")
        self.assertIn('WEB_DIRECTORY = "./web"', package)
        self.assertIn('/dgx-h3/profile/status', routes)
        self.assertIn('/dgx-h3/profile/action', routes)
        self.assertIn('X-DGX-H3-Action', routes)
        self.assertIn('app.registerExtension', frontend)
        self.assertIn('explicit-user-action', frontend)
        self.assertNotIn('control-token', frontend)
        self.assertNotIn('Authorization', frontend)

    def test_profile_manager_auto_refresh_has_bounded_lifecycle(self) -> None:
        frontend = required_text("custom_nodes/dgx_remote_h3/web/profile_manager.js")
        self.assertIn("const ACTIVE_POLL_MS = 2000;", frontend)
        self.assertIn("const IDLE_POLL_MS = 10000;", frontend)
        self.assertIn("requestInFlight", frontend)
        self.assertIn("statusError ? IDLE_POLL_MS", frontend)
        self.assertIn("refreshStatus();", frontend)
        self.assertIn(
            'document.addEventListener("visibilitychange", handleVisibilityChange);',
            frontend,
        )
        self.assertIn("this.onRemoved = () => {", frontend)
        self.assertIn("clearTimeout(pollTimer);", frontend)
        self.assertIn(
            'document.removeEventListener("visibilitychange", handleVisibilityChange);',
            frontend,
        )
        for label in ("当前 Profile", "双 Rank", "健康状态", "最后更新"):
            self.assertIn(label, frontend)
            self.assertIn(f'this.addWidget("text", "{label}"', frontend)

    def test_entrypoint_declares_exact_privilege_transition(self) -> None:
        text = required_text("container/entrypoint.sh")
        self.assertIn('PRIVATE_SECRET="$PRIVATE_DIR/token"', text)
        self.assertIn('PRIVATE_CONTROL_SECRET="$PRIVATE_DIR/control-token"', text)
        for token in (
            "O_NOFOLLOW",
            "O_EXCL",
            "os.fsync",
            "chmod",
            "chown",
            "--reuid=65532",
            "--regid=65532",
            "--clear-groups",
            "--inh-caps=-all",
            "--ambient-caps=-all",
            "--bounding-set=-all",
            "--no-new-privs",
        ):
            self.assertIn(token, text)
        self.assertLess(text.index("chmod"), text.index("chown"))

    def test_runtime_uid_has_identity_and_writable_cache_roots(self) -> None:
        text = required_text("Dockerfile")
        self.assertIn("dgx-comfyui:x:65532:65532", text)
        self.assertIn("dgx-comfyui:x:65532:", text)
        self.assertIn("HOME=/data/user", text)
        self.assertIn("TMPDIR=/data/temp", text)
        self.assertIn("XDG_CACHE_HOME=/data/temp/cache", text)
        self.assertIn("TORCHINDUCTOR_CACHE_DIR=/data/temp/torchinductor", text)

    def test_steady_state_checks_zero_caps_and_source_unreadable(self) -> None:
        text = required_text("container/steady-state.sh")
        for field in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
            self.assertIn(field, text)
        self.assertIn("NoNewPrivs", text)
        self.assertIn("source secret remains readable", text)
        self.assertIn("private secret is not readable", text)

    def test_per_arch_locks_are_hash_pinned(self) -> None:
        for arch in ("arm64", "amd64"):
            lock = required_text(f"requirements/{arch}.lock")
            self.assertIn("--hash=sha256:", lock)
            self.assertNotRegex(lock, r"(?im)^\s*-e\s|@\s+(git|https?)")
            versions = {
                name: re.search(rf"(?m)^{name}==([^ \\\n]+)", lock).group(1)
                for name in ("torch", "torchaudio", "torchvision")
            }
            self.assertEqual(versions["torch"], "2.11.0+cpu")
            self.assertEqual(versions["torchaudio"], "2.11.0+cpu")
            self.assertEqual(versions["torchvision"], "0.26.0+cpu")
        compile_script = required_text("requirements/compile-locks.sh")
        self.assertIn("aarch64-manylinux_2_36", compile_script)
        self.assertIn("x86_64-manylinux_2_36", compile_script)
        self.assertIn("--generate-hashes", compile_script)
        self.assertIn("--torch-backend=cpu", compile_script)

    def test_ignore_files_exclude_secrets_and_runtime_data(self) -> None:
        gitignore = required_text(".gitignore")
        dockerignore = required_text(".dockerignore")
        for secret_pattern in ("secrets/*", "data/input/*", "data/output/*", "data/user/*", "data/temp/*"):
            self.assertIn(secret_pattern, gitignore)
        self.assertIn("secrets", dockerignore)
        self.assertIn("data", dockerignore)


if __name__ == "__main__":
    unittest.main()
