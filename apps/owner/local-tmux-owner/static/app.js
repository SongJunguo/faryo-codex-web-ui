(async () => {
  'use strict';
  const appScript = document.currentScript;
  const assetRevision = new URL(appScript?.src || location.href).searchParams.get('v') || 'dev';
  const ownerModule = (name) => import(`./owner/${name}?v=${encodeURIComponent(assetRevision)}`);
  const apiClientModulePromise = ownerModule('api-client.mjs');
  const activityGroupsModulePromise = ownerModule('activity-groups.mjs');
  const attachmentControllerModulePromise = ownerModule('attachment-controller.mjs');
  const historyControllerModulePromise = ownerModule('history-controller.mjs');
  const richBlockControllerModulePromise = ownerModule('rich-block-controller.mjs');
  const captureControllerModulePromise = ownerModule('capture-controller.mjs');
  const composerDeliveryModulePromise = ownerModule('composer-delivery.mjs');
  const goalStatusModulePromise = ownerModule('goal-status.mjs');
  const statusControllerModulePromise = ownerModule('status-controller.mjs');
  // Workspace review is an optional surface. Start loading it immediately,
  // but never let a transient asset failure block capture/history rendering.
  const changesPanelModulePromise = ownerModule('changes-panel.mjs');
  const [
    { createApiClient, sessionApiPath, validateBrowserEnvelope },
    { activityItemCollapsible, activityItemSummary, activityStatus, groupActivityBlocks, mergeCommandEvents },
    { createAttachmentController },
    { createHistoryController, isStructuredCapture },
    { createRichBlockController, shouldRenderEagerly },
    { createCaptureController },
    { createComposerDelivery },
    { goalViewModel },
    { createStatusController },
  ] = await Promise.all([
    apiClientModulePromise,
    activityGroupsModulePromise,
    attachmentControllerModulePromise,
    historyControllerModulePromise,
    richBlockControllerModulePromise,
    captureControllerModulePromise,
    composerDeliveryModulePromise,
    goalStatusModulePromise,
    statusControllerModulePromise,
  ]);

  const $ = (id) => document.getElementById(id);
  const ownerUiApi = window.FaryoOwnerUI || {};
  const params = new URLSearchParams(location.search);
  const initialSelectedSession = params.get('session') || '';
  const composerShellController = typeof ownerUiApi.mountComposerShell === 'function'
    ? ownerUiApi.mountComposerShell($('composerShellRoot'), {
      onSuggestionSelect: (index) => {
        const item = commandMatches()[index];
        if (item) applyCommandSuggestion(item);
      },
    })
    : null;
  if (!composerShellController) throw new Error('Faryo Preact composer is unavailable');
  const statusShellController = typeof ownerUiApi.mountStatusShell === 'function'
    ? ownerUiApi.mountStatusShell($('statusShellRoot'), {
      onGoalClick: handleGoalPillClick,
      onFastToggle: async () => {
        try {
          await toggleFastMode();
        } catch (err) {
          setError(userErrorMessage(err));
        }
      },
    })
    : null;
  if (!statusShellController) throw new Error('Faryo Preact status shell is unavailable');
  const conversationStore = typeof ownerUiApi.createConversationStore === 'function'
    ? ownerUiApi.createConversationStore({ session: initialSelectedSession, mode: 'compact' })
    : null;
  const transcriptShellController = conversationStore && typeof ownerUiApi.mountTranscriptShell === 'function'
    ? ownerUiApi.mountTranscriptShell($('transcriptShellRoot'), conversationStore)
    : null;
  if (!transcriptShellController) throw new Error('Faryo Preact transcript shell is unavailable');
  document.documentElement.dataset.faryoTranscriptUi = 'preact';
  const outputWrap = $('outputWrap');
  const output = transcriptShellController.output;
  const promptInput = $('promptInput');
  const attachmentInput = $('attachmentInput');
  const attachmentPreview = $('attachmentPreview');
  const errorBox = $('errorBox');
  const goalPill = $('goalPill');
  const bottomBtn = $('bottomBtn');
  const questionNavigatorElement = $('questionNavigator');
  const questionNavMarkers = $('questionNavMarkers');
  const questionNavPreview = $('questionNavPreview');
  const dockMenu = $('dockMenu');
  const sessionMenu = $('sessionMenu');
  const detailsPanel = $('detailsPanel');
  const changesPanel = $('changesPanel');
  const panelBackdrop = $('panelBackdrop');
  const promptShell = document.querySelector('.prompt-shell');
  const metaLineRe = /^\s*(gpt|o\d)[\w.\- ]*·\s+/;
  const codexCompactRules = window.FaryoCodexCompactRules || {};
  const markdownRenderer = window.FaryoMarkdownAst || {};
  const internalAnnotations = window.FaryoInternalAnnotations || {};
  const eventStreamParser = window.FaryoEventStream || {};
  const stableBlocks = window.FaryoStableBlocks || {};
  const questionNavigatorApi = window.FaryoQuestionNavigator || {};
  const codexCommandApi = window.FaryoCodexCommands || {};
  const copyFidelityApi = window.FaryoCopyFidelity || {};
  const clipboardImageApi = window.FaryoClipboardImages || {};
  const immersiveModeApi = window.FaryoImmersiveMode || {};
  const keyboardLayoutApi = window.FaryoKeyboardLayout || {};
  const composerLayoutApi = window.FaryoComposerLayout || {};
  document.documentElement.dataset.faryoClipboardPaste = (
    typeof clipboardImageApi.filesFromClipboard === 'function'
    && typeof clipboardImageApi.insertText === 'function'
  ) ? 'ready' : 'unavailable';
  const copyFidelity = typeof copyFidelityApi.create === 'function'
    ? copyFidelityApi.create({ root: output, parseMarkdown: (source) => markdownRenderer.parse(source) })
    : null;
  document.documentElement.dataset.faryoCopy = copyFidelity ? 'ready' : 'unavailable';
  const runtimeCompactRules = {
    userPromptRe: /^\s*›\s+/,
    compactBlocks: (text) => [{ kind: 'output', text: text || 'No output yet' }],
    processSummaryCard: (text) => text || '',
    approvalPendingRe: /(?:^|\n)\s*(?:Reviewing(?:\s+\d+)?\s+approval requests?(?:\s+\(|\s*$)|Automatic approval review\b|Approval requested\b|Allow Codex to run\b|Would you like to (?:run the following command|make the following edits|grant these permissions)\?)/i,
  };
  const COMPACT_CAPTURE_LINES = 320, FULL_CAPTURE_LINES = 800;
  const SESSION_BACKEND = Object.freeze({
    APP_SERVER: 'web-managed',
    CODEX_TUI: 'terminal-managed',
  });
  function sessionBackendLabel(value) {
    if (value === SESSION_BACKEND.APP_SERVER) return 'Codex App Server';
    if (value === SESSION_BACKEND.CODEX_TUI) return 'Codex TUI (tmux)';
    return 'Unknown backend';
  }
  const FETCH_TIMEOUT_MS = 12000, MAX_ATTACHMENTS = 35;
  const TIP_REFRESH_MS = 120000, STATUS_REFRESH_MS = 20000, FULL_REFRESH_MS = 10000, CAPTURE_FALLBACK_MS = 2500;
  const CAPTURE_SAFETY_MS = 12000, EVENT_STREAM_IDLE_MS = 28000;
  const WORKBENCH_CACHE_KEY = 'faryoWorkbenchSnapshot', WORKBENCH_CACHE_MS = 120000;
  const PET_SEND_MS = 1500;
  const PET_STOP_MS = 850;
  const PET_RUN_DECAY_MS = 1200;
  const IMAGE_MAX_EDGE = 1280, IMAGE_JPEG_QUALITY = 0.60;
  const PROMPT_TIPS = [
    'Tap pet to interrupt',
    'Tap + for tools',
    'Type / for commands',
    'Type cd for recent dirs',
    'Ctrl/⌘ Enter sends',
    'Tap Enter for the TUI choice',
    'Raw shows terminal',
    'Tap Raw again to lock',
    'Tap ↓ for latest',
    '⧉ copies last output',
    'Tap title to fold header',
    'Tap the Faryo logo for home',
    'Tap expand for full screen',
    'Tap version to fold footer',
    'Tap folder to switch sessions',
    'Set font on home',
  ];
  let captureController = null, statusController = null, pendingDeferredCapture = null;
  let statusRefreshTimer = null;
  let liveState = 'fallback';
  let petSending = false, petSendTimer = null, petStopping = false, petStopTimer = null, agentRunning = false, queuedSendNowAvailable = false, interruptInFlight = false, lastPetPhase = '';
  let currentFastStatus = 'off';
  let outputActivity = 0, outputActivityTimer = null, lastCaptureSignature = '', lastCompactCapture = null, lastFullCapture = null;
  let outputMode = 'compact', fullLocked = false, preserveErrorUntil = 0, seenInitialPageShow = false, errorTimer = null, currentPromptTip = '';
  let lastLiveWakeAt = 0;
  let lastCodexUpdateNotice = '';
  let markdownRenderRevision = 0, highlighterRenderFrame = 0;
  const markdownHtmlCache = new Map();
  const activityDetailCache = new Map();
  const pendingAttachments = [];
  const routeMatch = location.pathname.match(/^\/(hp|pc|txy)(?:\/|$)/);
  const routeBase = routeMatch ? `/${routeMatch[1]}` : '';
  const isStandalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone;
  const conversationScroller = outputWrap;
  document.documentElement.dataset.faryoScrollSurface = 'conversation';
  let keyboardLayoutController = null, composerLayoutController = null;
  const OWNER_TOKEN_STORAGE_KEY = 'faryoOwnerToken:v1';
  const queryOwnerToken = params.get('token') || '';
  let ownerToken = queryOwnerToken;
  try {
    if (queryOwnerToken) sessionStorage.setItem(OWNER_TOKEN_STORAGE_KEY, queryOwnerToken);
    else ownerToken = sessionStorage.getItem(OWNER_TOKEN_STORAGE_KEY) || '';
  } catch (_err) {}
  if (queryOwnerToken) {
    params.delete('token');
    const cleanQuery = params.toString();
    history.replaceState(null, '', `${location.pathname}${cleanQuery ? `?${cleanQuery}` : ''}${location.hash}`);
  }
  const ownerApiClient = createApiClient({
    routeBase,
    ownerToken,
    fetch: (...args) => window.fetch(...args),
    FormData: window.FormData,
  });
  const api = ownerApiClient.request;
  let selectedSession = initialSelectedSession;
  const composerDelivery = createComposerDelivery({
    storage: sessionStorage,
    routeKey: routeBase || 'owner',
    getSession: () => selectedSession,
    crypto: window.crypto,
    AbortController: window.AbortController,
    setTimeout: window.setTimeout.bind(window),
    clearTimeout: window.clearTimeout.bind(window),
    timeoutMs: FETCH_TIMEOUT_MS,
    sendAction: (payload, options) => postAction('/api/send', payload, options),
    onChecking: () => setError('Checking whether the message was delivered…', { timeoutMs: 0 }),
  });
  const interactionHost = typeof ownerUiApi.mountInteractionHost === 'function'
    ? ownerUiApi.mountInteractionHost($('interactionRoot'), {
      onRespond: async (request) => {
        const requestedSession = selectedSession;
        const response = await postAction('/api/interaction/respond', {
          ...request,
          clientRequestId: newInteractionRequestId(),
        });
        if (selectedSession !== requestedSession) return { interaction: null, ignored: true };
        syncStructuredInteraction(response.interaction || null);
        refreshStatus({ silent: true }).catch(handleBackgroundError);
        refreshCapture(currentCaptureLines(), { silent: true }).catch(handleBackgroundError);
        return response;
      },
      onError: (error) => setError(userErrorMessage(error)),
    })
    : null;
  document.documentElement.dataset.faryoInteractionUi = interactionHost ? 'preact' : 'fallback';
  const HISTORY_PAGE_TURNS = 12;
  const HISTORY_REFRESH_MIN_MS = 2500;
  let historyController = null, richBlockController = null;
  let initialLatestScrollPending = true, initialLatestScrollTimer = null;
  let viewportTailPinned = true;
  let submitInFlight = false;
  let activeSurfacePanel = null, panelReturnFocus = null;
  let goalDetailsRequestGeneration = 0;
  let launchErrorVisible = false;
  let immersiveController = null;
  const restoringLivePanels = new WeakSet();
  let questionNavigatorController = null;
  if (typeof questionNavigatorApi.createController === 'function') {
    try {
      questionNavigatorController = questionNavigatorApi.createController({
        view: window,
        navigator: questionNavigatorElement,
        markers: questionNavMarkers,
        current: $('questionNavCurrent'),
        total: $('questionNavTotal'),
        preview: questionNavPreview,
        scroller: conversationScroller,
        output,
        resolveTarget: resolveQuestionTarget,
        prepareTarget: prepareQuestionTarget,
      });
    } catch (_error) {
      questionNavigatorController = null;
    }
  }

  function setWorkbenchInert(inert) {
    for (const element of [document.querySelector('header'), outputWrap, document.querySelector('footer'), questionNavigatorElement]) {
      if (element) element.inert = inert;
    }
  }

  function panelFocusable(panel) {
    if (!panel) return [];
    return [...panel.querySelectorAll('button:not([disabled]), a[href], input:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')]
      .filter((element) => element.getClientRects().length > 0);
  }

  function closeSurfacePanels({ restoreFocus = true } = {}) {
    const returnFocus = panelReturnFocus;
    for (const panel of [sessionMenu, detailsPanel, changesPanel]) panel?.classList.add('hidden');
    panelBackdrop?.classList.add('hidden');
    panelBackdrop?.setAttribute('aria-hidden', 'true');
    document.documentElement.classList.remove('panel-open');
    $('draftState')?.setAttribute('aria-expanded', 'false');
    $('detailsBtn')?.setAttribute('aria-expanded', 'false');
    goalPill?.setAttribute('aria-expanded', 'false');
    setWorkbenchInert(document.documentElement.classList.contains('has-pending-interaction'));
    activeSurfacePanel = null;
    panelReturnFocus = null;
    clearGoalDetails();
    if (restoreFocus && returnFocus?.isConnected) requestAnimationFrame(() => returnFocus.focus());
  }

  function openSurfacePanel(panel, trigger) {
    if (!panel) return;
    if (activeSurfacePanel === panel && !panel.classList.contains('hidden')) {
      closeSurfacePanels();
      return;
    }
    if (activeSurfacePanel) closeSurfacePanels({ restoreFocus: false });
    closeDockMenu();
    activeSurfacePanel = panel;
    panelReturnFocus = trigger || document.activeElement;
    panel.classList.remove('hidden');
    panelBackdrop?.classList.remove('hidden');
    panelBackdrop?.setAttribute('aria-hidden', 'false');
    document.documentElement.classList.add('panel-open');
    $('draftState')?.setAttribute('aria-expanded', panel === sessionMenu ? 'true' : 'false');
    $('detailsBtn')?.setAttribute('aria-expanded', panel === detailsPanel ? 'true' : 'false');
    goalPill?.setAttribute('aria-expanded', panel === detailsPanel ? 'true' : 'false');
    setWorkbenchInert(true);
    requestAnimationFrame(() => (panel.querySelector('[data-close-panel]') || panelFocusable(panel)[0])?.focus());
  }

  function trapSurfacePanelFocus(event) {
    if (event.key !== 'Tab' || !activeSurfacePanel) return;
    const focusable = panelFocusable(activeSurfacePanel);
    if (!focusable.length) { event.preventDefault(); return; }
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }

  function persistPromptDraft(session = selectedSession, value = promptInput.value) {
    composerDelivery.persistDraft(session, value);
  }
  function persistPendingSubmission(
    submission = composerDelivery.pendingSubmission,
    session = submission?.session || selectedSession,
  ) {
    composerDelivery.persistPending(submission, session);
  }
  function clearPendingSubmission(submission) {
    composerDelivery.clearPending(submission);
  }
  function clearDeliveredPromptDraft(submission) {
    composerDelivery.clearDeliveredDraft(submission);
  }
  function preserveFailedPromptDraft(submission) {
    composerDelivery.preserveFailedDraft(submission);
  }
  function restorePromptDraft() {
    promptInput.value = composerDelivery.restore(selectedSession).inputValue;
  }


  document.documentElement.classList.toggle('standalone', Boolean(isStandalone));
  document.documentElement.dataset.faryoDisplayMode = isStandalone ? 'standalone' : 'browser';
  if (typeof immersiveModeApi.createController === 'function') {
    immersiveController = immersiveModeApi.createController({
      document,
      target: document.documentElement,
      root: document.documentElement,
      toggleButtons: [$('immersiveBtn'), $('detailsFullscreenBtn')],
      exitButton: $('immersiveExitBtn'),
      onChange: (active) => {
        if (active && activeSurfacePanel) closeSurfacePanels({ restoreFocus: false });
        syncKeyboardState();
      },
      onError: (reason) => {
        if (activeSurfacePanel) closeSurfacePanels({ restoreFocus: false });
        setError(reason === 'unsupported'
          ? 'Full screen is unavailable here. Install Faryo from Home for an app-style window.'
          : 'The browser did not enter full screen. Try again from a direct tap, or install Faryo from Home.');
      },
    });
  }
  document.documentElement.dataset.faryoImmersive = immersiveController ? 'ready' : 'unavailable';
  if (typeof keyboardLayoutApi.createKeyboardLayout === 'function') {
    keyboardLayoutController = keyboardLayoutApi.createKeyboardLayout(window, {
      root: document.documentElement,
      onChange: (snapshot) => requestAnimationFrame(() => syncKeyboardState(snapshot)),
    });
  }
  if (typeof composerLayoutApi.createComposerLayout === 'function') {
    composerLayoutController = composerLayoutApi.createComposerLayout(window, {
      root: document.documentElement,
      footer: document.querySelector('footer'),
      isTailPinned: () => viewportTailPinned || isNearBottom(),
      onChange: ({ tailPinned }) => {
        if (tailPinned) requestAnimationFrame(() => scrollBottom(true));
        else updateBottomButton();
      },
    });
  }
  restorePromptDraft();
  promptInput.addEventListener('input', () => {
    composerDelivery.discardPendingIfChanged(promptInput.value, selectedSession);
    persistPromptDraft();
    autosize();
    updateSendVisibility();
    renderCommandSuggestions();
  });

  function syncKeyboardState(keyboardSnapshot = keyboardLayoutController?.getSnapshot()) {
    const keyboardActive = Boolean(keyboardSnapshot?.visible) || document.activeElement === promptInput;
    const previousKeyboardActive = document.documentElement.classList.contains('keyboard-open');
    const keepTailPinned = viewportTailPinned || isNearBottom();
    document.documentElement.classList.toggle('keyboard-open', keyboardActive);
    if (keepTailPinned && (previousKeyboardActive !== keyboardActive || keyboardSnapshot?.changed)) {
      requestAnimationFrame(() => scrollBottom(true));
    }
    updateSendVisibility();
    renderCommandSuggestions();
  }
  promptInput.addEventListener('focus', () => syncKeyboardState());
  promptInput.addEventListener('blur', () => setTimeout(syncKeyboardState, 120));
  promptInput.addEventListener('blur', () => setTimeout(hideCommandSuggestions, 120));
  syncKeyboardState();
  function fitPromptTip(text) { return Array.from(text || '').length > 22 ? Array.from(text).slice(0, 21).join('') + '...' : text; }
  function setPromptTip(tip) { currentPromptTip = tip || PROMPT_TIPS[0]; promptInput.placeholder = fitPromptTip(currentPromptTip); promptInput.title = currentPromptTip; }

  function rotatePromptTip() {
    if (promptInput.value) return;
    const pool = PROMPT_TIPS.filter((tip) => tip !== currentPromptTip);
    setPromptTip(pool[Math.floor(Math.random() * pool.length)] || PROMPT_TIPS[0]);
    autosize();
  }
  rotatePromptTip();
  setInterval(rotatePromptTip, TIP_REFRESH_MS);

  function autosize() {
    if (!promptInput.value) {
      promptInput.style.height = '';
      promptInput.style.overflowY = 'hidden';
      promptShell?.classList.remove('composer-multiline');
      return;
    }
    promptInput.style.height = 'auto';
    const promptStyle = getComputedStyle(promptInput);
    const lineHeight = Number.parseFloat(promptStyle.lineHeight) || 20;
    const verticalPadding = (Number.parseFloat(promptStyle.paddingTop) || 0)
      + (Number.parseFloat(promptStyle.paddingBottom) || 0);
    const minimumHeight = Number.parseFloat(promptStyle.minHeight) || 0;
    const oneLineHeight = Math.max(minimumHeight, lineHeight + verticalPadding);
    const visualLines = 1 + Math.ceil(
      Math.max(0, promptInput.scrollHeight - oneLineHeight - 0.5) / lineHeight,
    );
    const alreadyMultiline = Boolean(promptShell?.classList.contains('composer-multiline'));
    const useVerticalActions = visualLines >= (alreadyMultiline ? 2 : 3);
    promptShell?.classList.toggle('composer-multiline', useVerticalActions);
    if (useVerticalActions !== alreadyMultiline) promptInput.style.height = 'auto';
    promptInput.style.overflowY = promptInput.scrollHeight > 136 ? 'auto' : 'hidden';
    promptInput.style.height = Math.min(promptInput.scrollHeight, 136) + 'px';
  }
  autosize();

  let recentDirFetchAt = 0;
  function recentDirCommands() {
    const data = cachedWorkbench();
    if (!data && Date.now() - recentDirFetchAt > 30000) { recentDirFetchAt = Date.now(); refreshSessionMenu().then(() => renderCommandSuggestions()).catch(() => {}); }
    const route = routeBase.replace('/', '');
    const seen = new Set(), items = [];
    const sessions = (data?.sessions || []).slice().sort((a, b) => Number(b.updatedTs || 0) - Number(a.updatedTs || 0));
    for (const item of sessions) {
      const cwd = String(item.cwd || '').trim();
      if (!cwd || cwd === '~' || (route && String(item.route || '') !== route) || seen.has(cwd)) continue;
      if (String(item.tmuxSession || '') === selectedSession && String(item.route || '') === route) continue;
      seen.add(cwd);
      items.push(`cd ${cwd}`);
      if (items.length >= 4) break;
    }
    return items;
  }
  function commandMatches() {
    if (typeof codexCommandApi.match !== 'function') return [];
    const query = promptInput.value.trimStart().toLowerCase();
    return codexCommandApi.match(promptInput.value, { recentDirectories: query.startsWith('cd') ? recentDirCommands() : [], limit: 64 });
  }
  function applyCommandSuggestion(item) {
    const value = String(item?.value || item || '');
    if (!value) return false;
    promptInput.value = value;
    promptInput.focus();
    promptInput.setSelectionRange(value.length, value.length);
    persistPromptDraft();
    autosize();
    updateSendVisibility();
    renderCommandSuggestions();
    return true;
  }
  function renderCommandSuggestions() {
    const items = commandMatches();
    const suggestions = items.map((item) => {
      const label = item.matchedAlias || item.command || item.value;
      const aliases = !item.matchedAlias && item.aliases?.length ? ` · ${item.aliases.join(', ')}` : '';
      const hint = item.argumentHint ? ` ${item.argumentHint}` : '';
      return {
        label,
        hint,
        description: item.description || '',
        category: item.category || 'Command',
        aliases,
        risk: item.risk || '',
      };
    });
    const summary = promptInput.value.trimStart() === '/'
      ? `${items.length} Codex commands · ↑↓ to explore`
      : '';
    composerShellController.updateSuggestions(suggestions, summary);
  }
  function hideCommandSuggestions() {
    composerShellController.updateSuggestions([], '');
  }
  for (const id of ['petControl', 'dockPlusBtn']) $(id)?.addEventListener('pointerdown', (event) => event.preventDefault());

  function updateSendVisibility() {
    const ready = promptInput.value.trim() || pendingAttachments.length > 0;
    const docked = !document.documentElement.classList.contains('keyboard-open');
    composerShellController.updateControls({
      sendVisible: Boolean(ready),
      plusVisible: !Boolean(ready && docked),
    });
    updatePetControl();
  }
  updateSendVisibility();

  function isNearBottom() { return conversationScroller.scrollHeight - conversationScroller.scrollTop - conversationScroller.clientHeight < 80; }
  function updateBottomButton() {
    if (pendingDeferredCapture && isNearBottom()) { applyDeferredCapture(true); return; }
    bottomBtn.classList.toggle('hidden', isNearBottom());
  }

  function applyDeferredCapture(force = false) {
    if (!pendingDeferredCapture) return false;
    if (!force && !isNearBottom()) return false;
    const capture = pendingDeferredCapture;
    pendingDeferredCapture = null;
    renderOutput(capture);
    scrollBottom(true);
    return true;
  }

  function renderCaptureWhenSafe(capture, keepBottom, renderOptions = {}) {
    noteOutputActivity(capture);
    const previousScrollTop = conversationScroller.scrollTop;
    pendingDeferredCapture = null;
    renderOutput(capture, renderOptions);
    if (initialLatestScrollPending) applyInitialLatestScroll(capture?.captureSource !== 'codex-jsonl');
    else if (keepBottom) scrollBottom(true);
    else requestAnimationFrame(() => {
      conversationScroller.scrollTop = previousScrollTop;
      updateBottomButton();
    });
  }

  function scrollBottom(force = false) {
    if (force || isNearBottom()) {
      requestAnimationFrame(() => {
        conversationScroller.scrollTop = conversationScroller.scrollHeight;
        viewportTailPinned = true;
        updateBottomButton();
      });
    }
  }

  function beginInitialLatestScroll() {
    initialLatestScrollPending = true;
    if (initialLatestScrollTimer) clearTimeout(initialLatestScrollTimer);
    initialLatestScrollTimer = null;
  }

  function cancelInitialLatestScroll() {
    initialLatestScrollPending = false;
    if (initialLatestScrollTimer) clearTimeout(initialLatestScrollTimer);
    initialLatestScrollTimer = null;
  }

  function applyInitialLatestScroll(final = false) {
    if (!initialLatestScrollPending || historyController?.userIntentActive()) return false;
    const apply = () => {
      if (!initialLatestScrollPending || historyController?.userIntentActive()) return;
      conversationScroller.scrollTop = conversationScroller.scrollHeight;
      updateBottomButton();
    };
    requestAnimationFrame(() => {
      apply();
      requestAnimationFrame(apply);
    });
    if (final) {
      if (initialLatestScrollTimer) clearTimeout(initialLatestScrollTimer);
      initialLatestScrollTimer = setTimeout(() => {
        if (!initialLatestScrollPending || historyController?.userIntentActive()) return;
        apply();
        initialLatestScrollPending = false;
        initialLatestScrollTimer = null;
      }, 500);
    }
    return true;
  }

  function livePanelStorageKey(session = selectedSession) {
    return `faryoLiveExpanded:${routeBase || 'owner'}:${session || 'default'}`;
  }

  function storedLivePanelPreference(session = selectedSession) {
    try { return sessionStorage.getItem(livePanelStorageKey(session)); }
    catch (_err) { return null; }
  }

  function persistLivePanelPreference(session, expanded) {
    try { sessionStorage.setItem(livePanelStorageKey(session), expanded ? '1' : '0'); }
    catch (_err) {}
  }

  function liveTerminalState() {
    const panel = output.querySelector('.compact-live-terminal');
    if (!panel) return null;
    const expanded = panel.open === true;
    return {
      session: panel.dataset.session || selectedSession,
      expanded,
      scroll: expanded ? (window.FaryoLiveScroll?.snapshot(panel.querySelector('pre')) || null) : null,
    };
  }

  function resolvedLivePanelExpanded(state, session = selectedSession) {
    if (typeof window.FaryoLiveScroll?.resolveExpanded === 'function') {
      return window.FaryoLiveScroll.resolveExpanded(session, state, storedLivePanelPreference(session), window.innerWidth);
    }
    if (state?.session === session && typeof state.expanded === 'boolean') return state.expanded;
    const stored = storedLivePanelPreference(session);
    return stored === '1' || (stored !== '0' && window.innerWidth >= 720);
  }

  function restoreLiveTerminalState(state) {
    const panel = output.querySelector('.compact-live-terminal');
    if (!panel) return;
    const expanded = resolvedLivePanelExpanded(state, selectedSession);
    restoringLivePanels.add(panel);
    panel.open = expanded;
    requestAnimationFrame(() => {
      if (expanded) {
        window.FaryoLiveScroll?.restore(
          panel.querySelector('pre'),
          state?.session === selectedSession ? state.scroll : null,
        );
      }
      setTimeout(() => restoringLivePanels.delete(panel), 0);
    });
  }

  function selectionInsideLivePanel(panel) {
    const selection = window.getSelection?.();
    if (!panel || !selection || selection.isCollapsed) return false;
    return panel.contains(selection.anchorNode) || panel.contains(selection.focusNode);
  }

  function liveLineCount(text) {
    return String(text || '').split('\n').filter((line) => line.length).length;
  }

  function updateLivePanelLabel(panel, text, paused = false) {
    const label = panel?.querySelector('.compact-live-state');
    if (!label) return;
    const lines = liveLineCount(text);
    label.textContent = paused ? `Updates paused · ${lines} lines ready` : `Agent working · ${lines} lines`;
  }

  function createLiveTerminalPanel() {
    const panel = document.createElement('details');
    panel.className = 'compact-live-terminal';
    panel.dataset.session = selectedSession || 'default';
    panel.dataset.faryoTransient = 'live';
    panel.dataset.liveRevision = '0';
    panel.innerHTML = '<summary class="compact-live-title"><span class="live-dot"></span><span>Live from tmux</span><span class="compact-live-state">Agent working</span><button class="compact-live-copy" type="button" aria-label="Copy Live from tmux" title="Copy Live from tmux">⧉</button></summary><pre></pre>';
    output.appendChild(panel);
    return panel;
  }

  function commitLiveTerminalText(panel, text, state = null) {
    const pre = panel?.querySelector('pre');
    if (!pre) return;
    const scrollState = state || liveTerminalState();
    pre.textContent = String(text || '');
    panel.__faryoPendingLiveText = null;
    panel.__faryoPendingLiveRemoval = false;
    panel.dataset.liveRevision = String(Number(panel.dataset.liveRevision || 0) + 1);
    updateLivePanelLabel(panel, text, false);
    restoreLiveTerminalState(scrollState);
  }

  function syncLiveTerminal(text, state = null) {
    const value = String(text || '');
    let panel = output.querySelector('.compact-live-terminal');
    if (!value) {
      if (!panel) return;
      if (selectionInsideLivePanel(panel)) {
        panel.__faryoPendingLiveRemoval = true;
        const label = panel.querySelector('.compact-live-state');
        if (label) label.textContent = 'Finished · selection held';
      } else {
        panel.remove();
      }
      return;
    }
    if (!panel) panel = createLiveTerminalPanel();
    panel.dataset.session = selectedSession || 'default';
    panel.__faryoPendingLiveRemoval = false;
    const pre = panel.querySelector('pre');
    if (pre?.textContent === value && !panel.__faryoPendingLiveText) return;
    if (selectionInsideLivePanel(panel)) {
      panel.__faryoPendingLiveText = value;
      updateLivePanelLabel(panel, value, true);
      return;
    }
    commitLiveTerminalText(panel, value, state);
  }

  function flushDeferredLiveTerminal() {
    const panel = output.querySelector('.compact-live-terminal');
    if (!panel || selectionInsideLivePanel(panel)) return;
    if (panel.__faryoPendingLiveRemoval) {
      panel.remove();
      return;
    }
    if (typeof panel.__faryoPendingLiveText === 'string') {
      const value = panel.__faryoPendingLiveText;
      commitLiveTerminalText(panel, value, liveTerminalState());
    }
  }

  let liveSelectionFlushTimer = null;
  document.addEventListener('selectionchange', () => {
    if (liveSelectionFlushTimer) clearTimeout(liveSelectionFlushTimer);
    liveSelectionFlushTimer = setTimeout(flushDeferredLiveTerminal, 80);
  });

  conversationScroller.addEventListener('scroll', () => {
    viewportTailPinned = isNearBottom();
    updateBottomButton();
    richBlockController?.setTailPinned(isNearBottom());
    maybeLoadOlderHistory();
  }, { passive: true });
  conversationScroller.addEventListener('wheel', noteHistoryUserIntent, { passive: true });
  conversationScroller.addEventListener('touchstart', noteHistoryUserIntent, { passive: true });
  conversationScroller.addEventListener('touchmove', noteHistoryUserIntent, { passive: true });
  conversationScroller.addEventListener('pointerdown', noteHistoryUserIntent, { passive: true });
  bottomBtn.addEventListener('click', () => { if (!applyDeferredCapture(true)) scrollBottom(true); });
  output.addEventListener('toggle', (event) => {
    const panel = event.target.closest?.('.compact-live-terminal');
    if (!panel) return;
    if (restoringLivePanels.has(panel)) {
      restoringLivePanels.delete(panel);
      return;
    }
    persistLivePanelPreference(panel.dataset.session || selectedSession, panel.open);
    if (panel.open) requestAnimationFrame(() => window.FaryoLiveScroll?.restore(panel.querySelector('pre'), null));
  }, true);
  output.addEventListener('click', async (event) => {
    const liveCopy = event.target.closest('.compact-live-copy');
    if (liveCopy) {
      event.preventDefault();
      event.stopPropagation();
      const text = liveCopy.closest('.compact-live-terminal')?.querySelector('pre')?.textContent || '';
      try {
        await navigator.clipboard.writeText(text);
        liveCopy.textContent = '✓';
        setTimeout(() => { if (liveCopy.isConnected) liveCopy.textContent = '⧉'; }, 900);
      } catch (_error) {
        setError('Copy failed');
      }
      return;
    }
    const protectedLink = event.target.closest('a[data-faryo-fetch-href]');
    if (protectedLink) {
      event.preventDefault();
      await openProtectedResource(protectedLink);
      return;
    }
    const codeCopy = event.target.closest('.markdown-code-copy');
    if (codeCopy) {
      const text = codeCopy.closest('.markdown-code-block')?.querySelector('pre')?.textContent || '';
      try {
        await navigator.clipboard.writeText(text);
        codeCopy.textContent = 'Copied';
        codeCopy.setAttribute('aria-label', 'Code copied');
        setTimeout(() => {
          if (!codeCopy.isConnected) return;
          codeCopy.textContent = 'Copy';
          codeCopy.setAttribute('aria-label', 'Copy code');
        }, 1000);
      } catch (_error) {
        setError('Copy failed');
      }
      return;
    }
    const copy = event.target.closest('.copy-output-block');
    if (copy) {
      const block = copy.closest('.compact-block.output');
      const payload = copyFidelity?.payloadForBlock(block);
      const copied = payload ? await copyFidelity.write(payload) : false;
      if (copied) {
        copy.textContent = '✓';
        setTimeout(() => { if (copy.isConnected) copy.textContent = '⧉'; }, 900);
      } else {
        setError('Copy failed');
      }
      return;
    }
    const image = event.target.closest('.chat-image-thumb');
    if (image) {
      const source = image.querySelector('img');
      showImageLightbox(source?.currentSrc || source?.src || '', image.dataset.label || '');
      return;
    }
    const markdownImage = event.target.closest('.chat-markdown-image');
    if (markdownImage) {
      showImageLightbox(markdownImage.currentSrc || markdownImage.src || '', markdownImage.alt || 'Image preview');
      return;
    }
  });
  document.addEventListener('copy', (event) => {
    if (outputMode === 'compact') copyFidelity?.handleCopy(event);
  });
  window.addEventListener('faryo-markdown-highlighter-ready', () => {
    markdownRenderRevision += 1;
    clearMarkdownRenderCache();
    if (!lastCompactCapture || outputMode !== 'compact' || highlighterRenderFrame) return;
    highlighterRenderFrame = requestAnimationFrame(() => {
      highlighterRenderFrame = 0;
      const keepBottom = isNearBottom();
      renderOutput(lastCompactCapture);
      if (keepBottom) scrollBottom(true);
    });
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    document.getElementById('imageLightbox')?.classList.add('hidden');
  });
  window.addEventListener('pagehide', () => {
    for (const url of protectedImageUrls.values()) URL.revokeObjectURL(url);
    protectedImageUrls.clear();
  });

  function setError(message, options = {}) {
    if (!message && Date.now() < preserveErrorUntil) return;
    if (errorTimer) { clearTimeout(errorTimer); errorTimer = null; }
    if (!message) { errorBox.classList.add('hidden'); errorBox.textContent = ''; return; }
    errorBox.textContent = message;
    errorBox.classList.remove('hidden');
    const timeoutMs = options.timeoutMs === undefined ? 5000 : options.timeoutMs;
    if (timeoutMs > 0) {
      errorTimer = window.setTimeout(() => {
        errorTimer = null;
        setError('');
      }, timeoutMs);
    }
  }

  function userErrorMessage(err) {
    const status = err && err.status;
    const raw = (err && err.message) || 'Request failed';
    const messageMap = {
      'single session mode': 'Single-session mode is enabled.',
    };
    const detail = messageMap[raw] || raw;
    const recovery = String(err?.recovery || err?.payload?.recovery || '').trim();
    const title = String(err?.errorTitle || err?.payload?.errorTitle || '').trim();
    const explanation = title && title !== detail ? `${title}: ${detail}` : detail;
    const body = recovery && recovery !== explanation ? `${explanation}\n${recovery}` : explanation;
    return err?.errorCode ? body : status ? `HTTP ${status}: ${body}` : body;
  }

  function setBusy(isBusy) {
    for (const id of ['sendBtn', 'refreshBtn', 'dockFullBtn', 'detailsChatBtn', 'detailsRawBtn', 'detailsRefreshBtn', 'dockPlusBtn', 'attachmentBtn', 'fastToggle']) {
      const el = $(id);
      if (el) el.disabled = isBusy;
    }
  }

  function handleBackgroundError(err) {
    if (!err || err.name === 'AbortError') return;
    console.debug('background refresh failed', err);
  }

  async function loadOwnerCapabilities() {
    const payload = await api('/api/capabilities');
    document.documentElement.dataset.faryoCapabilitySchema = String(payload.schemaVersion || 'unknown');
    $('detailsChangesBtn').hidden = payload.features?.workspaceChanges === false;
    $('detailsDiagnosticsBtn').hidden = payload.features?.diagnostics === false;
    return payload;
  }

  async function loadCodexCommandCatalog() {
    if (typeof codexCommandApi.replaceInventory !== 'function') return null;
    const payload = await api('/api/command-catalog');
    codexCommandApi.replaceInventory(payload.commands, payload);
    document.documentElement.dataset.faryoCommandCatalog = payload.source || 'fallback';
    document.documentElement.dataset.faryoCommandDrift = payload.drifted ? 'true' : 'false';
    renderCommandSuggestions();
    return payload;
  }

  function goalElapsedLabel(value) {
    const seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds < 0) return '—';
    if (seconds < 60) return `${Math.round(seconds)}s`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.floor(minutes / 60), remainder = minutes % 60;
    return remainder ? `${hours}h ${remainder}m` : `${hours}h`;
  }

  function clearGoalDetails() {
    goalDetailsRequestGeneration += 1;
    const section = $('goalDetailsSection');
    if (section) section.hidden = true;
    if ($('goalDetailsState')) $('goalDetailsState').textContent = '';
    if ($('goalDetailsObjective')) $('goalDetailsObjective').textContent = '';
    for (const id of ['goalDetailsElapsed', 'goalDetailsTokens', 'goalDetailsBudget']) {
      if ($(id)) $(id).textContent = '—';
    }
  }

  async function loadGoalDetails() {
    const section = $('goalDetailsSection');
    if (!section) return;
    const generation = ++goalDetailsRequestGeneration;
    const requestedSession = selectedSession;
    section.hidden = false;
    requestAnimationFrame(() => section.scrollIntoView({ block: 'nearest' }));
    $('goalDetailsState').textContent = 'Loading…';
    $('goalDetailsObjective').textContent = '';
    try {
      const payload = await api(apiPath('/api/goal'));
      if (generation !== goalDetailsRequestGeneration || requestedSession !== selectedSession) return;
      const status = String(payload.status || 'none').replaceAll('_', ' ');
      $('goalDetailsState').textContent = status === 'none'
        ? 'No active goal'
        : status.charAt(0).toUpperCase() + status.slice(1);
      $('goalDetailsObjective').textContent = payload.objective
        || (status === 'none' ? 'No objective is active.' : 'Objective unavailable.');
      $('goalDetailsElapsed').textContent = goalElapsedLabel(payload.timeUsedSeconds);
      $('goalDetailsTokens').textContent = Number.isFinite(Number(payload.tokensUsed))
        ? Number(payload.tokensUsed).toLocaleString()
        : '—';
      $('goalDetailsBudget').textContent = Number.isFinite(Number(payload.tokenBudget))
        ? Number(payload.tokenBudget).toLocaleString()
        : 'No fixed budget';
      if (payload.objectiveTruncated) $('goalDetailsObjective').textContent += '\n\n[Objective display truncated]';
    } catch (error) {
      if (generation !== goalDetailsRequestGeneration || requestedSession !== selectedSession) return;
      $('goalDetailsState').textContent = 'Goal details unavailable';
      $('goalDetailsObjective').textContent = userErrorMessage(error);
    }
  }

  async function downloadDiagnostics() {
    const button = $('detailsDiagnosticsBtn');
    button.disabled = true;
    try {
      const payload = await api('/api/diagnostics');
      const blob = new Blob([`${JSON.stringify(payload, null, 2)}\n`], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = 'faryo-diagnostics.json';
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      button.querySelector('span').textContent = 'Diagnostics downloaded';
      setTimeout(() => { if (button.isConnected) button.querySelector('span').textContent = 'Download diagnostics'; }, 1200);
    } catch (error) {
      setError(userErrorMessage(error));
    } finally {
      button.disabled = false;
    }
  }

  function apiPath(path) {
    return sessionApiPath(path, selectedSession);
  }

  function historyAnchorSnapshot() {
    const scrollerTop = conversationScroller.getBoundingClientRect().top;
    const child = [...output.children].find((element) => element.getBoundingClientRect().bottom > scrollerTop + 1);
    return child ? {
      key: child.dataset.faryoBlockKey || '',
      top: child.getBoundingClientRect().top,
      scrollTop: conversationScroller.scrollTop,
      scrollHeight: conversationScroller.scrollHeight,
    } : { key: '', top: 0, scrollTop: conversationScroller.scrollTop, scrollHeight: conversationScroller.scrollHeight };
  }

  function restoreHistoryAnchor(snapshot) {
    if (!snapshot) return;
    const apply = () => {
      const target = snapshot.key
        ? [...output.children].find((element) => element.dataset.faryoBlockKey === snapshot.key)
        : null;
      conversationScroller.scrollTop = target
        ? conversationScroller.scrollTop + target.getBoundingClientRect().top - snapshot.top
        : snapshot.scrollTop + Math.max(0, conversationScroller.scrollHeight - snapshot.scrollHeight);
      updateBottomButton();
    };
    requestAnimationFrame(() => {
      apply();
      requestAnimationFrame(apply);
    });
  }

  historyController = createHistoryController({
    view: window,
    api,
    apiPath,
    scroller: conversationScroller,
    output,
    pageTurns: HISTORY_PAGE_TURNS,
    refreshMinMs: HISTORY_REFRESH_MIN_MS,
    fetchTimeoutMs: FETCH_TIMEOUT_MS,
    getSelectedSession: () => selectedSession,
    getExpectedSessionId: (fallback) => String(lastCompactCapture?.sessionId || fallback || ''),
    getLastCapture: () => lastCompactCapture,
    getOutputMode: () => outputMode,
    renderCapture: renderOutput,
    anchorSnapshot: historyAnchorSnapshot,
    restoreAnchor: restoreHistoryAnchor,
    isInitialLatestPending: () => initialLatestScrollPending,
    applyInitialLatestScroll,
    beginInitialLatestScroll,
    cancelInitialLatestScroll,
    isNearBottom,
    scrollBottom,
    setError,
    userErrorMessage,
    handleBackgroundError,
  });

  richBlockController = createRichBlockController({
    view: window,
    scroller: conversationScroller,
    observerRoot: outputWrap,
    isNearBottom,
    renderBlock: (node, descriptor) => {
      node.innerHTML = renderTextWithFilesSafely(descriptor.text, descriptor.renderOptions);
    },
    onHydrated: (node, descriptor, state) => {
      copyFidelity?.bindBlock(node, {
        source: descriptor.copySource,
        renderSource: descriptor.renderSource,
        kind: descriptor.kind,
      });
      void hydrateProtectedImages(node);
      questionNavigatorController?.refreshLayout?.();
      if (state.wasNearBottom) scrollBottom(true);
    },
    onReleased: () => {
      releaseDetachedProtectedImages();
      questionNavigatorController?.refreshLayout?.();
    },
  });

  function resetConversationHistory() { historyController.reset(); richBlockController?.clear(); }
  function structuredCapture(capture) { return isStructuredCapture(capture); }
  function loadedHistoryTurns() { return historyController.loadedTurns(); }
  function mergedConversationCapture(capture) { return historyController.mergedCapture(capture); }
  function resolveQuestionTarget(question) { return historyController.resolveQuestionTarget(question); }
  function prepareQuestionTarget(target) { richBlockController?.ensure(target, 1); }
  function scheduleConversationHistoryRefresh(capture, delay = 80) { historyController.scheduleRefresh(capture, delay); }
  function noteHistoryUserIntent() { historyController.noteUserIntent(); }
  function maybeLoadOlderHistory() { historyController.maybeLoadOlder(); }


  function localResourcePath(path) {
    return routeBase + apiPath(path);
  }

  function localFileTarget(path, line = 0, column = 0) {
    const endpoint = ownerToken ? '/api/local-file' : '/api/local-file/view';
    const resourcePath = `${endpoint}?path=${encodeURIComponent(path)}${line ? `&line=${line}` : ''}${column ? `&column=${column}` : ''}`;
    const href = localResourcePath(resourcePath);
    return ownerToken ? { href, fetchHref: href } : href;
  }

  function localImageTarget(path) {
    const href = localResourcePath(`/api/local-image?path=${encodeURIComponent(path)}`);
    return ownerToken ? { href: '', fetchHref: href } : href;
  }

  function setLiveState(state) {
    liveState = state;
    if ($('detailsConnection')) $('detailsConnection').textContent = state;
    updatePetControl();
  }

  function ensureOutputActivityTimer() {
    if (outputActivityTimer) return;
    outputActivityTimer = setInterval(() => {
      outputActivity = Math.max(0, outputActivity - 0.6);
      updatePetControl();
      if (outputActivity === 0) {
        clearInterval(outputActivityTimer);
        outputActivityTimer = null;
      }
    }, PET_RUN_DECAY_MS);
  }

  function noteOutputActivity(capture) {
    const text = `${capture?.text || ''}\n${capture?.liveText || ''}`;
    const signature = `${text.length}:${text.slice(-180)}`;
    if (signature === lastCaptureSignature) return;
    const delta = Math.max(0, text.length - Number(lastCaptureSignature.split(':', 1)[0] || 0));
    lastCaptureSignature = signature;
    if (!agentRunning) return;
    outputActivity = Math.min(5, outputActivity + (delta > 1600 ? 1.8 : delta > 360 ? 1.2 : 0.8));
    ensureOutputActivityTimer();
    updatePetControl();
  }

  function petPhase() {
    if (petStopping) return 'stopping';
    if (petSending) return 'send';
    if (pendingAttachments.some((item) => ['compressing', 'uploading'].includes(item.status))) return 'carrying';
    if (pendingAttachments.length) return 'carrying';
    if (queuedSendNowAvailable) return 'queued';
    if (outputMode === 'full' && fullLocked) return 'offline';
    if (promptInput.value.trim() || document.activeElement === promptInput || document.documentElement.classList.contains('keyboard-open')) return 'working';
    if (agentRunning) return 'running';
    if (liveState === 'live') return 'idle';
    if (liveState === 'reconnecting') return 'resting';
    return 'offline';
  }

  function updatePetControl() {
    const pet = $('petControl');
    if (!pet) return;
    const phase = petPhase();
    const labels = { stopping: 'stopping', send: 'sending', carrying: 'carrying files', queued: 'queued message; tap to interrupt and send now (Esc)', working: 'working', running: 'running', idle: 'online', resting: 'reconnecting', offline: 'offline' };
    if (phase !== lastPetPhase) {
      lastPetPhase = phase;
      pet.className = `pet-control pet-${phase}`;
      pet.title = `Faryo ${labels[phase] || phase}`;
      pet.setAttribute('aria-label', `${pet.title}; tap to interrupt`);
      if (phase === 'queued') pet.setAttribute('aria-keyshortcuts', 'Escape');
      else pet.removeAttribute('aria-keyshortcuts');
    }
    if (phase === 'running' || phase === 'queued') {
      const speed = outputActivity >= 3.5 ? '.48s' : outputActivity >= 1.6 ? '.72s' : outputActivity > 0 ? '1.08s' : '1.6s';
      pet.style.setProperty('--pet-run-speed', speed);
    } else if (pet.style.getPropertyValue('--pet-run-speed')) {
      pet.style.removeProperty('--pet-run-speed');
    }
  }

  function playPetSend() {
    petSending = true;
    agentRunning = true;
    outputActivity = Math.max(outputActivity, 2.2);
    ensureOutputActivityTimer();
    if (promptShell) {
      promptShell.classList.remove('pet-sending');
      void promptShell.offsetWidth;
      promptShell.classList.add('pet-sending');
    }
    if (petSendTimer) clearTimeout(petSendTimer);
    petSendTimer = setTimeout(() => {
      petSending = false;
      petSendTimer = null;
      promptShell?.classList.remove('pet-sending');
      updatePetControl();
    }, PET_SEND_MS);
    updatePetControl();
  }

  function stopPetSend() {
    if (petSendTimer) clearTimeout(petSendTimer);
    petSendTimer = null;
    petSending = false;
    promptShell?.classList.remove('pet-sending');
  }

  function playPetStop() {
    petStopping = true;
    if (petStopTimer) clearTimeout(petStopTimer);
    petStopTimer = setTimeout(() => {
      petStopping = false;
      petStopTimer = null;
      updatePetControl();
    }, PET_STOP_MS);
    updatePetControl();
  }

  function stopPetStop() {
    if (petStopTimer) clearTimeout(petStopTimer);
    petStopTimer = null;
    petStopping = false;
  }

  function setStatusRefresh(on) { if (statusRefreshTimer) clearInterval(statusRefreshTimer); statusRefreshTimer = null; if (on && !document.hidden) statusRefreshTimer = setInterval(() => refreshStatus({ silent: true }).catch(handleBackgroundError), STATUS_REFRESH_MS); }
  function headerStatusVisible() { return !document.querySelector('header')?.classList.contains('collapsed'); }
  function syncStatusRefresh(refreshNow = false) { const on = headerStatusVisible(); setStatusRefresh(on); if (on && refreshNow) refreshStatus({ silent: true }).catch(handleBackgroundError); }

  statusController = createStatusController({
    view: window,
    timeoutMs: FETCH_TIMEOUT_MS,
    getScope: () => conversationStore.scope(),
    acceptScope: (scope) => conversationStore.accepts(scope),
    setError,
    loadStatus: (signal) => api(apiPath('/api/status'), { signal }),
    onStatus: renderStatus,
  });

  captureController = createCaptureController({
    view: window,
    compactLines: COMPACT_CAPTURE_LINES,
    fullLines: FULL_CAPTURE_LINES,
    fetchTimeoutMs: FETCH_TIMEOUT_MS,
    fullRefreshMs: FULL_REFRESH_MS,
    fallbackRefreshMs: CAPTURE_FALLBACK_MS,
    safetyRefreshMs: CAPTURE_SAFETY_MS,
    eventIdleTimeoutMs: EVENT_STREAM_IDLE_MS,
    currentLines: currentCaptureLines,
    getOutputMode: () => outputMode,
    getScope: () => conversationStore.scope(),
    acceptScope: (scope) => conversationStore.accepts(scope),
    isHidden: () => document.hidden,
    setError,
    setLiveState,
    loadCapture: (lines, signal) => {
      const format = outputMode === 'compact' ? '' : '&format=html';
      return api(apiPath(`/api/capture?lines=${lines}${format}`), { signal });
    },
    onCapture: (capture, meta) => {
      if (!conversationStore.commitCapture(capture, meta.scope)) return;
      const keepBottom = isNearBottom();
      if (capture.sessionTitle) renderSessionLabel(capture.sessionTitle);
      if (Object.prototype.hasOwnProperty.call(capture, 'agentRunning')) {
        const nextRunning = Boolean(capture.agentRunning);
        if (meta.source === 'refresh' || nextRunning !== agentRunning) {
          agentRunning = nextRunning;
          updatePetControl();
        }
      }
      if (Object.prototype.hasOwnProperty.call(capture, 'queuedSendNowAvailable')) {
        queuedSendNowAvailable = Boolean(capture.queuedSendNowAvailable);
        updatePetControl();
      }
      if (meta.source === 'refresh' || outputMode === 'compact') {
        renderCaptureWhenSafe(capture, keepBottom, { conversationCommitted: true });
      }
    },
    handleBackgroundError,
    refreshStatusIfVisible: () => {
      if (headerStatusVisible()) refreshStatus({ silent: true }).catch(handleBackgroundError);
    },
    fetch: (...args) => window.fetch(...args),
    eventUrl: (cursor = '') => routeBase + apiPath(
      `/api/events?lines=${COMPACT_CAPTURE_LINES}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`,
    ),
    ownerHeaders: ownerApiClient.ownerHeaders,
    eventStreamParser,
    validateEnvelope: validateBrowserEnvelope,
    debug: (...args) => console.debug(...args),
  });

  function refreshCapture(lines = currentCaptureLines(), options = {}) {
    return captureController.refresh(lines, options);
  }
  function startEventStream() { captureController.startEventStream(); }
  function closeEventStream() { captureController.closeEventStream(); }
  function setCaptureFallback(on) { captureController.setFallback(on); }
  function setFullRefresh(on) { captureController.setFullRefresh(on); }


  function compactGitLabel(git) {
    if (!git) return 'No Git';
    if (git.state === 'error' && /^(?:⚠️|⚠)?\s*DETACHED\b/u.test(git.label || '')) return (git.label || '⚠️ DETACHED').replace(/^(?:⚠️|⚠)?\s*/u, '⚠️ ');
    const clean = git.state === 'clean';
    const icon = clean ? '🌿' : '✏️';
    const raw = (git.label || '').replace(/^(?:🌿|✏️|✏)\s*/u, '').trim();
    const parts = raw.split(/\s+/).filter(Boolean);
    const markRe = /^(?:[+-]\d+|±\d+|\?\d+|[↑↓]\d+|m[+-]\d+)$/;
    const marks = parts.filter((part) => markRe.test(part)).join(' ');
    const branch = parts.filter((part) => !markRe.test(part)).join(' ') || 'git';
    const shortBranch = branch.length > 14 ? `${branch.slice(0, 13)}…` : branch;
    return `${icon}${marks ? ` ${marks}` : ''} ${shortBranch}`;
  }

  function gitStatusModel(git) {
    return {
      text: compactGitLabel(git),
      title: git ? git.title : 'Current directory is not a Git repository',
      state: (git && git.state) || 'muted',
    };
  }

  function weeklyElapsedPercent(rateLimit) {
    if (rateLimit.resetsAt === null || rateLimit.resetsAt === ''
      || rateLimit.windowDurationMins === null || rateLimit.windowDurationMins === '') return null;
    const resetSeconds = Number(rateLimit.resetsAt);
    const windowMinutes = Number(rateLimit.windowDurationMins);
    if (!Number.isFinite(resetSeconds) || !Number.isFinite(windowMinutes) || windowMinutes <= 0) return null;
    const windowMs = windowMinutes * 60 * 1000;
    const startMs = resetSeconds * 1000 - windowMs;
    const elapsedMs = Math.min(Math.max(Date.now() - startMs, 0), windowMs);
    return Math.round((elapsedMs / windowMs) * 100);
  }

  function numericTokenCount(value) {
    if (value === null || value === '' || typeof value === 'undefined') return null;
    const number = Number(value);
    return Number.isFinite(number) && number >= 0 ? Math.round(number) : null;
  }

  function compactTokenCount(value) {
    const count = numericTokenCount(value);
    if (count === null) return null;
    if (count >= 1_000_000) {
      const millions = Math.round((count / 1_000_000) * 10) / 10;
      return `${Number.isInteger(millions) ? millions : millions.toFixed(1)}m`;
    }
    if (count >= 100_000) return `${Math.round(count / 1000)}k`;
    if (count >= 10_000) return `${(count / 1000).toFixed(1)}k`;
    if (count >= 1000) return `${Math.round(count / 1000)}k`;
    return String(count);
  }

  function exactTokenCount(value) {
    const count = numericTokenCount(value);
    if (count === null) return null;
    try { return new Intl.NumberFormat().format(count); }
    catch (_err) { return String(count); }
  }

  function contextStatusModel(contextUsage) {
    const percent = Number(contextUsage.percent);
    const percentText = Number.isFinite(percent) ? `${quotaPercent(percent)}%` : null;
    const usedTokens = numericTokenCount(contextUsage.usedTokens ?? contextUsage.inputTokens);
    const contextWindow = numericTokenCount(contextUsage.contextWindow);
    const reportedWindow = contextUsage.contextWindowSource === 'agent-reported';
    const hasReportedCounts = reportedWindow && usedTokens !== null && contextWindow > 0;
    const compactCounts = hasReportedCounts ? `${compactTokenCount(usedTokens)}/${compactTokenCount(contextWindow)}` : '';
    const compact = `Ctx ${percentText || '--'}${compactCounts ? ` · ${compactCounts}` : ''}`;
    const detail = hasReportedCounts
      ? `${exactTokenCount(usedTokens)} / ${exactTokenCount(contextWindow)} tokens${percentText ? ` · ${percentText} used` : ''}`
      : (percentText ? `${percentText} used` : 'Unavailable');
    return {
      compact,
      detail,
      title: hasReportedCounts ? `Agent-reported context · ${detail}` : detail,
    };
  }

  function quotaPercent(value) {
    if (value === null || value === '' || typeof value === 'undefined') return null;
    const number = Number(value);
    if (!Number.isFinite(number)) return null;
    const rounded = Math.round(Math.max(0, Math.min(100, number)) * 10) / 10;
    return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
  }

  function weeklyResetLabel(rateLimit) {
    if (rateLimit.resetsAt === null || rateLimit.resetsAt === '' || typeof rateLimit.resetsAt === 'undefined') return '';
    const resetSeconds = Number(rateLimit.resetsAt);
    if (!Number.isFinite(resetSeconds)) return '';
    const reset = new Date(resetSeconds * 1000);
    if (Number.isNaN(reset.getTime())) return '';
    try {
      return new Intl.DateTimeFormat(undefined, {
        month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
      }).format(reset);
    } catch (_err) {
      return reset.toLocaleString();
    }
  }

  function quotaStatusModel(rateLimit) {
    const percent = rateLimit.usedPercent === null || typeof rateLimit.usedPercent === 'undefined' ? NaN : Number(rateLimit.usedPercent);
    const scopedPercent = rateLimit.scopedPercent === null || typeof rateLimit.scopedPercent === 'undefined' ? NaN : Number(rateLimit.scopedPercent);
    if (Number.isFinite(scopedPercent)) {
      const used = quotaPercent(percent);
      const scopedUsed = quotaPercent(scopedPercent);
      const remaining = used === null ? null : quotaPercent(100 - Number(used));
      const scopedRemaining = quotaPercent(100 - Number(scopedUsed));
      const scopedLabel = rateLimit.scopedLabel || 'Model';
      const compact = remaining === null ? 'Week --' : `Week ${remaining}% left`;
      const detail = [remaining === null ? 'All unavailable' : `All ${remaining}% left`, `${scopedLabel} ${scopedRemaining}% left`].join(' · ');
      return {
        compact,
        detail,
        title: `Weekly quota · ${detail}`,
        percent: used === null ? 0 : Number(used),
        weekPercent: Number(scopedUsed),
      };
    }
    const weekPercent = weeklyElapsedPercent(rateLimit);
    if (!Number.isFinite(percent)) {
      return {
        compact: 'Week --',
        detail: 'Unavailable',
        title: 'Quota unknown',
        percent: 0,
        weekPercent: Number.isFinite(weekPercent) ? weekPercent : 0,
      };
    }
    const clamped = Math.max(0, Math.min(100, percent));
    const used = quotaPercent(clamped);
    const remaining = quotaPercent(100 - clamped);
    const reset = weeklyResetLabel(rateLimit);
    const compact = `Week ${remaining}% left`;
    const detail = `${remaining}% left · ${used}% used${reset ? ` · resets ${reset}` : ''}`;
    return {
      compact,
      detail,
      title: `Weekly quota · ${detail}`,
      percent: clamped,
      weekPercent: Number.isFinite(weekPercent) ? Math.max(0, Math.min(100, weekPercent)) : 0,
    };
  }

  function leadingText(text, maxChars) {
    const chars = Array.from(String(text || ''));
    return chars.length <= maxChars ? chars.join('') : chars.slice(0, maxChars).join('') + '...';
  }

  function compactPathLabel(path) {
    const value = String(path || '').replace(/\\/g, '/').replace(/\/$/, '');
    if (!value) return 'cwd unknown';
    if (value === '~') return '~';
    return value.split('/').filter(Boolean).pop() || value;
  }

  function compactModelLabel(model) {
    return String(model || 'model')
      .replace(/\s+/g, ' ')
      .trim()
      .replace(/\s+fast$/i, '')
      .replace(/\bgpt(?=[-\s])/i, 'GPT');
  }

  function fastStatusModel(value, { visible = true, disabled = false } = {}) {
    const active = String(value || '').toLowerCase() === 'on';
    return {
      fastVisible: visible,
      fastActive: active,
      fastDisabled: disabled,
      fastText: active ? 'Fast' : 'Default',
      fastTitle: active
        ? 'Fast is enabled for this conversation. Click to use Default speed.'
        : 'Default speed for this conversation. Click to enable Fast; Fast uses more quota.',
    };
  }

  function updateFolderLabel(data) {
    const cwdText = data.displayCwd || data.shortCwd || data.cwd || 'cwd unknown';
    const folderLabel = `📁 ${compactPathLabel(cwdText)}`;
    $('draftState').textContent = leadingText(folderLabel, 22);
    $('draftState').title = cwdText;
  }

  function updateCachedSessionTitle(sessionLabel) {
    try {
      const cached = JSON.parse(sessionStorage.getItem(WORKBENCH_CACHE_KEY) || 'null');
      if (!cached?.data) return;
      const route = routeBase.replace('/', '');
      let changed = false;
      for (const collection of [cached.data.sessions, cached.data.activeSessions]) {
        if (!Array.isArray(collection)) continue;
        for (const item of collection) {
          if (String(item?.tmuxSession || '') !== selectedSession) continue;
          if (route && item?.route && String(item.route) !== route) continue;
          if (item.title === sessionLabel) continue;
          item.title = sessionLabel;
          changed = true;
        }
      }
      if (!changed) return;
      sessionStorage.setItem(WORKBENCH_CACHE_KEY, JSON.stringify(cached));
      if (activeSurfacePanel === sessionMenu) renderSessionMenu(cached.data, false);
    } catch (_err) {}
  }

  function renderSessionLabel(value, { syncCache = true } = {}) {
    const sessionLabel = String(value || '').replace(/\s+/g, ' ').trim();
    if (!sessionLabel) return;
    $('topicText').textContent = leadingText(sessionLabel, 18);
    $('sessionTitle').title = `${$('ownerText').textContent || 'TMUX'} · ${sessionLabel}`;
    if ($('detailsSession')) $('detailsSession').textContent = sessionLabel;
    document.title = `${sessionLabel} · Faryo`;
    if (syncCache) updateCachedSessionTitle(sessionLabel);
  }

  function renderStatus(data) {
    const model = data.model || `tmux:${data.session || 'unknown'}`;
    const ownerLabel = data.ownerLabel || 'TMUX';
    const contextUsage = data.contextUsage || {};
    const context = contextStatusModel(contextUsage);
    const weeklyRateLimit = data.weeklyRateLimit || {};
    const sessionLabel = data.sessionTitle || data.sessionId || data.session || 'Starting Codex';
    const modelLabel = data.agentState === 'starting'
      ? data.codexUpdateStatus === 'pending'
        ? 'Checking Codex update…'
        : 'Starting Codex…'
      : compactModelLabel(model);
    const fastVisible = data.agentSource === 'codex-cli'
      && !['starting', 'exited'].includes(String(data.agentState || ''));
    currentFastStatus = String(data.fastStatus || '').toLowerCase() === 'on' ? 'on' : 'off';
    const fastView = fastStatusModel(currentFastStatus, {
      visible: fastVisible,
      disabled: !fastVisible
        || Boolean(data.agentRunning)
        || Boolean(data.interaction)
        || data.agentState === 'pending_interaction',
    });
    const nextSession = data.session || selectedSession;
    if (nextSession !== selectedSession) conversationStore.switchSession(nextSession);
    selectedSession = nextSession;
    if (data.session) {
      const currentUrl = new URL(location.href);
      if (currentUrl.searchParams.get('session') !== data.session) {
        currentUrl.searchParams.set('session', data.session);
        history.replaceState(null, '', `${currentUrl.pathname}${currentUrl.search}${currentUrl.hash}`);
      }
    }
    $('ownerText').textContent = ownerLabel;
    renderSessionLabel(sessionLabel);
    const quota = quotaStatusModel(weeklyRateLimit);
    const goalModel = goalViewModel(data.goalStatus);
    const gitModel = gitStatusModel(data.gitStatus);
    const subtitleTitle = `${context.compact} · ${quota.compact} · ${goalModel.detail} · ${model}${data.fastStatus ? ` · fast:${data.fastStatus}` : ''}`;
    statusShellController.update({
      contextText: context.compact,
      contextTitle: context.title,
      quotaText: quota.compact,
      quotaTitle: quota.title,
      quotaPercent: quota.percent,
      quotaWeekPercent: quota.weekPercent,
      modelText: modelLabel,
      modelTitle: `${model} · ${fastView.fastText} speed`,
      ...fastView,
      subtitleTitle,
      goalVisible: goalModel.visible,
      goalText: goalModel.compact,
      goalTitle: goalModel.visible ? `Goal status · ${goalModel.detail}` : '',
      goalTone: goalModel.tone,
      gitText: gitModel.text,
      gitTitle: gitModel.title,
      gitState: gitModel.state,
    });
    if ($('detailsContext')) $('detailsContext').textContent = context.detail;
    if ($('detailsQuota')) $('detailsQuota').textContent = quota.detail;
    if ($('detailsGoal')) $('detailsGoal').textContent = goalModel.detail;
    if (!goalModel.visible) clearGoalDetails();
    document.documentElement.classList.toggle('has-goal-status', goalModel.visible);
    syncStructuredInteraction(data.interaction || null);
    updateFolderLabel(data);
    if ($('detailsOwner')) $('detailsOwner').textContent = ownerLabel;
    if ($('detailsModel')) $('detailsModel').textContent = `${modelLabel} · ${fastView.fastText}`;
    if ($('detailsGit')) $('detailsGit').textContent = gitModel.text;
    if ($('detailsBackend')) $('detailsBackend').textContent = sessionBackendLabel(data.backend);
    if (data.agentState === 'starting' && outputMode === 'compact' && !lastCompactCapture) {
      output.classList.add('compact-blocks');
      const updatePending = data.codexUpdateStatus === 'pending';
      conversationStore.beginStarting(updatePending);
      output.replaceChildren();
    }
    const updateNotice = `${data.session || ''}:${data.codexUpdateStatus || ''}`;
    if (data.codexUpdateStatus === 'failed' && updateNotice !== lastCodexUpdateNotice) {
      lastCodexUpdateNotice = updateNotice;
      setError('Codex auto-update failed, so Faryo continued with the installed version.', { timeoutMs: 10000 });
    }
    if (data.launchError) {
      launchErrorVisible = true;
      setError(String(data.launchError), { timeoutMs: 0 });
    } else if (launchErrorVisible) {
      launchErrorVisible = false;
      preserveErrorUntil = 0;
      setError('');
    }
    agentRunning = Boolean(data.agentRunning);
    queuedSendNowAvailable = Boolean(data.queuedSendNowAvailable);
    updatePetControl();
  }

  function switchSession(route, session) {
    const next = new URL(routeBase === `/${route}` ? location.href : `/${route}/`, location.origin);
    next.searchParams.set('session', session);
    if (routeBase !== `/${route}`) return location.assign(`${next.pathname}${next.search}${location.hash}`);
    persistPromptDraft();
    persistPendingSubmission();
    conversationStore.switchSession(session);
    selectedSession = session;
    statusShellController.update({ fastVisible: false, fastDisabled: true });
    syncStructuredInteraction(null);
    clearGoalDetails();
    beginInitialLatestScroll();
    closeSurfacePanels({ restoreFocus: false });
    restorePromptDraft();
    autosize();
    updateSendVisibility();
    history.replaceState(null, '', `${next.pathname}${next.search}${location.hash}`);
    sessionMenu.classList.add('hidden');
    resetConversationHistory();
    resetRefreshState();
    clearMarkdownRenderCache();
    closeEventStream();
    lastCaptureSignature = '';
    lastCompactCapture = lastFullCapture = null;
    renderModeLoading(outputMode);
    refreshStatus({ silent: true }).catch(handleBackgroundError);
    refreshCapture(currentCaptureLines(), { silent: true }).catch(handleBackgroundError);
    if (outputMode === 'compact') startEventStream();
  }

  function cachedWorkbench() {
    try {
      const cached = JSON.parse(sessionStorage.getItem(WORKBENCH_CACHE_KEY) || 'null');
      return cached?.data && Date.now() - Number(cached.storedAt || 0) <= WORKBENCH_CACHE_MS ? cached.data : null;
    } catch (_err) { return null; }
  }

  function renderSessionMenu(data, needsRefresh) {
    const list = (data?.sessions || []).filter((item) => item.active && item.tmuxSession).map((item) => { const route = String(item.route || '').trim(), session = String(item.tmuxSession), where = item.cwdLabel || compactPathLabel(item.cwd || ''), meta = escapeHtml(`${item.routeLabel || route || 'Owner'}${where ? ` · ${where}` : ''}${item.updatedAt ? ` · ${item.updatedAt}` : ''}`), active = routeBase === `/${route}` && session === selectedSession; return `<button type="button" class="${active ? 'active' : ''}" data-route="${escapeHtml(route)}" data-session="${escapeHtml(session)}"><span><strong>${escapeHtml(String(item.title || item.id || session))}</strong><small>${meta}</small></span><em>${active ? 'Now' : 'Open'}</em></button>`; }).join('');
    const current = routeBase && selectedSession ? `<button type="button" class="active" data-route="${escapeHtml(routeBase.replace('/', ''))}" data-session="${escapeHtml(selectedSession)}"><span><strong>${escapeHtml($('topicText').textContent || selectedSession)}</strong><small>${escapeHtml($('draftState').title || $('draftState').textContent || 'Current session')}</small></span><em>Now</em></button>` : '';
    const refresh = needsRefresh ? '<button type="button" data-refresh="workbench"><span><strong>Refresh</strong><small>Load latest gateway sessions</small></span><em>↻</em></button>' : '';
    sessionMenu.innerHTML = `<div class="surface-panel-heading"><div><span class="surface-panel-eyebrow">Workspace</span><strong id="sessionPanelTitle">Running sessions</strong></div><button class="panel-close" type="button" data-close-panel aria-label="Close running sessions">×</button></div>${list || current || '<div class="session-empty">No cached sessions</div>'}${refresh}`;
  }

  async function refreshSessionMenu() {
    const headers = ownerApiClient.ownerHeaders();
    const res = await fetch('/api/workbench', { headers, cache: 'no-store' }), data = await res.json(); if (!res.ok || data.ok === false) throw new Error(data.error || 'Failed to load sessions');
    try { sessionStorage.setItem(WORKBENCH_CACHE_KEY, JSON.stringify({ storedAt: Date.now(), data })); } catch (_err) {}
    renderSessionMenu(data, false);
  }

  function escapeHtml(text) {
    return text.replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[ch]));
  }

  function decorateMetaLines(renderedHtml, text) {
    const htmlLines = renderedHtml.split('\n');
    const textLines = text.split('\n');
    if (htmlLines.length !== textLines.length) return renderedHtml;
    return htmlLines.map((line, index) => {
      const plainLine = textLines[index];
      if (!metaLineRe.test(plainLine)) return line;
      return `<span class="agent-meta-line">${line || ' '}</span>`;
    }).join('\n');
  }

  function renderPlanBlock(text) {
    const titleRe = /^(?:[-*•]\s*)?(?:Updated Plan|Plan updated)\b/i;
    const itemRe = /^(?:\[(?:x|X|✓|✔)\]|\[\s?\]|[✔✓☑□☐-]|\d+\.)\s*/;
    const items = [];
    for (const raw of text.split('\n')) {
      const line = raw.trim().replace(/^[│|└├↳]\s*/, '').trim();
      if (!line || titleRe.test(line)) continue;
      const item = line
        .replace(/^\[(?:x|X|✓|✔)\]\s*/, '✓ ')
        .replace(/^\[\s?\]\s*/, '□ ')
        .replace(/^[✔✓☑]\s*/, '✓ ')
        .replace(/^[□☐]\s*/, '□ ');
      if (itemRe.test(line) || !items.length) items.push(item);
      else items[items.length - 1] = `${items[items.length - 1]} ${item}`;
    }
    if (!items.length) return '<section class="compact-process-line">📝 Plan updated</section>';
    return `<section class="compact-block plan"><div class="compact-plan-title">Plan updated</div>${items.length ? `<div class="compact-plan-list">${items.map((item) => `<div class="compact-plan-item">${escapeHtml(item)}</div>`).join('')}</div>` : ''}</section>`;
  }

  function clearMarkdownRenderCache() {
    markdownHtmlCache.clear();
  }

  function renderMarkdownSegment(source, mode) {
    const text = String(source || '');
    const cacheable = typeof stableBlocks.fingerprint === 'function';
    const cacheKey = cacheable
      ? `${markdownRenderRevision}:${mode}:${text.length}:${stableBlocks.fingerprint(text)}`
      : '';
    const cached = cacheKey ? markdownHtmlCache.get(cacheKey) : null;
    if (cached?.source === text) {
      markdownHtmlCache.delete(cacheKey);
      markdownHtmlCache.set(cacheKey, cached);
      return cached.html;
    }
    const html = markdownRenderer.render(text, {
      localFileHref: localFileTarget,
      localImageHref: localImageTarget,
    }, { mode });
    if (cacheKey) {
      markdownHtmlCache.set(cacheKey, { source: text, html });
      while (markdownHtmlCache.size > 256) {
        markdownHtmlCache.delete(markdownHtmlCache.keys().next().value);
      }
    }
    return html;
  }

  function parsedInternalAnnotations(value) {
    if (typeof internalAnnotations.parse === 'function') return internalAnnotations.parse(value);
    return { body: String(value || ''), citations: [] };
  }

  function copyableOutputText(value) {
    if (typeof internalAnnotations.strip === 'function') return internalAnnotations.strip(value);
    return String(value || '');
  }

  function renderMemoryReferences(citations) {
    const groups = Array.isArray(citations) ? citations : [];
    if (!groups.length) return '';
    const entries = groups.flatMap((group) => Array.isArray(group?.entries) ? group.entries : []);
    const count = entries.length;
    const notes = entries.map((entry) => String(entry?.note || '').trim()).filter(Boolean);
    const items = notes.length
      ? `<ul>${notes.map((note) => `<li>${escapeHtml(note)}</li>`).join('')}</ul>`
      : '<p>Saved context was used for this answer.</p>';
    return `<details class="memory-reference-card"><summary>Memory references${count ? ` · ${count}` : ''}</summary><div class="memory-reference-body">${items}</div></details>`;
  }

  const imagePathRe = /\.(?:jpe?g|png|webp|gif|heic|heif)$/i;
  const filePathRe = /\.(?:md|txt|json|csv|rtf|pdf|docx?|xlsx?|pptx?|odt|odp|ods|bash|c|cc|cfg|cpp|css|go|h|hpp|html|ini|java|js|jsx|lean|log|py|rs|sh|sql|tex|toml|ts|tsx|xml|ya?ml|zsh)$/i;

  function cleanTypedPath(value, suffixRe) {
    let text = String(value || '').trim();
    if ((text.startsWith('<') && text.endsWith('>')) || (/^(['"`]).*\1$/.test(text))) text = text.slice(1, -1).trim();
    text = text.replace(/[),.;]+$/g, '');
    return suffixRe.test(text) ? text : '';
  }

  function renderImageLine(line) {
    const match = String(line || '').match(/^\s*Image\s*:\s*(.+?)\s*$/i);
    const path = match && cleanTypedPath(match[1], imagePathRe);
    if (!path) return '';
    const target = localImageTarget(path);
    const src = typeof target === 'string' ? target : '';
    const fetchSrc = typeof target === 'object' ? target.fetchHref : '';
    const label = path.split(/[\\/]/).pop() || 'image';
    const sourceAttributes = src
      ? ` src="${escapeHtml(src)}"`
      : ` data-faryo-fetch-src="${escapeHtml(fetchSrc)}" aria-busy="true"`;
    return `<button class="chat-image-thumb" type="button" data-label="${escapeHtml(label)}"><img class="chat-image"${sourceAttributes} alt="${escapeHtml(label)}" loading="lazy"></button>`;
  }

  const protectedImageUrls = new Map();

  function releaseDetachedProtectedImages() {
    for (const [image, url] of protectedImageUrls) {
      if (image.isConnected) continue;
      URL.revokeObjectURL(url);
      protectedImageUrls.delete(image);
    }
  }

  async function fetchProtectedResource(href) {
    const target = new URL(String(href || ''), location.href);
    const localApiPaths = new Set([
      `${routeBase}/api/local-image`,
      `${routeBase}/api/local-file`,
      `${routeBase}/api/local-file/view`,
    ]);
    if (target.origin !== location.origin || !localApiPaths.has(target.pathname)) {
      throw new Error('Local resource URL was rejected');
    }
    target.searchParams.delete('token');
    const headers = ownerApiClient.ownerHeaders();
    const response = await fetch(target.pathname + target.search, {
      headers,
      cache: 'no-store',
      credentials: 'same-origin',
    });
    if (!response.ok) {
      const error = new Error(`Local resource failed ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return response;
  }

  async function hydrateProtectedImages(root) {
    releaseDetachedProtectedImages();
    const images = [...(root?.querySelectorAll('img[data-faryo-fetch-src]') || [])];
    await Promise.all(images.map(async (image) => {
      if (image.dataset.faryoFetching === '1') return;
      image.dataset.faryoFetching = '1';
      try {
        const response = await fetchProtectedResource(image.dataset.faryoFetchSrc);
        const blob = await response.blob();
        if (!image.isConnected) return;
        const previous = protectedImageUrls.get(image);
        if (previous) URL.revokeObjectURL(previous);
        const url = URL.createObjectURL(blob);
        protectedImageUrls.set(image, url);
        image.src = url;
        image.removeAttribute('data-faryo-fetch-src');
        image.removeAttribute('aria-busy');
      } catch (_error) {
        image.removeAttribute('aria-busy');
        image.classList.add('resource-load-error');
      } finally {
        delete image.dataset.faryoFetching;
      }
    }));
  }

  async function openProtectedResource(link) {
    const popup = window.open('about:blank', '_blank');
    if (popup) {
      popup.opener = null;
      popup.document.title = 'Loading local file';
    }
    try {
      const response = await fetchProtectedResource(link.dataset.faryoFetchHref);
      const url = URL.createObjectURL(await response.blob());
      if (popup) popup.location.replace(url);
      else {
        const fallback = document.createElement('a');
        fallback.href = url;
        fallback.target = '_blank';
        fallback.rel = 'noopener noreferrer';
        fallback.click();
      }
      window.setTimeout(() => URL.revokeObjectURL(url), 60000);
    } catch (error) {
      popup?.close();
      setError(userErrorMessage(error));
    }
  }

  function showImageLightbox(src, label) {
    if (!src) return;
    let box = document.getElementById('imageLightbox');
    if (!box) {
      box = document.createElement('div');
      box.id = 'imageLightbox';
      box.className = 'image-lightbox hidden';
      box.innerHTML = '<img alt=""><div class="image-lightbox-caption"></div>';
      box.addEventListener('click', () => box.classList.add('hidden'));
      document.body.appendChild(box);
    }
    box.querySelector('img').src = src;
    box.querySelector('img').alt = label || 'Image preview';
    box.querySelector('.image-lightbox-caption').textContent = label || '';
    box.classList.remove('hidden');
  }

  function renderFileLine(line) {
    const match = String(line || '').match(/^\s*(?:(File|Attachment)\s*:\s*)?(.+?)\s*$/i);
    const path = match && cleanTypedPath(match[2], filePathRe);
    if (!path || (!match[1] && !/^(?:\/|~\/|\.{1,2}\/|[\w.-]+\/|[\w.-]+\.[A-Za-z0-9]{1,8}$)/.test(path))) return '';
    const target = localFileTarget(path);
    const href = typeof target === 'string' ? target : target.href;
    const fetchHref = typeof target === 'object' ? target.fetchHref : '';
    const label = path.split(/[\\/]/).pop() || 'file';
    const fetchAttribute = fetchHref ? ` data-faryo-fetch-href="${escapeHtml(fetchHref)}"` : '';
    return `<a class="file-link" href="${escapeHtml(href)}"${fetchAttribute}>File ${escapeHtml(label)}</a>`;
  }

  function renderTextWithFiles(text, renderOptions = {}) {
    const parsed = parsedInternalAnnotations(text);
    const originalLines = String(parsed.body || '').split('\n');
    let renderedText = '';
    if (typeof markdownRenderer.render === 'function' && markdownRenderer.ready?.()) {
      const rendered = [];
      let markdownLines = [];
      let fenceChar = '';
      const flushMarkdown = () => {
        if (!markdownLines.length) return;
        const mode = renderOptions.mode === 'streaming' ? 'streaming' : 'settled';
        rendered.push(`<div class="markdown-body">${renderMarkdownSegment(markdownLines.join('\n'), mode)}</div>`);
        markdownLines = [];
      };
      originalLines.forEach((line) => {
        const fenceMatch = line.trimStart().match(/^(`{3,}|~{3,})/);
        const insideFence = Boolean(fenceChar);
        const special = !insideFence && !fenceMatch && (renderImageLine(line) || renderFileLine(line));
        if (special) {
          flushMarkdown();
          rendered.push(special);
        } else {
          markdownLines.push(line);
        }
        if (fenceMatch) {
          const char = fenceMatch[1][0];
          if (!fenceChar) fenceChar = char;
          else if (fenceChar === char) fenceChar = '';
        }
      });
      flushMarkdown();
      renderedText = rendered.join('');
    } else {
      renderedText = originalLines.map((line) => renderImageLine(line) || renderFileLine(line) || escapeHtml(line)).join('\n');
    }
    return renderedText + renderMemoryReferences(parsed.citations);
  }

  function renderTextWithFilesSafely(text, renderOptions = {}) {
    try {
      return renderTextWithFiles(text, renderOptions);
    } catch (_error) {
      const parsed = parsedInternalAnnotations(text);
      return `<div class="rich-render-fallback" role="status"><strong>Rich text preview unavailable</strong><span>Showing safe plain text for this message.</span><pre>${escapeHtml(parsed.body || '')}</pre></div>${renderMemoryReferences(parsed.citations)}`;
    }
  }

  function compactRulesForCapture(capture) {
    const source = String(capture?.agentSource || capture?.source || '').toLowerCase();
    const rules = source === 'codex-cli' ? codexCompactRules : runtimeCompactRules;
    return {
      userPromptRe: rules.userPromptRe || runtimeCompactRules.userPromptRe,
      compactBlocks: rules.compactBlocks || runtimeCompactRules.compactBlocks,
      processSummaryCard: rules.processSummaryCard || runtimeCompactRules.processSummaryCard,
      approvalPendingRe: rules.approvalPendingRe || runtimeCompactRules.approvalPendingRe,
    };
  }

  function appendActivityDetailField(container, label, value, className = '') {
    const text = String(value || '');
    if (!text) return;
    const section = document.createElement('section');
    section.className = `activity-detail-field${className ? ` ${className}` : ''}`;
    const heading = document.createElement('strong');
    heading.textContent = label;
    const body = document.createElement('pre');
    body.textContent = text;
    section.append(heading, body);
    container.appendChild(section);
  }

  function renderActivityDetailBody(container, detail) {
    container.replaceChildren();
    container.dataset.state = 'ready';
    const meta = document.createElement('div');
    meta.className = `activity-detail-meta activity-status-${String(detail?.status || 'completed')}`;
    const values = [String(detail?.status || '')];
    if (Number.isFinite(Number(detail?.exitCode))) values.push(`exit ${Number(detail.exitCode)}`);
    if (Number.isFinite(Number(detail?.durationMs))) values.push(`${Number(detail.durationMs)} ms`);
    meta.textContent = values.filter(Boolean).join(' · ');
    container.appendChild(meta);
    if (detail?.type === 'command') {
      appendActivityDetailField(container, 'Command', detail.command);
      appendActivityDetailField(container, 'Working directory', detail.cwd);
      appendActivityDetailField(container, 'Output', detail.output, 'activity-detail-output');
    } else if (detail?.type === 'file_change') {
      for (const [index, change] of (detail.changes || []).entries()) {
        const changeNode = document.createElement('details');
        changeNode.className = 'activity-file-change';
        const summary = document.createElement('summary');
        summary.textContent = `${String(change?.kind || 'change')} · ${String(change?.path || `file ${index + 1}`)}`;
        changeNode.appendChild(summary);
        if (change?.diff) {
          const diff = document.createElement('pre');
          diff.textContent = String(change.diff);
          changeNode.appendChild(diff);
        }
        container.appendChild(changeNode);
      }
    } else if (detail?.type === 'search') {
      appendActivityDetailField(container, 'Query', detail.query);
      appendActivityDetailField(container, 'Results', detail.results);
    } else {
      appendActivityDetailField(container, 'Arguments', detail?.arguments);
      appendActivityDetailField(container, 'Result', detail?.result);
      appendActivityDetailField(container, 'Error', detail?.error, 'activity-detail-error');
    }
    if (detail?.truncated) {
      const note = document.createElement('p');
      note.className = 'activity-detail-note';
      note.textContent = 'Large detail was bounded; the beginning and end are shown.';
      container.appendChild(note);
    }
    if (container.children.length === 1) {
      const note = document.createElement('p');
      note.className = 'activity-detail-note';
      note.textContent = 'No additional detail was returned by Codex.';
      container.appendChild(note);
    }
  }

  async function loadActivityDetail(container, item) {
    if (!container || container.dataset.state === 'loading' || container.dataset.state === 'ready') return;
    const session = selectedSession;
    const itemId = String(item?.id || '');
    const key = `${session}:${itemId}`;
    const cached = activityDetailCache.get(key);
    if (cached) {
      renderActivityDetailBody(container, cached);
      return;
    }
    container.dataset.state = 'loading';
    container.textContent = 'Loading detail…';
    try {
      const payload = await api(apiPath(`/api/activity-detail?item=${encodeURIComponent(itemId)}`));
      if (!container.isConnected || selectedSession !== session) return;
      const detail = payload?.detail;
      if (!detail || typeof detail !== 'object') throw new Error('Activity detail is unavailable');
      activityDetailCache.set(key, detail);
      while (activityDetailCache.size > 64) activityDetailCache.delete(activityDetailCache.keys().next().value);
      renderActivityDetailBody(container, detail);
    } catch (error) {
      if (!container.isConnected || selectedSession !== session) return;
      container.dataset.state = 'error';
      container.textContent = userErrorMessage(error) || 'Activity detail is unavailable';
    }
  }

  function buildActivityList(model, openItems = new Set()) {
    const list = document.createElement('div');
    list.className = 'compact-activity-list';
    for (const [index, item] of (model.items || []).entries()) {
      const text = String(item?.text || '').trim();
      if (!text) continue;
      const status = activityStatus(item);
      if (activityItemCollapsible(item)) {
        const detail = document.createElement('details');
        detail.className = `compact-activity-item compact-activity-item-long activity-status-${status}`;
        detail.dataset.activityItemId = String(item?.id || `activity-${index}`);
        const detailSummary = document.createElement('summary');
        detailSummary.textContent = activityItemSummary(item, index);
        const body = document.createElement(item?.activity?.detailAvailable ? 'div' : 'pre');
        if (item?.activity?.detailAvailable) {
          body.className = 'activity-detail-body';
          body.textContent = 'Open to load detail';
          body.dataset.state = 'idle';
          detail.addEventListener('toggle', () => {
            if (detail.open) void loadActivityDetail(body, item);
          });
        } else {
          body.textContent = text;
        }
        detail.append(detailSummary, body);
        if (openItems.has(detail.dataset.activityItemId)) {
          detail.open = true;
          if (item?.activity?.detailAvailable) void loadActivityDetail(body, item);
        }
        list.appendChild(detail);
      } else {
        const line = document.createElement('div');
        line.className = `compact-activity-item activity-status-${status}`;
        line.textContent = item?.activity ? activityItemSummary(item, index) : text;
        list.appendChild(line);
      }
    }
    return list;
  }

  function materializeActivityList(node) {
    if (!node.open || node.querySelector(':scope > .compact-activity-list')) return;
    const model = node.__faryoActivityModel;
    if (!model) return;
    node.appendChild(buildActivityList(model, node.__faryoActivityOpenItems || new Set()));
  }

  function renderActivityCard(node, model) {
    if (!node.__faryoActivityToggleBound) {
      node.addEventListener('toggle', () => materializeActivityList(node));
      node.__faryoActivityToggleBound = true;
    }
    if (node.dataset.faryoActivitySignature === model.signature) {
      materializeActivityList(node);
      return;
    }
    const wasOpen = Boolean(node.open);
    const openItems = new Set(
      [...node.querySelectorAll(':scope > .compact-activity-list > details[open][data-activity-item-id]')]
        .map((item) => item.dataset.activityItemId),
    );
    const summary = document.createElement('summary');
    summary.className = 'compact-activity-title';
    const label = document.createElement('span');
    label.textContent = String(model.summary || 'Activity');
    label.title = label.textContent;
    summary.appendChild(label);

    node.__faryoActivityModel = model;
    node.__faryoActivityOpenItems = openItems;
    node.replaceChildren(summary);
    // Activity is always opt-in: running, waiting and failed batches retain
    // their status in the summary but never open without a user gesture.
    node.open = wasOpen;
    materializeActivityList(node);
    delete node.dataset.faryoActivityAttention;
    node.dataset.faryoActivitySignature = model.signature;
  }

  function renderCommandRow(node, model) {
    if (node.dataset.faryoCommandSignature === model.signature) return;
    const icon = document.createElement('span');
    icon.className = 'command-timeline-icon';
    icon.textContent = '/';
    const content = document.createElement('span');
    content.className = 'command-timeline-content';
    const label = document.createElement('strong');
    label.textContent = String(model.command?.label || model.command?.name || 'Codex command');
    const summary = document.createElement('span');
    summary.textContent = String(model.text || '');
    content.append(label, summary);
    const state = document.createElement('span');
    state.className = 'command-timeline-state';
    state.textContent = String(model.command?.status || 'completed');
    node.className = `command-timeline-row command-status-${String(model.command?.status || 'completed')}`;
    node.replaceChildren(icon, content, state);
    node.dataset.faryoCommandSignature = model.signature;
  }

  function renderCompactOutput(text, rules, renderOptions = {}) {
    const mode = renderOptions.mode === 'streaming' ? 'streaming' : 'settled';
    const structuredItems = Array.isArray(renderOptions.messageBlocks)
      ? renderOptions.messageBlocks.flatMap((item) => {
        const kind = String(item?.kind || '');
        const value = String(item?.text || '').trim();
        if (!['user', 'output', 'process', 'plan'].includes(kind) || !value) return [];
        return [{
          kind,
          text: value,
          turnKey: String(item.turnKey || ''),
          segmentKey: String(item.segmentKey || ''),
          keyHint: item.id ? `appserver:${item.id}` : '',
          mutable: item.final === false,
          questionKey: String(item.questionKey || ''),
          id: String(item.id || ''),
          final: item.final !== false,
          activity: item.activity && typeof item.activity === 'object' ? { ...item.activity } : null,
        }];
      })
      : [];
    const structuredBlocks = structuredItems.length
      ? groupActivityBlocks(mergeCommandEvents(structuredItems, renderOptions.commandEvents))
      : [];
    const rawBlocks = structuredBlocks.length
      ? structuredBlocks
      : mergeCommandEvents(rules.compactBlocks(text), renderOptions.commandEvents);
    if (!rawBlocks.length) rawBlocks.push({ kind: 'output', text: 'No output yet' });
    const streamKey = mode === 'streaming' ? String(renderOptions.streamKey || '') : '';
    if (streamKey && !structuredBlocks.length) {
      for (let index = rawBlocks.length - 1; index >= 0; index -= 1) {
        if (String(rawBlocks[index]?.kind || '') !== 'output') continue;
        rawBlocks[index] = { ...rawBlocks[index], keyHint: streamKey, mutable: true };
        break;
      }
    }
    if (renderOptions.appServerStreaming) {
      rawBlocks.push({
        kind: 'status',
        text: renderOptions.streamItemId ? 'Receiving response…' : 'Codex is working…',
        keyHint: 'appserver-stream-progress',
        mutable: true,
        streamProgress: true,
      });
    }
    const models = typeof stableBlocks.plan === 'function'
      ? stableBlocks.plan(rawBlocks, { mode, revision: markdownRenderRevision, tailCount: 2 })
      : rawBlocks.map((block, index) => ({
        ...block,
        kind: String(block.kind || 'output'),
        text: String(block.text ?? ''),
        key: `fallback-${index}`,
        signature: `fallback-${index}-${String(block.text ?? '')}`,
        stable: false,
      }));
    let richBlockTotal = 0;
    for (const model of models) {
      model.copySource = model.kind === 'output'
        ? copyableOutputText(model.text)
        : model.kind === 'user'
          ? String(model.text || '').replace(rules.userPromptRe, '').trim()
          : '';
      model.renderSource = ['output', 'user'].includes(model.kind) ? copyableOutputText(model.text) : '';
      if (['output', 'user'].includes(model.kind)) model.richIndex = richBlockTotal++;
    }
    const createNode = (model) => {
      if (model.kind === 'activity') {
        const node = document.createElement('details');
        node.className = 'compact-activity-card';
        return node;
      }
      if (model.kind === 'command') {
        const node = document.createElement('section');
        node.className = 'command-timeline-row';
        return node;
      }
      if (model.kind === 'process') {
        const node = document.createElement('section');
        node.className = 'compact-process-line';
        node.textContent = rules.processSummaryCard(model.text);
        return node;
      }
      if (model.kind === 'status') {
        const node = document.createElement('section');
        node.className = `compact-status-line${model.streamProgress ? ' appserver-stream-progress' : ''}`;
        if (model.streamProgress) {
          node.setAttribute('role', 'status');
          node.setAttribute('aria-live', 'polite');
        }
        node.textContent = model.text;
        return node;
      }
      if (model.kind === 'plan') {
        const template = document.createElement('template');
        template.innerHTML = renderPlanBlock(model.text);
        return template.content.firstElementChild;
      }
      const node = document.createElement('section');
      const kindClass = /^[A-Za-z0-9_-]+$/.test(model.kind) ? model.kind : 'output';
      node.className = `compact-block ${kindClass}`;
      return node;
    };
    let metrics;
    if (typeof stableBlocks.reconcile === 'function') {
      metrics = stableBlocks.reconcile(output, models, createNode);
    } else {
      const fragment = document.createDocumentFragment();
      for (const model of models) fragment.appendChild(createNode(model));
      output.replaceChildren(fragment);
      metrics = { created: models.length, reused: 0, removed: 0, stable: 0 };
    }
    const loadedQuestions = historyController.initialized ? loadedHistoryTurns() : [];
    let loadedQuestionIndex = 0;
    copyFidelity?.beginRender();
    models.forEach((model, index) => {
      const node = output.children[index];
      if (!node) return;
      if (model.kind === 'activity') renderActivityCard(node, model);
      if (model.kind === 'command') renderCommandRow(node, model);
      if (['output', 'user'].includes(model.kind)) {
        const descriptor = {
          signature: model.signature,
          kind: model.kind,
          text: model.text,
          copySource: model.copySource,
          renderSource: model.renderSource,
          renderOptions,
        };
        if (richBlockController) {
          richBlockController.prepare(node, descriptor, {
            eager: shouldRenderEagerly(model.richIndex, richBlockTotal),
          });
        } else {
          node.innerHTML = renderTextWithFilesSafely(model.text, renderOptions);
          node.dataset.faryoRichState = 'rendered';
        }
        if (node.dataset.faryoRichState === 'rendered') {
          copyFidelity?.bindBlock(node, { source: model.copySource, renderSource: model.renderSource, kind: model.kind });
        }
        node.dataset.faryoCopyBound = copyFidelity ? 'true' : 'false';
      }
      if (model.kind === 'user') {
        const historyTurn = loadedQuestions[loadedQuestionIndex++];
        const questionKey = String(model.questionKey || historyTurn?.key || '');
        if (questionKey) node.dataset.faryoQuestionKey = questionKey;
        else delete node.dataset.faryoQuestionKey;
        node.dataset.faryoQuestionPreview = typeof questionNavigatorApi.previewText === 'function'
          ? questionNavigatorApi.previewText(model.text, 88)
          : String(model.text || '').replace(/^\s*›\s*/u, '').trim().slice(0, 88);
      } else {
        delete node.dataset.faryoQuestionKey;
        delete node.dataset.faryoQuestionPreview;
      }
    });
    richBlockController?.prune();
    const blocks = output.querySelectorAll('.compact-block.output');
    blocks.forEach((block, index) => {
      const existing = block.querySelector(':scope > .copy-output-block');
      if (index !== blocks.length - 1) {
        existing?.remove();
        return;
      }
      if (existing) return;
      const button = document.createElement('button');
      button.className = 'copy-output-block';
      button.type = 'button';
      button.setAttribute('aria-label', 'Copy this output');
      button.title = 'Copy this output';
      button.textContent = '⧉';
      block.appendChild(button);
    });
    output.dataset.compactCreated = String(metrics.created);
    output.dataset.compactReused = String(metrics.reused);
    output.dataset.compactStable = String(metrics.stable);
    output.dataset.richRendered = String(richBlockController?.renderedCount ?? richBlockTotal);
    output.dataset.richDeferred = String(richBlockController?.pendingCount ?? 0);
  }

  function renderPlainOutput(text, rules) {
    const parsed = parsedInternalAnnotations(text);
    const value = parsed.body || 'No output yet';
    let inUserInput = false;
    output.innerHTML = value.split('\n').map((line) => {
      const rendered = escapeHtml(line);
      const imageLine = renderImageLine(line);
      if (imageLine) return imageLine;
      const fileLine = renderFileLine(line);
      if (fileLine) return fileLine;
      if (rules.userPromptRe.test(line)) inUserInput = true;
      else if (!line.trim()) inUserInput = false;
      if (metaLineRe.test(line)) return `<span class="agent-meta-line">${rendered || ' '}</span>`;
      return inUserInput ? `<span class="user-input-line">${rendered || ' '}</span>` : rendered;
    }).join('\n');
    if (parsed.citations.length) output.insertAdjacentHTML('beforeend', `\n${renderMemoryReferences(parsed.citations)}`);
  }

  function renderOutput(capture, renderOptions = {}) {
    if (!renderOptions.conversationCommitted && !conversationStore.commitCapture(capture)) return false;
    const liveStateSnapshot = liveTerminalState();
    if (outputMode === 'compact') lastCompactCapture = capture;
    else lastFullCapture = capture;
    scheduleConversationHistoryRefresh(capture);
    capture = mergedConversationCapture(capture);
    const isStructured = structuredCapture(capture);
    const sourceText = String(capture.text || '');
    const emptyStructured = isStructured
      && !sourceText.trim()
      && !capture.streaming
      && !(Array.isArray(capture.commandEvents) && capture.commandEvents.length);
    const text = emptyStructured
      ? 'No messages yet. Ask Codex to start this conversation.'
      : sourceText || 'No output yet';
    const rules = compactRulesForCapture(capture);
    output.dataset.captureSource = String(capture.captureSource || '');
    output.dataset.agentSource = String(capture.agentSource || '');
    output.dataset.structuredEmpty = emptyStructured ? 'true' : 'false';
    output.dataset.streaming = capture.streaming ? 'true' : 'false';
    output.dataset.streamItemId = String(capture.streamItemId || '');
    if ($('detailsSource')) $('detailsSource').textContent = String(capture.captureSource || capture.source || 'unknown');
    if ($('detailsBackend') && capture.backend) {
      $('detailsBackend').textContent = sessionBackendLabel(capture.backend);
    }
    syncStructuredInteraction(capture.interaction || null);
    updateStatusLineAutoExpand();
    output.classList.toggle('compact-blocks', outputMode === 'compact');
    if (outputMode === 'compact') {
      if (emptyStructured) {
        richBlockController?.clear();
        output.replaceChildren();
        delete output.dataset.renderFallback;
      } else {
        try {
          renderCompactOutput(text, rules, {
            mode: isStructured && !capture.streaming ? 'settled' : 'streaming',
            messageBlocks: capture.messageBlocks,
            commandEvents: capture.commandEvents,
            appServerStreaming: capture.captureSource === 'codex-app-server' && Boolean(capture.streaming),
            streamItemId: String(capture.streamItemId || ''),
            streamKey: capture.streamItemId
              ? `${capture.sessionId || ''}:${capture.streamTurnId || ''}:${capture.streamItemId}`
              : '',
          });
          delete output.dataset.renderFallback;
        } catch (_error) {
          conversationStore.markRenderError();
          const parsed = parsedInternalAnnotations(text);
          output.dataset.renderFallback = 'true';
          const livePanel = output.querySelector('[data-faryo-transient="live"]');
          for (const child of Array.from(output.children)) {
            if (child !== livePanel) child.remove();
          }
          const template = document.createElement('template');
          template.innerHTML = `<section class="compact-block output"><pre class="capture-render-fallback">${escapeHtml(parsed.body || '')}</pre>${renderMemoryReferences(parsed.citations)}</section>`;
          output.insertBefore(template.content, livePanel || null);
        }
      }
    }
    else {
      richBlockController?.clear();
      if (capture.html && !parsedInternalAnnotations(text).citations.length) output.innerHTML = decorateMetaLines(capture.html, text);
      else renderPlainOutput(text, rules);
    }
    if (outputMode === 'compact') {
      syncLiveTerminal(capture.agentRunning && capture.liveText ? capture.liveText : '', liveStateSnapshot);
    }
    const indexedQuestions = isStructured && historyController.initialized
      ? historyController.questions
      : null;
    questionNavigatorController?.sync(outputMode === 'compact', indexedQuestions);
    void hydrateProtectedImages(output);
    return true;
  }

  function resetRefreshState() {
    cancelActiveRefreshes();
    pendingDeferredCapture = null;
    questionNavigatorController?.reset();
  }

  function cancelActiveRefreshes() {
    captureController.cancelRefresh();
    statusController.cancel();
  }

  function handlePageShow(event) {
    if (!seenInitialPageShow && !event.persisted) {
      seenInitialPageShow = true;
      return;
    }
    seenInitialPageShow = true;
    resumeLiveConnection();
  }

  function refreshVisibleNow() {
    if (document.hidden) return;
    if (headerStatusVisible()) refreshStatus({ silent: true }).catch(handleBackgroundError);
    refreshCapture(currentCaptureLines(), { silent: true }).catch(handleBackgroundError);
  }

  function resumeLiveConnection() {
    if (document.hidden) return;
    const now = Date.now();
    if (now - lastLiveWakeAt < 750) return;
    lastLiveWakeAt = now;
    refreshVisibleNow();
    if (outputMode === 'compact') startEventStream();
    else setFullRefresh(!fullLocked);
  }

  function currentCaptureLines() { return outputMode === 'compact' ? COMPACT_CAPTURE_LINES : FULL_CAPTURE_LINES; }

  function renderOutputModeButton() {
    for (const compactBtn of [$('refreshBtn'), $('detailsChatBtn')]) {
      if (!compactBtn) continue;
      compactBtn.textContent = 'Chat';
      compactBtn.classList.toggle('mode-active', outputMode === 'compact');
    }
    for (const fullBtn of [$('dockFullBtn'), $('detailsRawBtn')]) {
      if (!fullBtn) continue;
      fullBtn.textContent = outputMode === 'full' && fullLocked ? 'Locked' : 'Raw';
      fullBtn.classList.toggle('mode-active', outputMode === 'full');
    }
    if (promptShell) promptShell.style.borderColor = outputMode === 'full' && fullLocked ? 'var(--accent)' : '';
  }

  function closeDockMenu() {
    if (dockMenu) dockMenu.classList.add('hidden');
    $('dockPlusBtn')?.classList.remove('open');
    $('dockPlusBtn')?.setAttribute('aria-expanded', 'false');
  }

  function toggleDockMenu() {
    const open = dockMenu.classList.toggle('hidden');
    const nextOpen = !open;
    $('dockPlusBtn')?.classList.toggle('open', nextOpen);
    $('dockPlusBtn')?.setAttribute('aria-expanded', nextOpen ? 'true' : 'false');
  }

  function renderModeLoading(mode) {
    const compact = mode === 'compact';
    conversationStore.beginLoading();
    output.classList.toggle('compact-blocks', compact);
    output.dataset.captureSource = '';
    output.dataset.agentSource = '';
    output.replaceChildren();
    questionNavigatorController?.sync(false, null);
  }

  async function setOutputMode(mode) {
    const togglingFull = mode === 'full' && outputMode === 'full';
    const returningToChat = mode === 'compact' && outputMode !== 'compact';
    const wasNearBottom = isNearBottom();
    const targetCapture = mode === 'compact' ? lastCompactCapture : lastFullCapture;
    resetRefreshState();
    if (returningToChat) persistLivePanelPreference(selectedSession, false);
    fullLocked = togglingFull ? !fullLocked : false;
    outputMode = mode;
    conversationStore.setMode(mode);
    renderOutputModeButton();
    if (targetCapture) renderOutput(targetCapture);
    else renderModeLoading(mode);
    if (wasNearBottom) scrollBottom(true);
    closeDockMenu();
    setFullRefresh(false);
    if (outputMode === 'compact') {
      startEventStream();
    } else {
      closeEventStream();
      setCaptureFallback(false);
      setLiveState(fullLocked ? 'fallback' : 'live');
    }
    if (togglingFull && fullLocked) return;
    await Promise.all([
      refreshStatus({ silent: true }),
      refreshCapture(currentCaptureLines(), { silent: true }),
    ]);
    setFullRefresh(outputMode === 'full' && !fullLocked);
  }

  function toggleClassState(selector, cls, key, force) {
    const enabled = force ?? localStorage.getItem(key) === '1';
    document.querySelector(selector).classList.toggle(cls, enabled);
    localStorage.setItem(key, enabled ? '1' : '0');
  }

  function updateStatusLineAutoExpand() {
    const on = pendingAttachments.length > 0;
    const statusLine = document.querySelector('.status-line');
    statusLine?.classList.toggle('auto-expanded', on);
    document.querySelector('footer')?.classList.toggle('auto-expanded', on);
  }

  function newInteractionRequestId() {
    return window.crypto?.randomUUID
      ? `ixr-${window.crypto.randomUUID()}`
      : `ixr-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`;
  }

  function syncStructuredInteraction(interaction) {
    interactionHost?.update(interaction || null);
    const active = Boolean(interaction && interaction.id);
    document.documentElement.classList.toggle('has-pending-interaction', active);
    if (active && interactionHost) updateStatusLineAutoExpand();
    if (active && activeSurfacePanel) closeSurfacePanels({ restoreFocus: false });
    setWorkbenchInert(active || Boolean(activeSurfacePanel));
  }

  const attachmentController = createAttachmentController({
    view: window,
    document,
    items: pendingAttachments,
    preview: attachmentPreview,
    input: attachmentInput,
    trigger: $('attachmentBtn'),
    promptInput,
    maximum: MAX_ATTACHMENTS,
    uploadConcurrency: 4,
    imageMaxEdge: IMAGE_MAX_EDGE,
    imageQuality: IMAGE_JPEG_QUALITY,
    routeBase,
    csrfHeaders: ownerApiClient.csrfHeaders,
    ownerHeaders: ownerApiClient.ownerHeaders,
    clipboardImageApi,
    setBusy,
    setError,
    userErrorMessage,
    closeDockMenu,
    onChange: () => {
      updateStatusLineAutoExpand();
      updateSendVisibility();
    },
  });
  attachmentController.connect();


  function refreshStatus(options = {}) {
    return statusController.refresh(options);
  }

  async function postAction(path, body, options = {}) {
    setBusy(true);
    setError('');
    try {
      const payload = Object.assign({ session: selectedSession }, body || {});
      const data = await api(path, { ...options, method: 'POST', body: JSON.stringify(payload) });
      return data;
    } finally {
      setBusy(false);
    }
  }

  function sendWithDeliveryRecovery(payload) {
    return composerDelivery.send(payload);
  }

  function structuredSlashCommand(value) {
    const invocation = String(value || '').trim();
    if (!invocation || /[\r\n\0]/.test(invocation)) return '';
    const command = invocation.split(/\s+/, 1)[0].toLowerCase();
    if (!/^\/[a-z][a-z-]*$/.test(command)) return '';
    return (codexCommandApi.inventory || []).some((entry) =>
      [entry.command, ...(entry.aliases || [])]
        .some((name) => String(name).toLowerCase() === command)
    ) ? invocation : '';
  }

  function commandCatalogEntry(invocation) {
    const command = String(invocation || '').trim().split(/\s+/, 1)[0].toLowerCase();
    return (codexCommandApi.inventory || []).find((entry) =>
      [entry.command, ...(entry.aliases || [])]
        .some((name) => String(name).toLowerCase() === command)
    ) || null;
  }

  function commandPendingKey(session) {
    return `faryoCommandPending:${routeBase || 'owner'}:${session || 'default'}`;
  }

  function commandRequest(command, session) {
    try {
      const stored = JSON.parse(sessionStorage.getItem(commandPendingKey(session)) || 'null');
      if (stored?.command === command && stored?.session === session && stored?.id) return stored;
    } catch (_error) {}
    const pending = { command, session, id: newInteractionRequestId() };
    try { sessionStorage.setItem(commandPendingKey(session), JSON.stringify(pending)); } catch (_error) {}
    return pending;
  }

  function clearCommandRequest(session) {
    try { sessionStorage.removeItem(commandPendingKey(session)); } catch (_error) {}
  }

  async function submitLocalCommand(command, options = {}) {
    const session = selectedSession, browserText = promptInput.value;
    const pending = commandRequest(command, session);
    const entry = commandCatalogEntry(command);
    if (agentRunning && entry && !entry.availableDuringTask) {
      clearCommandRequest(session);
      setError(`${entry.command} is disabled while the current Codex task is in progress.`);
      return false;
    }
    const risk = String(entry?.risk || entry?.behavior || '');
    const needsConfirmation = entry?.behavior === 'dangerous'
      || entry?.behavior === 'unclassified'
      || ['destructive', 'ends session', 'account', 'interrupts work', 'changes thread'].includes(risk);
    let confirmed = false;
    if (needsConfirmation) {
      document.documentElement.classList.add('has-pending-interaction');
      setWorkbenchInert(true);
      confirmed = await interactionHost.confirmCommand({
        command,
        description: entry?.description || 'This command has not been classified by this Faryo version.',
        risk: risk || 'unclassified',
      });
      document.documentElement.classList.remove('has-pending-interaction');
      setWorkbenchInert(Boolean(activeSurfacePanel));
      if (!confirmed) return false;
    }
    const response = await postAction('/api/interaction/start', {
      command,
      clientRequestId: pending.id,
      confirmed,
    });
    clearCommandRequest(session);
    if (!options.preserveBrowserDraft && selectedSession === session && promptInput.value === browserText) {
      promptInput.value = '';
      persistPromptDraft();
      autosize();
      updateSendVisibility();
    }
    if (selectedSession === session) syncStructuredInteraction(response.interaction || null);
    if (!options.skipStatusRefresh)
      refreshStatus({ silent: true }).catch(handleBackgroundError);
    refreshCapture(currentCaptureLines(), { silent: true }).catch(handleBackgroundError);
    return true;
  }

  async function toggleFastMode() {
    const session = selectedSession;
    const expected = currentFastStatus === 'on' ? 'off' : 'on';
    await submitLocalCommand('/fast', {
      preserveBrowserDraft: true,
      skipStatusRefresh: true,
    });
    if (session !== selectedSession) return;
    currentFastStatus = expected;
    statusShellController.update(fastStatusModel(expected));
    for (let attempt = 0; attempt < 5 && session === selectedSession; attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 120));
      if (statusController.refreshInFlight) continue;
      await refreshStatus({ silent: true });
      if (currentFastStatus === expected) break;
    }
  }


  renderOutputModeButton();
  toggleClassState('header', 'collapsed', 'rdHeaderCollapsed'); toggleClassState('.app', 'header-collapsed', 'rdHeaderCollapsed'); syncStatusRefresh(false);
  $('sessionTitle').addEventListener('click', (event) => { if (event.target.closest('#homeBtn')) return; const on = !document.querySelector('header').classList.contains('collapsed'); toggleClassState('header', 'collapsed', 'rdHeaderCollapsed', on); toggleClassState('.app', 'header-collapsed', 'rdHeaderCollapsed', on); syncStatusRefresh(!on); });
  sessionMenu.addEventListener('click', (event) => {
    const button = event.target.closest('button');
    if (button?.hasAttribute('data-close-panel')) { closeSurfacePanels(); return; }
    const item = button?.dataset;
    if (item?.refresh) { refreshSessionMenu().catch((err) => setError(userErrorMessage(err))); return; }
    if (item?.route && item?.session) switchSession(item.route, item.session);
  });
  $('draftState').addEventListener('click', async (event) => {
    event.stopPropagation();
    if (!sessionMenu.classList.contains('hidden')) { closeSurfacePanels(); return; }
    const cache = cachedWorkbench();
    renderSessionMenu(cache, !cache);
    openSurfacePanel(sessionMenu, $('draftState'));
  });
  $('detailsBtn').addEventListener('click', (event) => { event.stopPropagation(); openSurfacePanel(detailsPanel, $('detailsBtn')); });
  function handleGoalPillClick(event) {
    event?.stopPropagation();
    if (activeSurfacePanel !== detailsPanel || detailsPanel.classList.contains('hidden')) {
      openSurfacePanel(detailsPanel, goalPill);
    }
    loadGoalDetails();
  }
  detailsPanel.addEventListener('click', (event) => { if (event.target.closest('[data-close-panel]')) closeSurfacePanels(); });
  const changesButton = $('detailsChangesBtn');
  changesButton.disabled = true;
  changesPanelModulePromise.then(({ createChangesPanelController }) => {
    const changesPanelController = createChangesPanelController({
      view: window,
      routeBase,
      getSelectedSession: () => selectedSession,
      api,
      userErrorMessage,
      setError,
      openSurfacePanel,
      closeSurfacePanels,
    });
    changesPanelController.connect();
    changesButton.disabled = false;
  }).catch((error) => {
    changesButton.disabled = true;
    changesButton.title = 'Workspace changes are temporarily unavailable';
    console.debug('workspace changes module unavailable', error);
  });
  $('detailsDiagnosticsBtn').addEventListener('click', downloadDiagnostics);
  panelBackdrop.addEventListener('click', () => closeSurfacePanels());
  $('dockPlusBtn').addEventListener('click', (event) => {
    event.stopPropagation();
    toggleDockMenu();
  });
  document.addEventListener('click', (event) => {
    if (dockMenu.classList.contains('hidden')) return;
    if (event.target.closest('.composer')) return;
    closeDockMenu();
  });
  document.addEventListener('keydown', (event) => {
    trapSurfacePanelFocus(event);
    if (event.key !== 'Escape' || event.defaultPrevented) return;
    const overlayOpen = !dockMenu.classList.contains('hidden')
      || Boolean(activeSurfacePanel)
      || document.documentElement.classList.contains('has-pending-interaction');
    closeDockMenu();
    closeSurfacePanels();
    if (!overlayOpen && queuedSendNowAvailable) {
      event.preventDefault();
      interruptOrSendQueuedNow();
    }
  });
  $('refreshBtn').addEventListener('click', async () => {
    try {
      await setOutputMode('compact');
    } catch (err) {
      setError(userErrorMessage(err));
    }
  });
  $('dockFullBtn').addEventListener('click', async () => {
    try { await setOutputMode('full'); } catch (err) { setError(userErrorMessage(err)); }
  });
  $('detailsChatBtn').addEventListener('click', async () => {
    try { await setOutputMode('compact'); closeSurfacePanels(); } catch (err) { setError(userErrorMessage(err)); }
  });
  $('detailsRawBtn').addEventListener('click', async () => {
    try { await setOutputMode('full'); closeSurfacePanels(); } catch (err) { setError(userErrorMessage(err)); }
  });
  $('detailsRefreshBtn').addEventListener('click', async () => {
    try { await Promise.all([refreshStatus({ silent: true }), refreshCapture(currentCaptureLines(), { silent: true })]); }
    catch (err) { setError(userErrorMessage(err)); }
  });

  async function submitPrompt() {
    if (submitInFlight) return;
    const text = promptInput.value.trim();
    if (!text && !pendingAttachments.length) return;
    if (pendingAttachments.some((item) => ['compressing', 'uploading'].includes(item.status))) { setError('Attachments are still uploading'); return; }
    if (pendingAttachments.some((item) => item.status === 'error')) { setError('Remove failed attachments and try again'); return; }
    const localCommand = !pendingAttachments.length ? structuredSlashCommand(text) : '';
    if (localCommand) {
      submitInFlight = true;
      try {
        closeDockMenu();
        await submitLocalCommand(localCommand);
      } catch (err) {
        persistPromptDraft();
        setError(userErrorMessage(err));
      } finally {
        submitInFlight = false;
      }
      return;
    }
    const readyAttachments = pendingAttachments.filter((item) => item.path);
    const attachmentText = readyAttachments.map((item) => `${item.kind === 'image' ? 'Image' : 'Attachment'}: ${item.path}`).join('\n');
    const browserText = promptInput.value;
    const outboundText = [text, attachmentText].filter(Boolean).join('\n');
    const submissionSession = selectedSession;
    const submission = composerDelivery.prepareSubmission({
      session: submissionSession,
      browserText,
      outboundText,
      attachmentPaths: readyAttachments.map((item) => item.path),
    });
    submitInFlight = true;
    try {
      closeDockMenu();
      playPetSend();
      const delivery = await sendWithDeliveryRecovery({ session: submission.session, text: submission.outboundText, clientMessageId: submission.id });
      if (delivery.deliveryState === 'queued') queuedSendNowAvailable = true;
      clearDeliveredPromptDraft(submission);
      clearPendingSubmission(submission);
      attachmentController.clearSubmitted(submission.attachmentPaths);
      if (selectedSession === submission.session) {
        if (promptInput.value === submission.browserText) promptInput.value = '';
        persistPromptDraft();
        autosize();
        updateSendVisibility();
      }
      refreshStatus({ silent: true }).catch(handleBackgroundError);
      refreshCapture(currentCaptureLines(), { silent: true }).catch(handleBackgroundError);
      setTimeout(() => refreshCapture(currentCaptureLines(), { silent: true }).catch(handleBackgroundError), 500);
    } catch (err) {
      stopPetSend();
      updatePetControl();
      if (selectedSession === submission.session) persistPromptDraft();
      preserveFailedPromptDraft(submission);
      setError(userErrorMessage(err));
    } finally {
      submitInFlight = false;
    }
  }

  $('sendBtn').addEventListener('click', submitPrompt);
  promptInput.addEventListener('keydown', (event) => {
    if (event.defaultPrevented) return;
    if (event.key !== 'Enter' || !(event.ctrlKey || event.metaKey)) return;
    event.preventDefault();
    submitPrompt();
  });

  async function interruptOrSendQueuedNow() {
    if (interruptInFlight) return;
    interruptInFlight = true;
    const wasRunning = agentRunning;
    const wasQueued = queuedSendNowAvailable;
    try {
      stopPetSend();
      playPetStop();
      const data = await api('/api/interrupt', { method: 'POST', body: JSON.stringify({ session: selectedSession }) });
      queuedSendNowAvailable = data.queuedFollowupExpedited ? false : (data.interrupted ? false : wasQueued);
      agentRunning = data.queuedFollowupExpedited ? true : (data.interrupted ? false : wasRunning);
      if (data.queuedFollowupExpedited) {
        stopPetStop();
        playPetSend();
      }
      if (!data.interrupted) {
        stopPetStop();
        updatePetControl();
      }
      refreshStatus({ silent: true }).catch(handleBackgroundError);
      refreshCapture(currentCaptureLines(), { silent: true }).catch(handleBackgroundError);
      setTimeout(() => refreshCapture(currentCaptureLines(), { silent: true }).catch(handleBackgroundError), 500);
    } catch (err) {
      stopPetStop();
      agentRunning = wasRunning;
      queuedSendNowAvailable = wasQueued;
      updatePetControl();
      setError(userErrorMessage(err));
    } finally {
      interruptInFlight = false;
    }
  }

  $('petControl').addEventListener('click', interruptOrSendQueuedNow);


  const statusCollapseKey = 'rdStatusCollapsedV2';
  const storedStatusCollapse = localStorage.getItem(statusCollapseKey);
  const initialStatusCollapsed = storedStatusCollapse === null ? true : storedStatusCollapse === '1';
  toggleClassState('.status-line', 'collapsed', statusCollapseKey, initialStatusCollapsed);
  toggleClassState('footer', 'status-collapsed', statusCollapseKey, initialStatusCollapsed);
  $('versionToggle').addEventListener('click', () => {
    const on = !document.querySelector('.status-line').classList.contains('collapsed');
    toggleClassState('.status-line', 'collapsed', statusCollapseKey, on);
    toggleClassState('footer', 'status-collapsed', statusCollapseKey, on);
  });

  // Fetch one complete metadata snapshot even when the persisted header state
  // is collapsed. Periodic status polling remains gated by header visibility;
  // lightweight capture/SSE metadata keeps `/rename` live after this point.
  refreshStatus().catch((err) => setError(userErrorMessage(err)));
  loadOwnerCapabilities().catch(handleBackgroundError);
  loadCodexCommandCatalog().catch(handleBackgroundError);
  refreshCapture(currentCaptureLines()).catch((err) => setError(userErrorMessage(err)));
  startEventStream();
  document.documentElement.dataset.faryoAppReady = '1';
  window.addEventListener('pageshow', handlePageShow);
  window.addEventListener('focus', resumeLiveConnection);
  window.addEventListener('online', resumeLiveConnection);
  window.addEventListener('pagehide', (event) => {
    lastLiveWakeAt = 0;
    cancelInitialLatestScroll();
    cancelActiveRefreshes();
    closeEventStream();
    setCaptureFallback(false);
    setStatusRefresh(false);
    setFullRefresh(false);
    if (!event.persisted) {
      keyboardLayoutController?.destroy();
      composerLayoutController?.destroy();
    }
  });
  document.addEventListener('visibilitychange', () => {
    setStatusRefresh(!document.hidden && headerStatusVisible());
    if (document.hidden) { lastLiveWakeAt = 0; cancelActiveRefreshes(); closeEventStream(); setCaptureFallback(false); setFullRefresh(false); }
    else {
      resumeLiveConnection();
    }
  });
})();
