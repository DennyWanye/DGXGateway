// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import { uploadAudio } from "./lib/api";
afterEach(() => vi.unstubAllGlobals());
it("P5 audio upload carries explicit original key and binary headers, with local size/type limits before fetch", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ asset: { id: "audio" } }),
    });
  vi.stubGlobal("fetch", fetch);
  const file = new File(["valid fixture bytes"], "配音.wav", {
    type: "audio/wav",
  });
  await uploadAudio("project", file, "drama-ui-audio-original");
  expect(fetch).toHaveBeenCalledWith(
    "/dgx-drama/projects/project/audio-assets",
    expect.objectContaining({
      method: "POST",
      body: file,
      headers: expect.objectContaining({
        "Content-Type": "application/octet-stream",
        "X-Request-Key": "drama-ui-audio-original",
        "X-Filename": encodeURIComponent("配音.wav"),
      }),
    }),
  );
  await expect(
    uploadAudio("project", new File([], "empty.wav"), "key"),
  ).rejects.toThrow("256 MiB");
  await expect(
    uploadAudio("project", new File(["a"], "invalid.exe"), "key"),
  ).rejects.toThrow("WAV");
  expect(fetch).toHaveBeenCalledTimes(1);
});
it("P5 explicit missing ASR/config error code survives API decoding for a safe non-submitted result", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue({
        ok: false,
        status: 503,
        json: async () => ({
          error: { code: "ASR_MODEL_MISSING", message: "模型未配置" },
        }),
      }),
  );
  const { post } = await import("./lib/api");
  await expect(
    post("/timeline-revisions/t/transcriptions", {}, "drama-ui-original"),
  ).rejects.toMatchObject({ status: 503, code: "ASR_MODEL_MISSING" });
});
