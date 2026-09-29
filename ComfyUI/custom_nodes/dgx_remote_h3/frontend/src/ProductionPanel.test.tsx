// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import userEvent from "@testing-library/user-event";
import {
  ProductionPanel,
  compositionParameters,
  imageParameters,
  replaceSelectedCandidate,
} from "./ProductionPanel";
import { get, post, requestKey, uploadImage } from "@/lib/api";
import type { WorkbenchContext } from "@/lib/types";

it('preserves selected new image model, candidate count and parallel limit in the request', () => {
  for (const model of ['flux2-dev', 'flux2-dev-abliterated', 'llada-image-turbo']) {
    expect(imageParameters('generate_image', { model, prompt: 'bookshop', width: 1344, height: 768,
      seed: 42, count: 4, parallelism: 2 })).toMatchObject({ model, batch_count: 4, parallelism: 2, seed: 42 });
  }
});

it('rejects new image dimensions before submitting but preserves legacy Z-Image dimensions', () => {
  const form = {prompt: 'bookshop', width: 528, height: 512, seed: 42};
  expect(imageParameters('generate_image', form)).toMatchObject({ width: 528 });
  expect(() => imageParameters('generate_image', { ...form, model: 'llada-image-turbo' })).toThrow('32');
});
it('keeps only the latest comparison selection for one target slot', () => {
  const first = replaceSelectedCandidate({}, 'candidate-a', 'entity-1', 'identity_front', 'asset-a');
  const second = replaceSelectedCandidate(first, 'candidate-b', 'entity-1', 'identity_front', 'asset-b');
  expect(second).toEqual({ 'candidate-b': { targetId: 'entity-1', slot: 'identity_front', assetId: 'asset-b' } });
});
vi.mock("@/lib/api", () => ({
  get: vi.fn(),
  uploadImage: vi.fn(),
  post: vi.fn(),
  segment: encodeURIComponent,
  mediaUrl: (p: string, a: string) =>
    `/dgx-drama/projects/${p}/assets/${a}/media`,
  requestKey: vi.fn(() => "drama-ui-test-explicit-request-key"),
}));
const actor = {
  id: "actor",
  kind: "character",
  revision: 2,
  currentRevisionId: "actor-r2",
  approvedRevisionId: "actor-r2",
  revisions: [
    {
      id: "actor-r2",
      revision: 2,
      name: "悟空",
      description: "黑色头发",
      referenceRole: "identity_front",
      referenceAssetId: "actor-image",
      references: [{ role: "identity_front", assetId: "actor-image" }],
    },
  ],
};
const production = {
  revision: 4,
  currentRevisionId: "shot-r4",
  current: {
    id: "shot-r4",
    revision: 4,
    snapshot: {
      title: "第一镜",
      action: "凝视",
      framing: "全景",
      camera: "locked",
      prompt: "双人对峙",
      targetEditFrames: 96,
      characterRevisionIds: ["actor-r2"],
      costumeRevisionIds: [],
      propRevisionIds: [],
      locationRevisionId: null,
    },
  },
  invalidations: [],
  history: [],
};
function context(): WorkbenchContext {
  return {
    project: { id: "qa", width: 1344, height: 768, fps: 24 },
    episode: { id: "ep" },
    assets: [
      {
        id: "actor-image",
        kind: "image",
        filename: "goku.png",
        width: 1024,
        height: 1024,
      },
    ],
    shots: [{ id: "shot", title: "第一镜", currentRevisionId: "shot-r4" }],
    entities: [actor],
    capabilities: { referenceGenerationEnabled: true },
    refresh: vi.fn(async () => {}),
    notify: vi.fn(),
    navigate: vi.fn(),
    openCanvas: vi.fn(),
    params: {},
  };
}
beforeEach(() => {
  vi.mocked(requestKey).mockReset().mockReturnValue("drama-ui-test-explicit-request-key");
  sessionStorage.clear();
  vi.clearAllMocks();
  vi.mocked(get).mockImplementation(async (p: string) =>
    p.endsWith("/production")
      ? structuredClone(production)
      : p.endsWith("/reference-jobs")
        ? { jobs: [] }
        : p.endsWith("/image-library")
          ? { sources: [] }
          : { entity: structuredClone(actor) },
  );
  vi.mocked(post).mockResolvedValue({});
});
afterEach(cleanup);
describe('image model residency', () => {
 it('describes image resource finalization without promising unload', async () => {
  vi.mocked(get).mockImplementation(async p => p.endsWith('/reference-jobs') ? { jobs: [{ id: 'finishing-image', state: 'RELEASING', targetType: 'entity', targetId: 'actor', tool: 'generate_image', candidates: [] }] } : { sources: [] });
  const ctx = context(); render(<ProductionPanel ctx={ctx} mode="images" />);
  expect(await screen.findByText('正在处理模型资源')).toBeVisible();
  expect(screen.queryByText('正在释放制作资源')).toBeNull();
  expect(post).not.toHaveBeenCalled();
 });
 it('displays settled resident image job without another submission', async () => {
  vi.mocked(get).mockImplementation(async p => p.endsWith('/reference-jobs') ? { jobs: [{ id: 'resident-image', state: 'SUCCEEDED', targetType: 'entity', targetId: 'actor', tool: 'generate_image', candidates: [], memoryReleased: false, resourceStatus: 'resident', resourceSettled: true }] } : { sources: [] });
  const ctx = context(); render(<ProductionPanel ctx={ctx} mode="images" />);
  expect(await screen.findByText(/生成完成，模型暂留待复用；空闲5分钟后自动释放/)).toBeVisible();
  expect(post).not.toHaveBeenCalled();
 });
});
describe("image parameter contracts", () => {
  it("freezes explicit landscape size and allows seed zero", () =>
    expect(
      imageParameters("generate_image", {
        prompt: "湖畔",
        width: 1344,
        height: 768,
        seed: 0,
      }),
    ).toEqual({ prompt: "湖畔", width: 1344, height: 768, seed: 0, batch_count: 1, parallelism: 1 }));
  it("rejects dimensions that the model cannot accept", () =>
    expect(() =>
      imageParameters("generate_image", {
        prompt: "湖畔",
        width: 1345,
        height: 768,
        seed: 42,
      }),
    ).toThrow("16 的倍数"));
  it("keeps Qwen image ordering and all advanced controls", () =>
    expect(
      imageParameters("edit_image", {
        prompt: "三图合成",
        refs: ["scene", "actor1", "actor2"],
        seed: 42,
        steps: 40,
        count: 8,
        encoder: "huihui-abliterated-bf16",
        precision: "bf16",
      }),
    ).toEqual({
      prompt: "三图合成",
      asset_ids: ["scene", "actor1", "actor2"],
      seed: 42,
      steps: 40,
      batch_count: 8,
      parallelism: 2,
      encoder: "huihui-abliterated-bf16",
      model_precision: "bf16",
    }));
  it("sends only the English prompt, never the Chinese review copy", () =>
    expect(
      imageParameters("edit_image", {
        prompt: "keep the embroidered bodice",
        promptZh: "保留绣花胸衣",
        refs: ["ref1"],
        seed: 7,
        steps: 28,
        count: 1,
        encoder: "standard",
        precision: "fp8",
      }),
    ).toEqual({
      prompt: "keep the embroidered bodice",
      asset_ids: ["ref1"],
      seed: 7,
      steps: 28,
      batch_count: 1,
      parallelism: 1,
      encoder: "standard",
      model_precision: "fp8",
    }));
  it("rejects missing first Qwen input and nonpositive batch", () => {
    expect(() =>
      imageParameters("edit_image", {
        prompt: "test",
        refs: ["", "actor"],
        seed: 42,
      }),
    ).toThrow("输入图 1");
    expect(() =>
      imageParameters("edit_image", {
        prompt: "test",
        refs: ["scene"],
        seed: 42,
        steps: 28,
        count: 0,
      }),
    ).toThrow("候选数量");
  });
  it("preserves negative layer offsets and flip", () =>
    expect(
      compositionParameters("bg", [
        {
          name: " 前景 ",
          asset_id: "fg",
          x: -20,
          y: 0,
          width: 400,
          height: 600,
          opacity: 75,
          flip_x: true,
        },
      ]),
    ).toEqual({
      background_asset_id: "bg",
      layers: [
        {
          name: "前景",
          asset_id: "fg",
          x: -20,
          y: 0,
          width: 400,
          height: 600,
          opacity: 75,
          flip_x: true,
        },
      ],
    }));
  it("rejects absent layers and invalid opacity", () => {
    expect(() => compositionParameters("bg", [])).toThrow("1–16");
    expect(() =>
      compositionParameters("bg", [
        {
          name: "图层",
          asset_id: "a",
          x: 0,
          y: 0,
          width: 512,
          height: 512,
          opacity: 0,
        },
      ]),
    ).toThrow("不透明度");
  });
});
describe("production UI interactions (mocked business API)", () => {
  it("keeps unsaved material text through refresh and mode switching", async () => {
    const ctx = context();
    const ui = render(<ProductionPanel ctx={ctx} mode="materials" />);
    fireEvent.change(screen.getByLabelText("人物名称"), {
      target: { value: "尚未保存的人物" },
    });
    ui.rerender(
      <ProductionPanel
        ctx={{ ...ctx, entities: [...ctx.entities] }}
        mode="images"
      />,
    );
    ui.rerender(
      <ProductionPanel
        ctx={{ ...ctx, entities: [...ctx.entities] }}
        mode="materials"
      />,
    );
    expect(screen.getByLabelText("人物名称")).toHaveValue("尚未保存的人物");
    expect(
      screen.getByRole("region", { name: "人物与场景工作区" }),
    ).toHaveAttribute("data-production-dirty", "true");
    expect(post).not.toHaveBeenCalled();
  });
  it("saves draft with the version captured when editing began", async () => {
    const ctx = context();
    const ui = render(<ProductionPanel ctx={ctx} mode="materials" />);
    fireEvent.click(screen.getByRole("button", { name: /悟空 · v2/ }));
    fireEvent.change(screen.getByLabelText("形象、状态与制作说明"), {
      target: { value: "更改的描述" },
    });
    ui.rerender(
      <ProductionPanel
        ctx={{ ...ctx, entities: [{ ...actor, revision: 3 }] }}
        mode="materials"
      />,
    );
    vi.mocked(post).mockResolvedValue({ entity: actor });
    fireEvent.click(screen.getByRole("button", { name: "保存为新修订" }));
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(vi.mocked(post).mock.calls[0][1]).toMatchObject({
      expectedRevision: 2,
      description: "更改的描述",
    });
  });
  it("requires confirmation before abandoning an entity draft", async () => {
    const ctx = context();
    render(<ProductionPanel ctx={ctx} mode="materials" />);
    fireEvent.change(screen.getByLabelText("人物名称"), {
      target: { value: "未保存" },
    });
    fireEvent.click(screen.getByRole("button", { name: /悟空 · v2/ }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "返回继续编辑" }));
    expect(screen.getByLabelText("人物名称")).toHaveValue("未保存");
  });
  it("edits character dependencies with checkboxes and preserves remaining snapshot fields", async () => {
    const ctx = context();
    render(<ProductionPanel ctx={ctx} mode="shots" />);
    await screen.findByDisplayValue("第一镜");
    const box = screen.getAllByRole("checkbox")[0];
    expect(box).toBeChecked();
    fireEvent.click(box);
    fireEvent.click(screen.getByRole("button", { name: "保存镜头新修订" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/shots/shot/revisions",
        expect.objectContaining({
          expectedRevision: 4,
          baseRevisionId: "shot-r4",
          snapshot: expect.objectContaining({
            characterRevisionIds: [],
            prompt: "双人对峙",
            targetEditFrames: 96,
          }),
        }),
      ),
    );
  });
  it("shows immediate submission feedback and prevents a second pending POST", async () => {
    const ctx = context();
    ctx.params = { target: "entity:actor-r2" };
    let finish: (v: any) => void;
    vi.mocked(post).mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    render(<ProductionPanel ctx={ctx} mode="images" />);
    fireEvent.change(screen.getByLabelText("英文提示词（实际发给模型）"), {
      target: { value: "横屏湖畔" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交图片制作" }));
    expect(screen.getByRole("button", { name: "正在提交…" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "正在提交…" }));
    expect(post).toHaveBeenCalledTimes(1);
    expect(vi.mocked(post).mock.calls[0]).toEqual([
      "/entity-revisions/actor-r2/reference-jobs",
      {
        tool: "generate_image",
        referenceRole: "identity_front",
        parameters: { prompt: "横屏湖畔", width: 1344, height: 768, seed: 42, batch_count: 1, parallelism: 1 },
      },
      "drama-ui-test-explicit-request-key",
    ]);
    finish!({
      job: {
        id: "job",
        targetType: "entity",
        targetId: "actor",
        targetRevisionId: "actor-r2",
        tool: "generate_image",
        state: "RUNNING",
        candidates: [],
      },
    });
    await screen.findAllByText("正在制作");
  });
  it("locks after an ambiguous response while preserving the submitted form", async () => {
    const ctx = context();
    ctx.params = { target: "entity:actor-r2" };
    vi.mocked(post).mockRejectedValue(new Error("connection lost"));
    render(<ProductionPanel ctx={ctx} mode="images" />);
    fireEvent.change(screen.getByLabelText("英文提示词（实际发给模型）"), {
      target: { value: "保留这段要求" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交图片制作" }));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "结果待确认 · 请先核查" }),
      ).toBeDisabled(),
    );
    expect(screen.getByLabelText("英文提示词（实际发给模型）")).toHaveValue("保留这段要求");
    expect(post).toHaveBeenCalledTimes(1);
  });
  it("does not treat missing job receipt as permission to retry", async () => {
    const ctx = context();
    ctx.params = { target: "entity:actor-r2" };
    vi.mocked(post).mockResolvedValue({});
    render(<ProductionPanel ctx={ctx} mode="images" />);
    fireEvent.change(screen.getByLabelText("英文提示词（实际发给模型）"), {
      target: { value: "画面" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交图片制作" }));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "结果待确认 · 请先核查" }),
      ).toBeDisabled(),
    );
    expect(
      screen.getByText(/请求键：drama-ui-test-explicit-request-key/),
    ).toBeInTheDocument();
  });
  it("offers thumbnail choices with dimensions", async () => {
    render(<ProductionPanel ctx={context()} mode="materials" />);
    fireEvent.click(
      screen.getByRole("button", {
        name: "正面身份图 · 批准必需：选择参考图片",
      }),
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText("1024 × 1024")).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: /goku.png · 1024 × 1024/ }),
    );
    expect(
      screen.getByRole("button", { name: "正面身份图 · 批准必需：goku.png" }),
    ).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });
});

describe("production write lifecycle regression", () => {
  it("keeps typed entity fields and saves an externally imported reference in the new revision", async () => {
    const ctx = context();
    vi.mocked(uploadImage).mockResolvedValue({ asset: { id: 'external-image', filename: '外部参考.png', kind: 'image', width: 768, height: 1024 } });
    render(<ProductionPanel ctx={ctx} mode="materials" />);
    fireEvent.click(screen.getByRole('button', { name: '悟空 · v2' }));
    fireEvent.change(screen.getByLabelText('形象、状态与制作说明'), { target: { value: '导入前正在填写的人物说明' } });
    fireEvent.change(screen.getByLabelText('正面身份图 · 批准必需：选择电脑图片文件'), {
      target: { files: [new File(['image'], '外部参考.png', { type: 'image/png' })] },
    });
    await screen.findByRole('button', { name: '正面身份图 · 批准必需：外部参考.png' });
    expect(screen.getByLabelText('形象、状态与制作说明')).toHaveValue('导入前正在填写的人物说明');
    expect(post).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '保存为新修订' }));
    await waitFor(() => expect(post).toHaveBeenCalledWith('/entities/actor/revisions', expect.objectContaining({
      description: '导入前正在填写的人物说明', references: [{ role: 'identity_front', assetId: 'external-image' }],
    })));
  });
  it("locks fields and entity switching until a delayed save resolves", async () => {
    const ctx = context();
    let complete: (v: any) => void;
    vi.mocked(post).mockImplementation(
      () =>
        new Promise((resolve) => {
          complete = resolve;
        }),
    );
    render(<ProductionPanel ctx={ctx} mode="materials" />);
    fireEvent.click(screen.getByRole("button", { name: "悟空 · v2" }));
    const description = screen.getByLabelText("形象、状态与制作说明");
    fireEvent.change(description, { target: { value: "冻结保存内容" } });
    fireEvent.click(screen.getByRole("button", { name: "保存为新修订" }));
    expect(description).toBeDisabled();
    expect(screen.getByRole("button", { name: "新建人物" })).toBeDisabled();
    await userEvent.type(description, "等待期间不能修改");
    expect(description).toHaveValue("冻结保存内容");
    complete!({ entity: { ...actor, revision: 3 } });
    await waitFor(() => expect(description).not.toBeDisabled());
  });
  it("lets a turnaround sheet be confirmed without a front identity image", async () => {
    const ctx = context();
    ctx.entities = [{
      ...actor,
      approvedRevisionId: null,
      revisions: [{
        ...actor.revisions[0],
        referenceRole: "identity_sheet",
        references: [{ role: "identity_sheet", assetId: "actor-image" }],
      }],
    }];
    render(<ProductionPanel ctx={ctx} mode="materials" />);
    fireEvent.click(screen.getByRole("button", { name: "悟空 · v2" }));
    expect(screen.getByRole("button", { name: "确认此参考版本" })).toBeEnabled();
  });
  it("updates the concurrency revision after approval before the next edit", async () => {
    const ctx = context();
    ctx.entities = [{ ...actor, approvedRevisionId: null }];
    vi.mocked(post)
      .mockResolvedValueOnce({
        entity: { ...actor, revision: 3 },
        affectedShotIds: [],
      })
      .mockResolvedValueOnce({ entity: { ...actor, revision: 4 } });
    render(<ProductionPanel ctx={ctx} mode="materials" />);
    fireEvent.click(screen.getByRole("button", { name: "悟空 · v2" }));
    fireEvent.click(screen.getByRole("button", { name: "确认此参考版本" }));
    await waitFor(() =>
      expect(screen.getByLabelText("形象、状态与制作说明")).not.toBeDisabled(),
    );
    fireEvent.change(screen.getByLabelText("形象、状态与制作说明"), {
      target: { value: "批准后编辑" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存为新修订" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(vi.mocked(post).mock.calls[1][1]).toMatchObject({
      expectedRevision: 3,
      description: "批准后编辑",
    });
  });
  it("restores an ambiguous request on remount and only explicitly sends its original identity", async () => {
    const ctx = context();
    ctx.params = { target: "entity:actor-r2" };
    vi.mocked(post).mockRejectedValueOnce(new Error("lost connection"));
    const first = render(<ProductionPanel ctx={ctx} mode="images" />);
    fireEvent.change(screen.getByLabelText("英文提示词（实际发给模型）"), {
      target: { value: "冻结画面要求" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交图片制作" }));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "结果待确认 · 请先核查" }),
      ).toBeDisabled(),
    );
    const initialCall = vi.mocked(post).mock.calls[0];
    first.unmount();
    render(<ProductionPanel ctx={{ ...ctx, params: {} }} mode="images" />);
    expect(screen.getByLabelText("英文提示词（实际发给模型）")).toHaveValue("冻结画面要求");
    expect(
      screen.getByRole("button", { name: "结果待确认 · 请先核查" }),
    ).toBeDisabled();
    expect(post).toHaveBeenCalledTimes(1);
    vi.mocked(post).mockResolvedValueOnce({
      job: {
        id: "recovered",
        targetType: "entity",
        targetId: "actor",
        targetRevisionId: "actor-r2",
        tool: "generate_image",
        state: "RUNNING",
        candidates: [],
      },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "按原请求键确认提交结果" }),
    );
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(vi.mocked(post).mock.calls[1]).toEqual(initialCall);
    await waitFor(() =>
      expect(sessionStorage.getItem("dgx-drama-image-pending:qa")).toBeNull(),
    );
  });
});

