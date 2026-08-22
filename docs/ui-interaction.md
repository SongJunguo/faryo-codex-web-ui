# Faryo UI Interaction Model

This document describes the maintained Ubuntu/Linux Codex UI in the standalone
project. It is an interaction contract, not a record of inherited platform or
package capabilities.

## Product Shape

Faryo has two browser surfaces:

1. **Gateway workbench** selects an active or resumable Codex session and applies
   the public authentication/routing boundary.
2. **Owner session view** reads and controls one existing tmux-backed Codex
   session without changing its terminal dimensions.

The browser is a thin control surface. Durable conversation history belongs to
Codex, live terminal state belongs to tmux, and runtime secrets stay below
`~/.faryo/`.

A newly created Codex thread with zero turns is a valid structured conversation,
not an unavailable history source. Chat renders a quiet `No messages yet` state
until the first prompt creates a durable turn. TUI startup redraws remain terminal
evidence and may appear in Raw or the transient Live panel, but never masquerade
as chat history. The warning fallback is reserved for an actual failure of both
durable rollout and App Server reads. These lifecycle states are projected by a
Preact transcript shell from a session/generation/mode-scoped store; Markdown,
KaTeX and Live content remain independent rendering adapters.

## Gateway Workbench

The authenticated Gateway home page keeps two session regions separate:

- **Active Sessions** lists every recognized live Codex tmux pane, including
  sessions started directly on the desktop. Cards show Starting, Running,
  Waiting, Exited, or Desktop from descendant-process and TUI readiness
  evidence, not only the tmux top-level command.
- **Session History** lists inactive resumable threads, uses server-backed pages
  of 10 records, supports Previous/Next plus direct page-number jumps, and can
  search title/folder metadata with date and archive filter chips. Search never
  scans conversation content and never hides Active Sessions.

Resumable Codex cards offer `Options` and `Archive`; archived cards offer
`Restore`. Clicking the card body remains the fast path and uses the most recent
backend, recorded cwd and default context. `Options` opens the same authenticated
launch sheet as Start Codex, explicitly selects `Codex App Server` or `Codex TUI
(tmux)`, and sends its signed directory plus an optional bounded context window
with the first resume request, so it does not create a speculative session in
the old cwd. New defaults to App Server; Resume defaults to the thread's most
recent/source backend. Archive uses a clear
confirmation because it moves the thread out of Current results, while Restore
is immediate. These actions preserve the current search, filter, and page
query. Active, desktop, running, waiting, starting, exited, and archived cards
do not offer folder selection; the UI has no hard-delete action.

Only sessions created and stamped by Faryo expose remote Close. Desktop-created
tmux sessions can be opened but are not remotely destroyed.

Gateway Settings exposes recent Security activity without message content and
keeps two account actions distinct: sign out only this browser, or explicitly
revoke every inner Faryo login without stopping Codex/tmux.

`Start Codex` returns an accepted `starting` receipt after command/path policy
and managed tmux creation succeed; it does not call that receipt ready and does
not block on MCP startup. The page opens immediately. A pane-identity-scoped
background monitor then observes the real Codex process and drives Starting to
Waiting, a structured startup interaction, or Exited with a bounded error.
Owner restart reconstructs that monitor from the private tmux marker. A missing
CLI, invalid configured path, or unavailable shell still fails before redirect.
For Faryo-managed launches, Starting first runs a serialized automatic Codex
update preflight. Runtime discovery follows NVM's current recursive default
alias on every launch rather than persisting a Node-version path. The preflight
uses the matching npm only for the fixed official Codex package, verifies the
result, and launches a new TUI with the redundant startup update prompt disabled.
An update failure produces a bounded notice and continues with the installed
version; it never strands the browser in an updater screen. If the binary
changes, Owner restarts its shared App Server and refreshes the private command
catalog before normal capture continues.
Managed sessions use the first free `faryoN` name. The start flow asks for the
workstation, then opens a launch-options browser at the most recent cwd. It
shows the current path, parent, configured roots, recent locations and every
returned child folder without a count cap or cross-section removal. Hidden
dot-directories remain controlled only by the remembered Hidden toggle; an
optional search filters only while the user has entered text. The selected
canonical path is signed and revalidated by Owner.
The same sheet offers Default, 372K, 1M, and custom whole-number K-token context
choices. Default sends no override. A custom value is validated by Gateway and
Owner, then sets only that process's Codex context and 90% auto-compaction
threshold; it never rewrites the user's global Codex config. Directory
navigation keeps the in-progress context choice.

Backend selection happens before resume. If either an App Server actor or a TUI
writer already owns the thread, Owner rejects the competing start atomically;
Faryo never starts a second writer and then attempts cleanup. Protocol/storage
compatibility values are mapped at one boundary and are not shown to users.

