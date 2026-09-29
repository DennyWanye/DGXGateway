import { app } from "../../scripts/app.js";
import { appendReference, batchSummary, candidateCaption, parseCreativeParameter, creativeImageURL } from "./creative-batch.js";
import { api } from "../../scripts/api.js";

const TERMINAL = new Set(["SUCCEEDED", "FAILED", "INTERRUPTED", "CANCELED"]);
const uuid = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, "0")).join("");
const STORE = "dgx.creative.jobs.v1";
const DRAFT = "dgx.creative.draft.v1";
const LABELS = {retry_image_candidates: "只重拍已失败图片候选", comfy_reconcile: "恢复原图片归档与资源", edit_image: "Qwen 2511 参考图编辑", generate_image: "Z-Image 生图", generate_video: "H3 单镜头", video_batch_submit: "H3 多镜头批次", comfy_cancel: "取消图片任务", video_cancel: "取消视频批次", comfy_submit: "自定义 Comfy 工作流", prepare_image_cutout: "抠图", compose_image_layers: "分层合成"};
const el = (tag, text, parent) => { const e = document.createElement(tag); if (text) e.textContent = text; parent?.append(e); return e; };
function readStore(key, fallback) { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } }
async function rpc(method, params = {}) {
  const response = await fetch("/dgx-creative/mcp", {method: "POST", headers: {"Content-Type": "application/json", "X-DGX-Creative-Action": "explicit-user-action"}, body: JSON.stringify({jsonrpc: "2.0", id: uuid(), method, params})});
  const value = await response.json();
  if (!response.ok || value.error || value.result?.isError) throw new Error(value.error?.message || value.error || value.result?.content?.[0]?.text || "请求失败；请查询原任务，勿重复提交");
  return value.result;
}
async function call(name, args) { return (await rpc("tools/call", {name, arguments: args})).structuredContent; }

