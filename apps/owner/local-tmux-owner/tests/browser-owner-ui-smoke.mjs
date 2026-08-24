import { readFile } from "node:fs/promises";
import path from "node:path";

import { withBrowser } from "../../../../tools/browser-harness/playwright.mjs";

const root = path.resolve(import.meta.dirname, "../../../..");
const bundle = await readFile(
  path.join(root, "apps/owner/local-tmux-owner/static/owner-ui.js"),
  "utf8",
);
const styles = await readFile(
  path.join(root, "apps/owner/local-tmux-owner/static/style.css"),
  "utf8",
);
const chromeBin = process.env.CHROME_BIN || "/usr/bin/google-chrome";

for (const viewport of [
  { width: 390, height: 844 },
  { width: 432, height: 900 },
  { width: 1280, height: 800 },
]) {
  await withBrowser(
    {
      executablePath: chromeBin,
      viewport,
      mobile: viewport.width < 720,
    },
    async ({ page }) => {
      await page.setContent(
        '<meta name="viewport" content="width=device-width, initial-scale=1"><div class="app"><div id="status"></div><div id="composer"></div><div id="transcript"></div></div><div id="root"></div>',
      );
      await page.addStyleTag({ content: styles });
      await page.addScriptTag({ content: bundle });
      await page.evaluate(() => {
        document.documentElement.dataset.faryoUi = "workbench-v2";
        window.__composerClicks = [];
        window.__goalClicks = 0;
        window.__fastClicks = 0;
        window.__statusController = window.FaryoOwnerUI.mountStatusShell(
          document.getElementById("status"),
          {
            onGoalClick() {
              window.__goalClicks += 1;
            },
            onFastToggle() {
              window.__fastClicks += 1;
              return new Promise((resolve) => {
                window.__resolveFastToggle = resolve;
              });
            },
          },
        );
        window.__statusController.update({
          contextText: "Ctx 42% · 108k/258k",
          contextTitle: "108,000 / 258,000 tokens",
          quotaText: "Week 58% left",
          quotaTitle: "Weekly quota",
          quotaPercent: 42,
          quotaWeekPercent: 51,
          modelText: "GPT Example",
          modelTitle: "gpt-example",
          fastVisible: true,
          fastActive: false,
          fastDisabled: false,
          fastText: "Default",
          fastTitle: "Default speed for this conversation",
          subtitleTitle: "Synthetic status",
          goalVisible: true,
          goalText: "Goal Active",
          goalTitle: "Goal status · Active",
          goalTone: "active",
          gitText: "🌿 main",
          gitTitle: "Clean",
          gitState: "clean",
        });
        window.__composerController = window.FaryoOwnerUI.mountComposerShell(
          document.getElementById("composer"),
          {
            onSuggestionSelect(index) {
              window.__composerClicks.push(index);
            },
          },
        );
        window.__composerController.updateControls({
          sendVisible: true,
          plusVisible: false,
        });
        window.__composerController.updateSuggestions(
          [
            {
              label: "<img src=x onerror=alert(1)>",
              hint: "",
              description: "Synthetic command",
              category: "Test",
              aliases: "",
              risk: "unclassified",
            },
            {
              label: "/second",
              hint: "",
              description: "Second synthetic command",
              category: "Test",
              aliases: "",
              risk: "",
            },
          ],
          "2 commands",
        );
        window.__conversationStore =
          window.FaryoOwnerUI.createConversationStore({
            session: "alpha",
            mode: "compact",
          });
        window.__transcriptController =
          window.FaryoOwnerUI.mountTranscriptShell(
            document.getElementById("transcript"),
            window.__conversationStore,
          );
        const retained = document.createElement("section");
        retained.id = "legacyTranscriptChild";
        retained.textContent = "retained legacy body";
        window.__transcriptController.output.appendChild(retained);
        window.__interactionRequests = [];
        window.__interactionController =
          window.FaryoOwnerUI.mountInteractionHost(
            document.getElementById("root"),
            {
              async onRespond(request) {
                window.__interactionRequests.push(request);
                if (request.optionId === "opt-slow") {
                  return await new Promise((resolve) => {
                    window.__resolveSlowInteraction = resolve;
                  });
                }
                if (request.optionId === "opt-model-b") {
                  return {
                    interaction: {
                      id: "ix-reasoning",
                      generation: 2,
                      kind: "reasoning_select",
                      title: "Select reasoning level",
                      prompt: "Choose reasoning.",
                      options: [
                        {
                          id: "opt-high",
                          label: "High",
                          description: "Greater reasoning depth",
                          selected: true,
                          current: false,
                          disabled: false,
                        },
                      ],
                      actions: ["previous", "next", "choose", "cancel"],
                      source: "codex-tui",
                      status: "pending",
                    },
                  };
                }
                return { interaction: null, resolved: true };
              },
            },
          );
        window.__modelInteraction = {
          id: "ix-model",
          generation: 1,
          kind: "model_select",
          title: "Select model",
          prompt: "Choose the model used by the next turn.",
          options: [
            {
              id: "opt-model-a",
              label: "<img src=x onerror=alert(1)>",
              description: "Current model",
              selected: true,
              current: true,
              disabled: false,
            },
            {
              id: "opt-model-b",
              label: "Model B",
              description: "Balanced model",
              selected: false,
              current: false,
              disabled: false,
            },
          ],
          actions: ["previous", "next", "choose", "cancel"],
          source: "codex-tui",
          status: "pending",
        };
        window.__interactionController.update(window.__modelInteraction);
      });
      await page
        .locator('.interaction-backdrop[data-interaction-kind="model_select"]')
        .waitFor();
      await page.evaluate(() => window.__interactionController.update(null));
      await page
        .locator(".interaction-backdrop")
        .waitFor({ state: "detached" });
      const composer = await page.evaluate(() => ({
        prompt: Boolean(document.getElementById("promptInput")),
        sendVisible: !document
          .getElementById("sendBtn")
          .classList.contains("hidden"),
        plusHidden: document
          .getElementById("dockPlusBtn")
          .classList.contains("hidden"),
        actionGroup: Boolean(
          document.querySelector(".prompt-shell > .composer-actions"),
        ),
        suggestionCount: document.querySelectorAll(
          "#commandSuggest [role=option]",
        ).length,
        injectedImage: Boolean(document.querySelector("#commandSuggest img")),
        status: {
          context: document.getElementById("ctxText")?.textContent || "",
          goal: document.getElementById("goalPill")?.textContent || "",
          git: document.getElementById("phasePill")?.textContent || "",
          fast: document.getElementById("fastToggle")?.textContent || "",
          fastPressed:
            document
              .getElementById("fastToggle")
              ?.getAttribute("aria-pressed") || "",
          fastDisabled: Boolean(
            document.getElementById("fastToggle")?.disabled,
          ),
        },
      }));
      if (
        !composer.prompt ||
        !composer.sendVisible ||
        !composer.plusHidden ||
        !composer.actionGroup ||
        composer.suggestionCount !== 2 ||
        composer.injectedImage ||
        composer.status.context !== "Ctx 42% · 108k/258k" ||
        composer.status.goal !== "Goal Active" ||
        composer.status.git !== "🌿 main" ||
        composer.status.fast !== "Default" ||
        composer.status.fastPressed !== "false" ||
        composer.status.fastDisabled
      ) {
        throw new Error(
          `Owner composer shell failed: ${JSON.stringify({ viewport, composer })}`,
        );
      }
      const statusRail = await page.evaluate(async () => {
        const rail = document.querySelector(".meta-row");
        const model = document.getElementById("modelText");
        const initial = rail.scrollLeft;
        const maximum = Math.max(0, rail.scrollWidth - rail.clientWidth);
        rail.scrollLeft = maximum;
        await new Promise((resolve) => requestAnimationFrame(resolve));
        return {
          tabIndex: rail.tabIndex,
          overflowX: getComputedStyle(rail).overflowX,
          scrollbarWidth: getComputedStyle(rail).scrollbarWidth,
          initial,
          maximum,
          final: rail.scrollLeft,
          modelDisplay: getComputedStyle(model).display,
          pageOverflow:
            document.documentElement.scrollWidth >
            document.documentElement.clientWidth + 1,
        };
      });
      if (
        viewport.width < 720 &&
        (statusRail.tabIndex !== 0 ||
          statusRail.overflowX !== "auto" ||
          statusRail.scrollbarWidth !== "none" ||
          statusRail.maximum <= 0 ||
          statusRail.final < statusRail.maximum - 1 ||
          statusRail.modelDisplay === "none" ||
          statusRail.pageOverflow)
      ) {
        throw new Error(
          `Owner status rail failed: ${JSON.stringify({ viewport, statusRail })}`,
        );
      }
      const transcript = await page.evaluate(async () => {
        const frame = () =>
          new Promise((resolve) => requestAnimationFrame(resolve));
        const oldScope = window.__conversationStore.scope();
        window.__conversationStore.switchSession("beta");
        const staleAccepted = window.__conversationStore.commitCapture(
          {
            captureSource: "codex-jsonl",
            agentSource: "codex-cli",
            text: "obsolete",
          },
          oldScope,
        );
        const emptyAccepted = window.__conversationStore.commitCapture(
          { captureSource: "codex-empty", agentSource: "codex-cli", text: "" },
          window.__conversationStore.scope(),
        );
        await frame();
        const emptyPhase =
          document.querySelector(".transcript-shell")?.dataset
            .conversationPhase;
        window.__conversationStore.setMode("full");
        await frame();
        const rawLoadingPhase =
          document.querySelector(".transcript-shell")?.dataset
            .conversationPhase;
        window.__conversationStore.commitCapture(
          { captureSource: "tmux", agentSource: "codex-cli", text: "raw" },
          window.__conversationStore.scope(),
        );
        await frame();
        const rawReadyPhase =
          document.querySelector(".transcript-shell")?.dataset
            .conversationPhase;
        window.__conversationStore.setMode("compact");
        window.__conversationStore.commitCapture(
          { captureSource: "tmux", agentSource: "codex-cli", text: "fallback" },
          window.__conversationStore.scope(),
        );
        await frame();
        return {
          staleAccepted,
          emptyAccepted,
          emptyPhase,
          rawLoadingPhase,
          rawReadyPhase,
          fallbackPhase:
            document.querySelector(".transcript-shell")?.dataset
              .conversationPhase,
          legacyRetained: Boolean(
            document.getElementById("legacyTranscriptChild"),
          ),
        };
      });
      if (
        transcript.staleAccepted ||
        !transcript.emptyAccepted ||
        transcript.emptyPhase !== "empty" ||
        transcript.rawLoadingPhase !== "loading" ||
        transcript.rawReadyPhase !== "ready" ||
        transcript.fallbackPhase !== "fallback" ||
        !transcript.legacyRetained
      ) {
        throw new Error(
          `Owner transcript state failed: ${JSON.stringify(transcript)}`,
        );
      }
      await page.locator("#fastToggle").click();
      await page.waitForFunction(() => Boolean(window.__resolveFastToggle));
      const fastBusy = await page.evaluate(() => ({
        clicks: window.__fastClicks,
        busy:
          document.getElementById("fastToggle")?.getAttribute("aria-busy") ||
          "",
        disabled: Boolean(document.getElementById("fastToggle")?.disabled),
      }));
      if (
        fastBusy.clicks !== 1 ||
        fastBusy.busy !== "true" ||
        !fastBusy.disabled
      )
        throw new Error(
          `Fast toggle busy state failed: ${JSON.stringify(fastBusy)}`,
        );
      await page.evaluate(() => window.__resolveFastToggle());
      await page.waitForFunction(
        () =>
          document.getElementById("fastToggle")?.getAttribute("aria-busy") ===
          "false",
      );
      await page.evaluate(() =>
        window.__statusController.update({
          fastActive: true,
          fastText: "Fast",
          fastTitle: "Fast is enabled for this conversation",
        }),
      );
      const fastActive = await page.evaluate(() => ({
        text: document.getElementById("fastToggle")?.textContent || "",
        pressed:
          document.getElementById("fastToggle")?.getAttribute("aria-pressed") ||
          "",
        active:
          document.getElementById("fastToggle")?.classList.contains("active") ||
          false,
      }));
      if (
        fastActive.text !== "Fast" ||
        fastActive.pressed !== "true" ||
        !fastActive.active
      )
        throw new Error(
          `Fast toggle active state failed: ${JSON.stringify(fastActive)}`,
        );
      await page.evaluate(() => document.getElementById("goalPill").click());
      if ((await page.evaluate(() => window.__goalClicks)) !== 1)
        throw new Error("Status Goal callback failed");
      await page.evaluate(() =>
        document.querySelector("#commandSuggest [role=option]").click(),
      );
      if ((await page.evaluate(() => window.__composerClicks[0])) !== 0)
        throw new Error("Composer suggestion callback failed");
      await page.locator("#promptInput").focus();
      await page.keyboard.press("ArrowDown");
      const selectedCommand = await page
        .locator('#commandSuggest [aria-selected="true"]')
        .getAttribute("id");
      if (selectedCommand !== "command-option-1")
        throw new Error(
          `Composer keyboard selection failed: ${selectedCommand}`,
        );
      await page.keyboard.press("Enter");
      if ((await page.evaluate(() => window.__composerClicks[1])) !== 1)
        throw new Error("Composer keyboard activation failed");
      await page.evaluate(() =>
        window.__interactionController.update(window.__modelInteraction),
      );
      await page
        .locator('.interaction-backdrop[data-interaction-kind="model_select"]')
        .waitFor();
      const first = await page.evaluate(() => {
        const sheet = document.querySelector(".interaction-sheet");
        const rect = sheet.getBoundingClientRect();
        return {
          optionCount: document.querySelectorAll(".interaction-option").length,
          injectedImage: Boolean(
            document.querySelector(".interaction-option img"),
          ),
          horizontalOverflow:
            document.documentElement.scrollWidth >
            document.documentElement.clientWidth + 1,
          sheetBottom: Math.round(rect.bottom),
          viewportBottom: innerHeight,
        };
      });
      if (
        first.optionCount !== 2 ||
        first.injectedImage ||
        first.horizontalOverflow ||
        first.sheetBottom > first.viewportBottom + 1
      ) {
        throw new Error(
          `Owner interaction baseline failed: ${JSON.stringify({ viewport, first })}`,
        );
      }
      await page.locator(".interaction-option").nth(1).click();
      await page
        .locator(
          '.interaction-backdrop[data-interaction-kind="reasoning_select"]',
        )
        .waitFor();
      const request = await page.evaluate(
        () => window.__interactionRequests[0],
      );
      if (
        request.interactionId !== "ix-model" ||
        request.optionId !== "opt-model-b"
      ) {
        throw new Error(`Option response mismatch: ${JSON.stringify(request)}`);
      }
      await page.waitForTimeout(60);
      await page.keyboard.press("Escape");
      await page
        .locator(".interaction-backdrop")
        .waitFor({ state: "detached" });
      const finalRequest = await page.evaluate(() =>
        window.__interactionRequests.at(-1),
      );
      if (
        finalRequest.interactionId !== "ix-reasoning" ||
        finalRequest.action !== "cancel"
      ) {
        throw new Error(
          `Keyboard cancel mismatch: ${JSON.stringify(finalRequest)}`,
        );
      }
      await page.evaluate(() => {
        window.__confirmResult = window.__interactionController.confirmCommand({
          command: "/future-command <img src=x onerror=alert(1)>",
          description: "Unclassified command",
          risk: "unclassified",
        });
      });
      await page.locator('[data-interaction-kind="command_confirm"]').waitFor();
      const confirmation = await page.evaluate(() => ({
        injectedImage: Boolean(
          document.querySelector(".interaction-confirm-sheet img"),
        ),
        command:
          document.querySelector(".interaction-confirm-command")?.textContent ||
          "",
      }));
      if (confirmation.injectedImage || !confirmation.command.includes("<img"))
        throw new Error(
          `Command confirmation escaping failed: ${JSON.stringify(confirmation)}`,
        );
      await page.locator(".interaction-confirm-sheet button").first().click();
      if (await page.evaluate(async () => await window.__confirmResult))
        throw new Error("Cancelled command confirmation resolved true");

      await page.evaluate(() => {
        window.__interactionController.update({
          id: "ix-old",
          generation: 3,
          kind: "generic_tui",
          title: "Old menu",
          prompt: "Synthetic delayed interaction.",
          options: [
            {
              id: "opt-slow",
              label: "Slow option",
              description: "Delayed synthetic response",
              selected: true,
              current: false,
              disabled: false,
            },
          ],
          actions: ["choose", "cancel"],
          source: "codex-tui",
          status: "pending",
        });
      });
      await page.locator(".interaction-option").click();
      await page.waitForFunction(() =>
        Boolean(window.__resolveSlowInteraction),
      );
      await page.evaluate(() => {
        window.__interactionController.update({
          id: "ix-new",
          generation: 4,
          kind: "generic_tui",
          title: "New menu",
          prompt: "This interaction must survive the old response.",
          options: [],
          actions: ["choose", "cancel"],
          source: "codex-tui",
          status: "pending",
        });
        window.__resolveSlowInteraction({ interaction: null, resolved: true });
      });
      await page.waitForTimeout(60);
      const lateResponseState = await page.evaluate(() => ({
        title: document.getElementById("interactionTitle")?.textContent || "",
        chooseVisible: Boolean(
          document.querySelector(".interaction-choose")?.getClientRects()
            .length,
        ),
      }));
      if (
        lateResponseState.title !== "New menu" ||
        !lateResponseState.chooseVisible
      ) {
        throw new Error(
          `Late interaction response replaced current state: ${JSON.stringify(lateResponseState)}`,
        );
      }
      await page.locator(".interaction-choose").click();
      await page
        .locator(".interaction-backdrop")
        .waitFor({ state: "detached" });
      const chooseRequest = await page.evaluate(() =>
        window.__interactionRequests.at(-1),
      );
      if (
        chooseRequest.interactionId !== "ix-new" ||
        chooseRequest.action !== "choose"
      ) {
        throw new Error(
          `Choose action mismatch: ${JSON.stringify(chooseRequest)}`,
        );
      }

      await page.evaluate(() => {
        window.__interactionController.update({
          id: "ix-question",
          generation: 5,
          kind: "user_input",
          title: "Codex needs your input",
          prompt: "Answer before continuing.",
          responseKind: "questions",
          details: { command: "<img src=x onerror=alert(1)>" },
          questions: [
            {
              id: "choice",
              header: "Choice",
              question: "Select one value",
              options: [
                { label: "Alpha", description: "First option" },
                { label: "Beta", description: "Second option" },
              ],
              isOther: true,
              isSecret: false,
            },
          ],
          options: [],
          actions: ["cancel"],
          source: "codex-app-server",
          status: "pending",
        });
      });
      await page.locator('[data-interaction-kind="user_input"]').waitFor();
      const questionState = await page.evaluate(() => ({
        injectedImage: Boolean(
          document.querySelector(".interaction-request-details img"),
        ),
        command:
          document.querySelector(".interaction-request-details pre")
            ?.textContent || "",
        submitDisabled: Boolean(
          document.querySelector(".interaction-submit-answers")?.disabled,
        ),
      }));
      if (
        questionState.injectedImage ||
        !questionState.command.includes("<img") ||
        !questionState.submitDisabled
      ) {
        throw new Error(
          `Question form baseline failed: ${JSON.stringify(questionState)}`,
        );
      }
      await page
        .locator('.interaction-question-option input[value="Beta"]')
        .check();
      await page.locator(".interaction-submit-answers").click();
      await page
        .locator(".interaction-backdrop")
        .waitFor({ state: "detached" });
      const questionRequest = await page.evaluate(() =>
        window.__interactionRequests.at(-1),
      );
      if (
        questionRequest.interactionId !== "ix-question" ||
        questionRequest.answers?.choice?.[0] !== "Beta"
      ) {
        throw new Error(
          `Question response mismatch: ${JSON.stringify(questionRequest)}`,
        );
      }
    },
  );
}

console.log(
  "faryo-owner-interaction-ui=PASS mobile=yes desktop=yes injection=text stale-response=isolated choose=explicit questions=form",
);
