// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  VideoPanel,
  buildVideoBatch,
  explicitRejection,
  readPending,
  savePending,
} from "./VideoPanel";
import {
  EditPanel,
  splitTimelineClip,
  editableFrames,
  retryTimelineId,
  validateClips,
} from "./EditPanel";
import { get, post } from "@/lib/api";

vi.mock("@/lib/api", () => ({
  get: vi.fn(),
  post: vi.fn(),
  segment: encodeURIComponent,
  requestKey: () => "drama-ui-newkey",
  mediaUrl: (pid: string, id: string) =>
    `/dgx-drama/projects/${pid}/assets/${id}/media`,
  uploadReferenceAudio: vi.fn(),
  upload: vi.fn(),
}));
const draft = (id = "shot-1") => ({
  shotId: id,
  shotRevisionId: "revision-1",
  imageAssetId: "image-1",
  imageExtension: "png",
  approved: true,
  needsReview: false,
  audioAssetId: "audio-1",
  soundMode: "reference" as const,
  prompt: "悟空缓缓握拳",
  seed: 42,
});
const clip = {
  clipId: "clip-1",
  takeId: "take-1",
  inFrame: 0,
  frames: 96,
  keepSourceAudio: true,
  fit: "contain",
};
const context = () => ({
  project: { id: "project-1", fps: 24 },
  episode: { id: "episode-1", title: "第 1 集", revision: 1 },
  shots: [
    {
      id: "shot-1",
      title: "悟空迎战",
      revision: 1,
      currentRevisionId: "revision-1",
      selectedTakeId: "take-1",
      takes: [
        {
          id: "take-1",
          assetId: "video-1",
          reviewStatus: "approved",
          source: "h3_video",
        },
      ],
    },
  ],
  assets: [],
  entities: [],
  capabilities: { videoBatchEnabled: true, fitModes: ["contain", "cover"] },
  refresh: vi.fn().mockResolvedValue(undefined),
  notify: vi.fn(),
  navigate: vi.fn(),
  openCanvas: vi.fn(),
  params: {},
});
const job = {
  id: "job-1",
  state: "SUCCEEDED",
  episodeId: "episode-1",
  createdAt: 1788795471,
  memoryReleased: true,
  items: [
    {
      id: "item-1",
      index: 0,
      shotId: "shot-1",
      shotRevisionId: "revision-1",
      takeId: "take-1",
      assetId: "video-1",
      state: "SUCCEEDED",
      submissionPhase: "RECEIVED",
      reviewStatus: "approved",
    },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
  Object.defineProperty(HTMLElement.prototype, "hasPointerCapture", { configurable: true, value: () => false });
  Object.defineProperty(HTMLElement.prototype, "setPointerCapture", { configurable: true, value: () => {} });
  Object.defineProperty(HTMLElement.prototype, "releasePointerCapture", { configurable: true, value: () => {} });
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: () => {} });
  sessionStorage.clear();
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
  Object.defineProperty(HTMLElement.prototype, "checkVisibility", {
    configurable: true,
    value: () => true,
  });
  vi.mocked(get).mockImplementation(async (path: string) =>
    path.endsWith("/production-state")
      ? { paused: false, revision: 1 }
      : path.endsWith("/video-jobs")
        ? { jobs: [] }
        : path === "/library"
          ? { sources: [] }
          : {
              shots: context().shots,
              timeline: {
                id: "timeline-1",
                revision: 1,
                fps: 24,
                clips: [{ ...clip }],
              },
              exports: [],
            },
  );
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("冻结视频批次合同", () => {
  it("固定版本/音频/提示词/种子，不发送从客户端指定的模型和图片参数", () => {
    expect(buildVideoBatch([draft()])).toEqual({ shots: [expect.objectContaining({
          shotId: "shot-1",
          shotRevisionId: "revision-1",
          audioAssetId: "audio-1",
          actionImageAssetId: "",
          videoAssetId: "",
          audioDescription: "",
          prompt: expect.stringContaining("subject_definitions:"),
          seed: 42,
        })] });
    expect(
      buildVideoBatch(Array.from({ length: 32 }, (_, i) => draft(`shot-${i}`)))
        .shots,
    ).toHaveLength(32);
  });
  it.each([[[]], [Array.from({ length: 33 }, (_, i) => draft(`shot-${i}`))]])(
    "拒绝空批次和超过 32 镜頭",
    (value) => expect(() => buildVideoBatch(value)).toThrow("1–32"),
  );
  it("无图使用文字模式且不可与图生模式混批", () => {
    const text = { ...draft("text-shot"), imageAssetId: "", approved: false, audioAssetId: "", soundMode: "silent" as const };
    expect(buildVideoBatch([text]).shots[0].generation?.profile).toBe("10eros-fl2va-v1");
    expect(() => buildVideoBatch([text, draft()])).toThrow("分成两个批次");
  });
  it("拒绝重复镜头、未批准、失效依赖、缺少音频和不合适图片格式", () => {
    expect(() => buildVideoBatch([draft(), draft()])).toThrow("重复");
    for (const patch of [
      { approved: false },
      { shotRevisionId: "" },
      { needsReview: true },
      { audioAssetId: "", soundMode: "reference" as const },
      { imageExtension: "webp" },
    ])
      expect(() => buildVideoBatch([{ ...draft(), ...patch }])).toThrow();
  });
  it("按 UTF-8 字节限制提示词而不是字符数", () => {
    expect(
      buildVideoBatch([{ ...draft(), prompt: "汉".repeat(1000) }]).shots,
    ).toHaveLength(1);
    expect(() =>
      buildVideoBatch([{ ...draft(), prompt: "汉".repeat(5462) }]),
    ).toThrow("16384");
    expect(() => buildVideoBatch([{ ...draft(), prompt: "  " }])).toThrow(
      "不能为空",
    );
  });
  it.each(["", -1, 2147483648, 1.5, "Infinity"])("拒绝非法种子 %s", (seed) =>
    expect(() => buildVideoBatch([{ ...draft(), seed }])).toThrow("种子"),
  );
  it("回执保留原键和完整载荷，只恢复正确项目分集", () => {
    const pending = {
      key: "drama-ui-original",
      payload: buildVideoBatch([draft()]),
    };
    expect(savePending("p", "e", pending)).toBe(true);
    expect(readPending("p", "e")).toEqual({ ...pending, persisted: true });
    expect(readPending("p", "other")).toBeNull();
    sessionStorage.setItem(
      "dgx.drama.video.pending.v1:p:e",
      JSON.stringify({
        version: 1,
        projectId: "p",
        episodeId: "e",
        key: "bad",
        payload: pending.payload,
      }),
    );
    expect(readPending("p", "e")).toBeNull();
  });
  it("仅明确4xx拒绝解锁，网络异常408/429/5xx均保留冻结输入", () => {
    for (const status of [400, 401, 403, 409, 422])
      expect(explicitRejection({ status })).toBe(true);
    for (const error of [
      new Error("network"),
      { status: 408 },
      { status: 429 },
      { status: 500 },
      { status: 503 },
    ])
      expect(explicitRejection(error)).toBe(false);
  });
});

