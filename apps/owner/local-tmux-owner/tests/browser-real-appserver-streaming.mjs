import { withBrowser } from "../../../../tools/browser-harness/playwright.mjs";

const targetUrl = process.env.FARYO_SMOKE_URL || "";
const chromeBin = process.env.CHROME_BIN || "/usr/bin/google-chrome";
if (!targetUrl) throw new Error("FARYO_SMOKE_URL is required");

const prompt = `Reply without tools using exactly these Markdown parts:
## Browser Stream

Write a numbered list from 1 through 12. Each item must be a different complete Chinese sentence about reliable incremental rendering.

$a^2+b^2=c^2$

\`\`\`python
print("ok")
\`\`\`

STREAM_BROWSER_DONE`;

await withBrowser(
  {
    executablePath: chromeBin,
    viewport: { width: 390, height: 844 },
    mobile: true,
  },
  async ({ page }) => {
    const pageErrors = [];
    const requestFailures = [];
    const apiResponses = [];
    page.on("pageerror", (error) => pageErrors.push(String(error?.message || error)));
    page.on("requestfailed", (request) =>
      requestFailures.push({
        resource: new URL(request.url()).pathname,
        error: request.failure()?.errorText || "failed",
      }),
    );
    page.on("response", (response) => {
      const url = new URL(response.url());
      if (url.pathname.startsWith("/api/")) {
        apiResponses.push({ resource: url.pathname, status: response.status() });
      }
    });
    await page.goto(targetUrl, { waitUntil: "domcontentloaded" });
    try {
      await page.waitForFunction(
        () =>
          document.documentElement.dataset.faryoAppReady === "1" &&
          document.getElementById("output")?.dataset.captureSource ===
            "codex-app-server",
        null,
        { timeout: 25_000 },
      );
    } catch (error) {
      const initialState = await page.evaluate(() => ({
        appReady: document.documentElement.dataset.faryoAppReady || "",
        captureSource: document.getElementById("output")?.dataset.captureSource || "",
        documentTitle: document.title,
        bodyText: String(document.body?.innerText || "").slice(0, 240),
        scripts: [...document.scripts]
          .map((script) => script.src)
          .filter(Boolean)
          .map((source) => new URL(source).pathname),
      }));
      throw new Error(
        `Real App Server page did not initialize: ${JSON.stringify({ initialState, pageErrors, requestFailures, apiResponses })}`,
        { cause: error },
      );
    }
    await page.evaluate(() => {
      const output = document.getElementById("output");
      const nodeIds = new WeakMap();
      const state = {
        nextNodeId: 1,
        activeNodeIds: new Set(),
        activeLengths: new Set(),
        itemNodeIds: new Map(),
        unstableItem: false,
        observations: [],
        sawMutable: false,
        sawStreaming: false,
        sawProgress: false,
      };
      const sample = () => {
        if (output?.dataset.streaming !== "true") return;
        state.sawStreaming = true;
        if (output.querySelector(".appserver-stream-progress")) state.sawProgress = true;
        const streamItemId = output.dataset.streamItemId;
        if (!streamItemId) return;
        const outputs = output.querySelectorAll(
          ':scope > .compact-block.output[data-faryo-block-mutable="true"]',
        );
        const node = outputs[outputs.length - 1];
        if (!node) return;
        if (!nodeIds.has(node)) nodeIds.set(node, state.nextNodeId++);
        const nodeId = nodeIds.get(node);
        state.activeNodeIds.add(nodeId);
        const itemNodes = state.itemNodeIds.get(streamItemId) || new Set();
        itemNodes.add(nodeId);
        state.itemNodeIds.set(streamItemId, itemNodes);
        if (itemNodes.size > 1) state.unstableItem = true;
        if (
          state.observations.length < 12 &&
          state.observations.at(-1)?.nodeId !== nodeId
        ) {
          state.observations.push({
            nodeId,
            key: node.dataset.faryoBlockKey || "",
            mutable: node.dataset.faryoBlockMutable || "",
            created: output.dataset.compactCreated || "",
            reused: output.dataset.compactReused || "",
          });
        }
        state.activeLengths.add(String(node.innerText || "").length);
        if (node.dataset.faryoBlockMutable === "true") state.sawMutable = true;
      };
      const observer = new MutationObserver(sample);
      observer.observe(output, {
        attributes: true,
        attributeFilter: ["data-streaming", "data-stream-item-id"],
        childList: true,
        characterData: true,
        subtree: true,
      });
      sample();
      globalThis.__faryoRealStream = { state, observer, sample };
    });

    await page.locator("#promptInput").fill(prompt);
    const sendStartedAt = Date.now();
    await page.locator("#sendBtn").click();
    await page.waitForFunction(
      () => document.getElementById("promptInput")?.value === "",
      null,
      { timeout: 10_000 },
    );
    const sendAckMs = Date.now() - sendStartedAt;
    await page.waitForFunction(
      () => {
        const output = document.getElementById("output");
        return (
          output?.dataset.streaming === "false" &&
          output.innerText.includes("STREAM_BROWSER_DONE")
        );
      },
      null,
      { timeout: 180_000 },
    );
    try {
      await page.waitForFunction(
        () =>
          document.querySelectorAll("#output > .compact-block.user").length >= 2 &&
          document.querySelectorAll("#output > .compact-block.output").length >= 2 &&
          document.querySelectorAll("#questionNavMarkers .question-nav-marker:not(.unloaded)").length >= 2,
        null,
        { timeout: 25_000 },
      );
    } catch (error) {
      const roleState = await page.evaluate(() => {
        const output = document.getElementById("output");
        const markers = [
          ...document.querySelectorAll("#questionNavMarkers .question-nav-marker"),
        ];
        return {
          captureSource: output?.dataset.captureSource || "",
          streaming: output?.dataset.streaming || "",
          userBlocks: output?.querySelectorAll(":scope > .compact-block.user").length || 0,
          outputBlocks: output?.querySelectorAll(":scope > .compact-block.output").length || 0,
          processBlocks: output?.querySelectorAll(":scope > .compact-process-line").length || 0,
          markers: markers.length,
          loadedMarkers: markers.filter((marker) => !marker.classList.contains("unloaded")).length,
          historyRequests: performance
            .getEntriesByType("resource")
            .filter((entry) => String(entry.name || "").includes("/api/conversation-history"))
            .length,
          renderFallback: output?.dataset.renderFallback || "",
        };
      });
      throw new Error(
        `Real App Server roles or question anchors did not converge: ${JSON.stringify(roleState)}`,
        { cause: error },
      );
    }

    const jumpRequest = await page.evaluate(() => {
      const markers = [
        ...document.querySelectorAll(
          "#questionNavMarkers .question-nav-marker:not(.unloaded)",
        ),
      ];
      const marker = markers[0];
      const scroller = document.getElementById("outputWrap");
      const request = {
        index: marker?.dataset.questionIndex || "",
        key: marker?.dataset.questionKey || "",
        targetId: marker?.getAttribute("aria-controls") || "",
        before: Number(scroller?.scrollTop || 0),
      };
      marker?.click();
      return request;
    });
    await page.waitForFunction(
      ({ index, key, targetId }) => {
        const active = document.querySelector(
          '#questionNavMarkers .question-nav-marker[aria-current="step"]',
        );
        const scroller = document.getElementById("outputWrap");
        const target = document.getElementById(targetId);
        const scrollerRect = scroller?.getBoundingClientRect();
        const targetRect = target?.getBoundingClientRect();
        const offset =
          scrollerRect && targetRect ? targetRect.top - scrollerRect.top : -1;
        return (
          active?.dataset.questionIndex === index &&
          active?.dataset.questionKey === key &&
          offset >= 0 &&
          offset < Number(scroller?.clientHeight || 0)
        );
      },
      jumpRequest,
      { timeout: 10_000 },
    );
    const questionJump = await page.evaluate(({ targetId, before }) => {
      const scroller = document.getElementById("outputWrap");
      const target = document.getElementById(targetId);
      const scrollerRect = scroller?.getBoundingClientRect();
      const targetRect = target?.getBoundingClientRect();
      const offset =
        scrollerRect && targetRect ? targetRect.top - scrollerRect.top : -1;
      return {
        moved: Math.abs(Number(scroller?.scrollTop || 0) - Number(before)) > 4,
        targetVisible:
          offset >= 0 && offset < Number(scroller?.clientHeight || 0),
        targetUser: Boolean(target?.classList.contains("user")),
      };
    }, jumpRequest);

    const state = await page.evaluate(() => {
      globalThis.__faryoRealStream.sample();
      globalThis.__faryoRealStream.observer.disconnect();
      const stream = globalThis.__faryoRealStream.state;
      const output = document.getElementById("output");
      const appScript = document.querySelector('script[src*="app.js?"]');
      const appRevision = new URL(appScript.src).searchParams.get("v") || "";
      const captureAsset = performance
        .getEntriesByType("resource")
        .map((entry) => entry.name)
        .find((url) => url.includes("/owner/capture-controller.mjs?"));
      const captureRevision = captureAsset
        ? new URL(captureAsset).searchParams.get("v") || ""
        : "";
      return {
        sawStreaming: stream.sawStreaming,
        sawMutable: stream.sawMutable,
        sawProgress: stream.sawProgress,
        activeNodeCount: stream.activeNodeIds.size,
        streamItemCount: stream.itemNodeIds.size,
        unstableItem: stream.unstableItem,
        observations: stream.observations,
        activeLengthCount: stream.activeLengths.size,
        userBlockCount: output.querySelectorAll(":scope > .compact-block.user").length,
        outputBlockCount: output.querySelectorAll(":scope > .compact-block.output").length,
        loadedQuestionMarkers: document.querySelectorAll("#questionNavMarkers .question-nav-marker:not(.unloaded)").length,
        appRevision,
        captureRevision,
        katexCount: output.querySelectorAll(".katex").length,
        codeCount: output.querySelectorAll(".markdown-code-block").length,
        reasoningPlaceholderCount: [...output.children].filter(
          (node) => node.textContent.trim() === "Working",
        ).length,
        openActivityCount: output.querySelectorAll(
          ":scope > .compact-activity-card[open]",
        ).length,
        fallback: output.dataset.renderFallback || "",
        streamItemId: output.dataset.streamItemId || "",
        overflow:
          document.documentElement.scrollWidth >
          document.documentElement.clientWidth + 1,
      };
    });

    if (
      !state.sawStreaming ||
      !state.sawMutable ||
      !state.sawProgress ||
      state.activeNodeCount < 1 ||
      state.streamItemCount < 1 ||
      state.unstableItem ||
      state.activeLengthCount < 2 ||
      state.userBlockCount < 2 ||
      state.outputBlockCount < 2 ||
      state.loadedQuestionMarkers < 2 ||
      !questionJump.moved ||
      !questionJump.targetVisible ||
      !questionJump.targetUser ||
      !state.appRevision ||
      state.captureRevision !== state.appRevision ||
      state.katexCount < 2 ||
      state.codeCount < 2 ||
      state.reasoningPlaceholderCount ||
      state.openActivityCount ||
      state.fallback ||
      state.streamItemId ||
      state.overflow ||
      sendAckMs > 2_000 ||
      pageErrors.length
    ) {
      throw new Error(
        `Real App Server browser stream failed: ${JSON.stringify({ ...state, questionJump, sendAckMs, pageErrors })}`,
      );
    }

    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForFunction(
      () =>
        document.documentElement.dataset.faryoAppReady === "1" &&
        document.getElementById("output")?.innerText.includes(
          "STREAM_BROWSER_DONE",
        ) &&
        document.getElementById("output")?.querySelectorAll(".katex").length >=
          2,
      null,
      { timeout: 25_000 },
    );
    console.log(
      `faryo-browser-real-appserver=PASS send_ack_ms=${sendAckMs} frames=${state.activeLengthCount} roles=yes progress=yes loaded_markers=${state.loadedQuestionMarkers} question_jump=yes keyed_node=yes markdown=yes katex=yes ordinary_reload=yes revision=${state.appRevision}`,
    );
  },
);