it("protects an unsaved shot when another page deep-links to a different shot", async () => {
  const ctx = context();
  ctx.shots.push({
    id: "shot-b",
    title: "第二镜",
    currentRevisionId: "shot-b-r1",
  });
  vi.mocked(get).mockImplementation(async (p: string) =>
    p === "/shots/shot-b/production"
      ? {
          ...production,
          currentRevisionId: "shot-b-r1",
          current: {
            ...production.current,
            snapshot: {
              ...production.current.snapshot,
              title: "第二镜",
              action: "第二镜动作",
            },
          },
        }
      : p.endsWith("/production")
        ? structuredClone(production)
        : p.endsWith("/reference-jobs")
          ? { jobs: [] }
          : { sources: [] },
  );
  const ui = render(<ProductionPanel ctx={ctx} mode="shots" />);
  await screen.findByDisplayValue("第一镜");
  fireEvent.change(screen.getByLabelText("视频生成提示词"), {
    target: { value: "镜头 A 尚未保存的提示词" },
  });
  ui.rerender(<ProductionPanel ctx={ctx} mode="images" />);
  const requested = { ...ctx, params: { shotId: "shot-b" } };
  ui.rerender(<ProductionPanel ctx={requested} mode="shots" />);
  expect(screen.getByRole("dialog")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "返回继续编辑" }));
  expect(screen.getByLabelText("视频生成提示词")).toHaveValue(
    "镜头 A 尚未保存的提示词",
  );
  expect(screen.getByLabelText("镜头名称")).toHaveValue("第一镜");
  ui.rerender(<ProductionPanel ctx={requested} mode="shots" />);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  ui.rerender(
    <ProductionPanel
      ctx={{ ...ctx, params: { shotId: "shot-b" } }}
      mode="shots"
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "确认继续" }));
  await screen.findByDisplayValue("第二镜");
  expect(post).not.toHaveBeenCalled();
});

