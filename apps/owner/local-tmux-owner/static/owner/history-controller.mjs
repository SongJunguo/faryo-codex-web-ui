export function emptyConversationHistory() {
  return {
    revision: "",
    sessionId: "",
    totalTurns: 0,
    questions: [],
    turns: new Map(),
    loadedStart: null,
    loadedEnd: 0,
    olderCursor: "",
    initialized: false,
  };
}

export function isStructuredCapture(capture) {
  return capture?.captureSource === "codex-jsonl"
    || capture?.captureSource === "codex-app-server"
    || capture?.captureSource === "codex-empty";
}

const MESSAGE_KINDS = new Set(["user", "output", "process", "plan"]);
const ACTIVITY_TYPES = new Set(["command", "file_change", "search", "mcp", "tool", "image", "compaction", "error", "unknown"]);
const ACTIVITY_STATUSES = new Set(["running", "waiting", "completed", "failed", "declined"]);

export function normalizeActivity(value) {
  if (!value || typeof value !== "object") return null;
  const type = String(value.type || "");
  const status = String(value.status || "");
  if (!ACTIVITY_TYPES.has(type) || !ACTIVITY_STATUSES.has(status)) return null;
  const result = {
    type,
    status,
    title: String(value.title || "").slice(0, 520),
    summary: String(value.summary || "").slice(0, 360),
    detailKind: String(value.detailKind || "none").slice(0, 48),
    detailAvailable: Boolean(value.detailAvailable),
  };
  for (const key of ["exitCode", "durationMs", "changeCount"]) {
    if (Number.isFinite(Number(value[key]))) result[key] = Number(value[key]);
  }
  return result;
}

export function normalizeMessageBlocks(value) {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item, index) => {
    const kind = String(item?.kind || "");
    const text = String(item?.text || "").trim();
    if (!MESSAGE_KINDS.has(kind) || !text) return [];
    const block = {
      id: String(item?.id || `anonymous-${index}`),
      turnKey: String(item?.turnKey || ""),
      questionKey: String(item?.questionKey || ""),
      kind,
      role: String(item?.role || ""),
      text,
      revision: Math.max(0, Number(item?.revision || 0)),
      final: item?.final !== false,
    };
    const activity = normalizeActivity(item?.activity);
    if (activity) block.activity = activity;
    return [block];
  });
}

export function mergeMessageBlocks(historyBlocks, liveBlocks) {
  const merged = normalizeMessageBlocks(historyBlocks);
  const positions = new Map(merged.map((block, index) => [block.id, index]));
  for (const block of normalizeMessageBlocks(liveBlocks)) {
    const position = positions.get(block.id);
    if (position === undefined) {
      positions.set(block.id, merged.length);
      merged.push(block);
    } else {
      merged[position] = block;
    }
  }
  return merged;
}

