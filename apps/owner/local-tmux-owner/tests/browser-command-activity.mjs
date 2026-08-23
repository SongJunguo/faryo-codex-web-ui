import { readFile } from "node:fs/promises";

import { withBrowser } from "../../../../tools/browser-harness/playwright.mjs";


const targetUrl = process.env.FARYO_SMOKE_URL || "";
const loginUser = process.env.FARYO_SMOKE_LOGIN_USER || "";
const passwordFile = process.env.FARYO_SMOKE_LOGIN_PASSWORD_FILE || "";
const loginPassword = passwordFile ? (await readFile(passwordFile, "utf8")).trim() : "";
const chromeBin = process.env.CHROME_BIN || "/usr/bin/google-chrome";
if (!targetUrl) throw new Error("FARYO_SMOKE_URL is required");

const turnA = "appserver-turn-anonymous-a";
const turnB = "appserver-turn-anonymous-b";
const completedActivities = [
  {
    id: "appserver-item-0123456789abcdef",
    turnKey: turnA,
    kind: "process",
    role: "process",
    text: "Ran printf anonymous · exit 0",
    final: true,
    activity: {
      type: "command",
      status: "completed",
      title: "printf anonymous",
      summary: "Command finished",
      detailKind: "command_output",
      detailAvailable: true,
      exitCode: 0,
      durationMs: 12,
    },
  },
  ...Array.from({ length: 999 }, (_, index) => ({
    id: `appserver-item-${String(index + 1).padStart(16, "0")}`,
    turnKey: turnA,
    kind: "process",
    role: "process",
    text: `Ran anonymous task ${index + 2} · exit 0`,
    final: true,
    activity: {
      type: "command",
      status: "completed",
      title: `anonymous task ${index + 2}`,
      summary: "Command finished",
      detailKind: "command_output",
      detailAvailable: false,
      exitCode: 0,
    },
  })),
];
const turnABlocks = [
  { id: "user-a", turnKey: turnA, questionKey: turnA, kind: "user", role: "user", text: "Run an anonymous check", final: true },
  ...completedActivities,
  { id: "answer-a", turnKey: turnA, kind: "output", role: "assistant", text: "The check passed with \(x^2\).", final: true },
];
const turnBBlocks = [
  { id: "user-b", turnKey: turnB, questionKey: turnB, kind: "user", role: "user", text: "Try a file update", final: true },
  {
    id: "appserver-item-fedcba9876543210",
    turnKey: turnB,
    kind: "process",
    role: "process",
    text: "Edited sample.txt",
    final: true,
    activity: {
      type: "file_change",
      status: "failed",
      title: "sample.txt",
      summary: "1 file change",
      detailKind: "file_changes",
      detailAvailable: true,
      changeCount: 1,
    },
  },
  { id: "answer-b", turnKey: turnB, kind: "output", role: "assistant", text: "The update was not applied.", final: true },
];
const messageBlocks = [...turnABlocks, ...turnBBlocks];
const commandEvents = [{
  id: "cmd_abcdefghijklmnop",
  kind: "command",
  name: "/rename",
  label: "Conversation title",
  summary: "Renamed conversation to “Anonymous session”",
  status: "completed",
  anchorKey: turnB,
  startedAt: 10,
  completedAt: 11,
  final: true,
}];
const capture = {
  ok: true,
  text: "› Run an anonymous check\n\n• The check passed with \\(x^2\\).\n\n› Try a file update\n\n• The update was not applied.",
  messageBlocks,
  commandEvents,
  captureSource: "codex-app-server",
  agentSource: "codex-app-server",
  agentProfile: "codex",
  backend: "web-managed",
  agentRunning: false,
  queuedSendNowAvailable: false,
  streaming: false,
  sessionId: "anonymous-thread",
  sessionTitle: "Anonymous session",
  interaction: null,
  interactionRevision: "none",
  streamRevision: 1,
  updatedAt: "2026-01-01T00:00:00Z",
};

