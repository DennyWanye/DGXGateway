(function () {
  "use strict";

  const ui = window.DGXGatewayUI;
  const $ = (id) => document.getElementById(id);
  const state = {
    snapshot: null,
    profileDrafts: {},
    snapshotError: null,
    authenticated: false,
    submitting: false,
    pollTimer: null,
    pollInFlight: false,
    pollQueued: false,
    telemetry: [],
    telemetrySequence: 0,
    historyLoading: false,
    historyLoadedAt: 0,
    historyNote: "正在读取已保存历史",
    telemetryWindowMs: 3600000,
    telemetryStartMs: null,
    telemetryEndMs: null,
    telemetryLoading: false,
    lastTelemetryPaint: 0,
    chartSignature: null,
    chartModel: null,
    pointerFrame: null,
    hiddenSeries: new Set(),
    snapshotGeneration: 0,
    toastTimer: null,
  };

  function requestId() {
    const bytes = new Uint8Array(16);
    if (window.crypto && window.crypto.getRandomValues) {
      window.crypto.getRandomValues(bytes);
      return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
    }
    return `${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}-${Math.random().toString(16).slice(2)}`;
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>'"]/g, (character) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
    })[character]);
  }

  const fmt = (value) => value == null ? "—" : escapeHtml(value);
  const fmtBytes = (value) => value == null ? "—" : `${(value / 1024 / 1024 / 1024).toFixed(1)} GB`;
  const fmtDecimal = (value, digits = 2) => value == null || !Number.isFinite(Number(value)) ? "—" : Number(value).toFixed(digits);
  const fmtModel = (value) => escapeHtml(ui.serviceLabel(value));

  function elapsed(createdAt, endedAt) {
    const started = Date.parse(createdAt || "");
    if (!Number.isFinite(started)) return "—";
    const ended = endedAt ? Date.parse(endedAt) : Date.now();
    if (!Number.isFinite(ended)) return "—";
    const seconds = Math.max(0, Math.floor((ended - started) / 1000));
    const minutes = Math.floor(seconds / 60);
    const remainder = seconds % 60;
    return minutes ? `${minutes} 分 ${remainder} 秒` : `${remainder} 秒`;
  }

  function nodeIdentity(role, address) {
    const fallback = ui.NODE_IDENTITIES[role] || {machine: "未知节点", address: "—", roleLabel: "未知角色", technicalRole: role || "Unknown"};
    return {...fallback, address: address || fallback.address};
  }

  function nodeCard(node) {
    const online = node.online === true && node.stale !== true;
    const gpu = node.gpu || {};
    const memory = node.unified_memory || {};
    const disk = node.disk || {};
    const load = node.cpu_load || {};
    const rails = node.rails || {};
    const railText = Object.entries(rails).map(([name, rail]) => {
      const counters = rail.risk_counters || {};
      const risk = Object.values(counters).reduce((sum, value) => sum + (Number(value) || 0), 0);
      return `${escapeHtml(name)} ${fmt(rail.operstate)}/${fmt(rail.mtu)}/${fmt(rail.speed_mbps)}M · risk ${risk}`;
    }).join(" · ") || "—";
    const uptime = node.uptime_seconds == null ? "—" : `${(node.uptime_seconds / 3600).toFixed(1)}h`;
    const identity = nodeIdentity(node.role, node.address);
    return `<article class="card node-card">
      <div class="card-head"><div><p class="label node-machine">${escapeHtml(identity.machine)}</p><h3>${fmt(node.hostname)}</h3><p class="node-identity"><strong>${escapeHtml(identity.address)}</strong><span>${escapeHtml(identity.roleLabel)}（${escapeHtml(identity.technicalRole)}）</span></p></div><span class="badge ${online ? "ok" : "bad"}">${online ? "● ONLINE" : "× OFFLINE"}</span></div>
      <div class="metrics">
        <div class="metric"><span>GPU 温度 / 利用率</span><strong>${fmtDecimal(gpu.temperature_c, 1)}°C · ${fmtDecimal(gpu.utilization_percent, 1)}%</strong></div>
        <div class="metric"><span>统一内存可用</span><strong>${fmtBytes(memory.available_bytes)}</strong></div>
        <div class="metric"><span>GPU 功耗</span><strong>${fmtDecimal(gpu.power_watts, 2)} W</strong></div>
        <div class="metric"><span>磁盘可用</span><strong>${fmtBytes(disk.available_bytes)}</strong></div>
        <div class="metric"><span>CPU load (1/5/15)</span><strong>${fmtDecimal(load.one, 2)} / ${fmtDecimal(load.five, 2)} / ${fmtDecimal(load.fifteen, 2)}</strong></div>
        <div class="metric"><span>Uptime / Boot ID</span><strong>${uptime} · ${escapeHtml((node.boot_id || "—").slice(0, 8))}</strong></div>
        <div class="metric metric-wide"><span>QSFP 高速口（接口 link / MTU / speed / counters）</span><strong>${railText}</strong></div>
      </div>
      <p class="muted sample-note">采样 ${fmt(node.sampled_at)}${node.stale ? " · STALE" : ""}</p>
    </article>`;
  }

  function profileOptionLabel(modelId, profile) {
    if (modelId === "h3-ring4") {
      return {
        "fl2va-official": "P0 · fl2va-official（官方 FL2VA）",
        "ref2va-official": "P1 · ref2va-official（官方 Ref2VA）",
        "ref2va-heretic": "P2 · ref2va-heretic（官方 DiT + Heretic）",
        "hybrid-experimental": "P3 · hybrid-experimental（Hybrid，需显式开启）",
      }[profile] || profile;
    }
    return profile;
  }

  function modelCard(model, controls, snapshot) {
    const isH3 = model.model_id === "minimax-h3";
    const isRing4 = model.model_id === "h3-ring4";
    const profiles = model.profiles || [];
    const unavailable = model.state === "UNAVAILABLE";
    const selectorDisabled = controls.disabled || model.mutable === false || unavailable;
    const isContext = ["mia-dsv41-ablit", "dsv41-ring-tp4"].includes(model.model_id) && profiles.length > 0;
    const hasProfileSelector = isH3 || isContext || isRing4;
    const selected = state.profileDrafts[model.model_id] || model.profile || model.saved_profile || model.default_profile || profiles[0];
    const selectorId = isH3 ? "h3-profile" : isRing4 ? "ring4-profile" : model.model_id === "mia-dsv41-ablit" ? "mia-context" : model.model_id === "dsv41-ring-tp4" ? "ring-context" : "context-profile";
    const selectorLabel = isContext ? "上下文长度" : isH3 ? "H3 Profile" : isRing4 ? "Ring Profile（P0–P3）" : "Profile";
    const action = ui.modelActionState(snapshot, model, controls, profiles.length ? selected : null);
    const selector = hasProfileSelector ? `<label class="profile-label" for="${selectorId}">${selectorLabel}</label><select id="${selectorId}" data-profile-model="${escapeHtml(model.model_id)}" ${selectorDisabled ? "disabled" : ""}>${profiles.map((profile) => `<option value="${escapeHtml(profile)}" ${profile === selected ? "selected" : ""}>${escapeHtml(profileOptionLabel(model.model_id, profile))}</option>`).join("")}</select>` : "";
    const description = isContext ? `实际生效：${model.state === "HEALTHY" ? escapeHtml(model.profile || "未知") : "未运行"} · 输入与输出合计<br>${model.model_id === "dsv41-ring-tp4" ? "smoke32k / 256K / 512K；修改后需重载四机 Ring" : model.model_id === "mia-dsv41-ablit" ? "256K / 512K；修改后需重载模型" : "修改后需重启"}`
      : isRing4 ? `实际生效：${model.state === "HEALTHY" || model.state === "ACTIVE" ? profileOptionLabel(model.model_id, model.profile || "未知") : "未运行"} · P0–P3 为四机 Ring 同一服务，切换时独占重载`
      : fmt(model.profile || (unavailable ? "缺少已验收的生命周期服务" : "固定生产配置"));
    return `<article class="card model-card" data-model-card="${escapeHtml(model.model_id)}"${action.current ? ' data-current="true"' : ""}>
      <div class="card-head"><h3>${fmtModel(model.model_id)}</h3><span class="badge ${model.state === "HEALTHY" ? "ok" : model.state === "STOPPED" ? "" : "warn"}">${fmt(model.state)}</span></div>
      ${model.model_id === "comfyui" || model.model_id === "minimax-music3" ? '<p class="muted">ComfyUI 制作服务。图片与音乐工作流按需加载；空闲后自动回收。Music3 只是其中一种工作流，不是这个服务的名称。</p>' : ""}
      ${model.model_id === "longcat-video-avatar-1.5" ? '<p class="muted">按任务加载模型；生成与归档期间独占资源。</p><p><a href="/avatar.html">打开数字人生成</a></p>' : ""}
      ${selector}
      <p class="muted model-description">${description}</p>
      <button data-model="${escapeHtml(model.model_id)}" ${action.disabled ? "disabled" : ""}${action.current ? ' aria-current="true"' : ""}>${action.label}</button>
    </article>`;
  }

  function providerCard(provider) {
    return `<article class="card"><div class="card-head"><h3>${fmt(provider.provider_id)}</h3><span class="badge warn">${fmt(provider.state)}</span></div><p class="muted">${(provider.tasks || []).map(escapeHtml).join(" · ")}</p><p class="muted provider-reason">${fmt(provider.reason)}</p></article>`;
  }

  function operationMarkup(operation) {
    if (!operation) {
      return `<div class="operation-empty"><span class="operation-icon">✓</span><div><strong>当前没有切换任务</strong><p class="muted">集群处于稳定状态，可以在下方选择目标模型。</p></div></div>`;
    }
    const progress = operation.progress || {percent: 100, step_index: 4, steps: [], message: "状态不可识别", tone: "critical", terminal: false};
    const percent = Math.max(0, Math.min(100, Number(progress.percent) || 0));
    const steps = (progress.steps || []).map((step, index) => {
      const status = progress.terminal || index < progress.step_index ? "complete" : index === progress.step_index ? "active" : "pending";
      return `<li class="step ${status}"><span aria-hidden="true">${status === "complete" ? "✓" : index + 1}</span><strong>${escapeHtml(step)}</strong></li>`;
    }).join("");
    const source = fmtModel(operation.source_model);
    const target = fmtModel(operation.target_model);
    const endedAt = progress.terminal ? operation.updated_at : "";
    return `<div class="operation-content tone-${escapeHtml(progress.tone)}">
      <div class="operation-heading"><div><p class="label">${progress.terminal ? "LAST OPERATION" : "OPERATION IN PROGRESS"}</p><h3>${escapeHtml(progress.message)}</h3></div><span class="operation-percent">${percent}%</span></div>
      <div class="route"><span>${source}</span><b aria-hidden="true">→</b><span>${target}${operation.target_profile ? ` / ${escapeHtml(operation.target_profile)}` : ""}</span></div>
      <div class="progress-track" role="progressbar" aria-label="模型切换进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}" aria-valuetext="${escapeHtml(progress.message)}"><span class="progress-fill progress-${percent}"></span></div>
      <ol class="stepper">${steps}</ol>
      <div class="operation-meta"><span>Operation ID <code>${escapeHtml(operation.operation_id)}</code></span><span>已用时 <strong data-elapsed="${escapeHtml(operation.created_at)}" data-ended="${escapeHtml(endedAt)}">${elapsed(operation.created_at, endedAt)}</strong></span></div>
      ${operation.error ? `<p class="operation-advice" role="alert">操作遇到异常。为了安全，新的切换已暂停；请检查恢复状态。</p>` : ""}
    </div>`;
  }

  function connectionMarkup(connection) {
    if (!connection) return `<p class="muted">当前没有运行中的本地模型，连接信息将在模型就绪后显示。</p>`;
    if (!connection.available) return `<p class="connection-error">${fmt(connection.reason || "当前连接信息不可用")}</p>`;
    const authNote = connection.api_key_required ? "Bearer 鉴权" : "无需 Bearer API Key";
    return `<p class="connection-heading"><strong>应用连接信息</strong><span>${fmt(connection.api_type)}</span></p>
      <dl class="connection-grid">
        <div><dt>Base URL</dt><dd><code data-connection="base-url">${fmt(connection.base_url)}</code></dd></div>
        <div><dt>API Key</dt><dd><code data-connection="api-key">${fmt(connection.api_key)}</code><small>${escapeHtml(authNote)} · ${fmt(connection.credential_source)}</small></dd></div>
        <div><dt>${connection.model_name === "comfyui" || connection.model_name === "minimax-music3" ? "服务名称" : "模型名称"}</dt><dd><code data-connection="model-name">${fmtModel(connection.model_name)}</code>${connection.model_name === "comfyui" || connection.model_name === "minimax-music3" ? '<small>生命周期 ID：comfyui；历史别名 minimax-music3；具体模型由工作流指定</small>' : ""}</dd></div>
        <div><dt>请求入口</dt><dd><code data-connection="request-path">${fmt(connection.request_path)}</code></dd></div>
      </dl>`;
  }

  function seriesClass(key) {
    const [role, metric] = String(key).split(".");
    return `series-machine-${role} series-metric-${metric}`;
  }

  function telemetryMarkup(model) {
    const horizontal = [0, 25, 50, 75, 100].map((percent) => {
      const y = model.plot.bottom - (percent / 100) * (model.plot.bottom - model.plot.top);
      const leftValue = Math.round((percent / 100) * model.leftMaximum);
      return `<g class="grid-line"><line x1="${model.plot.left}" y1="${y}" x2="${model.plot.right}" y2="${y}"></line><text x="${model.plot.left - 10}" y="${y + 4}" text-anchor="end">${leftValue}</text><text x="${model.plot.right + 10}" y="${y + 4}">${percent}%</text></g>`;
    }).join("");
    const series = model.series.map((item) => {
      const segments = item.segments.map((segment, segmentIndex) => {
        const points = segment.map((point) => `${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(" ");
        const dot = segment.length === 1 ? `<circle cx="${segment[0].x}" cy="${segment[0].y}" r="2" class="single-sample"></circle>` : "";
        return `<g data-segment="${segmentIndex}"><polyline points="${points}"></polyline>${dot}</g>`;
      }).join("");
      return `<g class="chart-series ${seriesClass(item.key)}" data-series="${escapeHtml(item.key)}" aria-label="${escapeHtml(item.label)}">${segments}</g>`;
    }).join("");
    const axisLabel = model.endTime - model.startTime > 86400000 ? fullTimeLabel : timeLabel;
    return `<svg class="telemetry-svg" viewBox="0 0 ${model.width} ${model.height}" role="img" aria-labelledby="telemetry-svg-title telemetry-svg-description">
      <title id="telemetry-svg-title">四机 GPU 实时遥测</title>
      <desc id="telemetry-svg-description">DGX 1 到 DGX 4 的温度、统一内存、功耗及 GPU 利用率。每台机器一种颜色；温度实线、内存虚线、功耗点线、利用率点划线。数据缺失时曲线断开。</desc>
      ${horizontal}
      <line class="axis-line" x1="${model.plot.left}" y1="${model.plot.bottom}" x2="${model.plot.right}" y2="${model.plot.bottom}"></line>
      ${series}
      ${[0, 0.25, 0.5, 0.75, 1].map((fraction) => `<text class="axis-caption" x="${model.plot.left + fraction * (model.plot.right - model.plot.left)}" y="326" text-anchor="${fraction === 0 ? "start" : fraction === 1 ? "end" : "middle"}">${axisLabel(model.startTime + fraction * (model.endTime - model.startTime))}</text>`).join("")}
      <rect class="chart-capture" x="${model.plot.left}" y="${model.plot.top}" width="${model.plot.right - model.plot.left}" height="${model.plot.bottom - model.plot.top}" tabindex="0" role="slider" aria-label="遥测曲线，左右方向键查看采样" aria-valuemin="0" aria-valuemax="${Math.max(0,state.telemetry.length-1)}" aria-valuenow="${Math.max(0,state.telemetry.length-1)}"></rect>
    </svg>`;
  }

  function timeLabel(value) {
    return new Date(value).toLocaleTimeString("zh-CN", {hour12: false, hour: "2-digit", minute: "2-digit"});
  }

  function fullTimeLabel(value) {
    return new Date(value).toLocaleString("zh-CN", {hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit"});
  }

  function telemetryRange() {
    const endMs = state.telemetryEndMs != null ? state.telemetryEndMs : Date.now();
    const startMs = state.telemetryStartMs != null ? state.telemetryStartMs : endMs - state.telemetryWindowMs;
    return {startMs, endMs, windowMs: endMs - startMs, fixed: state.telemetryEndMs != null};
  }

  function telemetryMergeOptions(range) {
    if (range.windowMs <= 3600000) {
      return {startMs: range.startMs, endMs: range.endMs, bucketMs: 5000, maxPoints: 720};
    }
    return {startMs: range.startMs, endMs: range.endMs, bucketMs: Math.max(1, Math.floor(range.windowMs / 1440)), maxPoints: 1600};
  }

  function windowLabel(windowMs) {
    return {
      3600000: "最近一小时",
      86400000: "过去 1 天",
      259200000: "过去 3 天",
      604800000: "过去 7 天",
      1209600000: "过去 14 天",
      2592000000: "过去 30 天",
    }[windowMs] || "自定义时段";
  }

  function renderTelemetry(force) {
    const range = telemetryRange();
    const signature = `${state.telemetry.length}:${state.telemetry.at(-1)?.received_at_ms}:${[...state.hiddenSeries].join(",")}:${state.historyNote}:${range.startMs}:${range.endMs}`;
    if (signature === state.chartSignature) return;
    const now = Date.now();
    const paintEveryMs = state.telemetryWindowMs > 3600000 ? 30000 : 4000;
    if (!force && now - state.lastTelemetryPaint < paintEveryMs) return;
    state.chartSignature = signature;
    state.lastTelemetryPaint = now;
    const model = ui.chartModel(state.telemetry, state.hiddenSeries, {startTime: range.startMs, endTime: range.endMs, maximumGapMs: Math.max(45000, Math.ceil(range.windowMs / 500))});
    state.chartModel = model;
    $("chart-legend").innerHTML = Object.keys(ui.NODE_IDENTITIES).map((role) => {
      const node = (state.snapshot && state.snapshot.nodes || []).find((item) => item.role === role);
      const identity = nodeIdentity(role, node && node.address);
      const metricLabels = {temperature: "温度", memory: "内存", power: "功耗", utilization: "利用率"};
      const visible = model.series.some((series) => series.role === role && !series.hidden);
      const controls = model.series.filter((series) => series.role === role).map((series) => `<button class="legend-item ${seriesClass(series.key)}" data-legend="${escapeHtml(series.key)}" aria-pressed="${series.hidden ? "false" : "true"}"><svg class="legend-swatch" viewBox="0 0 36 8" aria-hidden="true" focusable="false"><line x1="1" y1="4" x2="35" y2="4"></line></svg>${escapeHtml(metricLabels[series.metric])}</button>`).join("");
      return `<section class="legend-machine" data-legend-role="${escapeHtml(role)}" data-role-hidden="${visible ? "false" : "true"}" tabindex="0" role="group" aria-pressed="${visible ? "true" : "false"}" aria-label="${escapeHtml(identity.machine)} ${escapeHtml(identity.address)}：${visible ? "显示" : "已隐藏"}，Enter 或 Space 切换">
        <div class="legend-machine-heading series-machine-${role}">
          <div class="machine-heading-name"><span class="machine-dot" aria-hidden="true"></span><strong>${escapeHtml(identity.machine)}</strong></div>
          <span class="legend-machine-meta">${escapeHtml(identity.address)} · ${escapeHtml(identity.roleLabel)}（${escapeHtml(identity.technicalRole)}）</span>
        </div>
        <div class="legend-series">${controls}</div>
      </section>`;
    }).join("");
    $("telemetry-chart").innerHTML = state.telemetry.length ? telemetryMarkup(model) : `<div class="chart-empty">等待第一帧遥测数据…</div>`;
    const latest = state.telemetry[state.telemetry.length - 1];
    const labels = {ok: "四机遥测正常", partial: "部分遥测缺失，曲线已断开", snapshot_error: "快照获取失败，全部曲线已断开"};
    $("chart-status").textContent = latest ? labels[latest.status] : "等待遥测数据";
    const rangeLabel = range.fixed ? "自定义时段" : windowLabel(state.telemetryWindowMs);
    $("chart-time").textContent = `${rangeLabel} · ${fullTimeLabel(model.startTime)}–${fullTimeLabel(model.endTime)}（本地时间） · ${state.telemetry.length} 个采样点 · ${state.historyNote} · 无数据时留空`;

    $("chart-status").className = latest && latest.status !== "ok" ? "chart-warning" : "";
  }

  function showTelemetryPoint(index) {
    const frame = state.telemetry[index];
    if (!frame) return;
    const values = ui.TELEMETRY_SERIES.filter((s) => !state.hiddenSeries.has(s.key)).map((s) =>
      `${s.label} ${frame.values[s.key] == null ? "缺失" : frame.values[s.key].toFixed(1) + s.unit}`);
    $("chart-tooltip").textContent = `${new Date(frame.received_at_ms).toLocaleString("zh-CN", {hour12:false})} · ${values.join(" · ")}`;
    const slider = $("telemetry-chart").querySelector(".chart-capture");
    if (slider) { slider.setAttribute("aria-valuenow", index); slider.setAttribute("aria-valuetext", $("chart-tooltip").textContent); }
  }

  $("chart-legend").addEventListener("click", (event) => {
    const metricButton = event.target.closest("[data-legend]");
    if (metricButton) {
      const key = metricButton.dataset.legend;
      if (state.hiddenSeries.has(key)) state.hiddenSeries.delete(key); else state.hiddenSeries.add(key);
      renderTelemetry(true);
      return;
    }
    const machineButton = event.target.closest("[data-legend-role]");
    if (!machineButton) return;
    const role = machineButton.dataset.legendRole;
    const keys = ui.TELEMETRY_SERIES.filter((series) => series.role === role).map((series) => series.key);
    const allHidden = keys.every((key) => state.hiddenSeries.has(key));
    keys.forEach((key) => allHidden ? state.hiddenSeries.delete(key) : state.hiddenSeries.add(key));
    renderTelemetry(true);
  });
  $("chart-legend").addEventListener("keydown", (event) => {
    if (!["Enter", " "].includes(event.key)) return;
    if (event.target.closest("[data-legend]")) return;
    const machineButton = event.target.closest("[data-legend-role]");
    if (!machineButton) return;
    event.preventDefault();
    machineButton.click();
  });
  $("telemetry-chart").addEventListener("pointermove", (event) => {
    if (state.pointerFrame !== null || !state.chartModel) return;
    const clientX = event.clientX;
    state.pointerFrame = requestAnimationFrame(() => {
      state.pointerFrame = null;
      const svg = $("telemetry-chart").querySelector("svg");
      if (!svg || !state.telemetry.length) return;
      const bounds = svg.getBoundingClientRect(), model = state.chartModel;
      const x = (clientX - bounds.left) / bounds.width * model.width;
      const target = model.startTime + (x - model.plot.left) / (model.plot.right - model.plot.left) * (model.endTime - model.startTime);
      let low = 0, high = state.telemetry.length - 1;
      while (low < high) { const mid = (low + high) >> 1; if (state.telemetry[mid].received_at_ms < target) low = mid + 1; else high = mid; }
      if (low > 0 && Math.abs(state.telemetry[low-1].received_at_ms-target) < Math.abs(state.telemetry[low].received_at_ms-target)) low--;
      if (Math.abs(state.telemetry[low].received_at_ms-target) > 45000) {
        $("chart-tooltip").textContent = `${new Date(target).toLocaleString("zh-CN", {hour12:false})} · 此时刻附近无采样记录`;
      } else {
        showTelemetryPoint(low);
      }
    });
  });
  $("telemetry-chart").addEventListener("keydown", (event) => {
    if (!event.target.matches(".chart-capture")) return;
    if (!["ArrowLeft","ArrowRight","Home","End"].includes(event.key)) return;
    event.preventDefault();
    const current = Number(event.target.getAttribute("aria-valuenow"));
    const index = event.key === "Home" ? 0 : event.key === "End" ? state.telemetry.length-1 : current + (event.key === "ArrowLeft" ? -1 : 1);
    showTelemetryPoint(Math.max(0,Math.min(state.telemetry.length-1,index)));
  });

  async function restoreTelemetry() {
    if (state.historyLoading || document.hidden || Date.now() - state.historyLoadedAt < 60000 || state.telemetryStartMs != null || state.telemetryWindowMs !== 3600000) return;
    state.historyLoading = true;
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch("/api/v1/recording/recent", {cache:"no-store",signal:controller.signal});
      if (!response.ok) throw new Error("history_unavailable");
      const payload = await response.json();
      const frames = (payload.samples || []).map((sample) => ui.telemetryFrame({snapshot:sample}, Date.parse(sample.sampled_at), ++state.telemetrySequence));
      state.telemetry = ui.mergeTelemetry(frames, state.telemetry, Date.now());
      state.historyLoadedAt = Date.now();
      state.historyNote = "已合并后台历史";
    } catch (_) { state.historyNote = "历史暂不可用，继续显示实时采样"; }
    finally { clearTimeout(timer); state.historyLoading = false; renderTelemetry(true); }
  }

  async function loadTelemetryRange(startMs, endMs, note) {
    if (state.telemetryLoading) return;
    state.telemetryLoading = true;
    state.lastTelemetryPaint = 0;
    state.historyNote = note || "正在读取历史遥测…";
    renderTelemetry(true);
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(`/api/v1/recording/range?start=${Math.floor(startMs)}&end=${Math.floor(endMs)}`, {cache: "no-store", signal: controller.signal});
      if (!response.ok) throw new Error("range_unavailable");
      const payload = await response.json();
      const frames = (payload.samples || []).map((sample) => ui.telemetryFrame({snapshot: sample}, Date.parse(sample.sampled_at), ++state.telemetrySequence));
      state.telemetry = ui.mergeTelemetry(state.telemetry, frames, Date.now(), telemetryMergeOptions({startMs, endMs, windowMs: endMs - startMs}));
      const available = payload.range || {};
      state.historyNote = available.available_start == null ? "该时段暂无已保存数据" : "已载入所选时段";
      if (available.available_start != null && available.available_start > startMs) {
        state.historyNote += `（本地最早仅到 ${fullTimeLabel(available.available_start)}）`;
      }
    } catch (_) {
      state.historyNote = "历史范围暂不可用，保留现有数据";
    } finally {
      clearTimeout(timer);
      state.telemetryLoading = false;
      renderTelemetry(true);
    }
  }

  function render() {
    const snapshot = state.snapshot;
    const controls = ui.controlState(snapshot, {
      authenticated: state.authenticated,
      submitting: state.submitting,
      snapshotError: Boolean(state.snapshotError),
    });
    $("connection").textContent = state.snapshotError ? "● 状态不可用" : snapshot ? "● 实时连接" : "正在连接";
    $("connection").className = `connection ${state.snapshotError ? "bad-text" : snapshot ? "ok-text" : ""}`;
    $("control-reason").textContent = controls.reason;
    $("control-reason").className = `control-reason ${controls.disabled ? "locked" : "ready"}`;
    $("login-button").textContent = snapshot?.session?.lan_no_auth ? "局域网免登录" : state.authenticated ? "已登录" : "管理登录";
    $("login-button").disabled = Boolean(snapshot?.session?.lan_no_auth);

    if (!snapshot) {
      $("lock").textContent = "LOCK UNKNOWN";
      $("lock").className = "badge warn";
      $("active-model").textContent = state.snapshotError ? "状态不可用" : "读取中…";
      $("runtime-detail").textContent = "";
      $("residency-panel").hidden = true;
      $("active-connection").innerHTML = `<p class="muted">连接信息暂不可用。</p>`;
      $("models").innerHTML = "";
      $("nodes").innerHTML = "";
      $("providers").innerHTML = "";
      $("operation").innerHTML = operationMarkup(null);
      renderTelemetry();
      return;
    }

    const lockState = snapshot.lock && snapshot.lock.state || "UNKNOWN";
    $("lock").textContent = `LOCK ${lockState}`;
    $("lock").className = `badge ${lockState === "UNLOCKED" ? "ok" : "warn"}`;
    const summary = ui.runtimeSummary(snapshot);
    $("active-model").textContent = summary.title;
    $("runtime-detail").textContent = state.snapshotError ? "状态读取失败，上次任务信息仅供参考。" : summary.detail;
    const release = ui.residencyAction(snapshot, state);
    const resident = snapshot.residency || {};
    $("residency-panel").hidden = !release.visible;
    $("release-residency").disabled = release.disabled;
    $("release-residency").textContent = release.label;
    const residentLabel = {IDLE: "空闲待复用，稍后自动释放", RELEASING: "正在安全释放模型内存", RECOVERY_REQUIRED: "需要恢复确认，请核查原任务后尝试释放"}[resident.state] || "资源状态待确认";
    const expires = resident.expires_at ? new Date(typeof resident.expires_at === "number" ? resident.expires_at * 1000 : resident.expires_at).toLocaleTimeString() : "";
    $("residency-detail").textContent = `${residentLabel}${resident.model_id ? ` · ${ui.serviceLabel(resident.model_id)}` : ""}${expires && resident.state === "IDLE" ? ` · 预计释放时间 ${expires}` : ""}${!state.authenticated ? " · 管理登录后可手动释放" : ""}`;
    $("active-connection").innerHTML = state.snapshotError
      ? `<p class="connection-error">状态读取失败，已停止发布旧连接参数。</p>`
      : connectionMarkup(snapshot.active_connection);
    $("sample-time").textContent = `最近采样：${snapshot.sampled_at || "—"}`;
    $("nodes").innerHTML = (snapshot.nodes || []).map(nodeCard).join("");
    $("models").innerHTML = (snapshot.models || []).map((model) => modelCard(model, controls, snapshot)).join("");
    $("providers").innerHTML = (snapshot.providers || []).map(providerCard).join("");
    $("operation").innerHTML = operationMarkup(ui.selectedOperation(snapshot));
    renderTelemetry();
    document.querySelectorAll("[data-model]").forEach((button) => {
      button.addEventListener("click", () => switchModel(button.dataset.model));
    });
    document.querySelectorAll("[data-profile-model]").forEach((profileNode) => profileNode.addEventListener("change", () => {
      const modelId = profileNode.dataset.profileModel;
      state.profileDrafts[modelId] = profileNode.value;
      const model = (state.snapshot.models || []).find((item) => item.model_id === modelId);
      const button = document.querySelector(`[data-model="${modelId}"]`);
      if (!model || !button) return;
      const action = ui.modelActionState(state.snapshot, model, controls, profileNode.value);
      button.disabled = action.disabled;
      button.textContent = action.label;
      if (action.current) button.setAttribute("aria-current", "true"); else button.removeAttribute("aria-current");
      const card = button.closest("[data-model-card]");
      if (card) {
        if (action.current) card.setAttribute("data-current", "true"); else card.removeAttribute("data-current");
      }
    }));
  }

  function showToast(message, tone) {
    const toast = $("toast");
    toast.textContent = message;
    toast.className = `toast ${tone || ""}`;
    toast.hidden = false;
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => { toast.hidden = true; }, 6000);
  }

  async function readJson(response) {
    try { return await response.json(); } catch (_) { return {}; }
  }

  async function refresh() {
    clearTimeout(state.pollTimer);
    if (document.hidden) return;
    if (state.pollInFlight) {
      state.pollQueued = true;
      return;
    }
    state.pollInFlight = true;
    const requestGeneration = state.snapshotGeneration;
    let attempt = {error: "snapshot_not_attempted"};
    try {
      const response = await fetch("/api/v1/snapshot", {cache: "no-store"});
      if (!response.ok) throw new Error(`snapshot_${response.status}`);
      const snapshot = await response.json();
      if (!snapshot || !snapshot.lock || !Array.isArray(snapshot.nodes) || !Array.isArray(snapshot.models)) throw new Error("snapshot_schema");
      attempt = {snapshot};
      if (requestGeneration === state.snapshotGeneration) {
        state.snapshot = snapshot;
        state.snapshotError = null;
        state.authenticated = Boolean(snapshot.session && snapshot.session.authenticated);
      }
    } catch (error) {
      attempt = {error: error.message || "snapshot_error"};
      if (requestGeneration === state.snapshotGeneration) state.snapshotError = error;
    } finally {
      state.telemetrySequence += 1;
      const now = Date.now();
      const range = telemetryRange();
      state.telemetry = ui.mergeTelemetry(state.telemetry,
        [ui.liveTelemetryFrame(attempt, now, state.telemetrySequence)], now, telemetryMergeOptions(range));
      state.pollInFlight = false;
      render();
      const delay = state.pollQueued ? 0 : 5000;
      state.pollQueued = false;
      state.pollTimer = setTimeout(refresh, delay);
    }
  }

  async function switchModel(model) {
    const controls = ui.controlState(state.snapshot, {
      authenticated: state.authenticated,
      submitting: state.submitting,
      snapshotError: Boolean(state.snapshotError),
    });
    if (controls.disabled) return;
    const profileNode = document.querySelector(`[data-profile-model="${model}"]`);
    const profile = profileNode ? profileNode.value : null;
    const target = (state.snapshot.models || []).find((item) => item.model_id === model);
    const action = ui.modelActionState(state.snapshot, target, controls, profile);
    if (action.disabled) {
      if (action.current) showToast(model === "minimax-h3" ? "当前配置已在运行" : "当前模型已在运行", "working");
      return;
    }
    const detail = `${ui.serviceLabel(state.snapshot.active_local_model && state.snapshot.active_local_model.model_id)} → ${ui.serviceLabel(model)}${profile ? ` / ${profile}` : ""}`;
    if (!window.confirm(`确认独占切换？\n\n${detail}\n\n源模型将先完全停止，目标模型健康验证通过后才会投入使用。`)) {
      return;
    }
    state.submitting = true;
    state.snapshotGeneration += 1;
    render();
    showToast("切换请求正在提交…", "working");
    try {
      const response = await fetch("/api/v1/switches", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({target_model: model, profile, idempotency_key: requestId()}),
      });
      const payload = await readJson(response);
      if (!response.ok) {
        if (response.status === 401 || response.status === 403) {
          state.authenticated = false;
          throw new Error("登录已失效，请重新登录");
        }
        if (response.status === 409 && payload.error === "busy") throw new Error("已有切换正在进行，请等待当前任务完成");
        throw new Error("当前集群状态不允许切换，请刷新后检查状态");
      }
      if (payload.operation && state.snapshot) state.snapshot = {...state.snapshot, current_operation: payload.operation};
      showToast(`请求已受理 · ${payload.operation ? payload.operation.operation_id : "正在同步状态"}`, "success");
    } catch (error) {
      showToast(error.message || "请求失败，请稍后重试", "danger");
    } finally {
      state.submitting = false;
      render();
      refresh();
    }
  }

  $("release-residency").addEventListener("click", async () => {
    const action = ui.residencyAction(state.snapshot, state);
    if (action.disabled) return;
    state.submitting = true;
    state.snapshotGeneration += 1;
    render();
    showToast("正在申请安全释放模型内存…", "working");
    try {
      const response = await fetch("/api/v1/residency/release", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({owner_job_id: action.ownerJobId}),
      });
      await readJson(response);
      if (!response.ok) {
        if ([401, 403].includes(response.status)) { state.authenticated = false; throw new Error("登录已失效，请重新登录"); }
        throw new Error(response.status === 409 ? "资源归属已变化，请刷新后查看当前任务" : "释放请求失败，请检查资源状态后重试");
      }
      showToast("释放请求已受理，正在同步实际资源状态", "success");
    } catch (error) {
      showToast(error.message || "释放请求失败", "danger");
    } finally {
      state.submitting = false;
      render();
      refresh();
    }
  });

  const dialog = $("login-dialog");
  $("login-button").addEventListener("click", () => {
    if (!state.authenticated) dialog.showModal();
  });
  $("login-submit").addEventListener("click", async (event) => {
    event.preventDefault();
    const response = await fetch("/api/v1/session", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({token: $("admin-token").value}),
    });
    if (response.ok) {
      dialog.close();
      $("admin-token").value = "";
      $("login-error").textContent = "";
      showToast("管理登录成功", "success");
      refresh();
    } else {
      $("login-error").textContent = "登录失败，请检查令牌";
    }
  });

  $("telemetry-range").addEventListener("change", (event) => {
    const durations = { "1h": 3600000, "1d": 86400000, "3d": 259200000, "7d": 604800000, "14d": 1209600000, "30d": 2592000000 };
    const windowMs = durations[event.target.value];
    if (!windowMs) return;
    state.telemetryWindowMs = windowMs;
    state.telemetryStartMs = null;
    state.telemetryEndMs = null;
    $("telemetry-start").value = "";
    $("telemetry-end").value = "";
    const endMs = Date.now();
    loadTelemetryRange(endMs - windowMs, endMs, "正在读取所选时间范围…");
  });

  $("telemetry-apply").addEventListener("click", () => {
    const startValue = $("telemetry-start").value;
    const endValue = $("telemetry-end").value;
    if (!startValue || !endValue) {
      showToast("请先选择开始和结束时间", "danger");
      return;
    }
    const startMs = new Date(startValue).getTime();
    const endMs = new Date(endValue).getTime();
    if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs <= startMs) {
      showToast("结束时间必须晚于开始时间", "danger");
      return;
    }
    state.telemetryStartMs = startMs;
    state.telemetryEndMs = endMs;
    state.telemetryWindowMs = endMs - startMs;
    $("telemetry-range").value = "";
    loadTelemetryRange(startMs, endMs, "正在读取自定义时段…");
  });

  setInterval(() => {
    document.querySelectorAll("[data-elapsed]").forEach((element) => {
      element.textContent = elapsed(element.dataset.elapsed, element.dataset.ended);
    });
  }, 1000);
  document.addEventListener("visibilitychange", () => {
    clearTimeout(state.pollTimer);
    if (!document.hidden) { restoreTelemetry(); refresh(); }
  });
  render();
  restoreTelemetry();
  refresh();
})();
