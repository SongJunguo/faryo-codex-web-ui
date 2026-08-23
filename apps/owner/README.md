# Faryo Owner

Faryo Owner is the local execution component. It exposes a loopback-only
Starlette/Uvicorn control surface and is reached through Faryo Gateway or a
configured reverse tunnel. New Web-managed sessions use the official Codex App
Server over a private Unix WebSocket; existing tmux-backed sessions remain an
explicit terminal-managed compatibility path.

The maintained fork validates Owner against an existing Ubuntu/Linux Codex TUI.
Other inherited terminal profiles are compatibility paths, not current support
claims.

## Runtime Boundary

Owner does not own public login, domain routing, Caddy, or the Gateway
workbench. It should bind only to `127.0.0.1`. The App Server socket is local
filesystem state and is never routed by Gateway.

Runtime configuration defaults to:

```text
~/.faryo/owner/config/faryo.env
~/.faryo/owner/data/
```

## Local Service

```bash
faryo status
faryo restart
faryo logs appserver
faryo logs owner
```

The unified installer creates and manages `faryo-appserver.service` plus direct
`faryo-owner.service`, and preserves the Owner token, Web-session registry and
explicit start roots. An Owner restart leaves App Server running so an active
turn can continue and reconnect. The old
`local-tmux-owner` service tmux plus keepalive timer is a rollback-only v1.4
compatibility path, not the maintained production supervisor.

Each installed version pins `FARYO_PYTHON` to its private standard venv. Ordinary
users do not need to select Conda or edit the Python path.

Owner does not resize tmux windows by default, so terminal UIs wrap at the
dimensions selected by real tmux clients. The server's positive
`--pane-width` option is an explicit compatibility opt-in for terminal-only
capture; it is never applied to a running Codex TUI.

Use `/health` for liveness and `/api/status` for authenticated runtime checks.
The status payload includes the source version for deployment acceptance.
`/api/capabilities` exposes only body-free protocol/runtime health; it never
includes a prompt, answer, cwd, token or private socket path.

## Internal architecture

Starlette routes and Uvicorn own only HTTP parsing, authentication middleware,
SSE response lifetime, static files and process lifespan. The Codex protocol
client, single-writer `WebSessionActor`, bounded replay journal, request broker,
registry and history projection are framework-neutral Python modules. This
keeps the hard state semantics independently testable and avoids binding
conversation logic to a Web framework.

Web-managed sessions receive stable `thread`/`turn`/`item` notifications from
the persistent App Server service. Agent-message deltas update one keyed item;
the final item replaces that same slot and later converges to Codex rollout
JSONL. Empty private-reasoning placeholders are not projected as messages.
Commands, searches and file changes remain inspectable. Each user message owns
its following output even when Codex retains one long protocol turn, and
contiguous activity batches stay at their chronological position instead of
being hoisted into one old card. Every batch defaults collapsed, including
active and failed work; only an explicit user click mounts its details. The
replay journal is count- and byte-bounded and stores control metadata, not a
second persistent copy of conversation bodies.
At either end of an expanded Activity list, continued wheel or touch scrolling
chains naturally into the surrounding conversation history.

The old `ThreadingHTTPServer` production entry has been removed. The remaining
synchronous `codex_app_server.py` helper is intentionally restricted to
terminal-managed compatibility operations such as reading or archiving an
existing TUI-owned thread; it is not a second writer for Web-managed sessions.

The session page participates in Gateway's root-scoped standalone PWA and also
offers a user-activated Fullscreen API mode. Browser chrome is never hidden
without an explicit tap; expanded and collapsed headers both retain a visible
exit path. Fullscreen state is browser-only and does not touch tmux geometry.

Inactive Codex history can be archived or restored through the authenticated
Owner lifecycle endpoints. Owner delegates those changes to Codex App Server's
`thread/archive` and `thread/unarchive`; it does not directly edit Codex state
or expose hard deletion.

Owner also exposes versioned capability metadata, redacted count-only
diagnostics, and a bounded read-only Git changes endpoint for the selected
session. The Git root must remain inside the Gateway workspace scope; fixed
commands disable external diff, textconv, pager and color. No endpoint can
stage, discard, commit, checkout or apply changes.

## Joining Gateway

Installing Owner only proves local runtime health. Gateway visibility also needs
route config, a matching Owner token, any required reverse tunnel loopback port,
and the workspace/file-inbox roots used by that route.

After configuration, run `faryo doctor`; the legacy endpoint diagnostic remains
available for advanced reverse-tunnel troubleshooting. See `runbook.md` for the
layered acceptance flow.
