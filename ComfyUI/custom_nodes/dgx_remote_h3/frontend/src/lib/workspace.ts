/** Restored projects deliberately retain IDs; browser drafts must not cross roots. */
export function workspaceIds(): string[] {
  const value = new URLSearchParams(location.search).get("restored");
  if (!value) return [];
  const ids = value.split("/");
  // An invalid workspace URL must never silently target the original project.
  return ids.length <= 4 && ids.every((id) => /^[a-f0-9]{32}$/.test(id))
    ? ids
    : ["invalid"];
}
export const workspaceApiPrefix = () =>
  workspaceIds()
    .map((id) => `/restored/${id}`)
    .join("");
export function restoredWorkspaceUrl(id: string): string {
  if (!/^[a-f0-9]{32}$/.test(id)) throw new Error("恢复工作区编号无效");
  const ids = [...workspaceIds(), id];
  if (ids.length > 4 || ids.includes("invalid"))
    throw new Error("恢复工作区层级超过上限，请从主工作区导入项目包");
  return `${location.pathname}?restored=${encodeURIComponent(ids.join("/"))}`;
}
/** Return to the original root in this document, preserving unrelated UI options. */
export function originalWorkspaceUrl(): string {
  const url = new URL(location.href);
  url.searchParams.delete("restored");
  return `${url.pathname}${url.search}${url.hash}`;
}
export function returnToOriginalWorkspace(): void {
  location.assign(originalWorkspaceUrl());
}
function scopedStorage(kind: "sessionStorage" | "localStorage") {
  const keyFor = (key: string) => {
    const ids = workspaceIds();
    return ids.length ? `dgx.workspace:${ids.join("/")}:${key}` : key;
  };
  return {
    getItem: (key: string) => window[kind].getItem(keyFor(key)),
    setItem: (key: string, value: string) =>
      window[kind].setItem(keyFor(key), value),
    removeItem: (key: string) => window[kind].removeItem(keyFor(key)),
  };
}
export const sessionStore = scopedStorage("sessionStorage");
export const localStore = scopedStorage("localStorage");

/** Explicit discard removes editable drafts only, never durable request receipts. */
export function discardEditableDrafts(projectId: string, episodeId: string) {
  if (!projectId) return;
  const scope = `${projectId}/${episodeId}`;
  for (const prefix of ['dgx.drama.edit.draft:', 'dgx.drama.subtitle.draft:', 'dgx.drama.story.draft:', 'dgx.drama.video.drafts.v1:']) {
    sessionStore.removeItem(prefix + scope);
  }
  const lastTarget = sessionStore.getItem(`dgx-drama-image-last-target:${projectId}:${episodeId}`);
  if (lastTarget) sessionStore.removeItem(`dgx-drama-image-draft:${projectId}:${episodeId}:${lastTarget}`);
}
