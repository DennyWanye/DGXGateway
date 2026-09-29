import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {createRequire} from "node:module";

const require = createRequire(import.meta.url);
const ui = require("../DGXGateway/static/ui-core.js");

function snapshot(overrides = {}) {
  return {
    lock: {state: "UNLOCKED"},
    active_local_model: {state: "NONE", model_id: null, profile: null},
    current_operation: null,
    latest_operation: null,
    ...overrides,
  };
}

assert.deepEqual(ui.controlState(snapshot(), {authenticated: true}), {
  disabled: false, busy: false, reason: "可以发起独占切换",
});
assert.equal(ui.controlState(snapshot(), {authenticated: false}).disabled, true);
assert.equal(ui.controlState(snapshot(), {authenticated: true, submitting: true}).busy, true);
assert.equal(ui.controlState(snapshot({lock: {state: "ACTIVE"}}), {authenticated: true}).disabled, true);
assert.equal(ui.controlState(snapshot({lock: {state: "UNKNOWN"}}), {authenticated: true}).disabled, true);

const active = {
  phase: "TARGET_STARTING",
  progress: {known: true, message: "正在启动目标模型"},
};
assert.equal(ui.controlState(snapshot({current_operation: active}), {authenticated: true}).reason, "正在启动目标模型");

const recovery = {
  phase: "RECOVERY_REQUIRED",
  progress: {known: true, message: "需要恢复"},
};
assert.equal(ui.controlState(snapshot({latest_operation: recovery}), {authenticated: true}).disabled, true);

const unknown = {
  phase: "FUTURE",
  progress: {known: false, message: "未知"},
};
assert.equal(ui.controlState(snapshot({latest_operation: unknown}), {authenticated: true}).busy, true);
assert.equal(ui.controlState(null, {authenticated: true, snapshotError: true}).disabled, true);
assert.equal(ui.controlState(snapshot({active_local_model: {state: "AMBIGUOUS"}}), {authenticated: true}).disabled, true);
assert.equal(ui.controlState(snapshot({active_local_model: null}), {authenticated: true}).disabled, true);

const enabledControls = ui.controlState(snapshot(), {authenticated: true});
const fixedModel = {model_id: "deepseek-v4-flash", state: "HEALTHY", mutable: true};
const activeFixed = snapshot({active_local_model: {state: "ACTIVE", model_id: fixedModel.model_id, profile: null}});
assert.deepEqual(ui.modelActionState(activeFixed, fixedModel, enabledControls, null), {
  disabled: true, current: true, label: "当前模型",
});
assert.equal(ui.modelActionState(snapshot(), fixedModel, enabledControls, null).disabled, false);

const h3Model = {model_id: "minimax-h3", state: "HEALTHY", mutable: true};
const activeH3 = snapshot({active_local_model: {state: "ACTIVE", model_id: h3Model.model_id, profile: "10eros-fl2va-v1"}});
assert.deepEqual(ui.modelActionState(activeH3, h3Model, enabledControls, "10eros-fl2va-v1"), {
  disabled: true, current: true, label: "当前配置",
});
assert.deepEqual(ui.modelActionState(activeH3, h3Model, enabledControls, "heretic-ref2va-v1"), {
  disabled: false, current: false, label: "切换到此配置",
});
const ringModel = {model_id: "dsv41-ring-tp4", state: "HEALTHY", mutable: true, profiles: ["smoke32k", "text256k", "text512k"]};
const activeRing = snapshot({active_local_model: {state: "ACTIVE", model_id: "dsv41-ring-tp4", profile: "smoke32k"}});
assert.deepEqual(ui.modelActionState(activeRing, ringModel, enabledControls, "text256k"), {
  disabled: false, current: false, label: "应用并重启",
});
assert.equal(ui.modelActionState(snapshot(), {model_id: "personaplex", state: "UNAVAILABLE", mutable: false}, enabledControls, null).label, "不可启动");

const node = (role, overrides = {}) => ({
  role,
  online: true,
  stale: false,
  gpu: {temperature_c: role === "head" ? 50 : 55, utilization_percent: 40, power_watts: 60},
  unified_memory: {total_bytes: 1000, available_bytes: 250},
  ...overrides,
});
const success = (sampledAt = "2026-09-03T00:00:00Z") => ({
  sampled_at: sampledAt,
  nodes: [node("head"), node("worker"), node("rank2"), node("rank3")],
});

