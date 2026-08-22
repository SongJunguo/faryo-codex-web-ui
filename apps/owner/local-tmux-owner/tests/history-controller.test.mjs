import assert from "node:assert/strict";
import test from "node:test";

import {
  createHistoryController,
  emptyConversationHistory,
  isStructuredCapture,
  mergeMessageBlocks,
  normalizeMessageBlocks,
} from "../static/owner/history-controller.mjs";

const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

function fixture(overrides = {}) {
  const output = { querySelectorAll: () => [] };
  const view = {
    AbortController,
    clearTimeout,
    setTimeout,
  };
  const controller = createHistoryController({
    view,
    output,
    scroller: { scrollTop: 0 },
    api: async () => ({
      revision: "rev-a",
      totalTurns: 3,
      start: 0,
      olderCursor: "older-a",
      questions: [{ key: "q0", preview: "First" }, { key: "q1", preview: "Second" }, { key: "q2", preview: "Third" }],
      turns: [{ index: 0, key: "q0", text: "› First\n\n• Answer" }, { index: 2, key: "q2", text: "› Third\n\n• Answer" }],
    }),
    apiPath: (path) => path,
    getSelectedSession: () => "codex",
    getExpectedSessionId: (fallback) => fallback || "thread-a",
    getLastCapture: () => null,
    getOutputMode: () => "compact",
    renderCapture() {},
    anchorSnapshot: () => null,
    restoreAnchor() {},
    isInitialLatestPending: () => false,
    applyInitialLatestScroll() {},
    beginInitialLatestScroll() {},
    cancelInitialLatestScroll() {},
    isNearBottom: () => true,
    scrollBottom() {},
    setError() {},
    userErrorMessage: (error) => error.message,
    handleBackgroundError() {},
    ...overrides,
  });
  return controller;
}

test("history state starts private and empty", () => {
  const state = emptyConversationHistory();
  assert.equal(state.initialized, false);
  assert.equal(state.turns.size, 0);
  assert.deepEqual(state.questions, []);
});

test("history pages merge intact turns and expose unloaded gaps", async () => {
  const controller = fixture();
  await controller.load({ latest: true });

  assert.equal(controller.initialized, true);
  assert.equal(controller.totalTurns, 3);
  assert.equal(controller.questions.length, 3);
  assert.equal(controller.loadedTurns().length, 2);
  const merged = controller.mergedCapture({ captureSource: "codex-jsonl", sessionId: "thread-a", text: "tail" });
  assert.match(merged.text, /First/);
  assert.match(merged.text, /1 earlier turn not loaded/);
  assert.equal(merged.historyTotalTurns, 3);
});

test("revision changes replace stale turn state", () => {
  const controller = fixture();
  controller.mergePage({ revision: "rev-a", totalTurns: 1, start: 0, turns: [{ index: 0, text: "old" }] }, "thread-a");
  controller.mergePage({ revision: "rev-b", totalTurns: 1, start: 0, turns: [{ index: 0, text: "new" }] }, "thread-a");
  assert.equal(controller.loadedTurns()[0].text, "new");
});

test("App Server live blocks extend history instead of being replaced by it", () => {
  const controller = fixture();
  controller.mergePage({
    revision: "rev-structured",
    totalTurns: 1,
    start: 0,
    questions: [{ index: 0, key: "turn-0", preview: "Question" }],
    turns: [{
      index: 0,
      key: "turn-0",
      text: "› Question\n\n• Old answer",
      blocks: [
        { id: "user-0", kind: "user", role: "user", text: "Question", questionKey: "turn-0" },
        { id: "answer-0", kind: "output", role: "assistant", text: "Old answer" },
      ],
    }],
  }, "thread-a");

  const merged = controller.mergedCapture({
    captureSource: "codex-app-server",
    sessionId: "thread-a",
    streaming: true,
    text: "live compatibility text",
    messageBlocks: [
      { id: "user-0", kind: "user", role: "user", text: "Question", questionKey: "turn-0" },
      { id: "answer-0", kind: "output", role: "assistant", text: "Growing answer", final: false },
      { id: "user-1", kind: "user", role: "user", text: "Current question", questionKey: "turn-1" },
      { id: "answer-1", kind: "output", role: "assistant", text: "Partial", final: false },
    ],
  });

  assert.equal(merged.text, "live compatibility text");
  assert.deepEqual(merged.messageBlocks.map((block) => block.id), ["user-0", "answer-0", "user-1", "answer-1"]);
  assert.equal(merged.messageBlocks[1].text, "Growing answer");
  assert.equal(merged.messageBlocks[3].final, false);
});

test("structured block merging retains history order and replaces matching live items", () => {
  const merged = mergeMessageBlocks(
    [{ id: "q", kind: "user", text: "Question" }, { id: "a", kind: "output", text: "Old" }],
    [{ id: "a", kind: "output", text: "Partial", final: false }, { id: "p", kind: "process", text: "Working" }],
  );
  assert.deepEqual(merged.map((block) => block.id), ["q", "a", "p"]);
  assert.equal(merged[1].text, "Partial");
});

test("structured capture detection stays source-specific", () => {
  assert.equal(isStructuredCapture({ captureSource: "codex-jsonl" }), true);
  assert.equal(isStructuredCapture({ captureSource: "codex-app-server" }), true);
  assert.equal(isStructuredCapture({ captureSource: "codex-empty" }), true);
  assert.equal(isStructuredCapture({ captureSource: "tmux" }), false);
});

test("typed activity metadata survives history normalization with an allowlist", () => {
  const [block] = normalizeMessageBlocks([{
    id: "tool-a",
    turnKey: "turn-a",
    kind: "process",
    text: "Called a tool",
    activity: {
      type: "tool",
      status: "completed",
      title: "anonymous.tool",
      summary: "Tool finished",
      detailKind: "tool_call",
      detailAvailable: true,
      secret: "must not survive",
    },
  }]);
  assert.equal(block.activity.type, "tool");
  assert.equal(block.activity.detailAvailable, true);
  assert.equal("secret" in block.activity, false);
});

test("app-server history refresh waits for a settled capture", async () => {
  let requests = 0;
  const controller = fixture({
    refreshMinMs: 1,
    api: async () => {
      requests += 1;
      return {
        revision: "rev-live",
        totalTurns: 1,
        start: 0,
        questions: [{ index: 0, key: "q0", preview: "Question" }],
        turns: [{ index: 0, key: "q0", text: "› Question\n\n• Answer" }],
      };
    },
  });
  controller.scheduleRefresh({ captureSource: "codex-app-server", streaming: true, text: "Partial" }, 1);
  await delay(10);
  assert.equal(requests, 0);
  controller.scheduleRefresh({ captureSource: "codex-app-server", streaming: false, text: "Final" }, 1);
  await delay(20);
  assert.equal(requests, 1);
});