it("refreshes a clean cached shot detail when an entity approval invalidates its dependencies", async () => {
  const ctx = context();
  const ui = render(<ProductionPanel ctx={ctx} mode="shots" />);
  await screen.findByDisplayValue("第一镜");
  const changed = {
    ...production,
    revision: 5,
    invalidations: [
      {
        id: "inv-1",
        entityId: "actor",
        oldRevisionId: "actor-r2",
        newRevisionId: "actor-r3",
        reason: "人物参考更新",
      },
    ],
  };
  vi.mocked(get).mockImplementation(async (p: string) =>
    p.endsWith("/production")
      ? changed
      : p.endsWith("/reference-jobs")
        ? { jobs: [] }
        : { sources: [] },
  );
  ui.rerender(
    <ProductionPanel
      ctx={{
        ...ctx,
        shots: [{ ...ctx.shots[0], needsReview: true }],
        entities: [{ ...actor, revision: 3, approvedRevisionId: "actor-r3" }],
      }}
      mode="shots"
    />,
  );
  await screen.findByText("关联资料有更新，需要复核");
  expect(screen.getByText(/人物参考更新/)).toBeInTheDocument();
  expect(
    vi.mocked(get).mock.calls.filter(([p]) => p === "/shots/shot/production"),
  ).toHaveLength(2);
  expect(post).not.toHaveBeenCalled();
});
it("preserves dirty shot inputs on dependency refresh and explicitly confirms reloading detail", async () => {
  const ctx = context();
  const ui = render(<ProductionPanel ctx={ctx} mode="shots" />);
  await screen.findByDisplayValue("第一镜");
  fireEvent.change(screen.getByLabelText("视频生成提示词"), {
    target: { value: "尚未保存的新提示词" },
  });
  const changed = {
    ...production,
    revision: 5,
    invalidations: [{ id: "inv-1", entityId: "actor", reason: "参考更新" }],
  };
  vi.mocked(get).mockImplementation(async (p: string) =>
    p.endsWith("/production")
      ? changed
      : p.endsWith("/reference-jobs")
        ? { jobs: [] }
        : { sources: [] },
  );
  ui.rerender(
    <ProductionPanel
      ctx={{
        ...ctx,
        shots: [{ ...ctx.shots[0], needsReview: true }],
        entities: [{ ...actor, revision: 3 }],
      }}
      mode="shots"
    />,
  );
  expect(screen.getByLabelText("视频生成提示词")).toHaveValue("尚未保存的新提示词");
  expect(
    vi.mocked(get).mock.calls.filter(([p]) => p === "/shots/shot/production"),
  ).toHaveLength(1);
  const reload = screen.getByRole("button", { name: "重新读取镜头详情" });
  fireEvent.click(reload);
  fireEvent.click(screen.getByRole("button", { name: "返回继续编辑" }));
  expect(screen.getByLabelText("视频生成提示词")).toHaveValue("尚未保存的新提示词");
  fireEvent.click(reload);
  fireEvent.click(screen.getByRole("button", { name: "确认继续" }));
  await screen.findByText("关联资料有更新，需要复核");
  expect(screen.getByLabelText("视频生成提示词")).toHaveValue("双人对峙");
  expect(post).not.toHaveBeenCalled();
});
it("renders preparation and cancellation service states in Chinese", async () => {
  vi.mocked(get).mockImplementation(async (p: string) =>
    p.endsWith("/reference-jobs")
      ? {
          jobs: [
            {
              id: "prepared",
              state: "PREPARED",
              tool: "generate_image",
              targetType: "entity",
              targetId: "actor",
              candidates: [],
            },
            {
              id: "cancel",
              state: "CANCEL_REQUESTED",
              tool: "generate_image",
              targetType: "entity",
              targetId: "actor",
              candidates: [],
            },
          ],
        }
      : { sources: [] },
  );
  render(<ProductionPanel ctx={context()} mode="images" />);
  await screen.findByText("已保存，等待执行");
  expect(screen.getByText("已请求取消 · 等待确认")).toBeInTheDocument();
});

