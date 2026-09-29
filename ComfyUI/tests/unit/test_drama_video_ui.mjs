import assert from "node:assert/strict";
import { test } from "node:test";
import { buildVideoBatch, createVideoWorkbench } from "../../custom_nodes/dgx_remote_h3/web/drama/video.js";
import { post, uploadReferenceAudio, validateReferenceAudioBytes } from "../../custom_nodes/dgx_remote_h3/web/drama/api.js";

const draft = { shotId: "shot-1", shotRevisionId: "revision-4", imageAssetId: "approved-image", approved: true,
  prompt: "走进房间", seed: "42", audioAssetId: "audio-1", title: "镜头一", profile: "must-not-travel", duration: 99 };

function wav({ frames = 96000, channels = 1, rate = 24000, bits = 16, encoding = 1 } = {}) {
  const dataSize = frames * channels * bits / 8;
  const bytes = new Uint8Array(44 + dataSize), view = new DataView(bytes.buffer);
  const tag = (offset, value) => { for (const [index, char] of [...value].entries()) bytes[offset + index] = char.charCodeAt(0); };
  tag(0, "RIFF"); view.setUint32(4, 36 + dataSize, true); tag(8, "WAVE"); tag(12, "fmt ");
  view.setUint32(16, 16, true); view.setUint16(20, encoding, true); view.setUint16(22, channels, true);
  view.setUint32(24, rate, true); view.setUint32(28, rate * channels * bits / 8, true);
  view.setUint16(32, channels * bits / 8, true); view.setUint16(34, bits, true);
  tag(36, "data"); view.setUint32(40, dataSize, true); return bytes;
}

test("batch freezes revisions and only sends allowed input fields", () => {
  assert.deepEqual(buildVideoBatch([draft]), { shots: [{ shotId: "shot-1", shotRevisionId: "revision-4",
    prompt: "走进房间", seed: 42, audioAssetId: "audio-1" }] });
  assert.equal(draft.seed, "42", "form draft must not be mutated");
  assert.equal(buildVideoBatch([draft, { ...draft, shotId: "shot-2" }]).shots.length, 2);
});

test("batch refuses unapproved reference, missing audio, duplicate shots and invalid seed", () => {
  assert.throws(() => buildVideoBatch([]));
  assert.throws(() => buildVideoBatch([draft, draft]), /重复/);
  for (const patch of [{ approved: false }, { needsReview: true }, { imageAssetId: null }, { audioAssetId: "" }, { seed: "-1" }, { seed: "1.5" }, { prompt: " " }, { prompt: "雨".repeat(700) }, { imageExtension: "webp" }]) {
    assert.throws(() => buildVideoBatch([{ ...draft, ...patch }]));
  }
  assert.throws(() => buildVideoBatch(Array.from({ length: 33 }, (_, i) => ({ ...draft, shotId: `shot-${i}` }))), /1–32/);
});

test("strict WAV validation accepts exact format and rejects changes or truncated bytes", () => {
  validateReferenceAudioBytes(wav());
  for (const options of [{ frames: 95999 }, { rate: 22050 }, { channels: 2 }, { bits: 8 }, { encoding: 3 }]) {
    assert.throws(() => validateReferenceAudioBytes(wav(options)));
  }
  assert.throws(() => validateReferenceAudioBytes(wav().subarray(0, 100)));
});

test("audio upload validates before POST and sends explicit action plus fixed project path", async () => {
  const prior = globalThis.fetch; let calls = 0, captured;
  globalThis.fetch = async (url, options) => {
    ++calls; captured = { url, options }; return { ok: true, status: 201, json: async () => ({ asset: { id: "audio" } }) };
  };
  try {
    const bytes = wav(); const file = { name: "参考 音频.wav", size: bytes.length, arrayBuffer: async () => bytes.buffer };
    await uploadReferenceAudio("project/1", file);
    assert.equal(calls, 1);
    assert.equal(captured.url, "/dgx-drama/projects/project%2F1/reference-audio");
    assert.equal(captured.options.headers["X-DGX-Drama-Action"], "explicit-user-action");
    assert.equal(captured.options.headers["X-Filename"], encodeURIComponent(file.name));
    assert.match(captured.options.headers["X-Request-Key"], /^drama-ui-/);
    assert.equal(captured.options.body, file);
    const invalid = wav({ frames: 1 });
    await assert.rejects(uploadReferenceAudio("project", { ...file, size: invalid.length, arrayBuffer: async () => invalid.buffer }));
    assert.equal(calls, 1, "invalid audio must not reach transport");
  } finally { globalThis.fetch = prior; }
});

