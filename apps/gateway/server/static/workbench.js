let installPrompt = null,
  lastAnchorRect = null,
  csrfToken = null;
const BROWSER_ENVELOPE_VERSION = 1;
function validateBrowserEnvelope(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return value;
  if (
    value.envelopeVersion !== undefined &&
    value.envelopeVersion !== BROWSER_ENVELOPE_VERSION
  ) {
    const error = new Error("Unsupported Faryo browser protocol version");
    error.status = 409;
    throw error;
  }
  return value;
}
function versionedRequestOptions(options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  if (["GET", "HEAD", "OPTIONS"].includes(method)) return options;
  if (typeof options.body !== "string" || !options.body) return options;
  try {
    const value = JSON.parse(options.body);
    if (!value || typeof value !== "object" || Array.isArray(value))
      return options;
    return {
      ...options,
      body: JSON.stringify({
        ...value,
        envelopeVersion: BROWSER_ENVELOPE_VERSION,
      }),
    };
  } catch (_error) {
    return options;
  }
}
async function readJsonResponse(response, label) {
  const text = await response.text();
  let data = null;
  try {
    data = JSON.parse(text);
  } catch (_error) {}
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    const normalized = text.trimStart().toLowerCase(),
      html =
        normalized.startsWith("<!doctype html") ||
        normalized.startsWith("<html"),
      authPage =
        html &&
        ["cloudflare access", "faryo sign in", "/cdn-cgi/access"].some(
          (marker) => normalized.includes(marker),
        ),
      temporary = [502, 503, 504].includes(response.status),
      message =
        authPage || [401, 403].includes(response.status)
          ? "Your web sign-in expired. Refresh this page and sign in again."
          : temporary
            ? "Faryo is restarting or temporarily unavailable. Please retry."
            : html
              ? `${label} returned a web page instead of API data. Refresh and retry.`
              : `${label} returned an invalid response.`;
    const error = new Error(message);
    error.status = response.status;
    error.retryable = temporary;
    throw error;
  }
  validateBrowserEnvelope(data);
  if (!response.ok || data.ok === false) {
    const error = new Error(
      data.error || `${label} failed (${response.status})`,
    );
    error.status = response.status;
    error.retryable = [502, 503, 504].includes(response.status);
    throw error;
  }
  return data;
}
async function fetchJson(url, options, label) {
  const response = await fetch(url, versionedRequestOptions(options));
  return readJsonResponse(response, label || "Request");
}
async function csrfHeaders() {
  if (!csrfToken) {
    const data = await fetchJson(
      "/api/csrf",
      { cache: "no-store" },
      "Sign-in check",
    );
    csrfToken = data.csrf || "";
  }
  return { "X-Faryo-Csrf": csrfToken };
}
window.FaryoAppearance?.apply();
if ("serviceWorker" in navigator)
  navigator.serviceWorker.register("/sw.js").catch(() => {});
window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();
  installPrompt = event;
  const btn = document.getElementById("installApp");
  if (btn) btn.hidden = false;
});
document.addEventListener(
  "pointerdown",
  (event) => {
    const el = event.target.closest(
      'button,a,.session-card,.package-card,[role="button"]',
    );
    if (!el) return;
    const rect = el.getBoundingClientRect();
    lastAnchorRect = {
      left: rect.left,
      right: rect.right,
      top: rect.top,
      bottom: rect.bottom,
    };
  },
  { capture: true, passive: true },
);
document.addEventListener("click", (event) => {
  const settings = document.getElementById("settings");
  if (event.target.closest("#settings>button"))
    settings.classList.toggle("open");
  else if (!event.target.closest("#settings"))
    settings.classList.remove("open");
  const appearanceBtn = event.target.closest?.(".appearance-btn");
  if (appearanceBtn?.id === "themeBtn") {
    window.FaryoAppearance?.cycle("theme");
    return;
  }
  if (appearanceBtn?.id === "fontBtn") {
    window.FaryoAppearance?.cycle("font");
    return;
  }
  if (appearanceBtn?.id === "sizeBtn") {
    window.FaryoAppearance?.cycle("size");
    return;
  }
  const installBtn = event.target.closest?.("#installApp");
  if (installBtn && installPrompt) {
    installPrompt.prompt();
    installPrompt = null;
    installBtn.hidden = true;
  }
});
const WORKBENCH_CACHE_KEY = "faryoWorkbenchSnapshot";
const DIRECTORY_HIDDEN_PREFERENCE_KEY = "faryoDirectoryShowHiddenV1";
const CONTEXT_WINDOW_MIN_K = 32;
const CONTEXT_WINDOW_MAX_K = 1050;
const SESSION_BACKEND = Object.freeze({
  APP_SERVER: Object.freeze({
    key: "APP_SERVER",
    wire: "web-managed",
    label: "Codex App Server",
  }),
  CODEX_TUI: Object.freeze({
    key: "CODEX_TUI",
    wire: "terminal-managed",
    label: "Codex TUI (tmux)",
  }),
});
function sessionBackendKey(value, source = "") {
  const raw = String(value || "");
  if (raw === SESSION_BACKEND.APP_SERVER.wire)
    return SESSION_BACKEND.APP_SERVER.key;
  if (raw === SESSION_BACKEND.CODEX_TUI.wire)
    return SESSION_BACKEND.CODEX_TUI.key;
  return source === "codex-app-server"
    ? SESSION_BACKEND.APP_SERVER.key
    : SESSION_BACKEND.CODEX_TUI.key;
}
function sessionBackendWire(key) {
  return key === SESSION_BACKEND.CODEX_TUI.key
    ? SESSION_BACKEND.CODEX_TUI.wire
    : SESSION_BACKEND.APP_SERVER.wire;
}
const labels = JSON.parse(
    document.getElementById("faryoRouteLabels")?.textContent || "{}",
  ),
  initialHistoryParams = new URLSearchParams(location.search),
  validPeriods = new Set(["all", "today", "7d", "30d"]),
  validArchives = new Set(["active", "archived", "all"]),
  initialPeriod = initialHistoryParams.get("period") || "all",
  initialArchive = initialHistoryParams.get("archive") || "active",
  historyFilters = {
    q: String(initialHistoryParams.get("q") || "")
      .trim()
      .slice(0, 96),
    period: validPeriods.has(initialPeriod) ? initialPeriod : "all",
    archive: validArchives.has(initialArchive) ? initialArchive : "active",
  };
let draggedPackage = null;
let assetTargetPackage = null;
let handoffTargets = [];
let latestAgentCwdChoices = {};
let actionBusy = false;
let historyPage = Math.max(
    1,
    Number.parseInt(initialHistoryParams.get("page") || "1", 10) || 1,
  ),
  historyTotalPages = 1,
  workbenchRequestGeneration = 0,
  workbenchAbortController = null,
  historySearchTimer = null;
const NOTIFICATION_PREFERENCE_KEY = "faryoAttentionNotificationsV1";
let attentionItems = new Map(),
  dismissedAttention = new Set(),
  lastLifecycleStates = new Map(),
  attentionInitialized = false;
const preactFactory = window.FaryoPreactWorkbench?.createWorkbenchRenderer;
if (typeof preactFactory !== "function")
  throw new Error("Faryo Preact workbench bundle is unavailable");