export function createHistoryController(options = {}) {
  const view = options.view || globalThis;
  const api = options.api;
  const scroller = options.scroller;
  const output = options.output;
  if (typeof api !== "function" || !scroller || !output) {
    throw new TypeError("History controller requires api, scroller, and output");
  }
  const pageTurns = Number(options.pageTurns || 12);
  const refreshMinMs = Number(options.refreshMinMs || 2500);
  const fetchTimeoutMs = Number(options.fetchTimeoutMs || 12000);
  let history = emptyConversationHistory();
  let loadPromise = null;
  let requestController = null;
  let refreshTimer = null;
  let runId = 0;
  let captureSignature = "";
  let lastRefreshAt = 0;
  let userIntentUntil = 0;
  let olderLoadQueued = false;

  function loadedTurns() {
    return [...history.turns.values()].sort((left, right) => left.index - right.index);
  }

  function displayText() {
    const turns = loadedTurns();
    const blocks = [];
    let previous = null;
    for (const turn of turns) {
      if (previous !== null && turn.index > previous + 1) {
        const missing = turn.index - previous - 1;
        blocks.push(`• … ${missing} earlier turn${missing === 1 ? "" : "s"} not loaded; use the question rail to fetch them …`);
      }
      blocks.push(String(turn.text || ""));
      previous = turn.index;
    }
    return blocks.filter(Boolean).join("\n\n");
  }

  function displayBlocks() {
    const blocks = [];
    let previous = null;
    for (const turn of loadedTurns()) {
      if (previous !== null && turn.index > previous + 1) {
        const missing = turn.index - previous - 1;
        blocks.push({
          id: `history-gap-${previous + 1}-${turn.index}`,
          kind: "process",
          role: "process",
          text: `… ${missing} earlier turn${missing === 1 ? "" : "s"} not loaded; use the question rail to fetch them …`,
          final: true,
        });
      }
      blocks.push(...normalizeMessageBlocks(turn.blocks));
      previous = turn.index;
    }
    return blocks;
  }

  function mergedCapture(capture) {
    if (
      options.getOutputMode() !== "compact"
      || !isStructuredCapture(capture)
      || !history.initialized
      || !history.turns.size
      || (history.sessionId && capture.sessionId && history.sessionId !== capture.sessionId)
    ) {
      return capture;
    }
    const historyText = displayText();
    if (capture.captureSource === "codex-app-server") {
      const liveBlocks = normalizeMessageBlocks(capture.messageBlocks);
      if (liveBlocks.length) {
        return {
          ...capture,
          text: capture.streaming ? String(capture.text || historyText) : historyText,
          messageBlocks: mergeMessageBlocks(displayBlocks(), liveBlocks),
          historyTotalTurns: history.totalTurns,
        };
      }
      if (capture.streaming) return capture;
    }
    return { ...capture, text: historyText, historyTotalTurns: history.totalTurns };
  }

  function reset() {
    runId += 1;
    requestController?.abort();
    requestController = null;
    loadPromise = null;
    if (refreshTimer) view.clearTimeout(refreshTimer);
    refreshTimer = null;
    captureSignature = "";
    lastRefreshAt = 0;
    olderLoadQueued = false;
    history = emptyConversationHistory();
  }

  function mergePage(data, expectedSessionId = "") {
    const revision = String(data?.revision || "");
    if (!revision) throw new Error("Conversation history revision is missing");
    if (history.revision && history.revision !== revision) history = emptyConversationHistory();
    history.revision = revision;
    history.sessionId = expectedSessionId || history.sessionId;
    history.totalTurns = Number(data.totalTurns || 0);
    history.questions = Array.isArray(data.questions)
      ? data.questions.map((item, index) => ({
        index: Number.isInteger(Number(item?.index)) ? Number(item.index) : index,
        key: String(item?.key || `question-${index}`),
        preview: String(item?.preview || "Untitled question"),
      }))
      : [];
    for (const turn of data.turns || []) {
      const index = Number(turn?.index);
      if (!Number.isInteger(index) || index < 0) continue;
      history.turns.set(index, {
        index,
        key: String(turn.key || `question-${index}`),
        preview: String(turn.preview || ""),
        text: String(turn.text || ""),
        blocks: normalizeMessageBlocks(turn.blocks),
      });
    }
    const loaded = loadedTurns();
    history.loadedStart = loaded.length ? loaded[0].index : null;
    history.loadedEnd = loaded.length ? loaded[loaded.length - 1].index + 1 : 0;
    if (Number(data.start) === history.loadedStart) history.olderCursor = String(data.olderCursor || "");
    history.initialized = true;
  }

  function loadedTarget(key) {
    return [...output.querySelectorAll(".compact-block.user")]
      .find((element) => element.dataset.faryoQuestionKey === key) || null;
  }

  async function load(loadOptions = {}) {
    const around = loadOptions.around !== undefined
      && loadOptions.around !== null
      && Number.isInteger(Number(loadOptions.around))
      ? Number(loadOptions.around)
      : null;
    if (around !== null && history.turns.has(around)) return history.turns.get(around);
    if (loadPromise) {
      try { await loadPromise; } catch (_error) {}
      if (around !== null && history.turns.has(around)) return history.turns.get(around);
    }
    const expectedRunId = runId;
    const session = options.getSelectedSession();
    const expectedSessionId = String(options.getExpectedSessionId(history.sessionId) || "");
    const query = new URLSearchParams({ limit: String(pageTurns) });
    if (loadOptions.cursor) query.set("cursor", String(loadOptions.cursor));
    if (around !== null) query.set("around", String(around));
    const controller = new view.AbortController();
    requestController = controller;
    const timeoutId = view.setTimeout(() => controller.abort(), fetchTimeoutMs);
    const anchor = loadOptions.preserveAnchor ? options.anchorSnapshot() : null;
    const keepBottom = Boolean(
      options.isInitialLatestPending()
      || (loadOptions.latest && options.isNearBottom())
    );
    loadPromise = (async () => {
      const data = await api(
        options.apiPath(`/api/conversation-history?${query}`),
        { signal: controller.signal },
      );
      if (expectedRunId !== runId || session !== options.getSelectedSession()) return null;
      mergePage(data, expectedSessionId);
      lastRefreshAt = Date.now();
      const lastCapture = options.getLastCapture();
      if (lastCapture && options.getOutputMode() === "compact") {
        options.renderCapture(lastCapture);
        if (anchor) options.restoreAnchor(anchor);
        else if (options.isInitialLatestPending() && loadOptions.latest) {
          options.applyInitialLatestScroll(true);
        } else if (keepBottom && !userIntentActive()) {
          options.scrollBottom(true);
        }
      }
      return data;
    })();
    try {
      return await loadPromise;
    } catch (error) {
      if (Number(error?.status) === 409) {
        reset();
        if (!loadOptions.retrying) return load({ latest: true, retrying: true });
      }
      if (loadOptions.latest && options.isInitialLatestPending()) {
        options.applyInitialLatestScroll(true);
      }
      throw error;
    } finally {
      view.clearTimeout(timeoutId);
      if (requestController === controller) requestController = null;
      loadPromise = null;
      if (olderLoadQueued) {
        olderLoadQueued = false;
        view.setTimeout(maybeLoadOlder, 0);
      }
    }
  }

  async function resolveQuestionTarget(question) {
    if (loadedTarget(question?.key)) return true;
    try {
      await load({ around: Number(question?.index) });
    } catch (error) {
      options.setError(options.userErrorMessage(error));
      throw error;
    }
    return Boolean(loadedTarget(question?.key));
  }

  function scheduleRefresh(capture, delay = 80) {
    const source = String(capture?.captureSource || "");
    if (
      options.getOutputMode() !== "compact"
      || !["codex-jsonl", "codex-app-server"].includes(source)
      || (source === "codex-app-server" && capture?.streaming)
    ) return;
    if (history.sessionId && capture.sessionId && history.sessionId !== capture.sessionId) {
      reset();
      options.beginInitialLatestScroll();
    }
    const text = String(capture.text || "");
    const signature = `${capture.sessionId || ""}:${text.length}:${text.slice(-160)}`;
    if (captureSignature === signature && (history.initialized || loadPromise || refreshTimer)) return;
    captureSignature = signature;
    if (refreshTimer) view.clearTimeout(refreshTimer);
    const wait = Math.max(delay, refreshMinMs - (Date.now() - lastRefreshAt));
    refreshTimer = view.setTimeout(() => {
      refreshTimer = null;
      load({ latest: true }).catch(options.handleBackgroundError);
    }, wait);
  }

  function noteUserIntent() {
    options.cancelInitialLatestScroll();
    userIntentUntil = Date.now() + 600;
  }

  function userIntentActive() {
    return Date.now() <= userIntentUntil;
  }

  function maybeLoadOlder() {
    if (
      !userIntentActive()
      || options.getOutputMode() !== "compact"
      || !history.initialized
      || !history.olderCursor
      || scroller.scrollTop > 120
    ) return;
    if (loadPromise) {
      olderLoadQueued = true;
      return;
    }
    userIntentUntil = 0;
    load({ cursor: history.olderCursor, preserveAnchor: true })
      .catch(options.handleBackgroundError);
  }

  return {
    reset,
    load,
    mergePage,
    mergedCapture,
    loadedTurns,
    displayText,
    displayBlocks,
    resolveQuestionTarget,
    scheduleRefresh,
    noteUserIntent,
    userIntentActive,
    maybeLoadOlder,
    get initialized() { return history.initialized; },
    get questions() { return history.questions; },
    get sessionId() { return history.sessionId; },
    get totalTurns() { return history.totalTurns; },
  };
}
