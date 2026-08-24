# Faryo Local Tmux Owner

Local structured Web control surface for a tmux-backed Faryo endpoint. Faryo
Gateway reaches this service through path routing or a reverse tunnel. This
service exposes bounded status/capture/send operations and versioned Codex TUI
interactions; it does not expose arbitrary terminal key or shell endpoints.

## Start

By default, bind only to localhost:

```bash
cd local-tmux-owner
python3 server.py --session tmux --host 127.0.0.1 --port 8765
```

Public access should be exposed through Gateway, not by binding this service to
the public network:

```text
https://<your-faryo-domain>/<route>/
```

Direct local URL printed at startup:

```text
http://<host>:8765/?token=<token>
```

## Markdown and Math Rendering

Compact Chat uses one local AST pipeline:

```text
micromark -> mdast -> GFM/math nodes -> safe HTML -> KaTeX
```

It supports CommonMark, GFM tables/task lists/strikethrough/autolinks, CJK
punctuation next to strong emphasis, and `$...$`, `$$...$$`,
`\\(...\\)`, and `\\[...\\]` math. Tables, formulas, and code are parsed as
different node types, so TeX inside code stays literal and long formulas scroll
inside their own container instead of narrowing the page.

Settled fenced code uses a local Shiki JavaScript-regex engine. TypeScript,
shell, and JSON are available at startup; research and systems languages such
as Python, LaTeX, Lean, MATLAB, C/C++, Rust, and SQL load only when used. A
missing grammar or failed chunk keeps the escaped plain-code fallback. The
generated `highlight/manifest.json` is checked by release packaging, so lazy
chunks cannot be silently omitted.

The math delimiter and CJK extensions are adapted from a pinned DeepSeek
Harness commit under MIT. Exact sources, versions, and license texts are under
`static/vendor/markdown-ast`. KaTeX CSS and fonts remain under
`static/vendor/katex`. All runtime assets are local; production needs no Node
process, CDN, or permissive external-resource CSP. Raw HTML is escaped, links
and images pass protocol allowlists, and KaTeX runs with `trust: false`.

For a bound Codex session, Compact Chat incrementally reads original finalized
message text from Codex's rollout JSONL. The cache is isolated per session,
tracks file identity and byte offset, and does not commit a partial final JSONL
record. Codex App Server remains a compatibility fallback for sessions without
a readable rollout. While a message is incomplete, the streaming grammar keeps
math literal; the finalized structured message switches atomically to the full
GFM/math grammar. Raw remains terminal evidence. If both structured sources are
unavailable, the tmux fallback deliberately avoids guessing damaged formula
boundaries and displays a warning.

Chat and Raw cache their last successful captures independently. Raw replaces
the output area with complete terminal evidence; returning to Chat immediately
replays only the structured/compact capture and restores the separate Live tmux
panel before the next network refresh arrives.

Live tmux keeps at most 180 lines from the current user turn. Its `<details>` and
`<pre>` nodes survive normal Compact Chat reconciliation, so manual scroll and
browser selection remain anchored. New terminal text is held in memory while a
selection is active, flushed when selection ends, and can also be copied with
the panel's dedicated copy button.

Structured history serves at most 12 recent complete turns in the initial
capture. A separate full-history index records only displayable message byte
boundaries and truncated user previews, not tool-event bodies. The authenticated
`/api/conversation-history` endpoint uses revision-bound cursors to load older
pages or a page around a selected question. A 512 KiB recent-transcript ceiling
and a 2 MiB history-page target keep mobile responses bounded; a single complete
turn is not split merely to satisfy the target.

Compact Chat plans stable content keys for top-level conversation blocks and
reconciles those elements instead of replacing the entire output DOM. All but
the changing two-block tail are marked stable, and a 256-entry in-memory LRU
cache avoids reparsing unchanged Markdown. The cache is cleared on session
switches and highlighter revisions; it is never persisted to browser storage.
Live tmux remains outside that frozen history and restores its own scroll
snapshot before the next paint.

