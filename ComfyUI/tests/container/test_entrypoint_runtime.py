from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
IMAGE = "python:3.13-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e"


@unittest.skipUnless(shutil.which("docker"), "docker is required")
class EntrypointRuntimeTests(unittest.TestCase):
    def run_container(self, secret_mode: int = 0o600, env: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
        self.assertTrue(
            (ROOT / "container/entrypoint.sh").is_file(),
            "required container/entrypoint.sh is missing",
        )
        canary = "container-contract-canary-7a3c5e"
        with tempfile.TemporaryDirectory() as temp_dir:
            secret = pathlib.Path(temp_dir) / "token"
            secret.write_text(canary + "\n")
            secret.chmod(secret_mode)
            command = [
                "docker", "run", "--rm", "--read-only",
                "--tmpfs", "/run/dgx-h3-secret:rw,noexec,nosuid,nodev,size=64k,mode=0700",
                "--tmpfs", "/run/secrets-source:rw,noexec,nosuid,nodev,size=64k,mode=0700",
                "--cap-drop", "ALL",
                "--cap-add", "CHOWN", "--cap-add", "SETGID",
                "--cap-add", "SETUID", "--cap-add", "SETPCAP",
                "--security-opt", "no-new-privileges:true",
                "--mount", f"type=bind,src={ROOT / 'container'},dst=/opt/dgx/container,readonly",
                "--mount", f"type=bind,src={secret},dst=/run/secrets-source/h3_gateway_token,readonly",
                "--mount", f"type=bind,src={secret},dst=/run/secrets-source/h3_profile_control_token,readonly",
            ]
            for item in env:
                command.extend(("--env", item))
            command.extend((
                "--entrypoint", "/opt/dgx/container/entrypoint.sh",
                IMAGE,
                "/bin/sh", "-c",
                "id -u; id -g; grep -E '^(Cap(Inh|Prm|Eff|Bnd|Amb)|NoNewPrivs):' /proc/self/status",
            ))
            return subprocess.run(command, text=True, capture_output=True, timeout=30)

    def test_valid_secret_reaches_non_root_zero_cap_steady_state_without_leak(self) -> None:
        result = self.run_container()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"(?m)^65532\n65532$")
        self.assertNotIn("container-contract-canary", result.stdout + result.stderr)
        for field in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
            self.assertRegex(result.stdout, rf"(?m)^{field}:\s+0+$")
        self.assertRegex(result.stdout, r"(?m)^NoNewPrivs:\s+1$")

    def test_permissive_source_secret_fails_before_command(self) -> None:
        result = self.run_container(secret_mode=0o644)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("65532\n65532", result.stdout)
        self.assertIn("source secret permissions are not private", result.stderr)

    def test_production_override_environment_fails_closed(self) -> None:
        result = self.run_container(env=("H3_URL=http://attacker.invalid",))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("65532\n65532", result.stdout)
        self.assertIn("production endpoint override is forbidden", result.stderr)


if __name__ == "__main__":
    unittest.main()
