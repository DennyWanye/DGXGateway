// @vitest-environment jsdom
import { beforeEach, afterEach, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { StoryPanel, sourceSlice, parseSourceFile } from "./StoryPanel";
import { validateStoryContent } from "./StoryForms";
import { get, post } from "@/lib/api";
vi.mock("@/lib/api", () => ({
  get: vi.fn(),
  post: vi.fn(),
  requestKey: () => "drama-ui-story-original",
  segment: encodeURIComponent,
}));
const source = {
  id: "source-1",
  kind: "source",
  revision: 1,
  content: { title: "归来", text: "林舟😀来到湖边。" },
  approved: false,
};
const actor = {
    id: "hero",
    kind: "character",
    revision: 1,
    currentRevisionId: "hero-r1",
    name: "林舟",
  },
  place = {
    id: "place",
    kind: "location",
    revision: 1,
    currentRevisionId: "place-r1",
    name: "湖边",
  };
const beats = {
  id: "beats-1",
  revision: 1,
  approved: true,
  content: {
    schemaVersion: 1,
    sourceRevisionIds: ["source-1"],
    entityRevisionIds: ["hero-r1", "place-r1"],
    targetFrames: 96,
    beats: [
      {
        beatId: "b1",
        title: "抵达",
        description: "林舟来到湖边",
        sourceSpans: [],
      },
    ],
    endingState: { summary: "他看到了来人", unresolvedQuestions: [] },
  },
};
const script = {
  id: "script-1",
  revision: 1,
  approved: true,
  content: {
    schemaVersion: 1,
    sourceRevisionIds: ["source-1"],
    entityRevisionIds: ["hero-r1", "place-r1"],
    beatRevisionId: "beats-1",
    scenes: [
      {
        sceneId: "s1",
        locationId: "place",
        beatIds: ["b1"],
        actions: ["看向湖面"],
        lines: [
          {
            lineId: "l1",
            speakerId: "hero",
            text: "你来了。",
            provenance: { kind: "user_authored" },
          },
        ],
      },
    ],
  },
};
const entities = {
  id: "entities-1",
  revision: 1,
  content: {
    schemaVersion: 1,
    sourceRevisionIds: ["source-1"],
    candidates: [
      {
        candidateId: "candidate-hero",
        kind: "character",
        name: "林舟",
        aliases: [],
        description: "青年",
        sourceSpans: [],
      },
    ],
  },
};
const storyboard = {
  id: "storyboard-1",
  revision: 1,
  content: {
    schemaVersion: 1,
    scriptRevisionId: "script-1",
    shots: [
      {
        candidateId: "shot-1",
        sceneId: "s1",
        title: "湖边重逢",
        action: "看向来人",
        framing: "medium",
        camera: "locked",
        prompt: "写实",
        targetEditFrames: 96,
        generation: {
          contractVersion: "h3-ref2va-v1",
          profile: "heretic-ref2va-v1",
          requestDurationSeconds: 4,
        },
        dialogue: [{ lineId: "l1", speakerId: "hero", text: "你来了。" }],
      },
    ],
    omittedLines: [],
  },
};
function state() {
  return {
    episodeId: "episode",
    heads: {
      source: { revision: 1, currentRevisionId: source.id },
      entities: { revision: 1, currentRevisionId: entities.id },
      beats: {
        revision: 1,
        currentRevisionId: beats.id,
        approvedRevisionId: beats.id,
      },
      script: {
        revision: 1,
        currentRevisionId: script.id,
        approvedRevisionId: script.id,
      },
      storyboard: { revision: 1, currentRevisionId: storyboard.id },
    },
    revisions: {
      source: [source],
      entities: [entities],
      beats: [beats],
      script: [script],
      storyboard: [storyboard],
    },
    editingDrafts: {},
    entityMappings: [],
    textJobs: [],
  };
}
function context() {
  return {
    project: { id: "project", fps: 24 },
    episode: { id: "episode" },
    entities: [actor, place],
    shots: [],
    assets: [],
    capabilities: {
      storyEnabled: true,
      textGenerationEnabled: false,
      textGenerationReason: "Gateway 文本资格待验收",
    },
    refresh: vi.fn().mockResolvedValue(undefined),
    notify: vi.fn(),
    navigate: vi.fn(),
    openCanvas: vi.fn(),
    params: {},
  };
}
async function mount() {
  const ctx = context(),
    view = render(<StoryPanel ctx={ctx} />);
  await screen.findByDisplayValue("归来");
  return { ctx, ...view };
}
beforeEach(() => {
  sessionStorage.clear();
  vi.clearAllMocks();
  vi.mocked(get).mockResolvedValue(state());
  vi.mocked(post).mockImplementation(async (path) =>
    path.endsWith("/confirm-entity")
      ? { entity: { id: "confirmed-entity" }, mapping: {} }
      : path.endsWith("/import")
        ? { shots: [{ id: "imported-shot" }], mappings: [] }
        : path.endsWith("/reconcile")
          ? { job: { id: "unknown", state: "SUBMISSION_UNKNOWN" } }
          : {
              revision: { ...source, id: "source-2", revision: 2 },
              head: { revision: 2 },
            },
  );
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
    configurable: true,
    value: () => {},
  });
  Object.defineProperty(HTMLElement.prototype, "checkVisibility", {
    configurable: true,
    value: () => true,
  });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("P6 source limits and Unicode codepoint provenance keep emoji and reject truncation/bad UTF-8", () => {
  expect(sourceSlice("甲😀乙", 1, 2)).toBe("😀");
  expect(() => sourceSlice("甲😀乙", 1, 4)).toThrow("区间");
  expect(parseSourceFile(new TextEncoder().encode("甲\r\n😀\n").buffer)).toBe(
    "甲\r\n😀\n",
  );
  expect(() => parseSourceFile(new Uint8Array([255]).buffer)).toThrow();
  expect(() => parseSourceFile(new ArrayBuffer(256 * 1024 + 1))).toThrow(
    "截断",
  );
});
it("P6 immutable source save is separate from generation/approval and keeps original whitespace", async () => {
  await mount();
  fireEvent.change(screen.getByLabelText("故事原文"), {
    target: { value: "  新故事\n结尾  " },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存原始故事新修订" }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      "/episodes/episode/source-revisions",
      { title: "归来", text: "  新故事\n结尾  ", expectedRevision: 1 },
      "drama-ui-story-original",
    ),
  );
  expect(post).toHaveBeenCalledTimes(1);
});
it("P6 all stage drafts survive switching, refresh and reload, while CAS base remains original", async () => {
  let ui = await mount();
  fireEvent.change(screen.getByLabelText("故事原文"), {
    target: { value: "来源未保存编辑" },
  });
  fireEvent.click(screen.getByRole("button", { name: /人物与场景/ }));
  fireEvent.change(screen.getByLabelText("候选 1 描述"), {
    target: { value: "人物未保存描述" },
  });
  const updated = state();
  updated.heads.entities.revision = 2;
  updated.heads.entities.currentRevisionId = "entities-2";
  updated.revisions.entities = [{ ...entities, id: "entities-2", revision: 2 }];
  vi.mocked(get).mockResolvedValue(updated);
  fireEvent.click(screen.getByRole("button", { name: "刷新故事记录" }));
  await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
  expect(screen.getByLabelText("候选 1 描述")).toHaveValue("人物未保存描述");
  ui.unmount();
  ui = await (async () => {
    const ctx = context();
    return { ctx, ...render(<StoryPanel ctx={ctx} />) };
  })();
  expect(await screen.findByLabelText("候选 1 描述")).toHaveValue(
    "人物未保存描述",
  );
  fireEvent.click(screen.getByRole("button", { name: "保存人物与场景新修订" }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      "/episodes/episode/entities-revisions",
      expect.objectContaining({ expectedRevision: 1 }),
      expect.any(String),
    ),
  );
  fireEvent.click(screen.getByRole("button", { name: /原始故事/ }));
  expect(screen.getByLabelText("故事原文")).toHaveValue("来源未保存编辑");
});
it("P6 delayed/UNKNOWN writes lock forms and reload only restores original-key explicit recovery", async () => {
  let reject: (e: any) => void = () => {};
  vi.mocked(post).mockImplementationOnce(
    () => new Promise((_, no) => (reject = no)),
  );
  let ui = await mount();
  fireEvent.change(screen.getByLabelText("故事原文"), {
    target: { value: "冻结这份新原文" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存原始故事新修订" }));
  expect(screen.getByLabelText("故事原文")).toBeDisabled();
  const original = vi.mocked(post).mock.calls[0];
  await act(async () => reject(new Error("response lost")));
  ui.unmount();
  ui = await mount();
  expect(post).toHaveBeenCalledTimes(1);
  expect(screen.getByLabelText("故事原文")).toHaveValue("冻结这份新原文");
  fireEvent.click(
    screen.getByRole("button", { name: "按原请求键核查故事操作" }),
  );
  await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
  expect(vi.mocked(post).mock.calls[1]).toEqual(original);
});
it("P6 unavailable Gateway generation is visibly disabled while handwritten candidates remain editable", async () => {
  await mount();
  fireEvent.click(screen.getByRole("button", { name: /人物与场景/ }));
  expect(screen.getByRole("button", { name: "生成本阶段候选" })).toBeDisabled();
  expect(screen.getByText("Gateway 文本资格待验收")).toBeInTheDocument();
  expect(screen.getByLabelText("候选 1 名称")).not.toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "确认 林舟 为资料草稿" }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      "/story-revisions/entities-1/confirm-entity",
      { candidateId: "candidate-hero", expectedRevision: 1 },
      expect.any(String),
    ),
  );
  expect(post).toHaveBeenCalledTimes(1);
});
it("P6 invalid fields locate form inputs and save editable draft without creating formal revision", async () => {
  await mount();
  fireEvent.click(screen.getByRole("button", { name: /情节节拍/ }));
  fireEvent.change(screen.getByLabelText("节拍 1 标题"), {
    target: { value: "" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存情节节拍新修订" }));
  expect(post).not.toHaveBeenCalled();
  fireEvent.click(
    screen.getByRole("button", { name: /beats.0.title：节拍标题不能为空/ }),
  );
  expect(screen.getByLabelText("节拍 1 标题")).toHaveFocus();
  vi.mocked(post).mockResolvedValue({
    draft: {
      id: "invalid-1",
      validation: {
        valid: false,
        errors: [{ path: "beats[0].title", message: "标题不能为空" }],
      },
    },
  });
  fireEvent.click(screen.getByRole("button", { name: "先保存编辑草稿" }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      "/episodes/episode/beats-drafts",
      expect.objectContaining({
        rawText: expect.stringContaining('"title":""'),
        expectedRevision: 0,
      }),
      expect.any(String),
    ),
  );
});
it("P6 import requires explicit selection and produces only a draft import request, no production call", async () => {
  await mount();
  fireEvent.click(screen.getByRole("button", { name: /镜头提案/ }));
  expect(
    screen.getByRole("button", { name: "导入选中镜头草稿" }),
  ).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox", { name: "湖边重逢 · 4 秒" }));
  fireEvent.click(screen.getByRole("button", { name: "导入选中镜头草稿" }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      "/story-revisions/storyboard-1/import",
      { candidateIds: ["shot-1"], expectedRevision: 1 },
      expect.any(String),
    ),
  );
  expect(post).toHaveBeenCalledTimes(1);
});
it("P6 missing dialogue requires explicit omission and changed text cannot pretend to be approved dialogue", () => {
  const missing = structuredClone(storyboard.content);
  missing.shots[0].dialogue = [];
  expect(
    validateStoryContent("storyboard", missing, state(), [actor, place]).some(
      (e) => e.path === "omittedLines",
    ),
  ).toBe(true);
  const forged = structuredClone(storyboard.content);
  forged.shots[0].dialogue[0].text = "没有这句";
  expect(
    validateStoryContent("storyboard", forged, state(), [actor, place]).some(
      (e) => e.message.includes("完整原句"),
    ),
  ).toBe(true);
});
it("P6 malformed model editing draft remains raw and cannot crash or replace the handwritten form", async () => {
  const value: any = state();
  value.editingDrafts.entities = [
    {
      id: "broken",
      revision: 1,
      rawText: '{"schemaVersion":1,"candidates":{}}',
      validation: {
        valid: false,
        errors: [{ path: "candidates", message: "不是数组" }],
      },
    },
  ];
  vi.mocked(get).mockResolvedValue(value);
  await mount();
  fireEvent.click(screen.getByRole("button", { name: /人物与场景/ }));
  fireEvent.click(screen.getByText("编辑草稿 1 · 需要修正"));
  fireEvent.click(screen.getByRole("button", { name: "载入草稿字段继续编辑" }));
  expect(screen.getByRole("alert")).toHaveTextContent("候选列表结构不完整");
  expect(screen.getByLabelText("候选 1 名称")).toHaveValue("林舟");
  expect(post).not.toHaveBeenCalled();
});

it("P6 auxiliary source-range inputs survive stage changes and refresh and set global dirty", async () => {
  const ui = await mount();
  fireEvent.click(screen.getByRole("button", { name: /人物与场景/ }));
  fireEvent.click(screen.getByText("原文依据（可选）· 0 处"));
  fireEvent.change(screen.getByLabelText("候选 1 依据原文"), {
    target: { value: "source-1" },
  });
  fireEvent.change(screen.getByLabelText("候选 1 起始字符"), {
    target: { value: "2" },
  });
  expect(ui.container.querySelector("[data-panel=story]")).toHaveAttribute(
    "data-dirty",
    "true",
  );
  fireEvent.click(screen.getByRole("button", { name: /情节节拍/ }));
  fireEvent.click(screen.getByRole("button", { name: /人物与场景/ }));
  fireEvent.click(screen.getByText("原文依据（可选）· 0 处"));
  expect(screen.getByLabelText("候选 1 起始字符")).toHaveValue(2);
  fireEvent.click(screen.getByRole("button", { name: "刷新故事记录" }));
  await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
  expect(screen.getByLabelText("候选 1 起始字符")).toHaveValue(2);
});
it("P6 explicit original quote sets whole line and Unicode evidence; subsequent edit marks user authorship", async () => {
  const { webcrypto } = await import("node:crypto");
  vi.stubGlobal("crypto", webcrypto);
  await mount();
  fireEvent.click(screen.getByRole("button", { name: /剧本台词/ }));
  fireEvent.click(screen.getByText("原文依据（可选）· 0 处"));
  fireEvent.change(screen.getByLabelText("场景 1 台词 1 依据原文"), {
    target: { value: "source-1" },
  });
  fireEvent.change(screen.getByLabelText("场景 1 台词 1 起始字符"), {
    target: { value: "2" },
  });
  fireEvent.change(screen.getByLabelText("场景 1 台词 1 结束字符"), {
    target: { value: "3" },
  });
  fireEvent.click(
    screen.getByRole("button", { name: "引用原句并替换这条台词" }),
  );
  await waitFor(() =>
    expect(screen.getByLabelText("场景 1 台词 1 文字")).toHaveValue("😀"),
  );
  fireEvent.change(screen.getByLabelText("场景 1 台词 1 文字"), {
    target: { value: "人工改写" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存剧本台词新修订" }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      "/episodes/episode/script-revisions",
      expect.objectContaining({
        content: expect.objectContaining({
          scenes: [
            expect.objectContaining({
              lines: [
                expect.objectContaining({
                  text: "人工改写",
                  provenance: { kind: "user_authored" },
                }),
              ],
            }),
          ],
        }),
      }),
      expect.any(String),
    ),
  );
});
it("P6 approval is independent and disabled while script edits remain unsaved", async () => {
  const value = state();
  value.revisions.script = [{ ...script, approved: false }];
  vi.mocked(get).mockResolvedValue(value);
  await mount();
  fireEvent.click(screen.getByRole("button", { name: /剧本台词/ }));
  fireEvent.click(screen.getByRole("button", { name: "批准当前剧本" }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      "/story-revisions/script-1/approve",
      { expectedRevision: 1 },
      expect.any(String),
    ),
  );
  await waitFor(() =>
    expect(screen.getByLabelText("场景 1 台词 1 文字")).not.toBeDisabled(),
  );
  fireEvent.change(screen.getByLabelText("场景 1 台词 1 文字"), {
    target: { value: "新版未批准" },
  });
  expect(screen.getByRole("button", { name: "批准当前剧本" })).toBeDisabled();
});
it("P6 unknown text task only offers original task reconcile, never automatic generation repair", async () => {
  const value: any = state();
  value.textJobs = [
    {
      id: "unknown",
      kind: "draft_script",
      state: "SUBMISSION_UNKNOWN",
      resourceStatus: "held",
      outputStatus: "none",
      gatewayId: "gateway-original",
      attempts: [
        { id: "attempt", epoch: 1, rawText: "", validation: { errors: [] } },
      ],
    },
  ];
  vi.mocked(get).mockResolvedValue(value);
  await mount();
  fireEvent.click(screen.getByText("提交结果待核对 · 剧本台词"));
  expect(post).not.toHaveBeenCalled();
  expect(
    screen.getByRole("button", { name: "基于原输入请求一次修复候选" }),
  ).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "核对原文本任务" }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      "/text-jobs/unknown/reconcile",
      {},
      expect.any(String),
    ),
  );
});

it("P6 explicit repair freezes original input revisions and disables a second repair", async () => {
  const ctx = context();
  ctx.capabilities.textGenerationEnabled = true;
  const stored: any = state();
  const original = {
    id: "invalid-1",
    kind: "draft_script",
    state: "SUCCEEDED",
    outputStatus: "invalid_draft",
    resourceStatus: "released",
    inputRevisions: {
      sourceRevisionIds: ["old-source"],
      entityRevisionIds: ["old-entity"],
      beatRevisionId: "old-beat",
    },
    attempts: [
      {
        id: "a1",
        rawText: "{bad",
        validation: {
          valid: false,
          errors: [{ path: "$", message: "结构无效" }],
        },
      },
    ],
  };
  stored.textJobs = [original];
  vi.mocked(get).mockImplementation(async () => structuredClone(stored));
  vi.mocked(post).mockImplementation(async () => {
    const job = { ...original, id: "repair-1", repairOf: original.id };
    stored.textJobs.push(job);
    return { job };
  });
  render(<StoryPanel ctx={ctx} />);
  await screen.findByDisplayValue("归来");
  fireEvent.click(
    screen.getByRole("button", { name: "基于原输入请求一次修复候选" }),
  );
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      "/episodes/episode/text-jobs",
      {
        ...original.inputRevisions,
        kind: "draft_script",
        repairOf: original.id,
      },
      "drama-ui-story-original",
    ),
  );
  await waitFor(() =>
    expect(
      screen.getAllByRole("button", { name: "基于原输入请求一次修复候选" }),
    ).toHaveLength(2),
  );
  screen
    .getAllByRole("button", { name: "基于原输入请求一次修复候选" })
    .forEach((button) => expect(button).toBeDisabled());
  expect(post).toHaveBeenCalledTimes(1);
});

