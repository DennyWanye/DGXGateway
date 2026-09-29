import { get, post, segment, mediaUrl, requestKey } from "./api.js";
import { openShotOnCanvas } from "./canvas.js";
import { createImageSubmissionFeedback, imageJobFeedback } from "./image-feedback.js";

const KINDS = { character: "人物", location: "场景", costume: "服装", prop: "道具" };
const ROLES = { character: [["identity_front", "正面身份图"], ["identity_3q", "四分之三侧面"], ["identity_back", "背视图"], ["costume_fullbody", "服装全身图"], ["identity_sheet", "全位图"]],
  location: [["location_wide", "场景全景"], ["location_detail", "场景细节"], ["layout", "空间布局"]],
  costume: [["costume_fullbody", "服装全身图"]], prop: [["prop_detail", "道具细节"]] };
const TASK_STATES = { PREPARED: "已保存，等待执行", LOADING: "加载模型", RELEASING: "释放模型资源", CANCEL_REQUESTED: "取消待确认", UNKNOWN: "结果未知，先核查", QUEUED: "等待执行", SUBMITTED: "已提交", RUNNING: "正在制作", COLLECTING: "归档素材", VALIDATING: "校验图片",
  SUCCEEDED: "候选已归档", FAILED: "失败", CANCELED: "已取消", INTERRUPTED: "已中断", SUBMISSION_UNKNOWN: "提交结果不明，需核查" };
const node = (tag, text, parent, className) => {
  const el = document.createElement(tag); if (text != null) el.textContent = text;
  if (className) el.className = className; parent?.append(el); return el;
};
const nameOf = value => value?.name || value?.title || value?.filename || value?.id || "未命名";
const currentOf = entity => entity.current || entity.revisions?.find(r => r.id === entity.currentRevisionId) || entity.revisions?.at(-1);
const isImage = asset => asset.kind === "image" || String(asset.contentType || "").startsWith("image/");
const positiveInt = (input, title, min = 1) => {
  const value = Number(input.value); if (!input.value.trim() || !Number.isSafeInteger(value) || value < min) throw new Error(`${title}必须是至少 ${min} 的整数。`);
  return value;
};

export function imageBatchParameters(count, parallelism) {
  const n = Number(count), p = Number(parallelism);
  if (!Number.isSafeInteger(n) || n < 1 || !Number.isSafeInteger(p) || p < 1) throw new Error("候选数量和每批并行数量须为正整数。");
  return { batch_count: n, parallelism: Math.min(n, p) };
}
export function compositionParameters(backgroundAssetId, layers) {
  if (!backgroundAssetId || !Array.isArray(layers) || layers.length < 1 || layers.length > 16) throw new Error("请选择背景并添加 1–16 个图层。");
  const number = (value, title, min, max) => {
    const result = Number(value);
    if (String(value).trim() === "" || !Number.isSafeInteger(result) || result < min || result > max) throw new Error(`${title}须为 ${min}–${max} 的整数。`);
    return result;
  };
  return { background_asset_id: backgroundAssetId, layers: layers.map(layer => {
    if (!layer.asset_id || !layer.name?.trim() || layer.name.trim().length > 80) throw new Error("每个图层必须有图片和 1–80 字名称。");
    return { name: layer.name.trim(), asset_id: layer.asset_id,
      x: number(layer.x, "水平偏移", -4096, 4096), y: number(layer.y, "垂直偏移", -4096, 4096),
      width: number(layer.width, "图层宽度", 1, 4096), height: number(layer.height, "图层高度", 1, 4096),
      opacity: number(layer.opacity, "不透明度（百分比）", 1, 100), flip_x: layer.flip_x === true };
  }) };
}

