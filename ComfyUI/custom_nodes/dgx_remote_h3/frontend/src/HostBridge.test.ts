// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// Run the actual host code, replacing only its two external ComfyUI imports
// and module URL. ComfyUI's scripts do not live in this Vite project.
const source = readFileSync(
  resolve(process.cwd(), "../web/studio_manager.js"),
  "utf8",
)
  .replace(/^import \{ app \} from .*;\n/m, "")
  .replace(/^import \{ openShotOnCanvas \} from .*;\n/m, "")
  .replaceAll(
    "import.meta.url",
    JSON.stringify(
      "http://localhost/extensions/dgx_remote_h3/studio_manager.js",
    ),
  );
let setup: () => Promise<void>;
let listenerSpy: ReturnType<typeof vi.spyOn>;
const openShotOnCanvas = vi.fn(async () => {});
function dispatch(
  frame: HTMLIFrameElement,
  data: Record<string, any>,
  sourceWindow: Window | null = frame.contentWindow,
  origin = location.origin,
) {
  window.dispatchEvent(
    new MessageEvent("message", { data, origin, source: sourceWindow }),
  );
}
async function start() {
  await setup();
  return {
    frame: document.querySelector<HTMLIFrameElement>(
      '[data-dgx-studio="frame"]',
    )!,
    launch: document.querySelector<HTMLButtonElement>(
      '[data-dgx-studio="launch"]',
    )!,
  };
}
beforeEach(() => {
  document.body.innerHTML = "";
  document.body.style.overflow = "";
  history.replaceState({}, "", "/?dramaUI=studio");
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: true, json: async () => ({ enabled: true }) })),
  );
  listenerSpy = vi.spyOn(window, "addEventListener");
  openShotOnCanvas.mockClear();
  new Function("app", "openShotOnCanvas", source)(
    {
      registerExtension: (extension: any) => {
        setup = extension.setup;
      },
    },
    openShotOnCanvas,
  );
});
afterEach(() => {
  const frame = document.querySelector<HTMLIFrameElement>(
    '[data-dgx-studio="frame"]',
  );
  if (frame) dispatch(frame, { type: "dgx-studio:close" });
  for (const [type, listener, options] of listenerSpy.mock.calls)
    window.removeEventListener(
      type as string,
      listener as EventListener,
      options as any,
    );
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});
it("isolates asynchronously appended body children and restores their exact prior inert states", async () => {
  const original = document.createElement("main");
  original.inert = false;
  const alreadyInert = document.createElement("aside");
  alreadyInert.inert = true;
  document.body.append(original, alreadyInert);
  const { frame } = await start();
  expect(original.inert).toBe(true);
  expect(alreadyInert.inert).toBe(true);
  expect(frame.inert).not.toBe(true);
  const lateLaunch = document.createElement("button");
  lateLaunch.textContent = "旧短剧工作台";
  lateLaunch.inert = false;
  const lateProtected = document.createElement("div");
  lateProtected.inert = true;
  document.body.append(lateLaunch, lateProtected);
  await Promise.resolve();
  expect(lateLaunch.inert).toBe(true);
  expect(lateProtected.inert).toBe(true);
  lateLaunch.remove();
  lateLaunch.inert = false;
  document.body.append(lateLaunch);
  await Promise.resolve();
  expect(lateLaunch.inert).toBe(true);
  dispatch(frame, { type: "dgx-studio:close" });
  expect(original.inert).toBe(false);
  expect(alreadyInert.inert).toBe(true);
  expect(lateLaunch.inert).toBe(false);
  expect(lateProtected.inert).toBe(true);
  const afterClose = document.createElement("button");
  afterClose.inert = false;
  document.body.append(afterClose);
  await Promise.resolve();
  expect(afterClose.inert).toBe(false);
});
it("duplicate show and close preserve the original body overflow and focus", async () => {
  document.body.style.overflow = "scroll";
  const focused = document.createElement("button");
  focused.inert = false;
  document.body.append(focused);
  focused.focus();
  const { frame, launch } = await start();
  expect(document.body.style.overflow).toBe("hidden");
  expect(document.activeElement).toBe(frame);
  launch.click();
  launch.click();
  dispatch(frame, { type: "dgx-studio:close" });
  expect(document.body.style.overflow).toBe("scroll");
  expect(document.activeElement).toBe(focused);
  expect(focused.inert).toBe(false);
  document.body.style.overflow = "auto";
  dispatch(frame, { type: "dgx-studio:close" });
  expect(document.body.style.overflow).toBe("auto");
  expect(document.activeElement).toBe(focused);
  launch.focus();
  launch.click();
  expect(document.body.style.overflow).toBe("hidden");
  dispatch(frame, { type: "dgx-studio:close" });
  expect(document.body.style.overflow).toBe("auto");
  expect(document.activeElement).toBe(launch);
});
it("keeps the legacy rollback gate and prevents duplicate host installation", async () => {
  history.replaceState({}, "", "/?dramaUI=legacy");
  await setup();
  expect(document.querySelector("[data-dgx-studio]")).toBeNull();
  expect(fetch).not.toHaveBeenCalled();
  history.replaceState({}, "", "/?dramaUI=studio");
  await start();
  await setup();
  expect(document.querySelectorAll('[data-dgx-studio="frame"]')).toHaveLength(
    1,
  );
});
it("ignores close and canvas requests from other sources or origins", async () => {
  const { frame } = await start();
  dispatch(frame, { type: "dgx-studio:close" }, window);
  expect(frame.hidden).toBe(false);
  dispatch(
    frame,
    { type: "dgx-studio:close" },
    frame.contentWindow,
    "https://untrusted.invalid",
  );
  expect(frame.hidden).toBe(false);
  dispatch(
    frame,
    { type: "dgx-studio:canvas", shotId: "a".repeat(32) },
    window,
  );
  await Promise.resolve();
  expect(openShotOnCanvas).not.toHaveBeenCalled();
  dispatch(frame, { type: "dgx-studio:canvas", shotId: "invalid" });
  await Promise.resolve();
  expect(openShotOnCanvas).not.toHaveBeenCalled();
  dispatch(frame, { type: "dgx-studio:canvas", shotId: "a".repeat(32) });
  await Promise.resolve();
  expect(openShotOnCanvas).toHaveBeenCalledExactlyOnceWith("a".repeat(32));
});
it("opens the studio by default and keeps legacy as an explicit rollback", async () => {
  history.replaceState({}, "", "/?dramaUI=legacy");
  await setup();
  expect(document.querySelector('[data-dgx-studio]')).toBeNull();
  history.replaceState({}, "", "/");
  const { frame } = await start();
  expect(frame.hidden).toBe(false);
});
it("does not expose a studio when drama capability is disabled", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => ({ enabled: false }) })));
  await setup();
  expect(document.querySelector('[data-dgx-studio]')).toBeNull();
});
