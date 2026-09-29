import subprocess
import stat
import tempfile
import unittest
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LifecycleContractTests(unittest.TestCase):
    def test_systemd_units_have_one_owner_and_no_restart(self):
        main = (ROOT / "systemd/minimax-h3-two-node.service").read_text()
        gateway = (ROOT / "systemd/minimax-h3-gateway.service").read_text()
        self.assertIn("Type=oneshot", main)
        self.assertIn("RemainAfterExit=yes", main)
        self.assertIn("Restart=no", main)
        self.assertIn("After=user@1000.service docker.service", main)
        self.assertIn("RuntimeDirectory=minimax-h3-two-node", main)
        self.assertIn("RuntimeDirectoryMode=0755", main)
        self.assertIn("minimax-h3-control start --llm-already-quiesced", main)
        self.assertNotIn("network-online.target", main)
        self.assertIn("Restart=no", gateway)
        self.assertIn("LoadCredential=gateway-token:/etc/minimax-h3/gateway.token", gateway)
        self.assertIn("NoNewPrivileges=yes", gateway)
        self.assertIn("IPAddressAllow=192.0.2.27/24", gateway)
        self.assertNotIn("Restart=always", main + gateway)

    def test_worker_dispatch_accepts_only_exact_forced_commands(self):
        dispatcher = ROOT / "scripts/minimax-h3-two-node/worker-dispatch"
        env = {
            "SSH_ORIGINAL_COMMAND": "lifecycle-status",
            "H3_TESTING": "1",
            "H3_WORKER_HELPER": "/usr/bin/true",
            "H3_WORKER_MODEL_HELPER": "/usr/bin/true",
        }
        accepted = subprocess.run([str(dispatcher)], env=env, capture_output=True, text=True)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        env["SSH_ORIGINAL_COMMAND"] = "start-ray-worker heretic-ref2va-v1"
        accepted = subprocess.run([str(dispatcher)], env=env, capture_output=True, text=True)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        for profile in ("heretic-ref2va-hmnsfw-v1", "10eros-fl2va-v1"):
            env["SSH_ORIGINAL_COMMAND"] = f"start-ray-worker {profile}"
            accepted = subprocess.run([str(dispatcher)], env=env, capture_output=True, text=True)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)

        for command in ["", "status; id", "start-rank0", "cleanup extra", "start-ray-worker both", "scp -t /tmp/x"]:
            with self.subTest(command=command):
                env["SSH_ORIGINAL_COMMAND"] = command
                rejected = subprocess.run([str(dispatcher)], env=env, capture_output=True, text=True)
                self.assertNotEqual(rejected.returncode, 0)

        for command in [
            "status",
            "model-tree-status",
            "verify-model final",
            "verify-model staging-20260830T000000Z",
            "prepare-model-staging 20260830T000000Z",
            "finalize-model 20260830T000000Z",
            "rsync --server -logDtpre.iLsfxCIvu --numeric-ids . /home/operator/dgx-models/video-generation/snapshots/minimax-h3-official.staging-20260830T000000Z/",
        ]:
            with self.subTest(model_command=command):
                env["SSH_ORIGINAL_COMMAND"] = command
                accepted = subprocess.run([str(dispatcher)], env=env, capture_output=True, text=True)
                self.assertEqual(accepted.returncode, 0, accepted.stderr)

        source = dispatcher.read_text()
        self.assertIn('exec sudo -n "$lifecycle_helper" "$helper_command"', source)
        self.assertIn('if [ "${H3_TESTING-}" = 1 ]; then', source)

    def test_worker_model_rsync_is_limited_to_named_multi_profile_staging(self):
        source = (ROOT / "scripts/minimax-h3-two-node/worker-model-dispatch").read_text()
        self.assertIn("minimax-h3-heretic-ref2va-hmnsfw-v1", source)
        self.assertIn("minimax-h3-10eros-fl2va-v1", source)
        self.assertIn("staging-[0-9]{8}T[0-9]{6}Z", source)
        self.assertNotIn("snapshots/.*", source)

    def test_runtime_action_handles_user_systemd_llm_owner(self):
        source = (ROOT / "scripts/minimax-h3-two-node/minimax-h3-runtime-action").read_text()
        self.assertIn('app\\.slice/([^/]+\\.service)', source)
        self.assertIn('--user --machine="${H3_EXPECTED_USER}@.host"', source)
        self.assertIn('"scope":scope', source)
        self.assertIn('install -d -m 0755 "$(dirname "$ATTESTATION_PATH")"', source)

    def test_controller_allows_model_load_actions_to_outlive_default_ssh_timeout(self):
        source = (ROOT / "scripts/minimax-h3-two-node/minimax-h3-control").read_text()
        self.assertIn('"wait-ranks-ready": 2700', source)
        self.assertIn('"start-api": 600', source)
        self.assertIn('timeout=self.ACTION_TIMEOUTS.get(action, 120)', source)

    def test_status_reads_progress_without_waiting_for_the_mutation_flock(self):
        source = (ROOT / "scripts/minimax-h3-two-node/minimax-h3-control").read_text()
        status_branch = source.index('if args.command == "status": # progress-read-without-flock')
        lock_branch = source.index('with lock_path.open("a+", encoding="utf-8") as lock_handle:')
        self.assertLess(status_branch, lock_branch)

    def test_dedicated_ssh_provisioner_is_bounded_and_reversible(self):
        provisioner = (ROOT / "scripts/minimax-h3-two-node/provision-worker-ssh.sh").read_text()
        required = [
            "StrictHostKeyChecking=yes",
            "UpdateHostKeys=no",
            "IdentitiesOnly=yes",
            "ClearAllForwardings=yes",
            "minimax-h3-two-node-key-v1",
            'command="/usr/local/libexec/minimax-h3-worker-dispatch"',
            "ssh-keyscan",
            "ssh-keygen -lf",
            "verify",
            "rollback",
            "authorized_keys",
            "known_hosts",
        ]
        for needle in required:
            with self.subTest(needle=needle):
                self.assertIn(needle, provisioner)
        self.assertNotIn("StrictHostKeyChecking=no", provisioner)
        self.assertNotIn("UserKnownHostsFile=/dev/null", provisioner)
        self.assertNotIn("dgx-cluster-deepseek", provisioner)
        self.assertIn('marker_encoded=$(printf \'%s\' "$MARKER" | base64)', provisioner)
        self.assertIn('marker=$(printf \'%s\' "$1" | base64 -d)', provisioner)

    def test_local_action_dispatch_accepts_only_named_hooks(self):
        dispatcher = ROOT / "scripts/minimax-h3-two-node/minimax-h3-action"
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / "runtime-helper"
            helper.symlink_to("/usr/bin/true")
            accepted = subprocess.run(
                [str(dispatcher), "status-local"],
                env={"H3_TESTING": "1", "H3_RUNTIME_HELPER": str(helper)},
                capture_output=True,
                text=True,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            rank1 = subprocess.run(
                [str(dispatcher), "start-rank1"],
                env={"H3_TESTING": "1", "H3_RUNTIME_HELPER": str(helper)},
                capture_output=True,
                text=True,
            )
            self.assertEqual(rank1.returncode, 0, rank1.stderr)
            profiled = subprocess.run(
                [str(dispatcher), "start-rank1", "heretic-ref2va-v1"],
                env={"H3_TESTING": "1", "H3_RUNTIME_HELPER": str(helper)},
                capture_output=True,
                text=True,
            )
            self.assertEqual(profiled.returncode, 0, profiled.stderr)
            for profile in ("heretic-ref2va-hmnsfw-v1", "10eros-fl2va-v1"):
                profiled = subprocess.run(
                    [str(dispatcher), "start-rank1", profile],
                    env={"H3_TESTING": "1", "H3_RUNTIME_HELPER": str(helper)},
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(profiled.returncode, 0, profiled.stderr)
            for command in ["status-local;id", "unknown", "../status-local"]:
                with self.subTest(command=command):
                    rejected = subprocess.run(
                        [str(dispatcher), command],
                        env={"H3_TESTING": "1", "H3_RUNTIME_HELPER": str(helper)},
                        capture_output=True,
                        text=True,
                    )
                    self.assertNotEqual(rejected.returncode, 0)

    def test_security_installer_never_enables_or_starts_units(self):
        installer = (ROOT / "scripts/minimax-h3-two-node/install-security.sh").read_text()
        self.assertNotIn("systemctl enable", installer)
        self.assertNotIn("systemctl start", installer)
        self.assertNotIn("ufw reset", installer)
        self.assertIn("minimax-h3-gateway-18081-v1", installer)
        self.assertIn("runtime-overlays/worker.py", installer)
        self.assertIn("rollback", installer)
        self.assertIn("trap rollback_partial_rules ERR", installer)

    def test_fabric_firewall_allows_peer_scoped_dynamic_nccl_bootstrap(self):
        installer = (ROOT / "scripts/minimax-h3-two-node/install-security.sh").read_text()
        fabric_rules = installer.split("fabric_rules()", 1)[1].split("existing_roce_allow()", 1)[0]
        self.assertIn(
            'ufw allow in on "$FABRIC_A_IF" from "$FABRIC_A_PEER" to "$FABRIC_A_IP" proto tcp',
            fabric_rules,
        )
        self.assertIn(
            'ufw allow in on "$FABRIC_B_IF" from "$FABRIC_B_PEER" to "$FABRIC_B_IP" proto tcp',
            fabric_rules,
        )

    def test_video_sync_timeout_matches_gateway_policy(self):
        runtime_action = (ROOT / "scripts/minimax-h3-two-node/minimax-h3-runtime-action").read_text()
        self.assertIn("VLLM_OMNI_VIDEO_SYNC_TIMEOUT=3600", runtime_action)
        self.assertNotIn("VLLM_OMNI_VIDEO_SYNC_TIMEOUT=1800", runtime_action)

    def test_installer_owns_runtime_actions_and_rollback_hashes_them(self):
        installer = (ROOT / "scripts/minimax-h3-two-node/install-security.sh").read_text()
        for name in (
            "minimax-h3-runtime-action",
            "minimax-h3-worker-action",
            "minimax-h3-llm-oracle",
        ):
            with self.subTest(name=name):
                self.assertGreaterEqual(installer.count(name), 3)

    def test_firewall_cutover_requires_preimage_guard_and_finalize(self):
        installer = (ROOT / "scripts/minimax-h3-two-node/install-security.sh").read_text()
        required = [
            "prepare-firewall",
            "finalize-firewall",
            "rollback-firewall",
            "systemd-run",
            "--on-active=120s",
            "user.rules",
            "user6.rules",
            "ufw status numbered",
            "iptables-save",
            "ip6tables-save",
            "nft list ruleset",
            "ss -H -lntup",
            "ufw --force enable",
            "minimax-h3-management-ssh-v1",
        ]
        for needle in required:
            with self.subTest(needle=needle):
                self.assertIn(needle, installer)
        self.assertIn("11434", installer)
        self.assertIn("8188", installer)
        install_case = installer.split("install)", 1)[1].split(";;", 1)[0]
        self.assertNotIn("ufw ", install_case)
        self.assertNotIn("systemd-run", install_case)
        self.assertIn("xtables-nft-multi", installer)
        self.assertIn("xtables-legacy-multi", installer)
        preimage = installer.split("require_safe_firewall_preimage()", 1)[1].split("capture_firewall_preimage()", 1)[0]
        self.assertIn("return 0", preimage)
        self.assertIn("set -Eeuo pipefail", installer)
        self.assertIn("existing_roce_allow", installer)

    def test_self_contained_guard_restores_exact_files_and_inactive_state(self):
        guard_source = ROOT / "scripts/minimax-h3-two-node/ufw-rollback-guard.sh"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx = root / "transaction"
            preimage = tx / "preimage"
            etc_ufw = root / "etc/ufw"
            etc_default = root / "etc/default"
            bin_dir = root / "bin"
            for path in (preimage, etc_ufw, etc_default, bin_dir):
                path.mkdir(parents=True, exist_ok=True)

            originals = {
                "user.rules": "original-v4\n",
                "user6.rules": "original-v6\n",
                "ufw.conf": "ENABLED=no\n",
                "default.ufw": "DEFAULT_INPUT_POLICY=\"DROP\"\n",
            }
            (preimage / "user.rules").write_text(originals["user.rules"])
            (preimage / "user6.rules").write_text(originals["user6.rules"])
            (preimage / "ufw.conf").write_text(originals["ufw.conf"])
            (preimage / "default.ufw").write_text(originals["default.ufw"])
            for path in (etc_ufw / "user.rules", etc_ufw / "user6.rules", etc_ufw / "ufw.conf", etc_default / "ufw"):
                path.write_text("mutated\n")

            ufw_log = root / "ufw.log"
            fake_ufw = bin_dir / "ufw"
            fake_ufw.write_text(f"#!/bin/sh\necho \"$*\" >> {ufw_log}\nexit 0\n")
            fake_ufw.chmod(0o755)
            fake_flock = bin_dir / "flock"
            fake_flock.write_text("#!/bin/sh\nexit 0\n")
            fake_flock.chmod(0o755)
            guard = tx / "rollback-guard.sh"
            guard.write_bytes(guard_source.read_bytes())
            guard.chmod(0o700)
            (tx / "transaction.env").write_text(
                "\n".join([
                    f"TX_DIR={tx}",
                    f"UFW_COMMAND={fake_ufw}",
                    f"USER_RULES={etc_ufw / 'user.rules'}",
                    f"USER6_RULES={etc_ufw / 'user6.rules'}",
                    f"UFW_CONF={etc_ufw / 'ufw.conf'}",
                    f"DEFAULT_UFW={etc_default / 'ufw'}",
                    "PREVIOUS_ACTIVE=inactive",
                    "",
                ])
            )
            receipt_inputs = [
                preimage / "default.ufw",
                preimage / "ufw.conf",
                preimage / "user.rules",
                preimage / "user6.rules",
                tx / "transaction.env",
                guard,
            ]
            receipt = subprocess.run(
                ["sha256sum", *map(str, receipt_inputs)],
                check=True,
                capture_output=True,
                text=True,
            )
            (tx / "preimage.sha256").write_text(receipt.stdout)
            (tx / "state").write_text("PREIMAGE_CAPTURED\n")

            result = subprocess.run(
                [str(guard), str(tx)],
                env=dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}"),
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((etc_ufw / "user.rules").read_text(), originals["user.rules"])
            self.assertEqual((etc_ufw / "user6.rules").read_text(), originals["user6.rules"])
            self.assertEqual((etc_ufw / "ufw.conf").read_text(), originals["ufw.conf"])
            self.assertEqual((etc_default / "ufw").read_text(), originals["default.ufw"])
            self.assertIn("--force disable", ufw_log.read_text())
            self.assertEqual((tx / "state").read_text().strip(), "ROLLED_BACK")

    def test_uncalibrated_budget_manifest_is_fail_closed(self):
        budget = (ROOT / "manifests/minimax-h3-coexistence-budget.json").read_text()
        self.assertIn('"status": "uncalibrated"', budget)
        self.assertNotIn('"combined_smoke_passed": true', budget)

    def test_security_install_and_rollback_are_idempotent_in_fixture_root(self):
        installer = ROOT / "scripts/minimax-h3-two-node/install-security.sh"
        with tempfile.TemporaryDirectory() as tmp:
            fixture_root = Path(tmp)
            env = dict(os.environ, H3_INSTALL_ROOT=str(fixture_root))
            first = subprocess.run([str(installer), "install", "head"], env=env, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            second = subprocess.run([str(installer), "install", "head"], env=env, capture_output=True, text=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertTrue((fixture_root / "etc/minimax-h3/gateway.token").is_file())
            self.assertTrue((fixture_root / "etc/minimax-h3/ray.token").is_file())
            self.assertEqual(stat.S_IMODE((fixture_root / "etc/minimax-h3/ray.token").stat().st_mode), 0o600)
            self.assertFalse((fixture_root / "etc/systemd/system/multi-user.target.wants/minimax-h3-two-node.service").exists())

            rollback = subprocess.run([str(installer), "rollback", "head"], env=env, capture_output=True, text=True)
            self.assertEqual(rollback.returncode, 0, rollback.stderr)
            self.assertFalse((fixture_root / "usr/local/sbin/minimax-h3-control").exists())
            self.assertFalse((fixture_root / "etc/minimax-h3/gateway.token").exists())
            self.assertFalse((fixture_root / "etc/minimax-h3/ray.token").exists())

    def test_security_rollback_allows_calibrated_budget_to_be_removed(self):
        installer = ROOT / "scripts/minimax-h3-two-node/install-security.sh"
        with tempfile.TemporaryDirectory() as tmp:
            fixture_root = Path(tmp)
            env = dict(os.environ, H3_INSTALL_ROOT=str(fixture_root))
            subprocess.run([str(installer), "install", "head"], env=env, check=True, capture_output=True, text=True)
            budget = fixture_root / "etc/minimax-h3/coexistence-budget.json"
            budget.write_text('{"schema_version":1,"status":"calibrated"}\n')
            rollback = subprocess.run([str(installer), "rollback", "head"], env=env, capture_output=True, text=True)
            self.assertEqual(rollback.returncode, 0, rollback.stderr)
            self.assertFalse(budget.exists())

    def test_security_rollback_refuses_modified_installed_file(self):
        installer = ROOT / "scripts/minimax-h3-two-node/install-security.sh"
        with tempfile.TemporaryDirectory() as tmp:
            fixture_root = Path(tmp)
            env = dict(os.environ, H3_INSTALL_ROOT=str(fixture_root))
            subprocess.run([str(installer), "install", "worker"], env=env, check=True, capture_output=True, text=True)
            installed = fixture_root / "usr/local/libexec/minimax-h3-gateway"
            installed.write_text("modified")
            rollback = subprocess.run([str(installer), "rollback", "worker"], env=env, capture_output=True, text=True)
            self.assertNotEqual(rollback.returncode, 0)
            self.assertEqual(installed.read_text(), "modified")


if __name__ == "__main__":
    unittest.main()