const productionWithReview = () => ({
  ...structuredClone(production),
  invalidations: [
    {
      id: "review-one",
      entityId: "actor",
      reason: "人物参考更新",
      oldRevisionId: "actor-r1",
      newRevisionId: "actor-r2",
    },
  ],
});
async function chooseShotReference() {
  fireEvent.click(
    screen.getByRole("button", { name: "选择参考画面：选择参考图片" }),
  );
  fireEvent.click(
    screen.getByRole("button", { name: "goku.png · 1024 × 1024" }),
  );
}
it.each(["reason", "review-checkbox", "reference"])(
  "preserves auxiliary-only %s drafts when dependency data refreshes",
  async (kind) => {
    const ctx = context();
    vi.mocked(get).mockImplementation(async (p: string) =>
      p.endsWith("/production")
        ? productionWithReview()
        : p.endsWith("/reference-jobs")
          ? { jobs: [] }
          : { sources: [] },
    );
    const ui = render(<ProductionPanel ctx={ctx} mode="shots" />);
    await screen.findByDisplayValue("第一镜");
    if (kind === "reason")
      fireEvent.change(screen.getByLabelText("接受旧依赖的原因"), {
        target: { value: "只写了复核原因，尚未提交" },
      });
    if (kind === "review-checkbox")
      fireEvent.click(screen.getByRole("checkbox", { name: /人物参考更新/ }));
    if (kind === "reference") await chooseShotReference();
    expect(screen.getByRole("region", { name: "镜头工作区" })).toHaveAttribute(
      "data-production-dirty",
      "true",
    );
    ui.rerender(
      <ProductionPanel
        ctx={{ ...ctx, entities: [{ ...actor, revision: 3 }] }}
        mode="shots"
      />,
    );
    expect(
      vi.mocked(get).mock.calls.filter(([p]) => p === "/shots/shot/production"),
    ).toHaveLength(1);
    if (kind === "reason")
      expect(screen.getByLabelText("接受旧依赖的原因")).toHaveValue(
        "只写了复核原因，尚未提交",
      );
    if (kind === "review-checkbox")
      expect(
        screen.getByRole("checkbox", { name: /人物参考更新/ }),
      ).toBeChecked();
    if (kind === "reference") {
      expect(
        screen.getByRole("button", { name: "选择参考画面：goku.png" }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "确认参考画面" }),
      ).not.toBeDisabled();
    }
    fireEvent.click(screen.getByRole("button", { name: "重新读取镜头详情" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "返回继续编辑" }));
    expect(screen.getByRole("region", { name: "镜头工作区" })).toHaveAttribute(
      "data-production-dirty",
      "true",
    );
    expect(post).not.toHaveBeenCalled();
  },
);
it("keeps former review drafts visible and clearable when a new shot revision returns no invalidations", async () => {
  const ctx = context();
  let stored: any = productionWithReview();
  vi.mocked(get).mockImplementation(async (p: string) =>
    p.endsWith("/production")
      ? structuredClone(stored)
      : p.endsWith("/reference-jobs")
        ? { jobs: [] }
        : { sources: [] },
  );
  vi.mocked(post).mockImplementation(async (p: string, body: any) => {
    stored.revision++;
    if (p.endsWith("/revisions")) {
      stored.current.snapshot = { ...body.snapshot };
      stored.invalidations = [];
    }
    if (p.endsWith("/reference-selection")) {
      stored.current.snapshot.approvedReferenceAssetId = body.assetId;
      stored.invalidations = [];
    }
    return {};
  });
  render(<ProductionPanel ctx={ctx} mode="shots" />);
  await screen.findByDisplayValue("第一镜");
  await chooseShotReference();
  fireEvent.change(screen.getByLabelText("接受旧依赖的原因"), {
    target: { value: "保留旧版人物构图" },
  });
  fireEvent.click(screen.getByRole("checkbox", { name: /人物参考更新/ }));
  fireEvent.change(screen.getByLabelText("视频生成提示词"), {
    target: { value: "新的提示词" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存镜头新修订" }));
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "确认参考画面" }),
    ).not.toBeDisabled(),
  );
  expect(screen.getByText("保留的复核草稿")).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent(
    "当前镜头版本没有待复核项",
  );
  expect(screen.getByLabelText("接受旧依赖的原因")).toHaveValue(
    "保留旧版人物构图",
  );
  expect(screen.getByText(/之前勾选的 1 项包含/)).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "确认接受所选旧依赖" }),
  ).toBeDisabled();
  expect(
    screen.getByRole("button", { name: "选择参考画面：goku.png" }),
  ).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "确认参考画面" }));
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "清除复核草稿" }),
    ).not.toBeDisabled(),
  );
  expect(screen.getByLabelText("接受旧依赖的原因")).toHaveValue(
    "保留旧版人物构图",
  );
  fireEvent.click(screen.getByRole("button", { name: "清除复核草稿" }));
  expect(screen.queryByLabelText("接受旧依赖的原因")).not.toBeInTheDocument();
  expect(screen.getByRole("region", { name: "镜头工作区" })).toHaveAttribute(
    "data-production-dirty",
    "false",
  );
  expect(vi.mocked(post).mock.calls.map(([p]) => p)).toEqual([
    "/shots/shot/revisions",
    "/shots/shot/reference-selection",
  ]);
});
it("submitting a valid dependency review preserves an unapproved reference selection", async () => {
  const ctx = context();
  let stored: any = productionWithReview();
  vi.mocked(get).mockImplementation(async (p: string) =>
    p.endsWith("/production")
      ? structuredClone(stored)
      : p.endsWith("/reference-jobs")
        ? { jobs: [] }
        : { sources: [] },
  );
  vi.mocked(post).mockImplementation(async (p: string) => {
    if (p.endsWith("/review")) {
      stored.revision++;
      stored.invalidations[0].resolved = true;
    }
    return {};
  });
  render(<ProductionPanel ctx={ctx} mode="shots" />);
  await screen.findByDisplayValue("第一镜");
  await chooseShotReference();
  fireEvent.change(screen.getByLabelText("接受旧依赖的原因"), {
    target: { value: "保留已批准旧依赖" },
  });
  fireEvent.click(screen.getByRole("checkbox", { name: /人物参考更新/ }));
  fireEvent.click(screen.getByRole("button", { name: "确认接受所选旧依赖" }));
  await waitFor(() =>
    expect(screen.getByLabelText("接受旧依赖的原因")).toHaveValue(""),
  );
  expect(
    screen.getByRole("button", { name: "选择参考画面：goku.png" }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "确认参考画面" }),
  ).not.toBeDisabled();
  expect(screen.getByRole("region", { name: "镜头工作区" })).toHaveAttribute(
    "data-production-dirty",
    "true",
  );
  expect(post).toHaveBeenCalledTimes(1);
});

it("freezes a typed Ring4 duration without changing edit length", async () => {
  const ctx = context();
  ctx.capabilities.videoCapabilities = {
    profiles: [
      {
        id: "heretic-ref2va-v1",
        name: "H3 · 稳定 4 秒",
        workbenchDurationSeconds: [4],
        verifiedPersistentDurationSeconds: [4],
      },
      {
        id: "heretic-ref2va-hmnsfw-v1",
        name: "H3 · 长视频",
        workbenchDurationSeconds: [4, 15],
        verifiedPersistentDurationSeconds: [],
      },
    ],
  };
  const user = userEvent.setup();
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
    configurable: true,
    value: () => {},
  });
  render(<ProductionPanel ctx={ctx} mode="shots" />);
  await screen.findByDisplayValue("第一镜");
  expect(screen.getByText("H3 Ring4 · 自适应多模态")).toBeInTheDocument();
  const duration = screen.getByRole("spinbutton", { name: "请求生成时长" });
  expect(duration).toHaveAttribute("min", "4");
  expect(duration).toHaveAttribute("max", "15");
  fireEvent.change(duration, { target: { value: "10" } });
  expect(screen.getByText(/15 秒真实成片已跑通/)).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "保存镜头新修订" }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      "/shots/shot/revisions",
      expect.objectContaining({
        snapshot: expect.objectContaining({
          targetEditFrames: 96,
          generation: {
            contractVersion: "h3-ref2va-v1",
            profile: "heretic-ref2va-hmnsfw-v1",
            requestDurationSeconds: 10,
          },
        }),
      }),
    ),
  );
});

it("P6 reviews entity and script dependencies separately with fresh CAS and keeps remaining draft", async () => {
  const ctx = context();
  let stored: any = productionWithReview();
  stored.current.snapshot.approvedReferenceAssetId = "actor-image";
  stored.invalidations.push({
    id: "script-inv",
    kind: "script",
    reason: "批准剧本更新",
    oldRevisionId: "script-v1",
    newRevisionId: "script-v2",
  });
  vi.mocked(get).mockImplementation(async (p) =>
    p.endsWith("/production")
      ? structuredClone(stored)
      : p.endsWith("/reference-jobs")
        ? { jobs: [] }
        : { sources: [] },
  );
  vi.mocked(post).mockImplementation(async (_p, body: any) => {
    expect(body.expectedRevision).toBe(stored.revision);
    stored.revision++;
    stored.invalidations = stored.invalidations.filter(
      (i: any) => !body.invalidationIds.includes(i.id),
    );
    return { revision: stored.revision };
  });
  render(<ProductionPanel ctx={ctx} mode="shots" />);
  await screen.findByText(/剧本与对白/);
  expect(screen.getByRole("button", { name: "确认参考画面" })).toBeDisabled();
  expect(
    screen.getByRole("button", { name: "下一步：H3 Ring4 生成" }),
  ).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "查看故事与剧本" }));
  expect(ctx.navigate).toHaveBeenCalledWith("story");
  fireEvent.click(screen.getByRole("checkbox", { name: /人物参考更新/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /剧本与对白/ }));
  fireEvent.change(screen.getByLabelText("接受旧依赖的原因"), {
    target: { value: "本镜头继续使用原对白与人物构图" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认接受所选旧依赖" }));
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "确认保留所选旧剧本对白" }),
    ).not.toBeDisabled(),
  );
  expect(post).toHaveBeenCalledTimes(1);
  expect(vi.mocked(post).mock.calls[0].slice(0, 2)).toEqual([
    "/shots/shot/review",
    {
      expectedRevision: 4,
      invalidationIds: ["review-one"],
      reason: "本镜头继续使用原对白与人物构图",
    },
  ]);
  expect(screen.getByRole("checkbox", { name: /剧本与对白/ })).toBeChecked();
  expect(screen.getByLabelText("接受旧依赖的原因")).toHaveValue(
    "本镜头继续使用原对白与人物构图",
  );
  fireEvent.click(
    screen.getByRole("button", { name: "确认保留所选旧剧本对白" }),
  );
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "确认参考画面" }),
    ).not.toBeDisabled(),
  );
  expect(vi.mocked(post).mock.calls[1].slice(0, 2)).toEqual([
    "/shots/shot/script-review",
    {
      expectedRevision: 5,
      invalidationIds: ["script-inv"],
      reason: "本镜头继续使用原对白与人物构图",
    },
  ]);
});