export function createProductionWorkbench(context) {
  const root = node("section", null, null, "block drama-production");
  const state = { key: null, projectId: null, episodeId: null, entities: [], assets: [], shots: [], library: [], jobs: [],
    tab: "entities", entityId: null, entityRevisionId: null, kind: "character", shotId: null, production: null, dirty: false, epoch: 0 };
  let content;
  let heading;
  let imageFeedbackView = null;
  const path = (kind, id) => `/${kind}/${segment(id)}`;
  const images = () => state.assets.filter(isImage);
  const pictureChoices = () => [["", "暂不选择"], ...images().map(asset => [asset.id, nameOf(asset)])];
  function markDirty() { state.dirty = true; }
  function confirmDiscard() { return !state.dirty || window.confirm("制作表单有未保存修改，是否放弃这些修改？"); }
  function button(parent, text, action, disabled = false, primary = false) {
    const el = node("button", text, parent, primary ? "primary" : ""); el.type = "button";
    el.dataset.unavailable = String(disabled); el.disabled = disabled || context.isBusy();
    el.onclick = () => context.run(action); return el;
  }
  function input(parent, label, value = "", type = "text", dirty = true) {
    const wrap = node("label", label, parent);
    const el = node(type === "textarea" ? "textarea" : "input", null, wrap);
    if (type !== "textarea") el.type = type;
    el.value = value ?? ""; if (dirty) el.oninput = markDirty;
    return el;
  }
  function select(parent, label, choices, selected = "", { multiple = false, dirty = true } = {}) {
    const wrap = node("label", label, parent); const el = node("select", null, wrap); el.multiple = multiple;
    for (const [value, title] of choices) { const option = node("option", title, el); option.value = value; if (multiple) option.selected = selected.includes(value); }
    if (!multiple) el.value = selected ?? ""; else el.size = Math.min(5, Math.max(2, choices.length));
    if (dirty) el.onchange = markDirty;
    return el;
  }
  function image(parent, assetId, label) {
    if (!assetId) return;
    const img = node("img", null, parent, "drama-reference-image");
    img.src = mediaUrl(state.projectId, assetId); img.alt = label || "项目参考图"; img.loading = "lazy";
    img.onerror = () => { img.alt = "图片不可用，请刷新核查素材"; };
  }
  async function reload() {
    const epoch = ++state.epoch;
    const pid = state.projectId;
    if (!pid) return;
    const [entities, project, library, jobs] = await Promise.all([
      get(`${path("projects", pid)}/entities`), get(path("projects", pid)),
      get(`${path("projects", pid)}/image-library`), get(`${path("projects", pid)}/reference-jobs`),
    ]);
    if (epoch !== state.epoch) return;
    state.entities = entities.entities || []; state.assets = project.assets || [];
    state.library = library.sources || []; state.jobs = jobs.jobs || [];
    if (state.episodeId) {
      const episode = await get(path("episodes", state.episodeId));
      if (epoch !== state.epoch) return;
      state.shots = episode.shots || [];
    } else state.shots = [];
    if (state.shotId && state.shots.some(shot => shot.id === state.shotId)) {
      const result = await get(`${path("shots", state.shotId)}/production`);
      if (epoch !== state.epoch) return;
      state.production = result;
    } else { state.shotId = null; state.production = null; }
    render();
  }
  async function chooseShot(id) {
    if (!confirmDiscard()) return;
    state.dirty = false; state.shotId = id; state.production = null;
    const epoch = ++state.epoch;
    if (id) {
      const result = await get(`${path("shots", id)}/production`);
      if (epoch !== state.epoch) return;
      state.production = result;
    }
    render();
  }
  async function activate(tab) {
    if (!["entities", "shots", "images", "jobs"].includes(tab)) return false;
    if (state.tab === tab) return true;
    if (!confirmDiscard()) return false;
    state.dirty = false; state.tab = tab;
    if (tab === "shots" && !state.shotId && state.shots.length) await chooseShot(state.shots[0].id);
    else render();
    return true;
  }
  function disclosure(parent, title, text) {
    const box = node("details", null, parent, "technical-details");
    node("summary", title, box);
    if (text) node("p", text, box, "muted");
    return box;
  }
  function render() {
    root.replaceChildren();
    heading = node("div", null, root, "row");
    node("h3", state.tab === "shots" ? "镜头内容与参考画面" : "人物与参考素材", heading);
    button(heading, "刷新制作数据", async () => { if (!confirmDiscard()) return; state.dirty = false; await reload(); context.report("制作版本和图片任务已刷新。"); });
    const tabs = node("div", null, root, "row drama-tabs");
    for (const [key, title] of (state.tab === "shots" ? [] : [["entities", "人物与场景"], ["images", "参考图片"], ["jobs", "图片制作"]])) {
      const tab = button(tabs, title, async () => {
        await activate(key);
      }, false, state.tab === key);
      tab.setAttribute("aria-pressed", String(state.tab === key));
    }
    content = node("div", null, root);
    node("p", { entities: "先选资料类型，填写说明和参考图。保存草稿后，再批准可用于镜头的版本。", shots: "选一个镜头，写清画面与动作；保存后批准参考图，就能去生成视频。", images: "把图片加入项目，供人物资料和镜头共用。", jobs: "选择制作目标与图片工具，再填写要求。生成结果会保留为候选，由你选择。" }[state.tab], content, "section-help");
    ({ entities: renderEntities, shots: renderShots, images: renderImages, jobs: renderJobs })[state.tab]();
  }
  function renderEntities() {
    const controls = node("div", null, content, "row");
    const kind = select(controls, "资料类型", Object.entries(KINDS), state.kind, { dirty: false });
    kind.onchange = () => {
      if (!confirmDiscard()) { kind.value = state.kind; return; }
      state.kind = kind.value; state.entityId = null; state.entityRevisionId = null; state.dirty = false; render();
    };
    const matches = state.entities.filter(entity => entity.kind === state.kind);
    const pick = select(controls, "选择资料", [["", `新建${KINDS[state.kind]}`], ...matches.map(entity => [entity.id, nameOf(currentOf(entity))])], state.entityId || "", { dirty: false });
    pick.onchange = () => {
      if (!confirmDiscard()) { pick.value = state.entityId || ""; return; }
      state.entityId = pick.value || null; state.entityRevisionId = null; state.dirty = false; render();
    };
    const entity = matches.find(item => item.id === state.entityId);
    const revision = entity?.revisions?.find(item => item.id === state.entityRevisionId) || (entity && currentOf(entity));
    if (entity) {
      const history = select(controls, "查看修订", entity.revisions.map(item => [item.id, `版本 ${item.revision} · ${item.name}${item.id === entity.approvedRevisionId ? " · 当前批准" : ""}`]), revision.id, { dirty: false });
      history.onchange = () => { if (!confirmDiscard()) { history.value = revision.id; return; } state.entityRevisionId = history.value; state.dirty = false; render(); };
      disclosure(content, "版本与记录", `资料编号 ${entity.id} · 当前版本 ${currentOf(entity)?.revision} · ${entity.approvedRevisionId ? "已有批准版本" : "尚未批准"}`);
    }
    const fields = node("div", null, content, "drama-form-grid");
    const name = input(fields, `${KINDS[state.kind]}名称`, revision?.name || "");
    const description = input(fields, "形象、状态与制作说明", revision?.description || "", "textarea");
    const referenceInputs = [];
    for (const [role, label] of ROLES[state.kind]) {
      const old = revision?.references?.find(ref => ref.role === role)?.assetId || (revision?.referenceRole === role ? revision.referenceAssetId : "");
      const pic = select(fields, label, pictureChoices(), old || "");
      referenceInputs.push({ role, input: pic });
      if (old) image(fields, old, label);
    }
    const mainRole = select(fields, "主参考用途", ROLES[state.kind], revision?.referenceRole || ROLES[state.kind][0][0]);
    node("p", `保存创建新草稿修订。批准必须提供${ROLES[state.kind][0][1]}；审核后批准指定版本供镜头引用。旧修订和素材会保留。`, content, "muted");
    const actions = node("div", null, content, "row");
    button(actions, entity ? "保存为新修订" : `创建${KINDS[state.kind]}草稿`, async () => {
      if (!name.value.trim()) throw new Error("请填写实体名称。");
      const references = referenceInputs.filter(ref => ref.input.value).map(ref => ({ role: ref.role, assetId: ref.input.value }));
      const referenceAssetId = references.find(ref => ref.role === mainRole.value)?.assetId || null;
      const body = { name: name.value.trim(), description: description.value, references,
        referenceAssetId, referenceRole: mainRole.value };
      if (entity) await post(`${path("entities", entity.id)}/revisions`, { ...body, expectedRevision: entity.revision });
      else {
        const result = await post(`${path("projects", state.projectId)}/entities`, { ...body, kind: state.kind });
        state.entityId = result.entity?.id || null;
      }
      state.entityRevisionId = null; state.dirty = false; await reload(); context.report("实体草稿已保存；批准后才能作为镜头依赖。");
    }, false, true);
    if (revision) button(actions, revision.id === entity.approvedRevisionId ? "此版本已批准" : "批准此版本", async () => {
      if (state.dirty) throw new Error("请先保存或放弃表单修改，再批准已存版本。");
      const result = await post(`${path("entities", entity.id)}/approval`, { revisionId: revision.id, expectedRevision: entity.revision });
      await reload();
      const affected = result.affectedShotIds || [];
      context.report(`实体版本已批准。${affected.length ? `以下镜头需复核：${affected.map(id => state.shots.find(shot => shot.id === id)?.title || id).join("、")}` : "没有新增受影响镜头。"}`);
    }, revision.id === entity.approvedRevisionId || !(revision.references || []).some(ref => ref.role === ROLES[state.kind][0][0]));
  }
  function dependencyChoices(kind, selected) {
    const choices = [];
    for (const entity of state.entities.filter(item => item.kind === kind)) {
      for (const revision of entity.revisions || []) {
        if (revision.id === entity.approvedRevisionId || selected.includes(revision.id)) {
          choices.push([revision.id, `${revision.name} · v${revision.revision}${revision.id === entity.approvedRevisionId ? " · 批准" : " · 旧依赖"}`]);
        }
      }
    }
    return choices;
  }
  function renderShots() {
    if (!state.episodeId) { node("p", "请先创建或选择分集。", content, "empty"); return; }
    const controls = node("div", null, content, "row");
    const shot = select(controls, "制作镜头", [["", "选择镜头"], ...state.shots.map(item => [item.id, `${item.title}${item.needsReview ? " · 需复核" : ""}`])], state.shotId || "", { dirty: false });
    shot.onchange = () => context.run(() => chooseShot(shot.value));
    const newShot = disclosure(content, "+ 新建镜头"); newShot.open = !state.shots.length;
    const titleNew = input(newShot, "新镜头名称", `镜头 ${String(state.shots.length + 1).padStart(2, "0")}`, "text", false);
    button(newShot, "新建空镜头", async () => {
      if (!titleNew.value.trim()) throw new Error("请填写镜头名称。");
      if (!confirmDiscard()) return;
      const result = await post(`${path("episodes", state.episodeId)}/shots`, { title: titleNew.value.trim() });
      state.dirty = false; state.shotId = result.shot.id;
      await reload(); await context.syncEpisode(); context.report("空镜头已创建。现在可以编辑人物、构图与参考画面。");
    });
    if (!state.production) { node("p", "选择一个镜头以编辑固定版本的制作输入。", content, "empty"); return; }
    const production = state.production;
    const snapshot = production.current?.snapshot || {};
    disclosure(content, `当前内容版本 ${production.current?.revision} · 查看记录`, `保存校验版本 ${production.revision} · ${production.currentRevisionId}`);
    const fields = node("div", null, content, "drama-form-grid");
    const title = input(fields, "镜头名称", snapshot.title || "");
    const action = input(fields, "人物动作", snapshot.action || "", "textarea");
    const framing = input(fields, "景别 / 构图", snapshot.framing || "medium");
    const camera = input(fields, "摄影机运动", snapshot.camera || "locked");
    const prompt = input(fields, "参考画面提示词", snapshot.prompt || "", "textarea");
    const advanced = disclosure(content, "人物、场景关联与精确时长（可选）");
    const advancedFields = node("div", null, advanced, "drama-form-grid");
    const frames = input(advancedFields, "目标剪辑长度（帧）", snapshot.targetEditFrames || 96, "number"); frames.min = "1"; frames.step = "1";
    const characters = select(advancedFields, "人物批准版本（可多选）", dependencyChoices("character", snapshot.characterRevisionIds || []), snapshot.characterRevisionIds || [], { multiple: true });
    const location = select(advancedFields, "场景批准版本", [["", "无场景依赖"], ...dependencyChoices("location", [snapshot.locationRevisionId].filter(Boolean))], snapshot.locationRevisionId || "");
    const costumes = select(advancedFields, "服装批准版本（可多选）", dependencyChoices("costume", snapshot.costumeRevisionIds || []), snapshot.costumeRevisionIds || [], { multiple: true });
    const props = select(advancedFields, "道具批准版本（可多选）", dependencyChoices("prop", snapshot.propRevisionIds || []), snapshot.propRevisionIds || [], { multiple: true });
    const selected = element => [...element.selectedOptions].map(option => option.value);
    const actions = node("div", null, content, "row");
    button(actions, "保存镜头新修订", async () => {
      if (!title.value.trim()) throw new Error("请填写镜头名称。");
      const next = { ...snapshot, title: title.value.trim(), action: action.value, framing: framing.value,
        camera: camera.value, prompt: prompt.value, targetEditFrames: positiveInt(frames, "剪辑长度"),
        characterRevisionIds: selected(characters), locationRevisionId: location.value || null,
        costumeRevisionIds: selected(costumes), propRevisionIds: selected(props) };
      await post(`${path("shots", state.shotId)}/revisions`, { expectedRevision: production.revision, baseRevisionId: production.currentRevisionId, snapshot: next });
      state.dirty = false; await reload(); await context.syncEpisode(); context.report("镜头已保存为新内容修订，旧采用与时间线继续保留。");
    }, false, true);
    button(actions, "在 ComfyUI 画布编辑", async () => {
      if (state.dirty) throw new Error("请先保存镜头表单；画布将读取服务端已存版本。");
      await openShotOnCanvas(state.shotId); context.close();
      context.report("镜头草稿节点已追加到原画布。编辑后点击节点“回写为新修订”，不会覆盖原工作流。");
    });
    if (snapshot.approvedReferenceAssetId) image(content, snapshot.approvedReferenceAssetId, "已批准镜头参考画面");
    const referenceSection = node("div", null, content, "reference-selection card");
    node("h4", "确认镜头参考画面", referenceSection);
    const reference = select(referenceSection, "选择参考画面", pictureChoices(), snapshot.approvedReferenceAssetId || "", { dirty: false });
    button(referenceSection, "批准此参考画面", async () => {
      if (state.dirty) throw new Error("请先保存镜头制作字段，再批准参考画面。");
      if (!reference.value) throw new Error("请选择项目图片。");
      await post(`${path("shots", state.shotId)}/reference-selection`, { expectedRevision: production.revision, assetId: reference.value });
      await reload(); await context.syncEpisode(); context.report("参考画面已批准并产生新镜头版本。");
    });
    const invalidations = production.invalidations || [];
    if (invalidations.length) {
      const review = node("div", null, content, "card");
      node("h4", "依赖变更与复核", review);
      const unresolved = [];
      for (const item of invalidations) {
        const wrap = node("label", null, review, "inline-label");
        const check = node("input", null, wrap); check.type = "checkbox";
        const resolved = Boolean(item.resolved || item.reviewedAt || item.resolvedAt || item.status === "RESOLVED");
        check.disabled = resolved; check.dataset.unavailable = String(resolved);
        const entityName = nameOf(currentOf(state.entities.find(entity => entity.id === item.entityId) || {}));
        node("span", `${entityName} · ${item.reason || item.message || item.id} · 原版本 ${item.oldRevisionId || "未知"} → 批准版本 ${item.newRevisionId || "未知"}${resolved ? " · 已复核" : ""}`, wrap);
        if (!resolved) unresolved.push({ item, check });
      }
      const reason = input(review, "接受旧依赖的原因（必填）", "", "textarea", false);
      button(review, "确认接受所选旧依赖", async () => {
        if (state.dirty) throw new Error("请先保存制作表单，再复核已存依赖。");
        const invalidationIds = unresolved.filter(row => row.check.checked).map(row => row.item.id);
        if (!invalidationIds.length || !reason.value.trim()) throw new Error("请选择具体复核项并填写接受旧依赖的原因。");
        await post(`${path("shots", state.shotId)}/review`, { expectedRevision: production.revision, invalidationIds, reason: reason.value.trim() });
        await reload(); await context.syncEpisode(); context.report("所选依赖复核已记录。");
      }, !unresolved.length);
    }
    const history = node("details", null, content);
    node("summary", `内容历史（${production.history?.length || 0} 个版本）`, history);
    for (const item of production.history || []) node("p", `v${item.revision} · ${item.snapshot?.title || "镜头"} · ${item.id}${item.approved ? " · 已批准" : ""}`, history, "muted");
  }
  async function uploadImage(file) {
    if (!file || !file.size || file.size > 8 * 1024 * 1024) throw new Error("请选择不超过 8 MiB 的非空图片。");
    const key = requestKey();
    let response;
    try {
      response = await fetch(`/dgx-drama/projects/${segment(state.projectId)}/images`, {
        method: "POST", headers: { "Content-Type": "application/octet-stream", "X-DGX-Drama-Action": "explicit-user-action",
          "X-Request-Key": key, "X-Filename": encodeURIComponent(file.name) }, body: file,
      });
    } catch (error) { throw new Error(`上传结果尚未确认，请刷新素材核查后再操作。请求键 ${key} · ${error.message}`); }
    const result = await response.json();
    if (!response.ok || result.error) throw new Error(result.error?.message || "图片未能通过归档检查。");
    return result.asset;
  }
  function renderImages() {
    node("p", "图片用于人物、场景和镜头参考。上传后须经完整解码检查，选择参考与批准版本分别操作。", content, "muted");
    const controls = node("div", null, content, "row");
    const source = select(controls, "固定图片素材库", [["", "选择源图片"], ...state.library.map(item => [item.sourceId, item.title])], "", { dirty: false });
    button(controls, "导入图片", async () => {
      if (!source.value) throw new Error("请选择源图片。");
      await post(`${path("projects", state.projectId)}/image-imports`, { sourceId: source.value });
      await reload(); await context.syncAssets(); context.report("图片已检查并归档。");
    }, !state.library.length);
    const file = input(controls, "上传图片（PNG / JPEG / WebP，最大 8 MiB）", "", "file", false);
    file.accept = "image/png,image/jpeg,image/webp";
    button(controls, "上传图片", async () => {
      await uploadImage(file.files?.[0]); await reload(); await context.syncAssets(); context.report("图片已完整校验并加入项目。");
    });
    const grid = node("div", null, content, "asset-grid");
    for (const asset of images()) {
      const card = node("div", null, grid, "card"); node("strong", nameOf(asset), card);
      image(card, asset.id, nameOf(asset)); node("p", `${asset.width || "?"} × ${asset.height || "?"} · ${asset.id}`, card, "muted");
    }
    if (!images().length) node("p", "尚无图片，可导入现有参考或上传图片。", content, "empty");
  }
  let jobsContainer = null;
  const jobViews = new Map();
  function renderJobs() {
    const summary = node("div", null, content, "image-feedback");
    summary.hidden = true;
    const summaryText = node("p", null, summary);
    summaryText.setAttribute("role", "status"); summaryText.setAttribute("aria-live", "polite");
    const jump = node("button", "查看任务与图片", summary); jump.type = "button";
    jump.onclick = () => {
      const card = jobViews.get(imageFeedbackView?.jobId)?.card;
      (card || jobsContainer)?.scrollIntoView({ block: "start", behavior: "instant" });
      card?.focus({ preventScroll: true });
    };
    imageFeedbackView = { summary, summaryText, jump, jobId: state.jobs[0]?.id || null };
    node("p", "任务固定绑定提交时的内容修订。候选逐张归档，由你选择；新图片不会自动替换或批准主参考。", content, "muted");
    node("p", "镜头编辑与合成必须包含所有固定实体版本的规范主参考；已有批准的镜头参考画面可替代这些输入。若参考数量超过工具输入上限，请先合成并批准关键帧，再基于该关键帧编辑。", content, "muted");
    const form = node("div", null, content, "card");
    const targets = [
      ...state.entities.map(entity => [`entity:${entity.currentRevisionId}`, `${KINDS[entity.kind]} · ${nameOf(currentOf(entity))} · v${currentOf(entity)?.revision}`]),
      ...state.shots.filter(shot => shot.currentRevisionId).map(shot => [`shot:${shot.currentRevisionId}`, `镜头 · ${shot.title}`]),
    ];
    const target = select(form, "制作目标（固定当前内容版本）", [["", "选择实体或镜头"], ...targets], "", { dirty: false });
    const roleBox = node("div", null, form);
    let roleSelect = null;
    function renderRole() {
      roleBox.replaceChildren();
      const [kind, rid] = (target.value || "").split(":");
      const entity = kind === "entity" ? state.entities.find(row => row.currentRevisionId === rid) : null;
      roleSelect = entity ? select(roleBox, "参考用途", ROLES[entity.kind], ROLES[entity.kind][0][0], { dirty: false }) : null;
    }
    target.addEventListener("change", renderRole);
    renderRole();
    const tool = select(form, "图片工具", [["generate_image", "Z-Image 生图"], ["edit_image", "Qwen 参考图编辑"], ["compose_image_layers", "图片分层合成"]], "generate_image", { dirty: false });
    const parameters = node("div", null, form, "drama-form-grid");
    let collect;
    function renderParameters() {
      parameters.replaceChildren();
      if (tool.value === "generate_image") {
        const model = select(parameters, "生图模型", [["z-image-turbo", "Z-Image Turbo"], ["llada-image-turbo", "LLaDA Image Turbo · 4 步"], ["flux2-dev", "FLUX.2 dev · 标准"], ["flux2-dev-abliterated", "FLUX.2 dev · 去限制编码器"]], "z-image-turbo", { dirty: false });
        const prompt = input(parameters, "画面提示词", "", "textarea", false);
        const width = input(parameters, "宽度（像素）", 1024, "number", false);
        const height = input(parameters, "高度（像素）", 1024, "number", false);
        const seed = input(parameters, "种子", 42, "number", false);
        const count = input(parameters, "候选数量", 1, "number", false);
        const parallel = input(parameters, "每批并行数量", 2, "number", false);
        node("p", "总候选分批制作；请求上限不代表已观测并行，实际执行见任务。种子按原候选序号递增。", parameters, "muted");
        collect = () => {
          if (!prompt.value.trim()) throw new Error("请填写画面提示词。");
          return { prompt: prompt.value, ...(model.value === "z-image-turbo" ? {} : { model: model.value }), width: positiveInt(width, "宽度"), height: positiveInt(height, "高度"), seed: positiveInt(seed, "种子", 0), ...imageBatchParameters(count.value, parallel.value) };
        };
      } else if (tool.value === "edit_image") {
        const prompt = input(parameters, "编辑要求", "", "textarea", false);
        const refs = [1, 2, 3].map(index => select(parameters, `输入图 ${index}${index === 1 ? "（必选）" : "（可选）"}`, pictureChoices(), "", { dirty: false }));
        const seed = input(parameters, "种子", 42, "number", false);
        const steps = input(parameters, "采样步数（20–40）", 28, "number", false); steps.min = "20"; steps.max = "40";
        const count = input(parameters, "候选数量", 1, "number", false); count.min = "1";
        const parallel = input(parameters, "每批并行数量", 2, "number", false);
        node("p", "候选分批制作，实际并行以任务观测为准；种子按原候选序号递增。", parameters, "muted");
        const encoder = select(parameters, "文本编码器", [["standard", "标准"], ["huihui-abliterated-bf16", "Huihui BF16"]], "standard", { dirty: false });
        const precision = select(parameters, "模型精度", [["fp8", "FP8"], ["bf16", "BF16"]], "fp8", { dirty: false });
        collect = () => {
          if (!prompt.value.trim() || !refs[0].value) throw new Error("请填写编辑要求并选择第一张输入图。");
          const stepValue = positiveInt(steps, "采样步数", 20), batch = positiveInt(count, "候选数量");
          if (stepValue > 40) throw new Error("采样步数须为 20–40。");
          return { prompt: prompt.value, asset_ids: refs.map(ref => ref.value).filter(Boolean), seed: positiveInt(seed, "种子", 0),
            steps: stepValue, ...imageBatchParameters(batch, parallel.value), encoder: encoder.value, model_precision: precision.value };
        };
      } else {
        node("p", "确定性单张结果，不生成随机多候选。合成图层必须是已抠图、含真实透明区域的 PNG / WebP；普通不透明图片不能直接作为图层。当前短剧面板不执行抠图，请先在原 DGX 创作面板使用 prepare_image_cutout，或导入已有透明抠图素材。背景图片可以不透明。", parameters, "muted");
        const background = select(parameters, "背景图片", pictureChoices(), "", { dirty: false });
        const layersHost = node("div", null, parameters, "drama-layer-list");
        const rows = [];
        function addLayer() {
          if (rows.length >= 16) throw new Error("最多添加 16 个图层。");
          const card = node("div", null, layersHost, "card drama-form-grid");
          const row = { card };
          row.name = input(card, "图层名称", `图层 ${rows.length + 1}`, "text", false);
          row.asset = select(card, "图层图片", pictureChoices(), "", { dirty: false });
          for (const [key, label, value] of [["x", "水平偏移（可为负）", 0], ["y", "垂直偏移（可为负）", 0], ["width", "宽度", 512], ["height", "高度", 512], ["opacity", "不透明度（1–100%）", 100]]) row[key] = input(card, label, value, "number", false);
          for (const key of ["x", "y"]) { row[key].min = "-4096"; row[key].max = "4096"; row[key].step = "1"; }
          for (const key of ["width", "height"]) { row[key].min = "1"; row[key].max = "4096"; row[key].step = "1"; }
          row.opacity.step = "1"; row.opacity.min = "1"; row.opacity.max = "100";
          row.flip = select(card, "水平翻转", [["no", "不翻转"], ["yes", "翻转"]], "no", { dirty: false });
          button(card, "删除图层", () => { rows.splice(rows.indexOf(row), 1); card.remove(); });
          rows.push(row);
        }
        button(parameters, "增加图层", addLayer); addLayer();
        collect = () => compositionParameters(background.value, rows.map(row => ({
          name: row.name.value, asset_id: row.asset.value, x: row.x.value, y: row.y.value,
          width: row.width.value, height: row.height.value, opacity: row.opacity.value, flip_x: row.flip.value === "yes",
        })));
      }
    }
    tool.onchange = renderParameters; renderParameters();
    if (context.capabilities.referenceGenerationEnabled === false) node("p", "图片制作适配器尚未启用；仍可导入图片或已有任务。", form, "muted");
    const submitRow = node("div", null, form, "image-submit-feedback");
    const receipt = node("p", "填写完成后提交；进度和结果会显示在这里。", submitRow);
    receipt.setAttribute("role", "status"); receipt.setAttribute("aria-live", "polite");
    const feedbackView = imageFeedbackView;
    function showFeedback(value) {
      // Do not let polling of an older job overwrite a new validation error or receipt wait.
      if (!value.job) feedbackView.jobId = null;
      feedbackView.summary.hidden = false;
      feedbackView.summary.dataset.tone = value.tone;
      feedbackView.summaryText.textContent = value.text;
      feedbackView.summaryText.setAttribute("role", value.tone === "error" ? "alert" : "status");
      receipt.textContent = value.text; receipt.dataset.tone = value.tone;
      receipt.setAttribute("role", value.tone === "error" ? "alert" : "status");
      feedbackView.submitting = Boolean(value.submitting); feedbackView.uncertain = Boolean(value.uncertain);
      submitButton.textContent = value.submitting ? "正在提交…" : value.uncertain ? "结果待确认 · 请核查原任务" : "提交图片制作";
      submitButton.dataset.unavailable = String(value.submitting || value.uncertain || !targets.length || context.capabilities.referenceGenerationEnabled === false);
      submitButton.disabled = submitButton.dataset.unavailable === "true" || context.isBusy();
      if (value.job) feedbackView.jobId = value.job.id;
      jump.hidden = !feedbackView.jobId;
      submitRow.setAttribute("aria-busy", String(Boolean(value.submitting)));
    }
    feedbackView.show = showFeedback;
    const submit = createImageSubmissionFeedback({
      send: payload => post(payload.path, payload.body),
      show: showFeedback,
      accepted: job => {
        state.jobs = [job, ...state.jobs.filter(item => item.id !== job.id)];
        upsertJob(job); context.report("图片任务已接收。进度显示在提交按钮旁，也可点击“查看任务与图片”。"); schedule();
      },
    });
    const submitButton = button(submitRow, "提交图片制作", async () => {
      let payload;
      try {
        if (!target.value) throw new Error("请选择制作目标。");
        const [type, rid] = target.value.split(":");
        payload = { path: `${path(type === "entity" ? "entity-revisions" : "shot-revisions", rid)}/reference-jobs`, body: { tool: tool.value, parameters: collect(), ...(roleSelect?.value ? { referenceRole: roleSelect.value } : {}) } };
      } catch (error) { showFeedback({ tone: "error", text: error.message }); context.report(error.message, true); return; }
      await submit(payload);
    }, !targets.length || context.capabilities.referenceGenerationEnabled === false, true);
    const importing = node("details", null, content);
    node("summary", "导入已有 Gateway 图片任务", importing);
    node("p", "使用上方选定的制作目标和工具。导入只查询已有任务，不重新生成。", importing, "muted");
    const gatewayId = input(importing, "已有 Gateway job_id", "", "text", false);
    button(importing, "导入并核查已有任务", async () => {
      if (!target.value || !gatewayId.value.trim()) throw new Error("请选择制作目标并填写 Gateway 任务编号。");
      const [targetType, targetRevisionId] = target.value.split(":");
      const result = await post(`${path("projects", state.projectId)}/reference-imports`, { targetType, targetRevisionId, gatewayJobId: gatewayId.value.trim(), tool: tool.value });
      if (result.job) { state.jobs = [result.job, ...state.jobs.filter(job => job.id !== result.job.id)]; upsertJob(result.job); }
      context.report("已登记原 Gateway 任务，后续只查询和归档原结果。"); schedule();
    });
    jobsContainer = node("div", null, content, "drama-jobs"); jobViews.clear();
    for (const job of state.jobs) upsertJob(job);
    if (!state.jobs.length) node("p", "暂无图片任务。", jobsContainer, "empty");
  }
  async function useCandidate(job, candidate) {
    if (!confirmDiscard()) return;
    let result;
    if (job.targetType === "entity") {
      const entity = (await get(path("entities", job.targetId))).entity;
      const current = currentOf(entity);
      const stale = candidate.stale || job.targetRevisionId !== entity.currentRevisionId;
      if (stale && !window.confirm("此候选来自实体旧版本。确认将它作为当前实体的新参考草稿？")) return;
      const role = job.referenceRole || current.referenceRole || ROLES[entity.kind][0][0];
      const references = [...(current.references || []).filter(ref => ref.role !== role), { role, assetId: candidate.assetId }];
      result = await post(`${path("entities", entity.id)}/revisions`, { expectedRevision: entity.revision,
        name: current.name, description: current.description, references, referenceRole: role, referenceAssetId: candidate.assetId,
        candidateId: candidate.id, acceptStaleCandidate: Boolean(stale) });
      state.entityId = result.entity.id; state.entityRevisionId = null; state.kind = entity.kind; state.tab = "entities";
      context.report("候选已保存为实体新参考草稿。请检查后单独批准版本。");
    } else if (job.targetType === "shot") {
      const production = await get(`${path("shots", job.targetId)}/production`);
      const stale = candidate.stale || job.targetRevisionId !== production.currentRevisionId;
      if (stale && !window.confirm("此候选来自镜头旧版本。确认将它批准为当前镜头的新参考版本？")) return;
      await post(`${path("shots", job.targetId)}/reference-selection`, { expectedRevision: production.revision,
        assetId: candidate.assetId, candidateId: candidate.id, acceptStaleCandidate: Boolean(stale) });
      state.shotId = job.targetId; state.tab = "shots"; context.report("候选参考画面已批准并创建新镜头修订。");
    } else throw new Error("任务目标类型不受支持，不能选择该候选。");
    state.dirty = false; await reload(); await context.syncAssets(); await context.syncEpisode();
    await context.navigate?.(state.tab === "shots" ? "shots" : "prepare", state.tab);
  }
  function openImageGallery(job, selected) {
    const previous = document.activeElement;
    const dialog = node("dialog", null, document.body);
    dialog.style.cssText = "width:min(94vw,1400px);max-height:90vh;overflow:auto;background:#202329;color:#f1f3f6;border:1px solid #626c7e;border-radius:12px;padding:24px";
    dialog.setAttribute("aria-label", selected.length > 1 ? "候选图片对比" : "查看候选大图");
    node("h2", selected.length > 1 ? "候选图片对比" : "查看候选大图", dialog);
    node("p", "这是只读预览。关闭后可确认原候选，不会自动批准或替换参考。", dialog);
    const grid = node("div", null, dialog); grid.style.cssText = `display:grid;grid-template-columns:repeat(${selected.length > 1 ? 2 : 1},minmax(0,1fr));gap:16px`;
    for (const candidate of selected) {
      const figure = node("figure", null, grid); const img = node("img", null, figure);
      img.src = mediaUrl(state.projectId, candidate.assetId); img.alt = `候选 ${candidate.index == null ? candidate.id : candidate.index + 1} 大图`; img.loading = "lazy"; img.style.cssText = "width:100%;max-height:70vh;object-fit:contain";
      node("figcaption", `候选 ${candidate.index == null ? candidate.id : candidate.index + 1} · 种子 ${(candidate.seedText ?? candidate.seed) == null ? "历史未记录" : String(candidate.seedText ?? candidate.seed)}`, figure);
    }
    const close = node("button", "关闭图片查看", dialog); close.type = "button"; close.onclick = () => dialog.close();
    dialog.onclose = () => { dialog.remove(); previous?.focus(); };
    dialog.showModal(); close.focus();
  }
  function upsertJob(job) {
    if (!jobsContainer || state.tab !== "jobs") return;
    jobsContainer.querySelector(".empty")?.remove();
    let view = jobViews.get(job.id);
    if (!view) {
      const card = node("div", null, jobsContainer, "card"); card.tabIndex = -1;
      node("strong", `${({ generate_image: "生成图片", edit_image: "参考图编辑", compose_image_layers: "分层合成" })[job.tool] || job.tool}`, card);
      const record = disclosure(card, "任务编号与版本", job.id);
      const status = node("p", null, card); const detail = node("p", null, record, "muted");
      const actions = node("div", null, card, "row");
      const pictures = node("div", null, card, "asset-grid");
      view = { card, status, detail, error: node("p", null, card, "drama-task-error"), batch: node("p", null, card, "muted"), batchCandidates: node("p", null, card, "muted"), retries: node("div", null, card), actions, pictures, candidates: new Map(), compared: new Set(), retrySelected: new Set(), job };
      view.compare = node("button", "对比已选图片（0/4）", actions); view.compare.type = "button"; view.compare.disabled = true;
      view.compare.onclick = () => openImageGallery(view.job, (view.job.candidates || []).filter(c => view.compared.has(c.id)));
      jobViews.set(job.id, view);
      button(actions, "核查原任务并归档", async () => {
        const result = await post(`${path("reference-jobs", job.id)}/refresh`);
        if (result.job) { state.jobs = state.jobs.map(item => item.id === job.id ? result.job : item); upsertJob(result.job); }
        context.report("已核查原任务，未重新提交图片生成。");
      });
      view.cancel = button(actions, "取消原任务", async () => {
        const result = await post(`${path("reference-jobs", job.id)}/cancel`);
        if (result.job) upsertJob(result.job);
        context.report("取消意图已提交。请继续核查原任务直至终态。");
      });
    }
    view.job = job;
    if (imageFeedbackView?.jobId === job.id && !imageFeedbackView.submitting && !imageFeedbackView.uncertain) {
      imageFeedbackView.show({ ...imageJobFeedback(job), job });
    }
    view.status.textContent = TASK_STATES[job.state] || job.state;
    view.error.textContent = typeof job.error === "string" ? job.error : job.error?.message || "";
    view.detail.textContent = [`目标 ${job.targetType} · ${job.targetId}`, `固定修订 ${job.targetRevisionId}`,
      job.gatewayJobId ? `Gateway ${job.gatewayJobId}` : "尚无 Gateway 回执", typeof job.error === "string" ? job.error : job.error?.message].filter(Boolean).join(" · ");
    const terminal = ["SUCCEEDED", "FAILED", "CANCELED", "INTERRUPTED"].includes(job.state);
    view.cancel.disabled = terminal || context.isBusy(); view.cancel.dataset.unavailable = String(terminal);
    const batch = job.imageBatch || job.inputSnapshot?.gatewayStatus?.imageBatch;
    const plannedParameters = job.parameters || job.inputSnapshot?.parameters || {}, plannedCount = Number(plannedParameters.batch_count), plannedLimit = Number(plannedParameters.parallelism);
    const pendingBatchMessage = Number.isInteger(plannedCount) && plannedCount > 0 && Number.isInteger(plannedLimit) && plannedLimit > 0 ? `计划制作${plannedCount}张，每批上限${Math.min(plannedCount, plannedLimit)}张；等待执行后显示实测并行信息` : "等待执行后显示实测并行信息";
    view.batch.textContent = job.tool === "compose_image_layers" ? "确定性合成 · 单张结果" : batch ? `候选总数 ${batch.candidateCount} · 请求每批上限 ${batch.parallelLimit} 张 · ${batch.observedParallelism > 0 ? `已观测有效并行 ${batch.observedParallelism} 张` : "实际并行尚未观测"}` : terminal ? "历史任务未记录实际并行信息" : pendingBatchMessage;
    view.batchCandidates.textContent = (batch?.candidates || []).map(c => `候选 ${c.index + 1} · 种子 ${(c.seedText ?? c.seed) == null ? "未记录" : String(c.seedText ?? c.seed)} · ${TASK_STATES[c.state] || c.state}`).join("；");
    view.retries.replaceChildren();
    const retryKey = `dgx.drama.legacy.image-retry:${location.search}:${state.projectId}:${job.id}`;
    let retryIntent = null;
    try {
      const saved = JSON.parse(sessionStorage.getItem(retryKey) || "null");
      if (saved && typeof saved.key === "string" && saved.key.startsWith("drama-ui-") && Array.isArray(saved.indices) && saved.indices.length > 0 && saved.indices.every(i => Number.isInteger(i) && i >= 0)) retryIntent = saved;
    } catch { /* No safe saved intent. */ }
    const localCompleted = [...(Array.isArray(retryIntent?.history) ? retryIntent.history : []), ...(retryIntent?.jobId ? [retryIntent] : [])].filter(attempt => typeof attempt.jobId === "string" && Array.isArray(attempt.indices));
    const mergedRetries = new Map(localCompleted.map(attempt => [attempt.jobId, attempt]));
    for (const child of state.jobs.filter(row => (row.retryOf || row.inputSnapshot?.retryOf) === job.id)) {
      const indices = child.parameters?.candidate_indices || child.inputSnapshot?.retryIndices;
      if (Array.isArray(indices) && indices.length) mergedRetries.set(child.id, { jobId: child.id, indices });
    }
    const completed = [...mergedRetries.values()];
    const completedIndices = new Set(completed.flatMap(attempt => attempt.indices));
    const retryPending = !!retryIntent && !retryIntent.jobId;
    if (terminal && job.memoryReleased === true && !job.cleanupRequired && (batch?.candidates || []).some(candidate => candidate.state === "UNKNOWN" && candidate.error === "Saved image download requires recovery")) {
      node("p", "图片已生成，但下载归档未完成。仅重新读取已保存图片，不重新生成。", view.retries);
      button(view.retries, "恢复已生成图片", async () => {
        const result = await post(`${path("reference-jobs", job.id)}/recover`, {});
        if (!result.job?.id) throw new Error("恢复回执缺少任务编号，请核查原任务；不会重新生成图片。");
        state.jobs = state.jobs.map(row => row.id === result.job.id ? result.job : row); upsertJob(result.job);
        context.report("已核查已生成图片的归档状态，不会重新生成。");
      });
    }
    const retryable = terminal && job.memoryReleased === true && !job.cleanupRequired ? (batch?.candidates || []).filter(c => Number.isInteger(c.index) && !completedIndices.has(c.index) && ["FAILED", "CANCELED"].includes(c.state)) : [];
    if (retryable.length || retryIntent || completed.length) {
      node("p", "仅重拍明确失败或取消候选；成功和结果未知不会重拍。", view.retries);
      for (const candidate of retryable) {
        const label = node("label", `候选 ${candidate.index + 1} `, view.retries);
        const checkbox = node("input", null, label); checkbox.type = "checkbox"; checkbox.setAttribute("aria-label", `重拍候选 ${candidate.index + 1}`);
        checkbox.checked = retryPending ? retryIntent.indices.includes(candidate.index) : view.retrySelected.has(candidate.index); checkbox.disabled = retryPending;
        checkbox.onchange = () => checkbox.checked ? view.retrySelected.add(candidate.index) : view.retrySelected.delete(candidate.index);
      }
      for (const attempt of completed) {
        node("p", `已创建重拍任务 ${attempt.jobId}，请在任务列表查看。`, view.retries);
        button(view.retries, `查看重拍任务 · 候选 ${attempt.indices.map(index => index + 1).join("、")}`, async () => {
          if (!jobViews.has(attempt.jobId)) { const result = await get(path("reference-jobs", attempt.jobId)); if (result.job) upsertJob(result.job); }
          const target = jobViews.get(attempt.jobId)?.card; target?.scrollIntoView?.({ block: "center" }); target?.focus();
        });
      }
      if (retryable.length || retryPending) button(view.retries, retryPending ? "按原请求键核查重拍结果" : "重拍所选失败候选", async () => {
        let intent = retryIntent;
        if (!intent || intent.jobId) {
          const allowed = new Set(retryable.map(c => c.index)), indices = [...view.retrySelected];
          if (!indices.length || indices.some(i => !allowed.has(i))) throw new Error("请选择已确认失败或取消的候选。");
          intent = { indices, key: requestKey(), history: completed.map(attempt => ({ key: attempt.key, indices: attempt.indices, jobId: attempt.jobId })) };
          sessionStorage.setItem(retryKey, JSON.stringify(intent));
          retryIntent = intent;
        }
        try {
          const result = await post(`${path("reference-jobs", job.id)}/retry`, { candidateIndices: intent.indices }, intent.key);
          if (!result.job?.id) throw new Error("重拍回执不明，请按原键核查");
          sessionStorage.removeItem(retryKey);
          result.job = { ...result.job, retryOf: result.job.retryOf || job.id, parameters: { ...result.job.parameters, candidate_indices: result.job.parameters?.candidate_indices || intent.indices } };
          view.retrySelected.clear();
          state.jobs = [result.job, ...state.jobs.filter(j => j.id !== result.job.id)]; upsertJob(result.job);
          context.report("已创建重拍任务，成功候选仍保留。");
        } catch (error) {
          if (error.status >= 400 && error.status < 500 && ![408, 429].includes(error.status)) {
            const previous = completed.at(-1);
            if (previous) sessionStorage.setItem(retryKey, JSON.stringify({ ...previous, history: completed.slice(0, -1) })); else sessionStorage.removeItem(retryKey);
          }
          throw error;
        } finally { upsertJob(view.job); }
      });
    }
    for (const candidate of job.candidates || []) {
      let candidateView = view.candidates.get(candidate.id);
      if (!candidateView) {
        const card = node("div", null, view.pictures, "card");
        image(card, candidate.assetId, "图片候选");
        const zoom = node("button", `放大候选 ${candidate.index == null ? (job.candidates || []).indexOf(candidate) + 1 : candidate.index + 1}`, card); zoom.type = "button"; zoom.onclick = () => openImageGallery(view.job, [candidateView.candidate]);
        const comparison = node("label", "加入对比（最多4张）", card), check = node("input", null, comparison); check.type = "checkbox"; check.setAttribute("aria-label", `对比候选 ${candidate.index == null ? (job.candidates || []).indexOf(candidate) + 1 : candidate.index + 1}`);
        check.onchange = () => { if (check.checked && view.compared.size >= 4) { check.checked = false; context.report("最多选择4张对比。"); return; } if (check.checked) view.compared.add(candidate.id); else view.compared.delete(candidate.id); view.compare.textContent = `对比已选图片（${view.compared.size}/4）`; view.compare.disabled = view.compared.size < 2; };
        const label = node("p", null, card, "muted");
        candidateView = { label, candidate };
        view.candidates.set(candidate.id, candidateView);
        button(card, job.targetType === "entity" ? "选为实体参考草稿" : "批准为镜头参考", () => useCandidate(view.job, candidateView.candidate));
      }
      candidateView.candidate = candidate;
      candidateView.label.textContent = `候选 ${candidate.index == null ? (job.candidates || []).indexOf(candidate) + 1 : candidate.index + 1} · 种子 ${(candidate.seedText ?? candidate.seed) == null ? "历史未记录" : String(candidate.seedText ?? candidate.seed)} · ${TASK_STATES[candidate.state] || candidate.state || "已归档"}${candidate.stale ? " · 来自旧版本，选择时需确认" : " · 待选择"}`;
    }
  }
  let timer = null;
  let polling = false;
  function schedule() {
    clearTimeout(timer);
    if (context.isVisible()) timer = setTimeout(poll, 5000);
  }
  async function poll() {
    if (!context.isVisible()) return;
    if (context.isBusy() || polling || state.tab !== "jobs") { schedule(); return; }
    const epoch = state.epoch; polling = true;
    try {
      const result = await get(`${path("projects", state.projectId)}/reference-jobs`);
      if (epoch !== state.epoch) return;
      state.jobs = result.jobs || [];
      for (const job of state.jobs) upsertJob(job);
    } catch (error) { context.report(`图片任务查询暂不可用：${error.message}`, true); }
    finally { polling = false; schedule(); }
  }
  window.addEventListener("dgx-drama-shot-saved", event => {
    if (state.shots.some(shot => shot.id === event.detail?.shotId)) {
      if (state.dirty) { context.report("画布已保存新修订；制作表单有未保存修改，请刷新前先核查。", true); return; }
      reload().then(() => context.syncEpisode()).catch(error => context.report(error.message, true));
    }
  });
  return {
    activate, activeTab: () => state.tab,
    mount(parent) {
      const current = context.getContext();
      const key = `${current.project?.id || ""}/${current.episode?.id || ""}`;
      parent.append(root);
      if (state.key !== key) {
        state.key = key; state.projectId = current.project?.id; state.episodeId = current.episode?.id;
        state.entities = []; state.assets = []; state.shots = []; state.jobs = []; state.library = [];
        state.entityId = null; state.entityRevisionId = null; state.shotId = null; state.production = null; state.dirty = false;
        root.replaceChildren(); node("p", "正在加载制作版本…", root, "muted");
        reload().catch(error => { if (state.key === key) { root.replaceChildren(); node("p", `制作数据加载失败：${error.message}`, root, "empty"); } });
      }
      schedule();
    },
    hasUnsaved: () => state.dirty,
    refresh: async () => { state.dirty = false; await reload(); },
    resume: schedule,
    pause: () => clearTimeout(timer),
  };
}
