// @vitest-environment jsdom
import { useState } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { AssetPicker } from "./AssetPicker";
import { uploadImage } from "@/lib/api";
import type { WorkbenchContext } from "@/lib/types";

const asset = { id: "new-image", filename: "外部人物.png", kind: "image", width: 768, height: 1024 };
const file = () => new File(["valid-image-test-bytes"], "外部人物.png", { type: "image/png" });
const reply = (body: any, ok = true, status = 200) => ({ ok, status, json: async () => body });
const refresh = vi.fn(async () => {}), notify = vi.fn(), select = vi.fn();
function Harness({ label = "正面身份图", pid = "project" }) {
  const [value, setValue] = useState("old-image");
  const ctx = { project: { id: pid }, assets: [{ id: "old-image", filename: "旧图.png", kind: "image", width: 512, height: 512 }], refresh, notify } as unknown as WorkbenchContext;
  return <AssetPicker ctx={ctx} value={value} label={label} onChange={id => { select(id); setValue(id); }} />;
}
beforeEach(() => { vi.clearAllMocks(); refresh.mockResolvedValue(undefined); vi.stubGlobal("fetch", vi.fn().mockResolvedValue(reply({ asset }))); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("opens the selected image fullscreen and does not import or replace it", () => {
  render(<Harness />);
  fireEvent.click(screen.getByRole("button", { name: "正面身份图：查看图片" }));
  const dialog = screen.getByRole("dialog", { name: "查看图片" });
  expect(dialog).toBeVisible();
  expect(dialog.querySelector("img")).toHaveAttribute("src", "/dgx-drama/projects/project/assets/old-image/media");
  expect(screen.queryByLabelText("搜索参考图片")).not.toBeInTheDocument();
  expect(fetch).not.toHaveBeenCalled();
  expect(select).not.toHaveBeenCalled();
});

it.each(["正面身份图", "场景全景", "输入图 1 · 完整参考画面"])("%s: imports and selects an external file without generating or approving", async label => {
  render(<Harness label={label} />);
  expect(screen.getByRole("button", { name: `${label}：从电脑导入` })).toBeEnabled();
  fireEvent.change(screen.getByLabelText(`${label}：选择电脑图片文件`), { target: { files: [file()] } });
  await waitFor(() => expect(select).toHaveBeenCalledWith(asset.id));
  expect(await screen.findByRole("button", { name: `${label}：外部人物.png` })).toBeVisible();
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(fetch).toHaveBeenCalledWith("/dgx-drama/projects/project/images", expect.objectContaining({ method: "POST", body: expect.any(File), headers: expect.objectContaining({ "X-DGX-Drama-Action": "explicit-user-action", "X-Filename": encodeURIComponent(file().name), "X-Request-Key": expect.any(String) }) }));
  expect(refresh).toHaveBeenCalledOnce();
});

it("shows loading and prevents duplicate import while a request is pending", async () => {
  let resolve: any;
  vi.mocked(fetch).mockImplementationOnce(() => new Promise(r => { resolve = r; }));
  render(<Harness />);
  fireEvent.click(screen.getByRole("button", { name: "正面身份图：旧图.png" }));
  const input = screen.getByLabelText("正面身份图：选择电脑图片文件");
  fireEvent.change(input, { target: { files: [file()] } });
  expect(screen.getByRole("status")).toHaveTextContent("正在上传并校验");
  expect(screen.getByRole("button", { name: "从电脑导入" })).toBeDisabled();
  fireEvent.change(input, { target: { files: [file()] } });
  expect(fetch).toHaveBeenCalledOnce();
  resolve(reply({ asset }));
  await waitFor(() => expect(select).toHaveBeenCalledWith(asset.id));
});

it("reports server validation errors inside the picker and retries explicitly", async () => {
  vi.mocked(fetch).mockResolvedValueOnce(reply({ error: { message: "图片内容损坏" } }, false, 400) as any);
  render(<Harness />);
  const input = screen.getByLabelText("正面身份图：选择电脑图片文件");
  fireEvent.change(input, { target: { files: [file()] } });
  expect(await screen.findByRole("alert")).toHaveTextContent("图片内容损坏");
  expect(select).not.toHaveBeenCalled(); expect(input).toHaveValue("");
  fireEvent.change(input, { target: { files: [file()] } });
  await waitFor(() => expect(select).toHaveBeenCalledWith(asset.id));
  expect(fetch).toHaveBeenCalledTimes(2);
});

it("retains successful selection when refreshing the library fails", async () => {
  refresh.mockRejectedValueOnce(new Error("network")); render(<Harness />);
  fireEvent.change(screen.getByLabelText("正面身份图：选择电脑图片文件"), { target: { files: [file()] } });
  expect(await screen.findByRole("status")).toHaveTextContent("无需重复导入");
  expect(select).toHaveBeenCalledWith(asset.id); expect(fetch).toHaveBeenCalledOnce();
});

it("rejects unsupported and oversized files before uploading; canceled selection does nothing", async () => {
  await expect(uploadImage("project", new File(["a"], "photo.heic"))).rejects.toThrow("PNG");
  await expect(uploadImage("project", new File([new Uint8Array(8 * 1024 * 1024 + 1)], "big.png"))).rejects.toThrow("8 MiB");
  await expect(uploadImage("project", new File([], "empty.png"))).rejects.toThrow("非空");
  render(<Harness />);
  fireEvent.change(screen.getByLabelText("正面身份图：选择电脑图片文件"), { target: { files: [] } });
  expect(fetch).not.toHaveBeenCalled(); expect(select).not.toHaveBeenCalled();
});

it("does not retry an uncertain upload, and gives the request key for reconciliation", async () => {
  vi.mocked(fetch).mockRejectedValueOnce(new Error("Network failed")); render(<Harness />);
  fireEvent.change(screen.getByLabelText("正面身份图：选择电脑图片文件"), { target: { files: [file()] } });
  expect(await screen.findByRole("alert")).toHaveTextContent("请求键：drama-ui-");
  expect(screen.getByRole("alert")).toHaveTextContent("勿重复上传");
  expect(fetch).toHaveBeenCalledOnce(); expect(select).not.toHaveBeenCalled();
});

it("ignores a late upload response after leaving the picker", async () => {
  let resolve: any; vi.mocked(fetch).mockImplementationOnce(() => new Promise(r => { resolve = r; }));
  const view = render(<Harness />);
  fireEvent.change(screen.getByLabelText("正面身份图：选择电脑图片文件"), { target: { files: [file()] } });
  view.unmount(); resolve(reply({ asset }));
  await new Promise(r => setTimeout(r, 0)); expect(select).not.toHaveBeenCalled();
});