assert.equal(ui.TELEMETRY_SERIES.length, 16);
assert.deepEqual(ui.NODE_IDENTITIES, {
  head: {machine: "DGX 1", address: "192.0.2.41", roleLabel: "主控 / API", technicalRole: "Head"},
  worker: {machine: "DGX 2", address: "192.0.2.42", roleLabel: "计算节点", technicalRole: "Worker"},
  rank2: {machine: "DGX 3", address: "192.0.2.39", roleLabel: "计算节点", technicalRole: "Rank 2"},
  rank3: {machine: "DGX 4", address: "192.0.2.40", roleLabel: "计算节点", technicalRole: "Rank 3"},
});
assert.deepEqual(ui.TELEMETRY_SERIES.map((series) => series.label), [
  "DGX 1 温度", "DGX 2 温度", "DGX 3 温度", "DGX 4 温度",
  "DGX 1 内存", "DGX 2 内存", "DGX 3 内存", "DGX 4 内存",
  "DGX 1 功耗", "DGX 2 功耗", "DGX 3 功耗", "DGX 4 功耗",
  "DGX 1 利用率", "DGX 2 利用率", "DGX 3 利用率", "DGX 4 利用率",
]);
assert.equal(ui.memoryUsedPercent({total_bytes: 100, available_bytes: 25}), 75);
assert.equal(ui.memoryUsedPercent({total_bytes: 100, available_bytes: -20}), 100);
assert.equal(ui.memoryUsedPercent({total_bytes: 100, available_bytes: 120}), 0);
assert.equal(ui.memoryUsedPercent({total_bytes: 0, available_bytes: 0}), null);
assert.equal(ui.memoryUsedPercent({total_bytes: 100, available_bytes: null}), null);
assert.equal(ui.memoryUsedPercent({total_bytes: null, available_bytes: 50}), null);

let history = [];
for (let index = 1; index <= 181; index += 1) {
  history = ui.appendTelemetry(history, ui.telemetryFrame({snapshot: success()}, index * 1000, index), 180);
}
assert.equal(history.length, 180);
assert.equal(history[0].sequence, 2);

const before = ui.telemetryFrame({snapshot: success("2026-09-03T00:00:10Z")}, 10000, 1);
const outage = ui.telemetryFrame({error: "HTTP 503"}, 15000, 2);
const after = ui.telemetryFrame({snapshot: success("2026-09-02T23:59:59Z")}, 20000, 3);
assert.equal(outage.status, "snapshot_error");
for (const series of ui.TELEMETRY_SERIES) {
  assert.equal(outage.values[series.key], null);
  assert.equal(ui.seriesSegments([before, outage, after], series.key).length, 2);
}

const partial = ui.telemetryFrame({snapshot: {
  sampled_at: "2026-09-03T00:00:20Z",
  nodes: [node("head"), node("worker", {stale: true})],
}}, 25000, 4);
assert.equal(partial.status, "partial");
assert.equal(partial.values["head.temperature"], 50);
assert.equal(partial.values["worker.temperature"], null);
assert.equal(ui.seriesSegments([before, partial, after], "worker.temperature").length, 2);
assert.equal(ui.seriesSegments([before, partial, after], "head.temperature").length, 1);

const late = ui.telemetryFrame({snapshot: success()}, 36001, 5);
assert.equal(ui.seriesSegments([before, late], "head.temperature").length, 2);
const emptyNodes = ui.telemetryFrame({snapshot: {sampled_at: "x", nodes: []}}, 40000, 6);
assert.equal(emptyNodes.status, "snapshot_error");

const explicitNull = ui.telemetryFrame({snapshot: {
  sampled_at: "2026-09-03T00:00:30Z",
  nodes: [node("head", {
    gpu: {temperature_c: null, utilization_percent: null, power_watts: null},
    unified_memory: {total_bytes: 1000, available_bytes: null},
  }), node("worker")],
}}, 30000, 7);
assert.equal(explicitNull.status, "partial");
for (const metric of ["temperature", "memory", "power", "utilization"]) {
  assert.equal(explicitNull.values[`head.${metric}`], null);
  assert.notEqual(explicitNull.values[`worker.${metric}`], null);
  assert.equal(ui.seriesSegments([before, explicitNull, after], `head.${metric}`).length, 2);
}