test("uncertain video submission makes one POST and instructs read-only recovery", async () => {
  const prior = globalThis.fetch; let calls = 0;
  globalThis.fetch = async () => { ++calls; throw new TypeError("connection lost"); };
  try {
    await assert.rejects(post("/episodes/episode/video-batches", buildVideoBatch([draft])), /请先刷新/);
    assert.equal(calls, 1);
  } finally { globalThis.fetch = prior; }
});

// Small DOM test double for controller identity/state behavior; real browser QA
// remains necessary for layout, native media playback, and ComfyUI integration.
class Element {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.parentNode = null; this.dataset = {}; this.style = {}; this.value = ""; this._text = ""; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
  get isConnected() { return this.connected || Boolean(this.parentNode?.isConnected); }
  append(...children) { for (const child of children) { child.remove(); this.children.push(child); child.parentNode = this; } }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(child => child !== this); this.parentNode = null; }
  replaceChildren(...children) { for (const child of this.children) child.parentNode = null; this.children = []; this._text = ""; this.append(...children); }
  setAttribute(name, value) { this[name] = value; }
  removeAttribute(name) { delete this[name]; }
  querySelector(selector) { return walk(this).find(el => selector.startsWith(".") && String(el.className || "").split(" ").includes(selector.slice(1))) || null; }
  querySelectorAll(selector) { return walk(this).filter(el => selector.split(",").includes(el.tagName.toLowerCase())); }
  pause() {}
}
const walk = element => element.children.flatMap(child => [child, ...walk(child)]);
const control = (root, label, tag) => walk(root).find(el => el.tagName === "LABEL" && el._text === label)?.children.find(el => el.tagName === tag.toUpperCase());
const click = async (root, title) => {
  const found = walk(root).find(el => el.tagName === "BUTTON" && el.textContent === title);
  assert.ok(found, `Missing button ${title}`); assert.equal(found.disabled, false, `Disabled button ${title}`); await found.onclick();
};
const settle = async () => { for (let i = 0; i < 8; ++i) await new Promise(resolve => setImmediate(resolve)); };
const response = value => ({ ok: true, status: 200, json: async () => value });

test("read-only refresh preserves the exact prompt editor and batch draft", async () => {
  const priorDocument = globalThis.document, priorFetch = globalThis.fetch;
  globalThis.document = { createElement: tag => new Element(tag) };
  const host = new Element("main"); host.connected = true;
  const incoming = { project: { id: "p1" }, episode: { id: "e1" }, shots: [{ id: "s1", title: "镜头一", takes: [] }],
    assets: [{ id: "audio-1", kind: "audio", role: "h3_reference", filename: "reference.wav" }] };
  let submitted;
  globalThis.fetch = async (url, options = {}) => {
    if (url.endsWith("/production-state")) return response({ revision: 1, paused: false, reservedShots: 0 });
    if (url.endsWith("/video-jobs")) return response({ jobs: [] });
    if (url.endsWith("/projects/p1")) return response({ assets: incoming.assets });
    if (url.endsWith("/shots/s1/production")) return response({ currentRevisionId: "revision-4", current: { approved: true, snapshot: { title: "镜头一", prompt: "初始提示", approvedReferenceAssetId: "image-1" } } });
    if (url.endsWith("/video-batches")) { submitted = JSON.parse(options.body); return response({ job: { id: "job-1", projectId: "p1", episodeId: "e1", state: "QUEUED", items: [] } }); }
    throw new Error(`Unexpected route ${url}`);
  };
  const controller = createVideoWorkbench({ capabilities: { videoBatchEnabled: true }, getContext: () => incoming,
    isBusy: () => false, isVisible: () => false, run: action => action(), report: () => {}, syncAssets: async () => {}, syncEpisode: async () => {} });
  try {
    controller.mount(host); await settle();
    control(host, "加入当前分集镜头", "select").value = "s1";
    await click(host, "加入批次");
    const prompt = control(host, "此镜头视频提示词", "textarea"); prompt.value = "用户修改的提示"; prompt.oninput();
    const audio = control(host, "4 秒参考音频", "select"); audio.value = "audio-1"; audio.onchange();
    assert.equal(controller.hasUnsaved(), true);
    await click(host, "只读刷新任务");
    assert.equal(control(host, "此镜头视频提示词", "textarea"), prompt, "poll/refresh must not replace editor DOM");
    assert.equal(prompt.value, "用户修改的提示");
    await click(host, "提交当前批次");
    assert.equal(submitted.shots[0].prompt, "用户修改的提示");
    assert.equal(submitted.shots[0].shotRevisionId, "revision-4");
    assert.equal(controller.hasUnsaved(), false, "successful receipt clears only submitted draft");
  } finally { controller.detach(); globalThis.document = priorDocument; globalThis.fetch = priorFetch; }
});

