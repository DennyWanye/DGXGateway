import json
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HEAD_ACTION = ROOT / "scripts/minimax-h3-two-node/minimax-h3-runtime-action"
WORKER_ACTION = ROOT / "scripts/minimax-h3-two-node/minimax-h3-worker-action"
PROXY_TRANSACTION = (
    ROOT
    / "plans/minimax-h3-two-node/spikes/runtime-build-ray-ranks/temporary-docker-proxy-transaction.sh"
)


class DockerProxyTransactionContractTests(unittest.TestCase):
    def test_uses_daemon_proxy_config_and_rolls_it_back(self):
        source = PROXY_TRANSACTION.read_text(encoding="utf-8")
        self.assertIn("/etc/docker/daemon.json", source)
        self.assertIn('"http-proxy"', source)
        self.assertIn('"https-proxy"', source)
        self.assertIn('"no-proxy"', source)
        self.assertIn('unlink "$daemon_config"', source)
        self.assertIn('docker pull "$probe_ref"', source)
        self.assertIn('docker image rm "$probe_ref"', source)
        self.assertNotIn('docker manifest inspect "$base_ref"', source)
        self.assertNotIn('Environment="HTTP_PROXY=', source)


class RuntimeActionTests(unittest.TestCase):
    def test_lock_attestation_path_is_resolved_after_loading_config(self):
        for path in (HEAD_ACTION, WORKER_ACTION):
            source = path.read_text(encoding="utf-8")
            self.assertLess(source.index('source "$CONFIG_PATH"'), source.index('ATTESTATION_PATH=${H3_MAC_LOCK_ATTESTATION_PATH'))

    def test_restore_can_preserve_intentionally_quiesced_llm(self):
        source = HEAD_ACTION.read_text(encoding="utf-8")
        self.assertIn('H3_RESTORE_LLM_ON_STOP:-1', source)
        self.assertIn('rm -f -- "$LLM_BASELINE"', source)

    def test_switch_from_recovery_refuses_a_live_llm_listener(self):
        source = HEAD_ACTION.read_text(encoding="utf-8")
        self.assertIn("assert_llm_quiesced", source)
        self.assertIn("11434 is active; refusing profile switch", source)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.command_log = self.root / "commands.log"
        self.config = self.root / "minimax-h3.env"
        self.attestation = self.root / "mac-lock-attestation.json"
        self.model = self.root / "model/FL2VA"
        self.model.mkdir(parents=True)
        self.token = self.root / "ray.token"
        self.token.write_text("test-token\n")
        self.token.chmod(0o600)
        self.output = self.root / "output"
        self.evidence = self.root / "rank-evidence"
        self.state = self.root / "state"
        self.runtime_lock = self.root / "runtime-lock.json"
        self.image_id = "sha256:" + "a" * 64
        self.ref2va_image_id = "sha256:" + "c" * 64
        self.overlay = self.root / "worker.py"
        self.overlay.write_text("# test overlay\n")
        overlay_sha = hashlib.sha256(self.overlay.read_bytes()).hexdigest()
        self.runtime_lock.write_text(json.dumps({
            "production": {"final_image_id": self.image_id},
            "candidate": {"final_image_id": self.ref2va_image_id},
            "deployment_overlays": [{
                "target": "/opt/h3-multinode/h3_multinode/worker.py",
                "sha256": overlay_sha,
            }],
        }))
        self.ref2va_model = self.root / "model-ref2va"
        self.ref2va_model.mkdir()
        self.fl2va_model_lock = self.root / "fl2va-model-lock.json"
        self.fl2va_model_lock.write_text(json.dumps({"tree_sha256": "d" * 64}))
        self.ref2va_model_lock = self.root / "ref2va-model-lock.json"
        self.ref2va_model_lock.write_text(json.dumps({"tree_sha256": "e" * 64}))
        (self.ref2va_model / "profile.json").write_text(json.dumps({
            "schema_version": 1,
            "profile_id": "heretic-ref2va-v1",
            "task_type": "ref2va",
            "profile_tree_sha256": "e" * 64,
        }))
        self.profiles = self.root / "profiles.json"
        self.profiles.write_text(json.dumps({
            "schema_version": 1,
            "profiles": {
                "heretic-hmnsfw-v1": {
                    "task_type": "fl2va",
                    "model_root": str(self.model),
                    "model_path": str(self.model),
                    "model_lock": str(self.fl2va_model_lock),
                    "runtime_lock": str(self.runtime_lock),
                    "runtime_image": "minimax-h3-two-node:production",
                    "runtime_overlay": str(self.overlay),
                    "expected_image_id": self.image_id,
                    "profile_tree_sha256": "d" * 64,
                },
                "heretic-ref2va-v1": {
                    "task_type": "ref2va",
                    "model_root": str(self.ref2va_model),
                    "model_path": str(self.ref2va_model),
                    "model_lock": str(self.ref2va_model_lock),
                    "runtime_lock": str(self.runtime_lock),
                    "runtime_image": "minimax-h3-two-node:ref2va-candidate",
                    "runtime_overlay": str(self.overlay),
                    "expected_image_id": self.ref2va_image_id,
                    "profile_tree_sha256": "e" * 64,
                },
            },
        }))
        self.config.write_text(
            "\n".join(
                [
                    "H3_HEAD_HOSTNAME=dgx-head",
                    "H3_EXPECTED_USER=operator",
                    "H3_WORKER_HOSTNAME=dgx-worker",
                    "H3_HEAD_FABRIC_A=192.0.2.33",
                    "H3_WORKER_FABRIC_A=192.0.2.34",
                    "H3_HEAD_FABRIC_B=192.0.2.35",
                    "H3_WORKER_FABRIC_B=192.0.2.36",
                    "H3_NCCL_SOCKET_IFNAME=enp1s0f0np0,enP2p1s0f0np0",
                    "H3_NCCL_IB_HCA==rocep1s0f0:1,roceP2p1s0f0:1",
                    "H3_NCCL_IB_GID_INDEX=3",
                    "H3_INTERNAL_API_BIND=192.0.2.33",
                    "H3_INTERNAL_API_PORT=8000",
                    "H3_EXTERNAL_BIND=192.0.2.30",
                    "H3_EXTERNAL_PORT=18081",
                    "H3_RAY_PORT=6379",
                    "H3_RENDEZVOUS_PORT=29500",
                    f"H3_FL2VA_PATH={self.model}",
                    f"H3_MODEL_PATH={self.model}",
                    f"H3_PROFILES_PATH={self.profiles}",
                    "H3_SELECTED_PROFILE=heretic-hmnsfw-v1",
                    f"H3_RUNTIME_LOCK={self.runtime_lock}",
                    "H3_RUNTIME_IMAGE=minimax-h3-two-node:production",
                    f"H3_RUNTIME_OVERLAY={self.overlay}",
                    f"H3_OUTPUT_DIR={self.output}",
                    f"H3_RANK_EVIDENCE_DIR={self.evidence}",
                    f"H3_RAY_AUTH_TOKEN_PATH={self.token}",
                    "H3_WORKER_SSH=operator@192.0.2.34",
                    "H3_WORKER_SSH_IDENTITY=/etc/minimax-h3/ssh/cluster_ed25519",
                    "H3_WORKER_SSH_KNOWN_HOSTS=/etc/minimax-h3/ssh/known_hosts",
                    "H3_LLM_COEXISTENCE_MODE=exclusive",
                    "",
                ]
            )
        )
        expires = datetime.now(timezone.utc) + timedelta(minutes=10)
        self.attestation.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "cluster_id": "dgx-head+dgx-worker",
                    "state": "ACTIVE",
                    "owner": "minimax-h3-two-node",
                    "token_sha256": "b" * 64,
                    "lease_generation": "c" * 32,
                    "expires_at": expires.isoformat(),
                }
            )
        )
        self.attestation.chmod(0o600)
        self._fake("docker", '#!/bin/sh\nprintf "docker %s\\n" "$*" >> "$H3_COMMAND_LOG"\ncase "$*" in *"image inspect"*"--format {{.Id}}"*) printf "%s\\n" "$H3_TEST_IMAGE_ID";; esac\n')
        self._fake("systemctl", '#!/bin/sh\nprintf "systemctl %s\\n" "$*" >> "$H3_COMMAND_LOG"\ncase "$1:$2" in is-active:*) exit 1;; is-enabled:*) echo disabled; exit 1;; esac\n')
        self._fake("ssh", '#!/bin/sh\nprintf "ssh %s\\n" "$*" >> "$H3_COMMAND_LOG"\nif [ "${*: -1}" = lifecycle-status ]; then echo READY; fi\n')
        self._fake("curl", '#!/bin/sh\nprintf "curl %s\\n" "$*" >> "$H3_COMMAND_LOG"\nprintf 200\n')
        self.health_overlay = self.root / "health-overlay"
        shutil.copytree(ROOT / "scripts/minimax-h3-two-node/long-video-overlay", self.health_overlay)

    def _fake(self, name, source):
        path = self.bin / name
        path.write_text(source)
        path.chmod(0o755)

    def env(self, **extra):
        result = dict(
            os.environ,
            PATH=f"{self.bin}:{os.environ['PATH']}",
            H3_TESTING="1",
            H3_CONFIG_PATH=str(self.config),
            H3_RUNTIME_STATE_DIR=str(self.state),
            H3_MAC_LOCK_ATTESTATION_PATH=str(self.attestation),
            H3_COMMAND_LOG=str(self.command_log),
            H3_TEST_IMAGE_ID=self.image_id,
            H3_TEST_OVERLAY_DIR=str(self.health_overlay),
        )
        result.update(extra)
        return result

    def run_action(self, script, action, **extra):
        if not script.is_file():
            self.fail(f"required runtime action hook is missing: {script.name}")
        return subprocess.run([str(script), action], env=self.env(**extra), capture_output=True, text=True)

    def log(self):
        return self.command_log.read_text() if self.command_log.exists() else ""

    def test_mutation_requires_explicit_runtime_authorization_and_live_mac_lock(self):
        denied = self.run_action(HEAD_ACTION, "start-ray-head")
        self.assertNotEqual(denied.returncode, 0)
        self.assertEqual(self.log(), "")

        expired = json.loads(self.attestation.read_text())
        expired["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.attestation.write_text(json.dumps(expired))
        denied = self.run_action(HEAD_ACTION, "start-ray-head", H3_ALLOW_RUNTIME_ACTIONS="1")
        self.assertNotEqual(denied.returncode, 0)
        self.assertEqual(self.log(), "")

    def test_ref2va_qualification_lock_owner_is_explicitly_allowed(self):
        value = json.loads(self.attestation.read_text())
        value["owner"] = "minimax-h3-ref2va-continuity"
        self.attestation.write_text(json.dumps(value))
        allowed = self.run_action(HEAD_ACTION, "stop-gateway", H3_ALLOW_RUNTIME_ACTIONS="1")
        self.assertEqual(allowed.returncode, 0, allowed.stderr)

        value["owner"] = "deepseek-v4-flash"
        self.attestation.write_text(json.dumps(value))
        denied = self.run_action(HEAD_ACTION, "stop-gateway", H3_ALLOW_RUNTIME_ACTIONS="1")
        self.assertNotEqual(denied.returncode, 0)

    def test_multi_profile_lock_owner_is_explicitly_allowed(self):
        value = json.loads(self.attestation.read_text())
        value["owner"] = "minimax-h3-multi-profile"
        self.attestation.write_text(json.dumps(value))
        allowed = self.run_action(HEAD_ACTION, "stop-gateway", H3_ALLOW_RUNTIME_ACTIONS="1")
        self.assertEqual(allowed.returncode, 0, allowed.stderr)

    def test_head_ray_uses_named_nonrestarting_container_and_fixed_private_ports(self):
        self.evidence.mkdir()
        previous = self.evidence / "rank-0.jsonl"
        previous.write_text('{"rank":0,"monotonic_ns":999}\n')
        result = self.run_action(HEAD_ACTION, "start-ray-head", H3_ALLOW_RUNTIME_ACTIONS="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(previous.exists())
        archived = list((self.evidence / "archive").glob("rank-0-*.jsonl"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_text(), '{"rank":0,"monotonic_ns":999}\n')
        log = self.log()
        for value in (
            "--name minimax-h3-2x-ray-head",
            "--restart=no",
            "--network host",
            "--gpus all",
            "--device /dev/infiniband",
            "--node-ip-address=192.0.2.33",
            "--port=6379",
            "--include-dashboard=false",
            "--min-worker-port=10002",
            "--max-worker-port=10031",
            "--node-manager-port=8076",
            "--object-manager-port=8077",
            "--runtime-env-agent-port=8079",
            f"{self.token}:/run/credentials/ray.token:ro",
            f"{self.model}:{self.model}:ro",
            f"{self.overlay}:/opt/h3-multinode/h3_multinode/worker.py:ro",
        ):
            self.assertIn(value, log)
        self.assertNotIn("8265", log)

    def test_api_driver_mounts_output_separately_and_keeps_model_read_only(self):
        result = self.run_action(HEAD_ACTION, "start-rank1", H3_ALLOW_RUNTIME_ACTIONS="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.log()
        for value in (
            "--name minimax-h3-2x-api",
            "--restart=no",
            "--pid=container:minimax-h3-2x-ray-head",
            "NCCL_IB_HCA==rocep1s0f0:1,roceP2p1s0f0:1",
            "NCCL_SOCKET_IFNAME=enp1s0f0np0,enP2p1s0f0np0",
            f"{self.model}:{self.model}:ro",
            f"{self.output}:/output",
            f"{self.evidence}:{self.evidence}",
            "--host 192.0.2.33",
            "--port 8000",
            "--num-gpus 2",
            "--usp 2",
            "--ring 1",
        ):
            self.assertIn(value, log)

    def test_ref2va_profile_selects_sealed_model_without_tts_task_flag(self):
        result = self.run_action(
            HEAD_ACTION,
            "start-rank1",
            H3_ALLOW_RUNTIME_ACTIONS="1",
            H3_SELECTED_PROFILE_OVERRIDE="heretic-ref2va-v1",
            H3_TEST_IMAGE_ID=self.ref2va_image_id,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        log = self.log()
        self.assertIn("minimax-h3-two-node:ref2va-candidate", log)
        self.assertIn(f"{self.ref2va_model}:{self.ref2va_model}:ro", log)
        self.assertIn(f"serve {self.ref2va_model}", log)
        self.assertIn("--enforce-eager", log)
        self.assertNotIn("--task-type", log)
        self.assertIn(f"{self.health_overlay}/executor.py:/opt/h3-multinode/h3_multinode/executor.py:ro", log)
        self.assertIn("VLLM_OMNI_VIDEO_SYNC_TIMEOUT=3600", log)
        for forbidden in ("VLLM_OMNI_VIDEO_SYNC_TIMEOUT=0", "--text-encoder-tp-size", "/api_server.py:", "/pipeline_minimax_h3.py:", "/vae.py:"):
            self.assertNotIn(forbidden, log)

    def test_ref2va_worker_mounts_only_the_executor_health_fix(self):
        result = self.run_action(WORKER_ACTION, "start-ray-worker", H3_ALLOW_RUNTIME_ACTIONS="1",
            H3_SELECTED_PROFILE_OVERRIDE="heretic-ref2va-v1", H3_TEST_IMAGE_ID=self.ref2va_image_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--name minimax-h3-2x-ray-worker", self.log())
        self.assertIn(f"{self.health_overlay}/executor.py:/opt/h3-multinode/h3_multinode/executor.py:ro", self.log())
        for forbidden in ("VLLM_OMNI_VIDEO_SYNC_TIMEOUT=0", "/api_server.py:", "/pipeline_minimax_h3.py:", "/vae.py:"):
            self.assertNotIn(forbidden, self.log())

    def test_ref2va_bad_executor_overlay_prevents_both_container_starts(self):
        with (self.health_overlay / "executor.py").open("a") as handle:
            handle.write("\n# corrupt overlay\n")
        for script, action in ((HEAD_ACTION, "start-rank1"), (WORKER_ACTION, "start-ray-worker")):
            with self.subTest(script=script.name):
                result = self.run_action(script, action, H3_ALLOW_RUNTIME_ACTIONS="1",
                    H3_SELECTED_PROFILE_OVERRIDE="heretic-ref2va-v1", H3_TEST_IMAGE_ID=self.ref2va_image_id)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("docker run ", self.log())

    def test_fl2va_keeps_the_qualified_compiled_runtime(self):
        result = self.run_action(HEAD_ACTION, "start-rank1", H3_ALLOW_RUNTIME_ACTIONS="1")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn("--enforce-eager", self.log())

    def test_unknown_or_unsealed_profile_fails_before_docker_mutation(self):
        denied = self.run_action(
            HEAD_ACTION,
            "start-ray-head",
            H3_ALLOW_RUNTIME_ACTIONS="1",
            H3_SELECTED_PROFILE_OVERRIDE="unknown-profile",
        )
        self.assertNotEqual(0, denied.returncode)
        self.assertEqual("", self.log())

        document = json.loads(self.profiles.read_text())
        document["profiles"]["heretic-ref2va-v1"]["expected_image_id"] = None
        self.profiles.write_text(json.dumps(document))
        denied = self.run_action(
            HEAD_ACTION,
            "start-ray-head",
            H3_ALLOW_RUNTIME_ACTIONS="1",
            H3_SELECTED_PROFILE_OVERRIDE="heretic-ref2va-v1",
        )
        self.assertNotEqual(0, denied.returncode)
        self.assertEqual("", self.log())

    def test_mismatched_profile_document_and_multi_profile_selection_fail_before_docker(self):
        profile = json.loads((self.ref2va_model / "profile.json").read_text())
        profile["profile_tree_sha256"] = "f" * 64
        (self.ref2va_model / "profile.json").write_text(json.dumps(profile))
        denied = self.run_action(
            HEAD_ACTION,
            "start-ray-head",
            H3_ALLOW_RUNTIME_ACTIONS="1",
            H3_SELECTED_PROFILE_OVERRIDE="heretic-ref2va-v1",
        )
        self.assertNotEqual(0, denied.returncode)
        self.assertEqual("", self.log())

        denied = self.run_action(
            HEAD_ACTION,
            "start-ray-head",
            H3_ALLOW_RUNTIME_ACTIONS="1",
            H3_SELECTED_PROFILE_OVERRIDE="heretic-hmnsfw-v1,heretic-ref2va-v1",
        )
        self.assertNotEqual(0, denied.returncode)
        self.assertEqual("", self.log())

    def test_profile_image_must_be_correlated_to_its_runtime_lock_before_docker(self):
        runtime = json.loads(self.runtime_lock.read_text())
        runtime["candidate"]["final_image_id"] = "sha256:" + "9" * 64
        self.runtime_lock.write_text(json.dumps(runtime))
        denied = self.run_action(
            HEAD_ACTION,
            "start-ray-head",
            H3_ALLOW_RUNTIME_ACTIONS="1",
            H3_SELECTED_PROFILE_OVERRIDE="heretic-ref2va-v1",
        )
        self.assertNotEqual(0, denied.returncode)
        self.assertEqual("", self.log())

    def test_exact_cleanup_remains_available_when_profile_catalog_is_damaged(self):
        self.profiles.write_text("not json")
        cleaned = self.run_action(
            HEAD_ACTION,
            "rollback-h3",
            H3_ALLOW_RUNTIME_ACTIONS="1",
        )
        self.assertEqual(0, cleaned.returncode, cleaned.stderr)
        self.assertIn("docker rm -f minimax-h3-2x-api minimax-h3-2x-ray-head", self.log())

    def test_worker_ray_uses_peer_identity_and_cleanup_is_named_only(self):
        self.evidence.mkdir()
        previous = self.evidence / "rank-1.jsonl"
        previous.write_text('{"rank":1,"monotonic_ns":999}\n')
        started = self.run_action(WORKER_ACTION, "start-ray-worker", H3_ALLOW_RUNTIME_ACTIONS="1")
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertFalse(previous.exists())
        archived = list((self.evidence / "archive").glob("rank-1-*.jsonl"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_text(), '{"rank":1,"monotonic_ns":999}\n')
        log = self.log()
        self.assertIn("--name minimax-h3-2x-ray-worker", log)
        self.assertIn("--address=192.0.2.33:6379", log)
        self.assertIn("--node-ip-address=192.0.2.34", log)
        self.assertIn("--min-worker-port=10002", log)
        self.assertIn("--restart=no", log)

        cleaned = self.run_action(WORKER_ACTION, "cleanup", H3_ALLOW_RUNTIME_ACTIONS="1")
        self.assertEqual(cleaned.returncode, 0, cleaned.stderr)
        cleanup_log = self.log().splitlines()[-2:]
        self.assertTrue(any("minimax-h3-2x-ray-worker" in line for line in cleanup_log))
        self.assertFalse(any("prune" in line for line in cleanup_log))

    def test_read_only_worker_status_does_not_require_lock_or_mutate(self):
        status = self.run_action(WORKER_ACTION, "status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("STOPPED", status.stdout)
        self.assertFalse(any(line.startswith("docker rm ") for line in self.log().splitlines()))

    def test_wait_ready_requires_active_gateway_and_fixed_listener(self):
        self._fake("ssh", '#!/bin/sh\nprintf "ssh %s\\n" "$*" >> "$H3_COMMAND_LOG"\necho READY\n')
        denied = self.run_action(HEAD_ACTION, "wait-ready", H3_WAIT_GATEWAY_TIMEOUT_SECONDS="1")
        self.assertNotEqual(denied.returncode, 0)

        self._fake(
            "systemctl",
            '#!/bin/sh\nprintf "systemctl %s\\n" "$*" >> "$H3_COMMAND_LOG"\ncase "$*" in "is-active --quiet minimax-h3-gateway.service") exit 0;; esac\nexit 1\n',
        )
        self._fake(
            "ss",
            '#!/bin/sh\nprintf "LISTEN 0 128 192.0.2.30:18081 0.0.0.0:*\\n"\n',
        )
        ready = self.run_action(HEAD_ACTION, "wait-ready", H3_WAIT_GATEWAY_TIMEOUT_SECONDS="1")
        self.assertEqual(ready.returncode, 0, ready.stderr)

    def test_exclusive_quiesce_records_actual_owner_and_restore_replays_oracle(self):
        unit = self.root / "ollama.service"
        unit.write_text("[Service]\nExecStart=/usr/bin/ollama serve\n")
        oracle = self.root / "oracle"
        oracle.write_text('#!/bin/sh\nprintf "oracle replay\\n" >> "$H3_COMMAND_LOG"\n')
        oracle.chmod(0o755)
        self._fake(
            "ss",
            '#!/bin/sh\nif [ ! -e "$H3_RUNTIME_STATE_DIR/llm-stopped" ]; then printf "LISTEN users:((ollama,pid=999999,fd=3))\\n"; fi\n',
        )
        self._fake(
            "systemctl",
            f'''#!/bin/sh
printf "systemctl %s\\n" "$*" >> "$H3_COMMAND_LOG"
case "$1" in
  show)
    case "$2" in
      ollama.service) printf "ActiveState=active\\nUnitFileState=enabled\\nFragmentPath={unit}\\n" ;;
      dgx-codex-production.service) printf "ActiveState=failed\\nUnitFileState=enabled\\nFragmentPath={unit}\\n" ;;
      *) printf "ActiveState=inactive\\nUnitFileState=disabled\\nFragmentPath={unit}\\n" ;;
    esac ;;
  stop) mkdir -p "$H3_RUNTIME_STATE_DIR"; : > "$H3_RUNTIME_STATE_DIR/llm-stopped" ;;
  start) rm -f "$H3_RUNTIME_STATE_DIR/llm-stopped" ;;
esac
''',
        )
        quiesced = self.run_action(
            HEAD_ACTION,
            "quiesce-llm",
            H3_ALLOW_RUNTIME_ACTIONS="1",
            H3_TEST_LLM_OWNER_UNIT="ollama.service",
            H3_LLM_ORACLE_HELPER=str(oracle),
        )
        self.assertEqual(quiesced.returncode, 0, quiesced.stderr)
        baseline = json.loads((self.state / "llm-baseline.json").read_text())
        self.assertEqual(baseline["listener_owner"], "ollama.service")
        self.assertIn("systemctl stop ollama.service", self.log())
        self.assertNotIn("systemctl stop dgx-codex-production.service", self.log())

        restored = self.run_action(
            HEAD_ACTION,
            "restore-llm",
            H3_ALLOW_RUNTIME_ACTIONS="1",
            H3_TEST_LLM_OWNER_UNIT="ollama.service",
            H3_LLM_ORACLE_HELPER=str(oracle),
        )
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertIn("systemctl start ollama.service", self.log())
        self.assertIn("oracle replay", self.log())
        self.assertFalse((self.state / "llm-baseline.json").exists())

    def test_exclusive_quiesce_can_target_user_manager(self):
        unit = self.root / "ollama.service"
        unit.write_text("[Service]\nExecStart=/usr/bin/ollama serve\n")
        self._fake(
            "ss",
            '#!/bin/sh\nif [ ! -e "$H3_RUNTIME_STATE_DIR/llm-stopped" ]; then printf "LISTEN users:((ollama,pid=999999,fd=3))\\n"; fi\n',
        )
        self._fake(
            "systemctl",
            f'''#!/bin/sh
printf "systemctl %s\\n" "$*" >> "$H3_COMMAND_LOG"
case "$*" in
  *" show "*) printf "ActiveState=active\\nUnitFileState=enabled\\nFragmentPath={unit}\\n" ;;
  *" stop "*) mkdir -p "$H3_RUNTIME_STATE_DIR"; : > "$H3_RUNTIME_STATE_DIR/llm-stopped" ;;
esac
''',
        )
        quiesced = self.run_action(
            HEAD_ACTION,
            "quiesce-llm",
            H3_ALLOW_RUNTIME_ACTIONS="1",
            H3_TEST_LLM_OWNER_UNIT="ollama.service",
            H3_TEST_LLM_OWNER_SCOPE="user",
        )
        self.assertEqual(quiesced.returncode, 0, quiesced.stderr)
        baseline = json.loads((self.state / "llm-baseline.json").read_text())
        self.assertEqual(baseline["listener_scope"], "user")
        self.assertIn("systemctl --user --machine=operator@.host stop ollama.service", self.log())


if __name__ == "__main__":
    unittest.main()
