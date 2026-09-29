// @vitest-environment jsdom
import { beforeEach, afterEach, expect, it } from "vitest";
import {
  sessionStore,
  localStore,
  workspaceApiPrefix,
  restoredWorkspaceUrl,
  discardEditableDrafts,
} from "./workspace";
const rid = "a".repeat(32),
  child = "b".repeat(32);
beforeEach(() => {
  history.replaceState({}, "", "/");
  sessionStorage.clear();
  localStorage.clear();
});
afterEach(() => history.replaceState({}, "", "/"));
it("isolates every draft and pending key for unchanged project IDs and preserves original values", () => {
  const keys = [
    "dgx.drama.backup.pending:p",
    "dgx.drama.edit.draft:p:e",
    "image:unknown:p",
    "video:pending:p:e",
    "story:draft:p:e",
    "story:pending:p:e",
    "review:pending:p:e",
    "asr:draft:p:e",
  ];
  for (const key of keys) sessionStore.setItem(key, "original");
  localStore.setItem("dgx.drama.last-selection.v1", "original-choice");
  history.replaceState({}, "", `/?restored=${rid}`);
  for (const key of keys) {
    expect(sessionStore.getItem(key)).toBeNull();
    sessionStore.setItem(key, "restored");
  }
  expect(localStore.getItem("dgx.drama.last-selection.v1")).toBeNull();
  localStore.setItem("dgx.drama.last-selection.v1", "restored-choice");
  sessionStore.removeItem(keys[0]);
  history.replaceState({}, "", "/");
  for (const key of keys) expect(sessionStore.getItem(key)).toBe("original");
  expect(localStore.getItem("dgx.drama.last-selection.v1")).toBe(
    "original-choice",
  );
});
it("opens a nested restoration in its complete parent namespace", () => {
  history.replaceState({}, "", `/?restored=${rid}`);
  const url = restoredWorkspaceUrl(child);
  history.replaceState({}, "", url);
  expect(workspaceApiPrefix()).toBe(`/restored/${rid}/restored/${child}`);
  expect(sessionStore.getItem("draft")).toBeNull();
});
it("invalid workspace identity never silently targets the original project", () => {
  history.replaceState({}, "", "/?restored=bad/../../");
  expect(workspaceApiPrefix()).toBe("/restored/invalid");
  expect(() => restoredWorkspaceUrl(child)).toThrow();
});
it('discards only editable drafts in the current namespace and keeps request receipts and other episodes', () => {
  const editable = ['dgx.drama.edit.draft:p/e', 'dgx.drama.subtitle.draft:p/e', 'dgx.drama.story.draft:p/e', 'dgx.drama.video.drafts.v1:p/e', 'dgx-drama-image-draft:p:e:shot:r'];
  const retained = ['dgx.drama.edit.draft:p/other', 'dgx.drama.edit.draft:other/e', 'dgx.drama.video.pending.v1:p:e', 'dgx.drama.story.pending:p/e', 'dgx.drama.p5.pending:p/e:timeline', 'dgx.drama.review.pending:p/e', 'dgx-drama-image-pending:p', 'dgx.drama.backup.pending:p'];
  for (const key of [...editable, ...retained]) sessionStore.setItem(key, 'original');
  history.replaceState({}, '', `/?restored=${rid}`);
  for (const key of [...editable, ...retained]) sessionStore.setItem(key, 'restored');
  sessionStore.setItem('dgx-drama-image-last-target:p:e', 'shot:r');
  discardEditableDrafts('p', 'e');
  for (const key of editable) expect(sessionStore.getItem(key)).toBeNull();
  for (const key of retained) expect(sessionStore.getItem(key)).toBe('restored');
  history.replaceState({}, '', '/');
  for (const key of [...editable, ...retained]) expect(sessionStore.getItem(key)).toBe('original');
});