it("P6 CAS conflict preserves edited source and reports exact service error", async () => {
  const { ctx } = await mount();
  fireEvent.change(screen.getByLabelText("故事原文"), {
    target: { value: "并发前的人工输入" },
  });
  vi.mocked(post).mockRejectedValue(
    Object.assign(new Error("当前来源已更新，请核对旧草稿"), {
      status: 409,
      errors: [{ path: "text", message: "来源版本冲突" }],
    }),
  );
  fireEvent.click(screen.getByRole("button", { name: "保存原始故事新修订" }));
  await waitFor(() =>
    expect(ctx.notify).toHaveBeenCalledWith(
      "当前来源已更新，请核对旧草稿",
      "error",
    ),
  );
  expect(screen.getByLabelText("故事原文")).toHaveValue("并发前的人工输入");
  expect(
    screen.getByRole("button", { name: /来源版本冲突/ }),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "按原请求键核查故事操作" }),
  ).not.toBeInTheDocument();
});

it.each([
  [
    "人物与场景",
    {
      kind: "extract_entities",
      sourceRevisionIds: ["source-1"],
      entityRevisionIds: [],
    },
  ],
  [
    "情节节拍",
    {
      kind: "draft_beats",
      sourceRevisionIds: ["source-1"],
      entityRevisionIds: ["hero-r1", "place-r1"],
    },
  ],
  [
    "剧本台词",
    {
      kind: "draft_script",
      sourceRevisionIds: ["source-1"],
      entityRevisionIds: ["hero-r1", "place-r1"],
      beatRevisionId: "beats-1",
    },
  ],
  [
    "镜头提案",
    {
      kind: "draft_storyboard",
      sourceRevisionIds: ["source-1"],
      entityRevisionIds: ["hero-r1", "place-r1"],
      scriptRevisionId: "script-1",
    },
  ],
])(
  "P6 generation sends the actual service TASK_KINDS and exact input contract for %s",
  async (label, expected) => {
    const ctx = context();
    ctx.capabilities.textGenerationEnabled = true;
    vi.mocked(post).mockResolvedValue({ job: { id: "text-new" } });
    render(<StoryPanel ctx={ctx} />);
    await screen.findByDisplayValue("归来");
    fireEvent.click(
      screen.getByRole("button", { name: new RegExp(label as string) }),
    );
    fireEvent.click(screen.getByRole("button", { name: "生成本阶段候选" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/episodes/episode/text-jobs",
        expected,
        "drama-ui-story-original",
      ),
    );
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "生成本阶段候选" }),
      ).not.toBeDisabled(),
    );
    expect(post).toHaveBeenCalledTimes(1);
  },
);

