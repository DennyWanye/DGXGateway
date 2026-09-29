// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import userEvent from "@testing-library/user-event";
import { EditPanel, validateClips, isVideoAsset } from "./EditPanel";
import {
  PostproductionPanel,
  AudioTracksEditor,
  validateAudioClips,
  validateSubtitles,
  splitSubtitle,
} from "./PostproductionPanel";
import { get, post, uploadAudio } from "@/lib/api";
import { webcrypto } from "node:crypto";
vi.mock("@/lib/api", () => ({
  get: vi.fn(),
  post: vi.fn(),
  segment: encodeURIComponent,
  requestKey: () => `drama-ui-${Math.random()}`,
  mediaUrl: (p: string, a: string) => `/media/${p}/${a}`,
  upload: vi.fn(),
  uploadAudio: vi.fn(),
}));
const video = {
  clipId: "clip-1",
  takeId: "take-1",
  inFrame: 0,
  frames: 96,
  keepSourceAudio: true,
  fit: "contain",
};
const sound = {
  audioClipId: "audio-clip",
  assetId: "voice",
  track: "voiceover",
  startFrame: 24,
  sourceInFrame: 0,
  frames: 144,
  gainDb: -3,
  fadeInFrames: 12,
  fadeOutFrames: 12,
};
const timeline = {
  id: "tl-1",
  revision: 1,
  fps: 24,
  clips: [video, { ...video, clipId: "clip-2" }],
  audioClips: [sound],
};
const ctx = () => ({
  project: { id: "project", fps: 24, width: 1344, height: 768 },
  episode: { id: "episode", revision: 1 },
  shots: [
    {
      id: "shot",
      title: "镜头",
      selectedTakeId: "take-1",
      takes: [
        {
          id: "take-1",
          assetId: "original",
          source: "h3_video",
          reviewStatus: "approved",
        },
      ],
    },
  ],
  assets: [{ id: "voice", kind: "audio", duration: 12, filename: "旁白.wav" }],
  entities: [],
  capabilities: {
    postproductionEnabled: true,
    asrReady: true,
    fitModes: ["contain", "cover"],
  },
  notify: vi.fn(),
  refresh: vi.fn().mockResolvedValue(undefined),
  navigate: vi.fn(),
  openCanvas: vi.fn(),
  params: {},
});
const subtitle = {
  id: "sub-1",
  revision: 1,
  sourceTimelineRevisionId: "tl-1",
  approved: false,
  needsReview: false,
  segments: [{ startFrame: 0, endFrame: 48, text: "你好世界" }],
};
const exportJob = {
  id: "export-1",
  timelineId: "tl-1",
  state: "SUCCEEDED",
  outputs: [
    { role: "proxy", assetId: "preview" },
    { role: "srt", assetId: "text" },
  ],
  recipe: { outputs: ["proxy", "srt"], presetVersion: "drama-export-v2" },
};
function mountPost(extra = {}) {
  const context = ctx(),
    onExport = vi.fn(),
    onStatus = vi.fn();
  return {
    context,
    onExport,
    onStatus,
    ...render(
      <PostproductionPanel
        ctx={context}
        timeline={timeline}
        timelineDirty={false}
        disabled={false}
        exports={[]}
        onExport={onExport}
        onStatus={onStatus}
        {...extra}
      />,
    ),
  };
}
beforeEach(() => {
  vi.clearAllMocks();
  sessionStorage.clear();
  Object.defineProperty(HTMLElement.prototype, "checkVisibility", {
    configurable: true,
    value: () => true,
  });
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  vi.mocked(get).mockImplementation(async (path) =>
    path.endsWith("/subtitles")
      ? { subtitles: [subtitle] }
      : path.endsWith("/transcriptions")
        ? { transcriptions: [] }
        : path === "/episodes/episode"
          ? { timeline: structuredClone(timeline), exports: [] }
          : { sources: [] },
  );
  vi.mocked(post).mockImplementation(async (path) =>
    path.includes("subtitle-revisions") ? { subtitle } : { timeline },
  );
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("P5 UI frozen oracle", () => {
  it("restores approved subtitles for the saved timeline on entry without marking a new draft", async () => {
    vi.mocked(get).mockImplementation(async path => path.endsWith('/subtitles')
      ? { subtitles: [{ ...subtitle, approved: true }] } : { transcriptions: [] });
    const onPreviewSubtitles = vi.fn();
    const view = mountPost({ onPreviewSubtitles });
    await waitFor(() => expect(screen.getByLabelText('字幕修订记录')).toHaveValue('sub-1'));
    await waitFor(() => expect(onPreviewSubtitles).toHaveBeenLastCalledWith(subtitle.segments));
    expect(view.onStatus).toHaveBeenLastCalledWith(false, false);
    expect(sessionStorage.getItem('dgx.drama.subtitle.draft:project/episode')).toBeNull();
  });
  it("preserves unsaved subtitles instead of replacing them with an approved revision", async () => {
    const segments = [{ startFrame: 0, endFrame: 24, text: '尚未保存的修改' }];
    sessionStorage.setItem('dgx.drama.subtitle.draft:project/episode', JSON.stringify({ segments, dirty: true, timelineId: 'tl-1', baseSubtitleRevision: 1 }));
    vi.mocked(get).mockImplementation(async path => path.endsWith('/subtitles')
      ? { subtitles: [{ ...subtitle, approved: true }] } : { transcriptions: [] });
    const onPreviewSubtitles = vi.fn();
    mountPost({ onPreviewSubtitles });
    await waitFor(() => expect(screen.getByLabelText('字幕修订记录').querySelectorAll('option')).toHaveLength(2));
    expect(onPreviewSubtitles).toHaveBeenLastCalledWith(segments);
    expect(screen.getByLabelText('字幕修订记录')).toHaveValue('');
  });
  it("releases hidden postproduction players while retaining subtitle edits", async () => {
    const view = mountPost({ exports: [exportJob] });
    await screen.findByLabelText('字幕修订记录');
    fireEvent.click(screen.getByRole('button', { name: '添加字幕' }));
    fireEvent.change(screen.getByLabelText('字幕 1 文字'), { target: { value: '切页仍保留' } });
    expect(document.querySelectorAll('video')).toHaveLength(2);
    view.rerender(<PostproductionPanel ctx={view.context} timeline={timeline} timelineDirty={false} disabled={false} exports={[exportJob]} onExport={view.onExport} onStatus={view.onStatus} mediaActive={false} />);
    expect(document.querySelectorAll('video')).toHaveLength(0);
    expect(screen.getByLabelText('字幕 1 文字')).toHaveValue('切页仍保留');
    view.rerender(<PostproductionPanel ctx={view.context} timeline={timeline} timelineDirty={false} disabled={false} exports={[exportJob]} onExport={view.onExport} onStatus={view.onStatus} mediaActive />);
    expect(document.querySelectorAll('video')).toHaveLength(2);
    expect(screen.getByLabelText('字幕 1 文字')).toHaveValue('切页仍保留');
  });
  it("omits keepSourceAudio as true for drama and false for MV", () => {
    const { keepSourceAudio: _keep, ...rest } = video;
    expect(validateClips([rest as typeof video])[0].keepSourceAudio).toBe(true);
    expect(validateClips([rest as typeof video], false, false)[0].keepSourceAudio).toBe(false);
  });
  it("uses the supplied clock so song-length audio is valid while overflow still fails", () => {
    const songClock = { ...sound, startFrame: 0, frames: 96, fadeInFrames: 0, fadeOutFrames: 0 };
    expect(validateAudioClips([songClock], 96, ctx().assets)).toEqual([songClock]);
    expect(() => validateAudioClips([sound], 96, ctx().assets)).toThrow();
  });
  it("keeps independent six-second voice across two four-second shots, rejects full timeline/source/fade bounds", () => {
    expect(validateAudioClips([sound], 192, ctx().assets)).toEqual([sound]);
    for (const patch of [
      { frames: 180 },
      { sourceInFrame: 200 },
      { fadeInFrames: 100, fadeOutFrames: 100 },
      { gainDb: "" },
      { startFrame: 0.5 },
    ])
      expect(() =>
        validateAudioClips([{ ...sound, ...patch }], 192, ctx().assets),
      ).toThrow();
  });
  it("requires explicit cover crop and omits crop from contain while retaining coordinates for cover", () => {
    expect(() => validateClips([{ ...video, fit: "cover" }], true)).toThrow(
      "裁切位置",
    );
    expect(
      validateClips([{ ...video, fit: "cover", crop: { x: 0, y: 1 } }], true)[0]
        .crop,
    ).toEqual({ x: 0, y: 1 });
    expect(
      validateClips([{ ...video, crop: { x: 0.5, y: 0.5 } }], true)[0],
    ).not.toHaveProperty("crop");
  });
  it("subtitle intervals split exactly and overlap or blank text cannot be saved", () => {
    expect(splitSubtitle(subtitle.segments[0])).toEqual([
      { startFrame: 0, endFrame: 24, text: "你好" },
      { startFrame: 24, endFrame: 48, text: "世界" },
    ]);
    expect(() =>
      splitSubtitle({ startFrame: 0, endFrame: 1, text: "你" }),
    ).toThrow();
    expect(() =>
      validateSubtitles(
        [
          { startFrame: 0, endFrame: 48, text: "甲" },
          { startFrame: 24, endFrame: 96, text: "乙" },
        ],
        192,
      ),
    ).toThrow("不重叠");
    expect(() =>
      validateSubtitles([{ startFrame: 0, endFrame: 48, text: "" }], 192),
    ).toThrow("文本");
  });
  it("saves audio with clips, preserves local voice edits across refresh and remount", async () => {
    const context = ctx();
    let ui = render(<EditPanel ctx={context} />);
    const input = await screen.findByLabelText("声音 1 音量 dB");
    fireEvent.change(input, { target: { value: "-9" } });
    fireEvent.click(screen.getByRole("button", { name: "刷新状态" }));
    await waitFor(() => expect(context.refresh).toHaveBeenCalled());
    expect(screen.getByLabelText("声音 1 音量 dB")).toHaveValue(-9);
    ui.unmount();
    ui = render(<EditPanel ctx={context} />);
    expect(await screen.findByLabelText("声音 1 音量 dB")).toHaveValue(-9);
    fireEvent.click(screen.getByRole("button", { name: "保存时间线" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/episodes/episode/timeline-revisions",
        expect.objectContaining({
          clips: timeline.clips,
          audioClips: [{ ...sound, gainDb: -9 }],
        }),
        expect.stringMatching(/^drama-ui-/),
      ),
    );
  });
  it("unknown timeline save freezes controls and remount only restores original-key recovery", async () => {
    vi.mocked(post).mockRejectedValue(new Error("reply lost"));
    const context = ctx();
    let ui = render(<EditPanel ctx={context} />);
    await screen.findByLabelText("声音 1 音量 dB");
    fireEvent.change(screen.getByLabelText("声音 1 音量 dB"), {
      target: { value: "-8" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存时间线" }));
    await screen.findByText(/原提交结果尚未确认/);
    const original = vi.mocked(post).mock.calls[0];
    expect(screen.getByLabelText("声音 1 音量 dB")).toBeDisabled();
    ui.unmount();
    ui = render(<EditPanel ctx={context} />);
    await screen.findByRole("button", { name: "按原请求键核查结果" });
    expect(post).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "按原请求键核查结果" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(vi.mocked(post).mock.calls[1]).toEqual(original);
  });
  it("manual subtitle persists, ASR NEEDS_REVIEW does not overwrite, loading asks before replacement", async () => {
    vi.mocked(get).mockImplementation(async (path) =>
      path.endsWith("/subtitles")
        ? { subtitles: [subtitle] }
        : {
            transcriptions: [
              {
                id: "asr",
                state: "NEEDS_REVIEW",
                result: {
                  segments: [{ startFrame: 0, endFrame: 48, text: "识别内容" }],
                },
              },
            ],
          },
    );
    const ui = mountPost();
    await screen.findByText("结果已保留，需人工载入");
    fireEvent.click(screen.getByRole("button", { name: "添加字幕" }));
    fireEvent.change(screen.getByLabelText("字幕 1 文字"), {
      target: { value: "人工保留" },
    });
    fireEvent.click(screen.getByRole("button", { name: "刷新字幕与转写记录" }));
    expect(screen.getByLabelText("字幕 1 文字")).toHaveValue("人工保留");
    fireEvent.click(screen.getByRole("button", { name: "载入识别结果到草稿" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "返回继续编辑" }));
    expect(screen.getByLabelText("字幕 1 文字")).toHaveValue("人工保留");
    ui.unmount();
    mountPost();
    expect(await screen.findByLabelText("字幕 1 文字")).toHaveValue("人工保留");
    expect(post).not.toHaveBeenCalled();
  });
  it("saves subtitle separately, does not approve or call generation; dirty timeline blocks ASR/export", async () => {
    mountPost();
    await screen.findByLabelText("字幕修订记录");
    fireEvent.click(screen.getByRole("button", { name: "添加字幕" }));
    fireEvent.change(screen.getByLabelText("字幕 1 文字"), {
      target: { value: "仅修改文字" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存字幕新修订" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/timeline-revisions/tl-1/subtitle-revisions",
        {
          expectedRevision: 1,
          segments: [{ startFrame: 0, endFrame: 48, text: "仅修改文字" }],
        },
        expect.any(String),
      ),
    );
    expect(post).toHaveBeenCalledTimes(1);
    cleanup();
    mountPost({ timelineDirty: true });
    expect(
      screen.getByRole("button", { name: "从对白声轨识别字幕" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "提交选定交付文件" }),
    ).toBeDisabled();
  });
  it("plays a bounded subtitle interval on stable rendered proxy, split/merge preserves timing", async () => {
    const ui = mountPost({ exports: [exportJob] });
    await screen.findByLabelText("字幕修订记录");
    fireEvent.change(screen.getByLabelText("字幕修订记录"), {
      target: { value: "sub-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "载入选定字幕修订" }));
    const video = screen.getByLabelText("字幕区间成片预览") as HTMLVideoElement;
    fireEvent.click(screen.getByRole("button", { name: "播放区间" }));
    await waitFor(() => expect(video.play).toHaveBeenCalled());
    video.currentTime = 2.1;
    fireEvent.timeUpdate(video);
    expect(video.pause).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "拆分字幕 1" }));
    expect(screen.getByLabelText("字幕 2 入点")).toHaveValue(24);
    fireEvent.click(screen.getAllByRole("button", { name: "与下一条合并" })[0]);
    expect(screen.queryByLabelText("字幕 2 文字")).not.toBeInTheDocument();
    expect(screen.getByLabelText("字幕区间成片预览")).toBe(video);
    expect(ui.onStatus).toHaveBeenCalledWith(true, false);
  });
  it("failed export retries the complete frozen recipe; technical success only becomes approved after explicit note", async () => {
    vi.mocked(post).mockResolvedValue({
      export: { ...exportJob, id: "retry" },
    });
    const ui = mountPost({
      exports: [
        { ...exportJob, state: "FAILED" },
        { ...exportJob, id: "export-success" },
      ],
    });
    fireEvent.click(
      screen.getByRole("button", { name: "按原完整配方重新导出" }),
    );
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/exports/export-1/retry",
        {},
        expect.any(String),
      ),
    );
    expect(screen.getByText("导出完成 · 可审片")).toBeInTheDocument();
    const approve = screen.getByRole("button", { name: "批准本次完整交付" });
    expect(approve).toBeDisabled();
    await waitFor(() =>
      expect(screen.getByLabelText("全片审片意见")).not.toBeDisabled(),
    );
    fireEvent.change(screen.getByLabelText("全片审片意见"), {
      target: { value: "已看完整片，声音字幕同步" },
    });
    vi.mocked(post).mockResolvedValue({
      exportId: "export-success",
      review: { status: "approved", note: "已看完整片，声音字幕同步" },
    });
    fireEvent.click(approve);
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/exports/export-success/approve",
        { note: "已看完整片，声音字幕同步" },
        expect.any(String),
      ),
    );
    await waitFor(() =>
      expect(ui.onExport).toHaveBeenCalledWith(
        expect.objectContaining({
          id: "export-success",
          review: expect.objectContaining({ status: "approved" }),
        }),
      ),
    );
  });
});

it("P5 delayed subtitle save locks text and reload recovery freezes exact revision and original key", async () => {
  let reject: (value: any) => void = () => {};
  vi.mocked(post).mockImplementationOnce(
    () =>
      new Promise((_, no) => {
        reject = no;
      }),
  );
  let ui = mountPost();
  await screen.findByLabelText("字幕修订记录");
  fireEvent.click(screen.getByRole("button", { name: "添加字幕" }));
  fireEvent.change(screen.getByLabelText("字幕 1 文字"), {
    target: { value: "固定的人工文字" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存字幕新修订" }));
  expect(screen.getByLabelText("字幕 1 文字")).toBeDisabled();
  const original = vi.mocked(post).mock.calls[0];
  await act(async () => reject(new Error("unknown reply")));
  ui.unmount();
  ui = mountPost();
  expect(await screen.findByLabelText("字幕 1 文字")).toHaveValue(
    "固定的人工文字",
  );
  expect(post).toHaveBeenCalledTimes(1);
  vi.mocked(post).mockResolvedValue({ subtitle });
  fireEvent.click(screen.getByRole("button", { name: "按原请求键核查结果" }));
  await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
  expect(vi.mocked(post).mock.calls[1]).toEqual(original);
});

it("P5 a newer subtitle revision cannot silently replace the editing base; explicit merge is required", async () => {
  const ui = mountPost();
  await screen.findByLabelText("字幕修订记录");
  fireEvent.click(screen.getByRole("button", { name: "添加字幕" }));
  fireEvent.change(screen.getByLabelText("字幕 1 文字"), {
    target: { value: "旧基线上的人工修改" },
  });
  vi.mocked(get).mockImplementation(async (path) =>
    path.endsWith("/subtitles")
      ? { subtitles: [{ ...subtitle, revision: 2, id: "sub-2" }] }
      : { transcriptions: [] },
  );
  fireEvent.click(screen.getByRole("button", { name: "刷新字幕与转写记录" }));
  await screen.findByRole("button", { name: "确认合并后的保存基线" });
  fireEvent.click(screen.getByRole("button", { name: "保存字幕新修订" }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      "/timeline-revisions/tl-1/subtitle-revisions",
      expect.objectContaining({ expectedRevision: 1 }),
      expect.any(String),
    ),
  );
  expect(ui.context.notify).not.toHaveBeenCalledWith(
    expect.stringContaining("自动合并"),
    expect.anything(),
  );
});

it("P5 project switch does not copy subtitle draft into a different project storage", async () => {
  const context = ctx(),
    onStatus = vi.fn();
  const props = {
    timeline,
    timelineDirty: false,
    disabled: false,
    exports: [],
    onExport: vi.fn(),
    onStatus,
  };
  const ui = render(<PostproductionPanel ctx={context} {...props} />);
  await screen.findByLabelText("字幕修订记录");
  fireEvent.click(screen.getByRole("button", { name: "添加字幕" }));
  fireEvent.change(screen.getByLabelText("字幕 1 文字"), {
    target: { value: "项目 A 专属内容" },
  });
  const other = {
    ...context,
    project: { ...context.project, id: "other-project" },
  };
  ui.rerender(<PostproductionPanel ctx={other} {...props} />);
  await waitFor(() =>
    expect(screen.queryByLabelText("字幕 1 文字")).not.toBeInTheDocument(),
  );
  expect(
    sessionStorage.getItem("dgx.drama.subtitle.draft:other-project/episode"),
  ).toBeNull();
  expect(
    sessionStorage.getItem("dgx.drama.subtitle.draft:project/episode"),
  ).toContain("项目 A 专属内容");
});

it("P5 cover UI requires a position, previews chosen crop, and saves that explicit coordinate", async () => {
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
    configurable: true,
    value: () => {},
  });
  render(<EditPanel ctx={ctx()} />);
  await screen.findByLabelText("声音 1 音量 dB");
  fireEvent.keyDown(screen.getByRole("combobox", { name: "片段 1 画幅适配" }), {
    key: "Enter",
  });
  await userEvent
    .setup()
    .click(screen.getByRole("option", { name: "填满画幅（选择裁切位置）" }));
  expect(
    screen.getByText("尚未指定裁切位置，保存前请选择。"),
  ).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "保存时间线" }));
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "片段 1 裁切上左" }),
    ).not.toBeDisabled(),
  );
  expect(post).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "片段 1 裁切上左" }));
  expect(screen.getByLabelText("片段 1 源视频预览")).toHaveStyle({
    objectFit: "cover",
    objectPosition: "0% 0%",
  });
  fireEvent.click(screen.getByRole("button", { name: "保存时间线" }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      "/episodes/episode/timeline-revisions",
      expect.objectContaining({
        clips: [
          expect.objectContaining({ fit: "cover", crop: { x: 0, y: 0 } }),
          timeline.clips[1],
        ],
        audioClips: [sound],
      }),
      expect.any(String),
    ),
  );
});