const workbenchRenderer = preactFactory({
  routeLabels: labels,
  containers: {
    packages: document.getElementById("packageList"),
    launchers: document.getElementById("newSessionSlot"),
    activeSessions: document.getElementById("activeSessionList"),
    sessions: document.getElementById("sessionList"),
  },
  actions: {
    packageDragStart(item, event) {
      draggedPackage = item.id;
      event.dataTransfer.setData("text/plain", item.id);
    },
    packageDragEnd() {
      draggedPackage = null;
      clearDropTargets();
    },
    addPackageFiles(item) {
      assetTargetPackage = item.id;
      document.getElementById("packageAssetInput")?.click();
    },
    sendPackage(item) {
      return withBusy(() => selectPackageTarget(item));
    },
    startLauncher(item) {
      return withBusy(async () => {
        const entries = launchableEntries(item.entries);
        if (!entries.length) {
          await notice(
            "No endpoint available",
            "No workstation can start another session.",
          );
          return;
        }
        const route = entries[0].id;
        const routeEntry = entries[0];
        const directory = await selectNewCwd(
          route,
          item.label,
          item.cwdChoices,
          {
            backend: item.backend,
            appServerReady: routeEntry?.appServerReady !== false,
            routes: entries,
          },
        );
        if (directory === null) return;
        await agentNew(directory.route || route, item.command, directory);
      });
    },
    sessionAction(item, action, event) {
      return withBusy(async () => {
        if (action === "close") {
          event.preventDefault();
          event.stopPropagation();
          await closeSession(item.route, item.tmuxSession || "", {
            appServer:
              sessionBackendKey(item.backend, item.source) ===
              SESSION_BACKEND.APP_SERVER.key,
            running: Boolean(
              item.agentRunning ||
              ["running", "pending_interaction"].includes(
                String(item.state || ""),
              ),
            ),
          });
          return;
        }
        if (action === "archive" || action === "restore") {
          event.preventDefault();
          event.stopPropagation();
          await changeSessionArchived(item, action === "archive");
          return;
        }
        if (action === "resume-options") {
          event.preventDefault();
          event.stopPropagation();
          if (!item.id) return;
          if (item.limitReached) {
            await notice(
              "Agent limit reached",
              "Close a running session first.",
            );
            return;
          }
          const recorded = String(item.cwd || "").trim(),
            recent = Array.isArray(latestAgentCwdChoices?.[item.route])
              ? latestAgentCwdChoices[item.route]
              : [],
            explicitChoices = {
              ...latestAgentCwdChoices,
              [item.route]: [
                ...(recorded
                  ? [
                      {
                        value: recorded,
                        path: recorded,
                        label: item.cwdLabel || directoryName(recorded),
                        kind: "recorded",
                      },
                    ]
                  : []),
                ...recent.filter(
                  (choice) =>
                    trimDirectoryPath(choice?.value || choice?.path) !==
                    trimDirectoryPath(recorded),
                ),
              ],
            },
            directory = await selectNewCwd(
              item.route,
              "resumed Codex",
              explicitChoices,
              {
                body: "Choose the backend, working directory and context window for this saved Codex conversation.",
                backend: item.backend,
                source: item.source,
                appServerReady: item.appServerReady !== false,
              },
            );
          if (directory === null) return;
          markAttentionRead(item);
          await resumeSession(
            item.route,
            item.id,
            item.source || "",
            directory,
            directory.backend,
          );
          return;
        }
        markAttentionRead(item);
        const lifecycle = String(item.state || "");
        if (lifecycle === "exited") {
          event.preventDefault();
          await notice(
            "Codex exited",
            "Close this managed shell; the Codex thread remains available in Session History.",
          );
          return;
        }
        if (item.tmuxSession) {
          location.href = `/${item.route}/?session=${encodeURIComponent(item.tmuxSession)}`;
          return;
        }
        if (!item.id) return;
        event.preventDefault();
        if (item.archived || lifecycle === "archived") {
          await notice(
            "Archived session",
            "Restore this thread before resuming it.",
          );
          return;
        }
        if (item.limitReached) {
          await notice("Agent limit reached", "Close a running session first.");
          return;
        }
        await resumeSession(
          item.route,
          item.id,
          item.source || "",
          null,
          item.backend,
        );
      });
    },
    draggedPackage: () => draggedPackage,
    async dropPackage(item, event) {
      const packageId =
        event.dataTransfer.getData("text/plain") || draggedPackage;
      if (packageId) {
        await injectPackage(
          packageId,
          item.route,
          item.tmuxSession || "",
          item.id || "",
          item.source || "",
        );
      }
    },
  },
});
window.__faryoRenderSessionFixture = (item, container) =>
  workbenchRenderer.renderSessionFixture(item, container);