it("P6 persists unknown script review and only replays original explicit intent on request", async () => {
  const ctx = context();
  let stored: any = structuredClone(production);
  stored.invalidations = [
    { id: "script-inv", kind: "script", reason: "批准剧本更新" },
  ];
  vi.mocked(get).mockImplementation(async (p) =>
    p.endsWith("/production")
      ? structuredClone(stored)
      : p.endsWith("/reference-jobs")
        ? { jobs: [] }
        : { sources: [] },
  );
  vi.mocked(post).mockRejectedValueOnce(new Error("连接中断，结果未知"));
  const view = render(<ProductionPanel ctx={ctx} mode="shots" />);
  await screen.findByText(/剧本与对白/);
  fireEvent.click(screen.getByRole("checkbox", { name: /剧本与对白/ }));
  fireEvent.change(screen.getByLabelText("接受旧依赖的原因"), {
    target: { value: "保留原剧情节奏" },
  });
  fireEvent.click(
    screen.getByRole("button", { name: "确认保留所选旧剧本对白" }),
  );
  await waitFor(() =>
    expect(ctx.notify).toHaveBeenCalledWith("连接中断，结果未知", "error"),
  );
  const original = vi.mocked(post).mock.calls[0];
  view.unmount();
  render(<ProductionPanel ctx={ctx} mode="shots" />);
  await screen.findByText(/复核请求尚未确认/);
  expect(post).toHaveBeenCalledTimes(1);
  vi.mocked(post).mockImplementation(async () => {
    stored.invalidations = [];
    stored.revision = 5;
    return { revision: 5 };
  });
  fireEvent.click(
    screen.getByRole("button", { name: "按原请求键核查复核结果" }),
  );
  await waitFor(() => expect(vi.mocked(post).mock.calls[1]).toEqual(original));
  await waitFor(() =>
    expect(screen.queryByText(/复核请求尚未确认/)).not.toBeInTheDocument(),
  );
});

describe('MF01/02/04 image workflow closure', () => {
  it('autofills the shot static draft and exact approved actor reference', async () => {
    const ctx = context(); ctx.params = { target: 'shot:shot-r4' };
    render(<ProductionPanel ctx={ctx} mode="images" />);
    await waitFor(() => expect(screen.getByLabelText('英文编辑要求（实际发给模型）')).toHaveValue('双人对峙。人物：悟空'));
    expect(screen.getByRole('button', { name: '输入图 1 · 必选，决定画幅：goku.png' })).toBeInTheDocument();
    expect(screen.getByText('1. 悟空 · v2')).toBeInTheDocument();
    expect(screen.getByLabelText('英文编辑要求（实际发给模型）')).not.toHaveValue(expect.stringContaining('凝视'));
    expect(post).not.toHaveBeenCalled();
  });
  it('does not replace manual prompt when the shot request resolves late', async () => {
    const ctx = context(); ctx.params = { target: 'shot:shot-r4' };
    let complete: (value: any) => void = () => {};
    vi.mocked(get).mockImplementation(async p => p.endsWith('/production') ? new Promise(resolve => { complete = resolve; }) : { jobs: [], sources: [] });
    render(<ProductionPanel ctx={ctx} mode="images" />);
    fireEvent.change(screen.getByLabelText('英文提示词（实际发给模型）'), { target: { value: '我正在手动写的画面' } });
    complete(structuredClone(production));
    await waitFor(() => expect(screen.getByRole('button', { name: '提交图片制作' })).toBeEnabled());
    expect(screen.getByLabelText('英文编辑要求（实际发给模型）')).toHaveValue('我正在手动写的画面');
    expect(screen.getByRole('button', { name: '输入图 1 · 必选，决定画幅：goku.png' })).toBeInTheDocument();
    vi.mocked(post).mockResolvedValue({ job: { id: 'edited-late-shot', state: 'QUEUED', candidates: [] } });
    fireEvent.click(screen.getByRole('button', { name: '提交图片制作' }));
    await waitFor(() => expect(post).toHaveBeenCalledWith('/shot-revisions/shot-r4/reference-jobs', expect.objectContaining({ tool: 'edit_image', parameters: expect.objectContaining({ prompt: '我正在手动写的画面', asset_ids: ['actor-image'] }) }), 'drama-ui-test-explicit-request-key'));
  });
  it('restores separate drafts when switching targets and after remount', async () => {
    const ctx = context(); ctx.params = { target: 'entity:actor-r2' };
    const ui = render(<ProductionPanel ctx={ctx} mode="images" />);
    fireEvent.change(screen.getByLabelText('英文提示词（实际发给模型）'), { target: { value: '人物手工草稿' } });
    ui.rerender(<ProductionPanel ctx={{ ...ctx, params: { target: 'shot:shot-r4' } }} mode="images" />);
    const shotPrompt = await screen.findByLabelText('英文编辑要求（实际发给模型）');
    fireEvent.change(shotPrompt, { target: { value: '镜头手工草稿' } });
    ui.rerender(<ProductionPanel ctx={{ ...ctx, params: { target: 'entity:actor-r2' } }} mode="images" />);
    expect(screen.getByLabelText('英文提示词（实际发给模型）')).toHaveValue('人物手工草稿');
    ui.unmount();
    render(<ProductionPanel ctx={{ ...ctx, params: {} }} mode="images" />);
    expect(screen.getByLabelText('英文提示词（实际发给模型）')).toHaveValue('人物手工草稿');
    expect(post).not.toHaveBeenCalled();
  });
  it('keeps the durable receipt across remount and requires explicit new group intent', async () => {
    const ctx = context(); ctx.params = { target: 'entity:actor-r2' };
    vi.mocked(post).mockResolvedValue({ job: { id: 'receipt-job', state: 'QUEUED', tool: 'generate_image', targetType: 'entity', targetId: 'actor', candidates: [] } });
    const ui = render(<ProductionPanel ctx={ctx} mode="images" />);
    fireEvent.click(screen.getByRole('button', { name: '提交图片制作' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '本组已提交' })).toBeDisabled());
    expect(post).toHaveBeenCalledTimes(1);
    ui.unmount();
    render(<ProductionPanel ctx={{ ...ctx, params: {} }} mode="images" />);
    expect(screen.getByRole('button', { name: '本组已提交' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '查看原任务' }));
    expect(ctx.navigate).toHaveBeenCalledWith('tasks', { kind: 'image', jobId: 'receipt-job' });
    expect(post).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: '再生成一组' }));
    expect(screen.getByRole('button', { name: '提交图片制作' })).toBeEnabled();
    expect(post).toHaveBeenCalledTimes(1);
  });
  it('shows every required identity and blocks Qwen instead of truncating over three inputs', async () => {
    const ctx = context(); ctx.params = { target: 'shot:shot-r4' };
    ctx.entities = Array.from({ length: 4 }, (_, i) => ({ id: `e${i}`, kind: 'character', approvedRevisionId: `r${i}`, revisions: [{ id: `r${i}`, name: `角色${i}`, revision: 1, referenceAssetId: `a${i}` }] }));
    const many = structuredClone(production); many.current.snapshot.characterRevisionIds = ['r0', 'r1', 'r2', 'r3'];
    vi.mocked(get).mockImplementation(async p => p.endsWith('/production') ? many : { jobs: [], sources: [] });
    render(<ProductionPanel ctx={ctx} mode="images" />);
    await screen.findByLabelText('英文编辑要求（实际发给模型）');
    expect(screen.getByText(/当前镜头需要 4 张参考/)).toBeInTheDocument();
    expect(screen.getByText('4. 角色3 · v1')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '提交图片制作' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '返回镜头核查参考' }));
    expect(ctx.navigate).toHaveBeenCalledWith('shots', { shotId: 'shot' });
    expect(post).not.toHaveBeenCalled();
  });
  it('sends the stored shot prompt into video navigation', async () => {
    const ctx = context(); const approved = structuredClone(production) as any;
    approved.current.snapshot.approvedReferenceAssetId = 'actor-image';
    vi.mocked(get).mockImplementation(async p => p.endsWith('/production') ? approved : { jobs: [], sources: [] });
    render(<ProductionPanel ctx={ctx} mode="shots" />);
    fireEvent.click(await screen.findByRole('button', { name: '下一步：H3 Ring4 生成' }));
    const prompt = vi.mocked(ctx.navigate).mock.calls.at(-1)?.[1].prompt as string;
    expect(prompt).toContain("subject_definitions:");
    expect(prompt).toContain("<Picture 1>");
    expect(prompt).toContain("[Shot 1] 双人对峙");
    expect(post).not.toHaveBeenCalled();
  });
  it("compiles an official prompt from reference nodes without submitting video", async () => {
    const ctx = context();
    const shot = structuredClone(production) as any;
    shot.current.snapshot.referenceNodes = [
      { id: "person", media: "image", assetId: "sheet", role: "character", description: "Keep the face." },
      { id: "place", media: "image", assetId: "palace", role: "scene", description: "Palace stage." },
      { id: "move", media: "video", assetId: "turn", role: "action", description: "Turn cadence only." },
    ];
    shot.current.snapshot.prompt = "Turn once and settle.";
    ctx.assets = [
      { id: "sheet", kind: "image", filename: "天舞.png" },
      { id: "palace", kind: "image", filename: "天宫.png" },
      { id: "turn", kind: "video", filename: "转圈.mp4" },
    ];
    vi.mocked(get).mockImplementation(async p => p.endsWith("/production") ? shot : { jobs: [], sources: [] });
    render(<ProductionPanel ctx={ctx} mode="shots" />);
    expect(await screen.findByLabelText("视频生成提示词")).toHaveValue("Turn once and settle.");
    fireEvent.click(screen.getByRole("button", { name: "预览提示词" }));
    const preview = await screen.findByLabelText("H3 提示词预览");
    for (const section of ["subject_definitions:", "summary:", "retention_analysis:", "detailed_description:", "overall_soundscape:", "non_diegetic_music:"]) {
      expect(preview.textContent).toContain(section);
    }
    expect(preview.textContent).toContain("<Subject 1> is the person in <Picture 1>");
    expect(preview.textContent).toContain("<Video 1>");
    expect(screen.getByLabelText("视频生成提示词")).toHaveValue("Turn once and settle.");
    expect(post).not.toHaveBeenCalled();
    expect(ctx.navigate).not.toHaveBeenCalled();
  });
  it('carries a finished Ref2VA contract into video navigation instead of the action note', async () => {
    const ctx = context();
    const approved = structuredClone(production) as any;
    const contract = ["subject_definitions:", "<Picture 1> identity.", "summary:", "One shot.", "retention_analysis:", "Keep the face.", "detailed_description:", "Dance once.", "overall_soundscape:", "Silk only.", "non_diegetic_music:", "No added score."].join("\n");
    approved.current.snapshot.prompt = contract;
    approved.current.snapshot.approvedReferenceAssetId = "actor-image";
    vi.mocked(get).mockImplementation(async p => p.endsWith("/production") ? approved : { jobs: [], sources: [] });
    render(<ProductionPanel ctx={ctx} mode="shots" />);
    fireEvent.click(await screen.findByRole("button", { name: "下一步：H3 Ring4 生成" }));
    expect(ctx.navigate).toHaveBeenCalledWith("video", { shotId: "shot", prompt: contract });
  });
});

