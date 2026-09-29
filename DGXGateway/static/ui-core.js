(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.DGXGatewayUI = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function selectedOperation(snapshot) {
    return snapshot && (snapshot.current_operation || snapshot.latest_operation) || null;
  }

  function serviceLabel(modelId) {
    return modelId === "comfyui" || modelId === "minimax-music3" ? "ComfyUI 制作服务"
      : modelId === "dsv41-ring-tp4" ? "DeepSeek V4.1 Ring TP4"
      : modelId === "h3-ring4" ? "MiniMax H3 Ring4"
      : modelId === "minimax-h3" ? "MiniMax H3 双机"
      : modelId || "无";
  }

  function runtimeSummary(snapshot) {
    const active = snapshot && snapshot.active_local_model || {};
    const runtime = snapshot && snapshot.runtime;
    const title = active.state === "ACTIVE"
      ? `${serviceLabel(active.model_id)}${active.profile ? ` · ${active.profile}` : ""}`
      : active.state === "NONE" ? "没有运行中的本地模型服务" : "服务状态待确认";
    if (!runtime) return {title, detail: (active.model_id === "comfyui" || active.model_id === "minimax-music3") ? "具体工作流模型待确认；服务名称不代表正在生成音乐。" : ""};
    const task = runtime.workload;
    const parts = [runtime.phase_label];
    if (task) parts.push(`任务模型：${task.model_name || "未知（自定义工作流）"}${task.job_id ? ` · 任务 ${task.job_id}` : ""}`);
    if (runtime.origin) parts.push(`${runtime.origin.label}${runtime.origin.job_id && !task ? ` · 任务 ${runtime.origin.job_id}` : ""}`);
    if (runtime.note) parts.push(runtime.note);
    return {title, detail: parts.filter(Boolean).join("；")};
  }

  function residencyAction(snapshot, options = {}) {
    const resident = snapshot && snapshot.residency || {};
    return {
      visible: !!resident.state && resident.state !== "NONE",
      disabled: resident.can_release !== true || !resident.owner_job_id || !options.authenticated || !!options.submitting || !!options.snapshotError,
      ownerJobId: resident.owner_job_id || null,
      label: options.submitting ? "正在提交释放…" : "立即释放模型内存",
    };
  }

  function controlState(snapshot, options) {
    const settings = options || {};
    if (settings.submitting) {
      return {disabled: true, busy: true, reason: "切换请求正在提交，请稍候"};
    }
    if (!snapshot || settings.snapshotError) {
      return {disabled: true, busy: true, reason: "集群状态暂时不可用，已禁止切换"};
    }
    const operation = selectedOperation(snapshot);
    if (snapshot.current_operation) {
      return {disabled: true, busy: true, reason: operation.progress && operation.progress.message || "已有切换正在进行"};
    }
    if (operation && (operation.phase === "RECOVERY_REQUIRED" || !operation.progress || operation.progress.known === false)) {
      return {disabled: true, busy: true, reason: "集群需要恢复确认，已禁止新的切换"};
    }
    const runtime = snapshot.runtime;
    const taskId = runtime && runtime.workload && runtime.workload.job_id;
    if (taskId) {
      return {disabled: true, busy: true, actionLabel: "制作任务占用中", reason: `制作任务 ${taskId} 正在持有或核查模型资源，请等待任务结束后再切换`};
    }
    const resident = snapshot.residency;
    if (resident && resident.state !== "NONE") {
      const idle = resident.state === "IDLE";
      return {disabled: true, busy: true, actionLabel: idle ? "先释放暂留模型" : "资源状态待确认",
        reason: idle ? "模型暂留待复用；如需手动切换，请先在上方释放模型内存，或等待自动释放" : "模型资源仍被持有、释放中或需要恢复确认，暂不可手动切换"};
    }
    if (runtime && !["IDLE", "STOPPED"].includes(runtime.phase)) {
      return {disabled: true, busy: true, actionLabel: "运行状态待确认", reason: "制作任务与模型运行状态尚未确认，已禁止手动切换"};
    }
    const active = snapshot.active_local_model;
    if (!active || !["ACTIVE", "NONE"].includes(active.state) || (active.state === "ACTIVE" && !active.model_id)) {
      const state = active && active.state || "UNKNOWN";
      return {disabled: true, busy: true, reason: `本地模型状态为 ${state}，已禁止切换`};
    }
    if (!snapshot.lock || snapshot.lock.state !== "UNLOCKED") {
      const state = snapshot.lock && snapshot.lock.state || "UNKNOWN";
      return {disabled: true, busy: true, reason: `唯一部署锁为 ${state}，暂不可切换`};
    }
    if (!settings.authenticated) {
      return {disabled: true, busy: false, reason: "请先完成管理登录"};
    }
    return {disabled: false, busy: false, reason: "可以发起独占切换"};
  }

  function modelActionState(snapshot, model, controls, requestedProfile) {
    const unavailable = !model || model.state === "UNAVAILABLE" || model.mutable === false;
    if (unavailable) return {disabled: true, current: false, label: "不可启动"};
    if (!controls || controls.disabled) {
      return {disabled: true, current: false, label: controls && controls.actionLabel || (controls && controls.busy ? "切换进行中" : "切换到此模型")};
    }
    const active = snapshot && snapshot.active_local_model || {};
    const sameModel = active.state === "ACTIVE" && active.model_id === model.model_id;
    const configurable = model.model_id === "minimax-h3" || (model.profiles || []).length > 0;
    const normalizedProfile = configurable ? (requestedProfile || null) : null;
    const sameTarget = sameModel && (!configurable || (active.profile || null) === normalizedProfile);
    if (sameTarget) {
      return {disabled: true, current: true, label: configurable ? "当前配置" : "当前模型"};
    }
    return {disabled: false, current: false, label: sameModel ? (model.model_id === "minimax-h3" ? "切换到此配置" : "应用并重启") : "切换到此模型"};
  }

  const NODE_IDENTITIES = Object.freeze({
    head: Object.freeze({machine: "DGX 1", address: "192.0.2.41", roleLabel: "主控 / API", technicalRole: "Head"}),
    worker: Object.freeze({machine: "DGX 2", address: "192.0.2.42", roleLabel: "计算节点", technicalRole: "Worker"}),
    rank2: Object.freeze({machine: "DGX 3", address: "192.0.2.39", roleLabel: "计算节点", technicalRole: "Rank 2"}),
    rank3: Object.freeze({machine: "DGX 4", address: "192.0.2.40", roleLabel: "计算节点", technicalRole: "Rank 3"}),
  });

  const TELEMETRY_SERIES = Object.freeze([
    {key: "head.temperature", role: "head", metric: "temperature", unit: "°C", axis: "left", label: "DGX 1 温度"},
    {key: "worker.temperature", role: "worker", metric: "temperature", unit: "°C", axis: "left", label: "DGX 2 温度"},
    {key: "rank2.temperature", role: "rank2", metric: "temperature", unit: "°C", axis: "left", label: "DGX 3 温度"},
    {key: "rank3.temperature", role: "rank3", metric: "temperature", unit: "°C", axis: "left", label: "DGX 4 温度"},
    {key: "head.memory", role: "head", metric: "memory", unit: "%", axis: "right", label: "DGX 1 内存"},
    {key: "worker.memory", role: "worker", metric: "memory", unit: "%", axis: "right", label: "DGX 2 内存"},
    {key: "rank2.memory", role: "rank2", metric: "memory", unit: "%", axis: "right", label: "DGX 3 内存"},
    {key: "rank3.memory", role: "rank3", metric: "memory", unit: "%", axis: "right", label: "DGX 4 内存"},
    {key: "head.power", role: "head", metric: "power", unit: "W", axis: "left", label: "DGX 1 功耗"},
    {key: "worker.power", role: "worker", metric: "power", unit: "W", axis: "left", label: "DGX 2 功耗"},
    {key: "rank2.power", role: "rank2", metric: "power", unit: "W", axis: "left", label: "DGX 3 功耗"},
    {key: "rank3.power", role: "rank3", metric: "power", unit: "W", axis: "left", label: "DGX 4 功耗"},
    {key: "head.utilization", role: "head", metric: "utilization", unit: "%", axis: "right", label: "DGX 1 利用率"},
    {key: "worker.utilization", role: "worker", metric: "utilization", unit: "%", axis: "right", label: "DGX 2 利用率"},
    {key: "rank2.utilization", role: "rank2", metric: "utilization", unit: "%", axis: "right", label: "DGX 3 利用率"},
    {key: "rank3.utilization", role: "rank3", metric: "utilization", unit: "%", axis: "right", label: "DGX 4 利用率"},
  ]);

  function finite(value) {
    if (value === null || value === undefined || typeof value === "boolean") return null;
    if (typeof value === "string" && value.trim() === "") return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function clamp(value, minimum, maximum) {
    return Math.max(minimum, Math.min(maximum, value));
  }

  function memoryUsedPercent(memory) {
    const total = finite(memory && memory.total_bytes);
    const available = finite(memory && memory.available_bytes);
    if (total === null || total <= 0 || available === null) return null;
    return clamp(((total - available) / total) * 100, 0, 100);
  }

  function emptyValues() {
    return Object.fromEntries(TELEMETRY_SERIES.map((series) => [series.key, null]));
  }

  function telemetryFrame(attempt, receiveTimeMs, sequence) {
    const values = emptyValues();
    const snapshot = attempt && attempt.snapshot;
    let status = "snapshot_error";
    if (snapshot && Array.isArray(snapshot.nodes) && snapshot.nodes.length > 0) {
      status = "ok";
      for (const role of Object.keys(NODE_IDENTITIES)) {
        const node = snapshot.nodes.find((item) => item && item.role === role);
        if (!node || node.online !== true || node.stale === true) {
          status = "partial";
          continue;
        }
        const gpu = node.gpu || {};
        values[`${role}.temperature`] = finite(gpu.temperature_c);
        values[`${role}.memory`] = memoryUsedPercent(node.unified_memory);
        values[`${role}.power`] = finite(gpu.power_watts);
        values[`${role}.utilization`] = finite(gpu.utilization_percent);
        if (["temperature", "memory", "power", "utilization"].some((metric) => values[`${role}.${metric}`] === null)) status = "partial";
      }
    }
    return {
      sequence,
      received_at_ms: receiveTimeMs,
      server_sampled_at: snapshot && snapshot.sampled_at || null,
      status,
      values,
    };
  }

  function liveTelemetryFrame(attempt, nowMs, sequence) {
    const sampled = Date.parse(attempt && attempt.snapshot && attempt.snapshot.sampled_at);
    if (!Number.isFinite(sampled) || nowMs - sampled > 60000 || sampled - nowMs > 5000) {
      return telemetryFrame({error: "invalid_or_stale_sample_time"}, nowMs, sequence);
    }
    return telemetryFrame(attempt, sampled, sequence);
  }

  function appendTelemetry(history, frame, maximum) {
    const limit = maximum || 180;
    return (history || []).concat(frame).slice(-limit);
  }

  function mergeTelemetry(history, incoming, nowMs) {
    const options = arguments.length > 3 ? arguments[3] : {};
    const startMs = options.startMs != null ? options.startMs
      : options.windowMs != null ? nowMs - options.windowMs
      : nowMs - 3600000;
    const endMs = options.endMs != null ? options.endMs : nowMs + 5000;
    const bucketMs = options.bucketMs;
    const maximum = options.maxPoints || 720;
    const buckets = new Map();
    for (const frame of [...(history || []), ...(incoming || [])]) {
      const time = finite(frame.received_at_ms);
      if (time === null || time < startMs || time > endMs) continue;
      const key = bucketMs ? Math.floor(time / bucketMs) : time;
      const previous = buckets.get(key);
      if (!previous || frame.received_at_ms >= previous.received_at_ms) buckets.set(key, frame);
    }
    const values = [...buckets.values()].sort((a, b) => a.received_at_ms - b.received_at_ms);
    return values.length > maximum ? values.slice(-maximum) : values;
  }

  function seriesSegments(history, key, maximumGapMs) {
    const gap = maximumGapMs == null ? 15000 : maximumGapMs;
    const segments = [];
    let current = [];
    let previousValidTime = null;
    for (const frame of history || []) {
      const value = frame && frame.values ? frame.values[key] : null;
      const time = finite(frame && frame.received_at_ms);
      const broken = value == null || time === null;
      if (broken) {
        if (current.length) segments.push(current);
        current = [];
        previousValidTime = null;
        continue;
      }
      if (previousValidTime !== null && time - previousValidTime > gap) {
        if (current.length) segments.push(current);
        current = [];
      }
      current.push({
        sequence: frame.sequence,
        received_at_ms: time,
        server_sampled_at: frame.server_sampled_at,
        value,
      });
      previousValidTime = time;
    }
    if (current.length) segments.push(current);
    return segments;
  }

  function chartModel(history, hiddenKeys, options) {
    options = options || {};
    const frames = history || [];
    const hidden = new Set(hiddenKeys || []);
    const width = 1000;
    const height = 340;
    const plot = {left: 58, right: 940, top: 24, bottom: 292};
    const times = frames.map((frame) => finite(frame.received_at_ms)).filter((value) => value !== null);
    const endTime = options.endTime == null ? (times.length ? Math.max(...times) : 1) : options.endTime;
    const startTime = options.startTime != null ? options.startTime
      : options.windowMs ? endTime - options.windowMs
      : (times.length ? Math.min(...times) : 0);
    const segmentsFor = (key) => seriesSegments(frames, key, options.maximumGapMs);
    const leftValues = [];
    for (const series of TELEMETRY_SERIES.filter((item) => item.axis === "left")) {
      for (const segment of segmentsFor(series.key)) {
        for (const point of segment) leftValues.push(point.value);
      }
    }
    const rawMaximum = leftValues.length ? Math.max(...leftValues) : 100;
    const leftMaximum = Math.max(100, Math.ceil(rawMaximum * 1.15 / 10) * 10);
    const x = (time) => startTime === endTime ? (plot.left + plot.right) / 2
      : plot.left + ((time - startTime) / (endTime - startTime)) * (plot.right - plot.left);
    const y = (value, axis) => {
      const maximum = axis === "right" ? 100 : leftMaximum;
      return plot.bottom - (clamp(value, 0, maximum) / maximum) * (plot.bottom - plot.top);
    };
    const series = TELEMETRY_SERIES.map((definition) => ({
      ...definition,
      hidden: hidden.has(definition.key),
      segments: hidden.has(definition.key) ? [] : segmentsFor(definition.key).map((segment) => segment.map((point) => ({
        ...point,
        x: x(point.received_at_ms),
        y: y(point.value, definition.axis),
      }))),
    }));
    return {width, height, plot, leftMaximum, series, startTime, endTime};
  }

  return {
    serviceLabel,
    runtimeSummary,
    residencyAction,
    selectedOperation,
    controlState,
    modelActionState,
    NODE_IDENTITIES,
    TELEMETRY_SERIES,
    memoryUsedPercent,
    telemetryFrame,
    liveTelemetryFrame,
    appendTelemetry,
    mergeTelemetry,
    seriesSegments,
    chartModel,
  };
});
