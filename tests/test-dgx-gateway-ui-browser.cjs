const assert = require("node:assert/strict");
const {chromium} = require("playwright");

(async () => {
  const baseUrl = process.env.DGX_GATEWAY_UI_FIXTURE_URL || "http://127.0.0.1:18083";
  let browser;
  try {
    browser = await chromium.launch({
      headless: true,
      executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    });
    const page = await browser.newPage({viewport: {width: 1280, height: 900}});
    const consoleErrors = [];
    page.on("console", (message) => {
      if (message.type() === "error") consoleErrors.push(message.text());
    });
    await page.goto(`${baseUrl}/fixture/reset`, {waitUntil: "domcontentloaded"});
    await page.goto(`${baseUrl}/`, {waitUntil: "domcontentloaded"});
  await page.locator('[role="progressbar"]').waitFor();
  const first = Number(await page.locator('[role="progressbar"]').getAttribute("aria-valuenow"));
  assert.ok(first >= 35 && first < 100, `unexpected first progress: ${first}`);
  assert.equal(await page.locator("[data-model]").count(), 4);
  assert.equal(await page.locator('[data-model="deepseek-v4-flash-vision-exp"]').count(), 1);
  for (const button of await page.locator("[data-model]").all()) assert.equal(await button.isDisabled(), true);
  await page.waitForFunction(() => document.querySelector('[role="progressbar"]')?.getAttribute("aria-valuenow") === "100", null, {timeout: 30000});
  assert.equal(await page.locator('[data-model="deepseek-v4-flash"]').isDisabled(), true);
  assert.equal(await page.locator('[data-model="deepseek-v4-flash"]').textContent(), "当前模型");
  assert.equal(await page.locator('[data-model-card="deepseek-v4-flash"]').getAttribute("data-current"), "true");
  assert.equal(await page.locator('[data-connection="base-url"]').textContent(), "http://192.0.2.30:11434/v1");
  assert.equal(await page.locator('[data-connection="api-key"]').textContent(), `fixture-${"a".repeat(56)}`);
  assert.equal(await page.locator('[data-connection="model-name"]').textContent(), "deepseek-v4-flash-0731-abliterated-anchored");
  assert.equal(await page.locator('[data-connection="request-path"]').textContent(), "/chat/completions");
  await page.evaluate(() => fetch("/fixture/scenario/h3-active"));
  await page.waitForFunction(() => document.querySelector('[data-connection="base-url"]')?.textContent === "http://192.0.2.30:18081/v1", null, {timeout: 7000});
  assert.match(await page.locator('[data-connection="model-name"]').textContent(), /minimax-h3-10eros-fl2va-v1$/);
  await page.evaluate(() => fetch("/fixture/scenario/playback"));
  await page.waitForFunction(() => document.querySelector('[data-connection="base-url"]')?.textContent === "http://192.0.2.30:11434/v1", null, {timeout: 7000});
  await page.evaluate(() => fetch("/fixture/fail-next-snapshot"));
  await page.waitForFunction(() => document.querySelector("#connection")?.textContent.includes("状态不可用"), null, {timeout: 7000});
  assert.equal(await page.locator('[data-connection="api-key"]').count(), 0, "snapshot failure must remove the stale API key");
  assert.match(await page.locator("#active-connection").textContent(), /停止发布旧连接参数/);
  await page.waitForFunction(() => document.querySelector('[data-connection="base-url"]')?.textContent === "http://192.0.2.30:11434/v1", null, {timeout: 7000});
  for (const model of ["deepseek-v4-flash-vision-exp", "minimax-h3", "comfyui"]) {
    assert.equal(await page.locator(`[data-model="${model}"]`).isDisabled(), false);
  }
  await page.evaluate(() => fetch("/fixture/scenario/h3-active"));
  const h3Page = await browser.newPage({viewport: {width: 1000, height: 760}});
  await h3Page.goto(`${baseUrl}/`, {waitUntil: "domcontentloaded"});
  const h3Button = h3Page.locator('[data-model="minimax-h3"]');
  const h3Profile = h3Page.locator("#h3-profile");
  assert.equal(await h3Button.isDisabled(), true);
  assert.equal(await h3Button.textContent(), "当前配置");
  assert.equal(await h3Button.getAttribute("aria-current"), "true");
  assert.equal(await h3Page.locator('[data-connection="base-url"]').textContent(), "http://192.0.2.30:18081/v1");
  assert.equal(await h3Page.locator('[data-connection="api-key"]').textContent(), `fixture-${"a".repeat(56)}`);
  assert.match(await h3Page.locator('[data-connection="model-name"]').textContent(), /minimax-h3-10eros-fl2va-v1$/);
  assert.equal(await h3Page.locator('[data-connection="request-path"]').textContent(), "/videos/sync");
  await h3Profile.selectOption("heretic-ref2va-v1");
  assert.equal(await h3Button.isDisabled(), false);
  assert.equal(await h3Button.textContent(), "切换到此配置");
  assert.equal(await h3Button.getAttribute("aria-current"), null);
  await h3Profile.selectOption("10eros-fl2va-v1");
  assert.equal(await h3Button.isDisabled(), true);
  assert.equal(await h3Button.textContent(), "当前配置");
  await h3Page.close();
  await page.evaluate(() => fetch("/fixture/scenario/playback"));
  const frozenElapsed = await page.locator("[data-elapsed]").textContent();
  await page.waitForTimeout(1100);
  assert.equal(await page.locator("[data-elapsed]").textContent(), frozenElapsed, "terminal elapsed time must stop increasing");
  assert.equal(await page.locator("[data-legend]").count(), 8);
  assert.equal(await page.locator(".legend-machine").count(), 2);
  assert.match(await page.locator(".legend-machine").nth(0).textContent(), /DGX 1.*192\.168\.10\.27.*主控 \/ API.*Head/s);
  assert.match(await page.locator(".legend-machine").nth(1).textContent(), /DGX 2.*192\.168\.10\.31.*计算节点.*Worker/s);
  assert.match(await page.locator("#nodes .node-card").nth(0).textContent(), /DGX 1.*192\.168\.10\.27.*主控 \/ API.*Head/s);
  assert.match(await page.locator("#nodes .node-card").nth(1).textContent(), /DGX 2.*192\.168\.10\.31.*计算节点.*Worker/s);
  const seriesStyles = await page.locator(".chart-series").evaluateAll((groups) => groups.map((group) => {
    const polyline = group.querySelector("polyline");
    return {key: group.dataset.series, color: getComputedStyle(group).color, dash: polyline ? getComputedStyle(polyline).strokeDasharray : "missing"};
  }));
  assert.equal(new Set(seriesStyles.map((series) => series.color)).size, 8, JSON.stringify(seriesStyles));
  assert.ok(seriesStyles.every((series) => series.dash === "none"), JSON.stringify(seriesStyles));
  const headSegments = await page.locator('[data-series="head.temperature"] polyline').count();
  const workerSegments = await page.locator('[data-series="worker.temperature"] polyline').count();
  assert.ok(headSegments >= 2, "the injected snapshot failure must break every series");
  assert.ok(workerSegments > headSegments, "worker-only stale telemetry must add one more worker gap");
  const headTemperatureLegend = page.locator('[data-legend="head.temperature"]');
  await headTemperatureLegend.focus();
  await headTemperatureLegend.press("Enter");
  assert.equal(await headTemperatureLegend.getAttribute("aria-pressed"), "false");
  assert.equal(await page.locator('[data-series="head.temperature"] polyline').count(), 0);
  await page.locator(".chart-capture").focus();
  await page.locator(".chart-capture").press("Home");
  assert.match(await page.locator("#chart-tooltip").textContent(), /(°C|%|W)/);
  for (const width of [375, 320]) {
    await page.setViewportSize({width, height: 800});
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    assert.ok(overflow <= 0, `horizontal overflow at ${width}px: ${overflow}px`);
  }
  const state = await page.evaluate(() => fetch("/fixture/state").then((response) => response.json()));
  assert.equal(state.posts, 0, "browser smoke must never submit a switch");
  await page.evaluate(() => fetch("/fixture/slow-next"));
  await page.waitForFunction(async () => (await fetch("/fixture/state")).json().then((value) => value.slow_active > 0), null, {timeout: 7000});
  page.on("dialog", (dialog) => dialog.accept());
  const switchButton = page.locator('[data-model="comfyui"]');
  await switchButton.click();
  await page.waitForTimeout(50);
  assert.equal(await page.locator('[data-model="comfyui"]').isDisabled(), true);
  await page.locator('[data-model="comfyui"]').click({force: true}).catch(() => {});
  await page.waitForTimeout(1650);
  assert.equal(await page.locator("#operation code").textContent(), "fixture-submit-001");
  assert.equal(await page.locator('[data-model="comfyui"]').isDisabled(), true);
  const afterSubmit = await page.evaluate(() => fetch("/fixture/state").then((response) => response.json()));
  assert.equal(afterSubmit.posts, 1, "a repeated click during submission must not create a second request");
  assert.equal(consoleErrors.filter((message) => message.includes("503 (Service Unavailable)")).length, 1);
  assert.deepEqual(consoleErrors.filter((message) => !message.includes("503 (Service Unavailable)")), []);
  await page.screenshot({path: "plans/dgx-cluster-gateway-ui-observability/value-smoke.png", fullPage: true});

  async function verifyTerminalScenario(name, expectedText, expectedClass, expectDisabled) {
    await page.evaluate((scenario) => fetch(`/fixture/scenario/${scenario}`), name);
    const scenarioPage = await browser.newPage({viewport: {width: 1000, height: 760}});
    await scenarioPage.goto(`${baseUrl}/`, {waitUntil: "domcontentloaded"});
    await scenarioPage.locator("#operation").getByText(expectedText).waitFor();
    assert.equal(await scenarioPage.locator(`#operation .${expectedClass}`).count(), 1);
    assert.equal(await scenarioPage.locator('[data-model="comfyui"]').isDisabled(), expectDisabled);
    await scenarioPage.close();
  }

  await verifyTerminalScenario("rollback", "切换未完成，已安全恢复原模型", "tone-rollback", false);
  await verifyTerminalScenario("failure", "切换失败，相关模型已安全停止", "tone-danger", false);
  await verifyTerminalScenario("recovery", "需要人工恢复确认，新的切换已锁定", "tone-critical", true);

  await page.evaluate(() => Promise.all([fetch("/fixture/scenario/rollback"), fetch("/fixture/post-mode/busy")]));
  const errorPage = await browser.newPage({viewport: {width: 1000, height: 760}});
  errorPage.on("dialog", (dialog) => dialog.accept());
  await errorPage.goto(`${baseUrl}/`, {waitUntil: "domcontentloaded"});
  await errorPage.locator('[data-model="comfyui"]').click();
  await errorPage.locator("#toast").getByText("已有切换正在进行，请等待当前任务完成").waitFor();
  await errorPage.waitForFunction(() => !document.querySelector('[data-model="comfyui"]').disabled);
  await errorPage.evaluate(() => fetch("/fixture/post-mode/forbidden"));
  await errorPage.locator('[data-model="comfyui"]').click();
  await errorPage.locator("#toast").getByText("登录已失效，请重新登录").waitFor();
  await errorPage.close();
    console.log(`gateway UI browser scenarios: PASS (${first}% → 100%, production posts=0, submit guard/rollback/failure/recovery/BUSY/auth covered)`);
  } finally {
    await browser?.close().catch(() => {});
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