describe('MF01 real-page task result navigation regression', () => {
  it('labels an accepted PREPARED task as saved and waiting to execute', async () => {
    vi.mocked(get).mockImplementation(async p => p.endsWith('/reference-jobs') ? { jobs: [{ id: 'queued-image', state: 'PREPARED', targetType: 'entity', targetId: 'actor', tool: 'generate_image', candidates: [] }] } : { sources: [] });
    render(<ProductionPanel ctx={context()} mode="images" />);
    expect(await screen.findByText('已保存，等待执行')).toBeInTheDocument();
    expect(screen.queryByText('准备提交')).not.toBeInTheDocument();
  });
  it('locates the requested old result while retaining the new draft and most recent submission receipt', async () => {
    const ctx = context(); ctx.params = { target: 'entity:actor-r2' };
    vi.mocked(post).mockResolvedValue({ job: { id: 'latest-canceled', state: 'CANCELED', targetType: 'entity', targetId: 'actor', tool: 'generate_image', candidates: [] } });
    const ui = render(<ProductionPanel ctx={ctx} mode="images" />);
    fireEvent.change(screen.getByLabelText('英文提示词（实际发给模型）'), { target: { value: '保留的新草稿意图' } });
    fireEvent.click(screen.getByRole('button', { name: '提交图片制作' }));
    await screen.findByRole('button', { name: '本组已提交' });
    ui.unmount();
    const scroll = vi.fn();
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: scroll });
    vi.mocked(get).mockImplementation(async p => p.endsWith('/reference-jobs') ? { jobs: [
      { id: 'latest-canceled', state: 'CANCELED', targetType: 'entity', targetId: 'actor', tool: 'generate_image', candidates: [] },
      { id: 'earlier-success', state: 'SUCCEEDED', targetType: 'entity', targetId: 'actor', tool: 'generate_image', candidates: [{ id: 'candidate-old', assetId: 'actor-image' }] },
    ] } : { sources: [] });
    render(<ProductionPanel ctx={{ ...ctx, params: { jobId: 'earlier-success' } }} mode="images" />);
    const selected = await screen.findByRole('region', { name: '图片任务 earlier-success' });
    expect(selected).toHaveAttribute('data-viewed', 'true');
    expect(screen.getByText('正在查看任务 earlier-success · 1 张候选')).toBeInTheDocument();
    expect(scroll).toHaveBeenCalledTimes(1);
    expect(scroll.mock.contexts[0]).toBe(selected);
    expect(screen.getByLabelText('英文提示词（实际发给模型）')).toHaveValue('保留的新草稿意图');
    fireEvent.click(screen.getByRole('button', { name: '查看原任务' }));
    expect(ctx.navigate).toHaveBeenCalledWith('tasks', { kind: 'image', jobId: 'latest-canceled' });
    expect(post).toHaveBeenCalledTimes(1);
  });
});

