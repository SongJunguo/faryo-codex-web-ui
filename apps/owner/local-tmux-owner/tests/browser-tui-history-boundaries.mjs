import { withBrowser } from "../../../../tools/browser-harness/playwright.mjs";

const targetUrl = process.env.FARYO_SMOKE_URL || "";
const chromeBin = process.env.CHROME_BIN || "/usr/bin/google-chrome";
if (!targetUrl) throw new Error("FARYO_SMOKE_URL is required");
const targetSession =
  new URL(targetUrl).searchParams.get("session") || "faryo1";

const questionKey = "anonymous-jsonl-question";
const quotedTui = [
  "Investigate this literal terminal transcript:",
  "› >_ OpenAI Codex (v0.000.0)",
  "› Ask Codex to do anything",
  "› ? for shortcuts",
  "› Do you trust the contents of this directory?",
  "› Press enter to continue",
  "",
  "Keep \\(x^2+y^2\\) in this same message.",
].join("\n");
const answer = "The quoted prompts remain literal user content.";
const blocks = [
  {
    id: "jsonl-user-anonymous",
    turnKey: questionKey,
    segmentKey: "record-0",
    questionKey,
    kind: "user",
    role: "user",
    text: quotedTui,
    revision: 0,
    final: true,
  },
  {
    id: "jsonl-answer-anonymous",
    turnKey: questionKey,
    segmentKey: "record-1",
    kind: "output",
    role: "assistant",
    text: answer,
    revision: 0,
    final: true,
  },
];
const compatibilityText = `› ${quotedTui}\n\n• ${answer}`;
const capture = {
  ok: true,
  text: compatibilityText,
  messageBlocks: [],
  commandEvents: [],
  captureSource: "codex-jsonl",
  agentSource: "codex-cli",
  agentProfile: "codex",
  backend: "terminal-managed",
  agentRunning: false,
  queuedSendNowAvailable: false,
  streaming: false,
  sessionId: "anonymous-jsonl-thread",
  sessionTitle: "Anonymous TUI history",
  interaction: null,
  interactionRevision: "none",
  streamRevision: 1,
  updatedAt: "2026-01-01T00:00:00Z",
};

