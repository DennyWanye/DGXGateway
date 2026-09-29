#!/usr/bin/env python3
"""Black-box tests for the portable ComfyUI operator bundle."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
import wave


BUNDLE_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = BUNDLE_ROOT / "scripts"
WORKFLOW = BUNDLE_ROOT / "workflows" / "dgx-remote-h3-t2va.json"
REF2VA_WORKFLOW = BUNDLE_ROOT / "workflows" / "dgx-remote-h3-ref2va.json"
FL2VA_WORKFLOW = BUNDLE_ROOT / "workflows" / "dgx-remote-h3-10eros-fl2va.json"
PROFILE_WORKFLOW = BUNDLE_ROOT / "workflows" / "dgx-remote-h3-profile-manager.json"
REF2VA_REFERENCE = BUNDLE_ROOT / "assets" / "ref2va-character-yellow-raincoat.png"
EVIDENCE_BUILDER = BUNDLE_ROOT / "tests" / "scripts" / "build-video-evidence.py"


def run(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, env=env, check=False)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BootstrapSecretTests(unittest.TestCase):
    def make_fake_ssh(self, root: Path, *, role_ok: bool = True) -> tuple[dict[str, str], Path]:
        bin_dir = root / "bin"
        bin_dir.mkdir()
        log_path = root / "ssh-argv.log"
        ssh = bin_dir / "ssh"
        role_output = "dgx-head\noperator\n" if role_ok else "wrong-host\noperator\n"
        ssh.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$*" >> "$FAKE_SSH_LOG"\n'
            'case "$*" in\n'
            "  *\"hostname; whoami\"*) printf '%s' \"$FAKE_ROLE_OUTPUT\" ;;\n"
            "  *\"sudo -n cat /etc/minimax-h3/gateway.token\"*) printf '%s\\n' \"$FAKE_TOKEN\" ;;\n"
            "  *) echo unexpected >&2; exit 71 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        ssh.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{bin_dir}:{env['PATH']}",
                "FAKE_SSH_LOG": str(log_path),
                "FAKE_ROLE_OUTPUT": role_output,
                "FAKE_TOKEN": "operator-test-canary",
            }
        )
        return env, log_path

    def test_bootstrap_role_checks_then_atomically_installs_mode_0600_secret(self) -> None:
        self.assertTrue((SCRIPTS / "bootstrap-secret.sh").is_file(), "bootstrap script missing")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env, log_path = self.make_fake_ssh(root)
            destination = root / "secrets" / "h3_gateway.token"
            result = run([str(SCRIPTS / "bootstrap-secret.sh"), str(destination)], env=env)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("operator-test-canary", result.stdout + result.stderr)
            self.assertEqual(destination.read_text(encoding="utf-8"), "operator-test-canary\n")
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            argv = log_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(argv), 2)
            self.assertIn("operator@192.0.2.30 hostname; whoami", argv[0])
            self.assertIn(
                "operator@192.0.2.30 sudo -n cat /etc/minimax-h3/gateway.token",
                argv[1],
            )

    def test_bootstrap_rejects_wrong_role_without_reading_token(self) -> None:
        self.assertTrue((SCRIPTS / "bootstrap-secret.sh").is_file(), "bootstrap script missing")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env, log_path = self.make_fake_ssh(root, role_ok=False)
            destination = root / "secrets" / "h3_gateway.token"
            result = run([str(SCRIPTS / "bootstrap-secret.sh"), str(destination)], env=env)

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(destination.exists())
            argv = log_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(argv), 1)
            self.assertIn("hostname; whoami", argv[0])

    def test_bootstrap_rejects_existing_symlink_destination(self) -> None:
        self.assertTrue((SCRIPTS / "bootstrap-secret.sh").is_file(), "bootstrap script missing")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env, _ = self.make_fake_ssh(root)
            secrets = root / "secrets"
            secrets.mkdir()
            target = root / "do-not-touch"
            target.write_text("sentinel", encoding="utf-8")
            destination = secrets / "h3_gateway.token"
            destination.symlink_to(target)
            result = run([str(SCRIPTS / "bootstrap-secret.sh"), str(destination)], env=env)

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(target.read_text(encoding="utf-8"), "sentinel")


class ManageScriptTests(unittest.TestCase):
    def test_macos_profile_controller_is_session_scoped_and_explicitly_removed(self) -> None:
        script = (SCRIPTS / "manage.sh").read_text(encoding="utf-8")
        self.assertIn("launchctl submit", script)
        self.assertIn('launchctl remove "$control_launchd_label"', script)
        self.assertNotIn("launchctl load", script)
        self.assertNotIn("launchctl bootstrap", script)

    def make_fake_tools(self, root: Path) -> tuple[dict[str, str], Path]:
        bin_dir = root / "bin"
        bin_dir.mkdir()
        log_path = root / "commands.log"
        for name in ("docker", "ssh", "curl", "ifconfig"):
            tool = bin_dir / name
            if name == "ssh":
                body = (
                    "#!/bin/sh\n"
                    'printf "ssh %s\\n" "$*" >> "$FAKE_COMMAND_LOG"\n'
                    "printf 'dgx-head\\noperator\\ninactive\\nactive\\ninactive\\ninactive\\n"
                    "{\"state\":\"READY\",\"profile\":\"heretic-ref2va-v1\","
                    "\"healthy\":true,\"exact_two\":true}\\n"
                    "LISTEN 192.0.2.30:18081\\n'\n"
                )
            elif name == "curl":
                body = (
                    "#!/bin/sh\n"
                    'printf "curl %s\\n" "$*" >> "$FAKE_COMMAND_LOG"\n'
                    "printf '{}\\n'\n"
                )
            elif name == "ifconfig":
                body = (
                    "#!/bin/sh\n"
                    "printf 'en1: flags=8863<UP,BROADCAST,RUNNING,SIMPLEX,MULTICAST> mtu 1500\\n'\n"
                    "printf '    inet 192.0.2.37 netmask 0xffffff00 broadcast 192.0.2.38\\n'\n"
                )
            else:
                body = (
                    "#!/bin/sh\n"
                    'printf "docker %s\\n" "$*" >> "$FAKE_COMMAND_LOG"\n'
                )
            tool.write_text(body, encoding="utf-8")
            tool.chmod(0o755)
        env = os.environ.copy()
        env.update({"PATH": f"{bin_dir}:{env['PATH']}", "FAKE_COMMAND_LOG": str(log_path)})
        return env, log_path

    def test_start_stop_status_and_logs_are_scoped_to_fixed_compose_project(self) -> None:
        self.assertTrue((SCRIPTS / "manage.sh").is_file(), "manage script missing")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env, log_path = self.make_fake_tools(root)
            secret = BUNDLE_ROOT / "secrets" / "h3_gateway.token"
            secret.parent.mkdir(exist_ok=True)
            old = secret.read_bytes() if secret.exists() and not secret.is_symlink() else None
            try:
                secret.write_text("operator-test-canary\n", encoding="utf-8")
                secret.chmod(0o600)
                for action in ("start", "status", "logs", "stop"):
                    result = run([str(SCRIPTS / "manage.sh"), action], env=env)
                    self.assertEqual(result.returncode, 0, f"{action}: {result.stderr}")
                    self.assertNotIn("operator-test-canary", result.stdout + result.stderr)
            finally:
                if old is None:
                    secret.unlink(missing_ok=True)
                else:
                    secret.write_bytes(old)
                    secret.chmod(0o600)

            commands = log_path.read_text(encoding="utf-8")
            self.assertIn("--project-name dgx-comfyui-h3", commands)
            self.assertIn("ssh -o BatchMode=yes", commands)
            self.assertIn("systemctl is-active deepseek-v4-flash-dspark.service", commands)
            self.assertIn("up -d", commands)
            self.assertIn(" ps", commands)
            self.assertIn(" logs", commands)
            self.assertIn(" down", commands)
            self.assertNotIn(" down -v", commands)
            self.assertNotIn("systemctl start", commands)

    def test_lan_bind_address_is_validated_and_reported(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env, log_path = self.make_fake_tools(root)
            secret = BUNDLE_ROOT / "secrets" / "h3_gateway.token"
            secret.parent.mkdir(exist_ok=True)
            old = secret.read_bytes() if secret.exists() and not secret.is_symlink() else None
            try:
                secret.write_text("operator-test-canary\n", encoding="utf-8")
                secret.chmod(0o600)
                lan_env = env | {"COMFYUI_BIND_ADDRESS": "192.0.2.37"}
                result = run([str(SCRIPTS / "manage.sh"), "start"], env=lan_env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("http://192.0.2.37:8188", result.stdout)

                for invalid in ("0.0.0.0", "127.0.0.1:8188", "192.0.2.37;id"):
                    with self.subTest(invalid=invalid):
                        log_path.write_text("", encoding="utf-8")
                        invalid_env = env | {"COMFYUI_BIND_ADDRESS": invalid}
                        rejected = run([str(SCRIPTS / "manage.sh"), "start"], env=invalid_env)
                        self.assertNotEqual(rejected.returncode, 0)
                        self.assertIn("Invalid COMFYUI_BIND_ADDRESS", rejected.stderr)
                        self.assertNotIn("docker ", log_path.read_text(encoding="utf-8"))
            finally:
                if old is None:
                    secret.unlink(missing_ok=True)
                else:
                    secret.write_bytes(old)
                    secret.chmod(0o600)

    def test_doctor_uses_only_read_only_cluster_checks_and_requires_deepseek_stopped(self) -> None:
        self.assertTrue((SCRIPTS / "manage.sh").is_file(), "manage script missing")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env, log_path = self.make_fake_tools(root)
            result = run([str(SCRIPTS / "manage.sh"), "doctor", "--skip-secret"], env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            commands = log_path.read_text(encoding="utf-8")
            self.assertIn("systemctl is-active minimax-h3-two-node.service", commands)
            self.assertIn("systemctl is-active minimax-h3-gateway.service", commands)
            self.assertIn("systemctl is-active deepseek-v4-flash-dspark.service", commands)
            self.assertIn("systemctl is-active deepseek-v4-flash-gateway.service", commands)
            self.assertIn("sudo -n /usr/local/sbin/minimax-h3-control status", commands)
            self.assertNotIn("systemctl start", commands)
            self.assertNotIn("systemctl restart", commands)
            self.assertNotIn("systemctl enable", commands)

    def test_start_installs_default_workflow_once_without_overwriting_user_copy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env, _ = self.make_fake_tools(root)
            secret = BUNDLE_ROOT / "secrets" / "h3_gateway.token"
            installs = {
                BUNDLE_ROOT / "data" / "user" / "default" / "workflows" / WORKFLOW.name: WORKFLOW,
                BUNDLE_ROOT / "data" / "user" / "default" / "workflows" / REF2VA_WORKFLOW.name: REF2VA_WORKFLOW,
                BUNDLE_ROOT / "data" / "user" / "default" / "workflows" / FL2VA_WORKFLOW.name: FL2VA_WORKFLOW,
                BUNDLE_ROOT / "data" / "user" / "default" / "workflows" / PROFILE_WORKFLOW.name: PROFILE_WORKFLOW,
                BUNDLE_ROOT / "data" / "input" / REF2VA_REFERENCE.name: REF2VA_REFERENCE,
            }
            audio_target = BUNDLE_ROOT / "data" / "input" / "ref2va-smoke-reference.wav"
            secret.parent.mkdir(exist_ok=True)
            old_secret = secret.read_bytes() if secret.exists() and not secret.is_symlink() else None
            old_targets = {
                target: target.read_bytes() if target.exists() and not target.is_symlink() else None
                for target in installs
            }
            old_audio = audio_target.read_bytes() if audio_target.exists() and not audio_target.is_symlink() else None
            try:
                secret.write_text("operator-test-canary\n", encoding="utf-8")
                secret.chmod(0o600)
                for target in installs:
                    target.unlink(missing_ok=True)
                audio_target.unlink(missing_ok=True)
                first = run([str(SCRIPTS / "manage.sh"), "start"], env=env)
                self.assertEqual(first.returncode, 0, first.stderr)
                with wave.open(str(audio_target), "rb") as stream:
                    self.assertEqual((stream.getnchannels(), stream.getsampwidth(), stream.getframerate()), (1, 2, 24_000))
                    self.assertEqual(stream.getnframes(), 96_000)
                for target, source in installs.items():
                    self.assertEqual(target.read_bytes(), source.read_bytes())
                    target.write_bytes(b"user-owned-copy\n")
                audio_target.write_bytes(b"user-owned-audio\n")
                second = run([str(SCRIPTS / "manage.sh"), "start"], env=env)
                self.assertEqual(second.returncode, 0, second.stderr)
                for target in installs:
                    self.assertEqual(target.read_bytes(), b"user-owned-copy\n")
                self.assertEqual(audio_target.read_bytes(), b"user-owned-audio\n")
            finally:
                if old_secret is None:
                    secret.unlink(missing_ok=True)
                else:
                    secret.write_bytes(old_secret)
                    secret.chmod(0o600)
                for target, old_target in old_targets.items():
                    if old_target is None:
                        target.unlink(missing_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(old_target)
                if old_audio is None:
                    audio_target.unlink(missing_ok=True)
                else:
                    audio_target.parent.mkdir(parents=True, exist_ok=True)
                    audio_target.write_bytes(old_audio)


class WorkflowTests(unittest.TestCase):
    def test_workflow_is_t2va_only_and_contains_no_endpoint_or_secret(self) -> None:
        self.assertTrue(WORKFLOW.is_file(), "workflow missing")
        workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
        nodes = [node for node in workflow["nodes"] if node["type"] == "DGXRemoteMiniMaxH3T2VA"]
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["widgets_values"][1:], ["768x448", 2, 42])
        encoded = json.dumps(workflow, ensure_ascii=False).lower()
        for forbidden in ("192.0.2.30", "18081", "authorization", "bearer", "token", "fl2va", "ref2va"):
            self.assertNotIn(forbidden, encoded)

    def test_ref2va_workflow_bundles_the_synthetic_character_reference(self) -> None:
        self.assertTrue(REF2VA_WORKFLOW.is_file(), "Ref2VA workflow missing")
        self.assertTrue(REF2VA_REFERENCE.is_file(), "synthetic character reference missing")
        workflow = json.loads(REF2VA_WORKFLOW.read_text(encoding="utf-8"))
        load_image = next(node for node in workflow["nodes"] if node["type"] == "LoadImage")
        self.assertEqual(load_image["widgets_values"][0], REF2VA_REFERENCE.name)
        self.assertEqual(REF2VA_REFERENCE.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
class VideoEvidenceTests(unittest.TestCase):
    def make_video(self, destination: Path, *, silent: bool = False) -> None:
        audio_source = "anullsrc=channel_layout=stereo:sample_rate=32000" if silent else "sine=frequency=440:sample_rate=32000:duration=2"
        result = run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=768x448:rate=24:duration=2",
                "-f",
                "lavfi",
                "-i",
                audio_source,
                "-shortest",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(destination),
            ]
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_builder_creates_hash_bound_five_sample_review_package(self) -> None:
        self.assertTrue(EVIDENCE_BUILDER.is_file(), "video evidence builder missing")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            video = root / "video.mp4"
            workflow = root / "workflow.json"
            history = root / "history.json"
            self.make_video(video)
            workflow.write_text('{"workflow":"test"}\n', encoding="utf-8")
            history.write_text('{"prompt_id":"prompt-test-1"}\n', encoding="utf-8")
            evidence = root / "evidence"
            result = run(
                [
                    "python3",
                    str(EVIDENCE_BUILDER),
                    "--scenario-id",
                    "S-CUI-2",
                    "--video",
                    str(video),
                    "--prompt",
                    "A continuous wide shot of ocean waves reaching a quiet beach at sunrise, with wind moving the grass and natural surf audio.",
                    "--resolution",
                    "768x448",
                    "--duration",
                    "2",
                    "--seed",
                    "42",
                    "--workflow",
                    str(workflow),
                    "--queue-history",
                    str(history),
                    "--prompt-id",
                    "prompt-test-1",
                    "--reviewer-kind",
                    "browser-agent",
                    "--reviewer-id",
                    "test-session",
                    "--output",
                    str(evidence),
                ]
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            samples = json.loads((evidence / "samples.json").read_text(encoding="utf-8"))
            self.assertEqual([row["fraction"] for row in samples["samples"]], [0.1, 0.3, 0.5, 0.7, 0.9])
            self.assertEqual(len({row["frame_index"] for row in samples["samples"]}), 5)
            for row in samples["samples"]:
                self.assertEqual(len(row["png_sha256"]), 64)
                self.assertEqual(row["source_video_sha256"], samples["video_sha256"])
            review = json.loads((evidence / "review.template.json").read_text(encoding="utf-8"))
            self.assertEqual(review["overall_verdict"], "PENDING")
            self.assertEqual(review["reviewer"]["kind"], "browser-agent")
            criterion_ids = {row["criterion_id"] for row in review["criteria"]}
            self.assertIn("s2-wavefront-motion", criterion_ids)
            self.assertIn("common-full-playback", criterion_ids)
            self.assertIn("bindings", review, "review hash bindings missing")
            self.assertEqual(review["bindings"]["samples_sha256"], sha256_file(evidence / "samples.json"))
            self.assertEqual(review["bindings"]["media_sha256"], sha256_file(evidence / "media.json"))
            self.assertEqual(review["bindings"]["scenario_sha256"], sha256_file(evidence / "scenario.json"))
            manifest = json.loads((evidence / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["scenario_id"], "S-CUI-2")
            self.assertEqual(manifest["video_sha256"], samples["video_sha256"])
            self.assertTrue((evidence / "contact-sheet.png").is_file())
            self.assertEqual(len(manifest["artifacts"]["contact-sheet.png"]), 64)

    def test_builder_reserves_scenario_one_for_human_user(self) -> None:
        self.assertTrue(EVIDENCE_BUILDER.is_file(), "video evidence builder missing")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            video = root / "video.mp4"
            workflow = root / "workflow.json"
            history = root / "history.json"
            self.make_video(video)
            workflow.write_text("{}\n", encoding="utf-8")
            history.write_text("{}\n", encoding="utf-8")
            result = run(
                [
                    "python3",
                    str(EVIDENCE_BUILDER),
                    "--scenario-id",
                    "S-CUI-1",
                    "--video",
                    str(video),
                    "--prompt",
                    "test",
                    "--resolution",
                    "768x448",
                    "--duration",
                    "2",
                    "--seed",
                    "42",
                    "--workflow",
                    str(workflow),
                    "--queue-history",
                    str(history),
                    "--prompt-id",
                    "prompt-test-1",
                    "--reviewer-kind",
                    "browser-agent",
                    "--reviewer-id",
                    "test-session",
                    "--output",
                    str(root / "evidence"),
                ]
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("human-user", result.stderr)

    def test_builder_requires_scenario_one_user_confirmation_artifact(self) -> None:
        self.assertTrue(EVIDENCE_BUILDER.is_file(), "video evidence builder missing")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            video = root / "video.mp4"
            workflow = root / "workflow.json"
            history = root / "history.json"
            self.make_video(video)
            workflow.write_text("{}\n", encoding="utf-8")
            history.write_text('{"prompt_id":"prompt-test-1"}\n', encoding="utf-8")
            result = run(
                [
                    "python3",
                    str(EVIDENCE_BUILDER),
                    "--scenario-id",
                    "S-CUI-1",
                    "--video",
                    str(video),
                    "--prompt",
                    "暖色工作灯下，微距镜头拍摄一名工程师稳定地焊接电路板，房间里有轻微环境声。",
                    "--resolution",
                    "768x448",
                    "--duration",
                    "2",
                    "--seed",
                    "42",
                    "--workflow",
                    str(workflow),
                    "--queue-history",
                    str(history),
                    "--prompt-id",
                    "prompt-test-1",
                    "--reviewer-kind",
                    "human-user",
                    "--reviewer-id",
                    "user",
                    "--output",
                    str(root / "evidence"),
                ]
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("confirmation", result.stderr.lower())

    def test_builder_fails_closed_for_silent_audio_and_retains_failure_manifest(self) -> None:
        self.assertTrue(EVIDENCE_BUILDER.is_file(), "video evidence builder missing")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            video = root / "video.mp4"
            workflow = root / "workflow.json"
            history = root / "history.json"
            self.make_video(video, silent=True)
            workflow.write_text("{}\n", encoding="utf-8")
            history.write_text('{"prompt_id":"prompt-test-1"}\n', encoding="utf-8")
            evidence = root / "evidence"
            result = run(
                [
                    "python3",
                    str(EVIDENCE_BUILDER),
                    "--scenario-id",
                    "S-CUI-2",
                    "--video",
                    str(video),
                    "--prompt",
                    "A continuous wide shot of ocean waves reaching a quiet beach at sunrise, with wind moving the grass and natural surf audio.",
                    "--resolution",
                    "768x448",
                    "--duration",
                    "2",
                    "--seed",
                    "42",
                    "--workflow",
                    str(workflow),
                    "--queue-history",
                    str(history),
                    "--prompt-id",
                    "prompt-test-1",
                    "--reviewer-kind",
                    "browser-agent",
                    "--reviewer-id",
                    "test-session",
                    "--output",
                    str(evidence),
                ]
            )
            self.assertNotEqual(result.returncode, 0)
            manifest = json.loads((evidence / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["verdict"], "FAIL_MEDIA")


if __name__ == "__main__":
    unittest.main(verbosity=2)