it("P6 script generation takes frozen source and entities from selected approved beats, not unrelated current stage defaults", async () => {
  const ctx = context();
  ctx.capabilities.textGenerationEnabled = true;
  const stored: any = state();
  stored.revisions.beats = [
    { ...beats, content: { ...beats.content, entityRevisionIds: ["hero-r1"] } },
  ];
  vi.mocked(get).mockResolvedValue(stored);
  vi.mocked(post).mockResolvedValue({ job: { id: "script-task" } });
  render(<StoryPanel ctx={ctx} />);
  await screen.findByDisplayValue("归来");
  fireEvent.click(screen.getByRole("button", { name: /剧本台词/ }));
  fireEvent.click(screen.getByRole("button", { name: "生成本阶段候选" }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      "/episodes/episode/text-jobs",
      {
        kind: "draft_script",
        sourceRevisionIds: ["source-1"],
        entityRevisionIds: ["hero-r1"],
        beatRevisionId: "beats-1",
      },
      expect.any(String),
    ),
  );
});

it("P6 approval refreshes dependent shots and displays content revision instead of CAS counter", async () => {
  const value = state();
  value.revisions.script = [{ ...script, approved: false }];
  vi.mocked(get).mockResolvedValue(value);
  const { ctx } = await mount();
  fireEvent.click(screen.getByRole("button", { name: /剧本台词/ }));
  vi.mocked(post).mockImplementation(async () => {
    value.heads.script.revision = 2;
    value.revisions.script = [{ ...script, approved: true }];
    return { revision: value.revisions.script[0], head: value.heads.script };
  });
  fireEvent.click(screen.getByRole("button", { name: "批准当前剧本" }));
  await waitFor(() => expect(ctx.refresh).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(ctx.notify).toHaveBeenCalledWith("当前修订已批准，可以继续下一阶段。", "success"));
  expect(await screen.findByText("当前修订 1 · 已批准")).toBeVisible();
  expect(screen.queryByText("当前修订 2")).not.toBeInTheDocument();
});