await withBrowser(
  {
    executablePath: chromeBin,
    viewport: { width: 390, height: 844 },
    mobile: true,
  },
  async ({ page }) => {
    const pageErrors = [];
    const apiRequests = new Map();
    page.on("pageerror", (error) =>
      pageErrors.push(String(error?.message || error)),
    );
    page.on("request", (request) => {
      const pathname = new URL(request.url()).pathname;
      if (!pathname.startsWith("/api/")) return;
      apiRequests.set(pathname, (apiRequests.get(pathname) || 0) + 1);
    });
    await page.route("**/api/capture**", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(capture),
      }),
    );
    await page.route("**/api/events**", (route) =>
      route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: `event: capture\ndata: ${JSON.stringify(capture)}\n\n`,
      }),
    );
    await page.route("**/api/status**", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ok: true,
          session: targetSession,
          sessionId: capture.sessionId,
          sessionTitle: capture.sessionTitle,
          ownerLabel: "Workstation",
          displayCwd: "~/workspace",
          model: "gpt-example",
          backend: capture.backend,
          agentRunning: false,
          agentState: "waiting",
          interaction: null,
          interactionRevision: "none",
          gitStatus: { available: false },
          updatedAt: capture.updatedAt,
        }),
      }),
    );
    await page.route("**/api/conversation-history**", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ok: true,
          source: "codex-jsonl",
          revision: "anonymous-jsonl-revision",
          totalTurns: 1,
          start: 0,
          end: 1,
          hasOlder: false,
          hasNewer: false,
          olderCursor: "",
          newerCursor: "",
          questions: [
            {
              index: 0,
              key: questionKey,
              preview: "Investigate this literal terminal transcript",
            },
          ],
          turns: [
            {
              index: 0,
              key: questionKey,
              preview: "Investigate this literal terminal transcript",
              text: compatibilityText,
              blocks,
            },
          ],
          updatedAt: capture.updatedAt,
        }),
      }),
    );

    await page.goto(targetUrl, { waitUntil: "domcontentloaded" });
    try {
      await page.waitForFunction(
        () =>
          document.documentElement.dataset.faryoAppReady === "1" &&
          document.querySelectorAll("#output > .compact-block.user").length ===
            1 &&
          document.querySelectorAll("#output > .compact-block.output")
            .length === 1 &&
          document.querySelector("#output > .compact-block.user .katex"),
        null,
        { timeout: 20_000 },
      );
    } catch (error) {
      const diagnostic = await page.evaluate(() => ({
        ready: document.documentElement.dataset.faryoAppReady || "",
        title: document.title,
        userBlocks: document.querySelectorAll("#output > .compact-block.user")
          .length,
        outputBlocks: document.querySelectorAll(
          "#output > .compact-block.output",
        ).length,
        katex: document.querySelectorAll("#output .katex").length,
        notice: document.getElementById("transcriptNotice")?.textContent || "",
      }));
      throw new Error(
        `TUI history fixture did not settle: ${JSON.stringify({ diagnostic, pageErrors, apiRequests: Object.fromEntries(apiRequests) })}`,
        { cause: error },
      );
    }

    const state = await page.evaluate(() => {
      const user = document.querySelector("#output > .compact-block.user");
      return {
        captureSource:
          document.getElementById("output")?.dataset.captureSource || "",
        userBlocks: document.querySelectorAll("#output > .compact-block.user")
          .length,
        outputBlocks: document.querySelectorAll(
          "#output > .compact-block.output",
        ).length,
        processLines: document.querySelectorAll(
          "#output > .compact-process-line",
        ).length,
        keyedUsers: document.querySelectorAll(
          "#output > .compact-block.user[data-faryo-question-key]",
        ).length,
        questionKey: user?.dataset.faryoQuestionKey || "",
        markerCount: document.querySelectorAll(
          "#questionNavMarkers .question-nav-marker",
        ).length,
        questionTotal: Number(
          document.getElementById("questionNavTotal")?.textContent || 0,
        ),
        promptGlyphs: (String(user?.innerText || "").match(/›/gu) || []).length,
        katexCount: user?.querySelectorAll(".katex").length || 0,
        fallbackCount: document.querySelectorAll(
          ".rich-render-fallback,.capture-render-fallback",
        ).length,
        horizontalOverflow:
          document.documentElement.scrollWidth >
          document.documentElement.clientWidth + 1,
      };
    });
    if (
      pageErrors.length ||
      state.captureSource !== "codex-jsonl" ||
      state.userBlocks !== 1 ||
      state.outputBlocks !== 1 ||
      state.processLines !== 0 ||
      state.keyedUsers !== 1 ||
      state.questionKey !== questionKey ||
      state.markerCount !== 0 ||
      state.questionTotal !== 1 ||
      state.promptGlyphs !== 5 ||
      state.katexCount < 1 ||
      state.fallbackCount !== 0 ||
      state.horizontalOverflow
    ) {
      throw new Error(
        `TUI history boundary regression: ${JSON.stringify({ state, pageErrors })}`,
      );
    }

    const composer = await page.evaluate(async () => {
      const input = document.getElementById("promptInput");
      const shell = document.querySelector(".prompt-shell");
      const actions = document.querySelector(".composer-actions");
      const plus = document.getElementById("dockPlusBtn");
      const send = document.getElementById("sendBtn");
      const settle = () =>
        new Promise((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(resolve)),
        );
      input.focus();
      await settle();
      input.value = "Short prompt";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      await settle();
      const short = {
        multiline: shell.classList.contains("composer-multiline"),
        direction: getComputedStyle(actions).flexDirection,
        textWidth: input.getBoundingClientRect().width,
        plus: plus.getBoundingClientRect().toJSON(),
        send: send.getBoundingClientRect().toJSON(),
      };
      input.value =
        "First visual line\nSecond visual line\nThird visual line\nFourth visual line";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      await settle();
      await new Promise((resolve) => setTimeout(resolve, 420));
      const long = {
        multiline: shell.classList.contains("composer-multiline"),
        direction: getComputedStyle(actions).flexDirection,
        textWidth: input.getBoundingClientRect().width,
        plus: plus.getBoundingClientRect().toJSON(),
        send: send.getBoundingClientRect().toJSON(),
      };
      input.value = "Short again";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      await settle();
      const restored = {
        multiline: shell.classList.contains("composer-multiline"),
        direction: getComputedStyle(actions).flexDirection,
      };
      input.value = "";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      return { short, long, restored };
    });
    if (
      composer.short.multiline ||
      composer.short.direction !== "row" ||
      composer.short.plus.left >= composer.short.send.left ||
      !composer.long.multiline ||
      composer.long.direction !== "column" ||
      Math.abs(composer.long.plus.left - composer.long.send.left) > 1 ||
      composer.long.plus.top >= composer.long.send.top ||
      composer.long.textWidth < composer.short.textWidth + 24 ||
      composer.restored.multiline ||
      composer.restored.direction !== "row"
    ) {
      throw new Error(
        `Mobile composer action layout regression: ${JSON.stringify(composer)}`,
      );
    }
  },
);

console.log(
  "faryo-browser-tui-history-boundaries=PASS turns=1 user-blocks=1 literal-prompts=5 question-keys=1 composer=adaptive-actions",
);