test("late response from previous project cannot populate the new project", async () => {
  const priorDocument = globalThis.document, priorFetch = globalThis.fetch;
  globalThis.document = { createElement: tag => new Element(tag) };
  const host = new Element("main"); host.connected = true;
  let incoming = { project: { id: "old" }, episode: { id: "old-episode" }, shots: [], assets: [] }, releaseOld;
  globalThis.fetch = async url => {
    if (url.includes("/projects/old/video-jobs")) return new Promise(resolve => { releaseOld = () => resolve(response({ jobs: [{ id: "old-job-must-not-appear", episodeId: "old-episode", state: "RUNNING", items: [] }] })); });
    if (url.endsWith("/production-state")) return response({ revision: 1, paused: false, reservedShots: url.includes("/new/") ? 2 : 99 });
    if (url.endsWith("/video-jobs")) return response({ jobs: [] });
    return response({ assets: [] });
  };
  const controller = createVideoWorkbench({ capabilities: { videoBatchEnabled: true }, getContext: () => incoming,
    isBusy: () => false, isVisible: () => false, run: action => action(), report: () => {}, syncAssets: async () => {}, syncEpisode: async () => {} });
  try {
    controller.mount(host); await settle();
    incoming = { ...incoming, project: { id: "new" }, episode: { id: "new-episode" } };
    controller.mount(host); await settle(); releaseOld(); await settle();
    assert.doesNotMatch(host.textContent, /old-job-must-not-appear|已预留 99/);
    assert.match(host.textContent, /已预留 2/);
  } finally { controller.detach(); globalThis.document = priorDocument; globalThis.fetch = priorFetch; }
});

test("job refresh preserves player and unsaved review notes; continue stays server-gated", async () => {
  const priorDocument = globalThis.document, priorFetch = globalThis.fetch;
  globalThis.document = { createElement: tag => new Element(tag) };
  const host = new Element("main"); host.connected = true;
  const take = { id: "take-1", assetId: "video-1", reviewStatus: "pending", source: "h3_video" };
  const incoming = { project: { id: "p1" }, episode: { id: "e1" }, assets: [], shots: [{ id: "s1", title: "镜头", takes: [take] }] };
  let memoryReleased = false, writes = 0;
  const job = () => ({ id: "job-1", projectId: "p1", episodeId: "e1", state: "FAILED", memoryReleased,
    cleanupRequired: false, canContinue: false, items: [{ id: "item-1", index: 0, shotId: "s1", shotRevisionId: "r1",
      assetId: "video-1", takeId: "take-1", reviewStatus: "pending", state: "SUCCEEDED", submissionPhase: "UNKNOWN" }] });
  globalThis.fetch = async (url, options = {}) => {
    if (options.method === "POST") ++writes;
    if (url.endsWith("/production-state")) return response({ revision: 1, paused: false });
    if (url.endsWith("/video-jobs")) return response({ jobs: [job()] });
    return response({ assets: [] });
  };
  const controller = createVideoWorkbench({ capabilities: { videoBatchEnabled: true }, getContext: () => incoming,
    isBusy: () => false, isVisible: () => false, run: action => action(), report: () => {}, syncAssets: async () => {}, syncEpisode: async () => {} });
  try {
    controller.mount(host); await settle();
    const video = walk(host).find(el => el.tagName === "VIDEO");
    assert.ok(video);
    const notes = control(host, "人工审片记录（批准和拒绝均必填，最多 2000 字）", "textarea");
    notes.value = "尚未提交的审片记录"; notes.oninput();
    memoryReleased = true; await click(host, "只读刷新任务");
    assert.equal(walk(host).find(el => el.tagName === "VIDEO"), video);
    assert.equal(notes.value, "尚未提交的审片记录"); assert.equal(controller.hasUnsaved(), true);
    assert.match(host.textContent, /资源已归还或未占用/);
    assert.equal(walk(host).find(el => el.tagName === "BUTTON" && el.textContent === "只续拍确定未发出的镜头").disabled, true);
    assert.equal(walk(host).find(el => el.tagName === "BUTTON" && el.textContent === "采用此视频").disabled, true);
    assert.equal(writes, 0, "mount and read-only refresh must never dispatch, release, review or continue");
  } finally { controller.detach(); globalThis.document = priorDocument; globalThis.fetch = priorFetch; }
});