it.each(["QUEUED", "WAITING_RESOURCE", "LOADING", "SUBMITTING", "RUNNING", "VALIDATING", "RELEASING"])(
  "P6 in-flight %s disables another generation while preserving editing and explicit original-task progress",
  async (jobState) => {
    const ctx = context();
    ctx.capabilities.textGenerationEnabled = true;
    const stored: any = state();
    stored.textJobs = [{ id: "original-active-job", episodeId: "episode", kind: "extract_entities", state: jobState, resourceStatus: "held" }];
    vi.mocked(get).mockResolvedValue(stored);
    render(<StoryPanel ctx={ctx} />);
    await screen.findByDisplayValue("归来");
    fireEvent.click(screen.getByRole("button", { name: /人物与场景/ }));
    expect(screen.getByRole("button", { name: "生成本阶段候选" })).toBeDisabled();
    expect(screen.getByRole("status", { name: "当前生成进度" })).toHaveTextContent("original-active-job");
    expect(screen.getByRole("status", { name: "当前生成进度" })).toHaveTextContent("人物与场景");
    expect(screen.getByLabelText("候选 1 名称")).not.toBeDisabled();
    fireEvent.change(screen.getByLabelText("候选 1 名称"), { target: { value: "继续编辑的人物" } });
    expect(screen.getByRole("button", { name: "保存人物与场景新修订" })).not.toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "核查任务进度" }));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    expect(screen.getByLabelText("候选 1 名称")).toHaveValue("继续编辑的人物");
    expect(post).not.toHaveBeenCalled();
  },
);