describe("剪辑与导出合同", () => {
  it("秒对齐到最近帧，帧输入必须保持整数语义", () => {
    expect(editableFrames("2.5", "seconds", 24)).toBe(60);
    expect(editableFrames("0.05", "seconds", 24)).toBe(1);
    expect(editableFrames("", "seconds", 24)).toBe("");
    expect(() =>
      validateClips([{ ...clip, frames: editableFrames("1.5", "frames", 24) }]),
    ).toThrow("整数帧");
  });
  it("源入点允许0，长度必须至少1帧，禁止空值和负值", () => {
    expect(
      validateClips([
        {
          ...clip,
          inFrame: "0",
          frames: "1",
          keepSourceAudio: false,
          fit: "cover",
        },
      ]),
    ).toEqual([
      { ...clip, inFrame: 0, frames: 1, keepSourceAudio: false, fit: "cover" },
    ]);
    for (const patch of [
      { inFrame: -1 },
      { frames: 0 },
      { frames: "" },
      { inFrame: "" },
    ])
      expect(() => validateClips([{ ...clip, ...patch }])).toThrow();
  });
  it("失败导出只能沿用其原冻结版本", () => {
    expect(
      retryTimelineId({ state: "FAILED", timelineId: "old-timeline" }),
    ).toBe("old-timeline");
    expect(() => retryTimelineId({ state: "FAILED" })).toThrow(
      "不能用当前版本代替",
    );
    expect(() =>
      retryTimelineId({ state: "SUCCEEDED", timelineId: "old" }),
    ).toThrow();
  });
});

