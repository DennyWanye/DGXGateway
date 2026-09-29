import { formatTaskTime } from "./workflow.js";
import { get, post, segment, mediaUrl, uploadReferenceAudio, requestKey } from "./api.js";

const TERMINAL = new Set(["SUCCEEDED", "FAILED", "CANCELED", "INTERRUPTED"]);
const STATES = { QUEUED: "等待派发", BLOCKED: "等待资源或依赖", DISPATCHING: "正在派发", SUBMISSION_UNKNOWN: "提交结果不明",
  SUBMITTED: "已提交原任务", RUNNING: "正在生成", COLLECTING: "归档与媒体检查", SUCCEEDED: "已完成",
  FAILED: "失败", CANCEL_REQUESTED: "已请求取消", CANCELED: "已取消", INTERRUPTED: "已中断",
  NOT_SUBMITTED: "确定未发出", REQUEST_STARTED: "请求已开始", RECEIVED: "已收到结果", UNKNOWN: "结果未知", OUTPUT_UNAVAILABLE: "原片暂不可用", INVALID: "媒体检查未通过" };
const REVIEW = { pending: "待人工审片", approved: "人工已批准", rejected: "人工已拒绝" };
const node = (tag, text, parent, className) => {
  const el = document.createElement(tag); if (text != null) el.textContent = text;
  if (className) el.className = className; parent?.append(el); return el;
};
const nameOf = asset => asset?.filename || asset?.title || asset?.name || asset?.id || "未命名";
const errorText = error => {
  const text = typeof error === "string" ? error : error?.message || "";
  if (text === "DGX busy; existing resources were not preempted") return "其他模型正在使用计算资源。本次没有抢占，请待资源可用后再处理。";
  if (text === "Gateway rejected video arguments") return "当时的服务未接受这次提交，任务记录已保留。";
  return text;
};
const audioAsset = asset => asset.role === "h3_reference" && (asset.kind === "audio" || String(asset.contentType || "").startsWith("audio/"));
const receiptKey = (pid, eid) => `dgx.drama.video.pending.v1:${pid}:${eid}`;
function savePending(pid, eid, pending) {
  try {
    sessionStorage.setItem(receiptKey(pid, eid), JSON.stringify({ version: 1, projectId: pid, episodeId: eid,
      key: pending.key, payload: pending.payload }));
    return true;
  } catch { return false; }
}
function loadPending(pid, eid) {
  try {
    const saved = JSON.parse(sessionStorage.getItem(receiptKey(pid, eid)));
    if (saved?.version !== 1 || saved.projectId !== pid || saved.episodeId !== eid || typeof saved.key !== "string"
      || !saved.key.startsWith("drama-ui-") || !Array.isArray(saved.payload?.shots)
      || !saved.payload.shots.length || saved.payload.shots.length > 32
      || saved.payload.shots.some(shot => !shot || typeof shot.shotId !== "string" || typeof shot.shotRevisionId !== "string"
        || typeof shot.audioAssetId !== "string" || typeof shot.prompt !== "string" || !Number.isSafeInteger(shot.seed))) return null;
    return { key: saved.key, payload: saved.payload, persisted: true };
  } catch { return null; }
}
function clearPending(pid, eid) {
  try { sessionStorage.removeItem(receiptKey(pid, eid)); } catch { /* Optional browser storage. */ }
}

export function buildVideoBatch(drafts) {
  if (!Array.isArray(drafts) || !drafts.length || drafts.length > 32) throw new Error("每批次请选择 1–32 个镜头。");
  const seen = new Set();
  return { shots: drafts.map((draft, index) => {
    if (!draft.shotId || !draft.shotRevisionId || !draft.imageAssetId || !draft.approved) throw new Error(`镜头 ${index + 1} 缺少已批准的参考画面，请回制作面板完成批准。`);
    if (draft.needsReview) throw new Error(`镜头 ${index + 1} 的固定实体依赖尚需复核，请先在制作面板完成复核并重新读取镜头版本。`);
    if (seen.has(draft.shotId)) throw new Error("同一批次不能重复加入同一个镜头。");
    seen.add(draft.shotId);
    if (!draft.audioAssetId) throw new Error(`镜头 ${index + 1} 尚未选择严格格式的参考音频。`);
    if (typeof draft.prompt !== "string" || !draft.prompt.trim()) throw new Error(`镜头 ${index + 1} 的生成提示词不能为空。`);
    if (new TextEncoder().encode(draft.prompt).length > 2048) throw new Error(`镜头 ${index + 1} 提示词不得超过 2048 UTF-8 字节，请精简后提交。`);
    if (draft.imageExtension && !["png", "jpg"].includes(draft.imageExtension)) throw new Error(`镜头 ${index + 1} 的 H3 参考图片须为 PNG 或 JPEG，请在制作面板重新选定并批准。`);
    const seed = Number(draft.seed);
    if (String(draft.seed).trim() === "" || !Number.isSafeInteger(seed) || seed < 0 || seed > 2147483647) throw new Error(`镜头 ${index + 1} 种子须为 0–2147483647 的整数。`);
    // The server derives image/profile/duration from the frozen production revision.
    return { shotId: draft.shotId, shotRevisionId: draft.shotRevisionId,
      audioAssetId: draft.audioAssetId, prompt: draft.prompt, seed };
  }) };
}

