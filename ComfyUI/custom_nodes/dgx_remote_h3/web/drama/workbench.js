import { get, post, upload, segment, requestKey, mediaUrl } from "./api.js";
import { createProductionWorkbench } from "./production.js";
import { createVideoWorkbench } from "./video.js";
import { WORKFLOW_STEPS, nextWorkflowAction, showWorkflowPage, editableFrames } from "./workflow.js";

const TERMINAL = new Set(["SUCCEEDED", "FAILED", "INTERRUPTED", "CANCELED"]);
const STATES = { QUEUED: "等待导出", PENDING: "等待导出", RUNNING: "正在导出", VALIDATING: "检查媒体",
  SUCCEEDED: "导出完成 · 待审片", FAILED: "导出失败", INTERRUPTED: "导出已中断", CANCELED: "已取消" };
const make = (tag, text, parent, className) => {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (className) node.className = className;
  parent?.append(node);
  return node;
};
const titleOf = value => value?.title || value?.filename || value?.name || value?.id || "未命名";
const PREFERENCE_KEY = "dgx.drama.last-selection.v1";
function readPreference() {
  try { return JSON.parse(localStorage.getItem(PREFERENCE_KEY)) || {}; }
  catch { return {}; }
}
function savePreference(projectId, episodeId) {
  // Only navigation preference lives in the browser; all business data comes from Gateway.
  try { localStorage.setItem(PREFERENCE_KEY, JSON.stringify({ projectId, episodeId })); }
  catch { /* Browser storage is optional. */ }
}
const integer = (value, title, min = 0) => {
  const number = Number(value);
  if (String(value).trim() === "" || !Number.isSafeInteger(number) || number < min) throw new Error(`${title}必须是大于等于 ${min} 的整数。`);
  return number;
};