test("lost video receipt locks draft and explicit confirmation reuses exact payload and request key", async () => {
  const priorDocument = globalThis.document, priorFetch = globalThis.fetch, priorStorage = globalThis.sessionStorage;
  const receipts = new Map();
  globalThis.sessionStorage = { setItem: (key, value) => receipts.set(key, value), getItem: key => receipts.get(key) || null, removeItem: key => receipts.delete(key) };
  globalThis.document = { createElement: tag => new Element(tag) };
  const host = new Element("main"); host.connected = true;
  const incoming = { project: { id: "p1" }, episode: { id: "e1" }, shots: [{ id: "s1", title: "镜头", takes: [] }],
    assets: [{ id: "audio-1", role: "h3_reference", kind: "audio" }] };
  const posts = [];
  globalThis.fetch = async (url, options = {}) => {
    if (url.endsWith("/production-state")) return response({ revision: 1, paused: false });
    if (url.endsWith("/video-jobs")) return response({ jobs: [] });
    if (url.endsWith("/projects/p1")) return response({ assets: incoming.assets });
    if (url.endsWith("/production")) return response({ currentRevisionId: "r1", current: { approved: true, snapshot: { title: "镜头", prompt: "原始冻结提示", approvedReferenceAssetId: "image-1" } } });
    if (url.endsWith("/video-batches")) {
      posts.push(JSON.parse(options.body));
      if (posts.length === 1) throw new TypeError("receipt lost after acceptance");
      return response({ job: { id: "same-job", projectId: "p1", episodeId: "e1", state: "QUEUED", items: [] } });
    }
    throw new Error(`Unexpected ${url}`);
  };
  const controller = createVideoWorkbench({ capabilities: { videoBatchEnabled: true }, getContext: () => incoming,
    isBusy: () => false, isVisible: () => false, run: action => action(), report: () => {}, syncAssets: async () => {}, syncEpisode: async () => {} });
  try {
    controller.mount(host); await settle();
    control(host, "加入当前分集镜头", "select").value = "s1"; await click(host, "加入批次");
    const audio = control(host, "4 秒参考音频", "select"); audio.value = "audio-1"; audio.onchange();
    await assert.rejects(click(host, "提交当前批次"), /请先刷新/);
    assert.equal(posts.length, 1);
    const receipt = JSON.parse([...receipts.values()][0]);
    assert.equal(receipt.key, posts[0].requestKey);
    assert.deepEqual(receipt.payload.shots, posts[0].shots);
    assert.equal(control(host, "此镜头视频提示词", "textarea").disabled, true);
    assert.equal(walk(host).find(el => el.tagName === "BUTTON" && el.textContent === "加入批次").disabled, true);
    assert.equal(walk(host).find(el => el.tagName === "BUTTON" && el.textContent === "清空批次草稿").disabled, true);
    await click(host, "按原请求键确认提交结果");
    assert.equal(posts.length, 2); assert.deepEqual(posts[1], posts[0]);
    assert.equal(controller.hasUnsaved(), false);
    assert.equal(receipts.size, 0);
  } finally { controller.detach(); globalThis.document = priorDocument; globalThis.fetch = priorFetch; globalThis.sessionStorage = priorStorage; }
});

test("same-project mount exposes a newly created shot without resetting controller", async () => {
  const priorDocument = globalThis.document, priorFetch = globalThis.fetch;
  globalThis.document = { createElement: tag => new Element(tag) };
  const host = new Element("main"); host.connected = true;
  let incoming = { project: { id: "p1" }, episode: { id: "e1" }, shots: [], assets: [] };
  globalThis.fetch = async url => response(url.endsWith("/production-state") ? { revision: 1, paused: false } : url.endsWith("/video-jobs") ? { jobs: [] } : { assets: [] });
  const controller = createVideoWorkbench({ capabilities: { videoBatchEnabled: true }, getContext: () => incoming,
    isBusy: () => false, isVisible: () => false, run: action => action(), report: () => {}, syncAssets: async () => {}, syncEpisode: async () => {} });
  try {
    controller.mount(host); await settle();
    const picker = control(host, "加入当前分集镜头", "select");
    const add = walk(host).find(el => el.tagName === "BUTTON" && el.textContent === "加入批次");
    assert.equal(add.disabled, true);
    incoming = { ...incoming, shots: [{ id: "new-shot", title: "新镜头", takes: [] }] };
    controller.mount(host);
    assert.equal(control(host, "加入当前分集镜头", "select"), picker);
    assert.deepEqual(picker.children.map(option => option.value), ["", "new-shot"]);
    assert.equal(add.disabled, false);
  } finally { controller.detach(); globalThis.document = priorDocument; globalThis.fetch = priorFetch; }
});

