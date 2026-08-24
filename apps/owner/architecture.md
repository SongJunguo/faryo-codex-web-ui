# Faryo Owner Architecture

Faryo Owner is the local Codex execution layer. It owns the private Codex App
Server connection and the compatibility control surface for existing Codex TUI
sessions in `tmux`. It does not own the public entry point, account login, path
routing, or Cloudflare Access.

`Owner` is an internal component name. It is not the Faryo product name or a
public brand surface.

## 1. Local Execution Flow

```text
Faryo Gateway
  -> route port or SSH reverse tunnel
  -> local execution endpoint 127.0.0.1:8765
  -> faryo-owner.service
     -> private Codex App Server socket -> Codex thread (default)
     -> tmux target session -> Codex TUI (compatibility)
```

The phone should not access Owner directly. Owner tokens should be injected by
Gateway or used only for local smoke/status checks.

## 2. Local Runtime

- `local-tmux-owner`: historical source-directory name for the local execution
  backend, bound to `127.0.0.1`; it no longer implies a service tmux session.
- `faryo-owner.service`: direct systemd user supervision for the Owner Python
  process; it does not run in a service tmux. Its `KillMode=process` contract is
  mandatory: restarting or updating the Web bridge must not signal tmux/Codex
  descendants that were originally launched from Owner.
- A newly created Codex is not declared ready on its first transient composer
  frame. Owner requires a continuous ready interval so the later MCP-startup
  phase can reset the gate; a recognized startup interaction remains available
  immediately. Existing idempotent launch IDs keep their non-blocking reuse
  contract.
- An optional SSH reverse tunnel from a local endpoint to the Gateway host, run
  as a user service on that endpoint. When enabled, verify from the Gateway side
  with the owner token and expected endpoint identity.
- `tmux:<target>`: the controlled target session.

The former `faryo-owner-keepalive.timer` and `tmux:local-tmux-owner` supervision
are retained only as a one-release migration fallback. They are absent from the
maintained runtime after successful installation.

## 3. Required Dependencies

- `tmux`: compatibility backend for terminal TUI sessions and existing desktop
  workflows. Codex App Server sessions do not create a tmux pane.
- `curl`: health, smoke, and status checks.
- `openssh-client`: required only when this endpoint establishes a reverse
  tunnel to the Gateway host.

Owner does not require inbound SSH and should not bind directly to public or
LAN addresses.

## 4. Product Data Directory

Default product data root:

```text
~/.faryo/owner/data/
  inbox/
  artifacts/
  cache/
  logs/
```

Private control state lives beside the data root under `~/.faryo/owner/state/`.
`command-timeline.json` contains only bounded browser-issued command lifecycle
metadata, uses mode `0600` below a `0700` directory, and is not a second Codex
transcript.

The workspace is the terminal command working directory. It is not the Faryo
product data root. Gateway may inject user/route-specific upload destinations,
but the default upload destination should come from the Faryo data directory.

## 5. Governance Parameters

- Owner bind: `127.0.0.1:8765`.
- Web capture: compact/full requests use 320/800-line soft targets. Structured
  Codex history may exceed them to retain complete recent turns, while separate
  tail and character ceilings keep the payload bounded.
- tmux history limit: default 500 lines.
- Token: private runtime config, never committed to Git.
- Product data root: default `~/.faryo/owner/data`.
- Workspace Changes is a separate read-only module: Git root resolution is
  scoped, commands are fixed/bounded, and returned paths are relative.
- Attachment storage is a separate bounded module: magic/MIME/suffix policy,
  upload size, generated names and dated retention do not depend on the HTTP
  handler.
- Local-file and start-directory policy is a separate module: configured roots,
  suffix allowlists, symlink resolution, directory limits and selection tokens
  are tested without tmux or HTTP globals.
- The read-only Changes browser controller is a native ES module with injected
  API/session/panel dependencies; `app.js` remains the composition root and the
  diff renderer stays a lazy local asset.
- `static/owner/api-client.mjs` is the native HTTP boundary for route-local API
  paths, Owner headers, cached Gateway CSRF, JSON/FormData request policy and
  bounded non-JSON errors. SSE/rendering state remains in the composition root.
- `static/owner/attachment-controller.mjs` owns the 35-file queue, four-way
  compression/upload pool, progress thumbnails, cancel/remove, clipboard paste
  and drag/drop. Its bounded horizontal strip never widens the page.
- `static/owner/history-controller.mjs` owns revision-bound turn maps, question
  indexes, latest/around/cursor loads, 409 reset, refresh debounce and near-top
  older-page loading. DOM anchor capture and rendering remain injected actions.
- `static/owner/capture-controller.mjs` owns capture request cancellation,
  coalescing, authenticated SSE parsing, heartbeat timeout, reconnect backoff,
  deduplicated safety/fallback polling and Raw refresh timers. Capture rendering
  and scroll decisions remain callbacks.
