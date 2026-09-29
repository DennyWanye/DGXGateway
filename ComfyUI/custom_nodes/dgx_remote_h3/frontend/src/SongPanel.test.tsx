// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { SongPanel } from "./SongPanel";
import { post, uploadAudio } from "@/lib/api";
import { webcrypto } from "node:crypto";

vi.mock("@/lib/api", () => ({
  get: vi.fn(),
  post: vi.fn(),
  segment: encodeURIComponent,
  requestKey: () => "drama-ui-song-test",
  mediaUrl: (p: string, a: string) => `/media/${p}/${a}`,
  uploadAudio: vi.fn(),
}));

const song = {
  id: "a".repeat(64),
  kind: "audio",
  filename: "v11.wav",
  duration: 4,
};
function ctx(extra: any = {}) {
  return {
    project: { id: "project", fps: 24, kind: "mv", width: 1344, height: 768 },
    episode: { id: "episode", revision: 3, song_asset_id: null },
    assets: [song],
    shots: [],
    entities: [],
    capabilities: {},
    notify: vi.fn(),
    refresh: vi.fn().mockResolvedValue(undefined),
    navigate: vi.fn(),
    openCanvas: vi.fn(),
    params: {},
    ...extra,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  sessionStorage.clear();
  Object.defineProperty(globalThis, "crypto", { configurable: true, value: webcrypto });
  vi.mocked(post).mockResolvedValue({ episode: { id: "episode", revision: 4, song_asset_id: song.id } });
});
afterEach(() => cleanup());

describe("歌曲页", () => {
  it("列出音频并绑定本集歌曲，不写时间线", async () => {
    const context = ctx();
    render(<SongPanel ctx={context} />);
    expect(screen.getByText("v11.wav")).toBeInTheDocument();
    expect(screen.getByText(/4.000 秒 · 96 帧/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "绑定为本集歌曲" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/episodes/episode/song",
        { songAssetId: song.id, expectedRevision: 3 },
        "drama-ui-song-test",
      ),
    );
    expect(vi.mocked(post).mock.calls.every(([path]) => !String(path).includes("timeline"))).toBe(true);
    await waitFor(() => expect(context.refresh).toHaveBeenCalled());
    expect(context.notify).toHaveBeenCalledWith("已绑定本集歌曲。时间线尚未改写。", "success");
  });

  it("已绑定条目不能重复提交", () => {
    render(
      <SongPanel
        ctx={ctx({ episode: { id: "episode", revision: 3, song_asset_id: song.id } })}
      />,
    );
    expect(screen.getByRole("button", { name: "当前歌曲" })).toBeDisabled();
    expect(post).not.toHaveBeenCalled();
  });

  it("上传走现有声音素材通道", async () => {
    vi.mocked(uploadAudio).mockResolvedValue({ asset: song });
    const context = ctx({ assets: [] });
    render(<SongPanel ctx={context} />);
    const file = new File([new Uint8Array([1, 2, 3, 4])], "v11.wav", { type: "audio/wav" });
    fireEvent.change(screen.getByLabelText("音频文件"), { target: { files: [file] } });
    fireEvent.click(screen.getByRole("button", { name: "保存到项目" }));
    await waitFor(() => expect(uploadAudio).toHaveBeenCalled());
    expect(vi.mocked(uploadAudio).mock.calls[0][0]).toBe("project");
    expect(post).not.toHaveBeenCalled();
  });
});
