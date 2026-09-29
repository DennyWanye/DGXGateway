#!/usr/bin/env python3
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shutil
import struct
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
PREPARE = ROOT / "scripts/prepare-deepseek-v4-flash-dspark.sh"
MODEL_LOCK = ROOT / "manifests/deepseek-v4-flash-dspark.lock.json"
FABRIC_LOCK = ROOT / "manifests/dgx-spark-fabric-qualification.lock.json"
OFFICIAL_METADATA = ROOT / "manifests/deepseek-v4-flash-official-metadata.json"
OFFICIAL_INVENTORY = ROOT / "manifests/deepseek-v4-flash-official-anchor-inventory.json"
ANCHOR_BUILDER = ROOT / "scripts/build-deepseek-v4-flash-anchor-inventory.py"
BOOTSTRAP_BUILDER = ROOT / "scripts/build-deepseek-v4-flash-bootstrap-lock.py"
BOOTSTRAP_LOCK = ROOT / "manifests/deepseek-v4-flash-bootstrap-input.lock.json"
BOOTSTRAP_LOCK_RELATIVE = "manifests/deepseek-v4-flash-bootstrap-input.lock.json"
MAIN_ROOT = Path("/Users/operator/projects/DGXSpark")
DOWNLOAD_MANIFEST = ROOT / "manifests/deepseek-v4-flash-ablit-anchored-download.json"
H3_EVIDENCE = (
    MAIN_ROOT
    / "plans/minimax-h3-two-node/spikes/fabric-network-transaction"
    / "nccl-evidence-post-upgrade-20260830T112621Z"
)
ADDENDUM = (
    MAIN_ROOT
    / "plans/deepseek-v4-flash-dspark-2node/verification/20260829T1951"
    / "h3-lock-provenance-addendum-v1.json"
)
FIXED_MODEL_DESTINATION = "operator@192.0.2.34:/home/operator/dgx-models/deepseek-v4-flash-ablit-anchored/"
FIXED_ARCHIVE_DESTINATION = "operator@192.0.2.34:/home/operator/dgx-models/deepseek-v4-flash-dspark/artifacts/dspark-vllm-gx10-a83948492cf13df4.tar"
FIXED_ARCHIVE_RECEIPT_DESTINATION = "operator@192.0.2.34:/home/operator/dgx-models/deepseek-v4-flash-dspark/artifacts/dspark-vllm-gx10-a83948492cf13df4.tar.receipt.json"
FIXED_ARCHIVE_DIRECTORY_DESTINATION = "operator@192.0.2.34:/home/operator/dgx-models/deepseek-v4-flash-dspark/artifacts/"
FIXED_RSYNC_SHELL = (
    "/usr/bin/ssh -i /home/operator/.ssh/deepseek-v4-flash-worker "
    "-o BatchMode=yes -o StrictHostKeyChecking=yes "
    "-o UserKnownHostsFile=/home/operator/.ssh/deepseek-v4-flash-worker-known_hosts"
)
INSTALL_PATH = "/usr/local/libexec/prepare-deepseek-v4-flash-dspark.sh"
MUTATING_COMMANDS = {
    "finalize-model-lock", "copy-model", "acquire-images", "save-runtime-archive",
    "copy-runtime-archive", "load-runtime-archive", "finalize-deployment-lock",
    "apply-runtime-patch", "install-integration-artifacts", "install-service-assets",
    "qualify-fabric-consumer",
    "record-license-acceptance", "record-node-identity", "copy-patched-source",
}
MUTATION_ROLES = {
    "finalize-model-lock": "head", "copy-model": "head", "acquire-images": "head",
    "save-runtime-archive": "head", "copy-runtime-archive": "head",
    "load-runtime-archive": "worker", "finalize-deployment-lock": "head",
    "apply-runtime-patch": "head",
    "install-integration-artifacts": "head",
    "install-service-assets": None,
    "qualify-fabric-consumer": "head",
    "record-license-acceptance": "head", "record-node-identity": None,
    "copy-patched-source": "head",
}