app.registerExtension({name: "dgx.creative.manager",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== "DGXCreativeTool") return;
    const previous = nodeType.prototype.onExecuted;
    nodeType.prototype.onExecuted = function(message) {
      previous?.apply(this, arguments);
      try {
        const row = JSON.parse(message.text[0]);
        const kind = row.shots ? "video" : row.output_asset || row.kind ? "media" : "comfy";
        if (row.job_id) {
          const saved = readStore(STORE, []);
          if (!saved.some(j => j.id === row.job_id)) saved.unshift({id: row.job_id, kind});
          localStorage.setItem(STORE, JSON.stringify(saved.slice(0, 40)));
          window.dispatchEvent(new Event("dgx-creative-job"));
        }
      } catch { /* Non-job tools have no task to attach. */ }
    };
  }, async setup() {
  const style = el("style", null, document.head);
  style.textContent = `.dgx-creative{position:fixed;inset:7vh 6vw;z-index:10001;background:#202329;color:#f1f3f6;border:1px solid #626c7e;border-radius:12px;padding:22px;overflow:auto;font:14px/1.5 sans-serif;box-shadow:0 15px 70px #000a}.dgx-creative[hidden]{display:none}.dgx-creative button,.dgx-creative select,.dgx-creative input,.dgx-creative textarea{font:inherit;padding:7px;margin:4px;background:#303640;color:inherit;border:1px solid #697486;border-radius:5px}.dgx-creative label{display:block;margin:9px 0}.dgx-creative input:not([type=file]),.dgx-creative textarea{width:90%}.dgx-creative textarea{min-height:80px}.dgx-creative pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#15181d;padding:12px}.dgx-creative a{color:#90caff}.dgx-creative .job{border-top:1px solid #697486;padding:12px 0}.dgx-creative video,.dgx-creative img{max-width:480px;max-height:280px}.dgx-creative-launch{position:fixed;left:72px;bottom:16px;z-index:9999;padding:12px 18px;border-radius:9px;background:#385ea8;color:white;border:1px solid #a8c5ff;cursor:pointer}`;
  const launch = el("button", "DGX 创作 · 图片 / 视频", document.body); launch.className = "dgx-creative-launch";
  const panel = el("section", null, document.body); panel.className = "dgx-creative"; panel.hidden = true;
  panel.setAttribute("aria-label", "DGX 创作面板");
  const close = el("button", "关闭面板", panel); close.style.float = "right"; close.onclick = () => { panel.hidden = true; };
  el("h2", "DGX 创作", panel);
  el("p", "与 MCP 共用素材、任务编号和取消状态。请使用下方任务卡的取消按钮；关闭面板或取消 ComfyUI 原生队列不会取消 Gateway 任务。图片在采样检查点中断；H3 取消会等待当前镜头结束，跳过剩余镜头后释放。", panel);
  const error = el("p", "", panel); error.setAttribute("role", "status");
  const assets = el("div", null, panel);
  const fileLabel = el("label", "上传图片或参考音频（音频需 4 秒 / 24kHz / 单声道 PCM16 WAV）", assets);
  const file = el("input", null, fileLabel); file.type = "file"; file.accept = "image/png,image/jpeg,image/webp,audio/wav";
  const upload = el("button", "注册素材", assets);
  const assetResult = el("pre", "素材注册后会自动填入当前表单。", assets);
  const select = el("select", null, panel); select.setAttribute("aria-label", "创作工具");
  const fields = el("div", null, panel);
  const submit = el("button", "提交到 Gateway", panel);
  const fresh = el("button", "新建请求编号", panel);
  el("p", "同一请求编号不会重复生成。修改参数后点击「新建请求编号」；网络超时可保留原编号重试。", panel);
  el("p", "提交回执（最新状态见下方任务卡）", panel);
  const result = el("pre", "", panel);
  el("h3", "任务与产物", panel);
  const jobInput = el("input", null, panel); jobInput.placeholder = "粘贴已有 job_id，可接续 MCP 创建的任务";
  const jobKind = el("select", null, panel); for (const kind of ["comfy", "video", "media"]) { const o = el("option", kind, jobKind); o.value = kind; }
  const attach = el("button", "查询并加入列表", panel);
  const jobsView = el("div", null, panel);
  let tools = [], inputs = {}, jobs = readStore(STORE, []), polling = false, lastRows = "";
  const saveDraft = () => localStorage.setItem(DRAFT, JSON.stringify({tool: select.value, values: Object.fromEntries(Object.entries(inputs).map(([k,e]) => [k,e.value]))}));
  function renderFields(saved = {}) {
    fields.replaceChildren(); inputs = {};
    const spec = tools.find(t => t.name === select.value); if (!spec) return;
    el("p", spec.description, fields);
    for (const [name, type] of Object.entries(spec.inputSchema.properties)) {
      const label = el("label", ({candidate_indices:"原候选编号（失败项，从0开始）",batch_count:"输出候选总数（正整数）",parallelism:"每批并行数量（正整数，实际以任务记录为准）",asset_ids:"输入参考图（最多3张，与输出候选数无关）"}[name] || name) + (spec.inputSchema.required.includes(name) ? " *" : "（可选）"), fields);
      const input = el(["object", "array"].includes(type.type) || name === "prompt" ? "textarea" : "input", null, label);
      input.value = saved[name] ?? (name === "idempotency_key" ? "comfy-ui-" + uuid() : name === "seed" ? "42" : name === "steps" ? "20" : name === "batch_count" ? "1" : name === "parallelism" ? "2" : name === "name" ? "shot-01" : "");
      input.oninput = saveDraft; inputs[name] = input;
    }
    saveDraft();
  }
  function remember(row, kind) {
    if (!row?.job_id) return;
    if (!jobs.some(j => j.id === row.job_id && j.kind === kind)) jobs.unshift({id: row.job_id, kind});
    jobs = jobs.slice(0, 40); localStorage.setItem(STORE, JSON.stringify(jobs));
  }
  function showJob(row, entry) {
    const card = el("div", null, jobsView); card.className = "job";
    el("strong", `${entry.kind} · ${row.state} · ${entry.id}`, card);
    el("p", `取消请求：${row.cancel_requested ? "已收到" : "无"}；内存释放：${row.memory_released ? "已确认" : "未确认"}${row.cleanup_required ? "；需要恢复" : ""}`, card);
    if (row.cancel_status) el("p", row.cancel_status, card);
    if (row.error) el("p", row.error, card);
    if (!TERMINAL.has(row.state) && entry.kind !== "media") {
      const cancel = el("button", "取消此任务", card);
      cancel.disabled = !!row.cancel_requested;
      cancel.onclick = async () => { cancel.disabled = true; try { await call(entry.kind + "_cancel", {job_id: entry.id}); await refresh(); } catch (e) { error.textContent = e.message; cancel.disabled = false; } };
    }
    if (entry.kind === "comfy") {
      el("p", batchSummary(row), card);
      const detail = el("details", null, card); el("summary", "候选与完整任务记录", detail); el("pre", JSON.stringify(row, null, 2), detail);
    }
    if (row.shots) el("p", row.shots.map(s => `${s.name}: ${s.state}`).join(" · "), card);
    const artifacts = [...(row.artifacts || []), ...(row.shots || []).flatMap(s => s.artifact ? [s.artifact] : [])];
    for (const a of artifacts) {
      let url; try { url = new URL(a.url); } catch { continue; }
      if (!["http:", "https:"].includes(url.protocol)) continue;
      const mediaURL = a.kind === "images" ? creativeImageURL(a, row.job_id) : url.href;
      if (!mediaURL) continue;
      if (a.kind === "images") el("p", candidateCaption(a), card);
      const link = el("a", "打开 / 保存产物", card); link.href = mediaURL; link.target = "_blank"; link.rel = "noopener";
      const media = el(/\.mp4$/i.test(url.pathname) ? "video" : "img", null, card); media.src = mediaURL; if (media.tagName === "IMG") media.alt = candidateCaption(a); if (media.tagName === "VIDEO") media.controls = true;
    }
    if (row.output_asset) el("pre", JSON.stringify(row.output_asset, null, 2), card);
  }
  async function refresh() {
    if (polling || panel.hidden) return;
    polling = true;
    try {
      const rows = await Promise.all(jobs.map(async entry => ({entry, row: await call(entry.kind + "_job", {job_id: entry.id})})));
      const serialized = JSON.stringify(rows);
      if (serialized !== lastRows) { jobsView.replaceChildren(); rows.forEach(({row,entry}) => showJob(row,entry)); lastRows = serialized; }
    } catch (e) { error.textContent = e.message; } finally { polling = false; }
  }
  upload.onclick = async () => {
    if (!file.files[0]) { error.textContent = "请先选择素材文件"; return; }
    upload.disabled = true;
    try {
      const f = file.files[0], audio = /\.wav$/i.test(f.name);
      if (f.size > (audio ? 256 * 1024 : 8 * 1024 * 1024)) throw new Error("素材超过上传大小限制");
      const encoded = await new Promise((resolve, reject) => { const r = new FileReader(); r.onload = () => resolve(r.result.split(",")[1]); r.onerror = reject; r.readAsDataURL(f); });
      const a = await call(audio ? "video_register_audio" : "comfy_register_image", {filename: f.name, [audio ? "audio_base64" : "image_base64"]: encoded});
      assetResult.textContent = JSON.stringify(a, null, 2);
      const id = a.audio_asset_id || a.asset_id;
      if (audio && inputs.audio_asset_id) inputs.audio_asset_id.value = id;
      if (!audio) {
        for (const k of ["asset_id", "image_asset_id", "background_asset_id"]) if (inputs[k]) inputs[k].value = id;
        if (inputs.asset_ids) { let old; try { old = JSON.parse(inputs.asset_ids.value || "[]"); } catch { old = []; } inputs.asset_ids.value = JSON.stringify(appendReference(old, id)); }
      }
      saveDraft();
    } catch (e) { error.textContent = e.message; } finally { upload.disabled = false; }
  };
  submit.onclick = async () => {
    submit.disabled = true; error.textContent = "";
    try {
      const spec = tools.find(t => t.name === select.value), args = {};
      for (const [name,input] of Object.entries(inputs)) {
        if (!input.value.trim() && !spec.inputSchema.required.includes(name)) continue;
        const type = spec.inputSchema.properties[name].type;
        args[name] = parseCreativeParameter(type, input.value);
      }
      saveDraft();
      const row = await call(select.value, args); result.textContent = JSON.stringify(row, null, 2);
      const kind = /^(video_|generate_video)/.test(select.value) ? "video" : /^(media_job|prepare_image_cutout|compose_image_layers)/.test(select.value) ? "media" : "comfy";
      remember(row, kind); await refresh();
    } catch (e) { error.textContent = e.message; } finally { submit.disabled = false; }
  };
  fresh.onclick = () => { if (inputs.idempotency_key) { inputs.idempotency_key.value = "comfy-ui-" + uuid(); saveDraft(); } };
  select.onchange = () => renderFields();
  attach.onclick = async () => { try { const row = await call(jobKind.value + "_job", {job_id: jobInput.value.trim()}); remember(row, jobKind.value); await refresh(); } catch (e) { error.textContent = e.message; } };
  launch.onclick = async () => {
    panel.hidden = false;
    if (!tools.length) {
      try {
        tools = (await rpc("tools/list")).tools;
        tools.sort((a,b) => (a.name === "edit_image" ? -1 : b.name === "edit_image" ? 1 : a.name.localeCompare(b.name)));
        select.replaceChildren(); for (const t of tools) { const o = el("option", LABELS[t.name] || t.name, select); o.value = t.name; }
        const draft = readStore(DRAFT, {}); if (tools.some(t => t.name === draft.tool)) select.value = draft.tool;
        renderFields(draft.values); await refresh();
      } catch (e) { error.textContent = e.message; }
    } else await refresh();
  };
  api.addEventListener("dgx-creative-job", event => {
    const entry = event.detail;
    if (entry?.id && ["comfy", "video"].includes(entry.kind)) {
      remember({job_id: entry.id}, entry.kind); refresh();
    }
  });
  window.addEventListener("dgx-creative-job", () => { jobs = readStore(STORE, []); refresh(); });
  setInterval(refresh, 5000);
}});
