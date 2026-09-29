from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/deepseek-v4-flash-vision-exp/oci_release.py"


def load_module():
    spec = importlib.util.spec_from_file_location("dsv4_vision_oci_release", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load OCI release tool")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OCI = load_module()


class OciReleaseContractTest(unittest.TestCase):
    def test_production_closure_and_toolchain_are_exactly_pinned(self):
        self.assertEqual(len(OCI.LAYERS), 37)
        self.assertEqual(len(set(digest for digest, _ in OCI.LAYERS)), 34)
        self.assertEqual(sum(size for _, size in OCI.LAYERS), 9_675_672_165)
        self.assertEqual(len(OCI.ROOTFS_DIFF_IDS), 37)
        self.assertEqual(
            OCI.SOURCE,
            "docker://docker.io/vllm/vllm-openai@"
            "sha256:8568b4bbc821903d93a0a9c17dd80382fdc0ba78eaa128e3eb5cb71c3bf06b79",
        )
        self.assertEqual(OCI.CONFIG_DIGEST, "sha256:b12d77aecd6ca64639f55ab946f90342ba108ae1c32e4c545242722eff3b04d1")
        self.assertEqual(OCI.SKOPEO_PACKAGE_VERSION, "1.13.3+ds1-2ubuntu0.24.04.3")
        self.assertRegex(OCI.SKOPEO_SHA256, r"^[0-9a-f]{64}$")
        self.assertRegex(OCI.POLICY_SHA256, r"^[0-9a-f]{64}$")

    def test_rejects_mutable_tags_credentials_and_tls_weakening(self):
        rejected = (
            "docker://docker.io/vllm/vllm-openai:latest",
            "docker://docker.io/vllm/vllm-openai:nightly",
            OCI.SOURCE + " --tls-verify=false",
            OCI.SOURCE + " --insecure-policy",
            OCI.SOURCE.replace("docker.io/", "user:secret@docker.io/"),
        )
        for source in rejected:
            with self.subTest(source=source), self.assertRaises(OCI.ReleaseError):
                OCI.validate_source(source)

    def test_proxy_is_loopback_socks_only_and_scrubs_ambient_environment(self):
        accepted = OCI.proxy_environment("socks5://127.0.0.1:31080")
        self.assertEqual(accepted["HTTPS_PROXY"], "socks5://127.0.0.1:31080")
        self.assertEqual(accepted["NO_PROXY"], "127.0.0.1,localhost")
        self.assertNotIn("DOCKER_AUTH_CONFIG", accepted)
        for value in (
            "http://127.0.0.1:31080",
            "socks5h://127.0.0.1:31080",
            "socks5://0.0.0.0:31080",
            "socks5://localhost:31080",
            "socks5://user:secret@127.0.0.1:31080",
            "socks5://127.0.0.1:80",
            "socks5://127.0.0.1:bad",
            "socks5://127.0.0.1:31080/path",
        ):
            with self.subTest(value=value), self.assertRaises(OCI.ReleaseError):
                OCI.proxy_environment(value)

    def test_path_validation_rejects_relative_traversal_and_symlink(self):
        for value in (Path("relative"), Path("/"), Path("/tmp/../etc")):
            with self.subTest(value=value), self.assertRaises(OCI.ReleaseError):
                OCI.validate_absolute_path(value)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            link = root / "link"
            link.symlink_to(root / "target")
            with self.assertRaisesRegex(OCI.ReleaseError, "symlink"):
                OCI.validate_absolute_path(link / "child", allow_missing_leaf=True)
        for value in ("relative", "/tmp/../root", "/tmp/value;id", "/tmp/value with-space"):
            with self.subTest(value=value), self.assertRaises(OCI.ReleaseError):
                OCI.safe_remote_path(value)

    def test_fixture_dir_requires_exact_set_hash_size_and_config_oracle(self):
        layer_raw = b"compressed-layer-fixture"
        layer_digest = "sha256:" + hashlib.sha256(layer_raw).hexdigest()
        config_value = {
            "os": "linux",
            "architecture": "arm64",
            "rootfs": {"type": "layers", "diff_ids": ["sha256:fixture-diff"]},
            "config": {"Entrypoint": ["vllm", "serve"]},
        }
        config_raw = json.dumps(config_value, separators=(",", ":")).encode()
        config_digest = "sha256:" + hashlib.sha256(config_raw).hexdigest()
        patches = (
            mock.patch.object(OCI, "LAYERS", ((layer_digest, len(layer_raw)),)),
            mock.patch.object(OCI, "LAYER_COUNT", 1),
            mock.patch.object(OCI, "COMPRESSED_BYTES", len(layer_raw)),
            mock.patch.object(OCI, "CONFIG_DIGEST", config_digest),
            mock.patch.object(OCI, "CONFIG_SIZE", len(config_raw)),
            mock.patch.object(OCI, "ROOTFS_DIFF_IDS", ("sha256:fixture-diff",)),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            manifest_raw = json.dumps(OCI.expected_manifest(), separators=(",", ":")).encode()
            manifest_digest = "sha256:" + hashlib.sha256(manifest_raw).hexdigest()
            with mock.patch.object(OCI, "MANIFEST_DIGEST", manifest_digest), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                (root / "manifest.json").write_bytes(manifest_raw)
                (root / "version").write_text("Directory Transport Version: 1.1\n")
                (root / config_digest.removeprefix("sha256:")).write_bytes(config_raw)
                layer_path = root / layer_digest.removeprefix("sha256:")
                layer_path.write_bytes(layer_raw)
                result = OCI.verify_dir(root, expected_uid=None)
                self.assertEqual(result["layer_count"], 1)

                extra = root / "unexpected"
                extra.write_bytes(b"x")
                with self.assertRaisesRegex(OCI.ReleaseError, "missing or extra"):
                    OCI.verify_dir(root, expected_uid=None)
                extra.unlink()
                layer_path.write_bytes(b"tampered")
                with self.assertRaisesRegex(OCI.ReleaseError, "layer closure"):
                    OCI.verify_dir(root, expected_uid=None)

    def test_loaded_image_oracle_does_not_require_repo_digest(self):
        item = {
            "Id": OCI.CONFIG_DIGEST,
            "Os": "linux",
            "Architecture": "arm64",
            "RootFS": {"Layers": list(OCI.ROOTFS_DIFF_IDS)},
            "Config": {"Entrypoint": ["vllm", "serve"]},
        }
        OCI.inspect_identity(item)
        item["Architecture"] = "amd64"
        with self.assertRaisesRegex(OCI.ReleaseError, "loaded image"):
            OCI.inspect_identity(item)

    def test_commands_keep_tls_policy_and_fabric_ssh_hardening(self):
        command = OCI.skopeo_base("socks5://127.0.0.1:31080")
        self.assertIn("--policy", command)
        self.assertNotIn("--insecure-policy", command)
        self.assertNotIn("--tls-verify=false", command)
        self.assertEqual(command[:5], ["sudo", "-n", "-u", "#996", "env"])
        ssh = OCI.ssh_base()
        self.assertIn("BatchMode=yes", ssh)
        self.assertIn("StrictHostKeyChecking=yes", ssh)
        self.assertEqual(ssh[-1], "operator@192.0.2.34")
        source = SCRIPT.read_text()
        self.assertIn('sudo -n docker image load --input', source)
        self.assertIn('sudo -n docker image inspect', source)


if __name__ == "__main__":
    unittest.main()