EXPECTED_ANCHORS = {
    "model-00001-of-00048.safetensors": (
        1059061856,
        "f3668ba4cccf1ca6a7eb84e888fb92c1cdc7204d472ba9db771e6fd3abf6b874",
    ),
    "model-00008-of-00048.safetensors": (
        3590024776,
        "224968d2b27f8669365ec08657a768dfec40da0585f85f302a31495931f6a526",
    ),
    "model-00016-of-00048.safetensors": (
        3590026352,
        "5b7ce16064a6edf193895bb7e87eaabf231d586b8ed3d1234b3199769227690b",
    ),
    "model-00024-of-00048.safetensors": (
        3590026352,
        "bbcb99c313bdefd48bbe02840e39ce04e3fb6d20b2fbb897df51b835b7165415",
    ),
    "model-00032-of-00048.safetensors": (
        3590026352,
        "4396e5d6f475c2b4697f723ff3fe847bc76cd5eb07fdd199e74a647750d81b36",
    ),
    "model-00040-of-00048.safetensors": (
        3590026352,
        "8bc93d8a7d1987dc86b14e22b1d8f42ec31da92c56edd9f312daf43f33a6a206",
    ),
    "model-00048-of-00048.safetensors": (
        3692775244,
        "cc43742bd24ae6bcdea343a91442f6f66aed2cfebcc6b235470204851ce2f8a9",
    ),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safetensor_bytes(name: str) -> bytes:
    header = json.dumps(
        {name: {"dtype": "F32", "shape": [0], "data_offsets": [0, 0]}},
        separators=(",", ":"),
    ).encode()
    padding = (-len(header)) % 8
    header += b" " * padding
    return struct.pack("<Q", len(header)) + header


def compressed_layers(image: dict) -> list[dict]:
    count = image["compressed_layer_count"]
    base, remainder = divmod(image["compressed_layer_bytes"], count)
    return [
        {"digest": f"sha256:{index + 1:064x}", "size": base + (1 if index < remainder else 0)}
        for index in range(count)
    ]


def acquisition_state(lock, origin=1000, last=2000, pulls=2):
    return {
        "schema_version": 1, "role": "head", "interface": "dgx-vpn",
        "rx_counter_path": "/sys/class/net/dgx-vpn/statistics/rx_bytes",
        "rx_origin": origin, "last_observed_rx": last, "pull_calls": pulls,
        "runtime_manifest_digest": lock["runtime_image"]["manifest_digest"],
        "gateway_manifest_digest": lock["gateway_image"]["manifest_digest"],
        "transaction_id": "1" * 32, "lease_generation": "2" * 32,
    }


def receipted_argv(args, base: Path):
    values = [*map(str, args)]
    if values and values[0] in MUTATING_COMMANDS and "--receipt-file" not in values:
        command = values[0]
        role = MUTATION_ROLES[command]
        if role is None:
            role = values[values.index("--role") + 1]
        if "--role" not in values:
            values.extend(["--role", role])
        receipt_path = base / f"receipt-{time.time_ns()}.running.json"
        values.extend(["--receipt-file", str(receipt_path)])
        receipt_path.write_text(json.dumps({
            "state": "running",
            "receipt": {
                "schema": "deepseek-dspark-receipt-v1",
                "transaction_id": "a" * 32,
                "host": "dgx-head" if role == "head" else "dgx-worker",
                "role": role,
                "verb": command,
                "resource": (f"deepseek-v4-flash-dspark:{command}:{role}"
                             if command == "install-service-assets" else
                             f"deepseek-v4-flash-dspark:{command}"),
                "argv": [INSTALL_PATH, *values],
                "prior_state_sha256": "1" * 64,
                "nonce": f"{time.time_ns() % (16 ** 32):032x}",
                "issued_at": "2026-08-31T00:00:00Z",
                "not_after": "2026-08-31T00:05:00Z",
                "lease_generation": "2" * 32,
            },
        }))
    return values


class StaticLocksTest(unittest.TestCase):
    def test_best_effort_cleanup_attempts_every_exact_target_and_preserves_errors(self):
        python_source = PREPARE.read_text().split("<<'PY'\n", 1)[1]
        definitions = python_source.rsplit("\ntry:\n    arguments = parser()", 1)[0]
        namespace = {}
        original_argv = list(os.sys.argv)
        try:
            os.sys.argv = ["-", str(PREPARE)]
            exec(compile(definitions, str(PREPARE), "exec"), namespace)
        finally:
            os.sys.argv = original_argv

        attempted = []

        def cleanup(target):
            attempted.append(target)
            if target == "head-container":
                raise RuntimeError("injected head cleanup failure")
            return "removed"

        outcomes, errors = namespace["best_effort_cleanup"](
            (("head", "head-container"), ("worker", "worker-container")), cleanup
        )
        self.assertEqual(attempted, ["head-container", "worker-container"])
        self.assertEqual(outcomes, {"head": "cleanup-failed", "worker": "removed"})
        self.assertEqual(len(errors), 1)
        self.assertIn("head: injected head cleanup failure", errors[0])

    def test_production_docker_and_rsync_executables_are_fixed(self):
        source = PREPARE.read_text()
        self.assertIn('DOCKER_COMMAND = ["/usr/bin/sudo", "-n", "/usr/bin/docker"]', source)
        self.assertIn('RSYNC_COMMAND = "/usr/bin/rsync"', source)
        self.assertNotIn('"--docker-bin"', source)
        self.assertNotIn('"--destination"', source)

    def test_model_lock_pins_sources_runtime_offline_and_81_allowlisted_paths(self):
        lock = json.loads(MODEL_LOCK.read_text())
        download = json.loads(DOWNLOAD_MANIFEST.read_text())
        self.assertEqual(lock["source"]["commit"], "0107cef1835a56d1a2bcdabf7d9e1a085b70338b")
        self.assertEqual(
            lock["runtime_image"]["manifest_digest"],
            "sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8",
        )
        self.assertEqual(
            lock["runtime_image"]["reference"],
            "ghcr.io/anemll/dspark-vllm-gx10@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8",
        )
        self.assertEqual(lock["runtime_image"]["config_digest"], "sha256:3430d6614a8e2925f34d059af6caf05aff42387326db4d05639a60f10f2654d8")
        self.assertEqual(lock["runtime_image"]["compressed_layer_bytes"], 9787492120)
        self.assertEqual(lock["runtime_image"]["distribution"], "head-save-fabric-copy-worker-load")
        self.assertFalse(lock["runtime_image"]["worker_registry_pull_allowed"])
        gateway = lock["gateway_image"]
        self.assertEqual(gateway["reference"], "docker.io/library/nginx@sha256:77d740efa8f9c4753f2a7212d8422b8c77411681971f400ea03d07fe38476cac")
        self.assertEqual(gateway["config_digest"], "sha256:35f3cbee4fb77c3efb39f2723a21ce181906139442a37de8ffc52d89641d9936")
        self.assertEqual(gateway["compressed_layer_count"], 8)
        self.assertEqual(gateway["compressed_layer_bytes"], 22924818)
        self.assertEqual(gateway["placement"], "head-only")
        acquisition = lock["image_acquisition"]
        self.assertEqual(acquisition["approval_message_sha256"], "069cac019d68a3ee4d4fe1e4d173825b62910d8148589efde0dcace5e984fd28")
        self.assertEqual(acquisition["rx_counter"], "/sys/class/net/dgx-vpn/statistics/rx_bytes")
        self.assertEqual(acquisition["cumulative_rx_budget_bytes"], 12000000000)
        self.assertEqual(acquisition["max_pull_calls"], 2)
        self.assertEqual(acquisition["allowed_registry_pull_role"], "head")
        self.assertEqual(acquisition["cache_policy"], "exact-digest-and-valid-journal-only")
        transfer = lock["fabric_transfer"]
        self.assertEqual(transfer["worker"], "operator@192.0.2.34")
        self.assertEqual(transfer["identity_file"], "/home/operator/.ssh/deepseek-v4-flash-worker")
        self.assertEqual(transfer["known_hosts_file"], "/home/operator/.ssh/deepseek-v4-flash-worker-known_hosts")
        self.assertEqual(transfer["model_destination"], FIXED_MODEL_DESTINATION)
        self.assertEqual(transfer["runtime_archive_destination"], FIXED_ARCHIVE_DESTINATION)
        self.assertEqual(transfer["runtime_archive_receipt_destination"], FIXED_ARCHIVE_RECEIPT_DESTINATION)
        self.assertEqual(lock["integration_artifacts"], {
            "finalized": False,
            "files": [
                {"source_path": "gateway/deepseek-v4-flash-nginx-staging.conf",
                 "install_path": "/etc/deepseek-v4-flash/nginx-staging.conf", "mode": "0644", "sha256": None},
                {"source_path": "systemd/deepseek-v4-flash-gateway-staging.service",
                 "install_path": "/etc/systemd/system/deepseek-v4-flash-gateway-staging.service",
                 "mode": "0644", "sha256": None},
            ],
            "semantic_constraints": {"listen": "127.0.0.1:11435", "restart": "no", "manual_only": True},
        })
        self.assertFalse(lock["model"]["closure_finalized"])
        self.assertEqual(lock["model"]["revision"], "a1e69379517383be9cd78c67defb04e77ad6aa68")
        self.assertEqual(lock["model"]["expected_path_count"], 81)
        self.assertEqual(lock["model"]["expected_total_bytes"], 166899479893)
        self.assertEqual(lock["model"]["expected_shard_count"], 48)
        self.assertEqual(lock["model"]["allowlisted_paths"], [row["path"] for row in download])
        self.assertEqual(lock["offline_environment"], {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
        })
        anchors = {
            row["path"]: (row["size"], row["sha256"])
            for row in lock["official_anchor_comparison"]["files"]
        }
        self.assertEqual(lock["official_anchor_comparison"]["revision"], "a1e69379517383be9cd78c67defb04e77ad6aa68")
        self.assertEqual(anchors, EXPECTED_ANCHORS)
        provenance = lock["official_anchor_comparison"]["metadata_source"]
        self.assertEqual(sha256(OFFICIAL_METADATA), provenance["response_body_sha256"])
        self.assertEqual(sha256(OFFICIAL_INVENTORY), provenance["inventory_sha256"])
        inventory = json.loads(OFFICIAL_INVENTORY.read_text())
        self.assertFalse(inventory["metadata_source"]["authenticated"])
        self.assertEqual(inventory["observed_at"], provenance["observed_at"])
        self.assertNotIn("authorization", OFFICIAL_INVENTORY.read_text().lower())
        self.assertFalse(lock["finalized"])
        self.assertIsNone(lock["model"]["root_sha256"])
        self.assertEqual(lock["model"]["files"], [])

    def test_service_asset_roles_are_strict_and_worker_has_no_gateway_or_secret(self):
        service = json.loads(MODEL_LOCK.read_text())["service_assets"]
        self.assertEqual(service["constraints"]["compose_service"], "vllm-dspark")
        head = service["roles"]["head"]
        worker = service["roles"]["worker"]
        self.assertEqual(len(head), 13)
        self.assertEqual(len(worker), 5)
        self.assertEqual({row["install_path"] for row in worker}, {
            "/usr/local/libexec/prepare-deepseek-v4-flash-dspark.sh",
            "/usr/local/libexec/run-deepseek-v4-flash-dspark.sh",
            "/etc/deepseek-v4-flash-dspark.env",
            "/opt/deepseek-v4-flash-dspark/docker-compose.dspark.yml",
            "/etc/systemd/system/deepseek-v4-flash-dspark.service",
        })
        self.assertTrue(any(row["kind"] == "secret-map" and row["mode"] == "0600" for row in head))
        self.assertFalse(any("gateway" in row["install_path"] or "fallback" in row["install_path"] or
                             row["kind"] == "secret-map" for row in worker))
        prepare = PREPARE.read_text()
        self.assertIn("def installed_file_sha256(path, test_mode):", prepare)
        self.assertIn('["/usr/bin/sudo", "-n", "/usr/bin/sha256sum", "--", str(path)]', prepare)
        self.assertIn('"worker": runtime["node_image_ids"]["worker"]', prepare)
        self.assertIn('image_refs["worker"] != runtime["config_digest"]', prepare)
        self.assertIn(" gids=run(['/usr/sbin/show_gids'])", prepare)
        self.assertNotIn("gids=run(['/usr/bin/show_gids'])", prepare)
        live_node = re.search(r"LIVE_NODE_SCRIPT = r'''(.*?)'''", prepare, re.DOTALL)
        self.assertIsNotNone(live_node)
        compile(live_node.group(1), "LIVE_NODE_SCRIPT", "exec")
        self.assertIn("fields.get('firmware-version','').strip().split()[0]", prepare)
        self.assertIn("line.split()[2] == '3' and 'v2' in line.split()", prepare)
        self.assertIn('"--entrypoint", "/usr/bin/python3"', prepare)
        self.assertIn('return [*base, "-m", "torch.distributed.run"', prepare)
        self.assertIn('"--ipc", "host", "--shm-size", "64g", "--ulimit", "memlock=-1:-1"', prepare)

    def test_runtime_patch_is_pinned_to_the_verified_upstream_pre_and_postimages(self):
        source = json.loads(MODEL_LOCK.read_text())["source"]
        patch_path = ROOT / source["local_patch"]["path"]
        self.assertEqual(sha256(patch_path), source["local_patch"]["sha256"])
        self.assertEqual({row["path"]: (row["preimage_sha256"], row["postimage_sha256"])
                          for row in source["patch_targets"]}, {
            "start-deepseek-v4-flash-dspark.sh": (
                "38d777e5ff8d1c21b642f3720ffe25a6320d1345c1b0da9ba3a1631feaa9bb7a",
                "d0d0ed01e8a4a48a12509bee6b98a96b9c2a684a4806cbed5244a193bc79f1f4"),
            "docker-compose.dspark.yml": (
                "1de6b25404787d9b9d826860a069a45271cfeb02c0fc71d82ffb018a4abb5073",
                "b9354ef64287b6da16d8ad35da76a38bc4b019c40df3dfdeb26a784f03587ff6"),
        })
        self.assertIn("  vllm-dspark:", patch_path.read_text())
        self.assertIn("    init: true", patch_path.read_text())

    def test_bootstrap_lock_rebuilds_identically_and_role_trees_fail_closed(self):
        upstream = Path("/Users/operator/.cache/dgx-spark/deepseek-v4-flash/upstream-0107cef1835a56d1a2bcdabf7d9e1a085b70338b")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            rebuilt = base / "rebuilt.json"
            subprocess.run([str(BOOTSTRAP_BUILDER), "--repo-root", ROOT,
                            "--upstream-root", upstream, "--output", rebuilt], check=True)
            self.assertEqual(rebuilt.read_bytes(), BOOTSTRAP_LOCK.read_bytes())
            lock = json.loads(BOOTSTRAP_LOCK.read_text())
            self.assertEqual(lock["file_count"], 19)
            self.assertEqual(lock["file_count"], len(lock["files"]))
            self.assertEqual(lock["total_bytes"], sum(row["size"] for row in lock["files"]))
            material = "".join(
                f"{row['sha256']}  {row['mode']}  {row['size']}  {','.join(row['roles'])}  {row['path']}\n"
                for row in lock["files"]
            ).encode()
            self.assertEqual(lock["root_sha256"], hashlib.sha256(material).hexdigest())
            input_roots = {}
            for role in ("head", "worker"):
                input_root = base / f"input-{role}"
                input_roots[role] = input_root
                for row in lock["files"]:
                    if role not in row["roles"]:
                        continue
                    target = input_root / row["path"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(ROOT / row["path"], target)
                    target.chmod(int(row["mode"], 8))
                control = input_root / "manifests/deepseek-v4-flash-bootstrap-input.lock.json"
                control.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(BOOTSTRAP_LOCK, control)
                control.chmod(0o644)
            source_root = base / "source"
            shutil.copytree(upstream, source_root, ignore=shutil.ignore_patterns(".git"))
            upstream_lock = lock["upstream_source"]
            identity = {key: upstream_lock[key] for key in (
                "schema_version", "repository", "commit", "git_tree", "variant", "files",
                "file_count", "total_bytes", "root_sha256")}
            (source_root / ".deepseek-source-identity.json").write_text(json.dumps(identity))
            env = {**os.environ, "DEEPSEEK_ARTIFACT_TEST_SEAM": "1"}
            for role in ("head", "worker"):
                argv = [str(PREPARE), "verify-bootstrap-input-lock", "--role", role,
                        "--lock", input_roots[role] / BOOTSTRAP_LOCK_RELATIVE,
                        "--test-input-root", input_roots[role]]
                if role == "head":
                    argv.extend(["--test-source-root", source_root])
                checked = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
                self.assertEqual(checked.returncode, 0, checked.stderr)
            forbidden = input_roots["worker"] / "gateway/deepseek-v4-flash-nginx.conf"
            forbidden.parent.mkdir(parents=True)
            forbidden.write_text("forbidden\n")
            rejected = subprocess.run(
                [str(PREPARE), "verify-bootstrap-input-lock", "--role", "worker",
                 "--lock", input_roots["worker"] / BOOTSTRAP_LOCK_RELATIVE,
                 "--test-input-root", input_roots["worker"]],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("missing or extra", rejected.stderr)
            forbidden.unlink()
            unsafe_link = input_roots["worker"] / "unsafe-link"
            unsafe_link.symlink_to(BOOTSTRAP_LOCK)
            unsafe = subprocess.run(
                [str(PREPARE), "verify-bootstrap-input-lock", "--role", "worker",
                 "--lock", input_roots["worker"] / BOOTSTRAP_LOCK_RELATIVE,
                 "--test-input-root", input_roots["worker"]],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            self.assertNotEqual(unsafe.returncode, 0)
            self.assertIn("unsafe path", unsafe.stderr)

    def test_every_finalization_dependency_has_a_receipted_producer(self):
        source = PREPARE.read_text()
        producers = {
            "source": ("apply-runtime-patch", "copy-patched-source"),
            "license": ("record-license-acceptance",),
            "integration": ("install-integration-artifacts",),
            "image_journal": ("acquire-images",),
            "runtime_archive": ("save-runtime-archive", "copy-runtime-archive", "load-runtime-archive"),
            "head_identity": ("record-node-identity",),
            "worker_identity": ("record-node-identity",),
        }
        for dependency, verbs in producers.items():
            with self.subTest(dependency=dependency):
                self.assertTrue(all(f'"{verb}"' in source for verb in verbs))
                self.assertTrue(all(verb in MUTATING_COMMANDS for verb in verbs))
        self.assertNotIn('["git", "-C"', source)

    def test_fabric_lock_embeds_exact_h3_receipt_hashes_and_full_provenance(self):
        lock = json.loads(FABRIC_LOCK.read_text())
        addendum = json.loads(ADDENDUM.read_text())
        self.assertEqual(lock["qualification_id"], "nccl-evidence-post-upgrade-20260830T112621Z")
        self.assertEqual(lock["producer_receipt"]["sha256"], "6590d80eddd2d4b2958d8197b1e33454f1626fb8b9ca1ad9c3109a4307befd8c")
        self.assertEqual(lock["candidate_sha256sums"], {
            "single_a": "fe8de280fa18e00a3dc1acbbcff8679168de430b9850a41b81fe3b5f16fe0a15",
            "single_b": "6e2d0696fc51544ffa982ced863a7a3344729d17de0ea0c768e1637be195e33b",
            "dual": "31fc83865cf38f5b2652287e93a56c90b630afeebe463d108437d7cebf9828ac",
        })
        self.assertEqual(lock["score_sha256s"], {
            "single_a": "e31d5b3049f59739b809e81119d1da5d0f0666b52f7e4908b9cd8c99a59235a9",
            "single_b": "f497c3339c164567f8f62e773dd349d43adced4337ab64e594a8ed736bd0de95",
            "dual": "876c652d3fd4f8c9cd0c1813198541cd399e3e0f72e514a845e8d40194fdfeeb",
        })
        self.assertEqual(lock["audit_seal_sha256"], "67a73b6020b13ad0987ca48566591654b12fd07bce0ec332ec774385a05532d1")
        self.assertEqual(lock["provenance"], addendum)
        limitations = lock["provenance"]["historical_limitations"]
        self.assertFalse(limitations["lock_level_released_at_retained"])
        self.assertFalse(limitations["immutable_lock_release_operation_id_retained"])
        self.assertTrue(limitations["transcript_ids_are_not_lock_operation_ids"])
        serialized = json.dumps(lock)
        self.assertNotIn("latest", serialized.lower())
        self.assertNotIn("glob", serialized.lower())

    def test_production_probe_docker_env_preserves_nccl_exact_match_prefix(self):
        binding = json.loads(FABRIC_LOCK.read_text())["selected_runtime_binding"]
        source = PREPARE.read_text()
        for name in ("NCCL_IB_HCA", "NCCL_SOCKET_IFNAME"):
            self.assertTrue(binding[name].startswith("="))
            self.assertIn(f'"{name}={binding[name]}"', source)

    def test_fabric_source_files_still_match_every_pinned_digest(self):
        lock = json.loads(FABRIC_LOCK.read_text())
        self.assertEqual(sha256(H3_EVIDENCE / "fabric-qualification-release-receipt-v1.json"), lock["producer_receipt"]["sha256"])
        for key, directory in (("single_a", "single-a"), ("single_b", "single-b"), ("dual", "dual")):
            self.assertEqual(sha256(H3_EVIDENCE / directory / "SHA256SUMS"), lock["candidate_sha256sums"][key])
            self.assertEqual(sha256(H3_EVIDENCE / f"{directory}-score.json"), lock["score_sha256s"][key])
        self.assertEqual(sha256(H3_EVIDENCE / "post-qualification-audit/SHA256SUMS"), lock["audit_seal_sha256"])

    def test_fabric_consumer_verifier_accepts_only_exact_generation_and_full_addendum(self):
        valid = subprocess.run(
            [str(PREPARE), "verify-fabric-lock", "--lock", str(FABRIC_LOCK),
             "--evidence-root", str(H3_EVIDENCE), "--provenance-addendum", str(ADDENDUM)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertIn("FABRIC_QUALIFICATION_LOCK_VERIFIED", valid.stdout)
        with tempfile.TemporaryDirectory() as directory:
            altered = Path(directory) / "addendum.json"
            value = json.loads(ADDENDUM.read_text())
            value["historical_limitations"]["lock_level_released_at_retained"] = True
            altered.write_text(json.dumps(value))
            rejected = subprocess.run(
                [str(PREPARE), "verify-fabric-lock", "--lock", str(FABRIC_LOCK),
                 "--evidence-root", str(H3_EVIDENCE), "--provenance-addendum", str(altered)],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("full pinned H3 addendum", rejected.stderr)


class MutationReceiptGateTest(unittest.TestCase):
    @staticmethod
    def commands(base, effects):
        missing = base / "missing.json"
        return {
            "finalize-model-lock": ["--model-root", base, "--download-manifest", missing,
                "--lock-template", missing, "--official-anchor-inventory", missing,
                "--official-metadata", missing,
                "--output", effects / "model-lock.json", "--role", "head"],
            "copy-model": ["--download-manifest", missing, "--lock", missing,
                "--source-root", base, "--role", "head"],
            "acquire-images": ["--role", "head", "--lock", missing,
                "--journal", effects / "journal.json", "--rx-counter", missing],
            "save-runtime-archive": ["--role", "head", "--lock", missing,
                "--journal", missing, "--archive", effects / "runtime.tar",
                "--receipt", effects / "archive.json"],
            "copy-runtime-archive": ["--lock", missing, "--archive", missing,
                "--receipt", missing, "--journal", missing, "--role", "head"],
            "load-runtime-archive": ["--role", "worker", "--lock", missing,
                "--archive", missing, "--receipt", missing],
            "finalize-deployment-lock": ["--bootstrap-lock", missing, "--model-lock", missing, "--source-receipt", missing,
                "--license-receipt", missing, "--integration-receipt", missing, "--image-journal", missing,
                "--runtime-archive", missing, "--archive-receipt", missing,
                "--head-identity-receipt", missing, "--worker-identity-receipt", missing,
                "--output", effects / "deployment-lock.json", "--role", "head"],
            "apply-runtime-patch": ["--lock", missing, "--source-root", base,
                "--patch", missing, "--source-receipt", missing, "--role", "head"],
            "install-integration-artifacts": ["--integration-receipt", effects / "integration.json",
                "--role", "head"],
            "install-service-assets": ["--role", "head", "--final-lock", missing,
                "--source-root", base, "--asset-receipt", effects / "service-assets.json",
                "--secret-fd", missing],
            "qualify-fabric-consumer": ["--role", "head", "--fabric-lock", missing,
                "--provenance-addendum", missing, "--deployment-lock", missing,
                "--output", effects / "fabric-consumer.json"],
            "record-license-acceptance": ["--role", "head", "--lock", missing,
                "--operator", "operator", "--approval-basis-id", "AC-DS-RESPONSIBLE-USE-20260830-USER-CONFIRMED",
                "--approval-basis", "User explicitly confirmed the selected DeepSeek V4 Flash model and replacement deployment in the controlling Codex task.",
                "--authority-commit", "52ccc1adc05922d203614dc263d91dd6dea2076d",
                "--output", effects / "license.json"],
            "record-node-identity": ["--role", "head", "--lock", missing,
                "--model-root", base, "--image-journal", missing,
                "--archive-receipt", missing, "--output", effects / "head.identity.json"],
            "copy-patched-source": ["--role", "head", "--lock", missing,
                "--source-root", base, "--source-receipt", missing],
        }

    def test_test_seam_cannot_bypass_receipt_gate_from_a_linux_style_install_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            copied = base / "prepare-deepseek-v4-flash-dspark.sh"
            shutil.copy2(PREPARE, copied)
            values = receipted_argv([
                "finalize-model-lock", "--model-root", base,
                "--download-manifest", base / "missing.json", "--lock-template", base / "missing.json",
                "--official-anchor-inventory", base / "missing.json", "--official-metadata", base / "missing.json",
                "--output", base / "output.json",
            ], base)
            result = subprocess.run(
                [str(copied), *values], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={**os.environ, "DEEPSEEK_ARTIFACT_RECEIPT_TEST_SEAM": "1"},
            )
            self.assertEqual(result.returncode, 75)
            self.assertIn("must be supplied on /proc/self/fd/3", result.stderr)
            self.assertFalse((base / "output.json").exists())

    def test_every_mutating_verb_fails_75_without_running_receipt_before_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            effects = base / "effects"
            effects.mkdir()
            common = ["--receipt-file", base / "missing-receipt.json"]
            commands = self.commands(base, effects)
            self.assertEqual(set(commands), MUTATING_COMMANDS)
            env = {**os.environ, "DEEPSEEK_ARTIFACT_RECEIPT_TEST_SEAM": "1"}
            for verb, specific in commands.items():
                with self.subTest(verb=verb):
                    result = subprocess.run(
                        [str(PREPARE), verb, *map(str, specific + common)],
                        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                    )
                    self.assertEqual(result.returncode, 75, result.stderr)
                    self.assertIn("mutation receipt is absent", result.stderr)
                    self.assertEqual(list(effects.iterdir()), [], "remote_effect_count must remain zero")

    def test_all_mutation_verbs_accept_exact_running_binding_and_reject_role_verb_or_argv_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            effects = base / "effects"
            effects.mkdir()
            env = {**os.environ, "DEEPSEEK_ARTIFACT_RECEIPT_TEST_SEAM": "1"}
            commands = self.commands(base, effects)
            self.assertEqual(set(commands), MUTATING_COMMANDS)
            for verb, specific in commands.items():
                with self.subTest(verb=verb, binding="exact"):
                    values = receipted_argv([verb, *specific], base)
                    receipt_path = Path(values[values.index("--receipt-file") + 1])
                    exact_record = json.loads(receipt_path.read_text())
                    reached_handler = subprocess.run(
                        [str(PREPARE), *values], text=True, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, env=env,
                    )
                    self.assertEqual(reached_handler.returncode, 2, reached_handler.stderr)
                    self.assertNotIn("running mutation receipt binding is invalid", reached_handler.stderr)
                    self.assertEqual(list(effects.iterdir()), [])

                for binding in ("role", "verb", "argv"):
                    with self.subTest(verb=verb, binding=binding):
                        altered = json.loads(json.dumps(exact_record))
                        if binding == "role":
                            altered["receipt"]["role"] = (
                                "worker" if altered["receipt"]["role"] == "head" else "head"
                            )
                        elif binding == "verb":
                            altered["receipt"]["verb"] = "wrong-verb"
                        else:
                            altered["receipt"]["argv"] = [*altered["receipt"]["argv"], "--unexpected"]
                        receipt_path.write_text(json.dumps(altered))
                        rejected = subprocess.run(
                            [str(PREPARE), *values], text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env,
                        )
                        self.assertEqual(rejected.returncode, 75, rejected.stderr)
                        self.assertIn("running mutation receipt binding is invalid", rejected.stderr)
                        self.assertEqual(list(effects.iterdir()), [])

    def test_nonrunning_wrong_argv_and_bad_generation_receipts_fail_before_output_write(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            output = base / "must-not-exist.json"
            values = receipted_argv([
                "finalize-model-lock", "--model-root", base,
                "--download-manifest", base / "missing.json", "--lock-template", base / "missing.json",
                "--official-anchor-inventory", base / "missing.json", "--official-metadata", base / "missing.json",
                "--output", output,
            ], base)
            receipt_path = Path(values[values.index("--receipt-file") + 1])
            valid_record = json.loads(receipt_path.read_text())
            env = {**os.environ, "DEEPSEEK_ARTIFACT_RECEIPT_TEST_SEAM": "1"}

            nonrunning = json.loads(json.dumps(valid_record))
            nonrunning["state"] = "pending"
            receipt_path.write_text(json.dumps(nonrunning))
            rejected = subprocess.run(
                [str(PREPARE), *values], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            )
            self.assertEqual(rejected.returncode, 75)
            self.assertFalse(output.exists())

            bad_generation = json.loads(json.dumps(valid_record))
            bad_generation["receipt"]["lease_generation"] = "not-a-generation"
            receipt_path.write_text(json.dumps(bad_generation))
            rejected = subprocess.run(
                [str(PREPARE), *values], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            )
            self.assertEqual(rejected.returncode, 75)
            self.assertFalse(output.exists())

            wrong_argv = json.loads(json.dumps(valid_record))
            wrong_argv["receipt"]["argv"][-1] = "/wrong/receipt"
            receipt_path.write_text(json.dumps(wrong_argv))
            rejected = subprocess.run(
                [str(PREPARE), *values], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            )
            self.assertEqual(rejected.returncode, 75)
            self.assertFalse(output.exists())


class AnchorInventoryBuilderTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.model = self.base / "model"
        self.model.mkdir()
        policy = json.loads(MODEL_LOCK.read_text())["official_anchor_comparison"]
        self.paths = [row["path"] for row in policy["files"]]
        siblings = []
        for path in self.paths:
            payload = safetensor_bytes("tensor")
            target = self.model / path
            target.write_bytes(payload)
            siblings.append({"rfilename": path, "lfs": {"size": len(payload), "sha256": sha256(target)}})
        for index in range(1, 49):
            path = f"model-{index:05d}-of-00048.safetensors"
            if path not in self.paths:
                siblings.append({"rfilename": path, "lfs": {"size": 9, "sha256": f"{index:064x}"}})
        self.metadata = self.base / "metadata.json"
        self.value = {"id": policy["repository"], "modelId": policy["repository"],
                      "sha": policy["revision"], "siblings": siblings}
        self.metadata.write_text(json.dumps(self.value))

    def tearDown(self):
        self.tempdir.cleanup()

    def run_builder(self, check=False):
        return subprocess.run([str(ANCHOR_BUILDER), str(self.metadata), str(self.model),
                               "--observed-at", "2026-08-30T23:42:58.820777+00:00"],
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)

    def test_builder_emits_fixed_secret_free_anchor_set(self):
        result = self.run_builder(check=True)
        value = json.loads(result.stdout)
        self.assertEqual(value["builder_version"], 2)
        self.assertEqual(value["anchor_paths"], self.paths)
        self.assertFalse(value["metadata_source"]["authenticated"])
        self.assertEqual([row["path"] for row in value["files"]], self.paths)
        self.assertEqual([row["path"] for row in value["official_shards"]],
                         [f"model-{index:05d}-of-00048.safetensors" for index in range(1, 49)])
        self.assertEqual(result.stdout, self.run_builder(check=True).stdout)

    def test_builder_rejects_repository_revision_and_missing_anchor(self):
        for mutation in ("repository", "missing-repository-field", "revision", "missing"):
            with self.subTest(mutation=mutation):
                value = json.loads(json.dumps(self.value))
                if mutation == "repository":
                    value["id"] = "wrong/repository"
                elif mutation == "missing-repository-field":
                    value.pop("modelId")
                elif mutation == "revision":
                    value["sha"] = "0" * 40
                else:
                    value["siblings"].pop()
                self.metadata.write_text(json.dumps(value))
                self.assertNotEqual(self.run_builder().returncode, 0)
                self.metadata.write_text(json.dumps(self.value))

    def test_builder_rejects_truncated_oversized_and_out_of_bounds_headers(self):
        target = self.model / self.paths[0]
        original = target.read_bytes()
        cases = {
            "truncated": b"short",
            "oversized": struct.pack("<Q", 256 * 1024 * 1024 + 1) + b"{}",
            "out-of-bounds": (lambda raw: struct.pack("<Q", len(raw)) + raw)(json.dumps({
                "tensor": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}
            }).encode()),
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                target.write_bytes(payload)
                self.assertNotEqual(self.run_builder().returncode, 0)
        target.write_bytes(original)

class PrepareBehaviorTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.model = self.base / "model"
        self.model.mkdir()
        self.download_path = self.base / "download.json"
        self.lock_path = self.base / "template.json"
        self.inventory_path = self.base / "official-inventory.json"
        self.metadata_path = self.base / "official-metadata.json"
        self.output_path = self.base / "final.json"

        source_download = json.loads(DOWNLOAD_MANIFEST.read_text())
        paths = [row["path"] for row in source_download]
        for path in paths:
            destination = self.model / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            if path.endswith(".safetensors"):
                destination.write_bytes(safetensor_bytes("tensor." + path[6:11]))
            else:
                destination.write_text("fixture:" + path + "\n")

        shards = [path for path in paths if path.startswith("model-") and path.endswith(".safetensors")]
        weight_map = {f"tensor.{path[6:11]}": path for path in shards}
        (self.model / "model.safetensors.index.json").write_text(json.dumps({"metadata": {}, "weight_map": weight_map}))

        rows = []
        for row in source_download:
            copied = dict(row)
            copied["revision"] = "a1e69379517383be9cd78c67defb04e77ad6aa68"
            rows.append(copied)
        self.download_path.write_text(json.dumps(rows))

        lock = json.loads(MODEL_LOCK.read_text())
        lock["model"]["expected_total_bytes"] = sum((self.model / path).stat().st_size for path in paths)
        fixture_anchors = []
        inventory_files = []
        for anchor in lock["official_anchor_comparison"]["files"]:
            path = anchor["path"]
            digest = sha256(self.model / path)
            fixture_anchors.append({"path": path, "size": (self.model / path).stat().st_size, "sha256": digest})
            header = json.loads((self.model / path).read_bytes()[8:].decode().strip())
            inventory_files.append({"path": path, "sha256": digest, "tensors": header})
        lock["official_anchor_comparison"]["files"] = fixture_anchors
        metadata = lock["official_anchor_comparison"]["metadata_source"]
        fixture_metadata = {
            "id": lock["official_anchor_comparison"]["repository"],
            "modelId": lock["official_anchor_comparison"]["repository"],
            "sha": lock["official_anchor_comparison"]["revision"],
            "siblings": [
                {"rfilename": path, "size": (self.model / path).stat().st_size,
                 "lfs": {"size": (self.model / path).stat().st_size, "sha256": sha256(self.model / path)}}
                for path in shards
            ],
        }
        self.metadata_path.write_text(json.dumps(fixture_metadata))
        metadata["response_body_sha256"] = sha256(self.metadata_path)
        inventory = {
            "schema_version": 1,
            "builder_version": 2,
            "anchor_paths": [row["path"] for row in fixture_anchors],
            "repository": lock["official_anchor_comparison"]["repository"],
            "revision": lock["official_anchor_comparison"]["revision"],
            "observed_at": metadata["observed_at"],
            "metadata_source": {
                key: metadata[key] for key in ("url", "response_body_sha256", "authenticated")
            },
            "official_shards": [
                {"path": row["rfilename"], "size": row["lfs"]["size"], "sha256": row["lfs"]["sha256"]}
                for row in fixture_metadata["siblings"]
            ],
            "files": inventory_files,
        }
        self.inventory_path.write_text(json.dumps(inventory))
        lock["official_anchor_comparison"]["metadata_source"]["inventory_sha256"] = sha256(self.inventory_path)
        self.lock_path.write_text(json.dumps(lock))

    def tearDown(self):
        self.tempdir.cleanup()

    def run_prepare(self, *args, check=True):
        return subprocess.run(
            [str(PREPARE), *receipted_argv(args, self.base)],
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "DEEPSEEK_ARTIFACT_TEST_SEAM": "1",
                 "DEEPSEEK_ARTIFACT_RECEIPT_TEST_SEAM": "1"},
        )

    def finalize(self, check=True, strict=True):
        args = [
            "finalize-model-lock",
            "--model-root", self.model,
            "--download-manifest", self.download_path,
            "--lock-template", self.lock_path,
            "--official-anchor-inventory", self.inventory_path,
            "--official-metadata", self.metadata_path,
            "--output", self.output_path,
        ]
        if strict:
            args.append("--strict-tree")
        return self.run_prepare(*args, check=check)

    def test_finalize_computes_full_closure_and_replays_it(self):
        result = self.finalize()
        self.assertIn("MODEL_LOCK_FINALIZED", result.stdout)
        lock = json.loads(self.output_path.read_text())
        self.assertFalse(lock["finalized"])
        self.assertTrue(lock["model"]["closure_finalized"])
        self.assertEqual(len(lock["model"]["files"]), 81)
        self.assertRegex(lock["model"]["root_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(lock["model"]["payload_extra_path_count"], 0)
        self.assertEqual(lock["model"]["source_extra_path_count"], 0)
        self.assertEqual(lock["model"]["verified_shard_count"], 48)
        replay = self.run_prepare("verify-model-lock", "--model-root", self.model, "--lock", self.output_path, "--strict-tree")
        self.assertIn("MODEL_LOCK_VERIFIED", replay.stdout)

    def test_corruption_index_gap_and_extra_file_fail_closed(self):
        self.finalize()
        target = self.model / "tokenizer.json"
        target.write_text("corrupt")
        corrupt = self.run_prepare("verify-model-lock", "--model-root", self.model, "--lock", self.output_path, check=False)
        self.assertNotEqual(corrupt.returncode, 0)
        self.assertIn("digest mismatch", corrupt.stderr)

        target.write_text("fixture:tokenizer.json\n")
        index = json.loads((self.model / "model.safetensors.index.json").read_text())
        index["weight_map"].pop(next(iter(index["weight_map"])))
        (self.model / "model.safetensors.index.json").write_text(json.dumps(index))
        gap = self.finalize(check=False)
        self.assertNotEqual(gap.returncode, 0)
        self.assertIn("index shard closure", gap.stderr)

        # Restore the fixture, then prove strict mode rejects downloader logs.
        self.setUp_index()
        (self.model / "logs").mkdir()
        (self.model / "logs/dsv4-1.download.log").write_text("must not copy")
        extra = self.finalize(check=False)
        self.assertNotEqual(extra.returncode, 0)
        self.assertIn("extra paths", extra.stderr)

    def test_head_extras_are_observation_only_and_worker_clean_tree_verifies(self):
        logs = self.model / "logs"
        logs.mkdir()
        for index in range(50):
            (logs / f"shard-{index:02d}.download.log").write_text("excluded\n")
        (self.model / "download-helper.sh").write_text("excluded\n")
        self.finalize(strict=False)
        lock = json.loads(self.output_path.read_text())
        self.assertFalse(lock["finalized"])
        self.assertTrue(lock["model"]["closure_finalized"])
        self.assertEqual(lock["model"]["payload_extra_path_count"], 0)
        self.assertEqual(lock["model"]["source_extra_path_count"], 51)
        head = self.run_prepare("verify-model-lock", "--model-root", self.model, "--lock", self.output_path)
        self.assertIn("source_extras=51", head.stdout)

        worker = self.base / "worker-model"
        worker.mkdir()
        for relative in lock["model"]["allowlisted_paths"]:
            destination = worker / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.model / relative, destination)
        verified = self.run_prepare(
            "verify-model-lock", "--model-root", worker, "--lock", self.output_path, "--strict-tree"
        )
        self.assertIn("source_extras=0", verified.stdout)

    def test_all_48_shards_are_cross_checked_against_fixed_official_metadata(self):
        anchor_paths = {row["path"] for row in json.loads(self.lock_path.read_text())
                        ["official_anchor_comparison"]["files"]}
        non_anchor = next(path for path in json.loads(self.download_path.read_text())
                          if path["path"].endswith(".safetensors") and path["path"] not in anchor_paths)["path"]
        target = self.model / non_anchor
        original = target.read_bytes()
        target.write_bytes(original + b"x")
        rejected = self.finalize(check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("official metadata shard size/SHA-256 mismatch", rejected.stderr)

    def test_model_closure_is_not_deployment_final_and_full_receipts_are_required(self):
        self.finalize()
        premature = self.run_prepare("verify-deployment-lock", "--lock", self.output_path, check=False)
        self.assertNotEqual(premature.returncode, 0)
        self.assertIn("deployment lock is not fully finalized", premature.stderr)

        lock = json.loads(self.output_path.read_text())
        runtime = lock["runtime_image"]
        gateway = lock["gateway_image"]
        root_hash = lock["model"]["root_sha256"]
        rootfs = ["sha256:diff-a", "sha256:diff-b"]

        service_input = self.base / "service-input"
        runtime_source = self.base / "runtime-source"
        runtime_source.mkdir()
        compose = runtime_source / "docker-compose.dspark.yml"
        upstream = Path("/Users/operator/.cache/dgx-spark/deepseek-v4-flash/upstream-0107cef1835a56d1a2bcdabf7d9e1a085b70338b")
        shutil.copy2(upstream / "docker-compose.dspark.yml", compose)
        shutil.copy2(upstream / "start-deepseek-v4-flash-dspark.sh",
                     runtime_source / "start-deepseek-v4-flash-dspark.sh")
        subprocess.run(["patch", "--batch", "--forward", "-p1", "-d", runtime_source,
                        "-i", ROOT / "patches/deepseek-v4-flash-dspark-local-runtime.patch"], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        service_source_hashes = {}
        for row in lock["service_assets"]["source_files"]:
            path = service_input / row["source_path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            if row["source_path"] == "scripts/deepseek-v4-flash-nccl-probe.py":
                shutil.copy2(ROOT / row["source_path"], path)
            elif row["source_path"].endswith(".service"):
                path.write_text("[Unit]\nDescription=fixture\n[Service]\nRestart=no\n")
            elif row["source_path"].endswith(".timer"):
                path.write_text("[Unit]\nDescription=fixture timer\n[Timer]\nOnActiveSec=1h\n")
            elif row["source_path"].endswith(".env.example"):
                shutil.copy2(ROOT / row["source_path"], path)
            elif row["source_path"].endswith("nginx.conf"):
                path.write_text("server { listen 127.0.0.1:11434; }\n")
            elif row["source_path"].endswith("nginx-staging.conf"):
                path.write_text("server { listen 127.0.0.1:11435; }\n")
            else:
                path.write_text("#!/usr/bin/env python3\n# fixture\n")
            service_source_hashes[row["source_path"]] = sha256(path)

        bootstrap = json.loads(BOOTSTRAP_LOCK.read_text())
        upstream_identity = {key: bootstrap["upstream_source"][key] for key in (
            "schema_version", "repository", "commit", "git_tree", "variant", "files",
            "file_count", "total_bytes", "root_sha256")}
        source_receipt = self.base / "source-receipt.json"
        source_receipt.write_text(json.dumps({
            "schema_version": 1,
            "commit": lock["source"]["commit"],
            "local_patch": lock["source"]["local_patch"],
            "patch_targets": lock["source"]["patch_targets"],
            "upstream_identity": upstream_identity,
            "head_identity": {"variant": "deepseek-local-patched", "root_sha256": "a" * 64,
                              "files": [{"path": "fixture", "sha256": "b" * 64}]},
            "worker_replica": {"root_sha256": "a" * 64,
                               "files": [{"path": "fixture", "sha256": "b" * 64}]},
            "replication": {"transaction_id": "1" * 32, "lease_generation": "2" * 32,
                            "destination": "/home/operator/dgx-models/deepseek-v4-flash-dspark/source",
                            "preimage": "missing", "rollback": "remove-new-destination-only"},
        }))
        license_receipt = self.base / "license-receipt.json"
        files = {row["path"]: row for row in lock["model"]["files"]}
        recorded_license = self.run_prepare(
            "record-license-acceptance", "--role", "head", "--lock", self.output_path,
            "--operator", "operator", "--approval-basis-id", "AC-DS-RESPONSIBLE-USE-20260830-USER-CONFIRMED",
            "--approval-basis", "User explicitly confirmed the selected DeepSeek V4 Flash model and replacement deployment in the controlling Codex task.",
            "--authority-commit", "52ccc1adc05922d203614dc263d91dd6dea2076d",
            "--output", license_receipt,
        )
        self.assertIn("LICENSE_ACCEPTANCE_RECORDED", recorded_license.stdout)
        image_journal = self.base / "image-journal.json"
        image_entries = {}
        for image in (runtime, gateway):
            image_entries[image["manifest_digest"]] = {
                "reference": image["reference"],
                "manifest_digest": image["manifest_digest"],
                "config_digest": image["config_digest"],
                "platform": image["platform"],
                "compressed_layer_bytes": image["compressed_layer_bytes"],
                "compressed_layers": compressed_layers(image),
                "rootfs_diff_ids": rootfs,
            }
        image_journal.write_text(json.dumps({
            "schema_version": 1,
            "images": image_entries,
            "acquisition": acquisition_state(lock),
        }))
        integration_receipt = self.base / "integration-receipt.json"
        integration_receipt.write_text(json.dumps({
            "schema_version": 1,
            "files": [
                {**{key: row[key] for key in ("source_path", "install_path", "mode")},
                 "sha256": str(index + 7) * 64}
                for index, row in enumerate(lock["integration_artifacts"]["files"])
            ],
            "service_asset_sources": [
                {"source_path": row["source_path"], "sha256": service_source_hashes[row["source_path"]]}
                for row in lock["service_assets"]["source_files"]
            ],
            "semantic_constraints": lock["integration_artifacts"]["semantic_constraints"],
        }))
        archive = self.base / "runtime.tar"
        archive.write_bytes(b"immutable-runtime-archive")
        archive_receipt = self.base / "archive-receipt.json"
        archive_receipt.write_text(json.dumps({
            "schema_version": 1,
            "kind": "deepseek-v4-flash-runtime-archive-receipt",
            "reference": runtime["reference"],
            "manifest_digest": runtime["manifest_digest"],
            "config_digest": runtime["config_digest"],
            "platform": runtime["platform"],
            "compressed_layer_count": runtime["compressed_layer_count"],
            "compressed_layer_bytes": runtime["compressed_layer_bytes"],
            "compressed_layers": compressed_layers(runtime),
            "rootfs_diff_ids": rootfs,
            "archive_path": str(archive),
            "archive_size": archive.stat().st_size,
            "archive_sha256": sha256(archive),
        }))
        head_receipt = self.base / "head-identity.json"
        worker_receipt = self.base / "worker-identity.json"
        catalog = {
            image["reference"]: {"RepoDigests": [image["reference"]], "Id": image["config_digest"],
                                 "Architecture": "arm64", "Os": "linux", "RootFS": {"Layers": rootfs}}
            for image in (runtime, gateway)
        }
        catalog[gateway["reference"]]["RepoDigests"] = [
            gateway["reference"].removeprefix("docker.io/library/")
        ]
        catalog[runtime["config_digest"]] = dict(catalog[runtime["reference"]])
        for role, output, include_repo in (("head", head_receipt, True), ("worker", worker_receipt, False)):
            fake_docker = self.base / f"node-docker-{role}.py"
            fake_docker.write_text(
                "#!/usr/bin/env python3\nimport json,sys\n"
                f"catalog={json.dumps(catalog)!r}\ninclude_repo={include_repo!r}\n"
                f"worker_runtime_ref={runtime['reference']!r}\n"
                "catalog=json.loads(catalog)\nitem=dict(catalog[sys.argv[3]])\n"
                "if not include_repo and sys.argv[3] == worker_runtime_ref: raise SystemExit(1)\n"
                "item=item if include_repo else {k:v for k,v in item.items() if k!='RepoDigests'}\n"
                "print(json.dumps([item]))\n"
            )
            fake_docker.chmod(0o755)
            produced = self.run_prepare(
                "record-node-identity", "--role", role, "--lock", self.output_path,
                "--model-root", self.model, "--image-journal", image_journal,
                "--archive-receipt", archive_receipt, "--output", output,
                "--test-docker-command-json", json.dumps([str(fake_docker)]),
            )
            self.assertIn(f"NODE_IDENTITY_RECORDED role={role}", produced.stdout)
        self.assertNotIn("repo_digests", json.loads(worker_receipt.read_text())["images"]["runtime"])
        final_lock = self.base / "deployment-final.json"
        command = [
            "finalize-deployment-lock", "--bootstrap-lock", BOOTSTRAP_LOCK,
            "--model-lock", self.output_path,
            "--source-receipt", source_receipt, "--license-receipt", license_receipt,
            "--integration-receipt", integration_receipt,
            "--image-journal", image_journal, "--runtime-archive", archive,
            "--archive-receipt", archive_receipt, "--head-identity-receipt", head_receipt,
            "--worker-identity-receipt", worker_receipt, "--output", final_lock,
        ]
        completed = self.run_prepare(*command)
        self.assertIn("DEPLOYMENT_LOCK_FINALIZED", completed.stdout)
        final = json.loads(final_lock.read_text())
        self.assertTrue(final["finalized"])
        self.assertTrue(final["model"]["closure_finalized"])
        self.assertEqual(final["source"]["local_patch"]["sha256"], lock["source"]["local_patch"]["sha256"])
        self.assertTrue(final["integration_artifacts"]["finalized"])
        self.assertEqual(final["finalization"]["worker_identity"]["receipt"]["role"], "worker")
        verified = self.run_prepare("verify-deployment-lock", "--lock", final_lock)
        self.assertIn("DEPLOYMENT_LOCK_VERIFIED", verified.stdout)

        secret = self.base / "gateway.token"
        secret.write_text("A" * 64)
        head_root = self.base / "head-installed"
        head_asset_receipt = self.base / "head-assets.json"
        installed_head = self.run_prepare(
            "install-service-assets", "--role", "head", "--final-lock", final_lock,
            "--source-root", runtime_source, "--asset-receipt", head_asset_receipt,
            "--secret-fd", secret, "--test-input-root", service_input,
            "--test-install-root", head_root,
        )
        self.assertIn("SERVICE_ASSETS_INSTALLED role=head targets=13", installed_head.stdout)
        head_assets = json.loads(head_asset_receipt.read_text())
        self.assertEqual(len(head_assets["targets"]), 13)
        self.assertNotIn("A" * 64, head_asset_receipt.read_text())
        secret_include = head_root / "etc/deepseek-v4-flash/gateway-secret.conf"
        self.assertEqual(secret_include.stat().st_mode & 0o777, 0o600)
        self.assertIn('"Bearer ' + "A" * 64 + '" 1;', secret_include.read_text())
        head_env = (head_root / "etc/deepseek-v4-flash-dspark.env").read_text()
        self.assertIn("NODE_RANK=0", head_env)
        self.assertIn("HEADLESS=\n", head_env)
        self.assertIn("NCCL_SOCKET_IFNAME==enp1s0f0np0", head_env)
        self.assertIn("NCCL_IB_HCA==rocep1s0f0:1,roceP2p1s0f0:1", head_env)
        self.assertIn("NCCL_CROSS_NIC=0", head_env)
        self.assertIn("DSPARK_MODEL=/cache/huggingface/deepseek-v4-flash-ablit-anchored", head_env)
        head_config = subprocess.run(
            ["docker", "compose", "--env-file", head_root / "etc/deepseek-v4-flash-dspark.env",
             "-f", head_root / "opt/deepseek-v4-flash-dspark/docker-compose.dspark.yml",
             "config", "--format", "json"], check=True, text=True, stdout=subprocess.PIPE).stdout
        rendered_head = json.loads(head_config)
        self.assertEqual(set(rendered_head["services"]), {"vllm-dspark"})
        self.assertEqual(rendered_head["services"]["vllm-dspark"]["environment"]["NODE_RANK"], "0")
        self.assertEqual(rendered_head["services"]["vllm-dspark"]["environment"]["HEADLESS"], "")

        worker_root = self.base / "worker-installed"
        worker_asset_receipt = self.base / "worker-assets.json"
        installed_worker = self.run_prepare(
            "install-service-assets", "--role", "worker", "--final-lock", final_lock,
            "--source-root", runtime_source, "--asset-receipt", worker_asset_receipt,
            "--test-input-root", service_input, "--test-install-root", worker_root,
        )
        self.assertIn("SERVICE_ASSETS_INSTALLED role=worker targets=5", installed_worker.stdout)
        self.assertFalse((worker_root / "etc/deepseek-v4-flash").exists())
        self.assertIn("DSPARK_ROLE=worker", (worker_root / "etc/deepseek-v4-flash-dspark.env").read_text())
        worker_env = (worker_root / "etc/deepseek-v4-flash-dspark.env").read_text()
        self.assertIn("NODE_RANK=1", worker_env)
        self.assertIn("HEADLESS=1", worker_env)
        self.assertIn("DSPARK_MODEL=/cache/huggingface/deepseek-v4-flash-ablit-anchored", worker_env)
        self.assertIn(f"DSPARK_VLLM_IMAGE={runtime['config_digest']}", worker_env)
        worker_config = subprocess.run(
            ["docker", "compose", "--env-file", worker_root / "etc/deepseek-v4-flash-dspark.env",
             "-f", worker_root / "opt/deepseek-v4-flash-dspark/docker-compose.dspark.yml",
             "config", "--format", "json"], check=True, text=True, stdout=subprocess.PIPE).stdout
        rendered_worker = json.loads(worker_config)
        self.assertEqual(set(rendered_worker["services"]), {"vllm-dspark"})
        worker_environment = rendered_worker["services"]["vllm-dspark"]["environment"]
        self.assertEqual(worker_environment["NODE_RANK"], "1")
        self.assertEqual(worker_environment["HEADLESS"], "1")
        self.assertEqual(worker_environment["VLLM_HOST_IP"], "192.0.2.34")
        self.assertEqual(worker_environment["NCCL_SOCKET_IFNAME"], "=enp1s0f0np0")
        self.assertEqual(worker_environment["NCCL_IB_HCA"], "=rocep1s0f0:1,roceP2p1s0f0:1")
        self.assertEqual(worker_environment["NCCL_CROSS_NIC"], "0")
        (worker_root / "opt/deepseek-v4-flash-dspark/docker-compose.dspark.yml").write_text("different\n")
        rejected_assets = self.run_prepare(
            "install-service-assets", "--role", "worker", "--final-lock", final_lock,
            "--source-root", runtime_source, "--asset-receipt", worker_asset_receipt,
            "--test-input-root", service_input, "--test-install-root", worker_root,
            check=False,
        )
        self.assertNotEqual(rejected_assets.returncode, 0)
        self.assertIn("refusing to overwrite a different service asset", rejected_assets.stderr)

        fabric_policy = json.loads(FABRIC_LOCK.read_text())
        def fixture_rails(role):
            cidrs = (["192.0.2.33/30", "192.0.2.35/30"] if role == "head" else
                     ["192.0.2.34/30", "192.0.2.36/30"])
            return {
                interface: {"hca": hca, "mac": f"02:00:00:00:00:0{index + (1 if role == 'head' else 3)}",
                            "pci": f"000{index}:01:00.0", "firmware": "28.45.4028",
                            "ip_cidrs": [cidrs[index]], "mtu": 9000, "speed_mbps": 200000,
                            "gid_index": 3, "gid_lines": [f"{hca} 3 v2"]}
                for index, (interface, hca) in enumerate((
                    ("enp1s0f0np0", "rocep1s0f0:1"),
                    ("enP2p1s0f0np0", "roceP2p1s0f0:1")))
            }
        consumer_fixture = self.base / "fabric-consumer-fixture.json"
        consumer_fixture.write_text(json.dumps({
            "nodes": {
                "head": {"hostname": "dgx-head", "management_ip": "192.0.2.30",
                         "fabric_ips": ["192.0.2.33", "192.0.2.35"], "boot_id": "head-boot",
                         "kernel": "6.17.0-1031-nvidia", "driver": "580.173.02", "dgx_ota": "7.5.0",
                         "netplan_sha256": fabric_policy["fabric"]["persistent_netplan"]["head_sha256"],
                         "rails": fixture_rails("head"), "default_routes": [{"dev": "wlP9s9"}],
                         "http_listeners": ["LISTEN 0 4096 192.0.2.30:11434 0.0.0.0:*"]},
                "worker": {"hostname": "dgx-worker", "management_ip": "192.0.2.31",
                           "fabric_ips": ["192.0.2.34", "192.0.2.36"], "boot_id": "worker-boot",
                           "kernel": "6.17.0-1031-nvidia", "driver": "580.173.02", "dgx_ota": "7.5.0",
                           "netplan_sha256": fabric_policy["fabric"]["persistent_netplan"]["worker_sha256"],
                           "rails": fixture_rails("worker"), "default_routes": [{"dev": "wlP9s9"}], "http_listeners": []},
            },
            "ssh_trust": {"known_hosts_sha256": "3" * 64,
                          "dedicated_public_key_fingerprint": "SHA256:fixture",
                          "pinned_addresses": ["192.0.2.31", "192.0.2.34", "192.0.2.36"]},
            "df_jumbo": {name: {"exit_code": 0, "output_sha256": "4" * 64} for name in (
                "head:enp1s0f0np0:192.0.2.34", "head:enP2p1s0f0np0:192.0.2.36",
                "worker:enp1s0f0np0:192.0.2.33", "worker:enP2p1s0f0np0:192.0.2.35")},
            "production_image_probe": {
                "commands_sha256": "5" * 64, "head_log_sha256": "6" * 64,
                "worker_log_sha256": "7" * 64, "counter_before_sha256": "8" * 64,
                "counter_after_sha256": "9" * 64, "risky_counter_changes": [],
                "exit_codes": {"head": 0, "worker": 0},
                "selected_hcas": ["rocep1s0f0", "roceP2p1s0f0"],
                "data_counter_deltas": {
                    role: {hca: {"port_xmit_data": 1, "port_rcv_data": 1}
                           for hca in ("rocep1s0f0", "roceP2p1s0f0")}
                    for role in ("head", "worker")
                },
                "container_cleanup": {"head": "absent", "worker": "absent"},
                "ranks": {role: {"rank": rank, "world_size": 2, "backend": "nccl", "wrong": 0,
                                  "torch_version": "2.9", "cuda_version": "13.0", "nccl_version": [2, 28, 3],
                                  "device_name": "NVIDIA GB10", "duration_seconds": 1.0,
                                  "iterations": 32, "elements": 16777216, "bytes_per_tensor": 67108864}
                          for role, rank in (("head", 0), ("worker", 1))},
            },
        }))
        consumer = self.base / "fabric-consumer.json"
        worker_consumer_root = self.base / "consumer-worker"
        qualified = self.run_prepare(
            "qualify-fabric-consumer", "--role", "head", "--fabric-lock", FABRIC_LOCK,
            "--provenance-addendum", ADDENDUM, "--deployment-lock", final_lock,
            "--output", consumer, "--test-consumer-fixture", consumer_fixture,
            "--test-worker-root", worker_consumer_root, "--test-input-root", service_input,
        )
        self.assertRegex(qualified.stdout, r"FABRIC_CONSUMER_QUALIFIED receipt_sha256=[0-9a-f]{64}")
        consumer_value = json.loads(consumer.read_text())
        self.assertEqual(consumer_value["terminal"], "PASS")
        self.assertEqual(consumer_value["transaction_id"], "a" * 32)
        self.assertEqual(consumer_value["lease_generation"], "2" * 32)
        worker_consumer = (worker_consumer_root /
            "home/operator/.local/state/deepseek-v4-flash-dspark/artifacts/fabric-consumer.receipt.json")
        self.assertEqual(sha256(consumer), sha256(worker_consumer))

        valid_fabric = json.loads(consumer_fixture.read_text())
        bad_hca = json.loads(json.dumps(valid_fabric))
        bad_hca["production_image_probe"]["data_counter_deltas"]["worker"]["roceP2p1s0f0"][
            "port_rcv_data"
        ] = 0
        consumer_fixture.write_text(json.dumps(bad_hca))
        rejected_hca = self.run_prepare(
            "qualify-fabric-consumer", "--role", "head", "--fabric-lock", FABRIC_LOCK,
            "--provenance-addendum", ADDENDUM, "--deployment-lock", final_lock,
            "--output", self.base / "bad-hca-consumer.json",
            "--test-consumer-fixture", consumer_fixture,
            "--test-worker-root", self.base / "bad-hca-worker", "--test-input-root", service_input,
            check=False,
        )
        self.assertNotEqual(rejected_hca.returncode, 0)
        self.assertIn("both locked HCAs carried data", rejected_hca.stderr)

        bad_fabric = json.loads(json.dumps(valid_fabric))
        bad_fabric["nodes"]["worker"]["rails"]["enP2p1s0f0np0"]["mtu"] = 1500
        consumer_fixture.write_text(json.dumps(bad_fabric))
        rejected_fabric = self.run_prepare(
            "qualify-fabric-consumer", "--role", "head", "--fabric-lock", FABRIC_LOCK,
            "--provenance-addendum", ADDENDUM, "--deployment-lock", final_lock,
            "--output", self.base / "bad-fabric-consumer.json",
            "--test-consumer-fixture", consumer_fixture,
            "--test-worker-root", self.base / "bad-consumer-worker", "--test-input-root", service_input,
            check=False,
        )
        self.assertNotEqual(rejected_fabric.returncode, 0)
        self.assertIn("MAC/PCI/HCA/fw/IP/MTU/GID evidence is invalid", rejected_fabric.stderr)

        bad_license = json.loads(license_receipt.read_text())
        bad_license["accepted"] = False
        license_receipt.write_text(json.dumps(bad_license))
        rejected = self.run_prepare(*command, check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("license acceptance receipt", rejected.stderr)

    def setUp_index(self):
        paths = [row["path"] for row in json.loads(self.download_path.read_text())]
        shards = [path for path in paths if path.startswith("model-") and path.endswith(".safetensors")]
        weight_map = {f"tensor.{path[6:11]}": path for path in shards}
        (self.model / "model.safetensors.index.json").write_text(json.dumps({"metadata": {}, "weight_map": weight_map}))
        lock = json.loads(self.lock_path.read_text())
        lock["model"]["expected_total_bytes"] = sum((self.model / path).stat().st_size for path in paths)
        self.lock_path.write_text(json.dumps(lock))

    def test_files_from_is_nul_terminated_exact_allowlist(self):
        result = subprocess.run(
            [str(PREPARE), "emit-files-from", "--download-manifest", str(self.download_path)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        paths = result.stdout.rstrip(b"\0").decode().split("\0")
        expected = [row["path"] for row in json.loads(self.download_path.read_text())]
        self.assertEqual(paths, expected)
        self.assertTrue(result.stdout.endswith(b"\0"))
        self.assertNotIn("logs/dsv4-1.download.log", paths)

    def test_unsafe_or_non_81_allowlist_is_rejected(self):
        rows = json.loads(self.download_path.read_text())
        rows[0]["path"] = "../escape"
        rows[0]["destination"] = "../escape"
        self.download_path.write_text(json.dumps(rows))
        result = self.run_prepare("emit-files-from", "--download-manifest", self.download_path, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe relative path", result.stderr)

    def test_fabric_copy_invokes_rsync_with_nul_allowlist_and_no_glob(self):
        self.finalize()
        recorder = self.base / "rsync-recorder.py"
        record = self.base / "rsync-record.json"
        recorder.write_text("""#!/usr/bin/env python3
import json, os, sys
payload = sys.stdin.buffer.read()
with open(os.environ['RSYNC_RECORD'], 'w') as out:
    json.dump({'argv': sys.argv[1:], 'stdin_hex': payload.hex()}, out)
""")
        recorder.chmod(0o755)
        result = subprocess.run(
            [str(PREPARE), *receipted_argv([
             "copy-model", "--download-manifest", self.download_path,
             "--lock", self.output_path, "--source-root", self.model,
             "--test-rsync-bin", recorder], self.base)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "RSYNC_RECORD": str(record), "DEEPSEEK_ARTIFACT_TEST_SEAM": "1",
                 "DEEPSEEK_ARTIFACT_RECEIPT_TEST_SEAM": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = json.loads(record.read_text())
        self.assertIn("--files-from=-", seen["argv"])
        self.assertIn("--from0", seen["argv"])
        self.assertIn("--relative", seen["argv"])
        self.assertIn("-e", seen["argv"])
        self.assertIn(FIXED_RSYNC_SHELL, seen["argv"])
        self.assertEqual(seen["argv"][-1], FIXED_MODEL_DESTINATION)
        self.assertNotIn("*", " ".join(seen["argv"]))
        expected = b"".join((row["path"].encode() + b"\0") for row in json.loads(self.download_path.read_text()))
        self.assertEqual(bytes.fromhex(seen["stdin_hex"]), expected)

    def test_runtime_patch_is_guarded_by_source_receipt_hashes(self):
        source = self.base / "source"
        source.mkdir()
        subprocess.run(["git", "init", "-q", source], check=True)
        subprocess.run(["git", "-C", source, "config", "user.email", "artifact@test.invalid"], check=True)
        subprocess.run(["git", "-C", source, "config", "user.name", "Artifact Test"], check=True)
        targets = ["start-deepseek-v4-flash-dspark.sh", "docker-compose.dspark.yml"]
        for target in targets:
            (source / target).write_text(f"before:{target}\n")
        subprocess.run(["git", "-C", source, "add", *targets], check=True)
        subprocess.run(["git", "-C", source, "commit", "-qm", "fixture"], check=True)
        commit = subprocess.run(
            ["git", "-C", source, "rev-parse", "HEAD"], check=True, text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
        preimages = {target: sha256(source / target) for target in targets}
        for target in targets:
            (source / target).write_text(f"after:{target}\n")
        postimages = {target: sha256(source / target) for target in targets}
        patch = self.base / "runtime.patch"
        patch.write_text(subprocess.run(
            ["git", "-C", source, "diff", "--", *targets], check=True, text=True,
            stdout=subprocess.PIPE,
        ).stdout)
        subprocess.run(["git", "-C", source, "checkout", "--", *targets], check=True)

        lock = json.loads(self.lock_path.read_text())
        lock["source"]["commit"] = commit
        lock["source"]["local_patch"]["sha256"] = sha256(patch)
        for row in lock["source"]["patch_targets"]:
            row["preimage_sha256"] = preimages[row["path"]]
            row["postimage_sha256"] = postimages[row["path"]]
        patch_lock = self.base / "patch-lock.json"
        patch_lock.write_text(json.dumps(lock))
        source_receipt = self.base / "source-receipt.json"
        identity_rows = []
        for target in sorted(targets):
            path = source / target
            identity_rows.append({"path": target, "mode": "0644", "size": path.stat().st_size,
                                  "sha256": sha256(path)})
        material = "".join(f"{row['sha256']}  {row['mode']}  {row['size']}  {row['path']}\n"
                           for row in identity_rows).encode()
        (source / ".deepseek-source-identity.json").write_text(json.dumps({
            "schema_version": 1, "repository": lock["source"]["repository"], "commit": commit,
            "git_tree": "a5258a8070b62fe77ed4bdb8887b39ef388e3a24", "variant": "upstream",
            "files": identity_rows, "file_count": len(identity_rows),
            "total_bytes": sum(row["size"] for row in identity_rows),
            "root_sha256": hashlib.sha256(material).hexdigest(),
        }))
        upstream_identity = json.loads((source / ".deepseek-source-identity.json").read_text())
        impossible_receipt = self.base / "receipt-is-a-directory"
        impossible_receipt.mkdir()
        rolled_back = self.run_prepare(
            "apply-runtime-patch", "--lock", patch_lock, "--source-root", source,
            "--patch", patch, "--source-receipt", impossible_receipt, check=False,
        )
        self.assertNotEqual(rolled_back.returncode, 0)
        self.assertEqual({target: sha256(source / target) for target in targets}, preimages)
        self.assertEqual(json.loads((source / ".deepseek-source-identity.json").read_text()), upstream_identity)
        applied = self.run_prepare(
            "apply-runtime-patch", "--lock", patch_lock, "--source-root", source,
            "--patch", patch, "--source-receipt", source_receipt,
        )
        self.assertIn("RUNTIME_PATCH_APPLIED_WITH_HASH_GUARDS", applied.stdout)
        self.assertEqual({target: sha256(source / target) for target in targets}, postimages)
        self.assertEqual((source / ".deepseek-source-identity.json").stat().st_mode & 0o777, 0o644)
        produced = json.loads(source_receipt.read_text())
        self.assertEqual(produced["worker_replica"], None)
        self.assertEqual(produced["head_identity"]["variant"], "deepseek-local-patched")
        failing_rsync = self.base / "source-rsync-fail.py"
        failing_rsync.write_text("#!/usr/bin/env python3\nraise SystemExit(23)\n")
        failing_rsync.chmod(0o755)
        failed_copy = self.run_prepare(
            "copy-patched-source", "--role", "head", "--lock", patch_lock,
            "--source-root", source, "--source-receipt", source_receipt,
            "--test-rsync-bin", failing_rsync, check=False,
        )
        self.assertNotEqual(failed_copy.returncode, 0)
        self.assertEqual(json.loads(source_receipt.read_text())["worker_replica"], None)
        partial_worker = self.base / "worker-partial-rsync"
        failed_partial = self.run_prepare(
            "copy-patched-source", "--role", "head", "--lock", patch_lock,
            "--source-root", source, "--source-receipt", source_receipt,
            "--test-rsync-bin", failing_rsync, "--test-worker-root", partial_worker,
            check=False,
        )
        self.assertNotEqual(failed_partial.returncode, 0)
        partial_parent = partial_worker / "home/operator/dgx-models/deepseek-v4-flash-dspark"
        self.assertFalse((partial_parent / ("source.staging-" + "a" * 32)).exists())
        self.assertFalse((partial_parent / "source").exists())
        self.assertIsNone(json.loads(source_receipt.read_text())["worker_replica"])
        rsync = self.base / "source-rsync.py"
        rsync.write_text("#!/usr/bin/env python3\nimport sys\n")
        rsync.chmod(0o755)

        for hook in ("race-target", "staging-drift", "post-readback-drift"):
            with self.subTest(worker_source_promotion=hook):
                worker_root = self.base / f"worker-{hook}"
                worker_parent = (worker_root /
                    "home/operator/dgx-models/deepseek-v4-flash-dspark")
                if hook != "race-target":
                    worker_parent.mkdir(parents=True)
                    (worker_parent / "unrelated-competitor").write_text("preserve\n")
                rejected_promotion = self.run_prepare(
                    "copy-patched-source", "--role", "head", "--lock", patch_lock,
                    "--source-root", source, "--source-receipt", source_receipt,
                    "--test-rsync-bin", rsync, "--test-worker-root", worker_root,
                    "--test-promote-hook", hook, check=False,
                )
                self.assertNotEqual(rejected_promotion.returncode, 0)
                destination = worker_parent / "source"
                staging = worker_parent / ("source.staging-" + "a" * 32)
                self.assertFalse(staging.exists())
                if hook == "race-target":
                    self.assertEqual((destination / "competitor-marker").read_text(), "competitor\n")
                else:
                    self.assertFalse(destination.exists())
                    self.assertEqual((worker_parent / "unrelated-competitor").read_text(), "preserve\n")
                self.assertIsNone(json.loads(source_receipt.read_text())["worker_replica"])

        unsafe_worker = self.base / "worker-unsafe-ancestor"
        unsafe_parent = unsafe_worker / "home/operator/dgx-models/deepseek-v4-flash-dspark"
        unsafe_parent.mkdir(parents=True)
        (unsafe_worker / "home/operator/dgx-models").chmod(0o777)
        unsafe_ancestor = self.run_prepare(
            "copy-patched-source", "--role", "head", "--lock", patch_lock,
            "--source-root", source, "--source-receipt", source_receipt,
            "--test-rsync-bin", rsync, "--test-worker-root", unsafe_worker,
            check=False,
        )
        self.assertNotEqual(unsafe_ancestor.returncode, 0)
        self.assertIn("fixed ancestor owner/mode is unsafe", unsafe_ancestor.stderr)
        self.assertFalse((unsafe_parent / "source").exists())

        symlink_worker = self.base / "worker-symlink-ancestor"
        external = self.base / "worker-symlink-external"
        symlink_worker.mkdir()
        external.mkdir()
        (symlink_worker / "home").symlink_to(external, target_is_directory=True)
        unsafe_symlink = self.run_prepare(
            "copy-patched-source", "--role", "head", "--lock", patch_lock,
            "--source-root", source, "--source-receipt", source_receipt,
            "--test-rsync-bin", rsync, "--test-worker-root", symlink_worker,
            check=False,
        )
        self.assertNotEqual(unsafe_symlink.returncode, 0)
        self.assertIn("fixed ancestor owner/mode is unsafe", unsafe_symlink.stderr)
        self.assertFalse((external / "operator/dgx-models/deepseek-v4-flash-dspark/source").exists())

        copied = self.run_prepare(
            "copy-patched-source", "--role", "head", "--lock", patch_lock,
            "--source-root", source, "--source-receipt", source_receipt,
            "--test-rsync-bin", rsync,
        )
        self.assertIn("PATCHED_SOURCE_COPIED", copied.stdout)
        replicated = json.loads(source_receipt.read_text())
        self.assertEqual(replicated["worker_replica"], replicated["head_identity"])
        self.assertEqual(replicated["replication"]["preimage"], "missing")
        self.assertEqual(replicated["replication"]["rollback"], "remove-new-destination-only")

    def test_staging_gateway_files_install_idempotently_and_emit_finalization_receipt(self):
        input_root = self.base / "integration-input"
        (input_root / "manifests").mkdir(parents=True)
        (input_root / "gateway").mkdir()
        (input_root / "systemd").mkdir()
        shutil.copy2(MODEL_LOCK, input_root / "manifests/deepseek-v4-flash-dspark.lock.json")
        nginx = input_root / "gateway/deepseek-v4-flash-nginx-staging.conf"
        unit = input_root / "systemd/deepseek-v4-flash-gateway-staging.service"
        nginx.write_text("server { listen 127.0.0.1:11435; }\n")
        unit.write_text("[Unit]\nDescription=staging\n[Service]\nRestart=no\n")
        lock_template = json.loads(MODEL_LOCK.read_text())
        for row in lock_template["service_assets"]["source_files"]:
            source = input_root / row["source_path"]
            if source.exists():
                continue
            source.parent.mkdir(parents=True, exist_ok=True)
            if row["source_path"].endswith(".service"):
                source.write_text("[Unit]\nDescription=fixture\n[Service]\nRestart=no\n")
            elif row["source_path"].endswith(".timer"):
                source.write_text("[Unit]\nDescription=timer\n[Timer]\nOnActiveSec=1h\n")
            elif row["source_path"].endswith(".env.example"):
                source.write_text("# public\n# role\nDSPARK_ROLE=head\nVLLM_API_KEY=\nDSPARK_API_KEYS=\n")
            else:
                source.write_text("fixture\n")
        install_root = self.base / "installed"
        receipt = self.base / "integration.receipt.json"
        args = [
            "install-integration-artifacts", "--integration-receipt", receipt,
            "--test-input-root", input_root, "--test-install-root", install_root,
        ]
        installed = self.run_prepare(*args)
        self.assertIn("INTEGRATION_ARTIFACTS_INSTALLED_AND_RECEIPTED", installed.stdout)
        expected_destinations = [
            install_root / "etc/deepseek-v4-flash/nginx-staging.conf",
            install_root / "etc/systemd/system/deepseek-v4-flash-gateway-staging.service",
        ]
        value = json.loads(receipt.read_text())
        self.assertEqual(len(value["service_asset_sources"]), 12)
        self.assertEqual([row["sha256"] for row in value["files"]],
                         [sha256(nginx), sha256(unit)])
        self.assertEqual([sha256(path) for path in expected_destinations],
                         [sha256(nginx), sha256(unit)])
        repeated = self.run_prepare(*args)
        self.assertIn("INTEGRATION_ARTIFACTS_INSTALLED_AND_RECEIPTED", repeated.stdout)

        expected_destinations[0].write_text("foreign\n")
        rejected = self.run_prepare(*args, check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("refusing to overwrite", rejected.stderr)
        self.assertEqual(expected_destinations[0].read_text(), "foreign\n")


class ImageAcquisitionTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.lock = json.loads(MODEL_LOCK.read_text())
        self.lock_path = self.base / "lock.json"
        self.lock_path.write_text(json.dumps(self.lock))
        self.journal = self.base / "journal.json"
        self.counter = self.base / "rx_bytes"
        self.counter.write_text("1000\n")
        self.log = self.base / "docker.log"
        self.state = self.base / "docker-state.json"
        self.fake_docker = self.base / "fake-docker.py"
        self.fake_docker.write_text("""#!/usr/bin/env python3
import json, os, pathlib, signal, subprocess, sys, time
log = pathlib.Path(os.environ['FAKE_DOCKER_LOG'])
state_path = pathlib.Path(os.environ['FAKE_DOCKER_STATE'])
state = json.loads(state_path.read_text()) if state_path.exists() else {}
args = sys.argv[1:]
with log.open('a') as out: out.write(json.dumps(args) + '\\n')
def inspect(ref):
    item = state.get(ref)
    if not item: raise SystemExit(1)
    print(json.dumps([item]))
if args[:2] == ['image', 'inspect']:
    inspect(args[2])
elif args and args[0] == 'pull':
    if os.environ.get('FAKE_DOCKER_BLOCK') == '1':
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        pathlib.Path(os.environ['FAKE_DOCKER_CHILD_PID']).write_text(str(child.pid))
        while True: time.sleep(1)
    catalog = json.loads(os.environ['FAKE_DOCKER_CATALOG'])
    state[args[1]] = catalog[args[1]]
    state_path.write_text(json.dumps(state))
elif args[:2] == ['manifest', 'inspect']:
    if os.environ.get('FAKE_DOCKER_MANIFEST_INSPECT_FAIL') == '1':
        raise SystemExit(1)
    print(json.dumps(json.loads(os.environ['FAKE_DOCKER_MANIFESTS'])[args[2]]))
elif args[:4] == ['buildx', 'imagetools', 'inspect', '--raw']:
    print(json.dumps(json.loads(os.environ['FAKE_DOCKER_MANIFESTS'])[args[4]], separators=(',', ':')))
elif args[:2] == ['image', 'save']:
    if '--output' in args:
        output = pathlib.Path(args[args.index('--output') + 1])
        output.write_bytes(b'frozen-runtime-archive')
    else:
        sys.stdout.buffer.write(b'frozen-runtime-archive')
elif args[:2] == ['image', 'load']:
    pass
else:
    raise SystemExit('unsupported fake docker argv: ' + repr(args))
""")
        self.fake_docker.chmod(0o755)

    def tearDown(self):
        self.tempdir.cleanup()

    def inspect_item(self, image):
        return {
            "RepoDigests": [image["reference"]],
            "Id": image["config_digest"],
            "Architecture": "arm64",
            "Os": "linux",
            "RootFS": {"Layers": ["sha256:diff-a", "sha256:diff-b"]},
        }

    def manifest_item(self, image):
        return {
            "schemaVersion": 2,
            "config": {"digest": image["config_digest"]},
            "layers": compressed_layers(image),
        }

    def env(self, state=None, **extra):
        catalog = {
            image["reference"]: self.inspect_item(image)
            for image in (self.lock["runtime_image"], self.lock["gateway_image"])
        }
        manifests = {}
        for image in (self.lock["runtime_image"], self.lock["gateway_image"]):
            manifests[image["reference"]] = self.manifest_item(image)
        if state is not None:
            self.state.write_text(json.dumps(state))
        return {
            **os.environ,
            "DEEPSEEK_ARTIFACT_TEST_SEAM": "1",
            "DEEPSEEK_ARTIFACT_RECEIPT_TEST_SEAM": "1",
            "FAKE_DOCKER_LOG": str(self.log),
            "FAKE_DOCKER_STATE": str(self.state),
            "FAKE_DOCKER_CATALOG": json.dumps(catalog),
            "FAKE_DOCKER_MANIFESTS": json.dumps(manifests),
            **extra,
        }

    def run_prepare(self, *args, env=None, check=True):
        return subprocess.run(
            [str(PREPARE), *receipted_argv(args, self.base)], check=check, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env or self.env(),
        )

    def acquire_args(self, role="head"):
        return (
            "acquire-images", "--role", role, "--lock", self.lock_path,
            "--journal", self.journal, "--rx-counter", self.counter,
            "--test-docker-command-json", json.dumps([str(self.fake_docker)]),
            "--poll-seconds", "0.01",
        )

    def test_exact_cache_and_journal_skip_without_pull(self):
        state = {}
        images = {}
        for name in ("runtime_image", "gateway_image"):
            image = self.lock[name]
            state[image["reference"]] = self.inspect_item(image)
            images[image["manifest_digest"]] = {
                "reference": image["reference"],
                "manifest_digest": image["manifest_digest"],
                "config_digest": image["config_digest"],
                "platform": image["platform"],
                "compressed_layer_bytes": image["compressed_layer_bytes"],
                "compressed_layers": self.manifest_item(image)["layers"],
                "rootfs_diff_ids": ["sha256:diff-a", "sha256:diff-b"],
            }
        self.journal.write_text(json.dumps({"schema_version": 1, "images": images,
                                            "acquisition": acquisition_state(self.lock, last=1000, pulls=0)}))
        result = self.run_prepare(*self.acquire_args(), env=self.env(state))
        self.assertIn("IMAGE_ACQUISITION_COMPLETE pulls=0", result.stdout)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertFalse(any(call and call[0] == "pull" for call in calls))
        self.assertFalse(any(call[:2] == ["manifest", "inspect"] for call in calls))

    def test_cache_and_invalid_budget_states_cannot_bypass_acquisition_provenance(self):
        state = {}
        images = {}
        for name in ("runtime_image", "gateway_image"):
            image = self.lock[name]
            state[image["reference"]] = self.inspect_item(image)
            images[image["manifest_digest"]] = {
                "reference": image["reference"], "manifest_digest": image["manifest_digest"],
                "config_digest": image["config_digest"], "platform": image["platform"],
                "compressed_layer_bytes": image["compressed_layer_bytes"],
                "compressed_layers": self.manifest_item(image)["layers"],
                "rootfs_diff_ids": ["sha256:diff-a", "sha256:diff-b"],
            }
        cases = {
            "missing": None,
            "overspent": acquisition_state(self.lock, last=1000 + 12000000000, pulls=2),
            "overcalls": acquisition_state(self.lock, last=1000, pulls=3),
        }
        for name, acquisition in cases.items():
            with self.subTest(name=name):
                value = {"schema_version": 1, "images": images if name == "missing" else {}}
                if acquisition is not None:
                    value["acquisition"] = acquisition
                self.journal.write_text(json.dumps(value))
                result = self.run_prepare(*self.acquire_args(), env=self.env(state), check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertRegex(result.stderr, "acquisition provenance|invalid or reset VPN")
                self.log.unlink(missing_ok=True)

        # A stale journal must not hide that the live cumulative counter has
        # crossed the cap since its last recorded observation, even on a
        # complete exact-digest cache hit.
        self.journal.write_text(json.dumps({
            "schema_version": 1, "images": images,
            "acquisition": acquisition_state(self.lock, last=1000, pulls=0),
        }))
        self.counter.write_text(f"{1000 + 12000000000}\n")
        live_overspent = self.run_prepare(*self.acquire_args(), env=self.env(state), check=False)
        self.assertNotEqual(live_overspent.returncode, 0)
        self.assertIn("invalid or reset VPN", live_overspent.stderr)
        self.assertFalse(self.log.exists())

    def test_missing_images_use_only_two_exact_digest_pull_calls(self):
        result = self.run_prepare(*self.acquire_args(), env=self.env({}))
        self.assertIn("IMAGE_ACQUISITION_COMPLETE pulls=2", result.stdout)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        pulls = [call for call in calls if call and call[0] == "pull"]
        manifests = [call for call in calls if call[:2] == ["manifest", "inspect"]]
        self.assertEqual(pulls, [["pull", self.lock["runtime_image"]["reference"]],
                                 ["pull", self.lock["gateway_image"]["reference"]]])
        self.assertTrue(all("@sha256:" in call[1] for call in pulls))
        self.assertEqual(manifests, [
            ["manifest", "inspect", self.lock["runtime_image"]["reference"]],
            ["manifest", "inspect", self.lock["gateway_image"]["reference"]],
        ])

    def test_oci_manifest_falls_back_to_buildx_raw_and_accepts_docker_hub_canonical_repo_digest(self):
        gateway = self.lock["gateway_image"]
        gateway_item = self.inspect_item(gateway)
        gateway_item["RepoDigests"] = [
            gateway["reference"].removeprefix("docker.io/library/")
        ]
        state = {gateway["reference"]: gateway_item}
        images = {
            gateway["manifest_digest"]: {
                "reference": gateway["reference"],
                "manifest_digest": gateway["manifest_digest"],
                "config_digest": gateway["config_digest"],
                "platform": gateway["platform"],
                "compressed_layer_bytes": gateway["compressed_layer_bytes"],
                "compressed_layers": self.manifest_item(gateway)["layers"],
                "rootfs_diff_ids": ["sha256:diff-a", "sha256:diff-b"],
            }
        }
        self.journal.write_text(json.dumps({
            "schema_version": 1,
            "images": images,
            "acquisition": acquisition_state(self.lock, last=1000, pulls=0),
        }))
        result = self.run_prepare(
            *self.acquire_args(),
            env=self.env(state, FAKE_DOCKER_MANIFEST_INSPECT_FAIL="1"),
        )
        self.assertIn("IMAGE_ACQUISITION_COMPLETE pulls=1", result.stdout)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        runtime = self.lock["runtime_image"]
        self.assertIn(["manifest", "inspect", runtime["reference"]], calls)
        self.assertIn(["buildx", "imagetools", "inspect", "--raw", runtime["reference"]], calls)

    def test_worker_registry_pull_is_rejected_before_docker(self):
        result = self.run_prepare(*self.acquire_args("worker"), env=self.env({}), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.returncode, 75)
        self.assertIn("running mutation receipt binding is invalid", result.stderr)
        self.assertFalse(self.log.exists())

    def test_production_acquisition_rejects_nonfixed_vpn_counter_before_docker(self):
        result = self.run_prepare(
            "acquire-images", "--role", "head", "--lock", self.lock_path,
            "--journal", self.journal, "--rx-counter", self.base / "other-counter",
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fixed dgx-vpn RX counter", result.stderr)
        self.assertFalse(self.log.exists())

    def test_test_command_injection_is_rejected_without_marked_seam(self):
        env = self.env({})
        env.pop("DEEPSEEK_ARTIFACT_TEST_SEAM")
        result = self.run_prepare(*self.acquire_args(), env=env, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("test command injection requires DEEPSEEK_ARTIFACT_TEST_SEAM=1", result.stderr)
        self.assertFalse(self.log.exists())

    def test_pull_call_budget_is_cumulative_in_journal_across_invocations(self):
        self.journal.write_text(json.dumps({
            "schema_version": 1,
            "images": {},
            "acquisition": acquisition_state(self.lock, last=1000),
        }))
        result = self.run_prepare(*self.acquire_args(), env=self.env({}), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("maximum exact digest pull calls exhausted", result.stderr)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertFalse(any(call and call[0] == "pull" for call in calls))

    def test_vpn_cap_terminates_exact_pull_process_group_only(self):
        child_pid = self.base / "pull-child.pid"
        unrelated = subprocess.Popen(["sleep", "60"], start_new_session=True)
        env = self.env({}, FAKE_DOCKER_BLOCK="1", FAKE_DOCKER_CHILD_PID=str(child_pid))
        proc = subprocess.Popen(
            [str(PREPARE), *receipted_argv(self.acquire_args(), self.base)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        try:
            deadline = time.monotonic() + 5
            while not child_pid.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(child_pid.exists(), "fake pull process group did not start")
            replacement = self.counter.with_suffix(".next")
            replacement.write_text(str(1000 + 12000000000) + "\n")
            os.replace(replacement, self.counter)
            stdout, stderr = proc.communicate(timeout=5)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("VPN RX budget reached", stderr)
            journal = json.loads(self.journal.read_text())
            self.assertEqual(journal["acquisition"]["pull_calls"], 1)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(child_pid.read_text()), 0)
            self.assertIsNone(unrelated.poll(), "unrelated process was terminated")
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            unrelated.terminate()
            unrelated.wait()

    def test_runtime_archive_is_hashed_before_load_and_postload_identity_is_checked(self):
        runtime = self.lock["runtime_image"]
        item = self.inspect_item(runtime)
        entries = {}
        for image in (runtime, self.lock["gateway_image"]):
            entries[image["manifest_digest"]] = {
                "reference": image["reference"], "manifest_digest": image["manifest_digest"],
                "config_digest": image["config_digest"], "platform": image["platform"],
                "compressed_layer_bytes": image["compressed_layer_bytes"],
                "compressed_layers": self.manifest_item(image)["layers"],
                "rootfs_diff_ids": ["sha256:diff-a", "sha256:diff-b"],
            }
        self.journal.write_text(json.dumps({"schema_version": 1, "images": entries,
                                            "acquisition": acquisition_state(self.lock, last=1000)}))
        archive = self.base / "runtime.tar"
        receipt = self.base / "runtime-receipt.json"
        env = self.env({runtime["reference"]: item})
        saved = self.run_prepare(
            "save-runtime-archive", "--role", "head", "--lock", self.lock_path,
            "--journal", self.journal, "--archive", archive, "--receipt", receipt,
            "--test-docker-command-json", json.dumps([str(self.fake_docker)]),
            env=env,
        )
        self.assertIn("RUNTIME_ARCHIVE_SAVED", saved.stdout)
        save_calls = [
            json.loads(line)
            for line in self.log.read_text().splitlines()
            if json.loads(line)[:2] == ["image", "save"]
        ]
        self.assertEqual(save_calls, [["image", "save", runtime["reference"]]])
        frozen = json.loads(receipt.read_text())
        self.assertEqual(frozen["archive_size"], archive.stat().st_size)
        self.assertEqual(frozen["archive_sha256"], sha256(archive))
        docker_log_before_wrong_path = self.log.read_text()
        wrong_worker_path = self.run_prepare(
            "load-runtime-archive", "--role", "worker", "--lock", self.lock_path,
            "--archive", archive, "--receipt", receipt,
            env=env, check=False,
        )
        self.assertNotEqual(wrong_worker_path.returncode, 0)
        self.assertIn("fixed fabric archive path", wrong_worker_path.stderr)
        self.assertEqual(self.log.read_text(), docker_log_before_wrong_path)
        rsync = self.base / "archive-rsync.py"
        rsync_record = self.base / "archive-rsync.json"
        rsync.write_text("""#!/usr/bin/env python3
import json, os, sys
with open(os.environ['ARCHIVE_RSYNC_RECORD'], 'w') as out: json.dump(sys.argv[1:], out)
""")
        rsync.chmod(0o755)
        copied = self.run_prepare(
            "copy-runtime-archive", "--lock", self.lock_path, "--archive", archive,
            "--receipt", receipt, "--journal", self.journal, "--test-rsync-bin", rsync,
            env={**env, "ARCHIVE_RSYNC_RECORD": str(rsync_record)},
        )
        self.assertIn("RUNTIME_ARCHIVE_COPY_COMPLETE", copied.stdout)
        self.assertEqual(json.loads(rsync_record.read_text()), [
            "--archive", "--partial", "--protect-args", "-e", FIXED_RSYNC_SHELL,
            str(archive), str(receipt), str(self.journal), FIXED_ARCHIVE_DIRECTORY_DESTINATION,
        ])
        worker_item = dict(item)
        worker_item.pop("RepoDigests")
        # Docker save/load preserves the config ID and RootFS but can load a
        # digest-only source as an untagged image with no RepoDigest alias.
        self.state.write_text(json.dumps({runtime["config_digest"]: worker_item}))
        loaded = self.run_prepare(
            "load-runtime-archive", "--role", "worker", "--lock", self.lock_path,
            "--archive", archive, "--receipt", receipt,
            "--test-docker-command-json", json.dumps([str(self.fake_docker)]),
            env=env,
        )
        self.assertIn("RUNTIME_ARCHIVE_LOADED_AND_VERIFIED", loaded.stdout)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertIn(["image", "load", "--input", str(archive)], calls)
        self.assertFalse(any(call and call[0] == "pull" for call in calls))

        archive.write_bytes(b"tampered")
        rejected = self.run_prepare(
            "load-runtime-archive", "--role", "worker", "--lock", self.lock_path,
            "--archive", archive, "--receipt", receipt,
            "--test-docker-command-json", json.dumps([str(self.fake_docker)]),
            env=env, check=False,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("archive size/SHA-256 mismatch", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