test("session recovery restores frozen receipt but never posts until explicit same-key confirmation", async () => {
  const priorDocument = globalThis.document, priorFetch = globalThis.fetch, priorStorage = globalThis.sessionStorage;
  const payload = buildVideoBatch([draft]);
  const receipts = new Map([["dgx.drama.video.pending.v1:p1:e1", JSON.stringify({ version: 1, projectId: "p1", episodeId: "e1", key: "drama-ui-frozen-key", payload })]]);
  globalThis.sessionStorage = { setItem: (key, value) => receipts.set(key, value), getItem: key => receipts.get(key) || null, removeItem: key => receipts.delete(key) };
  globalThis.document = { createElement: tag => new Element(tag) };
  const host = new Element("main"); host.connected = true;
  const incoming = { project: { id: "p1" }, episode: { id: "e1" }, shots: [], assets: [] };
  const posts = [];
  globalThis.fetch = async (url, options = {}) => {
    if (options.method === "POST") { posts.push(JSON.parse(options.body)); return response({ job: { id: "known-original-job", episodeId: "e1", state: "QUEUED", items: [] } }); }
    if (url.endsWith("/production-state")) return response({ revision: 1, paused: true });
    if (url.endsWith("/video-jobs")) return response({ jobs: [] });
    return response({ assets: [] });
  };
  const controller = createVideoWorkbench({ capabilities: { videoBatchEnabled: true }, getContext: () => incoming,
    isBusy: () => false, isVisible: () => false, run: action => action(), report: () => {}, syncAssets: async () => {}, syncEpisode: async () => {} });
  try {
    controller.mount(host); await settle();
    assert.equal(posts.length, 0); assert.equal(controller.hasPendingSubmission(), true); assert.equal(controller.pendingIsPersisted(), true);
    assert.equal(control(host, "此镜头视频提示词", "textarea").disabled, true);
    await click(host, "按原请求键确认提交结果");
    assert.deepEqual(posts, [{ ...payload, requestKey: "drama-ui-frozen-key" }]);
    assert.equal(receipts.size, 0); assert.equal(controller.hasPendingSubmission(), false);
  } finally { controller.detach(); globalThis.document = priorDocument; globalThis.fetch = priorFetch; globalThis.sessionStorage = priorStorage; }
});

// Navigation must preserve the exact editors, media position and pending identity.
test("generation/review tabs keep drafts and never post during navigation", async () => {
  const priorDocument = globalThis.document, priorFetch = globalThis.fetch;
  globalThis.document = { createElement: tag => new Element(tag) };
  const host = new Element("main"); host.connected = true; let writes = 0;
  const incoming = { project: { id: "p" }, episode: { id: "e" }, shots: [{ id: "s", title: "镜头" }], assets: [] };
  globalThis.fetch = async (url, options = {}) => {
    if (options.method === "POST") ++writes;
    if (url.endsWith("/production-state")) return response({ paused: false, revision: 1 });
    if (url.endsWith("/video-jobs")) return response({ jobs: [] });
    if (url.endsWith("/production")) return response({ currentRevisionId: "revision", current: { approved: true, snapshot: { title: "镜头", prompt: "原提示", approvedReferenceAssetId: "image" } } });
    return response({ assets: [] });
  };
  const controller = createVideoWorkbench({ capabilities: { videoBatchEnabled: true }, getContext: () => incoming,
    isBusy: () => false, isVisible: () => false, run: action => action(), report: () => {} });
  try {
    controller.mount(host); await settle();
    control(host, "加入当前分集镜头", "select").value = "s"; await click(host, "加入批次");
    const prompt = control(host, "此镜头视频提示词", "textarea"); prompt.value = "保留尚未提交的动作"; prompt.oninput();
    await click(host, "任务与审片");
    assert.equal(host.querySelector(".video-generation-pane").hidden, true);
    assert.equal(host.querySelector(".video-review-pane").hidden, false);
    await click(host, "准备生成");
    assert.equal(host.querySelector(".video-generation-pane").hidden, false);
    assert.equal(control(host, "此镜头视频提示词", "textarea"), prompt);
    assert.equal(prompt.value, "保留尚未提交的动作");
    assert.equal(controller.hasUnsaved(), true);
    assert.equal(writes, 0, "navigation cannot submit or mutate any job");
  } finally { controller.detach(); globalThis.document = priorDocument; globalThis.fetch = priorFetch; }
});