Gateway route labels come from runtime configuration. Public browser requests
never receive raw Owner tokens; Gateway injects them while proxying.

`/` is the one maintained Gateway home. The retired `/projects` orchestration
surface is not redirected or hidden behind the brand; it returns `404`. Generic
Files-to-session handoff remains part of the home workbench.

Chat and Raw keep separate capture caches. Raw intentionally replaces the
conversation area with the complete terminal, so it has no nested `Live from
tmux` card. Returning to Chat must synchronously restore structured Markdown/TeX
and then resume the independently collapsible Live panel.

For Codex App Server sessions, private reasoning items do not become repeated
`Working` messages. The active turn has one transient working/receiving status.
Commands, searches and edits are grouped by turn into a closed Activity card.
Its title reports the command, edit and search counts, so collapsed activity is
not mistaken for missing history. Opening the card exposes the audit trail in a
bounded scroll area; long command rows remain closed one level deeper until
explicitly selected. After an Owner reconnect, missing `thread/read` command
items are reconstructed from the durable rollout by stable turn and call IDs;
tool output and private reasoning bodies are not projected.

## Owner Session View

The Owner page contains:

- a Faryo logo link back to the Gateway home page;
- workstation/session title and session switcher;
- agent-reported context used/window and weekly quota when available;
- a privacy-safe Goal status pill whose authenticated, no-store details request
  loads the current objective only after an explicit click and clears it on
  close;
- git status, authoritative backend, and structured-source/connection details;
- Compact Chat and Raw output modes;
- attachments and a stable multiline composer;
- structured Codex interactions, interrupt, refresh, and return-to-latest
  controls.

Codex menus are not inferred by the browser. Owner parses the current terminal
through dedicated model, reasoning, usage, permissions, resume-directory,
workspace-trust and approval detectors, then publishes an opaque interaction
snapshot. The Preact sheet renders the actual options plus only the actions the
snapshot permits: Previous, Next, `Choose highlighted`, and Cancel. Clicking an
option or action revalidates the interaction generation before any key is sent.
Unknown blocking menus use the same explicit generic fallback instead of a
hidden raw key strip.

Exact slash commands use the structured command entry point rather than normal
message delivery. `/model` and the model status affordance share the real Codex
model/reasoning menu; `/usage` opens and closes as a local interaction without
creating a fake chat turn. The command catalog is refreshed from a private
read-only TUI inventory when the Codex version changes. A newly discovered
command is visibly unclassified and requires one explicit confirmation; Faryo
never retries Enter blindly.

Command capability includes whether Codex accepts the local command while a turn
is running. The installed Codex version and tested fallback version must match
before a fallback can grant busy-time permission. Verified commands such as
`/goal clear` are forwarded immediately; commands that Codex disables return a
specific conflict and cannot fall through into queued message delivery.

The model row also exposes the selected conversation's `Default` or `Fast`
service tier. Its button invokes the catalogued `/fast` local action through the
same one-Enter structured command path. The TUI model row is authoritative:
Faryo separates a trailing `fast` marker from the model and reasoning effort
instead of falling back to the global config. The button preserves an unsent web
draft, is disabled while the turn or another interaction is active, and updates
only the selected session. Switching sessions hides the old state until the new
status arrives.

Pending interaction identity includes the selected session and generation.
Status refresh, Details, Goal, Git, cwd, model and usage use the same scoped
acceptance rule. Reload/SSE reconnect rebuilds current state, session switches
clear the old sheet immediately, and late responses are ignored rather than
rendered over the newly selected session.

Opening menus, details, Raw mode, or the question rail must never resize tmux or
the Codex TUI.

Header actions remain deliberately separate: the logo returns home, the title
folds/unfolds the header, the folder switches sessions, and the sliders open
session details. Returning home uses same-origin `/` without carrying the Owner
token or selected-session query.

## Immersive Display

Faryo uses one bounded conversation scrollport in every Owner/Gateway/PWA mode.
The app shell is a `100dvh` grid whose composer is a normal layout row, so
history anchors, question navigation, live follow and input geometry share one
coordinate system. The viewport declares `interactive-widget=resizes-content`,
and Faryo explicitly disables VirtualKeyboard overlay when that API exists, so
the browser shrinks the app shell around the software keyboard. Faryo never
guesses keyboard pixels or adds a second keyboard-inset row.
On a coarse-pointer device while the composer is focused, footer and prompt
bottom spacing contract to the platform safe-area plus a 6 px focus-ring
clearance. That is the smallest real-device value that keeps the 3 px outline,
its 2 px offset and the rounded shell visible instead of clipping them at the
browser-owned boundary.