const chart = ui.chartModel([before, outage, after]);
assert.equal(chart.series.length, 16);
assert.equal(chart.series.find((series) => series.key === "head.temperature").segments.length, 2);
for (const series of chart.series) {
  for (const segment of series.segments) {
    assert.ok(segment.every((point) => Number.isFinite(point.x) && Number.isFinite(point.y)));
  }
}
const hiddenChart = ui.chartModel([before, after], ["head.temperature"]);
assert.equal(hiddenChart.series.find((series) => series.key === "head.temperature").segments.length, 0);
assert.ok(hiddenChart.series.find((series) => series.key === "worker.temperature").segments.length > 0);

const gatewayCss = readFileSync(new URL("../DGXGateway/static/app.css", import.meta.url), "utf8");
const gatewayApp = readFileSync(new URL("../DGXGateway/static/app.js", import.meta.url), "utf8");
assert.match(gatewayCss, /\.series-metric-memory\{--series-dash:7 5\}/);
assert.match(gatewayCss, /\.series-metric-power\{--series-dash:2 5\}/);
assert.match(gatewayCss, /\.series-metric-utilization\{--series-dash:12 5 2 5\}/);
assert.match(gatewayCss, /stroke-dasharray:var\(--series-dash, none\)/);
assert.equal(gatewayCss.includes("border-top:3px solid currentColor"), false);
assert.match(gatewayApp, /class="legend-swatch"/);
assert.equal(gatewayApp.includes('<span aria-hidden="true"></span>'), false);
console.log("gateway UI core tests: PASS");

// Hour window is bounded by time, not by the time the page has been open.
const now = Date.parse('2026-09-06T02:00:00Z');
const dense = Array.from({length: 1000}, (_, i) => ({...before, sequence:i,
  received_at_ms: now - (999-i)*5000}));
const hour = ui.mergeTelemetry([], dense, now);
assert.equal(hour.length,720);
assert.ok(hour.every(f => f.received_at_ms >= now-3600000));
assert.equal(ui.mergeTelemetry(hour,hour,now).length,720);
const timed=ui.chartModel(hour,[],{endTime:now,windowMs:3600000,maximumGapMs:45000});
assert.equal(timed.startTime,now-3600000);
assert.equal(timed.endTime,now);
assert.equal(timed.series[0].segments.at(-1).at(-1).x,timed.plot.right);
const gaps=ui.chartModel([{...before,received_at_ms:now-120000},{...before,received_at_ms:now}],[],{endTime:now,windowMs:3600000,maximumGapMs:45000});
assert.equal(gaps.series[0].segments.length,2);
const benchStart=performance.now();
for(let i=0;i<100;i++)ui.chartModel(hour,[],{endTime:now,windowMs:3600000,maximumGapMs:45000});
console.log(`hour chart 720 samples / 16 series: ${((performance.now()-benchStart)/100).toFixed(2)} ms/model`);

const staleLive=ui.liveTelemetryFrame({snapshot:success(new Date(now-61000).toISOString())},now,1);
assert.equal(staleLive.status,'snapshot_error');
assert.ok(Object.values(staleLive.values).every(v=>v===null));
const futureLive=ui.liveTelemetryFrame({snapshot:success(new Date(now+6000).toISOString())},now,2);
assert.ok(Object.values(futureLive.values).every(v=>v===null));
const correctLive=ui.liveTelemetryFrame({snapshot:success(new Date(now-1200).toISOString())},now,3);
assert.equal(correctLive.received_at_ms,now-1200);
assert.equal(correctLive.status,'ok');
console.log('stale/future timestamps are gaps, never relabeled as current: PASS');

// A context change on the active Vision model must stay actionable.
{
  const model = {model_id: 'deepseek-v4-flash-vision-exp', state: 'HEALTHY', profiles: ['32K','64K','128K','256K','512K']};
  const value = snapshot({active_local_model: {state: 'ACTIVE', model_id: model.model_id, profile: '256K'}});
  assert.equal(ui.modelActionState(value, model, {disabled: false}, '256K').disabled, true);
  const action = ui.modelActionState(value, model, {disabled: false}, '512K');
  assert.equal(action.disabled, false);
  assert.equal(action.label, '应用并重启');
  assert.equal(ui.modelActionState(value, model, {disabled: true, busy: true}, '512K').disabled, true);
}
console.log('Vision context change / same profile / busy guard: PASS');