describe('MF05 multi-candidate image controls and comparison', () => {
 const batchJob = (overrides: any = {}) => ({ id: 'multi-job', targetType: 'entity', targetId: 'actor', targetRevisionId: 'actor-r2', tool: 'generate_image', state: 'RUNNING', memoryReleased: false,
  imageBatch: { candidateCount: 4, parallelLimit: 2, executionMode: 'gpu_batch', candidates: [{ index: 0, seed: 42, state: 'SUCCEEDED' }, { index: 1, seed: 43, state: 'SUCCEEDED' }, { index: 2, seed: 44, state: 'RUNNING' }, { index: 3, seed: 45, state: 'QUEUED' }] },
  candidates: [{ id: 'c0', assetId: 'actor-image', index: 0, seed: 42, state: 'SUCCEEDED' }, { id: 'c1', assetId: 'another-image', index: 1, seed: 43, state: 'SUCCEEDED' }], ...overrides });
 const showJob = (job: any) => vi.mocked(get).mockImplementation(async p => p.endsWith('/reference-jobs') ? { jobs: [job] } : p.endsWith('/production') ? production : { sources: [] });
 it('submits text-to-image candidate count and per-batch limit separately', async () => {
  const ctx = context(); ctx.params = { target: 'entity:actor-r2' };
  vi.mocked(post).mockResolvedValue({ job: batchJob({ state: 'PREPARED' }) });
  render(<ProductionPanel ctx={ctx} mode="images" />);
  fireEvent.change(screen.getByLabelText('候选数量'), { target: { value: '4' } });
  fireEvent.change(screen.getByLabelText('每批并行数量'), { target: { value: '2' } });
  fireEvent.click(screen.getByRole('button', { name: '提交图片制作' }));
  await waitFor(() => expect(post).toHaveBeenCalledWith('/entity-revisions/actor-r2/reference-jobs', expect.objectContaining({ tool: 'generate_image', parameters: expect.objectContaining({ batch_count: 4, parallelism: 2 }) }), 'drama-ui-test-explicit-request-key'));
 });
 it('caps effective parallel limit at candidate count and rejects unsupported requests', () => {
  const form = { prompt: '图', width: 1024, height: 1024, seed: 42, count: 1, parallelism: 2 };
  expect(imageParameters('generate_image', form)).toMatchObject({ batch_count: 1, parallelism: 1 });
  expect(imageParameters('generate_image', { ...form, count: 20, parallelism: 4 })).toMatchObject({ batch_count: 20, parallelism: 4 });
  for (const count of [0, -1, 1.5, '', Infinity, true]) expect(() => imageParameters('generate_image', { ...form, count })).toThrow('候选数量');
  expect(imageParameters('generate_image', { ...form, count: 4, parallelism: 10 })).toMatchObject({ batch_count: 4, parallelism: 4 });
  for (const parallelism of [0, -1, 1.5, '', Infinity, true]) expect(() => imageParameters('generate_image', { ...form, parallelism })).toThrow('每批并行数量');
  expect(imageParameters('edit_image', { ...form, count: 4, refs: ['actor-image'], steps: 28, encoder: 'standard', precision: 'fp8' })).toMatchObject({ batch_count: 4, parallelism: 2 });
 });
 it('does not claim observed parallelism from requested limit and previews completed partial results', async () => {
  showJob(batchJob()); render(<ProductionPanel ctx={context()} mode="images" />);
  await screen.findByText('候选总数 4 · 请求每批上限 2 张 · 实际并行尚未观测');
  expect(screen.queryByText(/已观测有效并行/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '放大候选 1' }));
  expect(screen.getByRole('dialog', { name: '查看候选大图' })).toBeInTheDocument();
  expect(screen.getByRole('img', { name: '候选 1 大图' })).toHaveAttribute('src', '/dgx-drama/projects/qa/assets/actor-image/media');
  fireEvent.click(screen.getByRole('button', { name: '关闭图片查看' }));
  expect(post).not.toHaveBeenCalled();
 });
 it('opens read-only two-image comparison with actual candidate seeds and closes using Escape', async () => {
  const job = batchJob(); job.imageBatch.observedParallelism = 2; showJob(job);
  render(<ProductionPanel ctx={context()} mode="images" />);
  await screen.findByText('候选总数 4 · 请求每批上限 2 张 · 已观测有效并行 2 张');
  fireEvent.click(screen.getByRole('checkbox', { name: '对比候选 1' }));
  fireEvent.click(screen.getByRole('checkbox', { name: '对比候选 2' }));
  fireEvent.click(screen.getByRole('button', { name: '对比已选图片（2/4）' }));
  expect(screen.getByRole('dialog', { name: '候选图片对比' })).toBeInTheDocument();
  expect(screen.getByText('候选 1 · 种子 42')).toBeInTheDocument();
  expect(screen.getByText('候选 2 · 种子 43')).toBeInTheDocument();
  await userEvent.setup().keyboard('{Escape}');
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(post).not.toHaveBeenCalled();
 });
 it('changes a comparison selection from A to B without retaining A as current', async () => {
  const job = batchJob({ candidates: [
    { id: 'candidate-a', assetId: 'asset-a', index: 0, seed: 42, state: 'SUCCEEDED' },
    { id: 'candidate-b', assetId: 'asset-b', index: 1, seed: 43, state: 'SUCCEEDED' },
  ] });
  showJob(job);
  vi.mocked(get).mockImplementation(async p => p.endsWith('/reference-jobs') ? { jobs: [job] } : p.endsWith('/entities/actor') ? { entity: structuredClone(actor) } : { sources: [] });
  vi.mocked(post).mockImplementation(async (_path, body: any) => ({ entity: { ...actor, revision: 3, current: { ...actor.revisions[0], references: [{ role: 'identity_front', assetId: body.referenceAssetId }] } } }));
  const ctx = context(); render(<ProductionPanel ctx={ctx} mode="images" />);
  await screen.findByRole('checkbox', { name: '对比候选 1' });
  fireEvent.click(screen.getByRole('checkbox', { name: '对比候选 1' }));
  fireEvent.click(screen.getByRole('checkbox', { name: '对比候选 2' }));
  fireEvent.click(screen.getByRole('button', { name: '对比已选图片（2/4）' }));
  const dialog = screen.getByRole('dialog', { name: '候选图片对比' });
  const figures = Array.from(dialog.querySelectorAll('figure'));
  expect(figures[0].querySelector('button')).not.toBeDisabled();
  fireEvent.click(figures[0].querySelector('button')!);
  await waitFor(() => expect(post).toHaveBeenCalledWith('/entities/actor/revisions', expect.objectContaining({ candidateId: 'candidate-a' }), expect.any(String)));
  expect(figures[0].textContent).toContain('当前已选');
  fireEvent.click(figures[1].querySelector('button')!);
  await waitFor(() => expect(post).toHaveBeenCalledWith('/entities/actor/revisions', expect.objectContaining({ candidateId: 'candidate-b' }), expect.any(String)));
  expect(figures[0].textContent).not.toContain('当前已选');
  expect(figures[1].textContent).toContain('当前已选');
 });
 it('shows no gallery candidate as selected when the authoritative asset is outside the gallery', async () => {
  const job = batchJob({ candidates: [
    { id: 'candidate-a', assetId: 'asset-a', index: 0, seed: 42, state: 'SUCCEEDED' },
    { id: 'candidate-b', assetId: 'asset-b', index: 1, seed: 43, state: 'SUCCEEDED' },
  ] });
  vi.mocked(get).mockImplementation(async p => p.endsWith('/reference-jobs') ? { jobs: [job] } : p.endsWith('/entities/actor') ? { entity: structuredClone(actor) } : { sources: [] });
  render(<ProductionPanel ctx={context()} mode="images" />);
  await screen.findByRole('checkbox', { name: '对比候选 1' });
  fireEvent.click(screen.getByRole('checkbox', { name: '对比候选 1' }));
  fireEvent.click(screen.getByRole('checkbox', { name: '对比候选 2' }));
  fireEvent.click(screen.getByRole('button', { name: '对比已选图片（2/4）' }));
  await waitFor(() => expect(get).toHaveBeenCalledWith('/entities/actor'));
  await waitFor(() => expect(screen.queryByText('当前已选')).not.toBeInTheDocument());
  expect(screen.getAllByRole('button', { name: '选为资料参考草稿' })).toHaveLength(2);
 });
 it('limits comparison to four images without selecting or approving any reference', async () => {
  const job = batchJob({ candidates: Array.from({ length: 5 }, (_, index) => ({ id: `c${index}`, assetId: `asset${index}`, index, seed: 42 + index, state: 'SUCCEEDED' })) }); showJob(job);
  render(<ProductionPanel ctx={context()} mode="images" />); await screen.findByRole('checkbox', { name: '对比候选 5' });
  for (let i = 1; i <= 4; i++) fireEvent.click(screen.getByRole('checkbox', { name: `对比候选 ${i}` }));
  expect(screen.getByRole('checkbox', { name: '对比候选 5' })).toBeDisabled();
  expect(post).not.toHaveBeenCalled();
 });
 it('preserves legacy candidate images without inventing a seed or concurrency observation', async () => {
  showJob({ ...batchJob(), state: 'SUCCEEDED', imageBatch: undefined, candidates: [{ id: 'old', assetId: 'actor-image' }] });
  render(<ProductionPanel ctx={context()} mode="images" />);
  await screen.findByText('历史任务未记录实际并行信息，原候选仍可查看。');
  expect(screen.getByRole('button', { name: '放大候选 1' })).toBeEnabled();
  expect(screen.getByText('候选 1 · 种子 历史未记录 · 已归档')).toBeInTheDocument();
 });
 it('retries only selected definite failures and recovers the exact frozen subset after unknown response/remount', async () => {
  const job = batchJob({ state: 'FAILED', memoryReleased: true, candidates: [{ id: 'c0', assetId: 'actor-image', index: 0, seed: 42, state: 'SUCCEEDED' }] });
  job.imageBatch.candidates[1].state = 'FAILED'; job.imageBatch.candidates[2].state = 'UNKNOWN'; job.imageBatch.candidates[3].state = 'CANCELED'; showJob(job);
  vi.mocked(post).mockRejectedValueOnce(new Error('连接中断'));
  const ui = render(<ProductionPanel ctx={context()} mode="images" />);
  await screen.findByRole('checkbox', { name: '重拍候选 2' });
  expect(screen.queryByRole('checkbox', { name: '重拍候选 1' })).not.toBeInTheDocument();
  expect(screen.queryByRole('checkbox', { name: '重拍候选 3' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('checkbox', { name: '重拍候选 2' }));
  fireEvent.click(screen.getByRole('checkbox', { name: '重拍候选 4' }));
  fireEvent.click(screen.getByRole('button', { name: '重拍所选失败候选' }));
  fireEvent.click(screen.getByRole('button', { name: '按原请求键核查重拍结果' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '按原请求键核查重拍结果' })).toBeEnabled());
  expect(post).toHaveBeenCalledTimes(1);
  expect(vi.mocked(post).mock.calls[0]).toEqual(['/reference-jobs/multi-job/retry', { candidateIndices: [1, 3] }, 'drama-ui-test-explicit-request-key']);
  ui.unmount(); vi.mocked(post).mockResolvedValue({ job: { ...batchJob(), id: 'retry-child', state: 'PREPARED' } });
  render(<ProductionPanel ctx={context()} mode="images" />);
  await screen.findByRole('button', { name: '按原请求键核查重拍结果' });
  expect(post).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole('button', { name: '按原请求键核查重拍结果' }));
  await screen.findByRole('button', { name: '查看重拍任务 · 候选 2、4' });
  expect(vi.mocked(post).mock.calls[1]).toEqual(vi.mocked(post).mock.calls[0]);
 });
 it.each(['PREPARED', 'QUEUED', 'RUNNING'])('shows saved planned counts before %s obtains execution metadata', async state => {
  showJob(batchJob({ state, imageBatch: undefined, parameters: { batch_count: 2, parallelism: 2 } }));
  render(<ProductionPanel ctx={context()} mode="images" />);
  await screen.findByText('计划制作2张，每批上限2张；等待执行后显示实测并行信息');
  expect(screen.queryByText(/历史任务未记录/)).not.toBeInTheDocument();
  expect(screen.queryByText(/已观测有效并行/)).not.toBeInTheDocument();
 });
 it('uses server children across fresh browser storage to exclude previously retried indices and retain child navigation', async () => {
  const parent = batchJob({ state: 'FAILED', memoryReleased: true, candidates: [], imageBatch: { candidateCount: 8, parallelLimit: 2, candidates: [4,5,6,7].map(index => ({ index, seed: 42 + index, state: 'CANCELED' })) } });
  const child = { id: 'other-ui-child', retryOf: parent.id, state: 'PREPARED', parameters: { candidate_indices: [4, 5] }, candidates: [] };
  vi.mocked(get).mockImplementation(async p => p.endsWith('/reference-jobs') ? { jobs: [parent, child] } : { sources: [] });
  const ctx = context(); render(<ProductionPanel ctx={ctx} mode="images" />);
  await screen.findByRole('button', { name: '查看重拍任务 · 候选 5、6' });
  expect(screen.queryByRole('checkbox', { name: '重拍候选 5' })).not.toBeInTheDocument();
  expect(screen.queryByRole('checkbox', { name: '重拍候选 6' })).not.toBeInTheDocument();
  expect(screen.getByRole('checkbox', { name: '重拍候选 7' })).toBeEnabled();
  expect(screen.getByRole('checkbox', { name: '重拍候选 8' })).toBeEnabled();
  fireEvent.click(screen.getByRole('button', { name: '查看重拍任务 · 候选 5、6' }));
  expect(ctx.navigate).toHaveBeenCalledWith('tasks', { kind: 'image', jobId: child.id }); expect(post).not.toHaveBeenCalled();
 });
 it('displays authoritative seedText beyond JS integer precision in status, cards and comparison', async () => {
  const exact = '18446744073709551615', job = batchJob();
  job.imageBatch.candidates[0] = { index: 0, seed: 18446744073709551615, seedText: exact, state: 'SUCCEEDED' };
  job.candidates[0] = { ...job.candidates[0], seed: 18446744073709551615, seedText: exact }; showJob(job);
  render(<ProductionPanel ctx={context()} mode="images" />); await screen.findByRole('checkbox', { name: '对比候选 1' });
  expect(screen.getAllByText(new RegExp(exact))).toHaveLength(2);
  fireEvent.click(screen.getByRole('checkbox', { name: '对比候选 1' })); fireEvent.click(screen.getByRole('checkbox', { name: '对比候选 2' }));
  fireEvent.click(screen.getByRole('button', { name: '对比已选图片（2/4）' }));
  expect(screen.getByText(`候选 1 · 种子 ${exact}`)).toBeInTheDocument();
  expect(screen.queryByText(/18446744073709552000/)).not.toBeInTheDocument();
 });
 it('allows a different failed subset after a successful child receipt, retaining both child links and fresh keys', async () => {
  const job = batchJob({ state: 'FAILED', memoryReleased: true }); job.imageBatch.candidates[1].state = 'FAILED'; job.imageBatch.candidates[3].state = 'CANCELED'; showJob(job);
  vi.mocked(requestKey).mockReturnValueOnce('drama-ui-subset-first').mockReturnValueOnce('drama-ui-subset-second');
  vi.mocked(post).mockResolvedValueOnce({ job: { id: 'child-first', state: 'PREPARED', candidates: [] } }).mockResolvedValueOnce({ job: { id: 'child-second', state: 'PREPARED', candidates: [] } });
  const ctx = context(); render(<ProductionPanel ctx={ctx} mode="images" />);
  fireEvent.click(await screen.findByRole('checkbox', { name: '重拍候选 2' }));
  fireEvent.click(screen.getByRole('button', { name: '重拍所选失败候选' }));
  await screen.findByRole('button', { name: '查看重拍任务 · 候选 2' });
  expect(screen.queryByRole('checkbox', { name: '重拍候选 2' })).not.toBeInTheDocument();
  expect(screen.getByRole('checkbox', { name: '重拍候选 4' })).toBeEnabled();
  fireEvent.click(screen.getByRole('checkbox', { name: '重拍候选 4' }));
  fireEvent.click(screen.getByRole('button', { name: '重拍所选失败候选' }));
  await screen.findByRole('button', { name: '查看重拍任务 · 候选 4' });
  expect(vi.mocked(post).mock.calls).toEqual([
   ['/reference-jobs/multi-job/retry', { candidateIndices: [1] }, 'drama-ui-subset-first'],
   ['/reference-jobs/multi-job/retry', { candidateIndices: [3] }, 'drama-ui-subset-second'],
  ]);
  fireEvent.click(screen.getByRole('button', { name: '查看重拍任务 · 候选 2' }));
  expect(ctx.navigate).toHaveBeenCalledWith('tasks', { kind: 'image', jobId: 'child-first' });
  expect(screen.queryByRole('button', { name: '重拍所选失败候选' })).not.toBeInTheDocument();
 });
 it('recovers a known saved image only on explicit click without requesting another generation', async () => {
  const job = batchJob({ state: 'FAILED', memoryReleased: true }); job.imageBatch.candidates[2] = { index: 2, seed: 44, state: 'UNKNOWN', error: 'Saved image download requires recovery' }; showJob(job);
  vi.mocked(post).mockResolvedValue({ job: { ...job, imageBatch: { ...job.imageBatch, candidates: [] } } });
  render(<ProductionPanel ctx={context()} mode="images" />);
  const recover = await screen.findByRole('button', { name: '恢复已生成图片' });
  expect(post).not.toHaveBeenCalled();
  expect(screen.queryByRole('checkbox', { name: '重拍候选 3' })).not.toBeInTheDocument();
  fireEvent.click(recover);
  await waitFor(() => expect(post).toHaveBeenCalledExactlyOnceWith('/reference-jobs/multi-job/recover', {}));
  await waitFor(() => expect(screen.queryByRole('button', { name: '恢复已生成图片' })).not.toBeInTheDocument());
 });
 it.each([
  { state: 'RUNNING', memoryReleased: true, error: 'Saved image download requires recovery' },
  { state: 'FAILED', memoryReleased: false, error: 'Saved image download requires recovery' },
  { state: 'UNKNOWN', memoryReleased: true, error: 'Saved image download requires recovery' },
  { state: 'FAILED', memoryReleased: true, error: 'Gateway response unknown' },
 ])('does not offer saved recovery for unsafe or unrelated UNKNOWN: %j', async ({ state, memoryReleased, error }) => {
  const job = batchJob({ state, memoryReleased }); job.imageBatch.candidates[2] = { index: 2, seed: 44, state: 'UNKNOWN', error }; showJob(job);
  render(<ProductionPanel ctx={context()} mode="images" />); await screen.findByRole('region', { name: '图片任务 multi-job' });
  expect(screen.queryByRole('button', { name: '恢复已生成图片' })).not.toBeInTheDocument(); expect(post).not.toHaveBeenCalled();
 });
 it('does not offer retry until backend confirms memory release', async () => {
  const job = batchJob({ state: 'FAILED' }); job.imageBatch.candidates[1].state = 'FAILED'; showJob(job);
  render(<ProductionPanel ctx={context()} mode="images" />); await screen.findByRole('region', { name: '图片任务 multi-job' });
  expect(screen.queryByRole('checkbox', { name: '重拍候选 2' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '重拍所选失败候选' })).not.toBeInTheDocument();
 });
});