When at least two structured user turns exist, Compact Chat prepares a question
rail at the right edge from the complete server index. Dashed markers represent
unloaded turns. Selecting one fetches its page before jumping; a deliberate
scroll near the top fetches one older page and restores the same reading anchor.
The rail stays visually hidden during normal reading, appears temporarily after
a fast wheel/swipe, and remains available while hovered or keyboard-focused.
Live tail refreshes reuse already loaded pages and markers. On narrow screens it
overlays the extreme edge without reserving permanent width. The rail is absent
in Raw mode and stores neither previews nor navigation state in browser storage.

A fresh page load, explicit reload, or session switch follows the latest output
through the initial capture/history race. Browser scroll restoration is not used
as evidence of reading intent. Wheel, touch, or pointer interaction cancels that
initial follow state immediately, so later capture and history refreshes preserve
the reader's manual position.

The Faryo logo in the Owner header is a same-origin link to the Gateway home
page. It deliberately drops the current token and session query. The adjacent
title still folds/unfolds the header; the folder and details controls retain
their separate session-switching and status roles.

The same header includes an explicit full-screen toggle, with a labelled `Exit`
state while expanded and a floating `Exit full screen` control while the header
is folded. Details exposes the same action with a standalone-PWA hint. Entry is
always a direct user gesture; `fullscreenchange` handles browser/system exits,
and unsupported or denied requests fall back to the Gateway install guidance.
The page references Gateway's root manifest so an installed `standalone` PWA can
open any in-scope session without the ordinary URL bar.

