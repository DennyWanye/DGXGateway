import assert from "node:assert/strict";
import { test } from "node:test";
import { nextWorkflowAction, showWorkflowPage, formatTaskTime, editableFrames } from "../../custom_nodes/dgx_remote_h3/web/drama/workflow.js";

test("next action distinguishes missing episode, missing shots and pending selection", () => {
  assert.equal(nextWorkflowAction({}).screen, "overview");
  assert.equal(nextWorkflowAction({ episode: {} }).screen, "shots");
  assert.equal(nextWorkflowAction({ episode: {}, shots: [{ takes: [] }] }).screen, "shots");
  assert.equal(nextWorkflowAction({ episode: {}, shots: [{ takes: [{}] }] }).screen, "video");
});
test("a prior export does not hide new unfinished shots; ready selection leads to editing", () => {
  const context = { episode: {}, timeline: {}, exports: [{ state: "SUCCEEDED" }] };
  assert.equal(nextWorkflowAction({ ...context, shots: [{ takes: [] }] }).screen, "shots");
  assert.match(nextWorkflowAction({ ...context, shots: [{ selectedTakeId: "t" }] }).title, /成片/);
  assert.match(nextWorkflowAction({ episode: {}, shots: [{ selectedTakeId: "t" }] }).title, /时间线/);
});
test("step navigation changes visibility without replacing editors or seeking media", () => {
  const editor = { value: "unsaved" }, player = { currentTime: 2.5, pauses: 0, pause() { ++this.pauses; } };
  const pages = { video: { editor, querySelectorAll: () => [player] }, edit: { querySelectorAll: () => [] } };
  const buttons = Object.fromEntries(Object.keys(pages).map(id => [id, { setAttribute(k,v) { this[k] = v; }, removeAttribute(k) { delete this[k]; } }]));
  assert.equal(showWorkflowPage(pages, buttons, "edit"), true);
  assert.equal(pages.video.hidden, true); assert.equal(pages.edit.hidden, false);
  assert.equal(player.pauses, 1); assert.equal(player.currentTime, 2.5);
  assert.equal(buttons.edit["aria-current"], "step");
  showWorkflowPage(pages, buttons, "video");
  assert.equal(pages.video.editor, editor); assert.equal(editor.value, "unsaved");
  assert.equal(buttons.edit["aria-current"], undefined); assert.equal(pages.video.hidden, false);
});
test("unknown step cannot hide every page or pause a player", () => {
  const page = { hidden: false, querySelectorAll() { throw new Error("must not touch media"); } };
  assert.equal(showWorkflowPage({ overview: page }, {}, "bad"), false);
  assert.equal(page.hidden, false);
});

test("task timestamps accept persisted epoch seconds and ISO strings without showing 1970", () => {
  const epoch = 1788780539.45265;
  assert.equal(formatTaskTime(epoch), formatTaskTime(new Date(epoch * 1000).toISOString()));
  assert.equal(formatTaskTime(epoch * 1000), formatTaskTime(epoch));
  assert.equal(formatTaskTime("invalid"), "视频批次");
  assert.equal(formatTaskTime(null), "视频批次");
});

test("seconds editing maps to integer project frames; frame editing and invalid inputs retain validation", () => {
  assert.equal(editableFrames("4", "seconds", 24), 96);
  assert.equal(editableFrames("3.5", "seconds", 24), 84);
  assert.equal(editableFrames("0.1", "seconds", 24), 2);
  assert.equal(editableFrames("97", "frames", 24), "97");
  assert.equal(editableFrames("", "seconds", 24), "");
  assert.equal(editableFrames("bad", "seconds", 24), "");
});
