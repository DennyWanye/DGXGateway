// Presentation only: navigation never dispatches a production task.
export const WORKFLOW_STEPS = [
  { id: "overview", number: "◈", title: "制作概览", hint: "进度与下一步", description: "先看当前进度，再继续你要做的事。" },
  { id: "prepare", number: "1", title: "准备资料", hint: "人物 · 场景 · 参考素材", description: "准备人物与场景资料，上传或制作参考图片。已有视频也可以直接导入。" },
  { id: "shots", number: "2", title: "制作镜头", hint: "分镜 · 画面 · 动作", description: "把故事拆成镜头，写清动作和画面，选好并批准参考图。" },
  { id: "video", number: "3", title: "生成与审片", hint: "拍摄 · 看片 · 采用", description: "选择镜头生成视频，看片后先批准，再采用满意的版本。" },
  { id: "edit", number: "4", title: "剪辑导出", hint: "排列片段 · 导出成片", description: "把已采用的视频排成时间线，调整长度，保存后导出。" },
];
export function nextWorkflowAction({ episode, shots = [], timeline, exports = [] }) {
  if (!episode) return { screen: "overview", title: "先创建一个分集", detail: "在左侧展开“新建分集”，给这一集起个名字。" };
  if (!shots.length) return { screen: "shots", title: "创建第一个镜头", detail: "给镜头起名，描述动作和构图；也可以从已有视频建立镜头。" };
  const unselected = shots.filter(shot => !shot.selectedTakeId);
  if (unselected.some(shot => !(shot.takes || []).length)) return { screen: "shots", title: "继续准备镜头", detail: `${unselected.filter(shot => !(shot.takes || []).length).length} 个镜头还没有视频。先确认制作内容和参考图，再去生成。` };
  if (unselected.length) return { screen: "video", title: "查看候选并选片", detail: `${unselected.length} 个镜头还未采用视频。看片、记录意见，然后采用满意的版本。` };
  if (!timeline) return { screen: "edit", title: "把选片排成时间线", detail: "已有采用视频，可以按镜头顺序建立时间线，再调整每段长度。" };
  if (exports.some(item => item.state === "SUCCEEDED")) return { screen: "edit", title: "查看已导出的成片", detail: "成片已就绪，可以播放、下载，或继续修改时间线。" };
  return { screen: "edit", title: "检查时间线并导出", detail: "确认片段顺序和时长，保存时间线后导出成片。" };
}
export function showWorkflowPage(pages, buttons, selected) {
  if (!pages[selected]) return false;
  for (const [id, page] of Object.entries(pages)) {
    page.hidden = id !== selected;
    if (id !== selected) for (const media of page.querySelectorAll("video,audio")) media.pause();
  }
  for (const [id, button] of Object.entries(buttons)) {
    if (id === selected) button.setAttribute("aria-current", "step");
    else button.removeAttribute("aria-current");
  }
  return true;
}

export function formatTaskTime(value) {
  if (value == null || value === "") return "视频批次";
  const date = new Date(typeof value === "number" && value < 1e12 ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? "视频批次" : date.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function editableFrames(value, unit, fps) {
  if (String(value).trim() === "" || !Number.isFinite(Number(value))) return "";
  return unit === "seconds" ? Math.round(Number(value) * fps) : value;
}