export async function setupDramaWorkbench() {
  let capabilities;
  try { capabilities = await get("/capabilities"); }
  catch { return; } // Disabled/unavailable Gateway must not disturb the existing extension.
  if (!capabilities.enabled || document.querySelector(".dgx-drama-launch")) return;
  const style = make("link", null, document.head);
  style.rel = "stylesheet";
  style.href = new URL("./workbench.css", import.meta.url).href;
  const launch = make("button", "短剧工作台", document.body, "dgx-drama-launch");
  const panel = make("section", null, document.body, "dgx-drama");
  panel.hidden = true;
  panel.tabIndex = -1;
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-modal", "true");
  panel.setAttribute("aria-label", "短剧工作台");
  const header = make("header", null, panel, "header");
  const heading = make("div", null, header);
  make("h2", "短剧工作台", heading);
  make("p", "从参考画面到成片，一步一步完成", heading, "muted");
  const headerActions = make("div", null, header, "row");
  const refreshButton = make("button", "刷新项目", headerActions);
  const closeButton = make("button", "返回画布", headerActions);
  const message = make("div", "先选择项目与分集，再从左侧步骤继续。关闭工作台后，已提交的任务仍会继续。", panel, "message");
  message.setAttribute("role", "status");
  message.setAttribute("aria-live", "polite");
  const content = make("div", null, panel, "content");
  const sidebar = make("aside", null, content, "sidebar");
  const main = make("main", null, content, "main");
  const state = { projects: [], library: [], project: null, episodes: [], assets: [], episode: null,
    shots: [], timeline: null, clips: [], exports: [], busy: false, dirty: false, epoch: 0, screen: "overview", timeUnit: "seconds" };
  let pages = {}, navigation = {}, pageHeading, pageDescription;
  let timer = null;
  let pollInFlight = false;
  let lastFocus = null;
  const exportViews = new Map();
  const path = (type, id) => `/${type}/${segment(id)}`;
  const assetById = id => state.assets.find(asset => asset.id === id);
  const allTakes = () => state.shots.flatMap(shot => (shot.takes || []).map(take => ({ ...take, shotTitle: shot.title })));
  const syncEpisode = async () => {
    if (!state.episode) return;
    const epoch = state.epoch, episodeId = state.episode.id;
    const result = await get(path("episodes", episodeId));
    if (epoch !== state.epoch || state.episode?.id !== episodeId) return;
    state.episode = result.episode; state.shots = result.shots || [];
    render();
  };
  const production = capabilities.entityKinds ? createProductionWorkbench({
    capabilities, getContext: () => ({ project: state.project, episode: state.episode }),
    isBusy: () => state.busy, isVisible: () => !panel.hidden, run, report,
    close: () => closeButton.click(), syncAssets: refreshAssets,
    syncEpisode, navigate: goTo,
  }) : null;
  const video = Object.hasOwn(capabilities, "videoBatchEnabled") ? createVideoWorkbench({
    capabilities, getContext: () => ({ project: state.project, episode: state.episode, assets: state.assets, shots: state.shots }),
    isBusy: () => state.busy, isVisible: () => !panel.hidden, run, report,
    syncAssets: refreshAssets, syncEpisode,
  }) : null;

  function report(text, isError = false) {
    message.textContent = text;
    message.classList.toggle("error", isError);
    message.setAttribute("role", isError ? "alert" : "status");
  }
  function busy(value) {
    state.busy = value;
    for (const node of panel.querySelectorAll("button,input,select,textarea")) {
      if (node === closeButton) continue;
      node.disabled = value || node.dataset.unavailable === "true";
    }
  }
  async function run(action) {
    if (state.busy) return;
    busy(true);
    try { await action(); }
    catch (error) { report(error.message || String(error), true); }
    finally { busy(false); }
  }
  function button(parent, title, action, primary = false, disabled = false) {
    const node = make("button", title, parent, primary ? "primary" : "");
    node.type = "button";
    node.dataset.unavailable = String(disabled);
    node.disabled = state.busy || disabled;
    node.onclick = () => run(action);
    return node;
  }
  function field(parent, title, type = "text", value = "") {
    const label = make("label", title, parent);
    const input = make("input", null, label);
    input.type = type;
    input.value = value;
    input.disabled = state.busy;
    return input;
  }
  function select(parent, title, choices, selected = "") {
    const label = make("label", title, parent);
    const node = make("select", null, label);
    for (const [value, text] of choices) {
      const option = make("option", text, node);
      option.value = value;
    }
    node.value = selected;
    node.disabled = state.busy;
    return node;
  }
  function block(parent, title) {
    const node = make("section", null, parent, "block");
    make("h3", title, node);
    return node;
  }
  function requiredTitle(input, fallback) {
    const value = input.value.trim();
    if (!value) throw new Error(fallback);
    return value;
  }
  function canDiscard() {
    if (video?.hasPendingSubmission()) return window.confirm(video.pendingIsPersisted()
      ? "视频提交结果尚未确认，原任务可能仍在执行。本标签会话会保留原请求键，返回此分集可继续确认；建议先核查同一任务。确认离开？"
      : "视频提交结果尚未确认。建议先按原请求键确认同一任务；离开会丢弃本地核查身份，原任务可能仍在执行。确认离开并改为从项目任务列表核查？");
    return (!state.dirty && !production?.hasUnsaved() && !video?.hasUnsaved()) || window.confirm("时间线、制作表单、视频批次或审片记录有未保存修改。离开将丢弃这些修改，是否继续？");
  }
  function resetEpisode() {
    state.episode = null; state.shots = []; state.timeline = null;
    state.clips = []; state.exports = []; state.dirty = false;
    exportViews.clear();
  }
  async function loadProject(id, episodeId) {
    const epoch = ++state.epoch;
    resetEpisode();
    state.project = null; state.assets = []; state.episodes = [];
    render();
    if (!id) return;
    const result = await get(path("projects", id));
    if (epoch !== state.epoch) return;
    state.project = result.project;
    state.assets = result.assets || [];
    state.episodes = result.episodes || [];
    const target = state.episodes.find(ep => ep.id === episodeId) || state.episodes[0];
    if (target) await loadEpisode(target.id);
    else { savePreference(state.project.id, null); render(); }
  }
  async function loadEpisode(id) {
    const epoch = ++state.epoch;
    resetEpisode();
    render();
    const result = await get(path("episodes", id));
    if (epoch !== state.epoch) return;
    state.episode = result.episode;
    report(`已打开「${result.episode.title}」。从左侧选择制作步骤。`);
    state.shots = result.shots || [];
    state.timeline = result.timeline;
    state.clips = (result.timeline?.clips || []).map(clip => ({ ...clip }));
    state.exports = result.exports || [];
    savePreference(state.project.id, state.episode.id);
    render();
  }
  async function reload() {
    const preference = readPreference();
    const projectId = state.project?.id || preference.projectId;
    const episodeId = state.episode?.id || preference.episodeId;
    const [projects, library] = await Promise.all([get("/projects"), get("/library")]);
    state.projects = projects.projects || [];
    state.library = library.sources || [];
    const chosen = state.projects.find(project => project.id === projectId) || state.projects[0];
    await loadProject(chosen?.id, episodeId);
  }
  async function refreshAssets() {
    if (!state.project) return;
    const epoch = state.epoch, projectId = state.project.id;
    const result = await get(path("projects", projectId));
    if (epoch !== state.epoch || state.project?.id !== projectId) return;
    state.assets = result.assets || [];
    state.episodes = result.episodes || [];
    render();
  }
  function renderSidebar() {
    sidebar.replaceChildren();
    const projects = block(sidebar, "项目");
    const picker = select(projects, "当前项目", [["", "选择项目"], ...state.projects.map(p => [p.id, titleOf(p)])], state.project?.id || "");
    picker.onchange = () => {
      const selected = picker.value;
      if (!canDiscard()) { picker.value = state.project?.id || ""; return; }
      run(() => loadProject(selected));
    };
    const createProject = make("details", null, projects, "disclosure");
    createProject.open = !state.projects.length;
    make("summary", "+ 新建项目", createProject);
    const title = field(createProject, "新项目名称"); title.placeholder = "例如：雨夜来信";
    const preset = select(createProject, "交付画布", [["landscape", "横屏 · 1344 × 768 · 24fps"], ["portrait", "竖屏 · 1080 × 1920 · 24fps"]], "landscape");
    button(createProject, "新建项目", async () => {
      const name = requiredTitle(title, "请填写新项目名称。");
      if (!canDiscard()) return;
      const portrait = preset.value === "portrait";
      const result = await post("/projects", { title: name, width: portrait ? 1080 : 1344, height: portrait ? 1920 : 768, fps: 24 });
      state.projects = (await get("/projects")).projects || [];
      await loadProject(result.project.id);
      report("项目已保存。接下来新建分集并导入素材。");
    }, true);
    if (!state.project) return;
    const episodes = block(sidebar, "分集");
    const epPicker = select(episodes, "当前分集", [["", "选择分集"], ...state.episodes.map(ep => [ep.id, titleOf(ep)])], state.episode?.id || "");
    epPicker.onchange = () => {
      const selected = epPicker.value;
      if (!canDiscard()) { epPicker.value = state.episode?.id || ""; return; }
      run(async () => { if (selected) await loadEpisode(selected); else { ++state.epoch; resetEpisode(); render(); } });
    };
    const createEpisode = make("details", null, episodes, "disclosure");
    createEpisode.open = !state.episodes.length;
    make("summary", "+ 新建分集", createEpisode);
    const epTitle = field(createEpisode, "新分集名称", "text", `第 ${state.episodes.length + 1} 集`);
    button(createEpisode, "新建分集", async () => {
      const name = requiredTitle(epTitle, "请填写分集名称。");
      if (!canDiscard()) return;
      const result = await post(`${path("projects", state.project.id)}/episodes`, { title: name });
      state.episodes = (await get(path("projects", state.project.id))).episodes || [];
      await loadEpisode(result.episode.id);
      report("分集已创建。");
    });
    make("p", `画布 ${state.project.width} × ${state.project.height} · ${state.project.fps}fps`, createProject, "muted");
    const nav = make("nav", null, sidebar, "workflow-nav");
    nav.setAttribute("aria-label", "短剧制作步骤");
    navigation = {};
    for (const step of WORKFLOW_STEPS) {
      const item = button(nav, "", () => goTo(step.id));
      item.className = "workflow-step";
      item.setAttribute("aria-label", step.id === "overview" ? step.title : `${step.number} ${step.title}`);
      make("span", step.number, item, "step-number");
      const copy = make("span", null, item, "step-copy");
      make("strong", step.title, copy); make("small", step.hint, copy);
      navigation[step.id] = item;
    }
    make("p", "修改后记得保存。已提交任务会在后台继续。", sidebar, "sidebar-note");
  }
  function preview(parent, assetId, label = "影片预览") {
    const video = make("video", null, parent);
    video.controls = true; video.preload = "none";
    video.setAttribute("aria-label", label);
    video.src = mediaUrl(state.project.id, assetId);
    video.onerror = () => {
      if (!parent.querySelector(".media-error")) make("p", "影片无法播放：素材可能缺失或格式不受浏览器支持，可刷新核查或下载。", parent, "muted media-error");
    };
    return video;
  }
  function renderAssets(parent) {
    const assets = make("details", null, parent, "block disclosure");
    make("summary", "已有视频素材 · 导入 / 上传", assets);
    make("p", "导入已有源视频，或上传新视频。文件经完整检查后归档，可供多个镜头复用。", assets, "muted");
    const controls = make("div", null, assets, "row");
    const source = select(controls, "已配置的参考影片库", [["", "选择源视频"], ...state.library.map(s => [s.sourceId, s.title])]);
    button(controls, "导入影片", async () => {
      if (!source.value) throw new Error("请先选择参考影片。");
      await post(`${path("projects", state.project.id)}/imports`, { sourceId: source.value });
      await refreshAssets(); report("影片已验证并加入项目素材。");
    }, false, !state.library.length);
    const file = field(controls, "上传视频（最大 512 MiB）", "file"); file.accept = "video/*,.mp4,.mov,.mkv,.webm";
    button(controls, "上传并检查", async () => {
      report("正在上传和检查视频，请等待服务端回执。");
      await upload(state.project.id, file.files?.[0]);
      await refreshAssets(); report("视频已归档。");
    });
    if (!state.library.length) make("p", "尚未配置参考影片库，可以使用文件上传。", assets, "muted");
    const videos = state.assets.filter(asset => !["image", "audio"].includes(asset.kind) && !/^(image|audio)\//.test(String(asset.contentType || "")));
    if (!videos.length) { make("p", "项目里还没有视频。导入两段视频后即可建立剪辑镜头；参考图片请使用制作面板。", assets, "empty"); return; }
    const grid = make("div", null, assets, "asset-grid");
    for (const asset of videos) {
      const card = make("div", null, grid, "card");
      make("strong", titleOf(asset), card);
      const identity = make("details", null, card, "technical-details");
      make("summary", "素材编号", identity); make("p", asset.id, identity, "muted");
      const details = make("details", null, card);
      make("summary", "预览素材", details);
      details.ontoggle = () => { if (details.open && !details.querySelector("video")) preview(details, asset.id); };
      const name = field(card, "新镜头名称", "text", `镜头 ${String(state.shots.length + 1).padStart(2, "0")}`);
      button(card, "以此素材建镜头", async () => {
        const title = requiredTitle(name, "请填写镜头名称。");
        if (!canDiscard()) return;
        await post(`${path("episodes", state.episode.id)}/shots`, { title, assetId: asset.id });
        await loadEpisode(state.episode.id);
        report("镜头和候选已建立，请选择采用候选。");
      }, false, !state.episode);
    }
  }
  function renderShots(parent) {
    const shots = make("details", null, parent, "block disclosure");
    make("summary", `镜头顺序与已有选片 · ${state.shots.length} 个镜头`, shots);
    if (!state.shots.length) { make("p", "先在本页新建镜头，或到“准备资料”导入已有视频。", shots, "empty"); return; }
    state.shots.forEach((shot, index) => {
      const row = make("div", null, shots, "shot");
      const title = make("div", null, row, "row");
      make("strong", `${index + 1}. ${shot.title}`, title);
      make("span", `修订 ${shot.revision}`, title, "badge");
      make("span", shot.selectedTakeId ? "已有采用候选" : "尚未采用", title, "muted");
      if (shot.needsReview) make("span", "制作依赖需复核", title, "badge");
      const reorder = async delta => {
        if (!canDiscard()) return;
        const ids = state.shots.map(s => s.id);
        [ids[index], ids[index + delta]] = [ids[index + delta], ids[index]];
        await post(`${path("episodes", state.episode.id)}/shot-order`, { shotIds: ids, expectedRevision: state.episode.revision });
        await loadEpisode(state.episode.id);
        report("镜头顺序已保存；已保存时间线保持原顺序。");
      };
      button(title, "上移", () => reorder(-1), false, index === 0);
      button(title, "下移", () => reorder(1), false, index === state.shots.length - 1);
      button(title, "删除", async () => {
        if (!canDiscard()) return;
        if (!window.confirm(`删除镜头「${shot.title}」？生成提示词和参考会留在审计记录里，镜头本身不会恢复。`)) return;
        await post(`${path("shots", shot.id)}/deletion`, { expectedRevision: shot.revision });
        await loadEpisode(state.episode.id);
        report("镜头已删除。生成记录仍可审计。");
      });
      for (const [takeIndex, take] of (shot.takes || []).entries()) {
        const candidate = make("div", null, row, "take");
        const link = make("a", `候选 ${takeIndex + 1} · ${titleOf(assetById(take.assetId))}${take.stale ? " · 旧修订" : ""}`, candidate);
        link.href = mediaUrl(state.project.id, take.assetId); link.target = "_blank"; link.rel = "noopener";
        const needsReview = take.source === "h3_video" && take.reviewStatus !== "approved";
        if (needsReview) make("span", take.reviewStatus === "rejected" ? "人工已拒绝" : "请先在视频面板审片批准", candidate, "muted");
        button(candidate, shot.selectedTakeId === take.id ? "已采用" : needsReview ? "需先审片批准" : "采用此候选", async () => {
          if (!canDiscard()) return;
          if (take.stale && !window.confirm("此视频来自镜头旧修订，是否确认采用？")) return;
          await post(`${path("shots", shot.id)}/selection`, { takeId: take.id, expectedRevision: shot.revision, ...(take.stale ? { acceptStale: true } : {}) });
          await loadEpisode(state.episode.id); report("采用结果已保存。既有时间线片段仍保留原候选，需要时手动替换。");
        }, false, shot.selectedTakeId === take.id || needsReview);
      }
    });
  }
  function defaultClip(takeId) {
    return { clipId: requestKey(), takeId, inFrame: 0, frames: 96, keepSourceAudio: true, fit: "contain" };
  }
  function renderTimeline(parent) {
    const section = block(parent, "时间线");
    const fps = state.timeline?.fps || state.project.fps || 24;
    make("p", `先排好片段，再调整时长。默认按秒编辑，时间对齐最近一帧（${fps} 帧 = 1 秒）；也可切换到精确帧编辑。保存后才能导出。`, section, "muted");
    const unit = select(section, "时间单位", [["seconds", "秒 · 便于剪辑"], ["frames", "帧 · 精确编辑"]], state.timeUnit);
    unit.onchange = () => { state.timeUnit = unit.value; render(); };
    const unitName = state.timeUnit === "seconds" ? "秒" : "帧";
    const actions = make("div", null, section, "row");
    button(actions, "按镜头顺序建立时间线", async () => {
      if (state.clips.length && !window.confirm("这会替换当前时间线草稿，是否继续？")) return;
      state.clips = state.shots.filter(s => s.selectedTakeId).map(s => defaultClip(s.selectedTakeId));
      state.dirty = true; render();
      report("已建立时间线草稿，请检查长度后保存。");
    }, false, !state.shots.some(s => s.selectedTakeId));
    const adoptedTakes = allTakes().filter(t => state.shots.some(s => s.selectedTakeId === t.id));
    const addTake = select(actions, "追加片段", [["", "选择已采用候选"], ...adoptedTakes.map(t => [t.id, t.shotTitle])]);
    button(actions, "追加", async () => {
      if (!addTake.value) throw new Error("请选择已采用候选。");
      state.clips.push(defaultClip(addTake.value)); state.dirty = true; render();
    }, false, !adoptedTakes.length);
    const info = make("p", null, section, "timeline-info");
    function updateInfo() {
      const total = state.clips.reduce((sum, clip) => sum + (Number(clip.frames) || 0), 0);
      const fps = state.timeline?.fps || state.project.fps || 24;
      info.textContent = `${state.clips.length} 个片段 · ${total} 帧 · ${(total / fps).toFixed(3)} 秒 · ${state.dirty ? "未保存修改" : state.timeline ? `已保存版本 ${state.timeline.revision}` : "尚未保存"}`;
      info.classList.toggle("dirty", state.dirty);
      exportButton.disabled = state.busy || !state.timeline || state.dirty;
      exportButton.dataset.unavailable = String(!state.timeline || state.dirty);
    }
    if (!state.clips.length) make("p", "采用候选后，按镜头顺序建立时间线，或逐个追加片段。", section, "empty");
    else {
      const scroll = make("div", null, section, "table-scroll");
      const table = make("table", null, scroll);
      const head = make("tr", null, make("thead", null, table));
      for (const text of ["片段", "候选", `源入点（${unitName}）`, `保留长度（${unitName}）`, "原声", "画幅", "操作"]) make("th", text, head);
      const body = make("tbody", null, table);
      state.clips.forEach((clip, index) => {
        const row = make("tr", null, body);
        make("td", String(index + 1), row);
        const takeCell = make("td", null, row);
        const take = allTakes().find(t => t.id === clip.takeId);
        make("span", take?.shotTitle || "候选不可用", takeCell);
        for (const [key, label, min] of [["inFrame", "源入点", 0], ["frames", "保留长度", 1]]) {
          const cell = make("td", null, row);
          const input = make("input", null, cell); input.type = "number"; input.min = String(state.timeUnit === "seconds" ? min / fps : min); input.step = state.timeUnit === "seconds" ? "any" : "1";
          input.value = state.timeUnit === "seconds" && clip[key] !== "" ? Number((Number(clip[key]) / fps).toFixed(6)) : clip[key]; input.setAttribute("aria-label", `片段 ${index + 1} ${label}`);
          input.oninput = () => { clip[key] = editableFrames(input.value, state.timeUnit, fps); state.dirty = true; updateInfo(); };
        }
        const audioCell = make("td", null, row);
        const audio = make("input", null, audioCell); audio.type = "checkbox"; audio.checked = clip.keepSourceAudio !== false;
        audio.setAttribute("aria-label", `片段 ${index + 1} 保留原声`);
        audio.onchange = () => { clip.keepSourceAudio = audio.checked; state.dirty = true; updateInfo(); };
        const fitCell = make("td", null, row);
        const fit = make("select", null, fitCell);
        const supportedFits = new Set(capabilities.fitModes || ["contain"]);
        for (const [value, title] of [["contain", "完整画面（留边）"], ["cover", "居中填满（裁切）"]].filter(([value]) => supportedFits.has(value))) {
          const option = make("option", title, fit); option.value = value;
        }
        fit.value = clip.fit || "contain"; fit.setAttribute("aria-label", `片段 ${index + 1} 画幅适配`);
        fit.onchange = () => { clip.fit = fit.value; state.dirty = true; updateInfo(); };
        const controls = make("td", null, row);
        const move = delta => { [state.clips[index], state.clips[index + delta]] = [state.clips[index + delta], state.clips[index]]; state.dirty = true; render(); };
        button(controls, "↑", () => move(-1), false, index === 0).setAttribute("aria-label", `上移片段 ${index + 1}`);
        button(controls, "↓", () => move(1), false, index === state.clips.length - 1).setAttribute("aria-label", `下移片段 ${index + 1}`);
        button(controls, "移除", () => { state.clips.splice(index, 1); state.dirty = true; render(); });
      });
    }
    const saveActions = make("div", null, section, "row");
    button(saveActions, "保存时间线", async () => {
      const clips = state.clips.map((clip, index) => ({
        clipId: clip.clipId, takeId: clip.takeId,
        inFrame: integer(clip.inFrame, `片段 ${index + 1} 入点`),
        frames: integer(clip.frames, `片段 ${index + 1} 长度`, 1),
        keepSourceAudio: clip.keepSourceAudio !== false, fit: clip.fit || "contain",
      }));
      const result = await post(`${path("episodes", state.episode.id)}/timeline-revisions`, {
        expectedRevision: state.timeline?.revision || 0, clips,
      });
      state.timeline = result.timeline; state.clips = result.timeline.clips.map(clip => ({ ...clip }));
      state.dirty = false; render(); report("时间线已保存，可以开始导出。导出始终使用已保存的版本。");
    }, true, !state.clips.length);
    const exportButton = button(saveActions, "导出已保存版本", async () => {
      const result = await post(`${path("timeline-revisions", state.timeline.id)}/exports`);
      state.exports.unshift(result.export);
      upsertExport(result.export);
      report(`导出任务已提交：${result.export.id}。可以关闭面板，任务继续运行。`);
      schedulePoll();
    }, false, !state.timeline || state.dirty);
    updateInfo();
  }
  let exportsContainer = null;
  function upsertExport(item) {
    if (!exportsContainer) return;
    exportsContainer.querySelector(".empty")?.remove();
    let view = exportViews.get(item.id);
    if (!view) {
      const card = make("div", null, exportsContainer, "export-card");
      make("strong", `成片 · ${state.episode?.title || "当前分集"}`, card);
      const status = make("p", null, card, "badge");
      const identity = make("details", null, card, "technical-details");
      make("summary", "导出记录与版本", identity);
      make("p", `导出 ${item.id}`, identity, "muted");
      const detail = make("p", null, identity, "muted");
      const output = make("div", null, card);
      view = { card, status, detail, output, assetId: null, retry: null, item };
      exportViews.set(item.id, view);
    }
    view.item = item;
    view.status.textContent = STATES[item.state] || item.state || "等待状态";
    const validTimelineId = typeof item.timelineId === "string" && item.timelineId.trim().length > 0;
    const failure = item.error?.message || (typeof item.error === "string" ? item.error : "");
    view.detail.textContent = [validTimelineId ? `冻结时间线：${item.timelineId}` : "缺少冻结时间线编号，无法按原版本重新导出。", failure].filter(Boolean).join(" · ");
    const canRetry = item.state === "FAILED" || item.state === "INTERRUPTED";
    view.detail.parentNode.open = canRetry;
    if (canRetry && !view.retry) {
      view.retry = button(view.card, "按原版本重新导出", async () => {
        const original = view.item;
        if (!["FAILED", "INTERRUPTED"].includes(original.state)) throw new Error("仅失败或中断的导出可以重新提交，请刷新核查任务状态。");
        if (typeof original.timelineId !== "string" || !original.timelineId.trim()) throw new Error("缺少原任务的冻结时间线编号，请刷新核查，不能使用当前时间线替代。");
        const result = await post(`${path("timeline-revisions", original.timelineId)}/exports`);
        if (!result.export?.id) throw new Error("导出已提交但回执缺少任务编号，请刷新恢复任务列表，勿立即重复提交。");
        state.exports.unshift(result.export);
        upsertExport(result.export);
        report(`已按原冻结版本重新导出：${result.export.id}。本次创建了独立导出任务。`);
        schedulePoll();
      }, false, !validTimelineId);
    }
    if (view.retry) {
      view.retry.hidden = !canRetry;
      view.retry.dataset.unavailable = String(!canRetry || !validTimelineId);
      view.retry.disabled = state.busy || !canRetry || !validTimelineId;
    }
    if (item.assetId && view.assetId !== item.assetId) {
      view.assetId = item.assetId;
      view.output.replaceChildren();
      preview(view.output, item.assetId, "导出影片预览");
      const link = make("a", "打开 / 下载成片", view.output);
      link.href = mediaUrl(state.project.id, item.assetId); link.target = "_blank"; link.rel = "noopener";
      const download = make("a", " 保存文件", view.output);
      download.href = link.href; download.download = `drama-${item.id}.mp4`;
    }
  }
  async function goTo(screen, tab) {
    if (!pages[screen]) return false;
    if (production && (screen === "prepare" || screen === "shots")) {
      const desired = tab || (screen === "shots" ? "shots" : production.activeTab() === "shots" ? "entities" : production.activeTab());
      if (!await production.activate(desired)) return false;
      production.mount(pages[screen].querySelector(".production-host"));
    }
    if (screen === "video" && tab) video?.showView(tab);
    state.screen = screen;
    showCurrentPage(); main.scrollTop = 0;
    pageHeading.focus();
    return true;
  }
  function showCurrentPage() {
    const step = WORKFLOW_STEPS.find(item => item.id === state.screen) || WORKFLOW_STEPS[0];
    pageHeading.textContent = step.title;
    pageDescription.textContent = step.description;
    showWorkflowPage(pages, navigation, step.id);
  }
  function renderOverview(parent) {
    const hero = make("section", null, parent, "overview-hero");
    make("p", state.episode?.title || "准备开始新的一集", hero, "eyebrow");
    make("h3", state.project.title, hero);
    const action = nextWorkflowAction(state);
    make("p", action.detail, hero);
    if (state.episode) button(hero, action.title + " →", () => goTo(action.screen, action.screen === "video" ? "review" : undefined), true);
    else make("strong", action.title, hero);
    const stats = make("div", null, parent, "overview-stats");
    for (const [value, label] of [[state.shots.length, "镜头"], [state.shots.filter(s => s.selectedTakeId).length, "已选片"],
      [state.assets.length, "项目素材"], [state.exports.filter(e => e.state === "SUCCEEDED").length, "已导出"]]) {
      const stat = make("div", null, stats); make("strong", String(value), stat); make("span", label, stat);
    }
    const shortcuts = make("div", null, parent, "workflow-cards");
    for (const step of WORKFLOW_STEPS.filter(step => step.id !== "overview")) {
      const card = make("section", null, shortcuts, "card");
      make("span", `第 ${step.number} 步`, card, "eyebrow");
      make("h3", step.title, card); make("p", step.description, card, "muted");
      button(card, `进入${step.title} →`, () => goTo(step.id));
    }
    make("p", "可以随时切换步骤。新生成不会自动替换已采用的视频；剪辑和导出使用明确保存的版本。", parent, "muted overview-note");
  }
  function render() {
    renderSidebar(); main.replaceChildren(); exportViews.clear(); exportsContainer = null;
    pages = {};
    if (!state.project) { production?.pause(); video?.detach(); make("p", state.projects.length ? "从左侧选择一个项目，继续制作。" : "先在左侧创建项目，再建立分集。", main, "empty"); return; }
    const introduction = make("div", null, main, "page-intro");
    pageHeading = make("h2", null, introduction); pageHeading.tabIndex = -1;
    pageDescription = make("p", null, introduction, "muted");
    for (const step of WORKFLOW_STEPS) {
      pages[step.id] = make("section", null, main, "workflow-page");
      pages[step.id].dataset.step = step.id;
      pages[step.id].setAttribute("aria-label", step.title);
    }
    renderOverview(pages.overview);
    const prepareHost = make("div", null, pages.prepare, "production-host");
    const shotsHost = make("div", null, pages.shots, "production-host");
    production?.mount(production.activeTab() === "shots" ? shotsHost : prepareHost);
    renderAssets(pages.prepare);
    video?.mount(pages.video);
    if (state.episode) {
      renderShots(pages.shots); renderTimeline(pages.edit);
      exportsContainer = block(pages.edit, "导出任务与成片");
      if (!state.exports.length) make("p", "还没有成片。保存上方时间线，再点击“导出已保存版本”。", exportsContainer, "empty");
      for (const item of state.exports) upsertExport(item);
    } else {
      make("p", "请先在左侧新建或选择分集，然后开始制作镜头。", pages.shots, "empty");
      make("p", "选择分集后，这里会显示它的时间线与成片。", pages.edit, "empty");
    }
    showCurrentPage(); busy(state.busy);
  }
  function schedulePoll() {
    clearTimeout(timer);
    if (!panel.hidden) timer = setTimeout(poll, 2500);
  }
  async function poll() {
    if (panel.hidden || pollInFlight) return;
    if (state.busy) { schedulePoll(); return; }
    const epoch = state.epoch;
    const pending = state.exports.filter(item => !TERMINAL.has(item.state));
    pollInFlight = true;
    try {
      const results = await Promise.allSettled(pending.map(item => get(path("exports", item.id))));
      if (epoch !== state.epoch || panel.hidden) return;
      for (const result of results) {
        if (result.status === "rejected") { report(`导出状态查询失败：${result.reason.message}。稍后继续查询原任务。`, true); continue; }
        const item = result.value.export;
        const index = state.exports.findIndex(existing => existing.id === item.id);
        if (index >= 0) state.exports[index] = item;
        upsertExport(item); // Update status and output once; preserve any playing video.
      }
    } finally { pollInFlight = false; schedulePoll(); }
  }
  closeButton.onclick = () => {
    panel.hidden = true; clearTimeout(timer);
    production?.pause();
    video?.pause();
    for (const media of panel.querySelectorAll("video,audio")) media.pause();
    (lastFocus?.isConnected ? lastFocus : launch).focus();
  };
  panel.addEventListener("keydown", event => {
    if (event.key === "Escape") { event.stopPropagation(); closeButton.click(); }
    if (event.key !== "Tab") return;
    const nodes = [...panel.querySelectorAll("button:not(:disabled),a[href],input:not(:disabled),select:not(:disabled),textarea:not(:disabled),summary,video[controls],audio[controls]")]
      .filter(node => node.getClientRects().length);
    const first = nodes[0], last = nodes[nodes.length - 1];
    if (event.shiftKey && (document.activeElement === first || document.activeElement === panel)) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  });
  refreshButton.onclick = () => { if (canDiscard()) run(async () => { await reload(); report("已从 Gateway 恢复最新项目、时间线和导出状态。"); }); };
  launch.onclick = () => {
    lastFocus = document.activeElement; panel.hidden = false; closeButton.focus();
    if (!state.projects.length && !state.project) run(reload);
    production?.resume();
    video?.resume();
    schedulePoll();
  };
  window.addEventListener("beforeunload", event => {
    if (state.dirty || production?.hasUnsaved() || video?.hasUnsaved()) { event.preventDefault(); event.returnValue = ""; }
  });
  render();
}
