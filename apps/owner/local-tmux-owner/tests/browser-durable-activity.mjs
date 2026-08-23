import { withBrowser } from "../../../../tools/browser-harness/playwright.mjs";

const targetUrl = process.env.FARYO_SMOKE_URL || "";
const chromeBin = process.env.CHROME_BIN || "/usr/bin/google-chrome";
const expectedVersion = process.env.FARYO_SMOKE_EXPECT_VERSION || "";
const minimum = {
  commands: Number(process.env.FARYO_SMOKE_MIN_COMMANDS || 1),
  edits: Number(process.env.FARYO_SMOKE_MIN_EDITS || 1),
  searches: Number(process.env.FARYO_SMOKE_MIN_SEARCHES || 1),
};
if (!targetUrl) throw new Error("FARYO_SMOKE_URL is required");

await withBrowser(
  {
    executablePath: chromeBin,
    viewport: { width: 390, height: 844 },
    mobile: true,
  },
  async ({ page }) => {
    const pageErrors = [];
    page.on("pageerror", (error) =>
      pageErrors.push(String(error?.message || error)),
    );
    await page.goto(targetUrl, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(
      ({ commands, edits, searches }) => {
        if (document.documentElement.dataset.faryoAppReady !== "1")
          return false;
        const cards = [
          ...document.querySelectorAll("#output > .compact-activity-card"),
        ];
        const title = cards
          .map((card) => card.querySelector("summary")?.textContent || "")
          .join(" ");
        const count = (label) =>
          [...title.matchAll(new RegExp(`(\\d+) ${label}`, "g"))].reduce(
            (total, match) => total + Number(match[1] || 0),
            0,
          );
        return (
          count("commands?") >= commands &&
          count("edits?") >= edits &&
          count("search(?:es)?") >= searches
        );
      },
      minimum,
      { timeout: 25_000 },
    );

    const collect = () => {
      const cards = [
        ...document.querySelectorAll("#output > .compact-activity-card"),
      ];
      const titles = cards.map(
        (card) => card.querySelector("summary")?.textContent || "",
      );
      const title = titles.join(" ");
      const count = (label) =>
        [...title.matchAll(new RegExp(`(\\d+) ${label}`, "g"))].reduce(
          (total, match) => total + Number(match[1] || 0),
          0,
        );
      const appScript = document.querySelector('script[src*="app.js?"]');
      const appRevision = appScript
        ? new URL(appScript.src).searchParams.get("v") || ""
        : "";
      const activityAsset = performance
        .getEntriesByType("resource")
        .map((entry) => entry.name)
        .find((url) => url.includes("/owner/activity-groups.mjs?"));
      return {
        cards: cards.length,
        openCards: cards.filter((card) => card.open).length,
        commands: count("commands?"),
        edits: count("edits?"),
        searches: count("search(?:es)?"),
        activityItems: document.querySelectorAll(
          "#output .compact-activity-item",
        ).length,
        longItems: document.querySelectorAll(
          "#output .compact-activity-item-long",
        ).length,
        working: [...document.querySelectorAll("#output > *")].filter(
          (node) => node.textContent.trim() === "Working",
        ).length,
        rawProcess: document.querySelectorAll("#output > .compact-process-line")
          .length,
        titleOverflow: cards.some(
          (card) =>
            card.querySelector("summary")?.scrollWidth > card.clientWidth + 1,
        ),
        appRevision,
        activityRevision: activityAsset
          ? new URL(activityAsset).searchParams.get("v") || ""
          : "",
      };
    };

    const closed = await page.evaluate(collect);
    await page.evaluate(() => {
      for (const card of document.querySelectorAll(
        "#output > .compact-activity-card",
      )) {
        card.open = true;
      }
    });
    await page.waitForFunction(
      ({ commands, edits, searches }) => document.querySelectorAll(
        "#output .compact-activity-item",
      ).length >= commands + edits + searches,
      minimum,
      { timeout: 5_000 },
    );
    const opened = await page.evaluate(collect);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForFunction(
      ({ commands, edits, searches }) => {
        if (document.documentElement.dataset.faryoAppReady !== "1")
          return false;
        const title = [
          ...document.querySelectorAll(
            "#output > .compact-activity-card > summary",
          ),
        ]
          .map((summary) => summary.textContent || "")
          .join(" ");
        const count = (label) =>
          [...title.matchAll(new RegExp(`(\\d+) ${label}`, "g"))].reduce(
            (total, match) => total + Number(match[1] || 0),
            0,
          );
        return (
          count("commands?") >= commands &&
          count("edits?") >= edits &&
          count("search(?:es)?") >= searches
        );
      },
      minimum,
      { timeout: 25_000 },
    );
    const reloaded = await page.evaluate(collect);

    if (
      closed.cards < 1 ||
      closed.openCards ||
      opened.openCards !== opened.cards ||
      opened.activityItems <
        minimum.commands + minimum.edits + minimum.searches ||
      opened.longItems < 1 ||
      reloaded.openCards ||
      closed.activityItems ||
      reloaded.activityItems ||
      closed.commands < minimum.commands ||
      closed.edits < minimum.edits ||
      closed.searches < minimum.searches ||
      closed.working ||
      closed.rawProcess ||
      closed.titleOverflow ||
      !closed.appRevision ||
      closed.activityRevision !== closed.appRevision ||
      (expectedVersion && closed.appRevision !== expectedVersion) ||
      pageErrors.length
    ) {
      throw new Error(
        `Durable activity browser check failed: ${JSON.stringify({ closed, opened, reloaded, pageErrors })}`,
      );
    }
    console.log(
      `faryo-browser-durable-activity=PASS cards=${closed.cards} commands=${closed.commands} edits=${closed.edits} searches=${closed.searches} ordinary_reload=yes revision=${closed.appRevision}`,
    );
  },
);