- Gateway's root manifest uses `display: standalone`, and every maintained page
  references it. An installed Faryo launches without the normal URL bar.
- The session header and Details expose `Enter full screen`. It calls the
  Fullscreen API only from the user's tap and requests hidden navigation UI.
- While the header is expanded, the same control becomes a labelled `Exit`.
- Folding the header reveals a compact `Exit full screen` pill; browser Back,
  system gestures and Esc remain valid exits.
- `fullscreenchange` reconciles exits initiated outside Faryo. State is not
  persisted, so reloads and newly opened sessions never enter automatically.
- Unsupported or denied requests leave the page intact and point to the Home
  install path.
- A normal browser tab may keep its address bar visible. Faryo does not force
  browser chrome; installed standalone PWA and user-activated Fullscreen are the
  explicit no-address-bar modes.

## Workspace Changes and Diagnostics

Session Details opens a separate read-only Changes panel. The Owner resolves the
selected tmux cwd to a Git root within the configured workspace boundary and
returns relative file status plus a bounded unified diff. The browser lazy-loads
local diff2html and DOMPurify assets only when the panel opens.

- phone defaults to line-by-line; desktop can use line or split view;
- wide split diffs scroll inside the panel and never widen the conversation;
- filenames enter the DOM through `textContent`; rendered diff HTML is sanitized;
- large output is explicitly truncated; untracked files are listed without
  pretending Git has diff content for them;
- no stage, discard, commit, checkout, apply or arbitrary revision/path action
  exists.

Details can download a versioned diagnostics JSON containing feature flags,
protocol boundaries and count-only runtime metadata. It contains no token,
Cookie, hostname, username, session id, cwd, path, title, prompt or answer.

## Attention

Gateway derives attention from existing lifecycle transitions rather than
conversation text. Waiting and Exited sessions appear with generic labels and
route/time metadata. Dismissal is in-memory and resets when the page reloads or
the lifecycle changes.

Browser notifications are opt-in, page-open only in v1.3, and always use the
generic body `A session completed or needs input.` A notification closure may
navigate to the exact session, but raw target data is not placed in its body or
metadata. Background Web Push/VAPID is deliberately outside this release.

## Compact Chat

Compact Chat is the default reading mode. It renders finalized Codex rollout
messages as stable user/assistant blocks through the local Markdown AST, GFM,
KaTeX, and Shiki pipeline.

Rules:

- raw HTML is escaped;
- dangerous URL protocols are rejected;
- TeX inside code stays literal;
- wide formulas, tables, and code scroll inside their own containers;
- a failed rich-render block falls back to safe plain text without stopping later
  updates;
- stable finalized blocks retain their DOM identity while the changing tail is
  reconciled.

Raw mode remains available for terminal evidence. If structured Codex history is
unavailable, Compact Chat must show an explicit fallback warning rather than
pretend that damaged tmux text is complete Markdown.

## Long Conversations

The initial structured transcript contains at most 12 recent complete turns.
Owner maintains a revision-bound index for all user turns and serves older
content through authenticated cursor pages. Tool events and rollout paths never
enter that index response.

When at least two user turns are indexed, Faryo prepares a right-edge question
rail:

- hidden and non-interactive during normal reading;
- revealed temporarily by a fast user wheel/swipe;
- auto-hidden after scrolling stops;
- held open while hovered or keyboard-focused;
- click, Arrow keys, Home, and End jump between questions;
- unloaded markers use a distinct dashed state and fetch their page before jump;
- deliberate top scrolling loads one older page while preserving the visible
  block anchor;
- the active marker follows the reading anchor and selects the final question at
  the bottom;
- mobile display overlays the extreme edge and never reserves permanent content
  width;
- live appends reuse existing marker nodes and preserve the main scroll position.

Question previews are truncated DOM-only labels. They are not written to local
or session storage.

## Main Scroll Contract

- A fresh load, explicit browser reload, or session switch starts at the latest
  conversation output after the first structured history page settles.
- A reader at the bottom follows the latest content.
- A reader who scrolls into history stays at that position during refreshes.
- The return-to-latest control is visible when needed and must not cover a table,
  formula, code block, or composer.
- Programmatic refresh or initial scroll-to-bottom does not reveal the question
  rail; only user scroll intent does.

## Live from tmux

While Codex is working, Compact Chat may include a separate collapsible
`Live from tmux` panel for transient execution evidence.

- It is outside stable structured history.
- A new/at-bottom panel follows terminal output.
- A manually scrolled panel preserves its inner position across refreshes.
- Its expansion preference is isolated per session.
- It retains up to 180 lines from the current user turn and exposes a dedicated
  copy button.
