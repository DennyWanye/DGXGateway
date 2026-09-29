import { app } from "../../../scripts/app.js";
import { get, post, segment } from "./api.js";

const IDENTITY = ["shotId", "expectedRevision", "baseRevisionId", "templateVersion"];
const TEXT = ["title", "action", "framing", "camera", "prompt"];
const widget = (node, name) => node.widgets?.find(item => item.name === name);
const value = (node, name) => widget(node, name)?.value;
const set = (node, name, data) => { const item = widget(node, name); if (item) item.value = data; };

function applyProjection(node, projection) {
  for (const name of IDENTITY) set(node, name, projection[name]);
  for (const name of TEXT) set(node, name, projection.snapshot[name] || "");
  set(node, "targetEditFrames", projection.snapshot.targetEditFrames || 96);
  set(node, "snapshot_json", JSON.stringify(projection.snapshot, null, 2));
  node.updateDramaIdentity?.();
  node.setDirtyCanvas?.(true, true);
}

export async function openShotOnCanvas(shotId) {
  const projection = await get(`/shots/${segment(shotId)}/canvas`);
  if (!app.graph || !window.LiteGraph?.createNode) throw new Error("ComfyUI 画布尚未准备好，请稍后重试。");
  const node = window.LiteGraph.createNode("DGXDramaShotDraft");
  if (!node) throw new Error("短剧镜头节点尚未加载，请确认候选服务已注册 DGXDramaShotDraft 并刷新页面。");
  applyProjection(node, projection);
  node.title = `短剧 · ${projection.snapshot.title || shotId}`;
  // Append only: existing graphs and their unsaved changes are never replaced.
  const nodes = app.graph._nodes || [];
  node.pos = nodes.length ? [Math.max(...nodes.map(item => item.pos[0] + item.size[0])) + 60, nodes[0].pos[1]] : [80, 80];
  app.graph.add(node);
  node.setSize?.([470, 750]);
  app.canvas?.selectNode?.(node);
  app.canvas?.centerOnNode?.(node);
  app.graph.setDirtyCanvas?.(true, true);
  return node;
}

app.registerExtension({
  name: "dgx.drama.canvas",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== "DGXDramaShotDraft") return;
    const previous = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function (...args) {
      previous?.apply(this, args);
      const node = this;
      for (const name of [...IDENTITY, "snapshot_json"]) {
        const item = widget(node, name);
        if (item?.inputEl) item.inputEl.readOnly = true;
        if (item) item.disabled = true;
      }
      const statusPanel = document.createElement("div");
      statusPanel.style.cssText = "background:#18252b;color:#edf5f7;padding:10px;border:1px solid #68808d;border-radius:6px;white-space:pre-wrap;overflow-wrap:anywhere;overflow:auto;font:13px/1.5 system-ui;min-height:110px;";
      const identity = document.createElement("div");
      identity.style.cssText = "font-size:11px;color:#b7cbd4;margin-bottom:8px";
      const statusText = document.createElement("div");
      statusText.setAttribute("role", "status"); statusText.setAttribute("aria-live", "polite");
      statusPanel.append(identity, statusText);
      const status = node.addDOMWidget("回写状态", "drama-status", statusPanel, { serialize: false, hideOnZoom: false });
      status.serialize = false; status.options.serialize = false;
      status.computeSize = () => [0, 140];
      node.updateDramaIdentity = () => {
        identity.textContent = `镜头 ${value(node, "shotId") || "未绑定"}\n基础版本 ${value(node, "baseRevisionId") || "未绑定"} · 乐观锁 ${value(node, "expectedRevision") || "未绑定"}`;
      };
      node.updateDramaIdentity();
      const setStatus = (text, error = false) => { statusText.textContent = text; statusText.style.color = error ? "#ffb4b4" : "#d1efb1"; node.setDirtyCanvas?.(true, true); };
      setStatus("未回写；执行节点只输出草稿。");
      const previousConfigure = node.onConfigure;
      node.onConfigure = function (...configureArgs) { previousConfigure?.apply(this, configureArgs); node.updateDramaIdentity(); };
      const save = node.addWidget("button", "回写为新修订", null, async () => {
        if (save.disabled) return;
        save.disabled = true;
        try {
          const snapshot = JSON.parse(value(node, "snapshot_json"));
          if (!snapshot || Array.isArray(snapshot) || typeof snapshot !== "object") throw new Error("基础快照格式错误，请重新从工作台打开镜头。");
          for (const name of TEXT) snapshot[name] = String(value(node, name) ?? "");
          const frames = Number(value(node, "targetEditFrames"));
          if (!Number.isSafeInteger(frames) || frames < 1) throw new Error("剪辑长度必须为正整数帧。");
          snapshot.targetEditFrames = frames;
          const payload = Object.fromEntries(IDENTITY.map(name => [name, value(node, name)]));
          payload.expectedRevision = Number(payload.expectedRevision);
          if (!payload.shotId || !payload.baseRevisionId || !Number.isSafeInteger(payload.expectedRevision) || payload.expectedRevision < 1) throw new Error("缺少有效的镜头身份与基础版本，请从工作台重新打开镜头。");
          setStatus("正在回写原基础版本…");
          await post(`/shots/${segment(payload.shotId)}/canvas-revisions`, { ...payload, snapshot });
          setStatus("新修订已保存。此节点保留原基础版本；继续编辑请重新从工作台打开。");
          window.dispatchEvent(new CustomEvent("dgx-drama-shot-saved", { detail: { shotId: payload.shotId } }));
          // Retain the original projection: another click exercises CAS instead of silently rebasing.
        } catch (error) { setStatus(error.message, true); }
        finally { save.disabled = false; node.setDirtyCanvas?.(true, true); }
      }, { serialize: false });
    };
  },
});