function historyFilterActive() {
  return (
    !!historyFilters.q ||
    historyFilters.period !== "all" ||
    historyFilters.archive !== "active"
  );
}
function historyRequestQuery() {
  const params = new URLSearchParams({ page: String(historyPage) });
  if (historyFilters.q) params.set("q", historyFilters.q);
  if (historyFilters.period !== "all")
    params.set("period", historyFilters.period);
  if (historyFilters.archive !== "active")
    params.set("archive", historyFilters.archive);
  return params.toString();
}
function syncHistoryLocation() {
  const url = new URL(location.href);
  for (const key of ["page", "q", "period", "archive"])
    url.searchParams.delete(key);
  if (historyPage > 1) url.searchParams.set("page", String(historyPage));
  if (historyFilters.q) url.searchParams.set("q", historyFilters.q);
  if (historyFilters.period !== "all")
    url.searchParams.set("period", historyFilters.period);
  if (historyFilters.archive !== "active")
    url.searchParams.set("archive", historyFilters.archive);
  history.replaceState(null, "", url);
}
function storeWorkbench(data) {
  if (historyFilterActive()) return;
  try {
    sessionStorage.setItem(
      WORKBENCH_CACHE_KEY,
      JSON.stringify({ storedAt: Date.now(), data }),
    );
  } catch (_error) {}
}
function restoreWorkbench() {
  if (historyFilterActive() || historyPage !== 1) return;
  try {
    const cached = JSON.parse(
      sessionStorage.getItem(WORKBENCH_CACHE_KEY) || "null",
    );
    if (cached?.data) renderWorkbench(cached.data);
  } catch (_error) {}
}
function markRoutes(entries) {
  for (const item of entries || []) {
    const chip = document.getElementById(`route-${item.id}`);
    if (!chip) continue;
    chip.className = `route-chip ${item.state || "error"}`;
    const state = chip.querySelector(".route-state");
    if (state) {
      state.textContent = item.stateText || "—";
      state.title = item.detail || item.stateText || "";
    }
  }
}
function clearDropTargets() {
  document
    .querySelectorAll(".session-card.drop-target")
    .forEach((el) => el.classList.remove("drop-target"));
}
function placeSheet(modal) {
  if (!lastAnchorRect) {
    modal.classList.remove("anchored");
    return;
  }
  const margin = 16,
    gap = 8,
    sheet = modal.querySelector(".sheet"),
    width = Math.min(320, innerWidth - margin * 2),
    center = (lastAnchorRect.left + lastAnchorRect.right) / 2;
  modal.classList.add("open", "anchored");
  const height = sheet.offsetHeight,
    left =
      innerWidth < 620
        ? (innerWidth - width) / 2
        : Math.max(
            margin,
            Math.min(innerWidth - width - margin, center - width / 2),
          ),
    below = lastAnchorRect.bottom + gap,
    above = lastAnchorRect.top - height - gap,
    top =
      below + height + margin <= innerHeight ? below : Math.max(margin, above);
  modal.style.setProperty("--sheet-left", `${left}px`);
  modal.style.setProperty("--sheet-top", `${top}px`);
}
function resetSheetMode() {
  const modal = document.getElementById("modal"),
    toolbar = document.getElementById("directoryToolbar"),
    workstationPicker = document.getElementById("workstationPicker"),
    workstationControls = document.getElementById("workstationControls"),
    workstationHelp = document.getElementById("workstationHelp"),
    breadcrumb = document.getElementById("directoryBreadcrumb"),
    search = document.getElementById("directorySearch"),
    hiddenToggle = document.getElementById("directoryHiddenToggle"),
    launchSettings = document.getElementById("launchSettings"),
    backendHelp = document.getElementById("sessionBackendHelp"),
    backendSummary = document.getElementById("sessionBackendSummary"),
    contextInput = document.getElementById("contextWindowCustom"),
    contextSummary = document.getElementById("contextWindowSummary"),
    contextError = document.getElementById("contextWindowError");
  modal.classList.remove("directory-mode");
  toolbar.hidden = true;
  workstationPicker.hidden = true;
  workstationControls.replaceChildren();
  workstationHelp.textContent = "";
  breadcrumb.replaceChildren();
  search.value = "";
  search.oninput = null;
  hiddenToggle.onclick = null;
  hiddenToggle.disabled = false;
  hiddenToggle.setAttribute("aria-pressed", "false");
  hiddenToggle.classList.remove("active");
  launchSettings.open = true;
  for (const button of document.querySelectorAll("[data-session-backend]")) {
    button.onclick = null;
    button.disabled = false;
    button.setAttribute(
      "aria-pressed",
      button.dataset.sessionBackend === SESSION_BACKEND.APP_SERVER.key
        ? "true"
        : "false",
    );
  }
  backendHelp.textContent =
    "App Server is recommended for the best web experience.";
  backendSummary.textContent = "App Server";
  for (const button of document.querySelectorAll("[data-context-window-k]")) {
    button.onclick = null;
    button.setAttribute(
      "aria-pressed",
      button.dataset.contextWindowK === "0" ? "true" : "false",
    );
  }
  contextInput.value = "";
  contextInput.oninput = null;
  contextInput.removeAttribute("aria-invalid");
  contextInput.closest(".context-window-custom")?.classList.remove("active");
  contextSummary.textContent = "Default context";
  contextError.hidden = true;
  contextError.textContent = "";
}
function sheet(title, body, choices) {
  return new Promise((resolve) => {
    const modal = document.getElementById("modal"),
      list = document.getElementById("modalChoices"),
      actions = document.getElementById("modalActions");
    resetSheetMode();
    document.getElementById("modalTitle").textContent = title;
    document.getElementById("modalBody").textContent = body || "";
    const done = (value) => {
      modal.classList.remove("open", "anchored");
      modal.onclick = null;
      resolve(value);
    };
    list.replaceChildren(
      ...(choices || []).map((item) => {
        const element = document.createElement(item.static ? "div" : "button");
        element.className = item.static
          ? "activity-row"
          : `choice-btn${item.danger ? " danger" : ""}`;
        element.innerHTML = `<strong>${escapeHtml(item.label)}</strong>${item.meta ? `<span>${escapeHtml(item.meta)}</span>` : ""}`;
        if (!item.static) {
          element.type = "button";
          element.disabled = !!item.disabled;
          element.addEventListener("click", () => done(item.value));
        }
        return element;
      }),
    );
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "mini-btn";
    cancel.textContent = "Cancel";
    cancel.addEventListener("click", () => done(null));
    actions.replaceChildren(cancel);
    modal.onclick = (event) => {
      if (event.target === modal) done(null);
    };
    placeSheet(modal);
    modal.classList.add("open");
  });
}
async function notice(title, body) {
  await sheet(title, body, [{ label: "OK", value: "ok" }]);
}
async function selectPackageTarget(item) {
  const targets = handoffTargets.filter(
    (target) => target.id || target.tmuxSession,
  );
  if (!targets.length) {
    await notice(
      "No session available",
      "Start or resume a session before sending files.",
    );
    return;
  }
  const choices = targets.map((target, index) => {
    const active = !!target.tmuxSession,
      agent = target.source === "codex-cli" ? "Codex" : "Runtime",
      route = target.routeLabel || labels[target.route] || target.route,
      state = active
        ? "Active"
        : target.limitReached
          ? "Limit reached"
          : "Resume and send";
    return {
      label: target.title || target.id || "Untitled session",
      meta: `${route} · ${agent} · ${state}`,
      value: String(index),
      disabled: !active && !!target.limitReached,
    };
  });
  const selected = await sheet(
    "Send files to a session",
    item.title || "Choose the destination session.",
    choices,
  );
  if (selected === null) return;
  const target = targets[Number(selected)];
  if (target)
    await injectPackage(
      item.id,
      target.route,
      target.tmuxSession || "",
      target.id || "",
      target.source || "",
    );
}
async function withBusy(task) {
  if (actionBusy) return;
  actionBusy = true;
  try {
    return await task();
  } catch (error) {
    await notice("Action failed", error.message || String(error));
  } finally {
    actionBusy = false;
  }
}
function activityTime(value) {
  const timestamp = Date.parse(String(value || ""));
  if (!Number.isFinite(timestamp)) return "Unknown time";
  const seconds = Math.max(0, Math.round((Date.now() - timestamp) / 1000));
  if (seconds < 60) return "Just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}
function attentionKey(item) {
  return `${item.route || ""}:${item.tmuxSession || item.id || ""}`;
}
function attentionTarget(item) {
  const session = item.tmuxSession || "";
  return item.route && session
    ? `/${item.route}/?session=${encodeURIComponent(session)}`
    : "";
}
function attentionLabel(state) {
  return state === "exited" ? "Managed session exited" : "Session needs input";
}
function notificationsEnabled() {
  try {
    return localStorage.getItem(NOTIFICATION_PREFERENCE_KEY) === "1";
  } catch (_error) {
    return false;
  }
}
function updateNotificationUi() {
  const state = document.getElementById("notificationState");
  if (!state) return;
  if (!("Notification" in window)) {
    state.textContent = "Unavailable in this browser";
    return;
  }
  state.textContent =
    notificationsEnabled() && Notification.permission === "granted"
      ? "On · page-open only"
      : Notification.permission === "denied"
        ? "Blocked by browser"
        : "Off · page-open only";
}
function updateAttentionUi() {
  const count = attentionItems.size,
    countElement = document.getElementById("attentionCount"),
    summary = document.getElementById("attentionSummary"),
    button = document.getElementById("attentionCenter");
  if (countElement) {
    countElement.textContent = String(count);
    countElement.dataset.active = String(count > 0);
  }
  if (summary)
    summary.textContent = count
      ? `${count} session${count === 1 ? "" : "s"} need attention`
      : "Nothing needs attention";
  button?.classList.toggle("attention-row", count > 0);
}
function showAttentionNotification(item) {
  if (
    !notificationsEnabled() ||
    !("Notification" in window) ||
    Notification.permission !== "granted"
  )
    return;
  try {
    const notification = new Notification("Faryo needs attention", {
      body: "A session completed or needs input.",
      tag: "faryo-attention",
    });
    notification.onclick = () => {
      window.focus();
      const target = attentionTarget(item);
      if (target) location.href = target;
      notification.close();
    };
  } catch (_error) {}
}
function processAttention(items) {
  const nextStates = new Map();
  for (const item of items || []) {
    const key = attentionKey(item),
      state = String(item.state || ""),
      previous = lastLifecycleStates.get(key),
      token = `${key}:${state}`;
    nextStates.set(key, state);
    if (["waiting", "exited"].includes(state)) {
      if (!dismissedAttention.has(token) && !attentionItems.has(key))
        attentionItems.set(key, {
          key,
          route: item.route,
          routeLabel: item.routeLabel || labels[item.route] || item.route,
          state,
          tmuxSession: item.tmuxSession || "",
          time: new Date().toISOString(),
        });
      if (
        attentionInitialized &&
        previous &&
        previous !== state &&
        ["running", "starting"].includes(previous)
      )
        showAttentionNotification(item);
    } else if (["running", "starting"].includes(state)) {
      attentionItems.delete(key);
      for (const value of dismissedAttention)
        if (value.startsWith(`${key}:`)) dismissedAttention.delete(value);
    }
  }
  for (const key of attentionItems.keys())
    if (!nextStates.has(key)) attentionItems.delete(key);
  lastLifecycleStates = nextStates;
  attentionInitialized = true;
  updateAttentionUi();
}
function markAttentionRead(item) {
  const key = attentionKey(item),
    stored = attentionItems.get(key);
  if (stored) dismissedAttention.add(`${key}:${stored.state}`);
  attentionItems.delete(key);
  updateAttentionUi();
}
async function showAttentionCenter() {
  document.getElementById("settings")?.classList.remove("open");
  const items = [...attentionItems.values()],
    choices = items.map((item) => ({
      label: attentionLabel(item.state),
      meta: `${item.routeLabel || "Workstation"} · ${activityTime(item.time)}`,
      value: item.key,
    }));
  if (items.length)
    choices.push({
      label: "Dismiss all",
      meta: "Hides the current states until they change or this page reloads.",
      value: "clear",
    });
  const selected = await sheet(
    "Attention",
    "Status only. Message text, titles, paths and raw identifiers are not shown.",
    choices.length
      ? choices
      : [
          {
            static: true,
            label: "Nothing needs attention",
            meta: "Running-to-waiting and exited transitions appear here.",
          },
        ],
  );
  if (selected === "clear") {
    for (const item of attentionItems.values())
      dismissedAttention.add(`${item.key}:${item.state}`);
    attentionItems.clear();
    updateAttentionUi();
    return;
  }
  const item = attentionItems.get(selected);
  if (!item) return;
  markAttentionRead(item);
  const target = attentionTarget(item);
  if (target) location.href = target;
}
async function toggleNotifications() {
  document.getElementById("settings")?.classList.remove("open");
  if (!("Notification" in window)) {
    await notice(
      "Notifications unavailable",
      "This browser does not expose the Notification API.",
    );
    return;
  }
  let permission = Notification.permission;
  if (permission === "default")
    permission = await Notification.requestPermission();
  const enabled = permission === "granted" && !notificationsEnabled();
  try {
    if (enabled) localStorage.setItem(NOTIFICATION_PREFERENCE_KEY, "1");
    else localStorage.removeItem(NOTIFICATION_PREFERENCE_KEY);
  } catch (_error) {}
  updateNotificationUi();
  if (permission === "denied")
    await notice(
      "Notifications blocked",
      "Enable notifications in browser settings if you want page-open attention alerts.",
    );
}
async function showSecurityActivity() {
  document.getElementById("settings")?.classList.remove("open");
  const data = await fetchJson(
      "/api/security-activity?limit=30",
      { cache: "no-store" },
      "Security activity",
    ),
    entries = Array.isArray(data.entries) ? data.entries : [],
    actionLabels = {
      start: "Start",
      resume: "Resume",
      archive: "Archive",
      unarchive: "Restore",
      close: "Close",
      send: "Send",
      interrupt: "Interrupt",
      enter: "Enter",
      up: "Up",
      down: "Down",
      "file-inject": "File transfer",
      "revoke-sessions": "Revoke sessions",
    },
    rows = entries.map((item) => ({
      static: true,
      label: `${actionLabels[item.action] || item.action || "Control"} · ${item.result || "unknown"}`,
      meta: [
        activityTime(item.time),
        item.route ? labels[item.route] || item.route : "Gateway",
        item.target || "no target",
        item.idempotent ? "idempotent retry" : "",
      ]
        .filter(Boolean)
        .join(" · "),
    }));
  await sheet(
    "Security activity",
    "Recent control metadata only. Message text, titles and paths are never recorded.",
    rows.length
      ? rows
      : [
          {
            static: true,
            label: "No control activity yet",
            meta: "Actions will appear here after you use Faryo controls.",
          },
        ],
  );
}
async function revokeSignedInDevices() {
  document.getElementById("settings")?.classList.remove("open");
  const confirmed = await sheet(
    "Revoke signed-in devices",
    "This invalidates every inner Faryo login for your account. It does not stop Codex or close tmux.",
    [
      {
        label: "Revoke all Faryo sessions",
        meta: "You will sign in again on this device.",
        value: "revoke",
        danger: true,
      },
    ],
  );
  if (confirmed !== "revoke") return;
  await fetchJson(
    "/api/auth/revoke-all",
    {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(await csrfHeaders()) },
      body: JSON.stringify({ confirm: "revoke" }),
    },
    "Revoke sessions",
  );
  location.href = "/logout";
}
function launchableEntries(entries) {
  return (entries || []).filter(
    (entry) =>
      !["offline", "error"].includes(String(entry?.state || "")) &&
      entry?.canCreate !== false &&
      String(entry?.id || ""),
  );
}
async function directoryPage(route, path, showHidden = false) {
  const params = new URLSearchParams();
  if (path) params.set("path", path);
  if (showHidden) params.set("showHidden", "1");
  const query = params.size ? `?${params}` : "";
  return fetchJson(
    `/${route}/api/directories${query}`,
    { cache: "no-store" },
    "Directory browser",
  );
}
function trimDirectoryPath(value) {
  let path = String(value || "").trim();
  while (path.length > 1 && path.endsWith("/")) path = path.slice(0, -1);
  return path;
}
function directoryName(value) {
  const parts = trimDirectoryPath(value).split("/").filter(Boolean);
  return parts[parts.length - 1] || "Home";
}
function directoryCanonical(value, data) {
  const path = trimDirectoryPath(value);
  if (path === "~" || path.startsWith("~/")) {
    const match = (data.roots || [])
      .map((item) => ({
        ...item,
        display: trimDirectoryPath(item.displayPath),
      }))
      .filter(
        (item) =>
          item.path &&
          item.display.startsWith("~") &&
          (path === item.display || path.startsWith(item.display + "/")),
      )
      .sort((a, b) => b.display.length - a.display.length)[0];
    if (match)
      return trimDirectoryPath(match.path) + path.slice(match.display.length);
  }
  return path;
}
function directoryBreadcrumbItems(data) {
  const current = directoryCanonical(data.path, data),
    roots = (data.roots || []).map((item) => ({
      ...item,
      canonical: directoryCanonical(item.path, data),
    })),
    root = roots
      .filter(
        (item) =>
          current === item.canonical ||
          current.startsWith(item.canonical + "/"),
      )
      .sort((a, b) => b.canonical.length - a.canonical.length)[0];
  if (!root)
    return [
      {
        label: data.displayPath || directoryName(current),
        path: data.path,
        current: true,
      },
    ];
  const rootLabel =
      String(root.displayPath || "") === "~"
        ? "~"
        : directoryName(root.canonical),
    items = [
      {
        label: rootLabel,
        path: root.path,
        current: current === root.canonical,
      },
    ],
    tail = current.slice(root.canonical.length).split("/").filter(Boolean);
  let cursor = root.canonical;
  for (const part of tail) {
    cursor = (cursor === "/" ? "" : cursor) + "/" + part;
    items.push({ label: part, path: cursor, current: cursor === current });
  }
  if (items.length > 3)
    return [
      items[0],
      { label: "…", path: items[items.length - 3].path, collapsed: true },
      ...items.slice(-2),
    ];
  return items;
}
function directoryPickerModel(data, recent, query, expanded) {
  const search = String(query || "")
      .trim()
      .toLowerCase(),
    current = directoryCanonical(data.path, data),
    parent = directoryCanonical(data.parent, data),
    roots = (data.roots || []).map((item) => ({
      ...item,
      canonical: directoryCanonical(item.path, data),
    })),
    reserved = new Set(
      [current, parent, ...roots.map((item) => item.canonical)].filter(Boolean),
    ),
    recentSeen = new Set(reserved),
    allRecent = [];
  for (const item of recent || []) {
    const value = String(item.value || item.path || ""),
      canonical = directoryCanonical(value, data);
    if (!canonical || recentSeen.has(canonical)) continue;
    recentSeen.add(canonical);
    allRecent.push({
      kind: "recent",
      icon: "↺",
      label: item.label || directoryName(canonical),
      meta: item.path || value,
      path: value,
    });
  }
  const parentItem = data.parent
      ? {
          kind: "parent",
          icon: "↰",
          label: "..",
          meta: "Parent folder",
          path: data.parent,
        }
      : null,
    locations = roots
      .filter(
        (item) =>
          item.canonical &&
          item.canonical !== current &&
          !current.startsWith(item.canonical + "/"),
      )
      .map((item) => ({
        kind: "location",
        icon: "⌂",
        label: item.displayPath || directoryName(item.canonical),
        meta: "Configured location",
        path: item.path,
      })),
    folders = (data.directories || []).map((item) => ({
      kind: "folder",
      icon: "📁",
      label: item.name || directoryName(item.path),
      meta: "",
      path: item.path,
    }));
  const matches = (item) =>
      !search || `${item.label} ${item.meta}`.toLowerCase().includes(search),
    recentMatches = allRecent.filter(matches),
    recentVisible =
      search || expanded ? recentMatches : recentMatches.slice(0, 4),
    locationVisible = locations.filter(matches),
    folderVisible = [
      ...(parentItem ? [parentItem] : []),
      ...folders.filter(matches),
    ];
  return {
    recent: recentVisible,
    locations: locationVisible,
    folders: folderVisible,
    hasMore: !search && allRecent.length > 4,
    total: recentVisible.length + locationVisible.length + folderVisible.length,
  };
}
function directoryRow(item, done) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `directory-row directory-row-${item.kind}`;
  button.innerHTML = `<span class="directory-row-icon" aria-hidden="true">${item.icon}</span><span class="directory-row-copy"><strong>${escapeHtml(item.label)}</strong>${item.meta ? `<small>${escapeHtml(item.meta)}</small>` : ""}</span><span class="directory-row-arrow" aria-hidden="true">›</span>`;
  button.addEventListener("click", () => done({ path: item.path }));
  return button;
}
function directorySection(title, items, done, more) {
  if (!items.length && !more) return null;
  const section = document.createElement("section");
  section.className = "directory-section";
  section.dataset.directorySection = title.toLowerCase();
  const heading = document.createElement("div");
  heading.className = "directory-section-heading";
  const label = document.createElement("span");
  label.textContent = title;
  heading.appendChild(label);
  if (more) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "directory-more";
    button.textContent = "Show all";
    button.addEventListener("click", more);
    heading.appendChild(button);
  }
  section.append(heading, ...items.map((item) => directoryRow(item, done)));
  return section;
}
function bindWorkstationPicker(entries, state, onSelect) {
  const picker = document.getElementById("workstationPicker"),
    controls = document.getElementById("workstationControls"),
    help = document.getElementById("workstationHelp"),
    choices = launchableEntries(entries);
  picker.hidden = choices.length < 2;
  controls.replaceChildren(
    ...choices.map((entry) => {
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.workstationRoute = entry.id;
      button.textContent = entry.label || labels[entry.id] || entry.id;
      button.onclick = async () => {
        if (button.disabled || state.id === entry.id) return;
        for (const item of controls.querySelectorAll("button"))
          item.disabled = true;
        help.textContent = `Loading ${button.textContent}…`;
        try {
          const changed = await onSelect(entry.id);
          if (changed !== false) state.id = entry.id;
        } catch (_error) {
          help.textContent = `Could not load ${button.textContent}. Try again.`;
          return;
        } finally {
          for (const item of controls.querySelectorAll("button"))
            item.disabled = false;
        }
        sync();
      };
      return button;
    }),
  );
  const sync = () => {
    const selected = choices.find((entry) => entry.id === state.id);
    for (const button of controls.querySelectorAll("button"))
      button.setAttribute(
        "aria-pressed",
        String(button.dataset.workstationRoute === state.id),
      );
    help.textContent = selected
      ? `${selected.activeCount || 0}/${selected.maxRunning || 0} active sessions`
      : "Choose a workstation.";
  };
  sync();
}
function bindSessionBackendPicker(state, { appServerReady = true } = {}) {
  const buttons = [...document.querySelectorAll("[data-session-backend]")],
    help = document.getElementById("sessionBackendHelp"),
    summary = document.getElementById("sessionBackendSummary"),
    appServerButton = buttons.find(
      (button) =>
        button.dataset.sessionBackend === SESSION_BACKEND.APP_SERVER.key,
    );
  appServerButton.disabled = !appServerReady;
  if (!appServerReady && state.key === SESSION_BACKEND.APP_SERVER.key)
    state.key = SESSION_BACKEND.CODEX_TUI.key;
  const sync = () => {
    for (const button of buttons)
      button.setAttribute(
        "aria-pressed",
        String(button.dataset.sessionBackend === state.key),
      );
    help.textContent = !appServerReady
      ? "Codex App Server is reconnecting; use Codex TUI (tmux) for this launch."
      : state.key === SESSION_BACKEND.APP_SERVER.key
        ? "App Server is recommended for structured streaming and reliable web control."
        : "TUI compatibility keeps Codex inside a tmux terminal session.";
    summary.textContent =
      state.key === SESSION_BACKEND.APP_SERVER.key
        ? "App Server"
        : "TUI (tmux)";
  };
  for (const button of buttons)
    button.onclick = () => {
      if (button.disabled) return;
      state.key = button.dataset.sessionBackend;
      sync();
    };
  sync();
  return () => sessionBackendWire(state.key);
}
function contextWindowValue(state) {
  if (state.mode === "default") return 0;
  const raw =
    state.mode === "custom"
      ? String(state.custom || "")
      : String(state.k || "");
  if (!/^[0-9]{1,4}$/.test(raw)) return null;
  const value = Number.parseInt(raw, 10);
  return value >= CONTEXT_WINDOW_MIN_K && value <= CONTEXT_WINDOW_MAX_K
    ? value
    : null;
}
function bindContextWindowPicker(state) {
  const buttons = [...document.querySelectorAll("[data-context-window-k]")],
    input = document.getElementById("contextWindowCustom"),
    custom = input.closest(".context-window-custom"),
    help = document.getElementById("contextWindowHelp"),
    summary = document.getElementById("contextWindowSummary"),
    error = document.getElementById("contextWindowError");
  const clearError = () => {
      error.hidden = true;
      error.textContent = "";
      input.removeAttribute("aria-invalid");
    },
    sync = () => {
      const selected = contextWindowValue(state);
      for (const button of buttons) {
        const value = Number.parseInt(button.dataset.contextWindowK || "0", 10);
        button.setAttribute(
          "aria-pressed",
          String(
            state.mode === "default"
              ? value === 0
              : state.mode === "preset" && value === state.k,
          ),
        );
      }
      custom.classList.toggle("active", state.mode === "custom");
      if (state.mode === "default") {
        help.textContent = "Inherit this workstation's Codex settings.";
        summary.textContent = "Default context";
      } else if (selected !== null) {
        help.textContent = `${selected}K requested · auto-compact at ${Math.floor((selected * 90) / 100)}K. Codex may report a slightly smaller usable window.`;
        summary.textContent = `${selected}K context`;
      } else {
        help.textContent = `Enter a whole number from ${CONTEXT_WINDOW_MIN_K} to ${CONTEXT_WINDOW_MAX_K} K.`;
        summary.textContent = "Custom context";
      }
    };
  input.value = state.mode === "custom" ? String(state.custom || "") : "";
  for (const button of buttons)
    button.onclick = () => {
      const value = Number.parseInt(button.dataset.contextWindowK || "0", 10);
      state.mode = value ? "preset" : "default";
      state.k = value;
      state.custom = "";
      input.value = "";
      clearError();
      sync();
    };
  input.oninput = () => {
    state.mode = "custom";
    state.custom = input.value.trim();
    clearError();
    sync();
  };
  sync();
  return () => {
    const value = contextWindowValue(state);
    if (value !== null) return value;
    error.textContent = `Enter a whole number from ${CONTEXT_WINDOW_MIN_K} to ${CONTEXT_WINDOW_MAX_K} K.`;
    error.hidden = false;
    input.setAttribute("aria-invalid", "true");
    document.getElementById("launchSettings").open = true;
    input.focus({ preventScroll: true });
    return null;
  };
}
function directorySheet(data, recent, label, options = {}) {
  return new Promise((resolve) => {
    const modal = document.getElementById("modal"),
      list = document.getElementById("modalChoices"),
      actions = document.getElementById("modalActions"),
      toolbar = document.getElementById("directoryToolbar"),
      breadcrumb = document.getElementById("directoryBreadcrumb"),
      search = document.getElementById("directorySearch"),
      hiddenToggle = document.getElementById("directoryHiddenToggle"),
      launchSettings = document.getElementById("launchSettings");
    resetSheetMode();
    modal.classList.remove("anchored");
    modal.classList.add("directory-mode");
    document.getElementById("modalTitle").textContent =
      "Choose working directory";
    document.getElementById("modalBody").textContent =
      options.body || `Choose where this ${label} session should work.`;
    toolbar.hidden = false;
    launchSettings.open = !matchMedia("(max-width: 620px)").matches;
    let readBackend = bindSessionBackendPicker(
      options.backendState || { key: SESSION_BACKEND.APP_SERVER.key },
      { appServerReady: options.appServerReady !== false },
    );
    const readContextWindowK = bindContextWindowPicker(
      options.contextWindowState || { mode: "default", k: 0, custom: "" },
    );
    let expanded = false,
      currentData = data,
      currentRecent = recent,
      showHidden = Boolean(data.showHidden ?? options.showHidden);
    const done = (value) => {
        modal.classList.remove("open", "anchored", "directory-mode");
        modal.onclick = null;
        resetSheetMode();
        resolve(value);
      },
      render = () => {
        const model = directoryPickerModel(
            currentData,
            currentRecent,
            search.value,
            expanded,
          ),
          nodes = [],
          recentSection = directorySection(
            "Recent",
            model.recent,
            done,
            model.hasMore
              ? () => {
                  expanded = true;
                  render();
                }
              : null,
          ),
          folderSection = directorySection(
            "Folders",
            model.folders,
            done,
            null,
          ),
          locationSection = directorySection(
            "Locations",
            model.locations,
            done,
            null,
          );
        for (const section of [recentSection, folderSection, locationSection])
          if (section) nodes.push(section);
        if (!model.total) {
          const empty = document.createElement("div");
          empty.className = "directory-empty";
          empty.textContent = search.value
            ? "No matching folders"
            : "This folder has no subfolders";
          nodes.push(empty);
        }
        list.replaceChildren(...nodes);
      };
    const renderBreadcrumb = () =>
      breadcrumb.replaceChildren(
        ...directoryBreadcrumbItems(currentData).map((item) => {
          const button = document.createElement("button");
          button.type = "button";
          button.className = `directory-crumb${item.collapsed ? " directory-crumb-collapsed" : ""}`;
          button.textContent = item.label;
          if (item.current) {
            button.disabled = true;
            button.setAttribute("aria-current", "location");
          } else
            button.addEventListener("click", () => done({ path: item.path }));
          return button;
        }),
      );
    const syncHiddenToggle = () => {
      hiddenToggle.setAttribute("aria-pressed", showHidden ? "true" : "false");
      hiddenToggle.classList.toggle("active", showHidden);
      hiddenToggle.title = showHidden
        ? "Hide dot-prefixed folders"
        : "Show dot-prefixed folders";
    };
    hiddenToggle.onclick = async () => {
      if (hiddenToggle.disabled || typeof options.onToggleHidden !== "function")
        return;
      hiddenToggle.disabled = true;
      try {
        const next = await options.onToggleHidden(
          !showHidden,
          currentData.path,
        );
        if (next && typeof next === "object") {
          currentData = next;
          showHidden = Boolean(next.showHidden);
          expanded = false;
          renderBreadcrumb();
          render();
          requestAnimationFrame(() => {
            breadcrumb.scrollLeft = breadcrumb.scrollWidth;
          });
        }
      } finally {
        hiddenToggle.disabled = false;
        syncHiddenToggle();
      }
    };
    bindWorkstationPicker(
      options.routes || [],
      options.routeState || { id: options.route || "" },
      async (nextRoute) => {
        if (typeof options.onRouteChange !== "function") return false;
        const next = await options.onRouteChange(nextRoute, showHidden);
        if (!next?.data) return false;
        currentData = next.data;
        currentRecent = Array.isArray(next.recent) ? next.recent : [];
        showHidden = Boolean(next.data.showHidden ?? showHidden);
        expanded = false;
        search.value = "";
        readBackend = bindSessionBackendPicker(
          options.backendState || { key: SESSION_BACKEND.APP_SERVER.key },
          { appServerReady: next.appServerReady !== false },
        );
        renderBreadcrumb();
        syncHiddenToggle();
        render();
        requestAnimationFrame(() => {
          breadcrumb.scrollLeft = breadcrumb.scrollWidth;
        });
        return true;
      },
    );
    renderBreadcrumb();
    syncHiddenToggle();
    search.oninput = render;
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "mini-btn directory-cancel";
    cancel.textContent = "Cancel";
    cancel.addEventListener("click", () => done(null));
    const select = document.createElement("button");
    select.type = "button";
    select.className = "directory-primary";
    select.textContent = `Start ${label} here`;
    select.addEventListener("click", () => {
      const contextWindowK = readContextWindowK();
      if (contextWindowK === null) return;
      done({
        cwd: String(currentData.path || ""),
        cwdToken: String(currentData.selectionToken || ""),
        contextWindowK,
        backend: readBackend(),
        route: String(options.routeState?.id || options.route || ""),
      });
    });
    actions.replaceChildren(cancel, select);
    modal.onclick = (event) => {
      if (event.target === modal) done(null);
    };
    render();
    modal.classList.add("open");
    requestAnimationFrame(() => {
      breadcrumb.scrollLeft = breadcrumb.scrollWidth;
    });
  });
}
async function firstAvailableDirectoryPage(route, recent, showHidden = false) {
  const candidates = [],
    seen = new Set();
  for (const item of recent) {
    const value = String(item?.value || "").trim();
    if (!value || seen.has(value)) continue;
    seen.add(value);
    candidates.push(value);
  }
  candidates.push("");
  let lastError = null;
  for (const candidate of candidates) {
    try {
      return {
        data: await directoryPage(route, candidate, showHidden),
        path: candidate,
      };
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError || new Error("No available working directory");
}
async function selectNewCwd(route, label, cwdChoices, rawOptions = {}) {
  const options =
    typeof rawOptions === "string" ? { body: rawOptions } : rawOptions || {};
  let currentRoute = route,
    recent = Array.isArray(cwdChoices?.[route]) ? cwdChoices[route] : [],
    currentEntry = (options.routes || []).find((entry) => entry.id === route);
  const contextWindowState = { mode: "default", k: 0, custom: "" };
  const backendState = {
    key: sessionBackendKey(options.backend, options.source),
  };
  const routeState = { id: route };
  let showHidden = false;
  try {
    showHidden = localStorage.getItem(DIRECTORY_HIDDEN_PREFERENCE_KEY) === "1";
  } catch (_error) {}
  const initial = await firstAvailableDirectoryPage(
    currentRoute,
    recent,
    showHidden,
  );
  let path = initial.path,
    data = initial.data;
  while (true) {
    const selected = await directorySheet(data, recent, label, {
      showHidden,
      body: options.body || "",
      contextWindowState,
      backendState,
      appServerReady:
        currentEntry?.appServerReady ?? options.appServerReady ?? true,
      routes: options.routes || [],
      route: currentRoute,
      routeState,
      onRouteChange: async (nextRoute, nextShowHidden) => {
        const nextRecent = Array.isArray(cwdChoices?.[nextRoute])
          ? cwdChoices[nextRoute]
          : [];
        const next = await firstAvailableDirectoryPage(
          nextRoute,
          nextRecent,
          nextShowHidden,
        );
        currentRoute = nextRoute;
        routeState.id = nextRoute;
        recent = nextRecent;
        path = next.path;
        data = next.data;
        currentEntry = (options.routes || []).find(
          (entry) => entry.id === nextRoute,
        );
        return {
          data,
          recent,
          appServerReady: currentEntry?.appServerReady !== false,
        };
      },
      onToggleHidden: async (nextShowHidden, currentPath) => {
        const next = await directoryPage(
          currentRoute,
          currentPath || path,
          nextShowHidden,
        );
        showHidden = nextShowHidden;
        data = next;
        try {
          if (showHidden)
            localStorage.setItem(DIRECTORY_HIDDEN_PREFERENCE_KEY, "1");
          else localStorage.removeItem(DIRECTORY_HIDDEN_PREFERENCE_KEY);
        } catch (_error) {}
        return next;
      },
    });
    if (selected === null) return null;
    if (selected.cwd)
      return { ...selected, route: selected.route || currentRoute };
    path = String(selected.path || "");
    data = await directoryPage(currentRoute, path, showHidden);
  }
}
function newLaunchRequestId() {
  return globalThis.crypto?.randomUUID
    ? `web-${crypto.randomUUID()}`
    : `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`;
}
async function agentNew(route, command, directory) {
  const payload = {
    route,
    command,
    backend: directory?.backend || SESSION_BACKEND.APP_SERVER.wire,
    client_launch_id: newLaunchRequestId(),
  };
  if (directory?.cwd) {
    payload.cwd = directory.cwd;
    payload.cwd_token = directory.cwdToken || "";
  }
  if (directory?.contextWindowK)
    payload.context_window_k = directory.contextWindowK;
  const request = async () =>
    fetchJson(
      "/api/agent/new",
      {
        method: "POST",
        cache: "no-store",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        body: JSON.stringify(payload),
      },
      "Start Codex",
    );
  let data;
  try {
    data = await request();
  } catch (error) {
    if (!error.retryable) throw error;
    await new Promise((resolve) => setTimeout(resolve, 350));
    data = await request();
  }
  if (
    data.clientLaunchId !== payload.client_launch_id ||
    !/^faryo[1-9][0-9]*$/.test(String(data.session || ""))
  )
    throw new Error("Start Codex returned a stale launch response");
  const redirect = `/${route}/?session=${encodeURIComponent(data.session)}`;
  if (data.redirect !== redirect)
    throw new Error("Start Codex returned an inconsistent session target");
  location.href = redirect;
}
async function resumeSession(
  route,
  agentSessionId,
  source,
  selectedDirectory = null,
  selectedBackend = "",
) {
  const payload = {
    route,
    agent_session_id: agentSessionId,
    source,
    backend:
      selectedBackend ||
      selectedDirectory?.backend ||
      sessionBackendWire(sessionBackendKey("", source)),
  };
  if (selectedDirectory?.cwd) {
    payload.cwd = selectedDirectory.cwd;
    payload.cwd_token = selectedDirectory.cwdToken || "";
  }
  if (selectedDirectory?.contextWindowK)
    payload.context_window_k = selectedDirectory.contextWindowK;
  const request = async () =>
    fetchJson(
      "/api/agent/resume",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        body: JSON.stringify(payload),
      },
      "Resume session",
    );
  let data = await request();
  if (data.requiresWorkingDirectory) {
    if (selectedDirectory?.cwd)
      throw new Error("The selected working directory was not accepted.");
    const recorded = String(data.recordedDisplayCwd || "the recorded folder"),
      directory = await selectNewCwd(
        route,
        "resumed Codex",
        latestAgentCwdChoices,
        {
          body: `${recorded} is unavailable. Choose a backend and working directory before Codex resumes.`,
          backend: payload.backend,
          source,
        },
      );
    if (directory === null) return;
    payload.cwd = directory.cwd;
    payload.cwd_token = directory.cwdToken || "";
    payload.backend = directory.backend || payload.backend;
    if (directory.contextWindowK)
      payload.context_window_k = directory.contextWindowK;
    data = await request();
    if (data.requiresWorkingDirectory)
      throw new Error("The selected working directory was not accepted.");
  }
  location.href =
    data.redirect || `/${route}/?session=${encodeURIComponent(data.session)}`;
}
async function closeSession(route, session, options = {}) {
  const interrupt = Boolean(options.appServer && options.running),
    body = interrupt
      ? "Codex is still working. This interrupts the current turn and closes the Faryo session; conversation history is retained."
      : options.appServer
        ? "This closes the Faryo web session and retains conversation history. Codex may keep the thread writer for up to 30 minutes, so choose App Server if you resume it immediately."
        : "This closes the running tmux session. Conversation history is retained.";
  const ok = await sheet(
    interrupt ? "Interrupt and close" : "Close Session",
    body,
    [
      {
        label: interrupt ? "Interrupt and close" : "Close Session",
        meta: session,
        value: "ok",
        danger: true,
      },
    ],
  );
  if (ok !== "ok") return;
  await fetchJson(
    `/${route}/api/session/close`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(await csrfHeaders()) },
      body: JSON.stringify({ session, interrupt }),
    },
    "Close session",
  );
  await refreshWorkbench();
}
async function changeSessionArchived(item, archived) {
  if (!item?.route || !item?.id) return;
  if (archived) {
    const confirmed = await sheet(
      "Archive session",
      "Move this Codex thread out of Current history. You can restore it from the Archived filter.",
      [
        {
          label: "Archive session",
          meta: "Reversible · conversation content is retained",
          value: "archive",
        },
      ],
    );
    if (confirmed !== "archive") return;
  }
  await fetchJson(
    `/api/session-history/${archived ? "archive" : "unarchive"}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(await csrfHeaders()) },
      body: JSON.stringify({ route: item.route, agent_session_id: item.id }),
    },
    archived ? "Archive session" : "Restore session",
  );
  await refreshWorkbench();
}
async function injectPackage(
  packageId,
  route,
  session,
  agentSessionId,
  source,
) {
  const payload = { package_id: packageId, route };
  if (session) payload.session = session;
  if (agentSessionId) {
    payload.agent_session_id = agentSessionId;
    payload.source = source;
  }
  const data = await fetchJson(
    "/api/bridge-inject",
    {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(await csrfHeaders()) },
      body: JSON.stringify(payload),
    },
    "Send files",
  );
  location.href =
    data.redirect ||
    `/${route}/${session ? `?session=${encodeURIComponent(session)}` : ""}`;
}
function renderWorkbench(data) {
  markRoutes(data.entries || []);
  const packages = data.inbox || data.packages || [],
    rawSessions = data.sessions || [],
    activeSessions = Array.isArray(data.activeSessions)
      ? data.activeSessions
      : rawSessions.filter((item) => item.tmuxSession),
    sessions = Array.isArray(data.activeSessions)
      ? rawSessions
      : rawSessions.filter((item) => !item.tmuxSession),
    history = data.history || {},
    applied = history.filter || historyFilters,
    entries = data.entries || [],
    cwdChoices = data.agentCwdChoices || {},
    pkg = packages[0],
    packageItems = pkg ? [pkg] : [],
    allowedCommands = new Set(data.newSessionCommands || ["codex"]),
    readyEntries = entries.filter(
      (entry) =>
        entry.state !== "offline" &&
        entry.state !== "error" &&
        entry.canCreate !== false,
    ),
    appServerState = readyEntries.length
      ? readyEntries.some((entry) => entry.appServerReady === true)
        ? "ready"
        : "unknown"
      : entries.find((entry) => entry.appServerState)?.appServerState ||
        "unavailable",
    launchers = [
      {
        id: "new-codex",
        command: "codex",
        label: "Codex",
        backend: SESSION_BACKEND.APP_SERVER.wire,
        description: readyEntries.length
          ? appServerState === "ready"
            ? "Choose App Server or TUI"
            : "TUI available · App Server reconnecting"
          : "No endpoint can start another session",
        disabled: !readyEntries.length,
        runtimeState: appServerState,
        entries: readyEntries,
        cwdChoices,
      },
    ].filter((item) => allowedCommands.has(item.command));
  latestAgentCwdChoices = cwdChoices;
  processAttention(activeSessions);
  historyFilters.q = String(applied.q || "").slice(0, 96);
  historyFilters.period = validPeriods.has(applied.period)
    ? applied.period
    : "all";
  historyFilters.archive = validArchives.has(applied.archive)
    ? applied.archive
    : "active";
  const seenTargets = new Set();
  handoffTargets = [...activeSessions, ...sessions].filter((item) => {
    const key = `${item.route}:${item.id || item.tmuxSession || ""}`;
    if (item.archived || !item.route || seenTargets.has(key)) return false;
    seenTargets.add(key);
    return true;
  });
  historyPage = Math.max(1, Number(history.page || historyPage || 1));
  historyTotalPages = Math.max(1, Number(history.totalPages || 1));
  const historyPageInput = document.getElementById("historyPageInput"),
    historySearchInput = document.getElementById("historySearchInput"),
    historySearchClear = document.getElementById("historySearchClear");
  if (historyPageInput) {
    historyPageInput.max = String(historyTotalPages);
    if (document.activeElement !== historyPageInput)
      historyPageInput.value = String(historyPage);
  }
  if (historySearchInput && document.activeElement !== historySearchInput)
    historySearchInput.value = historyFilters.q;
  if (historySearchClear) historySearchClear.hidden = !historyFilters.q;
  document.querySelectorAll("[data-history-period]").forEach((button) => {
    const active = button.dataset.historyPeriod === historyFilters.period;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  document.querySelectorAll("[data-history-archive]").forEach((button) => {
    const active = button.dataset.historyArchive === historyFilters.archive;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  document.getElementById("packageCount").textContent = pkg
    ? pkg.status === "pending"
      ? "· Ready"
      : "· Sent"
    : "· Empty";
  document.getElementById("activeSessionCount").textContent =
    `${activeSessions.length} live`;
  document.getElementById("historyCount").textContent = historyFilterActive()
    ? `${Number(history.total ?? sessions.length)} matches`
    : `${Number(history.total ?? sessions.length)} total`;
  document.getElementById("historyPageTotal").textContent =
    String(historyTotalPages);
  document.getElementById("historyPrev").disabled =
    history.hasPrevious === false || historyPage <= 1;
  document.getElementById("historyNext").disabled =
    history.hasNext === false || historyPage >= historyTotalPages;
  workbenchRenderer.render({
    packages: packageItems,
    launchers,
    activeSessions,
    sessions,
    routeLabels: labels,
    historyEmptyText: historyFilterActive()
      ? "No sessions match these filters"
      : "No session history",
  });
  syncHistoryLocation();
}
async function refreshWorkbench() {
  const requestedPage = historyPage,
    generation = ++workbenchRequestGeneration;
  workbenchAbortController?.abort();
  const controller = new AbortController();
  workbenchAbortController = controller;
  let data;
  try {
    data = await fetchJson(
      `/api/workbench?${historyRequestQuery()}`,
      { cache: "no-store", signal: controller.signal },
      "Workbench",
    );
  } catch (error) {
    if (error?.name === "AbortError") return null;
    throw error;
  } finally {
    if (workbenchAbortController === controller)
      workbenchAbortController = null;
  }
  if (
    generation !== workbenchRequestGeneration ||
    requestedPage !== historyPage
  )
    return data;
  storeWorkbench(data);
  renderWorkbench(data);
  return data;
}
async function goToHistoryPage(value) {
  const previous = historyPage,
    raw = String(value ?? "").trim(),
    requested = Number(raw),
    next =
      raw && Number.isInteger(requested)
        ? Math.min(historyTotalPages, Math.max(1, requested))
        : previous,
    input = document.getElementById("historyPageInput");
  if (next === previous) {
    if (input) input.value = String(previous);
    return;
  }
  historyPage = next;
  if (input) input.value = String(next);
  try {
    await refreshWorkbench();
    document
      .getElementById("sessionList")
      ?.scrollTo({ top: 0, behavior: "smooth" });
  } catch (error) {
    historyPage = previous;
    if (input) input.value = String(previous);
    throw error;
  }
}
async function changeHistoryPage(delta) {
  return goToHistoryPage(historyPage + delta);
}
function applyHistoryFilter(kind, value) {
  if (kind === "q")
    historyFilters.q = String(value || "")
      .trim()
      .slice(0, 96);
  else if (kind === "period" && validPeriods.has(value))
    historyFilters.period = value;
  else if (kind === "archive" && validArchives.has(value))
    historyFilters.archive = value;
  historyPage = 1;
  syncHistoryLocation();
  return refreshWorkbench();
}
function scheduleHistorySearch(value) {
  clearTimeout(historySearchTimer);
  historySearchTimer = setTimeout(() => {
    applyHistoryFilter("q", value).catch(() => {});
  }, 250);
}
function escapeHtml(value) {
  return String(value).replace(
    /[&<>"']/g,
    (ch) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        ch
      ],
  );
}
function fileToAttachment(file) {
  return new Promise((resolve, reject) => {
    if (file.size > 20 * 1024 * 1024) {
      reject(new Error("Attachment must be 20 MB or smaller"));
      return;
    }
    const reader = new FileReader();
    reader.onload = () =>
      resolve({
        file_name: file.name || "attachment",
        mime_type: file.type || "application/octet-stream",
        data_url: String(reader.result || ""),
      });
    reader.onerror = () =>
      reject(reader.error || new Error("Failed to read attachment"));
    reader.readAsDataURL(file);
  });
}
async function filesToAttachments(fileList) {
  const files = Array.from(fileList || []).slice(0, 4),
    attachments = [];
  for (const file of files) attachments.push(await fileToAttachment(file));
  return attachments;
}
async function createPackage(files) {
  const attachments = await filesToAttachments(files);
  if (!attachments.length) return;
  const title =
    attachments.length === 1
      ? attachments[0].file_name
      : `${attachments.length} files`;
  await fetchJson(
    "/api/bridge-packages",
    {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(await csrfHeaders()) },
      body: JSON.stringify({
        title,
        source: "Manual upload",
        intent: "Send these files to a selected session.",
        attachments,
      }),
    },
    "Add files",
  );
  await refreshWorkbench();
}
async function appendAttachmentsToPackage(packageId, files) {
  const attachments = await filesToAttachments(files);
  if (!attachments.length) return;
  await fetchJson(
    "/api/bridge-package-assets",
    {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(await csrfHeaders()) },
      body: JSON.stringify({ package_id: packageId, attachments }),
    },
    "Add files",
  );
  await refreshWorkbench();
}
document
  .getElementById("newPackage")
  ?.addEventListener("click", () =>
    document.getElementById("packageInput")?.click(),
  );
document
  .getElementById("securityActivity")
  ?.addEventListener("click", () => withBusy(showSecurityActivity));
document
  .getElementById("attentionCenter")
  ?.addEventListener("click", () => withBusy(showAttentionCenter));
document
  .getElementById("notificationControl")
  ?.addEventListener("click", () => withBusy(toggleNotifications));
document
  .getElementById("revokeSessions")
  ?.addEventListener("click", () => withBusy(revokeSignedInDevices));
const historySearchInput = document.getElementById("historySearchInput");
if (historySearchInput) {
  historySearchInput.value = historyFilters.q;
  historySearchInput.addEventListener("input", (event) => {
    document.getElementById("historySearchClear").hidden = !event.target.value;
    scheduleHistorySearch(event.target.value);
  });
}
document
  .getElementById("historySearchForm")
  ?.addEventListener("submit", (event) => {
    event.preventDefault();
    clearTimeout(historySearchTimer);
    applyHistoryFilter("q", historySearchInput?.value || "").catch(() => {});
  });
document.getElementById("historySearchClear")?.addEventListener("click", () => {
  clearTimeout(historySearchTimer);
  if (historySearchInput) historySearchInput.value = "";
  applyHistoryFilter("q", "").catch(() => {});
  historySearchInput?.focus();
});
document
  .querySelectorAll("[data-history-period]")
  .forEach((button) =>
    button.addEventListener("click", () =>
      applyHistoryFilter("period", button.dataset.historyPeriod).catch(
        () => {},
      ),
    ),
  );
document
  .querySelectorAll("[data-history-archive]")
  .forEach((button) =>
    button.addEventListener("click", () =>
      applyHistoryFilter("archive", button.dataset.historyArchive).catch(
        () => {},
      ),
    ),
  );
document
  .getElementById("historyPrev")
  ?.addEventListener("click", () => withBusy(() => changeHistoryPage(-1)));
document
  .getElementById("historyNext")
  ?.addEventListener("click", () => withBusy(() => changeHistoryPage(1)));
document.getElementById("historyJump")?.addEventListener("submit", (event) => {
  event.preventDefault();
  const input = document.getElementById("historyPageInput");
  withBusy(() => goToHistoryPage(input?.value));
});
document
  .getElementById("packageInput")
  ?.addEventListener("change", async (event) => {
    const files = Array.from(event.target.files || []),
      button = document.getElementById("newPackage"),
      label = button?.textContent || "Choose files";
    event.target.value = "";
    if (!files.length) return;
    if (button) {
      button.disabled = true;
      button.textContent = "Adding…";
    }
    try {
      await withBusy(() => createPackage(files));
    } finally {
      if (button) {
        button.disabled = false;
        button.textContent = label;
      }
    }
  });
document
  .getElementById("packageAssetInput")
  ?.addEventListener("change", async (event) => {
    const files = Array.from(event.target.files || []),
      packageId = assetTargetPackage;
    assetTargetPackage = null;
    event.target.value = "";
    if (packageId && files.length)
      await withBusy(() => appendAttachmentsToPackage(packageId, files));
  });
const handoffBox = document.getElementById("handoffBox");
handoffBox?.addEventListener("dragover", (event) => {
  if (event.dataTransfer?.types?.includes("Files")) {
    event.preventDefault();
    handoffBox.classList.add("drop-ready");
  }
});
handoffBox?.addEventListener("dragleave", () =>
  handoffBox.classList.remove("drop-ready"),
);
handoffBox?.addEventListener("drop", (event) => {
  if (!event.dataTransfer?.files?.length) return;
  event.preventDefault();
  handoffBox.classList.remove("drop-ready");
  const files = Array.from(event.dataTransfer.files);
  withBusy(() => createPackage(files));
});
function initialRefresh() {
  refreshWorkbench().catch(() => {
    workbenchRenderer.renderError("Workbench failed to load");
  });
}
function scheduleInitialRefresh() {
  const run = () =>
    requestAnimationFrame(() => setTimeout(initialRefresh, 180));
  if (document.readyState === "complete") run();
  else window.addEventListener("load", run, { once: true });
}
restoreWorkbench();
updateAttentionUi();
updateNotificationUi();
scheduleInitialRefresh();
setInterval(() => {
  if (!document.hidden) refreshWorkbench().catch(() => {});
}, 15000);