it("P5 subtitle-required outputs cannot export until current subtitles are explicitly approved", async () => {
  mountPost();
  await screen.findByLabelText("字幕修订记录");
  fireEvent.change(screen.getByLabelText("字幕修订记录"), {
    target: { value: "sub-1" },
  });
  fireEvent.click(screen.getByRole("checkbox", { name: "字幕烧录片" }));
  fireEvent.click(screen.getByRole("button", { name: "提交选定交付文件" }));
  await screen.findByText("请先选择并批准当前最新字幕修订。");
  expect(post).not.toHaveBeenCalled();
});

it("P5 malformed ASR retains raw text and validation error read-only without an empty load action", async () => {
  const raw = "<script>never_execute()</script>{invalid";
  vi.mocked(get).mockImplementation(async (path) =>
    path.endsWith("/subtitles")
      ? { subtitles: [] }
      : {
          transcriptions: [
            {
              id: "bad-asr",
              state: "NEEDS_REVIEW",
              result: {
                segments: [],
                rawText: raw,
                rawSha256: "a".repeat(64),
                validationError: "时间段缺少 endFrame",
                requiresCorrection: true,
              },
            },
          ],
        },
  );
  const ui = mountPost();
  await screen.findByText("结果已保留，需人工载入");
  fireEvent.click(screen.getByText("原始识别结果与校验详情"));
  expect(screen.getByLabelText("原始识别结果（只读）")).toHaveValue(raw);
  expect(screen.getByLabelText("原始识别结果（只读）")).toHaveAttribute(
    "readonly",
  );
  expect(
    screen.getByText("时间或格式校验未通过：时间段缺少 endFrame"),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "载入识别结果到草稿" }),
  ).not.toBeInTheDocument();
  expect(ui.container.querySelector("script")).toBeNull();
});

