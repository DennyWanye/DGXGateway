import assert from "node:assert/strict";
import {createRequire} from "node:module";
const require = createRequire(import.meta.url);
const ui = require("../DGXGateway/static/ui-core.js");

assert.equal(ui.serviceLabel("comfyui"), "ComfyUI 制作服务");
assert.equal(ui.serviceLabel("minimax-music3"), "ComfyUI 制作服务");
assert.equal(ui.serviceLabel("minimax-h3"), "MiniMax H3 双机");
assert.equal(ui.runtimeSummary({active_local_model: {state: "NONE"}}).title, "没有运行中的本地模型服务");
const base = {active_local_model: {state: "ACTIVE", model_id: "comfyui"}};
assert.ok(!ui.runtimeSummary(base).title.includes("music3"));
assert.match(ui.runtimeSummary(base).detail, /待确认/);
const snapshot = {...base, runtime: {phase_label: "工作流执行中", workload: {model_name: "Qwen Image Edit 2511 · FP8", job_id: "job-a"}, origin: {label: "制作任务自动启动"}}};
assert.match(ui.runtimeSummary(snapshot).detail, /Qwen Image Edit/);
assert.match(ui.runtimeSummary(snapshot).detail, /job-a/);
assert.match(ui.runtimeSummary(snapshot).detail, /自动启动/);
const resident = {residency: {state: "IDLE", can_release: true, owner_job_id: "job-a"}};
assert.equal(ui.residencyAction(resident, {authenticated: true}).disabled, false);
assert.equal(ui.residencyAction(resident, {authenticated: false}).disabled, true);
assert.equal(ui.residencyAction(resident, {authenticated: true, snapshotError: true}).disabled, true);
assert.equal(ui.residencyAction(resident, {authenticated: true, submitting: true}).disabled, true);
assert.equal(ui.residencyAction({residency: {state: "IDLE", can_release: false}}, {authenticated: true}).disabled, true);
assert.equal(ui.residencyAction({residency: {state: "NONE"}}).visible, false);
assert.equal(ui.residencyAction(resident, {authenticated: true}).ownerJobId, "job-a");
const unlocked = {...base, lock: {state: "UNLOCKED"}, current_operation: null, latest_operation: null};
for (const phase of ["QUEUED", "LOADING", "PREPARING", "SUBMITTING", "RUNNING", "RELEASING", "CANCELING", "UNKNOWN", "FUTURE"] ) {
  const controls = ui.controlState({...unlocked, runtime: {phase, workload: {job_id: "current-job"}}}, {authenticated: true});
  assert.equal(controls.disabled, true, `active task ${phase} must block manual switches despite unlocked deployment lease`);
  assert.match(controls.reason, /current-job/);
  assert.ok(!controls.reason.includes("可以发起"));
  assert.equal(ui.modelActionState(unlocked, {model_id: "minimax-h3", state: "STOPPED"}, controls).disabled, true);
}
for (const state of ["IDLE", "ACTIVE", "CLAIMED", "RELEASING", "RECOVERY_REQUIRED", "FUTURE"]) {
  const controls = ui.controlState({...unlocked, residency: {...resident.residency, state}}, {authenticated: true});
  assert.equal(controls.disabled, true, `residency ${state} must block manual switch`);
  if (state === "IDLE") assert.match(controls.reason, /先.*释放/);
}
assert.equal(ui.controlState({...unlocked, residency: {state: "NONE"}}, {authenticated: true}).disabled, false);
assert.equal(ui.controlState({...unlocked, runtime: {phase: "UNKNOWN", workload: null}}, {authenticated: true}).disabled, true);
assert.equal(ui.residencyAction({...unlocked, ...resident}, {authenticated: true}).disabled, false, "manual release is independent of disabled switch controls");
console.log("Runtime service/workflow labels and residency release guards: PASS");