describe("React 交互与未知结果恢复（模拟业务 API）", () => {
  it("重载只恢复不自动发送；双击只发送一次；网络不明后显式恢复沿用原键和载荷", async () => {
    const frozen = {
      key: "drama-ui-original",
      payload: buildVideoBatch([draft()]),
    };
    savePending("project-1", "episode-1", frozen);
    let rejectFirst: (error: Error) => void = () => {};
    vi.mocked(post)
      .mockImplementationOnce(
        () =>
          new Promise((_, reject) => {
            rejectFirst = reject;
          }),
      )
      .mockResolvedValueOnce({ job: { ...job } });
    render(<VideoPanel ctx={context()} />);
    await screen.findByText("原提交结果仍需确认");
    expect(post).not.toHaveBeenCalled();
    const button = screen.getByRole("button", {
      name: "按原请求键确认提交结果",
    });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(post).toHaveBeenCalledTimes(1);
    await act(async () => rejectFirst(new Error("网络响应中断")));
    expect(readPending("project-1", "episode-1")?.key).toBe(
      "drama-ui-original",
    );
    fireEvent.click(
      screen.getByRole("button", { name: "按原请求键确认提交结果" }),
    );
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(vi.mocked(post).mock.calls[0]).toEqual([
      "/episodes/episode-1/video-batches",
      frozen.payload,
      frozen.key,
    ]);
    expect(vi.mocked(post).mock.calls[1]).toEqual(
      vi.mocked(post).mock.calls[0],
    );
    await waitFor(() =>
      expect(readPending("project-1", "episode-1")).toBeNull(),
    );
  });
  it("刷新任务保留审片草稿和同一个播放器节点，可写入当前时间点", async () => {
    vi.mocked(get).mockImplementation(async (path: string) =>
      path.endsWith("/production-state")
        ? { paused: false, revision: 1 }
        : path.endsWith("/video-jobs")
          ? { jobs: [job] }
          : { shots: context().shots },
    );
    const ctx = { ...context(), params: { view: "review" } };
    const { container } = render(<VideoPanel ctx={ctx} />);
    const input = await screen.findByLabelText("审片意见");
    fireEvent.change(input, { target: { value: "人物一致，动作自然" } });
    const video = container.querySelector("video")!;
    video.currentTime = 2.125;
    fireEvent.click(screen.getByRole("button", { name: "记录当前时间点" }));
    expect((input as HTMLTextAreaElement).value).toBe(
      "人物一致，动作自然\n[00:02.125] ",
    );
    fireEvent.click(screen.getByRole("button", { name: "刷新状态" }));
    await waitFor(() => expect(ctx.refresh).toHaveBeenCalled());
    expect(container.querySelector("video")).toBe(video);
    expect(video.currentTime).toBe(2.125);
    expect((input as HTMLTextAreaElement).value).toContain("人物一致");
    expect(post).not.toHaveBeenCalled();
  });
  it("旧修订候选采用先核对版本并弹窗，确认前不修改选片", async () => {
    const ctx = context();
    ctx.shots[0].selectedTakeId = "";
    vi.mocked(get).mockImplementation(async (path: string) =>
      path.endsWith("/production-state")
        ? { paused: false, revision: 1 }
        : path.endsWith("/video-jobs")
          ? { jobs: [job] }
          : path.endsWith("/production")
            ? { revision: 4, currentRevisionId: "new-revision" }
            : { shots: ctx.shots },
    );
    vi.mocked(post).mockResolvedValue({});
    render(<VideoPanel ctx={{ ...ctx, params: { view: "review" } }} />);
    fireEvent.click(await screen.findByRole("button", { name: "设为本镜头选片" }));
    await screen.findByRole("dialog");
    expect(post).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(post).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "设为本镜头选片" }));
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByRole("button", { name: "确认继续" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/shots/shot-1/selection", {
        takeId: "take-1",
        expectedRevision: 4,
        acceptStale: true,
      }),
    );
  });
  it("通过和拒绝允许空意见，批准本身不采用视频", async () => {
    const ctx = context();
    ctx.shots[0].selectedTakeId = "";
    ctx.shots[0].takes[0].reviewStatus = "pending";
    vi.mocked(get).mockImplementation(async (path: string) =>
      path.endsWith("/production-state")
        ? { paused: false, revision: 1 }
        : path.endsWith("/video-jobs")
          ? { jobs: [job] }
          : { shots: ctx.shots },
    );
    vi.mocked(post).mockResolvedValue({});
    render(<VideoPanel ctx={{ ...ctx, params: { view: "review" } }} />);
    await screen.findByLabelText("审片意见");
    expect(
      (screen.getByRole("button", { name: "通过审片" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
    expect(
      (screen.getByRole("button", { name: "审片拒绝" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
    expect(
      (screen.getByRole("button", { name: "设为本镜头选片" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "通过审片" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/takes/take-1/review", {
        decision: "approved",
        notes: "",
      }),
    );
    expect(
      vi
        .mocked(post)
        .mock.calls.filter((call) => call[0].endsWith("/selection")),
    ).toHaveLength(0);
  });
  it("按候选隔离审片意见草稿", async () => {
    const ctx = context();
    ctx.shots[0].selectedTakeId = "";
    ctx.shots[0].takes[0].reviewStatus = "pending";
    const twoCandidates = {
      ...job,
      items: [
        { ...job.items[0], reviewStatus: "pending" },
        {
          ...job.items[0],
          id: "item-2",
          takeId: "take-2",
          assetId: "video-2",
          reviewStatus: "pending",
        },
      ],
    };
    vi.mocked(get).mockImplementation(async (path: string) =>
      path.endsWith("/production-state")
        ? { paused: false, revision: 1 }
        : path.endsWith("/video-jobs")
          ? { jobs: [twoCandidates] }
          : { shots: ctx.shots },
    );
    render(<VideoPanel ctx={{ ...ctx, params: { view: "review" } }} />);
    const first = await screen.findByLabelText("审片意见");
    fireEvent.change(first, { target: { value: "候选一的观察" } });
    fireEvent.click(screen.getByRole("button", { name: "候选 2 · 待审" }));
    const second = await screen.findByLabelText("审片意见");
    expect((second as HTMLTextAreaElement).value).toBe("");
    fireEvent.change(second, { target: { value: "候选二的观察" } });
    fireEvent.click(screen.getByRole("button", { name: "候选 1 · 待审" }));
    expect((await screen.findByLabelText("审片意见") as HTMLTextAreaElement).value).toBe("候选一的观察");
  });
  it("时间线刷新保留秒输入草稿与播放器，保存后才允许导出", async () => {
    const ctx = context(),
      { container } = render(<EditPanel ctx={ctx} />);
    const input = await screen.findByLabelText("片段 1 保留长度");
    const video = container.querySelector("video")!;
    fireEvent.change(input, { target: { value: "2.5" } });
    expect(
      (
        screen.getByRole("button", {
          name: "导出已保存版本",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "刷新状态" }));
    await waitFor(() => expect(ctx.refresh).toHaveBeenCalled());
    expect((input as HTMLInputElement).value).toBe("2.5");
    expect(container.querySelector("video")).toBe(video);
    vi.mocked(post).mockResolvedValueOnce({
      timeline: {
        id: "timeline-2",
        revision: 2,
        fps: 24,
        clips: [{ ...clip, frames: 60 }],
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存时间线" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/episodes/episode-1/timeline-revisions",
        { expectedRevision: 1, clips: [{ ...clip, frames: 60 }] },
        "drama-ui-newkey",
      ),
    );
    await waitFor(() =>
      expect(
        (
          screen.getByRole("button", {
            name: "导出已保存版本",
          }) as HTMLButtonElement
        ).disabled,
      ).toBe(false),
    );
  });
  it("延迟审片保存期间锁定输入与时间点按钮，回执不会覆盖新的可输入草稿", async () => {
    vi.mocked(get).mockImplementation(async (path: string) =>
      path.endsWith("/production-state")
        ? { paused: false, revision: 1 }
        : path.endsWith("/video-jobs")
          ? { jobs: [job] }
          : { shots: context().shots },
    );
    let resolveSave: (value: any) => void = () => {};
    vi.mocked(post).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveSave = resolve;
        }),
    );
    const { container } = render(
      <VideoPanel ctx={{ ...context(), params: { view: "review" } }} />,
    );
    const input = (await screen.findByLabelText(
      "审片意见",
    )) as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: "通过理由" } });
    fireEvent.click(screen.getByRole("button", { name: "通过审片" }));
    expect(
      container.querySelector("[data-panel=video]")?.getAttribute("aria-busy"),
    ).toBe("true");
    expect(input.disabled).toBe(true);
    expect(
      (
        screen.getByRole("button", {
          name: "记录当前时间点",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    await userEvent.setup().type(input, "迟到的新输入");
    expect(input.value).toBe("通过理由");
    await act(async () => resolveSave({}));
    await waitFor(() => expect(input.disabled).toBe(false));
    expect(input.value).toBe("");
  });
  it("延迟时间线保存期间锁定裁切和原声，回执后再允许编辑", async () => {
    let resolveSave: (value: any) => void = () => {};
    vi.mocked(post).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveSave = resolve;
        }),
    );
    const { container } = render(<EditPanel ctx={context()} />);
    const input = (await screen.findByLabelText(
      "片段 1 保留长度",
    )) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "2.5" } });
    fireEvent.click(screen.getByRole("button", { name: "保存时间线" }));
    expect(
      container.querySelector("[data-panel=edit]")?.getAttribute("aria-busy"),
    ).toBe("true");
    expect(input.disabled).toBe(true);
    expect(
      (screen.getByRole("checkbox", { name: "保留原声" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(
      (
        screen.getByRole("button", {
          name: "按镜头顺序建立时间线",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    await userEvent.setup().type(input, "9");
    expect(input.value).toBe("2.5");
    await act(async () =>
      resolveSave({
        timeline: {
          id: "saved",
          revision: 2,
          fps: 24,
          clips: [{ ...clip, frames: 60 }],
        },
      }),
    );
    await waitFor(() => expect(input.disabled).toBe(false));
    expect(input.value).toBe("2.5");
  });
  it("重新导出失败任务使用原任务完整配方，即使当前时间线版本不同", async () => {
    vi.mocked(get).mockImplementation(async (path: string) =>
      path === "/library"
        ? { sources: [] }
        : {
            shots: context().shots,
            timeline: {
              id: "latest-timeline",
              revision: 3,
              fps: 24,
              clips: [{ ...clip }],
            },
            exports: [
              {
                id: "export-old",
                state: "FAILED",
                timelineId: "frozen-old-timeline",
              },
            ],
          },
    );
    vi.mocked(post).mockResolvedValue({
      export: {
        id: "export-new",
        state: "QUEUED",
        timelineId: "frozen-old-timeline",
      },
    });
    render(<EditPanel ctx={context()} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "按原版本重新导出" }),
    );
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/exports/export-old/retry",
        {},
        "drama-ui-newkey",
      ),
    );
  });
});

describe("P4 generation identity", () => {
  const generation = {
    contractVersion: "h3-ref2va-v1" as const,
    profile: "heretic-ref2va-hmnsfw-v1" as const,
    requestDurationSeconds: 15,
  };
  it("freezes profile/duration, refuses mixed profiles and accepts Ring4 integer duration", () => {
    const payload = buildVideoBatch([{ ...draft(), generation }]);
    expect(payload.shots[0].generation).toEqual(generation);
    expect(() =>
      buildVideoBatch([{ ...draft(), generation }, { ...draft("other"), imageAssetId: "", audioAssetId: "", soundMode: "silent" }]),
    ).toThrow("分成两个批次");
    expect(buildVideoBatch([{ ...draft(), generation: { ...generation, requestDurationSeconds: 10 } }]).shots[0].generation?.requestDurationSeconds).toBe(10);
    expect(() => buildVideoBatch([{ ...draft(), generation: { ...generation, requestDurationSeconds: 16 } }])).toThrow();
  });
  it("restores long contract on reload and explicit original-key recovery only", async () => {
    const frozen = {
      key: "drama-ui-long-original",
      payload: buildVideoBatch([{ ...draft(), generation }]),
    };
    savePending("project-1", "episode-1", frozen);
    vi.mocked(post).mockResolvedValue({ job });
    render(<VideoPanel ctx={context()} />);
    await screen.findByText("原提交结果仍需确认");
    expect(post).not.toHaveBeenCalled();
    expect(readPending("project-1", "episode-1")?.payload).toEqual(
      frozen.payload,
    );
    fireEvent.click(
      screen.getByRole("button", { name: "按原请求键确认提交结果" }),
    );
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/episodes/episode-1/video-batches",
        frozen.payload,
        frozen.key,
      ),
    );
  });
  it("shows requested and actual video lengths separately after media metadata loads", async () => {
    vi.mocked(get).mockImplementation(async (path: string) =>
      path.endsWith("/production-state")
        ? { paused: false }
        : path.endsWith("/video-jobs")
          ? { jobs: [{ ...job, items: [{ ...job.items[0], generation }] }] }
          : { shots: context().shots },
    );
    const { container } = render(
      <VideoPanel ctx={{ ...context(), params: { view: "review" } }} />,
    );
    await screen.findByLabelText("审片意见");
    const video = container.querySelector("video")!;
    Object.defineProperty(video, "duration", {
      value: 15.0833333,
      configurable: true,
    });
    fireEvent.loadedMetadata(video);
    expect(
      screen.getByText("实际视频 15.08 秒 · 请求 15 秒；剪辑长度另行设置。"),
    ).toBeTruthy();
  });
});

describe("MF03/MF04 主流程", () => {
  const production = () => ({
    revision: 2, currentRevisionId: "revision-1", invalidations: [],
    current: { approved: true, snapshot: { title: "悟空迎战", action: "缓缓握拳", approvedReferenceAssetId: "image-1", generation: { contractVersion: "h3-ref2va-v1", profile: "heretic-ref2va-v1", requestDurationSeconds: 4 } } },
  });
  const setupDraft = () => {
    const ctx = { ...context(), params: { shotId: "shot-1", prompt: "悟空缓缓握拳" } };
    vi.mocked(get).mockImplementation(async (path: string) => path.endsWith("/production-state") ? { paused: false } : path.endsWith("/video-jobs") ? { jobs: [] } : path.endsWith("/production") ? production() : { shots: ctx.shots });
    return ctx;
  };
  it("无对白直接冻结结构化提示词，连点不重复提交", async () => {
    const ctx = setupDraft();
    vi.mocked(post).mockResolvedValue({ job: { id: "new-job", items: [] } });
    render(<VideoPanel ctx={ctx} />);
    await screen.findByText("查看将发给 H3 的内容");
    const submitButton = screen.getByRole("button", { name: "提交当前批次" });
    fireEvent.click(submitButton);
    fireEvent.click(submitButton);
    await waitFor(() => expect(post).toHaveBeenCalledWith("/episodes/episode-1/video-batches", expect.objectContaining({ shots: [expect.objectContaining({ audioAssetId: "", shotRevisionId: "revision-1", prompt: expect.stringMatching(/overall_soundscape:[\s\S]*No dialogue, narration, speech, or human voice/) })] }), "drama-ui-newkey"));
    expect(vi.mocked(post).mock.calls.filter(([path]) => path.endsWith("/video-batches"))).toHaveLength(1);
    expect(vi.mocked(post).mock.calls).toHaveLength(1);
  });
  it("生成队列不再提供声音方式和参考音频选择", async () => {
    const ctx = setupDraft();
    render(<VideoPanel ctx={ctx} />);
    await screen.findByText("查看将发给 H3 的内容");
    expect(screen.queryByRole("combobox", { name: "声音方式" })).toBeNull();
    expect(screen.queryByLabelText("原生对白台词")).toBeNull();
    expect(screen.queryByText("4 秒参考音频")).toBeNull();
  });
  it("时长修改阻止直接提交，保存和确认后冻结新修订", async () => {
    const ctx = { ...setupDraft(), capabilities: { videoBatchEnabled: true, videoCapabilities: { profiles: [ { id: "heretic-ref2va-v1", name: "H3 · 稳定", workbenchDurationSeconds: [4] }, { id: "heretic-ref2va-hmnsfw-v1", name: "H3 · 长视频", workbenchDurationSeconds: [4, 15] } ] } } };
    let prod = production();
    vi.mocked(get).mockImplementation(async (path: string) => path.endsWith("/production-state") ? { paused: false } : path.endsWith("/video-jobs") ? { jobs: [] } : path.endsWith("/production") ? prod : { shots: ctx.shots });
    vi.mocked(post).mockImplementation(async (path: string, body: any) => {
      if (path.endsWith("/revisions")) { prod = { ...prod, revision: 3, currentRevisionId: "revision-2", current: { approved: false, snapshot: body.snapshot } }; return prod; }
      if (path.endsWith("/reference-selection")) { prod = { ...prod, revision: 4, currentRevisionId: "revision-3", current: { ...prod.current, approved: true } }; return prod; }
      return path.endsWith("/silence") ? { asset: { id: "silent-asset" } } : { job: { id: "new-job", items: [] } };
    });
    render(<VideoPanel ctx={ctx} />);
    await screen.findByText("查看将发给 H3 的内容");
    const user = userEvent.setup();
    expect((screen.getByRole("spinbutton", { name: "生成视频时长" }) as HTMLInputElement).disabled).toBe(false);
    const duration = screen.getByRole("spinbutton", { name: "生成视频时长" });
    expect(duration.getAttribute("min")).toBe("4");
    expect(duration.getAttribute("max")).toBe("15");
    fireEvent.change(duration, { target: { value: "10" } });
    fireEvent.click(screen.getByRole("button", { name: "提交当前批次" }));
    await waitFor(() => expect(ctx.notify).toHaveBeenCalledWith(expect.stringContaining("尚未保存"), "error"));
    expect(post).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "保存时长" }));
    await waitFor(() => expect(ctx.refresh).toHaveBeenCalled());
    expect(vi.mocked(post).mock.calls[0]).toEqual(["/shots/shot-1/revisions", { expectedRevision: 2, baseRevisionId: "revision-1", snapshot: expect.objectContaining({ generation: { contractVersion: "h3-ref2va-v1", profile: "heretic-ref2va-hmnsfw-v1", requestDurationSeconds: 10 } }) }]);
    expect(vi.mocked(post).mock.calls[1]).toEqual(["/shots/shot-1/reference-selection", { expectedRevision: 3, assetId: "image-1" }]);
    fireEvent.click(screen.getByRole("button", { name: "提交当前批次" }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/episodes/episode-1/video-batches", expect.objectContaining({ shots: [expect.objectContaining({ shotRevisionId: "revision-3", generation: { contractVersion: "h3-ref2va-v1", profile: "heretic-ref2va-hmnsfw-v1", requestDurationSeconds: 10 } })] }), "drama-ui-newkey"));
  });
  it.each(["review-fail", "selection-fail", "success"])("组合审片严格先审后选并报告部分失败：%s", async (outcome) => {
    const ctx = context();
    ctx.shots[0].selectedTakeId = "";
    ctx.shots[0].takes[0].reviewStatus = "pending";
    vi.mocked(get).mockImplementation(async (path: string) => path.endsWith("/production-state") ? { paused: false } : path.endsWith("/video-jobs") ? { jobs: [job] } : path.endsWith("/production") ? production() : { shots: ctx.shots });
    vi.mocked(post).mockImplementation(async (path: string) => {
      if (path.endsWith("/review")) {
        if (outcome === "review-fail") throw new Error("审片写入失败");
        ctx.shots[0].takes[0].reviewStatus = "approved";
      }
      if (path.endsWith("/selection") && outcome === "selection-fail") throw new Error("修订冲突");
      return {};
    });
    render(<VideoPanel ctx={{ ...ctx, params: { view: "review" } }} />);
    fireEvent.change(await screen.findByLabelText("审片意见"), { target: { value: "人物稳定、口型同步" } });
    fireEvent.click(screen.getByRole("button", { name: "通过并设为本镜头选片" }));
    await waitFor(() => expect(ctx.notify).toHaveBeenCalled());
    expect(vi.mocked(post).mock.calls[0][0]).toBe("/takes/take-1/review");
    if (outcome === "review-fail") expect(post).toHaveBeenCalledTimes(1);
    else {
      expect(vi.mocked(post).mock.calls[1][0]).toBe("/shots/shot-1/selection");
      if (outcome === "selection-fail") expect(ctx.notify).toHaveBeenCalledWith(expect.stringContaining("审片已通过，但本镜头选片尚未确认成功"), "error");
      else expect(ctx.notify).toHaveBeenCalledWith(expect.stringContaining("到「剪辑与导出」"), "success");
    }
  });
  it("刷新或跨页返回仍显示镜头稿，不被导航提示词覆盖", async () => {
    const ctx = setupDraft();
    const mounted = render(<VideoPanel ctx={ctx} />);
    await screen.findByText("查看将发给 H3 的内容");
    mounted.unmount();
    render(<VideoPanel ctx={{ ...ctx, params: { shotId: "shot-1", prompt: "另一个导航默认提示词" } }} />);
    expect(await screen.findByText(/悟空缓缓握拳/)).toBeTruthy();
    expect(screen.queryByText("另一个导航默认提示词")).toBeNull();
    expect(post).not.toHaveBeenCalled();
  });
  it("无对白不再依赖默认静音素材服务", async () => {
    const ctx = setupDraft();
    vi.mocked(post).mockResolvedValue({ job: { id: "new-job", items: [] } });
    render(<VideoPanel ctx={ctx} />);
    await screen.findByText("查看将发给 H3 的内容");
    fireEvent.click(screen.getByRole("button", { name: "提交当前批次" }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/episodes/episode-1/video-batches", expect.anything(), "drama-ui-newkey"));
    expect(post).toHaveBeenCalledTimes(1);
    expect(readPending("project-1", "episode-1")).toBeNull();
  });

});

describe("MF03 真实页面回归：隐藏视频页不得提前消费镜头导航", () => {
  const queuedProduction = () => ({ revision: 2, currentRevisionId: "revision-2", invalidations: [], current: { approved: false, snapshot: { title: "待确认镜头", action: "缓慢转身", approvedReferenceAssetId: "", generation: { contractVersion: "h3-ref2va-v1", profile: "heretic-ref2va-v1", requestDurationSeconds: 4 } } } });
  it("隐藏→图片确认→进入视频，首次读取最新批准修订与参考图", async () => {
    const ctx = { ...context(), params: { shotId: "shot-1", prompt: "动作导航草稿" } };
    let prod = queuedProduction();
    vi.mocked(get).mockImplementation(async path => path.endsWith("/production") ? prod : path.endsWith("/production-state") ? { paused: false } : path.endsWith("/video-jobs") ? { jobs: [] } : { shots: ctx.shots });
    const mounted = render(<VideoPanel ctx={ctx} active={false} />);
    await waitFor(() => expect(get).toHaveBeenCalledWith("/projects/project-1/video-jobs"));
    expect(vi.mocked(get).mock.calls.filter(([path]) => path.endsWith("/production"))).toHaveLength(0);
    prod = { ...prod, revision: 3, currentRevisionId: "revision-3", current: { ...prod.current, approved: true, snapshot: { ...prod.current.snapshot, approvedReferenceAssetId: "confirmed-image" } } };
    mounted.rerender(<VideoPanel ctx={ctx} active />);
    const image = await screen.findByAltText("待确认镜头 主参考");
    expect(image.getAttribute("src")).toContain("confirmed-image");
    expect(screen.getByText(/动作导航草稿/)).toBeTruthy();
    expect(post).not.toHaveBeenCalled();
  });
  it("已有待确认草稿离开再进入时更新同镜头版本，保留手改提示词种子与对白", async () => {
    const ctx = { ...context(), params: { shotId: "shot-1", prompt: "动作导航草稿" } };
    let prod = queuedProduction();
    vi.mocked(get).mockImplementation(async path => path.endsWith("/production") ? prod : path.endsWith("/production-state") ? { paused: false } : path.endsWith("/video-jobs") ? { jobs: [] } : { shots: ctx.shots });
    const mounted = render(<VideoPanel ctx={ctx} active />);
    await screen.findByText("查看将发给 H3 的内容");
    fireEvent.change(screen.getByLabelText("种子（0–2147483647）"), { target: { value: "123" } });
    mounted.rerender(<VideoPanel ctx={ctx} active={false} />);
    prod = { ...prod, revision: 3, currentRevisionId: "revision-3", current: { ...prod.current, approved: true, snapshot: { ...prod.current.snapshot, approvedReferenceAssetId: "confirmed-image" } } };
    mounted.rerender(<VideoPanel ctx={{ ...ctx, params: { ...ctx.params, prompt: "新的默认提示词不得覆盖手改" } }} active />);
    await screen.findByAltText("待确认镜头 主参考");
    expect(screen.getByAltText("待确认镜头 主参考").getAttribute("src")).toContain("confirmed-image");
    expect(screen.queryByText("新的默认提示词不得覆盖手改")).toBeNull();
    expect((screen.getByLabelText("种子（0–2147483647）") as HTMLInputElement).value).toBe("123");
    expect(post).not.toHaveBeenCalled();
  });
});


describe("R5 剪辑工作区", () => {
  it("分割非零入点时连续保留源区间、声音、裁切和总帧数；拒绝边界与重复ID", () => {
    const original = { ...clip, inFrame: 12, frames: 96, keepSourceAudio: false, fit: "cover", crop: { x: 0, y: 1 } };
    const result = splitTimelineClip([original], original.clipId, 48, "second");
    expect(result.map(c => [c.inFrame, c.frames])).toEqual([[12, 36], [48, 60]]);
    expect(result[1]).toMatchObject({ takeId: clip.takeId, keepSourceAudio: false, crop: { x: 0, y: 1 } });
    expect(original.frames).toBe(96);
    for (const frame of [12, 108, 48.1]) expect(() => splitTimelineClip([original], original.clipId, frame, "second")).toThrow();
    expect(() => splitTimelineClip([original], original.clipId, 48, original.clipId)).toThrow();
  });
  it("顶部下载仅指向当前时间线成功主片，不误用旧版本或代理文件", async () => {
    vi.mocked(get).mockResolvedValue({ timeline: { id: "new", revision: 2, fps: 24, clips: [clip] }, exports: [
      { timelineId: "old", state: "SUCCEEDED", assetId: "old-master" },
      { timelineId: "new", state: "FAILED", assetId: "failed" },
      { timelineId: "new", state: "SUCCEEDED", outputs: [{ role: "proxy", assetId: "proxy" }, { role: "master", assetId: "current" }] },
    ] });
    render(<EditPanel ctx={context()} />);
    const link = await screen.findByRole("link", { name: "下载视频 · 已保存版本" });
    expect(link.getAttribute("href")).toContain("/current/media");
    expect(link.hasAttribute("download")).toBe(true);
    expect(within(screen.getByLabelText("保存与下载成片")).getAllByRole("link")).toHaveLength(1);
  });
  it("仅展开选中片段，修改可撤销/重做，切页释放源播放器并保留修改", async () => {
    vi.mocked(get).mockResolvedValue({ timeline: { id: "new", revision: 2, fps: 24, clips: [clip, { ...clip, clipId: "second" }] }, exports: [] });
    const r = render(<EditPanel ctx={context()} />);
    await screen.findByRole("spinbutton", { name: "片段 1 保留长度" });
    expect(screen.queryByRole("spinbutton", { name: "片段 2 保留长度" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "编辑片段 2 悟空迎战" }));
    const input = screen.getByRole("spinbutton", { name: "片段 2 保留长度" });
    fireEvent.change(input, { target: { value: "2.5" } }); fireEvent.blur(input);
    fireEvent.click(screen.getByRole("button", { name: "撤销片段修改" }));
    expect((input as HTMLInputElement).value).toBe("4");
    expect(screen.getByLabelText("保存与下载成片").textContent).toContain("已保存 · 版本 2");
    expect(sessionStorage.getItem('dgx.drama.edit.draft:project/episode')).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "重做片段修改" }));
    expect((input as HTMLInputElement).value).toBe("2.5");
    r.rerender(<EditPanel ctx={context()} active={false} />);
    expect(screen.queryByLabelText("片段 2 源视频预览")).toBeNull();
    r.rerender(<EditPanel ctx={context()} active />);
    expect((screen.getByRole("spinbutton", { name: "片段 2 保留长度" }) as HTMLInputElement).value).toBe("2.5");
    expect(post).not.toHaveBeenCalled();
  });
});