it("P5 subtitle and report deliveries never become selectable video source material", () => {
  for (const asset of [
    { kind: "subtitle", filename: "text.srt" },
    { kind: "report", filename: "report.json" },
    { filename: "text.ass", contentType: "text/plain" },
    { kind: "audio", filename: "voice.wav" },
  ])
    expect(isVideoAsset(asset)).toBe(false);
  expect(isVideoAsset({ kind: "video", filename: "master.mp4" })).toBe(true);
  expect(
    isVideoAsset({ contentType: "video/mp4", filename: "old-video" }),
  ).toBe(true);
});

it("O19 preserves an accepted upload with lost receipt across remount and only replays the exact original binary/key", async () => {
  const originalCrypto = globalThis.crypto;
  Object.defineProperty(globalThis, "crypto", {
    configurable: true,
    value: webcrypto,
  });
  const binary = (bytes: string) => {
    const file = new File([bytes], "原声音.wav", { type: "audio/wav" });
    Object.defineProperty(file, "arrayBuffer", {
      value: async () => new TextEncoder().encode(bytes).buffer,
    });
    return file;
  };
  const mountAudio = () => {
    const context = ctx(),
      onStatus = vi.fn();
    return {
      context,
      onStatus,
      ...render(
        <AudioTracksEditor
          ctx={context}
          clips={timeline.audioClips}
          onChange={vi.fn()}
          totalFrames={192}
          fps={24}
          disabled={false}
          onStatus={onStatus}
        />,
      ),
    };
  };
  const original = binary("original exact audio bytes");
  vi.mocked(uploadAudio).mockRejectedValueOnce(
    new TypeError("response lost after server committed"),
  );
  try {
    const first = mountAudio();
    fireEvent.change(screen.getByLabelText("导入声音（最大 256 MiB）"), {
      target: { files: [original] },
    });
    fireEvent.click(screen.getByRole("button", { name: "上传声音" }));
    await waitFor(() => expect(uploadAudio).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(first.context.notify).toHaveBeenCalledWith(
        "response lost after server committed",
        "error",
      ),
    );
    const key = vi.mocked(uploadAudio).mock.calls[0][2];
    expect(sessionStorage.getItem("dgx.drama.audio.upload:project")).toContain(
      String(key),
    );
    expect(first.onStatus).toHaveBeenLastCalledWith(true, false);
    first.unmount();
    const second = mountAudio();
    expect(screen.getByText(/原上传 原声音.wav/)).toBeInTheDocument();
    expect(uploadAudio).toHaveBeenCalledTimes(1);
    fireEvent.change(screen.getByLabelText("导入声音（最大 256 MiB）"), {
      target: { files: [binary("same name changed bytes")] },
    });
    fireEvent.click(screen.getByRole("button", { name: "按原键重新核查上传" }));
    await waitFor(() =>
      expect(second.context.notify).toHaveBeenCalledWith(
        "请选择原提交的同一音频文件，以保持请求身份。",
        "error",
      ),
    );
    expect(uploadAudio).toHaveBeenCalledTimes(1);
    expect(sessionStorage.getItem("dgx.drama.audio.upload:project")).toContain(
      String(key),
    );
    vi.mocked(uploadAudio).mockResolvedValueOnce({
      asset: { id: "accepted-original" },
    });
    fireEvent.change(screen.getByLabelText("导入声音（最大 256 MiB）"), {
      target: { files: [original] },
    });
    fireEvent.click(screen.getByRole("button", { name: "按原键重新核查上传" }));
    await waitFor(() => expect(second.context.refresh).toHaveBeenCalledOnce());
    expect(vi.mocked(uploadAudio).mock.calls[1]).toEqual([
      "project",
      original,
      key,
    ]);
    expect(sessionStorage.getItem("dgx.drama.audio.upload:project")).toBeNull();
    await waitFor(() =>
      expect(second.onStatus).toHaveBeenLastCalledWith(false, false),
    );
  } finally {
    Object.defineProperty(globalThis, "crypto", {
      configurable: true,
      value: originalCrypto,
    });
  }
});
