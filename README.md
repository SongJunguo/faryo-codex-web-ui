# Faryo — Self-hosted Codex Web UI

[![Release](https://img.shields.io/github/v/release/SongJunguo/faryo-codex-web-ui?display_name=tag&sort=semver)](https://github.com/SongJunguo/faryo-codex-web-ui/releases/latest)
[![Source CI](https://github.com/SongJunguo/faryo-codex-web-ui/actions/workflows/ci.yml/badge.svg)](https://github.com/SongJunguo/faryo-codex-web-ui/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](docs/local-installation.md)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**Faryo is an open-source, self-hosted Codex CLI Web UI and mobile agent
workbench for an Ubuntu/Linux workstation.**

Use it from a phone or desktop to stream structured Codex answers, render
Markdown and LaTeX with local KaTeX, send reliable follow-ups, attach files,
manage sessions, approve or interrupt work, and keep existing tmux/TUI sessions
available through a compatibility view. Faryo is a lightweight Codex agent UI
and session harness—not remote desktop, a hosted IDE, or a generic browser
terminal.

[Install](#installation) · [Screenshots](#real-ui-screenshots) ·
[Features](#current-functionality) · [Security](#security-boundary) ·
[Latest release](https://github.com/SongJunguo/faryo-codex-web-ui/releases/latest)

## Project Status

Faryo Codex Web UI started from the MIT-licensed `Snailflyer/faryo` project and
is now maintained as an independent, Codex-focused project. Its
[upstream baseline](https://github.com/SongJunguo/faryo-codex-web-ui/commit/625d8cecc15b03b71c41b57bc60d0315e30841c4)
is preserved in Git history; the original public URL remains recorded as the
local `upstream` remote for provenance, although it is currently unavailable.
This project is maintained at
[SongJunguo/faryo-codex-web-ui](https://github.com/SongJunguo/faryo-codex-web-ui).

Faryo is an independent community project. It is not affiliated with, endorsed
by, or sponsored by OpenAI; “Codex” is used only to describe compatibility with
Codex CLI. Faryo uses its own name and logo and includes no OpenAI brand assets.

## Why Faryo

Most lightweight tmux web bridges can only reproduce terminal text. Faryo uses
the official Codex App Server for structured Web sessions and Codex's rollout
history for durable convergence, while preserving existing Codex TUI sessions
in tmux as an explicit compatibility mode.

- **Formula-first reading:** original Markdown and TeX render through a safe AST
  pipeline and local KaTeX, including tables, cases, matrices, indexed operators,
  and wide display equations.
- **Explicit session ownership:** New and Resume can select `Codex App Server`
  or `Codex TUI (tmux)`. Exactly one writer owns a thread, and an occupied thread
  is never silently taken over by the other backend.
- **Real answer streaming:** stable item identities, bounded delta batching,
  cursor replay and snapshot recovery show the answer while Codex is producing
  it, then converge in place to the durable rollout without duplicate blocks.
- **Durable, typed activity trace:** private reasoning placeholders stay hidden;
  commands, searches, edits and MCP calls retain their official type and state,
  survive Owner reconnects, and collapse into one inspectable Activity card per
  turn. Output and diffs load only when opened.
- **Visible session commands:** `/rename`, `/model`, `/fast`, permissions and
  other mutating Web commands appear as compact lifecycle rows without becoming
  prompts or entering the model context.
- **Long-conversation navigation:** structured history stays bounded, and a
  fast-scroll question rail jumps between prior user turns.
- **Mobile immersive reading:** installable standalone PWA plus an explicit,
  user-activated full-screen mode with clear exit controls.
- **Reliable remote input:** delivery is confirmed, idempotent across retries and
  restarts, isolated by session, and preserves drafts on ambiguous failures.
- **Self-healing live output:** authenticated SSE remains the fast path, while a
  heartbeat watchdog, deduplicated safety capture and foreground/network wake
  hooks recover automatically after a suspended or half-open browser stream.
- **Fast asynchronous launch:** Start/Resume returns a real Starting state
  immediately while the selected App Server or TUI backend completes readiness
  asynchronously.
- **Per-session context windows:** new and resumed sessions can inherit the
  workstation default, use 372K or 1M presets, or accept a bounded custom value
  in K tokens without rewriting the global Codex configuration.
- **Queued follow-up Send now:** when Codex advertises Esc-to-send-now, the
  squirrel gains an ESC badge; phone tap and desktop Escape share one verified
  action without pretending Faryo can edit the queue.
- **Self-hosted security boundary:** Owner and Gateway stay on loopback behind an
  operator-controlled identity-aware HTTPS edge.
- **Clean agent environment:** persistent tmux and Codex children do not inherit
  Faryo service tokens, installation roots, or stale internal Python paths.

## Real UI Screenshots

<p align="center">
  <img src="docs/assets/screenshots/faryo-formula-mobile.png" alt="Faryo mobile Compact Chat rendering an operator table, piecewise function, square-root equation, matrix, code, and question rail" width="300">
  <img src="docs/assets/screenshots/faryo-formula-table-desktop.png" alt="Faryo desktop light theme rendering a GFM table with KaTeX operators and wide display equations" width="650">
</p>

<p align="center">
  <img src="docs/assets/screenshots/faryo-code-live-desktop-dark.png" alt="Faryo desktop dark theme showing Shiki code highlighting, a user turn, Live from tmux, the question rail, and composer" width="950">
</p>

<p align="center"><sub>Generated by the repository's real Chrome/Edge browser regression with an anonymous fixture. No private conversation, account, hostname, path, domain, or token is present.</sub></p>

## Maintained Scope

The current `main` branch is source-deployed and Codex-focused. Its maintained
acceptance path is:

```text
Ubuntu/Linux workstation
  -> Faryo Codex App Server on a private Unix socket
  -> Faryo Owner ASGI on loopback
  -> Faryo Gateway on loopback
  -> Cloudflare Tunnel + Access (or another hardened HTTPS edge)
  -> current Chrome or Microsoft Edge on phone/desktop

Existing Codex TUI (tmux) compatibility path:
  tmux -> Codex CLI/TUI -> Faryo Owner
```

This standalone project does **not** currently publish or maintain binary
release packages.
Historical distribution and non-Codex platform paths may remain in the tree for
upstream compatibility, but they are not part of this project's current validation
or support claims.

Current source line: **Faryo 1.11.7**. Latest tagged source release: **[Faryo
1.11.7](https://github.com/SongJunguo/faryo-codex-web-ui/releases/tag/v1.11.7)**.

## Current Functionality

### Structured Codex conversation

- Streams Codex App Server threads through `thread`, `turn`, and
  `item` lifecycle events, including agent-message deltas and final items.
- Sends input received during an active turn through the official `turn/steer`
  path instead of attempting a second turn. Busy Close is an explicit
  interrupt-and-close action that waits for the turn to settle and retains the
  Codex conversation history.
- Renders App Server user, assistant and plan items as distinct keyed blocks.
  One live working/receiving state accompanies incremental answer text; empty
  reasoning placeholders are omitted. Each user message becomes its own
  navigable conversation segment, even when Codex keeps several messages in one
  protocol turn. Tool activity keeps a typed command/file/search/MCP lifecycle
  and is grouped into chronological contiguous batches whose titles report
  semantic counts. Every Activity batch defaults closed, including running,
  waiting and failed work; its summary still exposes the current state. Command
  output, tool results and file diffs are fetched through an authenticated,
  no-store item endpoint only when a user expands that item. A closed group
  mounts only its summary row, so tool-heavy turns do not create a hidden DOM
  subtree during initial history rendering. Expanded Activity lists use native
  scroll chaining, so continued wheel or touch movement at a boundary resumes
  scrolling the surrounding conversation.
- Treats Codex rollout JSONL as the durable final source. An Owner restart can
  reconnect to the independent App Server service and recover both conversation
  messages and bounded tool activity without inventing a second message database.
- Keeps Codex TUI threads in tmux as a compatibility backend; tmux capture remains
  conservative live evidence and never becomes a competing writer. If the
  resident App Server is still inside Codex's delayed writer-release window,
  TUI resume uses the official local `--remote` endpoint to reuse that same
  writer instead of racing it.
- Keeps the initial payload to at most 12 recent complete question segments,
  then exposes older segments through a revision-bound cursor API. Formula-heavy answers cannot
  silently erase the complete question index.
- Separates stable conversation history from the transient `Live from tmux`
  execution panel.
- Keeps live output self-healing across ordinary network interruption, browser
  background suspension and back/forward cache restoration without requiring a
  hard refresh. A low-frequency safety capture is deduplicated before rendering.
- Treats a newly created zero-message Codex thread as a valid empty structured
  conversation. Its first page says `No messages yet` instead of replaying TUI
  startup frames through the lossy terminal fallback.

### Markdown, formulas, and code

Compact Chat uses one local rendering pipeline:

```text
micromark -> mdast -> CommonMark/GFM/math nodes -> safe HTML -> KaTeX
```

- Supports GFM tables, task lists, strikethrough, autolinks, CJK punctuation,
  `$...$`, `$$...$$`, `\(...\)`, and `\[...\]`.
- Keeps TeX inside code literal and lets wide formulas, tables, and code scroll
  inside their own containers.
- Uses local KaTeX CSS/fonts and lazy local Shiki language chunks; production
  rendering does not require a CDN or Node process.
- Escapes raw HTML, restricts URL protocols, and runs KaTeX with `trust: false`.
- Falls back per message to safe plain text when rich rendering fails, without
  stopping later live updates.

### Long-conversation navigation

- Builds a right-edge marker for every indexed user question, including turns
  that have not yet been loaded into the DOM.
- Links loaded App Server markers directly to their structured user blocks, so a
  visible marker cannot silently exist without a working question target.
- Stays hidden during normal reading and appears only after a fast user
  wheel/swipe.
- Auto-hides after scrolling stops, while hover and keyboard focus keep it
  available.
- Supports click, arrow keys, Home, and End for jumping between questions.
- Lazily loads the required page when an unloaded marker is selected and loads
  one older page near the top while preserving the current reading anchor.
- Overlays the extreme mobile edge without reserving permanent blank space or
  changing conversation width.
- Reuses stable marker DOM during live appends and stores no question text or
  navigation state in browser storage.

### Reliable browser-to-Codex delivery

- Keeps an immutable session, message ID, text, and attachment snapshot across
  retries.
- Sends a Codex App Server message once through `turn/start`, returns a
  fast idempotent acknowledgement even when the protocol response is slow, and
  converges from official item events.
- Serializes one tmux composer per Codex TUI (tmux) session without blocking
  unrelated sessions. That compatibility path still confirms submission from
  exact rollout, new queue, or safe idle-composer evidence instead of treating
  every cleared input as success.
- Persists minimal `pasted`/`accepted` idempotency state with mode-`600` files;
  message bodies and rollout paths are not stored.
- Returns an explicit ambiguous failure when evidence is insufficient, without
  re-pasting or blindly sending another key.
- Preserves browser and TUI drafts after conflicts, timeouts, or failed sends.

### Workbench interaction

- Uses `/` as the single Gateway home. The older Project Orchestration surface
  has been retired; generic Files-to-session handoff remains available.
- On narrow Gateway browser tabs, adapts the conversation to real document
  scrolling so mobile Edge/Chromium can apply their native toolbar auto-hide.
  Direct Owner, desktop and standalone PWA retain the bounded inner scroller.
- Also offers two explicit no-address-bar paths: installed `standalone` PWA and
  a tap-triggered Fullscreen API mode. Full screen can be left from the expanded
  header, a collapsed-header exit pill, or the browser/OS exit gesture; it is
  never entered automatically.
- Uses the Faryo logo as a direct return to the Gateway home page while the
  adjacent session title keeps its independent header-collapse action.
- Keeps a large composer geometry across focus, blur, and mobile keyboard state.
- Opens a workspace-scoped, strictly read-only Changes panel with lazy local
  diff2html + DOMPurify rendering, mobile line view and desktop split view. It
  exposes no stage, discard, commit, checkout or apply action.
- Publishes versioned capability metadata and downloadable redacted diagnostics
  containing feature flags and counts, never paths, titles, prompts or tokens.
- Accepts a user-triggered clipboard image paste directly in the composer,
  preserves accompanying plain text, and reuses the existing compressed upload,
  thumbnail, removal, attachment-limit, and idempotent-send path without asking
  for persistent clipboard access.
- Keeps up to 35 pending conversation attachments in a horizontally bounded
  preview strip, with at most four compression/upload workers active at once
  and a 25 MiB server-side limit for each file.
- Shows agent-reported context used/window and weekly quota when available.
- Shows the authoritative `Codex App Server` or `Codex TUI (tmux)` backend in
  Session Details instead of exposing storage/protocol names.
- Shows a session-scoped `Default`/`Fast` speed button beside the model. It
  submits the matching structured App Server command or Codex's exact `/fast`
  TUI command once, preserves any unsent browser draft, disables itself while
  Codex is busy or waiting on another interaction, and never changes another
  conversation or the global default.
- Shows the current Codex Goal state as a compact header pill. Clicking it loads
  the current objective, status, elapsed time, and available budget fields from
  an authenticated, no-store endpoint; closing the panel clears that text from
  the DOM. Objective text never enters routine status, diagnostics, history,
  audit, logs, or browser storage.
- Opens fresh, reloaded, and newly selected conversations at the latest output.
- Preserves the main reading position during structured refreshes.
- Keeps up to 180 lines from the current turn in an expanded Live tmux panel for
  Codex TUI (tmux) sessions,
  preserves the same DOM node and manual scroll position, pauses updates while
  its text is selected, and offers one-click copy of the visible terminal text.
- Uses a versioned slash-command catalog populated by a private, read-only TUI
  inventory probe. The bundled Codex 0.149.0 snapshot currently contains 46
  classified commands, but it is only a fallback: additions or removals are
  reconciled against the observed Codex version instead of being hard-coded
  forever.
- Uses each command's verified busy-time capability. Commands such as
  `/goal clear` can reach Codex while a task runs; commands that Codex really
  disables return a precise conflict and never become queued prompt text.
- Routes `/model`, `/usage`, permissions, approvals, resume questions, and
  unknown blocking menus through one structured interaction service. Model and
  reasoning choices, Previous/Next, explicit `Choose highlighted`, and Cancel
  are bound to an opaque interaction generation, so refreshes can rebuild the
  menu and stale tabs cannot send keys into a newer state.
- Reflects Codex `/rename` changes in the page title and session cards without
  renaming the underlying tmux session or requiring a reload.
- Records browser-issued mutating slash commands as a small private lifecycle
  (`running`, `waiting`, `completed`, or `failed`) and renders a compact system
  row after the relevant turn. The row survives Owner/browser reloads, remains
  outside Codex messages, and never stores Goal objectives or routine read-only
  panels such as `/usage`.
- Copies full answers and multi-message selections from in-memory original
  Markdown, and copies a selected formula as one original TeX expression
  instead of KaTeX's visual/MathML layers. Safe rich HTML is included when the
  browser supports it; Raw, code, and input selections keep native behavior.
- Provides Chat/Raw views, attachments, interrupt, structured Codex menus, page
  navigation, session switching, and a return-to-latest control.
- Uses a small local Preact + TypeScript + Vite bundle for the composer, command
  palette, interaction sheet, and dynamic status shell. The transcript,
  Markdown/TeX renderer, Raw view, and Live tmux remain isolated rendering
  surfaces; production requires neither Node nor a CDN.
- Keeps independent Chat and Raw capture caches, so returning from the full
  terminal view synchronously restores rendered Markdown/TeX and its separate
  `Live from tmux` panel instead of replaying terminal code in the conversation.
- Does not resize Codex or tmux windows; real tmux clients remain the source of
  terminal dimensions and wrapping.

### Gateway session management

- Keeps active tmux-backed Codex sessions separate from resumable history.
- Shows every recognized active Codex pane, including desktop-started sessions.
- Labels sessions from process-tree and TUI evidence as Starting, Running,
  Waiting, Exited, Desktop, Resumable, or Archived; a bash wrapper cannot hide
  its Codex descendant, and an exited managed shell remains safely closable.
- Keeps history server-paginated at 10 records per page with Previous/Next and
  direct page-number navigation.
- Searches hundreds of inactive sessions server-side by normalized title,
  explicit Codex `/rename` name, or working-folder basename, with Today/7-day/
  30-day and Current/Archived filters. Search never scans rollout or message
  content, never changes Active Sessions, and is not stored in browser storage.
- Archives resumable history and restores archived history through Codex App
  Server `thread/archive` and `thread/unarchive`. Faryo never edits Codex's
  SQLite/rollout files directly and deliberately exposes no hard-delete action.
- Keeps card-body clicks as the fast resume path using the thread's recorded
  working directory and default Codex context. `Options` opens the authenticated
  directory browser and resumes that same thread with an explicitly selected
  `Codex App Server` or `Codex TUI (tmux)` backend, signed cwd, and optional
  per-session context window.
  Active sessions do not expose this action; archived sessions must be restored
  first.
- Allows remote Close only for sessions that Faryo created and stamped.
- `Start Codex` defaults to a Codex App Server thread and immediately opens its
  Starting page. The same launch sheet can instead select `Codex TUI (tmux)`,
  which resolves the configured CLI with its matching Node runtime, creates a
  managed tmux, and lets a pane-identity monitor own final readiness/failure.
- Codex discovery is dynamic on every managed launch: Faryo follows the current
  NVM default and stable user commands before treating an old generated path as
  a fallback. It freezes an absolute Node/Codex pair only for that one process.
- Before new or resumed TUI startup, a locked preflight checks the fixed official
  `@openai/codex` package, updates it with the matching NVM npm when necessary,
  verifies the installed version, and then starts a fresh Codex process. Failed
  or timed-out updates continue with the installed version instead of losing the
  conversation to an in-TUI restart prompt.
- Start retries carry one stable launch ID across browser, Gateway, Owner
  restarts, and lost responses, so an ambiguous retry returns the same managed
  tmux instead of creating a duplicate. HTML login/edge errors are classified
  without exposing parser text.
- New managed sessions use `faryo1`, `faryo2`, ... names. After choosing a
  workstation, an authenticated directory browser opens at the most recent cwd
  with a compact breadcrumb, instant filtering, internally deduplicated Recent
  shortcuts, canonical Folders/Locations groups, a conventional Folders-first
  `..` parent entry, a remembered Hidden toggle, and a fixed `Start Codex here`
  action. Folders always contains every Owner-returned child even when that path
  is also a configured Root, Location, Parent, or Recent shortcut; there is no
  automatic entry cap.
- The same launch sheet offers Default, 372K and 1M context presets plus a
  custom whole-number K field. A custom value is passed as one-off Codex config,
  with auto-compaction at 90%; it does not edit `~/.codex/config.toml`. The
  selected model/provider must support the requested window, and the session
  header continues to show Codex's actual reported usable window.
- Injects Owner tokens server-side so public browser URLs do not contain them.
- Records body-free control metadata in a private mode-600, 7-day/5000-row
  audit using HMAC-pseudonymous targets. Security activity is scoped to the
  signed-in user/routes; prompts, answers, titles, paths, raw IDs and credentials
  are never audit fields.
- Maintains an in-memory Attention center for Waiting/Exited transitions and an
  operator-enabled page-open Notification path with generic body text. No
  notification contains a session title, path, prompt, answer or raw identifier.

## Architecture

```text
phone / desktop browser
  -> identity-aware HTTPS edge
  -> Faryo Gateway (127.0.0.1:8780)
  -> Faryo Owner ASGI (127.0.0.1:8765)
       |-> private Unix WebSocket -> persistent Codex App Server
       `-> existing tmux pane -> Codex CLI/TUI compatibility mode
```

Gateway owns public login, route authorization, session/history navigation, and
proxying. Owner uses Starlette/Uvicorn for authenticated HTTP, SSE and lifecycle
handling; framework-neutral Python modules own the session actors, bounded event
journal, App Server protocol, attachments and tmux compatibility adapter. The
App Server is a separate user service with no TCP listener, so an Owner-only
restart does not interrupt an active Codex App Server turn. Codex rollout history
remains the durable conversation source.

Gateway portal CSS and JavaScript are separate versioned static assets; dynamic
route labels enter through a nonce-protected JSON bootstrap. Focused local
Preact bundles own Gateway keyed session lists and the Owner's dynamic
interaction shell, while the Owner transcript and Markdown/TeX renderer remain
an imperative island behind narrow adapters. Development uses locked
Playwright/Ruff tooling, and every production runtime dependency or lazy bundled
UI asset remains local and independently documented.

## Installation

### Requirements

- Ubuntu/Linux with a systemd user manager
- Python 3.10 or newer with the standard `venv` module
- `tmux`, `curl`, and Codex CLI
- current Chrome or Microsoft Edge
- a public HTTPS edge only when remote access is required

Faryo does not modify Conda base or install Python packages into the operating
system interpreter. The installer uses Ubuntu's compatible `python3` only to
create an isolated, version-specific private venv below
`~/.local/share/faryo/`. Faryo itself does not require a separate production
Node/npm installation; when Codex was installed through npm, automatic updates
reuse the Node/npm shipped beside that discovered Codex launcher. Git, Ruff,
Playwright, and the root Node toolchain remain development requirements.

For a tagged release, download the reviewed installer and its checksum from the
same GitHub Release, verify it, then run it from the directory that should be the
initial allowed workspace:

```bash
sha256sum --check install-faryo.sh.sha256
bash install-faryo.sh --version v1.10.3 --workspace "$PWD"
```

Upgrading a pre-v1.5 checkout that still uses the dedicated Owner keepalive
supervisor requires the explicit `--migrate-owner` flag; normal Codex tmux
sessions are preserved and geometry-checked.

The installer verifies a versioned source archive and SHA-256 manifest, creates
the private venv, initializes missing mode-`600` configuration, and installs
three user services: private App Server, loopback Owner, and loopback Gateway.
Only Owner and Gateway listen on TCP. Existing config, tokens, attachments,
Codex history, and tmux sessions are preserved.

Developers can install a clean checkout through the same application path:

```bash
git clone https://github.com/SongJunguo/faryo-codex-web-ui.git
cd faryo-codex-web-ui
PYTHONPATH=src /usr/bin/python3 -m faryo_cli install --workspace "$PWD"
```

Everyday operation uses one command:

```bash
faryo doctor
faryo status
faryo start
faryo open
faryo logs appserver
faryo logs owner
faryo logs gateway
faryo restart
```

Updates are checksum-verified and health-gated. Each version keeps an independent
venv; failed switches restore the previous application and service units:

```bash
faryo update
faryo rollback
```

Mutable Owner assets use the active Faryo release as their cache key; Gateway
assets use an automatic content hash, and rejected assets are `no-store`. A
normal reload or newly opened tab is sufficient after an update; users should
never need a browser-specific hard-refresh gesture.

`faryo restart` leaves the independent App Server process running while Owner
and Gateway restart, preserving an active Codex App Server turn. `faryo stop` stops
all three Faryo services; it preserves tmux and Codex history but should not be
used during an active Codex App Server turn. `faryo uninstall` removes services and
versioned program files but preserves `~/.faryo`; irreversible private-data
removal requires both `--purge-data` and `--yes`.

Fresh installation stores the initial local login password at
`~/.faryo/gateway/config/initial-password`. Change it from the password page and
remove the stale plaintext file after confirming the new login. See
[Local installation and lifecycle](docs/local-installation.md) for the complete
layout, troubleshooting, update, rollback, and uninstall contract.

Remote access is intentionally separate. Follow the [Gateway runbook](apps/gateway/runbook.md)
and [security hardening guide](docs/gateway-security-hardening.md). A tunnel is
transport only; protect the complete hostname with an exact-identity access
layer while keeping Faryo's inner login enabled.

## Current Validation

The `main` branch was revalidated on 2026-08-22 with privacy-safe fixtures:

- canonical source gate: 225 Owner, 120 Gateway, and 65 unified-CLI Python tests,
  plus Ruff, ESLint, Prettier, TypeScript and reproducible local browser bundles;
- isolated real Codex App Server: initialize/capability fallback, body-delta
  streaming, keyed final convergence, Markdown/KaTeX/code, body-free replay
  journal, JSONL recovery, ordinary reload, active-turn Owner restart, a real
  sandbox approval, and a real plan-mode `request_user_input` round trip;
- real authenticated Gateway at 390x844: `/model`, pending-menu reload, Cancel,
  `/usage`, Preact composer geometry and no fake chat turn;
- stale interaction/session responses, duplicate request IDs, generation 409,
  CSRF, Owner token and body-free audit boundaries;
- explicit New/Resume App Server or TUI selection, atomic single-writer 409,
  authoritative Details labels, version-bound busy slash commands and stale
  status-generation rejection;
- real asynchronous Start: about 51ms accepted Starting response, duplicate
  launch reuse, Starting→Waiting browser transition, Owner-restart monitor
  recovery and unchanged pane identity;
- real queued follow-up Send now: mobile squirrel ESC badge, desktop physical
  Escape, expedited API receipt and an observed Escape in the synthetic Codex
  TUI; editable queue management remains unsupported;
- active legacy thread-ID URL recovery, signed missing-cwd resume selection,
  hidden-folder toggle, Git worktree status and server-paginated history search;
- 20-message idempotent delivery including Chinese, Markdown/TeX, attachment,
  offline/background/504 recovery, failed drafts and cross-session isolation;
- stalled-SSE heartbeat recovery, deduplicated safety capture and automatic
  pageshow/focus/online live-stream restoration;
- local Markdown AST/GFM/KaTeX/Shiki, 40-turn lazy history, full question index,
  exact Markdown/TeX copy, Raw→Chat isolation and 180-line stable Live tmux;
- 390x844 and 1440x900 layout, native-resize keyboard app shell, one conversation
  scrollport, transparent Grid-anchored composer with a measured tail reserve,
  PWA/fullscreen, read-only sanitized diff and protected-resource checks;
- release-version cache keys and Gateway script allowlist completeness: ordinary
  reload/new tab loads the current app without hard refresh;
- pinned GitHub Actions, Python/JavaScript CodeQL, weekly grouped dependency
  proposals, and explicit Ubuntu 22.04/Python 3.10 plus Ubuntu 24.04/Python 3.13
  source lanes;
- checksum update/rollback gates with `KillMode=process`; every deployed preview
  preserved all pre-existing tmux session names, pane PIDs and 145x44 geometry.

Run the canonical source checks:

```bash
scripts/check-source.sh
```

The script discovers the active Conda environment, the named `faryo` Conda
environment, and an installed NVM Node without requiring shell startup files.
For CI or another explicit runtime, use:

```bash
FARYO_PYTHON=/path/to/python \
FARYO_NODE_BIN=/path/to/node \
  scripts/check-source.sh
```

This single command validates source syntax, browser bundles, all maintained
Owner and Gateway tests, required runtime assets, and the absence of retired
compatibility paths. Pull requests and pushes to `main` run the same source-only
check; tagged releases publish a checksummed source archive, reviewed bootstrap
script, and release notes, not unmaintained binary packages.

## Security Boundary

Faryo can steer Codex with the permissions of the operating-system user running
Owner. Treat it as a remote administration surface.

- Bind Owner and Gateway only to loopback/private interfaces.
- Never expose Owner directly to the Internet.
- Put identity-aware access in front of the complete Gateway hostname.
- Use exact identities or a small managed group; configure no broad bypass.
- Keep the inner Faryo password/Cookie layer enabled.
- Treat Access session duration/MFA and the inner Faryo Cookie as independent,
  operator-selected controls.
- Use OS, VM, or container isolation when the Codex user must not read personal
  browser, SSH, Git, or other workstation data.
- Keep runtime secrets and private conversations outside Git and logs.

See [SECURITY.md](SECURITY.md) and
[Gateway Security Hardening](docs/gateway-security-hardening.md).

## Repository Layout

```text
apps/owner/         Loopback ASGI endpoint, App Server actors and tmux adapter
apps/gateway/       Authenticated routing and session/history workbench
apps/shared/        Shared state and browser appearance helpers
docs/               Security, deployment, and implementation plans
deploy/             App Server, Owner and Gateway user-service templates
scripts/            Source checks and maintenance scripts
tools/              Browser harness and reproducible frontend bundle builders
```

## Documentation

- [Owner runtime and Compact Chat](apps/owner/local-tmux-owner/README.md)
- [Gateway setup](apps/gateway/README.md)
- [Gateway runbook](apps/gateway/runbook.md)
- [Gateway security hardening](docs/gateway-security-hardening.md)
- [Current UI interaction contract](docs/ui-interaction.md)
- [Codebase architecture and similar-project benchmark](docs/codebase-architecture-and-product-benchmark.md)
- [Dependency ledger](docs/dependencies.md)
- [Implementation plans and validation evidence](docs/plans/README.md)
- [Current UI plan and evidence](docs/plans/deepseek-inspired-ui-plan.md)
- [Codex reliability plan and evidence](docs/plans/codex-reliability-hardening-plan.md)
- [Codebase cleanup scope and evidence](docs/plans/codebase-cleanup-plan.md)
- [Personal fork roadmap](docs/plans/personal-fork-roadmap.md)

## Inspiration and Acknowledgements

Faryo's modern agent-workbench direction was inspired in part by
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness). Its
math-delimiter and CJK-friendly Markdown compatibility extensions are adapted
from [a pinned DeepSeek Harness revision](https://github.com/deepseek-ai/deepseek-harness/tree/47f943859bef60e4160492346772ded9b24f765a)
under the MIT License.

Faryo remains an independent project and is not affiliated with or endorsed by
DeepSeek. No DeepSeek branding or product assets are included. Complete
attribution and license information is available in the
[bundled third-party notices](apps/owner/local-tmux-owner/static/vendor/markdown-ast/THIRD_PARTY_NOTICES.md).

## Upstream and License

The original Faryo history was published as `Snailflyer/faryo`; its preserved
baseline is linked in Project Status above. This standalone project keeps the
original URL as a non-pushable local `upstream` remote for attribution and
comparison, but pushes maintained changes only to
[SongJunguo/faryo-codex-web-ui](https://github.com/SongJunguo/faryo-codex-web-ui).

Faryo is released under the MIT License. See [LICENSE](LICENSE).
