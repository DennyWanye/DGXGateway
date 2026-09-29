// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { BackupPanel } from "./BackupPanel";
import { get, post, uploadRestore } from "@/lib/api";
vi.mock("@/lib/api", () => ({
  get: vi.fn(),
  post: vi.fn(),
  uploadRestore: vi.fn(),
  requestKey: () => "backup-original-key",
  segment: encodeURIComponent,
  apiUrl: (p: string) => "/dgx-drama" + p,
}));
const ctx = () => ({
  project: { id: "p", title: "备份测试", revision: 3 },
  episode: { id: "e" },
  assets: [],
  shots: [],
  entities: [],
  capabilities: { backupEnabled: true },
  refresh: vi.fn(),
  notify: vi.fn(),
  navigate: vi.fn(),
  openCanvas: vi.fn(),
  params: {},
});
beforeEach(() => {
  sessionStorage.clear();
  vi.clearAllMocks();
  vi.mocked(get).mockResolvedValue({ operations: [] });
  vi.mocked(post).mockResolvedValue({ operation: { id: "op" } });
});
afterEach(cleanup);
it("backs up only current project with frozen CAS and explicit key", async () => {
  render(<BackupPanel ctx={ctx()} />);
  fireEvent.click(screen.getByRole("button", { name: "创建项目备份" }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      "/projects/p/backups",
      { expectedProjectRevision: 3 },
      "backup-original-key",
    ),
  );
  expect(post).toHaveBeenCalledTimes(1);
});
it("unknown backup survives remount without automatic write and reuses exact key", async () => {
  vi.mocked(post).mockRejectedValueOnce(new Error("连接中断"));
  const context = ctx(),
    view = render(<BackupPanel ctx={context} />);
  fireEvent.click(screen.getByRole("button", { name: "创建项目备份" }));
  await waitFor(() =>
    expect(context.notify).toHaveBeenCalledWith("连接中断", "error"),
  );
  const original = vi.mocked(post).mock.calls[0];
  view.unmount();
  render(<BackupPanel ctx={ctx()} />);
  expect(post).toHaveBeenCalledTimes(1);
  fireEvent.click(
    screen.getByRole("button", { name: "按原请求键核查归档操作" }),
  );
  await waitFor(() => expect(vi.mocked(post).mock.calls[1]).toEqual(original));
});
it("upload validation is separate from explicit restore and preview persists", async () => {
  vi.mocked(uploadRestore).mockResolvedValue({
    upload: {
      id: "upload1",
      bytes: 40,
      preview: {
        projectId: "old-p",
        assetCount: 2,
        tables: { dr_projects: 1 },
        snapshotKind: "recovery_required",
      },
    },
  });
  const view = render(<BackupPanel ctx={ctx()} />);
  const file = new File(["tiny"], "project.drama.zip");
  fireEvent.change(screen.getByLabelText("项目备份文件"), {
    target: { files: [file] },
  });
  fireEvent.click(screen.getByRole("button", { name: "上传并检查项目包" }));
  await screen.findByText("项目包已检查");
  expect(uploadRestore).toHaveBeenCalledWith(file, "backup-original-key");
  expect(post).not.toHaveBeenCalled();
  view.unmount();
  render(<BackupPanel ctx={ctx()} />);
  expect(screen.getByText(/原项目 old-p/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "恢复已检查项目包" }));
  expect(post).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "确认继续" }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      "/restores",
      { uploadId: "upload1" },
      "backup-original-key",
    ),
  );
});
it("only successful archive has download and restore requires explicit confirmation", async () => {
  vi.mocked(get).mockResolvedValue({
    operations: [
      {
        id: "b",
        kind: "backup",
        state: "SUCCEEDED",
        result: { bytes: 12, manifest: { snapshotKind: "stable" } },
      },
      { id: "f", kind: "backup", state: "FAILED", error: "hash不符" },
    ],
  });
  render(<BackupPanel ctx={ctx()} />);
  await screen.findByText("hash不符");
  expect(screen.getAllByRole("link", { name: "下载项目包" })).toHaveLength(1);
  expect(screen.getByRole("link", { name: "下载项目包" })).toHaveAttribute(
    "href",
    "/dgx-drama/backups/b/download",
  );
  fireEvent.click(screen.getByRole("button", { name: "恢复这个备份" }));
  expect(post).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "确认继续" }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      "/restores",
      { backupId: "b" },
      "backup-original-key",
    ),
  );
});
it("restored workspace link and query mode are explicit without generation", async () => {
  vi.mocked(get).mockResolvedValue({
    operations: [
      {
        id: "a".repeat(32),
        kind: "restore",
        state: "SUCCEEDED",
        result: { mode: "isolated" },
      },
    ],
  });
  render(<BackupPanel ctx={ctx()} />);
  const link = await screen.findByRole("link", { name: "打开恢复工作区" });
  expect(link.getAttribute("href")).toContain("?restored=" + "a".repeat(32));
  fireEvent.click(screen.getByRole("button", { name: "启用原任务只读核查" }));
  expect(post).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "确认继续" }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      `/restores/${"a".repeat(32)}/query-mode`,
      { mode: "query_only" },
      "backup-original-key",
    ),
  );
  expect(post).toHaveBeenCalledTimes(1);
});
it("in-flight operation locks file selection and signals app guard", async () => {
  let finish: (value: any) => void = () => {};
  vi.mocked(post).mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  render(<BackupPanel ctx={ctx()} />);
  fireEvent.click(screen.getByRole("button", { name: "创建项目备份" }));
  expect(screen.getByLabelText("项目备份文件")).toBeDisabled();
  expect(
    screen.getByRole("region", { name: "项目备份与恢复" }),
  ).toHaveAttribute("aria-busy", "true");
  expect(
    screen.getByRole("region", { name: "项目备份与恢复" }),
  ).toHaveAttribute("data-dirty", "true");
  finish({ operation: { id: "b" } });
  await waitFor(() =>
    expect(screen.getByLabelText("项目备份文件")).not.toBeDisabled(),
  );
});
it("does not expose original workspace UNKNOWN receipt in restored project with the same ID", async () => {
  sessionStorage.setItem(
    "dgx.drama.backup.pending:p",
    JSON.stringify({
      path: "/projects/p/backups",
      body: { expectedProjectRevision: 3 },
      key: "original-unknown",
    }),
  );
  history.replaceState({}, "", `/?restored=${"a".repeat(32)}`);
  try {
    render(<BackupPanel ctx={ctx()} />);
    expect(
      screen.queryByRole("button", { name: "按原请求键核查归档操作" }),
    ).not.toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
    expect(sessionStorage.getItem("dgx.drama.backup.pending:p")).toContain(
      "original-unknown",
    );
  } finally {
    history.replaceState({}, "", "/");
  }
});
it("tracks a restored foreign project by receipt ID when current project history is empty", async () => {
  const id = "c".repeat(32);
  sessionStorage.setItem(
    "dgx.drama.backup.pending:p:uploaded",
    JSON.stringify({
      id: "upload",
      preview: { projectId: "different-project", tables: {}, assetCount: 1 },
    }),
  );
  vi.mocked(post).mockResolvedValue({
    operation: {
      id,
      kind: "restore",
      projectId: "different-project",
      state: "QUEUED",
    },
  });
  vi.mocked(get).mockImplementation(async (path: string) =>
    path === `/restores/${id}`
      ? {
          operation: {
            id,
            kind: "restore",
            projectId: "different-project",
            state: "SUCCEEDED",
            result: {},
          },
        }
      : { operations: [] },
  );
  const view = render(<BackupPanel ctx={ctx()} />);
  fireEvent.click(screen.getByRole("button", { name: "恢复已检查项目包" }));
  fireEvent.click(screen.getByRole("button", { name: "确认继续" }));
  expect(
    await screen.findByRole("link", { name: "打开恢复工作区" }),
  ).toHaveAttribute("href", `/?restored=${id}`);
  view.unmount();
  render(<BackupPanel ctx={ctx()} />);
  expect(
    await screen.findByRole("link", { name: "打开恢复工作区" }),
  ).toBeInTheDocument();
  expect(post).toHaveBeenCalledTimes(1);
});
it("keeps original upload intent after wrong same-size content conflicts", async () => {
  const intent = {
    path: "/restore-uploads",
    body: {},
    key: "original-upload-key",
    upload: { name: "same.zip", size: 4 },
  };
  sessionStorage.setItem("dgx.drama.backup.pending:p", JSON.stringify(intent));
  vi.mocked(uploadRestore).mockRejectedValue(
    Object.assign(new Error("内容hash不匹配"), {
      status: 409,
      code: "request_conflict",
    }),
  );
  const context = ctx();
  render(<BackupPanel ctx={context} />);
  fireEvent.change(screen.getByLabelText("项目备份文件"), {
    target: { files: [new File(["bad!"], "same.zip")] },
  });
  fireEvent.click(
    screen.getByRole("button", { name: "按原请求键核查归档操作" }),
  );
  await waitFor(() =>
    expect(context.notify).toHaveBeenCalledWith("内容hash不匹配", "error"),
  );
  expect(
    JSON.parse(sessionStorage.getItem("dgx.drama.backup.pending:p")!),
  ).toEqual(intent);
  expect(
    screen.getByRole("button", { name: "按原请求键核查归档操作" }),
  ).toBeInTheDocument();
});
it("loads earlier history using the supplied cursor", async () => {
  const cursor = "d".repeat(32);
  vi.mocked(get).mockImplementation(async (path: string) =>
    path.includes("?before=")
      ? {
          operations: [
            { id: "old", kind: "backup", state: "FAILED", error: "较早记录" },
          ],
          nextCursor: null,
        }
      : { operations: [], nextCursor: cursor },
  );
  render(<BackupPanel ctx={ctx()} />);
  fireEvent.click(
    await screen.findByRole("button", { name: "查看更早的备份记录" }),
  );
  expect(await screen.findByText("较早记录")).toBeInTheDocument();
  expect(get).toHaveBeenCalledWith(`/projects/p/backups?before=${cursor}`);
});