it('sends native LLaDA editing with one source and parallel candidates without Qwen-only settings', () => {
  const form = {editModel: 'llada-image-turbo', prompt: 'change stance', refs: ['reference', '', ''],
    width: 1344, height: 768, seed: 42, count: 2, parallelism: 2, steps: 28, encoder: 'standard', precision: 'fp8'};
  expect(imageParameters('edit_image', form)).toEqual({model: 'llada-image-turbo', prompt: 'change stance',
    asset_ids: ['reference'], width: 1344, height: 768, seed: 42, steps: 4, batch_count: 2, parallelism: 2});
  expect(() => imageParameters('edit_image', {...form, refs: ['a', 'b', '']})).toThrow('一张');
  expect(() => imageParameters('edit_image', {...form, width: 528})).toThrow('32');
});

it('switches Qwen and LLaDA editing controls while keeping the prompt and never submits on selection', async () => {
  render(<ProductionPanel ctx={context()} mode="images" />);
  const user = userEvent.setup();
  fireEvent.keyDown(screen.getByRole('combobox', {name: '图片工具'}), {key: 'ArrowDown'});
  fireEvent.click(screen.getByRole('option', {name: '参考图编辑 · 选择模型'}));
  fireEvent.change(screen.getByLabelText('英文编辑要求（实际发给模型）'), {target: {value: '保留人物，只修改动作'}});
  expect(screen.getByLabelText('采样步数（20–40）')).toBeVisible();
  fireEvent.keyDown(screen.getByRole('combobox', {name: '参考图编辑模型'}), {key: 'ArrowDown'});
  fireEvent.click(screen.getByRole('option', {name: 'LLaDA Image Turbo · 单图编辑 · 4 步'}));
  expect(screen.getByRole('button', {name: '3:4 人像'})).toBeVisible();
  fireEvent.click(screen.getByText('自定义像素（高级）'));
  expect(screen.getByLabelText('宽度（像素）')).toBeVisible();
  expect(screen.queryByLabelText('采样步数（20–40）')).toBeNull();
  expect(screen.queryByRole('button', {name: /输入图 2/})).toBeNull();
  fireEvent.keyDown(screen.getByRole('combobox', {name: '参考图编辑模型'}), {key: 'ArrowDown'});
  fireEvent.click(screen.getByRole('option', {name: 'Qwen Image Edit 2511 · 多图组合'}));
  expect(screen.getByLabelText('采样步数（20–40）')).toHaveValue(28);
  expect(screen.getByLabelText('英文编辑要求（实际发给模型）')).toHaveValue('保留人物，只修改动作');
  expect(post).not.toHaveBeenCalled();
});

it('recovers image context after the refreshed shot list includes the requested revision', async () => {
  const full = context(); full.params = { target: 'shot:shot-r4' };
  const view = render(<ProductionPanel ctx={{...full,shots:[]}} mode="images" />);
  await screen.findByText('目标修订已不在当前镜头列表，请重新选择制作目标。');
  view.rerender(<ProductionPanel ctx={full} mode="images" />);
  await waitFor(()=>expect(screen.getByLabelText('英文编辑要求（实际发给模型）')).toHaveValue('双人对峙。人物：悟空'));
  expect(screen.queryByText('目标修订已不在当前镜头列表，请重新选择制作目标。')).not.toBeInTheDocument();
  expect(post).not.toHaveBeenCalled();
});