- Its DOM node is stable. While the user has a non-collapsed selection inside
  Live, terminal updates are held in memory and the visible text/revision does
  not change; the newest pending version is applied after selection clears.
- Clicking the live card does not send interrupt; the explicit animated stop
  control retains that action.

## Composer and Delivery

The composer keeps the same large base geometry across focus, blur, and mobile
keyboard state, growing only with real multiline text.

An image pasted while the composer is focused enters the same attachment queue
as Attach/drag-and-drop, with immediate thumbnail, progress and remove controls.
Faryo reads clipboard data only from that user-triggered paste event, keeps any
`text/plain` from the same event, and leaves ordinary text paste native.

Submission rules:

- one browser action creates one client message ID and immutable target-session
  snapshot;
- retry and late response handling never switch to a newly selected session;
- a conflicting desktop TUI draft is not overwritten;
- failed or ambiguous sends retain the browser draft;
- Owner confirms Codex acceptance before clearing the draft;
- a working Codex uses Tab for a queued follow-up, while an idle Codex uses Enter;
- when Codex explicitly displays `press esc to interrupt and send immediately`,
  the squirrel gains an `ESC` badge and accessible Send-now label; clicking it
  or pressing physical Escape interrupts the current tool and expedites the
  queued follow-up through one receipt;
- no-evidence recovery remains an explicit failure rather than a false success.

Attachments remain associated with the submission that included them; a late
response cannot clear a later session's independent draft or attachments.
Faryo does not claim a queue list, edit, reorder, or cancel API: Send now is the
single TUI-advertised Escape action, while an ordinary busy squirrel click
remains interrupt.

## Responsive Layout

### Phone (360–430 px)

- single reading column;
- 10–12 px normal side padding;
- stable large composer above the bottom safe area;
- composer remains a normal app-shell row; the resized layout viewport shortens
  the conversation row without translating the input by hand;
- question rail overlays the extreme edge only while active;
- tables, code, and display math use internal horizontal scrolling;
- session/details panels cover the page instead of shrinking the conversation.

### Tablet/Desktop

- centered conversation axis around 748 px;
- question rail appears outside that reading axis when space permits;
- session/details panels remain overlays;
- composer stays centered and does not expand to the full monitor width.

## Accessibility and Privacy

- Interactive controls use semantic buttons and visible focus states.
- The question rail uses roving tabindex and accessible question labels.
- Reduced-motion preference disables smooth/animated transitions.
- Owner tokens are removed from the visible URL and are not written into resource
  DOM attributes.
- File/image previews use authenticated requests and temporary Blob URLs.
- Internal memory annotations render as bounded cards and do not enter copied
  answer text.

## Acceptance Matrix

The maintained matrix includes:

- 390x844 mobile Chrome;
- 1440x900 desktop Microsoft Edge;
- structured Markdown/GFM/KaTeX/Shiki;
- 40-question complete index, bounded first page, cursor preload, lazy jump,
  eventual full DOM loading, auto-hide, stable append, and unchanged width;
- isolated no-zsh startup, invalid executable, readiness timeout cleanup, and a
  real Gateway-to-Owner Codex start;
- mobile directory-picker containment, default recent cwd, HMAC tamper rejection,
  `faryoN` naming and exact test-session cleanup;
- offline/background recovery and 20-message exact delivery;
- heartbeat-stalled SSE recovery, deduplicated safety capture and automatic
  reconnection after pageshow, focus, online and foreground transitions;
- cross-session retry/delayed-response isolation;
- structured `/model` and `/usage`, generic-menu `Choose highlighted`, pending
  interaction reload, and late-response/session-switch isolation;
- protected files/images, CSP, and safe render fallback;
- unchanged Codex tmux dimensions before and after browser/deployment tests.
- user-activated full-screen enter/exit through header and Details, folded-header
  exit, manifest/standalone identity, rejection fallback and no horizontal
  overflow.
- one non-document conversation scrollport, normal-flow composer, native
  viewport resize with overlay disabled, trusted touch scroll, stable refresh
  anchor and user-activated Fullscreen/PWA behaviour.

Detailed evidence is maintained in
[`plans/v1.6-structured-interactions-and-owner-ui-plan.md`](plans/v1.6-structured-interactions-and-owner-ui-plan.md),
[`plans/deepseek-inspired-ui-plan.md`](plans/deepseek-inspired-ui-plan.md) and
[`plans/codex-reliability-hardening-plan.md`](plans/codex-reliability-hardening-plan.md),
plus [`plans/full-history-navigation-plan.md`](plans/full-history-navigation-plan.md).
