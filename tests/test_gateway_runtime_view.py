import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from DGXGateway.dgx_gateway.runtime_view import RuntimeViewProvider, job_runtime_view, runtime_view
from DGXGateway.dgx_gateway.image_workflows import qwen_image_edit, z_image


class RuntimeViewTests(unittest.TestCase):
    def snapshot(self, state="ACTIVE", model="comfyui", op="start"):
        return {"active_local_model": {"state": state, "model_id": model}, "current_operation": None,
                "latest_operation": {"operation_id": op, "target_model": model, "phase": "RELEASED", "outcome": "COMMITTED"} if op else None}

    def job(self, graph=None, **changes):
        return {"job_id": "a" * 32, "kind": "creative", "state": "RUNNING", "start_operation_id": "start",
                "workflow": graph if graph is not None else qwen_image_edit("test", ["reference.png"]), **changes}

    def test_qwen_and_z_image_are_not_music(self):
        for graph, expected in ((qwen_image_edit("test", ["ref.png"]), "Qwen Image Edit 2511 · FP8"),
                                (z_image("test"), "Z-Image Turbo · BF16")):
            with self.subTest(expected=expected):
                view = runtime_view(self.snapshot(), [self.job(graph)])
                self.assertEqual(view["service_id"], "comfyui")
                self.assertEqual(view["service_name"], "ComfyUI 制作服务")
                self.assertEqual(view["workload"]["model_name"], expected)
                self.assertFalse(view["workload"]["observed_weights"])
                self.assertEqual(view["origin"]["kind"], "automatic_job")
                self.assertEqual(view["origin"]["job_id"], "a" * 32)

    def test_raw_unknown_and_multiple_loaders_never_guess(self):
        for workflow in ({"1": {"class_type": "MusicLoader", "inputs": {}}},
                         {**z_image("x"), "100": {"class_type": "UNETLoader", "inputs": {"unet_name": "unknown"}}}):
            row = self.job(workflow, image_request={"tool": "qwen_image_edit"})
            self.assertIsNone(job_runtime_view(row, "creative")["model_name"])

    def test_video_identity_and_phase(self):
        row = self.job(kind="video", profile="heretic-ref2va-v1", workflow={})
        view = runtime_view(self.snapshot(model="minimax-h3"), [row])
        self.assertEqual(view["workload"]["model_name"], "MiniMax H3")
        self.assertEqual(view["workload"]["profile"], "heretic-ref2va-v1")

    def test_actual_music_loader_is_music_without_renaming_service(self):
        graph = {"1": {"class_type": "UNETLoader", "inputs": {"unet_name": "minimax_music3_dit_fp16.safetensors"}}}
        view = runtime_view(self.snapshot(), [self.job(graph)])
        self.assertEqual(view["workload"]["model_name"], "MiniMax Music 3 · FP16")
        self.assertEqual(view["service_name"], "ComfyUI 制作服务")

    def test_cleanup_operation_keeps_exact_job_origin(self):
        view = runtime_view(self.snapshot(op="cleanup"), [self.job(state="RELEASING", cleanup_operation_id="cleanup")])
        self.assertEqual(view["origin"]["operation_id"], "start")
        self.assertEqual(view["phase"], "RELEASING")
        self.assertEqual(view["phase_label"], "正在处理模型资源")

    def test_no_current_operation_does_not_replay_last_loading_phase(self):
        snapshot = self.snapshot()
        snapshot["latest_operation"]["phase"] = "TARGET_STARTING"
        self.assertEqual(runtime_view(snapshot)["phase"], "IDLE")

    def test_retained_task_detail_does_not_claim_memory_released(self):
        detail = job_runtime_view(self.job(state="SUCCEEDED", memory_released=False), "creative")
        self.assertNotEqual(detail["phase"], "RELEASED")

    def test_job_phase_matrix(self):
        for state, expected in (("QUEUED", "QUEUED"), ("LOADING", "LOADING"), ("UPLOADING", "PREPARING"),
                                ("SUBMITTING", "SUBMITTING"), ("RUNNING", "RUNNING"), ("RELEASING", "RELEASING"),
                                ("CANCELING", "CANCELING"), ("SUBMISSION_UNKNOWN", "UNKNOWN"), ("FUTURE", "UNKNOWN")):
            with self.subTest(state=state):
                self.assertEqual(runtime_view(self.snapshot(), [self.job(state=state)])["phase"], expected)

    def test_history_is_not_current_weights(self):
        view = runtime_view(self.snapshot(), [self.job(state="SUCCEEDED", memory_released=True)])
        self.assertIsNone(view["workload"])
        self.assertEqual(view["origin"]["kind"], "unknown")
        self.assertEqual(view["phase"], "IDLE")

    def test_no_service_does_not_claim_active_task_weights(self):
        view = runtime_view(self.snapshot(state="NONE", model=None), [self.job()])
        self.assertEqual(view["phase"], "UNKNOWN")

    def test_foreign_lifecycle_invalidates_task_claim(self):
        view = runtime_view(self.snapshot(op="other"), [self.job()])
        self.assertEqual(view["phase"], "UNKNOWN")
        self.assertEqual(view["origin"]["kind"], "unknown")

    def test_mismatched_service_never_claims_running(self):
        self.assertEqual(runtime_view(self.snapshot(model="minimax-h3"), [self.job()])["phase"], "UNKNOWN")

    def test_multiple_active_owners_fail_closed(self):
        view = runtime_view(self.snapshot(), [self.job(), self.job(kind="video")])
        self.assertEqual(view["phase"], "UNKNOWN")
        self.assertIsNone(view["workload"])

    def test_read_failure_is_unknown_not_idle(self):
        self.assertEqual(runtime_view(self.snapshot(), read_error=True)["phase"], "UNKNOWN")

    def test_stopped_service_and_recovery(self):
        self.assertEqual(runtime_view(self.snapshot(state="NONE", model=None))["phase"], "STOPPED")
        self.assertEqual(runtime_view(self.snapshot(), [self.job(state="INTERRUPTED", cleanup_required=True)])["phase"], "UNKNOWN")

    def test_manual_api_and_restart_need_explicit_matching_evidence(self):
        for kind in ("manual", "api", "gateway_restart"):
            self.assertEqual(runtime_view(self.snapshot(), operation_origin={"kind": kind, "operation_id": "start"})["origin"]["kind"], kind)
            self.assertEqual(runtime_view(self.snapshot(), operation_origin={"kind": kind, "operation_id": "older"})["origin"]["kind"], "unknown")

    def test_verified_resident_does_not_resurrect_comfy_weights(self):
        snapshot = self.snapshot()
        snapshot["residency"] = {"state": "IDLE", "model_id": "comfyui", "owner_job_id": "b" * 32, "start_operation_id": "start"}
        view = runtime_view(snapshot)
        self.assertEqual(view["phase_label"], "空闲待复用，稍后自动释放")
        self.assertIsNone(view["workload"])

    def test_idle_resident_requires_committed_latest_start_proof(self):
        for changes in ({"phase": "TARGET_STARTING"}, {"outcome": "ROLLED_BACK"},
                        {"operation_id": "foreign"}, {"target_model": "minimax-h3"}):
            snapshot = self.snapshot()
            snapshot["latest_operation"].update(changes)
            snapshot["residency"] = {"state": "IDLE", "model_id": "comfyui", "owner_job_id": "b" * 32, "start_operation_id": "start"}
            self.assertEqual(runtime_view(snapshot)["phase"], "UNKNOWN")

    def test_idle_resident_requires_no_current_lifecycle_and_same_h3_profile(self):
        snapshot = self.snapshot(model="minimax-h3")
        snapshot["active_local_model"]["profile"] = "stable"
        snapshot["residency"] = {"state": "IDLE", "model_id": "minimax-h3", "owner_job_id": "b" * 32, "start_operation_id": "start", "profile": "stable"}
        self.assertEqual(runtime_view(snapshot)["phase"], "IDLE")
        snapshot["current_operation"] = {"operation_id": "stop", "phase": "SOURCE_DRAINING"}
        self.assertEqual(runtime_view(snapshot)["phase"], "UNKNOWN")
        snapshot["current_operation"] = None
        snapshot["active_local_model"]["profile"] = "different"
        self.assertEqual(runtime_view(snapshot)["phase"], "UNKNOWN")

    def test_terminal_resident_contract_and_conflicts(self):
        row = self.job(state="SUCCEEDED", memory_released=False, resource_settled=True, resource_status="resident")
        self.assertEqual(job_runtime_view(row, "creative")["phase"], "RESIDENT")
        for changes in ({"resource_settled": False}, {"cleanup_required": True}, {"memory_released": None}):
            self.assertEqual(job_runtime_view({**row, **changes}, "creative")["phase"], "UNKNOWN")
        self.assertEqual(job_runtime_view({**row, "memory_released": True}, "creative")["phase"], "RELEASED")

    def test_residency_recovery_and_release_are_not_idle(self):
        for state, expected in (("RECOVERY_REQUIRED", "UNKNOWN"), ("RELEASING", "RELEASING")):
            snapshot = self.snapshot()
            snapshot["residency"] = {"state": state, "model_id": "comfyui", "owner_job_id": "b" * 32}
            self.assertEqual(runtime_view(snapshot)["phase"], expected)

    def test_lifecycle_recovery_never_shows_healthy_idle(self):
        snapshot = self.snapshot()
        snapshot["latest_operation"]["phase"] = "RECOVERY_REQUIRED"
        self.assertEqual(runtime_view(snapshot)["phase"], "UNKNOWN")
        self.assertIsNone(runtime_view(snapshot, [self.job()])["workload"])

    def test_inputs_are_never_mutated(self):
        snapshot, rows = self.snapshot(), [self.job()]
        before = copy.deepcopy((snapshot, rows))
        runtime_view(snapshot, rows)
        self.assertEqual((snapshot, rows), before)

    def test_provider_reads_only_active_pointer_and_not_get(self):
        with TemporaryDirectory() as temp:
            path = Path(temp)
            row = self.job()
            (path / (row["job_id"] + ".json")).write_text(json.dumps(row))
            (path / ("b" * 32 + ".json")).write_text("not JSON, historical data must never be read")
            app = SimpleNamespace(creative_jobs=SimpleNamespace(active=row["job_id"], root=path),
                                  video_jobs=SimpleNamespace(active=None), store=SimpleNamespace(events=lambda _: []))
            self.assertEqual(RuntimeViewProvider(app)(self.snapshot())["workload"]["model_name"], "Qwen Image Edit 2511 · FP8")
            app.creative_jobs.active = "../invalid"
            self.assertEqual(RuntimeViewProvider(app)(self.snapshot())["phase"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