it("P6 submission success announces the original task and visibly blocks repeat generation", async () => {
  const ctx = context(); ctx.capabilities.textGenerationEnabled = true;
  const stored: any = state(); vi.mocked(get).mockResolvedValue(stored);
  vi.mocked(post).mockImplementation(async () => {
    const job = { id: "new-loading-task", episodeId: "episode", kind: "extract_entities", state: "LOADING", resourceStatus: "held" };
    stored.textJobs = [job]; return { job };
  });
  render(<StoryPanel ctx={ctx} />); await screen.findByDisplayValue("归来");
  fireEvent.click(screen.getByRole("button", { name: /人物与场景/ }));
  fireEvent.click(screen.getByRole("button", { name: "生成本阶段候选" }));
  await waitFor(() => expect(ctx.notify).toHaveBeenCalledWith(expect.stringMatching(/候选生成任务已提交.*new-loading-task/), "success"));
  expect(await screen.findByRole("status", { name: "当前生成进度" })).toHaveTextContent("正在加载模型");
  expect(screen.getByRole("button", { name: "生成本阶段候选" })).toBeDisabled();
});

it("P6 old UNKNOWN without held resources and another episode's task do not lock this episode", async () => {
  const ctx = context(); ctx.capabilities.textGenerationEnabled = true;
  const stored: any = state();
  stored.textJobs = [
    { id: "historical-unknown", episodeId: "episode", kind: "extract_entities", state: "SUBMISSION_UNKNOWN", resourceStatus: "none" },
    { id: "elsewhere-loading", episodeId: "another-episode", kind: "extract_entities", state: "LOADING", resourceStatus: "held" },
  ];
  vi.mocked(get).mockResolvedValue(stored);
  render(<StoryPanel ctx={ctx} />); await screen.findByDisplayValue("归来");
  fireEvent.click(screen.getByRole("button", { name: /人物与场景/ }));
  expect(screen.getByRole("button", { name: "生成本阶段候选" })).not.toBeDisabled();
  expect(screen.queryByRole("status", { name: "当前生成进度" })).not.toBeInTheDocument();
});