export function createVideoWorkbench(context) {
  const root = node("section", null, null, "block drama-video");
  const state = { key: null, projectId: null, episodeId: null, epoch: 0, request: 0, assets: [], shots: [], jobs: [],
    production: null, drafts: [], dirty: false, defaultAudioId: "", takes: new Map(), reviewDrafts: new Map(), pendingSubmission: null };
  const jobViews = new Map();
  const episodeReads = new Map();
  let draftHost, jobsHost, productionStatus, pauseButton, submitButton, summary;
  let shotPicker, addShotButton, clearButton, audioUploadButton, audioFileInput, pendingStatus;
  let timer = null, polling = false;
  let generationPane, reviewPane, videoTabs = {}, selectedView = "generate", initialViewChosen = false;
  function showView(view) {
    if (!["generate", "review"].includes(view)) return;
    selectedView = view;
    if (generationPane) generationPane.hidden = view !== "generate";
    if (reviewPane) reviewPane.hidden = view !== "review";
    for (const [id, control] of Object.entries(videoTabs)) control.setAttribute("aria-pressed", String(view === id));
    if (view !== "review") for (const media of reviewPane?.querySelectorAll("video,audio") || []) media.pause();
    if (view !== "generate") for (const media of generationPane?.querySelectorAll("video,audio") || []) media.pause();
  }
  function details(parent, title) {
    const box = node("details", null, parent, "technical-details"); node("summary", title, box); return box;
  }
  const path = (kind, id) => `/${kind}/${segment(id)}`;
  const current = (epoch, pid = state.projectId, eid = state.episodeId) => epoch === state.epoch && pid === state.projectId && eid === state.episodeId;
  const isVisible = () => context.isVisible() && root.isConnected && Boolean(state.projectId);
  function button(parent, text, action, disabled = false, primary = false) {
    const el = node("button", text, parent, primary ? "primary" : ""); el.type = "button";
    el.dataset.unavailable = String(disabled); el.disabled = disabled || context.isBusy();
    el.onclick = () => context.run(action); return el;
  }
  function field(parent, label, value = "", type = "text") {
    const wrap = node("label", label, parent);
    const el = node(type === "textarea" ? "textarea" : "input", null, wrap);
    if (type !== "textarea") el.type = type;
    el.value = value; return el;
  }
  function select(parent, label, choices, value = "") {
    const wrap = node("label", label, parent); const el = node("select", null, wrap);
    for (const [id, name] of choices) { const option = node("option", name, el); option.value = id; }
    el.value = value; return el;
  }
  function report(error) { context.report(error.message || String(error), true); }
  function storeTakes(episode) {
    for (const shot of episode.shots || []) {
      for (const take of shot.takes || []) state.takes.set(take.id, { ...take, shotId: shot.id, shotTitle: shot.title,
        shotRevision: shot.revision, currentRevisionId: shot.currentRevisionId, selectedTakeId: shot.selectedTakeId });
    }
  }
  async function readEpisode(eid, force = false) {
    if (!eid) return;
    const epoch = state.epoch;
    if (!force && episodeReads.has(eid)) return episodeReads.get(eid);
    const promise = get(path("episodes", eid)).then(result => {
      if (!current(epoch) || episodeReads.get(eid) !== promise) return null;
      storeTakes(result);
      if (eid === state.episodeId) state.shots = result.shots || [];
      return result;
    });
    episodeReads.set(eid, promise);
    try { return await promise; }
    finally { if (episodeReads.get(eid) === promise) episodeReads.delete(eid); }
  }
  async function refreshRead({ updateAssets = true } = {}) {
    const epoch = state.epoch, request = ++state.request, pid = state.projectId, eid = state.episodeId;
    if (!pid) return;
    const reads = [get(`${path("projects", pid)}/production-state`), get(`${path("projects", pid)}/video-jobs`)];
    if (updateAssets) reads.push(get(path("projects", pid)));
    const results = await Promise.all(reads);
    if (!current(epoch, pid, eid) || request !== state.request) return;
    state.production = results[0]; state.jobs = results[1].jobs || [];
    if (updateAssets) state.assets = results[2].assets || [];
    if (!initialViewChosen) {
      initialViewChosen = true;
      showView(state.drafts.length || state.pendingSubmission || !state.jobs.length ? "generate" : "review");
    }
    updateSourceChoices();
    updateProduction();
    for (const job of state.jobs) {
      upsertJob(job);
      if ((job.items || []).some(item => item.takeId && !state.takes.has(item.takeId))) {
        try { await readEpisode(job.episodeId); }
        catch (error) { if (current(epoch)) context.report(`候选关联查询失败：${error.message}`, true); }
        if (!current(epoch, pid, eid) || request !== state.request) return;
        upsertJob(job);
      }
    }
    if (!state.jobs.length && jobsHost && !jobsHost.querySelector(".empty")) node("p", "项目还没有视频批次。", jobsHost, "empty");
  }
  function updateProduction() {
    if (!productionStatus) return;
    if (!state.production) productionStatus.textContent = "正在读取项目派发状态…";
    else productionStatus.textContent = `${state.production.paused ? "项目已暂停新派发" : "项目允许派发"} · 已预留 ${state.production.reservedShots || 0}/${state.production.maxReservedShots || 128} 镜头 · ${state.production.reservedSeconds || 0} 秒`;
    if (pauseButton) {
      pauseButton.textContent = state.production?.paused ? "恢复项目派发" : "暂停项目新派发";
      pauseButton.dataset.unavailable = String(!state.production);
      pauseButton.disabled = context.isBusy() || !state.production;
    }
    if (submitButton) {
      const disabled = !state.pendingSubmission && (!state.drafts.length || !state.production || state.production.paused || context.capabilities.videoBatchEnabled === false);
      submitButton.textContent = state.pendingSubmission ? "按原请求键确认提交结果" : "提交当前批次";
      submitButton.dataset.unavailable = String(disabled); submitButton.disabled = disabled || context.isBusy();
    }
    if (summary) summary.textContent = `${state.drafts.length} 个镜头 · 每镜请求 4 秒 · 预留 ${state.drafts.length * 4} 请求秒${state.dirty ? " · 尚未确认提交的草稿" : ""}`;
    if (pendingStatus) pendingStatus.textContent = state.pendingSubmission ? `提交身份与输入已锁定：${state.pendingSubmission.key}。回执不明时只能用原键确认同一业务任务，不能修改后创建第二次生成。${state.pendingSubmission.persisted ? "本标签会话已保留核查身份；页面重载后只恢复原键，不会自动重提。" : "浏览器会话暂存不可用；关闭页面会丢失本地核查身份，请先核查项目任务。"}` : "";
    for (const control of [clearButton, audioUploadButton, audioFileInput]) {
      if (control) { control.dataset.unavailable = String(Boolean(state.pendingSubmission)); control.disabled = Boolean(state.pendingSubmission) || context.isBusy(); }
    }
    updateSourceChoices();
  }
  function updateSourceChoices() {
    if (!shotPicker || !addShotButton) return;
    const selected = shotPicker.value;
    shotPicker.replaceChildren();
    for (const [id, title] of [["", "选择镜头"], ...state.shots.map(shot => [shot.id, `${shot.title}${shot.needsReview ? " · 需复核" : ""}`])]) {
      const option = node("option", title, shotPicker); option.value = id;
    }
    shotPicker.value = state.shots.some(shot => shot.id === selected) ? selected : "";
    const disabled = !state.shots.length || Boolean(state.pendingSubmission);
    addShotButton.dataset.unavailable = String(disabled); addShotButton.disabled = disabled || context.isBusy();
    shotPicker.dataset.unavailable = String(Boolean(state.pendingSubmission)); shotPicker.disabled = Boolean(state.pendingSubmission) || context.isBusy();
  }
  async function addShot(sid) {
    if (state.pendingSubmission) throw new Error("原提交结果尚未确认，不能修改已冻结的批次。");
    if (!sid) throw new Error("请选择当前分集的镜头。");
    if (state.drafts.length >= 32) throw new Error("每批次最多 32 个镜头。");
    if (state.drafts.some(draft => draft.shotId === sid)) throw new Error("该镜头已经在批次草稿中。");
    const epoch = state.epoch;
    const production = await get(`${path("shots", sid)}/production`);
    if (!current(epoch)) return;
    const snapshot = production.current?.snapshot;
    if (!snapshot || !production.currentRevisionId) throw new Error("镜头缺少内容版本，请先在镜头制作面板保存。");
    state.drafts.push({ shotId: sid, shotRevisionId: production.currentRevisionId,
      title: snapshot.title, imageAssetId: snapshot.approvedReferenceAssetId,
      imageExtension: state.assets.find(asset => asset.id === snapshot.approvedReferenceAssetId)?.extension,
      approved: Boolean(production.current.approved && snapshot.approvedReferenceAssetId),
      needsReview: (production.invalidations || []).some(item => !item.resolved),
      prompt: snapshot.prompt || "", seed: 42, audioAssetId: state.defaultAudioId });
    state.dirty = true; renderDrafts(); updateProduction();
  }
  function render() {
    root.replaceChildren(); jobViews.clear();
    const header = node("div", null, root, "row");
    node("h3", "视频制作", header);
    button(header, "只读刷新任务", async () => { await refreshRead(); context.report("视频任务与资源状态已更新；批次草稿保持不变。"); });
    const tabs = node("div", null, root, "row drama-tabs");
    videoTabs = {};
    for (const [id, title] of [["generate", "准备生成"], ["review", "任务与审片"]]) {
      videoTabs[id] = button(tabs, title, () => showView(id));
    }
    generationPane = node("div", null, root, "video-generation-pane");
    reviewPane = node("div", null, root, "video-review-pane");
    const contract = details(generationPane, "生成规格与参考音频要求");
    node("p", "稳定 H3 · 每镜请求 4 秒 · 1344 × 768 · 24fps。原片为 107 帧（约 4.458 秒），剪辑可取 96 帧；完整原片保留。参考图片须为 PNG / JPEG，模型为 heretic-ref2va-v1。多个镜头顺序生成，关闭工作台不会取消任务。", contract, "muted");
    const dispatch = node("div", null, generationPane, "dispatch-bar");
    productionStatus = node("p", null, dispatch, "timeline-info");
    pauseButton = button(dispatch, "读取项目状态…", async () => {
      if (!state.production) throw new Error("请先读取项目派发状态。");
      const epoch = state.epoch, paused = !state.production.paused;
      ++state.request;
      await post(`${path("projects", state.projectId)}/production-state`, { paused, expectedRevision: state.production.revision });
      if (!current(epoch)) return;
      await refreshRead({ updateAssets: false });
      context.report(paused ? "项目已暂停新派发；已提交批次仍继续归档。" : "项目恢复派发，已准备的任务可继续执行。");
    }, !state.production);
    node("p", "暂停后，已提交的批次仍继续处理。需要取消某个任务时，到“任务与审片”打开它的恢复工具。", generationPane, "muted");
    const upload = details(generationPane, "上传参考音频 · 已有音频可在镜头中直接选择");
    upload.open = !state.assets.some(audioAsset);
    node("p", "严格 4 秒、24000 Hz、单声道、PCM16 WAV，最大 256 KiB。这是 H3 生成参考音频，不是后期成片配音；上传不会自动裁切或转码。", upload, "muted");
    audioFileInput = field(upload, "选择参考 WAV", "", "file"); audioFileInput.accept = ".wav,audio/wav,audio/x-wav";
    audioUploadButton = button(upload, "验证并上传参考音频", async () => {
      const epoch = state.epoch, pid = state.projectId;
      const result = await uploadReferenceAudio(pid, audioFileInput.files?.[0]);
      if (!current(epoch, pid)) return;
      if (!result.asset?.id) throw new Error("上传回执缺少素材编号，请刷新项目素材核查。");
      state.defaultAudioId = result.asset.id;
      if (!state.assets.some(asset => asset.id === result.asset.id)) state.assets.push(result.asset);
      for (const draft of state.drafts) if (!draft.audioAssetId) { draft.audioAssetId = result.asset.id; state.dirty = true; }
      renderDrafts(); await context.syncAssets(); context.report("参考音频已验证并归档；尚未选择音频的草稿镜头已使用本次上传。");
    });
    if (state.episodeId) {
      const controls = node("div", null, generationPane, "row");
      shotPicker = select(controls, "加入当前分集镜头", [["", "选择镜头"]]);
      addShotButton = button(controls, "加入批次", () => addShot(shotPicker.value), !state.shots.length);
      clearButton = button(controls, "清空批次草稿", async () => {
        if (state.drafts.length && !window.confirm("清空未提交的批次草稿？已提交任务不受影响。")) return;
        state.drafts = []; state.dirty = false; renderDrafts(); updateProduction();
      });
      draftHost = node("div", null, generationPane, "drama-video-drafts"); renderDrafts();
      summary = node("p", null, generationPane, "timeline-info");
      pendingStatus = node("p", null, generationPane, "drama-task-error");
      submitButton = button(generationPane, "提交当前批次", async () => {
        if (!state.pendingSubmission) {
          state.pendingSubmission = { key: requestKey(), payload: buildVideoBatch(state.drafts) };
          state.pendingSubmission.persisted = savePending(state.projectId, state.episodeId, state.pendingSubmission);
        }
        const frozen = state.pendingSubmission;
        const epoch = state.epoch, eid = state.episodeId;
        ++state.request;
        renderDrafts(); updateProduction();
        let result;
        try { result = await post(`${path("episodes", eid)}/video-batches`, frozen.payload, frozen.key); }
        catch (error) {
          // Explicit client validation rejections did not create a job. Unknown,
          // timeout, overload, and server errors retain the exact submission.
          if (current(epoch) && error.status >= 400 && error.status < 500 && ![408, 429].includes(error.status)) {
            clearPending(state.projectId, state.episodeId);
            state.pendingSubmission = null; renderDrafts(); updateProduction();
          }
          throw error;
        }
        if (!current(epoch, state.projectId, eid)) return;
        if (!result.job?.id) throw new Error("提交回执缺少任务编号，请先只读刷新核查原任务，勿直接重提。");
        state.jobs.unshift(result.job); upsertJob(result.job);
        clearPending(state.projectId, state.episodeId);
        state.pendingSubmission = null; state.drafts = []; state.dirty = false; renderDrafts(); updateProduction();
        context.report(`视频批次已持久保存：${result.job.id}。未提交草稿已清空；任务状态以下方服务端记录为准。`);
        showView("review"); schedule();
      }, true, true);
    } else {
      draftHost = null; summary = null; submitButton = null; shotPicker = null; addShotButton = null; clearButton = null; pendingStatus = null;
      node("p", "选择分集后可建立视频批次。", generationPane, "empty");
    }
    if (context.capabilities.videoBatchEnabled === false) node("p", "视频批次适配器尚未启用；可读取已有任务和审片，提交按钮暂不可用。", generationPane, "muted");
    node("p", "打开一个批次，逐条看片并记录意见。先批准，再采用；失败或结果不明时请核查原任务。", reviewPane, "section-help");
    jobsHost = node("div", null, reviewPane, "drama-video-jobs");
    for (const job of state.jobs) upsertJob(job);
    if (!state.jobs.length) node("p", "项目还没有视频批次。", jobsHost, "empty");
    updateProduction(); showView(selectedView);
  }
  function renderDrafts() {
    if (!draftHost) return;
    draftHost.replaceChildren();
    if (!state.drafts.length) { node("p", "加入一个镜头进行单镜头生成，或按顺序加入多个镜头形成批次。", draftHost, "empty"); return; }
    state.drafts.forEach((draft, index) => {
      const card = node("div", null, draftHost, "card");
      node("strong", `${index + 1} · ${draft.title}`, card);
      node("p", draft.pendingRecovered ? "等待核查原提交，输入已锁定" : draft.approved ? "参考画面已批准" : "请先在制作镜头中批准参考画面", card, draft.approved ? "badge" : "drama-task-error");
      node("p", `冻结镜头版本 ${draft.shotRevisionId}`, details(card, "镜头版本记录"), "muted");
      if (draft.needsReview) node("p", "固定实体依赖发生变化：请先在镜头制作中完成复核，再重新读取当前版本。", card, "drama-task-error");
      const grid = node("div", null, card, "drama-form-grid");
      if (draft.imageAssetId) {
        const image = node("img", null, grid, "drama-reference-image"); image.loading = "lazy";
        image.src = mediaUrl(state.projectId, draft.imageAssetId); image.alt = `${draft.title} 已选参考画面`;
      }
      const fields = node("div", null, grid);
      const prompt = field(fields, "此镜头视频提示词", draft.prompt, "textarea");
      prompt.oninput = () => { draft.prompt = prompt.value; state.dirty = true; updateProduction(); };
      const seed = field(details(fields, "随机种子（高级）"), "种子（0–2147483647）", draft.seed, "number"); seed.min = "0"; seed.max = "2147483647"; seed.step = "1";
      seed.oninput = () => { draft.seed = seed.value; state.dirty = true; updateProduction(); };
      const audio = select(fields, "4 秒参考音频", [["", "选择已验证的参考音频"], ...state.assets.filter(audioAsset).map(asset => [asset.id, nameOf(asset)])], draft.audioAssetId);
      const player = node("audio", null, fields); player.controls = true; player.preload = "none";
      const updateAudio = () => { if (draft.audioAssetId) { player.src = mediaUrl(state.projectId, draft.audioAssetId); player.hidden = false; } else { player.removeAttribute("src"); player.hidden = true; } };
      updateAudio();
      audio.onchange = () => { draft.audioAssetId = audio.value; state.dirty = true; updateAudio(); updateProduction(); };
      const controls = node("div", null, card, "row");
      const move = delta => { [state.drafts[index], state.drafts[index + delta]] = [state.drafts[index + delta], state.drafts[index]]; state.dirty = true; renderDrafts(); updateProduction(); };
      button(controls, "上移镜头", () => move(-1), index === 0);
      button(controls, "下移镜头", () => move(1), index === state.drafts.length - 1);
      button(controls, "移出草稿", () => { state.drafts.splice(index, 1); state.dirty = state.drafts.length > 0; renderDrafts(); updateProduction(); });
      button(controls, "重新读取当前镜头版本", async () => {
        if (!window.confirm("重新读取此镜头当前版本将替换本行提示词和参考画面，音频与种子保留。是否继续？")) return;
        const epoch = state.epoch;
        const production = await get(`${path("shots", draft.shotId)}/production`);
        if (!current(epoch)) return;
        draft.shotRevisionId = production.currentRevisionId; draft.imageAssetId = production.current.snapshot.approvedReferenceAssetId;
        draft.imageExtension = state.assets.find(asset => asset.id === draft.imageAssetId)?.extension;
        draft.approved = Boolean(production.current.approved && draft.imageAssetId);
        draft.needsReview = (production.invalidations || []).some(item => !item.resolved);
        draft.title = production.current.snapshot.title; draft.prompt = production.current.snapshot.prompt || "";
        state.dirty = true; renderDrafts(); updateProduction();
      });
    });
    if (state.pendingSubmission) for (const control of draftHost.querySelectorAll("input,textarea,select,button")) { control.dataset.unavailable = "true"; control.disabled = true; }
  }
  function replaceJob(job) {
    const index = state.jobs.findIndex(item => item.id === job.id);
    if (index < 0) state.jobs.unshift(job); else state.jobs[index] = job;
    upsertJob(job);
  }
  async function jobAction(view, action) {
    const epoch = state.epoch, job = view.job;
    ++state.request;
    if (action === "continue" && (!job.canContinue || !(job.items || []).some(item => item.submissionPhase === "NOT_SUBMITTED"))) throw new Error("服务端未确认可续拍项。请先核查原任务，未知结果不能重拍。");
    const result = await post(`${path("video-jobs", job.id)}/${action}`);
    if (!current(epoch)) return;
    if (!result.job?.id) throw new Error("操作回执缺少任务编号，请只读刷新核查，勿重复提交。");
    replaceJob(result.job);
    const messages = { refresh: "已核查原任务和原派发键，未重新提交生成。",
      cancel: "取消意图已保存；请以实际镜头边界和内存释放状态为准。",
      release: "已显式请求释放属于本任务的资源，请核查内存释放结果。",
      continue: "仅确定未发出的镜头已创建新的续拍任务；结果未知的镜头保留在原任务中。" };
    context.report(messages[action]);
    await refreshRead({ updateAssets: false }); schedule();
  }
  function upsertJob(job) {
    if (!jobsHost) return;
    jobsHost.querySelector(".empty")?.remove();
    let view = jobViews.get(job.id);
    if (!view) {
      const card = node("details", null, jobsHost, "card video-batch");
      card.open = false;
      const heading = node("summary", null, card);
      node("strong", `${job.items?.length || 0} 个镜头 · ${formatTaskTime(job.createdAt)}`, heading);
      const status = node("span", null, heading, "badge");
      const resources = node("p", null, card);
      const record = details(card, "任务记录与恢复工具");
      node("p", `批次 ${job.id}`, record, "muted");
      const detail = node("p", null, record, "muted");
      const error = node("p", null, card, "drama-task-error");
      const actions = node("div", null, record, "row");
      const itemsHost = node("div", null, card, "drama-video-items");
      card.append(record);
      view = { card, status, resources, detail, error, actions, itemsHost, items: new Map(), job };
      jobViews.set(job.id, view);
      button(actions, "核查原任务", () => jobAction(view, "refresh"));
      view.cancel = button(actions, "取消原批次", () => jobAction(view, "cancel"));
      view.release = button(actions, "显式释放本任务资源", () => jobAction(view, "release"));
      view.continue = button(actions, "只续拍确定未发出的镜头", () => jobAction(view, "continue"));
    }
    view.job = job;
    view.status.textContent = `${STATES[job.state] || job.state}${job.cleanupRequired ? " · 待释放资源" : ""}${(job.items || []).some(item => item.submissionPhase === "UNKNOWN") ? " · 有结果待核查" : ""} · ${job.episodeId === state.episodeId ? "当前分集" : "其他分集"}`;
    view.status.dataset.tone = job.cleanupRequired || ["SUBMISSION_UNKNOWN", "FAILED", "INTERRUPTED"].includes(job.state) ? "warning" : job.state === "SUCCEEDED" ? "success" : "neutral";
    view.resources.textContent = `内存状态：${job.memoryReleased === true ? "资源已归还或未占用" : "尚未确认归还"} · 需要清理：${job.cleanupRequired ? "是" : "否"} · 取消意图：${job.cancelRequested ? "已记录" : "无"}`;
    view.resources.className = job.cleanupRequired ? "drama-task-error" : "muted";
    view.detail.textContent = `${job.gatewayJobId ? `原 Gateway 任务 ${job.gatewayJobId}` : "尚无 Gateway 回执"} · 派发键 ${job.dispatchKey || "待确认"}`;
    view.error.textContent = errorText(job.error);
    const settings = [[view.cancel, TERMINAL.has(job.state) || job.cancelRequested],
      [view.release, job.memoryReleased === true || !job.cleanupRequired || !job.gatewayJobId],
      [view.continue, state.production?.paused || !job.canContinue || !(job.items || []).some(item => item.submissionPhase === "NOT_SUBMITTED")]];
    for (const [control, disabled] of settings) { control.dataset.unavailable = String(disabled); control.disabled = disabled || context.isBusy(); }
    for (const item of job.items || []) upsertItem(view, item);
  }
  function upsertItem(jobView, item) {
    let view = jobView.items.get(item.id);
    if (!view) {
      const card = node("div", null, jobView.itemsHost, "card");
      const title = node("strong", null, card), status = node("p", null, card, "muted"), inputs = node("p", null, details(card, "生成参数与版本"), "muted");
      const error = node("p", null, card, "drama-task-error"), candidateHost = node("div", null, card);
      card.append(inputs.parentNode);
      view = { card, title, status, inputs, error, candidateHost, item, assetId: null, takeId: null };
      jobView.items.set(item.id, view);
    }
    view.item = item;
    const shot = state.shots.find(shot => shot.id === item.shotId);
    view.title.textContent = `${Number(item.index) + 1} · ${shot?.title || item.shotId}`;
    view.status.textContent = `${STATES[item.state] || item.state} · 提交阶段：${STATES[item.submissionPhase] || item.submissionPhase || "未知"}${item.submissionPhase === "NOT_SUBMITTED" ? "（仅此阶段允许续拍）" : ""}`;
    view.inputs.textContent = `固定修订 ${item.shotRevisionId} · 种子 ${item.seed} · 参考音频 ${item.audioAssetId}`;
    view.error.textContent = errorText(item.error);
    if (!item.takeId) return;
    const take = state.takes.get(item.takeId);
    const assetId = item.assetId || take?.assetId;
    if (!assetId) {
      if (!view.waiting) view.waiting = node("p", "候选已记录，正在读取可播放素材关联…", view.candidateHost, "muted");
      return;
    }
    if (view.assetId !== assetId || view.takeId !== item.takeId) {
      view.assetId = assetId; view.takeId = item.takeId;
      view.candidateHost.replaceChildren(); view.waiting = null;
      const video = node("video", null, view.candidateHost); video.controls = true; video.preload = "none";
      video.src = mediaUrl(state.projectId, assetId); video.setAttribute("aria-label", `${shot?.title || item.shotId} 视频候选`);
      video.onerror = () => { view.mediaError.textContent = "视频无法播放，请核查素材是否存在或下载检查。"; };
      view.mediaError = node("p", null, view.candidateHost, "drama-task-error");
      const link = node("a", "打开 / 保存候选视频", view.candidateHost); link.href = video.src; link.target = "_blank"; link.rel = "noopener";
      view.reviewStatus = node("p", null, view.candidateHost, "muted");
      view.notes = field(view.candidateHost, "人工审片记录（批准和拒绝均必填，最多 2000 字）", state.reviewDrafts.get(item.takeId) || "", "textarea");
      view.notes.maxLength = 2000;
      view.notes.oninput = () => { if (view.notes.value) state.reviewDrafts.set(item.takeId, view.notes.value); else state.reviewDrafts.delete(item.takeId); };
      const actions = node("div", null, view.candidateHost, "row");
      button(actions, "审片批准", () => reviewTake(jobView, view, "approved"));
      button(actions, "审片拒绝", () => reviewTake(jobView, view, "rejected"));
      view.adopt = button(actions, "采用此视频", () => adoptTake(jobView, view), true, true);
      node("p", "人工批准与采用分别操作。拒绝只记录审片意见，不自动移走已经采用的视频；旧采用和冻结时间线继续保留。", view.candidateHost, "muted");
    }
    const reviewStatus = item.reviewStatus || take?.reviewStatus || "pending";
    const adopted = take?.selectedTakeId === item.takeId;
    view.reviewStatus.textContent = `${REVIEW[reviewStatus] || reviewStatus}${adopted ? " · 当前已采用" : " · 尚未采用"}${take?.stale ? " · 来自旧修订" : ""}`;
    view.adopt.textContent = adopted ? "当前已采用" : "采用此视频";
    view.adopt.dataset.unavailable = String(reviewStatus !== "approved" || adopted);
    view.adopt.disabled = reviewStatus !== "approved" || adopted || context.isBusy();
  }
  async function reviewTake(jobView, view, decision) {
    const notes = view.notes.value.trim();
    if (!notes || notes.length > 2000) throw new Error("请填写 1–2000 字人工审片记录；批准时也需说明通过理由。");
    const epoch = state.epoch, takeId = view.item.takeId;
    ++state.request;
    await post(`${path("takes", takeId)}/review`, { decision, notes });
    if (!current(epoch)) return;
    state.reviewDrafts.delete(takeId); view.notes.value = "";
    await readEpisode(jobView.job.episodeId, true);
    if (!current(epoch)) return;
    const result = await get(path("video-jobs", jobView.job.id));
    if (!current(epoch)) return;
    replaceJob(result.job); await context.syncEpisode();
    context.report(decision === "approved" ? "人工审片已批准。还需单独点击采用，才会替换当前选片。" : "人工审片已拒绝；原采用与冻结时间线未改动。");
  }
  async function adoptTake(jobView, view) {
    const epoch = state.epoch, item = view.item;
    ++state.request;
    await readEpisode(jobView.job.episodeId, true);
    if (!current(epoch)) return;
    const take = state.takes.get(item.takeId);
    if (!take || take.reviewStatus !== "approved") throw new Error("此候选尚未通过人工审片，请先批准并刷新核查。");
    const production = await get(`${path("shots", item.shotId)}/production`);
    if (!current(epoch)) return;
    const stale = Boolean(take.stale || item.shotRevisionId !== production.currentRevisionId);
    if (stale && !window.confirm("此候选来自镜头旧修订。确认用它替换当前采用视频？已有冻结时间线不受影响。")) return;
    await post(`${path("shots", item.shotId)}/selection`, { takeId: item.takeId, expectedRevision: production.revision, ...(stale ? { acceptStale: true } : {}) });
    if (!current(epoch)) return;
    await readEpisode(jobView.job.episodeId, true);
    if (!current(epoch)) return;
    upsertJob(jobView.job); await context.syncAssets(); await context.syncEpisode();
    context.report("视频已采用。既有时间线保留原片段，需在时间线中显式替换或重新建立。");
  }
  function schedule() {
    clearTimeout(timer);
    if (isVisible()) timer = setTimeout(poll, 4000);
  }
  async function poll() {
    if (!isVisible()) return;
    if (context.isBusy() || polling) { schedule(); return; }
    polling = true;
    try { await refreshRead({ updateAssets: false }); }
    catch (error) { if (isVisible()) context.report(`视频状态查询暂不可用：${error.message}。仅查询原任务，不会重提。`, true); }
    finally { polling = false; schedule(); }
  }
  function clearContext() {
    ++state.epoch; ++state.request; clearTimeout(timer); episodeReads.clear(); jobViews.clear();
    state.key = null; state.projectId = null; state.episodeId = null;
    state.assets = []; state.shots = []; state.jobs = []; state.production = null;
    state.drafts = []; state.dirty = false; state.defaultAudioId = ""; state.takes.clear(); state.reviewDrafts.clear(); state.pendingSubmission = null;
    initialViewChosen = false;
    root.replaceChildren(); draftHost = null; jobsHost = null;
  }
  return {
    showView,
    mount(parent) {
      const incoming = context.getContext();
      const pid = incoming.project?.id, eid = incoming.episode?.id;
      if (!pid) { clearContext(); return; }
      const key = `${pid}/${eid || ""}`;
      parent.append(root);
      if (state.key !== key) {
        clearContext(); state.key = key; state.projectId = pid; state.episodeId = eid || null;
        state.assets = incoming.assets || []; state.shots = incoming.shots || [];
        state.pendingSubmission = eid ? loadPending(pid, eid) : null;
        if (state.pendingSubmission) {
          state.drafts = state.pendingSubmission.payload.shots.map(shot => ({ ...shot,
            title: state.shots.find(currentShot => currentShot.id === shot.shotId)?.title || shot.shotId,
            pendingRecovered: true }));
          state.dirty = true;
        }
        storeTakes({ shots: state.shots }); render();
        const epoch = state.epoch;
        refreshRead().catch(error => { if (current(epoch)) report(error); });
      } else {
        state.assets = incoming.assets || state.assets; state.shots = incoming.shots || state.shots;
        storeTakes({ shots: state.shots });
        updateSourceChoices();
        for (const job of state.jobs) upsertJob(job);
      }
      schedule();
    },
    hasUnsaved: () => state.dirty || state.reviewDrafts.size > 0,
    hasPendingSubmission: () => Boolean(state.pendingSubmission),
    pendingIsPersisted: () => Boolean(state.pendingSubmission?.persisted),
    refresh: refreshRead,
    resume: schedule,
    pause() { clearTimeout(timer); for (const media of root.querySelectorAll("video,audio")) media.pause(); },
    detach: clearContext,
  };
}
