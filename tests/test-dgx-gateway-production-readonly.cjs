const assert = require("node:assert/strict");
const {chromium} = require("playwright");

(async () => {
  const baseUrl = process.env.DGX_GATEWAY_URL || "http://192.0.2.37:18082";
  const expectedModel = process.env.DGX_GATEWAY_EXPECTED_MODEL || "deepseek-v4-flash";
  const browser = await chromium.launch({
    headless: true,
    executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  });
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  const consoleErrors = [];
  const nonGetRequests = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("request", (request) => {
    if (request.url().startsWith(baseUrl) && request.method() !== "GET") {
      nonGetRequests.push(`${request.method()} ${request.url()}`);
    }
  });
  await page.goto(`${baseUrl}/`, {waitUntil: "domcontentloaded"});
  assert.equal(await page.locator('link[rel="icon"]').getAttribute("href"), "/favicon.svg");
  await page.waitForFunction((model) => document.querySelector("#active-model")?.textContent.includes(model), expectedModel, {timeout: 20000});
  await page.waitForFunction(() => document.querySelectorAll("#nodes .node-card").length === 2, null, {timeout: 20000});
  await page.waitForFunction(() => document.querySelectorAll("[data-legend]").length === 8, null, {timeout: 20000});
  assert.equal(await page.locator("#lock").textContent(), "LOCK UNLOCKED");
  assert.equal(await page.locator("#nodes .node-card").count(), 2);
  assert.equal(await page.locator("[data-legend]").count(), 8);
  assert.deepEqual(nonGetRequests, []);
  assert.deepEqual(consoleErrors, []);
  await page.screenshot({
    path: "plans/dgx-cluster-gateway-ui-observability/production-readonly.png",
    fullPage: true,
  });
  await browser.close();
  console.log(`production read-only browser smoke: PASS (${expectedModel}, two nodes, eight series, no non-GET request)`);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
