import { app } from "../../scripts/app.js";
import { openShotOnCanvas } from "./drama/canvas.js";

// This host owns only the frame and the explicit canvas bridge. Business writes
// stay in the workbench and continue through the existing same-origin API.
app.registerExtension({
  name: "dgx.drama.studio",
  async setup() {
    const params = new URLSearchParams(location.search);
    // The classic interface remains an explicit rollback entry.
    if (params.get("dramaUI") === "legacy") return;
    let capabilities;
    try { const response=await fetch('/dgx-drama/capabilities'); if(!response.ok)return;capabilities=await response.json(); } catch {return;}
    if (!capabilities.enabled || document.querySelector('[data-dgx-studio]')) return;
    const launch=document.createElement('button');launch.dataset.dgxStudio='launch';launch.textContent='短剧工作台 · 新版';
    launch.style.cssText='position:fixed;bottom:20px;right:24px;z-index:10004;padding:12px 20px;border-radius:12px;background:#c2bcff;color:#252044;border:1px solid #d4ceff;font:600 14px system-ui;cursor:pointer;box-shadow:0 4px 24px #0005';
    const frame=document.createElement('iframe');frame.dataset.dgxStudio='frame';frame.title='短剧工作台 · 新版';
    frame.style.cssText='position:fixed;inset:0;width:100%;height:100%;border:0;background:#101113;z-index:10005';frame.hidden=true;
    frame.src=new URL('./studio/index.html',import.meta.url).href;
    let lastFocus = null;
    const inertState = new Map();
    let overflow = "";
    let isOpen = false;
    const isolate = child => {
      if (child === frame) return;
      // Remember each node once, including nodes already inert before opening.
      if (!inertState.has(child)) inertState.set(child, child.inert);
      child.inert = true;
    };
    const observer = new MutationObserver(records => {
      if (!isOpen) return;
      for (const record of records) {
        for (const node of record.addedNodes) {
          if (node.nodeType === Node.ELEMENT_NODE && node.parentElement === document.body) isolate(node);
        }
      }
    });
    const show = () => {
      // Repeated launches must preserve the original focus and overflow state.
      if (isOpen) { frame.focus(); return; }
      isOpen = true;
      lastFocus = document.activeElement;
      overflow = document.body.style.overflow;
      document.body.style.overflow = 'hidden';
      for (const child of document.body.children) isolate(child);
      observer.observe(document.body, { childList: true });
      frame.hidden = false;
      frame.focus();
    };
    const hide = () => {
      if (!isOpen) return;
      isOpen = false;
      observer.disconnect();
      frame.contentWindow?.postMessage({ type: 'dgx-studio:hidden' }, location.origin);
      frame.hidden = true;
      for (const [child, value] of inertState) child.inert = value;
      inertState.clear();
      document.body.style.overflow = overflow;
      (lastFocus?.isConnected && lastFocus !== document.body ? lastFocus : launch).focus();
    };
    launch.addEventListener('click',show);
    let canvasBusy=false;
    window.addEventListener('message',async event=>{
      if(event.origin!==location.origin||event.source!==frame.contentWindow)return;
      if(event.data?.type==='dgx-studio:close'){hide();return;}
      if(event.data?.type!=='dgx-studio:canvas'||canvasBusy)return;
      const shotId=event.data.shotId;if(typeof shotId!=='string'||!/^[a-f0-9]{24,64}$/.test(shotId))return;
      canvasBusy=true;
      try{await openShotOnCanvas(shotId);frame.contentWindow?.postMessage({type:'dgx-studio:canvas-result'},location.origin);}
      catch(error){frame.contentWindow?.postMessage({type:'dgx-studio:canvas-result',error:error.message||'打开画布失败'},location.origin);}
      finally{canvasBusy=false;}
    });
    window.addEventListener('dgx-drama-shot-saved',event=>frame.contentWindow?.postMessage({type:'dgx-studio:shot-saved',shotId:event.detail?.shotId},location.origin));
    document.body.append(launch,frame);
    show();
  },
});