Owner does not resize tmux or the Codex TUI. Browser sends retain an
immutable target session and client message id across timeout recovery,
confirmed submit delivery, idempotent retries, draft preservation on failure,
and conflict response when a different desktop draft already occupies the TUI
composer. Sends are serialized per tmux session, not globally, so a delayed
confirmation in one session does not block another session. For Codex, the
Owner recognizes both the idle `›` and working `»` composer prompts. It sends
`Enter` while idle and, following the Codex CLI interaction contract, sends
`Tab` while Codex is working so the web message becomes an explicit next-turn
follow-up (see the [official interactive shortcuts](https://learn.chatgpt.com/docs/developer-commands?surface=cli#interactive-shortcuts)).
An idle Enter may be confirmed when the exact text leaves the active composer.
A working Tab requires a new exact queued-follow-up occurrence or a new exact
rollout user event; an old identical queue item is not sufficient evidence.
When the real TUI additionally prints `press esc to interrupt and send
immediately`, status/capture expose only a boolean `queuedSendNowAvailable`.
The squirrel shows an ESC badge; its click and an unobstructed desktop Escape
key call the same interrupt endpoint. The response distinguishes
`queuedFollowupExpedited` from an ordinary interrupt. Faryo still exposes no
queue list/edit/reorder/cancel API.

Rebuild and test the committed browser bundle from the repository root with:

```bash
cd tools/markdown-engine
npm ci
npm run build
npm test
```

The dependency-free release test is:

```bash
node apps/owner/local-tmux-owner/tests/markdown-ast-bundle.test.js
```

The optional live-browser test requires a running Owner and Chrome:

```bash
FARYO_SMOKE_URL='http://127.0.0.1:8765/?token=<token>&session=<session>' \
  node apps/owner/local-tmux-owner/tests/browser-katex-smoke.mjs
```

The privacy-safe live-resilience browser check holds an authenticated SSE open
without heartbeats, requires deduplicated safety capture and automatic retry,
then verifies online, BFCache `pageshow`, and ordinary-reload recovery:

```bash
FARYO_SMOKE_URL='http://127.0.0.1:8765/?token=<token>&session=<session>' \
FARYO_SMOKE_EXPECT_RELEASE=v1.8.6 \
FARYO_SMOKE_EXPECT_CAPTURE_REVISION=faryo-owner-capture-3 \
  node apps/owner/local-tmux-owner/tests/browser-live-resilience.mjs
```

The same check accepts `FARYO_SMOKE_LOGIN_USER` and
`FARYO_SMOKE_LOGIN_PASSWORD_FILE` when its URL is an authenticated Gateway
route, so the production SSE proxy path can be exercised without exposing a
credential in process output.

For an exact Android Edge keyboard check, pair a private test phone through the
official wireless ADB flow, forward its `chrome_devtools_remote` socket to a
loopback CDP port, and reverse the local Owner port. Then run:

```bash
FARYO_ANDROID_CDP_URL=http://127.0.0.1:9223 \
FARYO_SMOKE_URL='http://127.0.0.1:8765/?token=<token>&session=<session>' \
  node tools/browser-harness/android-edge-keyboard.mjs --tap
```

The tool drives the real browser input surface and prints only the Faryo release,
viewport/inset values and known Faryo element bounds. It does not print the URL,
token, session title or conversation body. Use `--blur` to close focus and
`--resize-contract` only as an isolated A/B override for an older candidate.
Disconnect ADB and revoke the workstation after the private-device test.

Direct Owner local files and images are fetched with `X-Owner-Token` and opened
through temporary Blob URLs; the credential is not copied into resource DOM
attributes. The entry token is moved to tab-scoped storage and removed from the
visible URL; the authenticated event stream also uses the request header. The
isolated browser regression exercises both a file and an image, a memory-reference
card, a forced rich-render failure and the local AST Markdown/KaTeX fixture. It
checks the DOM and event URLs for the runtime token and confirms that Owner leaves
tmux sizing unchanged:

```bash
FARYO_RESOURCE_PYTHON=/path/to/project/python \
  apps/owner/local-tmux-owner/tests/browser-protected-resources.sh
```

Gateway resources keep using the authenticated same-origin route; Gateway
injects the Owner token server-side.

For an anonymous visual audit, set `FARYO_SMOKE_UI_SCREENSHOT` to a temporary
PNG path and choose `FARYO_SMOKE_UI_FOCUS=table`, `math`, or `code`. The fixture
replaces conversation text with generic Markdown before capture, checks that
wide content stays inside its own scroll container, and rejects a visible
scroll-to-latest control that overlaps the focused rich-output element.

The anonymous delivery matrix starts an isolated loopback Owner and temporary
tmux receiver, sends 20 exact-content short/Chinese/multiline/Markdown/TeX
messages, pastes one anonymous PNG through a real browser clipboard event,
preserves clipboard text, uploads and submits the image exactly once, verifies
network/background catch-up without reload, checks failed-draft preservation and
approval-control expansion, and removes all test state:

```bash
FARYO_DELIVERY_PYTHON=/path/to/project/python \
  apps/owner/local-tmux-owner/tests/browser-delivery-matrix.sh
```

The mobile-width cross-session regression holds retries and accepted responses
while the page switches between two anonymous sessions. It verifies that the
fixed original target receives both messages, the other session receives none,
same-text drafts stay isolated, and neither temporary tmux window is resized:

```bash
FARYO_DELIVERY_PYTHON=/path/to/project/python \
  apps/owner/local-tmux-owner/tests/browser-session-send-isolation.sh
```

The full-history browser matrix creates an anonymous 40-turn rollout and a
temporary Codex-shaped tmux runtime. It verifies a 12-turn first page, all 40
markers, top cursor preload with a stable anchor, lazy oldest-page loading,
eventual 40/40 DOM completeness, KaTeX on loaded pages, and unchanged tmux size:

```bash
FARYO_HISTORY_PYTHON=/path/to/project/python \
  apps/owner/local-tmux-owner/tests/browser-full-history.sh
```

The isolated runtime-start check removes zsh from PATH, starts a managed
`faryoN` Codex-shaped process through the bash fallback, validates an explicit
directory root, requires Owner to observe the live process before returning
success, and removes only the temporary session:

```bash
FARYO_START_PYTHON=/path/to/project/python \
  apps/owner/local-tmux-owner/tests/start-codex-runtime.sh
```

The command inventory check opens only the `/` popup in a temporary fixed-size
tmux, scrolls without submitting a command, compares the current CLI with the
versioned browser metadata, then confirms every existing Codex window kept its
original geometry:

```bash
apps/owner/local-tmux-owner/tests/codex-command-inventory.sh
```

The browser palette exposes descriptions, categories, argument hints and risk
cues. Codex's explicit `session_index.jsonl` thread name takes precedence over a
managed tmux startup label, so `/rename` propagates through capture/SSE and the
Gateway session list without changing the tmux session name.

Compact Chat copy uses a render-lifetime WeakMap rather than reconstructing text
from KaTeX DOM. The answer button and complete selected blocks preserve original
Markdown; a selected equation preserves its original TeX delimiters; partial
cross-block selections use a bounded structural serializer. Internal references,
Live tmux, buttons, local resource targets, and KaTeX's duplicate accessibility
tree are excluded. No answer source is placed in a DOM attribute or browser
storage. The browser regression is enabled with:

```bash
FARYO_SMOKE_CHECK_COPY_FIDELITY=1 \
  node apps/owner/local-tmux-owner/tests/browser-katex-smoke.mjs
```

`FARYO_START_DIRECTORY_ROOTS` is an `os.pathsep`/colon-separated list used only
by the authenticated directory picker and start validation. Owner canonicalizes
each selected path, lists every child directory without an entry cap, hides
dot-directories only while the explicit Hidden toggle is off, rejects symlink
escapes, and signs the selected absolute path. Gateway verifies that signature
before forwarding the launch; Owner validates the root again.

Persistent send receipts can be verified across a real Owner process restart
without writing to an existing conversation:

```bash
FARYO_RESTART_PYTHON=/path/to/project/python \
  apps/owner/local-tmux-owner/tests/send-restart-idempotency.sh
```

Version 2 delivery records contain only the client message ID, session, digest,
status and timestamp. An accepted record adds its receipt. A pasted checkpoint
may add the pre-submit queue count and rollout device/inode/offset needed for
safe restart recovery. Records contain neither the message body nor the rollout
path; their directory is `0700` and each file is `0600`.

Set `FARYO_DELIVERY_URL_TEMPLATE` with a literal `{session}` placeholder to run
the same non-attachment matrix against an already deployed Owner. The URL is
consumed as private runtime input and is never printed.

## Security Boundary

- Does not expose arbitrary shell execution.
- Does not provide a general file-write API; uploads are written only to the
  configured Faryo inbox.
- `attachment_storage.py` owns magic/MIME/suffix detection, the 25 MB bound,
  generated filenames and seven-day dated retention; the HTTP server only maps
  its bounded errors into API responses.
- `path_policy.py` owns local-file suffix lookup and start-directory root,
  symlink, listing-limit and selection-token rules independently of the tmux
  runtime.
- `static/owner/changes-panel.mjs` owns the read-only Changes payload, files,
  line/split state and lazy sanitized diff assets. It receives API, session and
  panel functions explicitly and does not read the Owner token.
- `static/owner/api-client.mjs` owns route-local API URL composition, Owner
  headers, single-flight Gateway CSRF caching, JSON/FormData headers and
  structured HTTP errors. It receives fetch, route and token dependencies and
  does not own transcript or rendering state.
- `static/owner/attachment-controller.mjs` owns up to 35 pending files with at
  most four simultaneous compression/upload tasks, horizontal preview,
  progress/cancel, clipboard images and drag/drop. The server still enforces
  the independent 25 MB per-file limit.
- `static/owner/history-controller.mjs` owns conversation revision state,
  complete question indexes, paged turn merging, around/cursor fetches, 409
  retry and debounced refresh. It receives scroll anchors and render callbacks
  instead of owning Markdown DOM.
- `static/owner/capture-controller.mjs` owns bounded capture requests, late
  response cancellation, SSE fetch/parser lifecycle, heartbeat timeout,
  reconnect backoff, deduplicated safety/fallback polling and unlocked Raw
  refresh. Each delivery carries the ConversationStore scope captured when the
  request or stream started, so a session/mode generation change rejects old
  frames before they touch metadata or DOM. Rendering remains in `app.js`.
- `apps/owner/ui/ConversationStore.ts` owns immutable session/generation/mode
  snapshots. `TranscriptShell.tsx` projects loading, startup, empty, terminal
  fallback and render-error states while preserving the adapter-owned
  Markdown/KaTeX and Live transcript DOM below it.
- `static/owner/composer-delivery.mjs` owns session-scoped draft/pending storage,
  stable client message IDs, success-only clearing, failed-draft preservation
  and one same-ID recovery attempt for ambiguous delivery errors.
- `owner_http.py` owns CSP/security headers, private-query log stripping,
  header/query token authentication, bounded JSON/multipart bodies, gzip JSON
  and file responses. Handler methods are thin route-facing delegates.
- `appserver_runtime.py` owns the production Owner's single supervised Unix
  socket channel, including bounded compatibility reads that previously used a
  second stdio process. `codex_app_server.py` remains only as a standalone
  compatibility fallback outside the ASGI composition root.
- `tmux_runtime.py` owns UTF-8 subprocess defaults, fixed tmux invocation,
  process-tree parsing and bounded session/thread/message identifiers; product
  policy remains in the higher Owner services.
- `delivery_store.py` owns body-free accepted/pasted checkpoints, atomic fsync,
  private permissions, TTL cleanup and corrupt/symlink rejection.
- `delivery_service.py` owns reference-counted per-session/per-message locks,
  in-memory delivery state, paste confirmation, Tab/Enter submission, durable
  retry and ambiguous 504 recovery. It receives all tmux/Codex operations
  through an explicit runtime adapter and stores no message body.
- `codex_history.py` owns rollout/App Server message extraction, intact recent
  turn budgets, question previews and revision-bound cursor syntax without
  reading tmux, HTTP, SQLite or private paths.
- Local file preview is token-protected and limited to supported file suffixes.
- `send` targets the controlled tmux pane and is maintained for Codex; generic
  terminal interfaces retain the conservative tmux path.
- Should not bind directly to public or LAN addresses.

Codex status reading is optional metadata for model, context, Goal, and
rate-limit display. The Owner header shows the remaining weekly percentage and
a compact Goal state; Session Details adds used/reset quota and can fetch the
current Goal objective from authenticated `/api/goal` only after a click. The
objective is excluded from routine status, diagnostics, logs and browser
storage, and the details DOM is cleared on close. Context used/window values
come from the agent's rollout rather than a configured model maximum.
Rate limits use one non-blocking, single-flight cache; an NVM-installed
`codex.js` is paired with its sibling Node runtime even when a systemd service
has no NVM directory in `PATH`. Without this metadata, the service still works
as a generic tmux control surface and displays the optional fields as
unavailable.

## API

- `GET /api/status`
- `GET /api/capture?lines=240`
- `GET /api/events?lines=320` (SSE structured capture plus transient live tail)
- `GET /api/capabilities` (versioned feature/protocol flags)
- `GET /api/diagnostics` (redacted feature/protocol flags and counts)
- `GET /api/interaction?session=...` (current opaque pending interaction)
- `GET /api/goal?session=...` (on-demand, no-store current Goal details)
- `GET /api/command-catalog` (versioned fallback/runtime slash inventory)
- `GET /api/workspace-changes?session=...` (bounded, workspace-scoped read-only Git status/diff)
- `GET /api/agent-sessions` (active and paginated Current/Archived metadata)
- `POST /api/agent-session/archive` (inactive Codex thread only)
- `POST /api/agent-session/unarchive`
- `POST /api/send` with `text`, `session`, and optional `clientMessageId`
- `POST /api/interaction/start` with a catalog command and client request ID
- `POST /api/interaction/respond` with an opaque interaction/action or option ID

Interaction start/respond re-captures the real Codex TUI under a per-session
lock. A stale generation returns `409` without sending a key, duplicate request
IDs return the original receipt, and browsers never submit line numbers,
arbitrary paths, or raw keys. The removed `/api/up`, `/api/down`, and
`/api/approve` compatibility endpoints are not part of the maintained API.

Archive and unarchive use Codex App Server thread lifecycle RPC, verify the
resulting metadata state, reject active threads, and honor the Gateway-provided
workspace scope. There is intentionally no thread-delete endpoint.

Workspace changes use fixed Git argv with external diff, textconv, fsmonitor,
hooks, pager and color disabled. Paths are relative and output is bounded. The
browser lazily loads the locally bundled diff2html + DOMPurify renderer; neither
the API nor UI can write Git state. Capability metadata explicitly reports that
editable pending-queue management is unsupported for the TUI-owned Codex turn.
