import importlib.util
import base64
import io
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "scripts/control-deepseek-v4-flash-vision-exp.py"


def load_controller():
    spec = importlib.util.spec_from_file_location("vision_controller_test", CONTROLLER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class VisionControllerContractTest(unittest.TestCase):
    def test_canary_is_decodable_and_red(self):
        from PIL import Image

        module = load_controller()
        image = Image.open(io.BytesIO(base64.b64decode(module.CANARY_PNG)))
        image.load()
        rgb = image.convert("RGB")
        self.assertEqual(rgb.size, (16, 16))
        self.assertEqual(rgb.getpixel((8, 8)), (255, 0, 0))

    def test_release_is_bound_to_acquired_task_id(self):
        module = load_controller()
        calls = []

        class Runner:
            def run(self, argv, **_kwargs):
                calls.append(argv)
                state = "ACTIVE" if "acquire" in argv else "UNLOCKED"
                return module.Completed(0, __import__("json").dumps({"state": state}), "")

        with tempfile.TemporaryDirectory() as directory:
            authority = module.LockAuthority(
                runner=Runner(), token_file=Path(directory) / "token",
                lock_root=Path(directory) / "lock",
            )
            authority.acquire("operation-123", "test")
            authority.release()
        self.assertIn("--operation-id", calls[-1])
        self.assertEqual(calls[-1][calls[-1].index("--operation-id") + 1], "operation-123")

    def test_runtime_assets_freeze_native_two_rank_contract(self):
        expected = {
            "configs/deepseek-v4-flash-vision-exp-head.env.example",
            "configs/deepseek-v4-flash-vision-exp-worker.env.example",
            "containers/deepseek-v4-flash-vision-exp/docker-compose.head.yml",
            "containers/deepseek-v4-flash-vision-exp/docker-compose.worker.yml",
            "systemd/deepseek-v4-flash-vision-exp.service",
            "systemd/deepseek-v4-flash-vision-exp-gateway.service",
            "gateway/deepseek-v4-flash-vision-exp-nginx.conf",
            "gateway/deepseek-v4-flash-vision-exp-nginx-staging.conf",
            "gateway/deepseek-v4-flash-vision-exp-nginx-maintenance.conf",
            "scripts/run-deepseek-v4-flash-vision-exp.sh",
        }
        for relative in expected:
            self.assertTrue((ROOT / relative).is_file(), relative)
        combined = "\n".join((ROOT / name).read_text() for name in expected)
        for required in (
            "deepseek-v4-flash-vision-exp",
            "com.docker.compose.project",
            "com.dgx-spark.model-id",
            "com.dgx-spark.role",
            "--served-model-name",
            "--tensor-parallel-size",
            "--pipeline-parallel-size",
            "--max-model-len",
            "--max-num-seqs",
            "--kv-cache-dtype",
            "--gpu-memory-utilization",
            "--enable-auto-tool-choice",
            "--tool-call-parser",
            "--reasoning-parser",
            "deepseek_v4",
            "vision_media_guard.VisionMediaGuard",
            "NCCL_SOCKET_IFNAME",
            "NCCL_IB_HCA",
            "NCCL_IB_GID_INDEX",
            "NCCL_NET",
            "NCCL_IB_DISABLE",
            "NCCL_IB_ROCE_VERSION_NUM",
            "/dev/infiniband:/dev/infiniband",
            "IPC_LOCK",
        ):
            self.assertIn(required, combined)
        self.assertNotIn("--speculative", combined)
        head = (ROOT / "containers/deepseek-v4-flash-vision-exp/docker-compose.head.yml").read_text()
        worker = (ROOT / "containers/deepseek-v4-flash-vision-exp/docker-compose.worker.yml").read_text()
        self.assertNotIn("external_launcher", combined)
        self.assertNotIn("torchrun", combined)
        self.assertIn("--headless", worker)
        self.assertNotIn("--headless", head)
        self.assertEqual(head.count("--gpu-memory-utilization\n      - \"0.85\""), 1)
        self.assertEqual(worker.count("--gpu-memory-utilization\n      - \"0.85\""), 1)
        self.assertIn("VLLM_HOST_IP: ${VLLM_HOST_IP}", worker)
        self.assertIn("VISION_IMAGE=sha256:REPLACE_WITH_VERIFIED_DERIVED_IMAGE_ID", combined)
        self.assertIn("verify_rank_identity", combined)

        unit = (ROOT / "systemd/deepseek-v4-flash-vision-exp.service").read_text()
        self.assertIn("User=operator\nSupplementaryGroups=docker\n", unit)

    def test_worker_assets_do_not_contain_gateway_secret_or_nginx(self):
        worker = "\n".join(
            (ROOT / name).read_text()
            for name in (
                "configs/deepseek-v4-flash-vision-exp-worker.env.example",
                "containers/deepseek-v4-flash-vision-exp/docker-compose.worker.yml",
            )
        ).lower()
        self.assertNotIn("secret", worker)
        self.assertNotIn("nginx", worker)
        self.assertNotIn("11434", worker)

    def test_gateway_borrowed_lease_orders_worker_then_head_without_acquire(self):
        module = load_controller()
        calls = []

        class Authority:
            token_file = Path("/tmp/existing.token")
            def guard(self):
                calls.append("guard")
                return {"state": "ACTIVE", "owner": "dgx-cluster-gateway", "lease_generation": "a" * 32}
            def acquire(self, *_args, **_kwargs):
                raise AssertionError("borrowed lease must not be acquired")
            def release(self):
                raise AssertionError("borrowed lease must not be released")

        with tempfile.TemporaryDirectory() as directory:
            controller = module.ClusterController(
                authority=Authority(), runner=object(),
                journal=module.Journal(Path(directory) / "journal.jsonl"),
                task_id="op-vision",
            )
            controller._mutate = lambda role, verb, transaction, **kwargs: calls.append((role, verb, kwargs)) or {"terminal": "succeeded"}
            controller.start("op-vision", _under_active=True, max_model_len=524288)
            controller.stop("op-vision", _under_active=True)
        self.assertLess(calls.index(("worker", "start", {"max_model_len": 524288})), calls.index(("head", "start", {"max_model_len": 524288})))
        self.assertLess(calls.index(("head", "stop", {})), calls.index(("worker", "stop", {})))

    def test_fail_close_head_activates_maintenance_after_rank_stop(self):
        module = load_controller()
        calls = []

        class Authority:
            token_file = Path("/tmp/existing.token")
            def guard(self):
                return {"state": "ACTIVE", "owner": "dgx-cluster-gateway", "lease_generation": "b" * 32}

        with tempfile.TemporaryDirectory() as directory:
            controller = module.ClusterController(
                authority=Authority(), runner=object(),
                journal=module.Journal(Path(directory) / "journal.jsonl"),
                task_id="op-loss",
            )
            controller._read_secret = lambda: "x" * 48
            controller._mutate = lambda role, verb, transaction, **_kwargs: calls.append((role, verb)) or {
                "terminal": "succeeded", "head_fail_closed": True,
                "maintenance_active": True, "recovery_id": transaction,
                "model_id": "deepseek-v4-flash-vision-exp",
            }
            proof = controller.fail_close_head("op-loss", _under_active=True)
        self.assertEqual(calls, [("head", "fail-close-head")])
        self.assertTrue(proof["head_fail_closed"])
        self.assertTrue(proof["maintenance_active"])

    def test_staging_gateway_is_an_explicit_loopback_only_action(self):
        module = load_controller()
        calls = []

        class Authority:
            token_file = Path("/tmp/existing.token")
            def guard(self):
                return {"state": "ACTIVE", "owner": "dgx-cluster-gateway", "lease_generation": "c" * 32}

        with tempfile.TemporaryDirectory() as directory:
            controller = module.ClusterController(
                authority=Authority(), runner=object(),
                journal=module.Journal(Path(directory) / "journal.jsonl"),
                task_id="op-staging",
            )
            controller._read_secret = lambda: "x" * 48
            controller._mutate = lambda role, verb, transaction, **kwargs: calls.append(
                (role, verb, transaction, kwargs.get("gateway_token"))
            ) or {"terminal": "succeeded"}
            controller.staging_activate_gateway("op-staging", _under_active=True)

        self.assertEqual(calls, [("head", "staging-activate-gateway", "op-staging", "x" * 48)])
        helper = (ROOT / "scripts/run-deepseek-v4-flash-vision-exp.sh").read_text()
        self.assertEqual(helper.count('install -d -m 0711 "$RUN_ROOT"'), 3)
        self.assertNotIn('install -d -m 0700 "$RUN_ROOT"', helper)
        staging = (ROOT / "gateway/deepseek-v4-flash-vision-exp-nginx-staging.conf").read_text()
        self.assertIn("staging-activate-gateway", helper)
        self.assertIn('[[ "${VISION_ROLE:-}" == "$role" ]]', helper)
        self.assertIn("sleep 15", helper)
        self.assertIn("verify_worker_stable", helper)
        self.assertIn("deepseek-v4-flash-vision-exp-ablit", helper)
        self.assertNotIn("deepseek-v4-flash-vision-exp-official", helper)
        self.assertIn("listen 127.0.0.1:11435;", staging)
        nginx_configs = "".join(
            path.read_text()
            for path in (ROOT / "gateway").glob("deepseek-v4-flash-vision-exp-nginx*.conf")
        )
        self.assertEqual(nginx_configs.count("map_hash_bucket_size 128;"), 3)
        self.assertNotIn("listen 192.0.2.30:11435", staging)
        gateway_unit = (ROOT / "systemd/deepseek-v4-flash-vision-exp-gateway.service").read_text()
        self.assertIn("docker.io/library/nginx@sha256:77d740", gateway_unit)
        self.assertIn("--read-only", gateway_unit)
        self.assertIn("--network host", gateway_unit)
        self.assertNotIn("/usr/sbin/nginx", gateway_unit)
        self.assertIn('-v "$RUN_ROOT:$RUN_ROOT:rw"', helper)
        self.assertIn('docker run --rm --network host --read-only', helper)
        self.assertIn('nginx -t -c /etc/nginx/nginx.conf >/dev/null', helper)
        self.assertIn("deadline=time.monotonic()+30", helper)
        self.assertIn("except (OSError,ValueError,urllib.error.URLError)", helper)
        self.assertIn('sudo -n python3 - "$trace_root"', helper)
        self.assertIn("native trace lacks CUDA tensor metadata", helper)
        self.assertNotIn("print('trace-proof-ok')", helper)
        self.assertIn("start=images[0]+1", helper)
        self.assertNotIn("start=images[-1]+1", helper)
        self.assertIn("start=starts[-1]", helper)

    def test_short_visual_canary_disables_reasoning_to_preserve_answer_budget(self):
        import json
        from unittest.mock import patch
        module = load_controller()
        captured = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, *_args):
                return json.dumps({"choices": [{"message": {"content": "red"}}]}).encode()
        def urlopen(request, **kwargs):
            captured.append(json.loads(request.data))
            return Response()
        controller = object.__new__(module.ClusterController)
        with patch.object(module.urllib.request, "urlopen", urlopen):
            answer, _ = controller._chat("fake-test-token", [{"role": "user", "content": "color?"}])
        self.assertEqual(answer, "red")
        self.assertEqual(captured[0]["chat_template_kwargs"], {"thinking": False, "enable_thinking": False})
        self.assertEqual(captured[0]["max_tokens"], 24)

    def test_health_runs_text_control_before_image_and_binds_both_hashes(self):
        module = load_controller()
        calls = []

        class Authority:
            def guard(self):
                return {"state": "ACTIVE", "owner": "dgx-cluster-gateway", "lease_generation": "d" * 32}

        with tempfile.TemporaryDirectory() as directory:
            controller = module.ClusterController(
                authority=Authority(), runner=object(),
                journal=module.Journal(Path(directory) / "journal.jsonl"), task_id="op-health",
            )
            controller._read_secret = lambda: "x" * 48
            def chat(_token, messages):
                has_image = isinstance(messages[0]["content"], list)
                calls.append(("chat", has_image))
                return ("red" if has_image else "no image", "2" * 64 if has_image else "1" * 64)
            controller._chat = chat
            controller._mutate = lambda role, verb, transaction, **kwargs: calls.append(
                (role, kwargs["control_body_sha256"], kwargs["request_body_sha256"])
            ) or {"terminal": "succeeded"}
            proof = controller.acceptance_health("op-health", _under_active=True)

        self.assertEqual(calls[:2], [("chat", False), ("chat", True)])
        self.assertEqual(calls[2:], [("head", "1" * 64, "2" * 64), ("worker", "1" * 64, "2" * 64)])
        self.assertTrue(proof["healthy"])


if __name__ == "__main__":
    unittest.main()