- `static/owner/composer-delivery.mjs` owns route/session-scoped drafts, pending
  message identity, success-only clearing, failed-draft restoration and one
  same-ID retry for ambiguous network/502/504 outcomes. DOM/animation stay out.
- `static/owner/goal-status.mjs` maps structured Codex goal states to the header
  pill and Details row. Routine status receives only status/timing metadata;
  objective text is fetched separately through authenticated `/api/goal`, is
  marked no-store, and is cleared from the DOM when Details closes.
- `codex_tui_interactions.py` contains pure, side-effect-free detectors for the
  current model, reasoning, usage, permissions, resume-directory, trust,
  approval and generic blocking-menu shapes. It never calls tmux or HTTP.
- `interaction_service.py` owns per-session serialization, opaque interaction
  and option IDs, monotonic generations, stale-response rejection, idempotent
  command/action receipts, and the only conversion from validated actions to
  TUI keys. `/model`, `/usage`, and future catalog commands do not pass through
  ordinary message delivery.
- `command_timeline.py` owns the shared App Server/TUI presentation lifecycle
  for browser-issued mutating slash commands. It records only command name,
  safe type-specific display metadata, state, timing and an optional hashed
  turn anchor; Goal objectives, messages, credentials and free-form unknown
  arguments are excluded. Interactive stages keep one event id until resolved.
- `apps/owner/ui/` is the readable Preact + strict TypeScript source for the
  composer, command palette, structured interaction sheet, error boundary and
  dynamic Context/Week/Model/Goal/Git shell. Its framework-neutral
  `ConversationStore` fences session/mode generations, while `TranscriptShell`
  owns loading, startup, empty, terminal-fallback and render-error states. Vite
  produces one checked-in local bundle; `app.js` remains the composition adapter
  for Markdown/TeX bodies, Raw, Live tmux and paged-history islands rather than a
  second renderer.
- `owner_http.py` owns browser security headers, query-redacted log paths,
  Owner-token validation, bounded JSON/multipart parsing, gzip JSON and file
  byte responses. The Handler delegates these primitives and keeps routing.
- `appserver_runtime.py` owns the production Unix-socket client and the bounded
  compatibility RPC allowlist used by history, goal and rate-limit reads.
  `codex_app_server.py` retains the serialized stdio fallback only for
  standalone callers outside the ASGI composition root.
- `appserver_session.py` projects official tool item types and states into a
  bounded browser activity envelope. Full command output, tool results and file
  diffs stay out of capture/history/SSE and are projected only by the
  authenticated, no-store `/api/activity-detail` route. `appserver_rollout.py`
  provides the same bounded single-item fallback after reconnect, while hidden
  reasoning remains unavailable.
- Low-level command execution and tmux/process-tree/identifier primitives are
  isolated in `tmux_runtime.py`; higher services keep policy and translate
  failures rather than rebuilding subprocess defaults.
- Reliable-send durable checkpoints are isolated in `delivery_store.py`; it
  enforces ID bounds, privacy-minimal records, atomic fsync, 0700/0600 modes,
  TTL cleanup and corrupt/symlink rejection. `delivery_service.py` owns the
  reference-counted session/message locks, in-memory checkpoints, paste,
  Tab/Enter confirmation, retry and ambiguity policy through an explicit
  runtime adapter; HTTP only translates its bounded result or error.
- Pure Codex message extraction, complete-turn budgeting, previews and
  revision-bound cursors are isolated in `codex_history.py`. The same bounded
  incremental rollout path recognizes `thread_goal_updated` and verified goal
  tool results, retaining only status/timing metadata; incremental file indexing
  and caches remain higher services until their state boundary is explicit.
- Capability and diagnostics payloads use an explicit allowlist and counts; they
  never expose private runtime configuration.
- Upstream control headers: use Faryo header names.

## 6. Verification

```bash
faryo doctor
faryo status
./scripts/smoke-test.sh
./scripts/verify-reverse-tunnel.sh
ss -ltnp | grep ':22 ' || true
```

Expected:

- Owner health is OK.
- Smoke test passes.
- Owner listens only on loopback.
- If this endpoint uses a reverse tunnel, its tunnel service is active.
- If this endpoint uses a reverse tunnel, `verify-reverse-tunnel.sh` can
  read Owner `/api/status` from the Gateway side and validate owner label and
  session.
- No unauthorized inbound `:22` listener is present.

## 7. Non-Goals

- Owner does not host Faryo Gateway.
- Owner does not provide a public login page.
- Owner does not commit tokens, password hashes, or runtime secrets.
- Owner does not expose arbitrary launch commands; the maintained launcher is
  the explicitly configured Codex runtime.
- A bounded per-launch context value is converted to fixed Codex configuration
  keys before process creation. It never edits the user config and cannot carry
  arbitrary command-line arguments.
- Owner scrubs Faryo/Gateway service-only values from persistent tmux globals at
  startup and before managed launches. Codex checks, update preflight and App
  Server processes use the same sanitized environment; unrelated user paths are
  retained.