await withBrowser(
  { executablePath: chromeBin, viewport: { width: 390, height: 844 }, mobile: true },
  async ({ page }) => {
    let detailRequests = 0;
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(String(error?.message || error)));
    await page.route("**/api/capture**", (route) => route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(capture),
    }));
    await page.route("**/api/events**", (route) => route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: `event: capture\ndata: ${JSON.stringify(capture)}\n\n`,
    }));
    await page.route("**/api/status**", (route) => route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ok: true,
        session: "faryo1",
        sessionId: capture.sessionId,
        sessionTitle: capture.sessionTitle,
        ownerLabel: "Workstation",
        displayCwd: "~/workspace",
        model: "gpt-example",
        backend: "web-managed",
        agentRunning: false,
        agentState: "waiting",
        interaction: null,
        interactionRevision: "none",
        gitStatus: { available: false },
        updatedAt: capture.updatedAt,
      }),
    }));
    await page.route("**/api/conversation-history**", (route) => route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ok: true,
        source: "codex-app-server",
        revision: "anonymous-revision",
        totalTurns: 2,
        start: 0,
        end: 2,
        hasOlder: false,
        hasNewer: false,
        olderCursor: "",
        questions: [
          { index: 0, key: turnA, preview: "Run an anonymous check" },
          { index: 1, key: turnB, preview: "Try a file update" },
        ],
        turns: [
          { index: 0, key: turnA, preview: "Run an anonymous check", text: capture.text.split("\n\n› Try")[0], blocks: turnABlocks },
          { index: 1, key: turnB, preview: "Try a file update", text: "› Try a file update\n\n• The update was not applied.", blocks: turnBBlocks },
        ],
        updatedAt: capture.updatedAt,
      }),
    }));
    await page.route("**/api/activity-detail**", (route) => {
      detailRequests += 1;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ok: true,
          item: "appserver-item-0123456789abcdef",
          detail: {
            type: "command",
            status: "completed",
            title: "printf anonymous",
            command: "printf anonymous",
            cwd: "/workspace",
            output: "anonymous command output",
            exitCode: 0,
            durationMs: 12,
            truncated: false,
          },
        }),
      });
    });

    await page.goto(targetUrl, { waitUntil: "domcontentloaded" });
    if (loginUser && loginPassword) {
      const username = page.locator('input[name="username"]');
      if (await username.count()) {
        await username.fill(loginUser);
        await page.locator('input[name="password"]').fill(loginPassword);
        await page.locator("form").evaluate((form) => form.requestSubmit());
      }
    }
    await page.waitForFunction(() => (
      document.documentElement.dataset.faryoAppReady === "1"
      && document.querySelectorAll("#output > .compact-activity-card").length === 2
      && document.querySelectorAll("#output > .command-timeline-row").length === 1
    ), null, { timeout: 25_000 });

    const initial = await page.evaluate(() => {
      const cards = [...document.querySelectorAll("#output > .compact-activity-card")];
      return {
        open: cards.map((card) => card.open),
        commandRows: document.querySelectorAll("#output > .command-timeline-row").length,
        commandText: document.querySelector("#output > .command-timeline-row")?.textContent || "",
        failedVisible: cards[1]?.textContent.includes("failed") || false,
        completedSummary: cards[0]?.querySelector(":scope > summary")?.textContent || "",
        collapsedItems: cards[0]?.querySelectorAll(".compact-activity-item").length || 0,
        detailBodies: document.querySelectorAll(".activity-detail-body[data-state=ready]").length,
        overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
      };
    });
    if (detailRequests || initial.open[0] || !initial.open[1] || initial.commandRows !== 1
      || !initial.commandText.includes("Renamed conversation") || !initial.failedVisible
      || !initial.completedSummary.includes("1,000 commands") || initial.collapsedItems
      || initial.detailBodies || initial.overflow) {
      throw new Error(`Initial activity hierarchy failed: ${JSON.stringify({ initial, detailRequests })}`);
    }

    const firstCard = page.locator("#output > .compact-activity-card").first();
    await firstCard.locator(":scope > summary").click();
    await firstCard.locator(".compact-activity-item-long > summary").click();
    await page.waitForFunction(() => document.querySelector(".activity-detail-output pre")?.textContent.includes("anonymous command output"));
    if (detailRequests !== 1) throw new Error(`Detail was not loaded exactly once: ${detailRequests}`);

    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => (
      document.documentElement.dataset.faryoAppReady === "1"
      && document.querySelectorAll("#output > .compact-activity-card").length === 2
      && document.querySelectorAll("#output > .command-timeline-row").length === 1
    ), null, { timeout: 25_000 });
    const reloaded = await page.evaluate(() => ({
      cards: document.querySelectorAll("#output > .compact-activity-card").length,
      commands: document.querySelectorAll("#output > .command-timeline-row").length,
      readyDetails: document.querySelectorAll(".activity-detail-body[data-state=ready]").length,
      activityRevision: new URL(
        performance.getEntriesByType("resource").map((entry) => entry.name)
          .find((url) => url.includes("/owner/activity-groups.mjs?")),
      ).searchParams.get("v") || "",
      appRevision: new URL(document.querySelector('script[src*="app.js?"]').src).searchParams.get("v") || "",
    }));
    if (reloaded.cards !== 2 || reloaded.commands !== 1 || reloaded.readyDetails || detailRequests !== 1
      || !reloaded.activityRevision || reloaded.activityRevision !== reloaded.appRevision || pageErrors.length) {
      throw new Error(`Ordinary reload activity contract failed: ${JSON.stringify({ reloaded, detailRequests, pageErrors })}`);
    }
    console.log(`faryo-browser-command-activity=PASS typed=yes command-row=yes on-demand=yes ordinary-reload=yes revision=${reloaded.appRevision}`);
  },
);
