import { withBrowser } from "../../../../tools/browser-harness/playwright.mjs";


const targetUrl = process.env.FARYO_COMMAND_URL || "";
const expectedVersion = process.env.FARYO_SMOKE_EXPECT_VERSION || "";
const expectedText = process.env.FARYO_COMMAND_EXPECT_TEXT || "Renamed conversation";
const expectedActivityText = process.env.FARYO_ACTIVITY_EXPECT_TEXT || "";
const chromeBin = process.env.CHROME_BIN || "/usr/bin/google-chrome";
if (!targetUrl) throw new Error("FARYO_COMMAND_URL is required");

await withBrowser(
  { executablePath: chromeBin, viewport: { width: 390, height: 844 }, mobile: true },
  async ({ page }) => {
    const errors = [];
    page.on("pageerror", (error) => errors.push(String(error?.message || error)));
    const collect = () => {
      const rows = [...document.querySelectorAll("#output > .command-timeline-row")];
      const app = document.querySelector('script[src*="app.js?"]');
      const activityAsset = performance.getEntriesByType("resource")
        .map((entry) => entry.name)
        .find((url) => url.includes("/owner/activity-groups.mjs?"));
      return {
        rows: rows.length,
        text: rows.map((row) => row.textContent || "").join(" "),
        source: document.getElementById("output")?.dataset.captureSource || "",
        userBlocks: document.querySelectorAll("#output > .compact-block.user").length,
        answerText: [...document.querySelectorAll("#output > .compact-block.output")]
          .map((block) => block.textContent || "").join(" "),
        appRevision: app ? new URL(app.src).searchParams.get("v") || "" : "",
        activityRevision: activityAsset ? new URL(activityAsset).searchParams.get("v") || "" : "",
        overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
      };
    };
    const wait = () => page.waitForFunction(
      ({ text, activity }) => document.documentElement.dataset.faryoAppReady === "1"
        && [...document.querySelectorAll("#output > .command-timeline-row")]
          .some((row) => (row.textContent || "").includes(text))
        && (!activity || document.querySelectorAll("#output > .compact-activity-card").length > 0),
      { text: expectedText, activity: expectedActivityText },
      { timeout: 25_000 },
    );

    await page.goto(targetUrl, { waitUntil: "domcontentloaded" });
    await wait();
    if (expectedActivityText) {
      const card = page.locator("#output > .compact-activity-card").first();
      if (!(await card.evaluate((node) => node.open))) await card.locator(":scope > summary").click();
      const item = card.locator(".compact-activity-item-long").first();
      if (!(await item.evaluate((node) => node.open))) await item.locator(":scope > summary").click();
      await page.waitForFunction(
        (text) => [...document.querySelectorAll(".activity-detail-body")]
          .some((node) => (node.textContent || "").includes(text)),
        expectedActivityText,
        { timeout: 15_000 },
      );
    }
    const first = await page.evaluate(collect);
    await page.reload({ waitUntil: "domcontentloaded" });
    await wait();
    const reloaded = await page.evaluate(collect);
    if (
      first.rows !== 1
      || reloaded.rows !== 1
      || !first.text.includes(expectedText)
      || first.source !== "codex-app-server"
      || (!expectedActivityText && first.userBlocks !== 0)
      || first.answerText.includes(expectedText)
      || first.overflow
      || !first.appRevision
      || first.appRevision !== first.activityRevision
      || reloaded.appRevision !== first.appRevision
      || (expectedVersion && first.appRevision !== expectedVersion)
      || errors.length
    ) {
      throw new Error(`Real command timeline failed: ${JSON.stringify({ first, reloaded, errors })}`);
    }
    console.log(`faryo-browser-real-command=PASS rows=1 transcript-pollution=no ordinary-reload=yes revision=${first.appRevision}`);
  },
);
