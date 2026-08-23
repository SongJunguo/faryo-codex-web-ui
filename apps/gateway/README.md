# Faryo Gateway

Faryo Gateway is the public gateway component. It handles the public web entry,
login, route authorization, the handoff workbench, and proxying to available
local execution surfaces.

The maintained fork validates one Ubuntu/Linux Codex route. Multi-route support
remains available in the implementation, but additional endpoint types are not
part of the current acceptance matrix.

## Runtime Boundary

Gateway does not own the local Codex/App Server or tmux execution surface. It
only routes to Owner components through configured loopback ports or reverse
tunnels.

For a single-machine deployment, both HTTP services stay on loopback:

```text
public HTTPS edge -> 127.0.0.1:8780 Gateway -> 127.0.0.1:8765 Owner
                                                    |
                                                    `-> private App Server Unix socket
```

Gateway performs browser login and injects the Owner token while proxying. The
Owner token must never be placed in a public URL or browser configuration.

Runtime configuration defaults to:

```text
~/.faryo/gateway/config/faryo.env
~/.faryo/gateway/config/gateway-auth.json
~/.faryo/gateway/state/gateway-cookie-secret
```

## Local Service

```bash
faryo status
faryo restart
faryo logs gateway
```

The user-level service template lives at:

```text
deploy/user-systemd/faryo-gateway.service
```

For a private, single-route installation from a clean repository root:

```bash
PYTHONPATH=src /usr/bin/python3 -m faryo_cli install --workspace "$PWD"
faryo doctor
```

The installer creates an exact-pinned private venv, reads or creates the private
Owner token, writes missing mode-`600` Gateway files below `~/.faryo`, and only
requires a token for the enabled route. Re-running it preserves an existing
login config. Low-level scripts remain for development and credential repair,
but are not the ordinary deployment interface.

The inner Faryo login defaults to a 30-day absolute session. Set
`FARYO_GATEWAY_SESSION_HOURS` in the private Gateway environment to an integer
from 1 through 720 when a different operator-selected lifetime is required.
Changing it does not alter Cloudflare Access sessions; the two layers are
configured independently.

`server/gateway_security.py` is the single policy implementation for signed
cookies, epoch revoke, CSRF, trusted login-rate keys, safe redirects, CSP and
browser hardening headers. It is independent from Starlette route adapters.

The v1.4 development tree runs `server/asgi_app.py` through Uvicorn. Explicit
ASGI contracts cover login, writes, proxying, SSE, uploads, MCP and fallback
routes. The previous `http.server` shell and migration switch have been removed.

`server/owner_client.py` centralizes Owner Token, route label, user,
history-scope, workspace and inbox headers plus JSON and multipart requests.
All ASGI adapters delegate to this client.

Gateway serves one root-scoped installable PWA manifest with `display:
standalone`. The home page exposes the browser install prompt when available,
and every maintained Owner session page references the same manifest. Launching
the installed app removes the ordinary URL bar while keeping Faryo's own Home
navigation and authentication boundaries.

Running-session limits are independent from history display. Configure them per
enabled route with `FARYO_TXY_MAX_RUNNING`, `FARYO_HP_MAX_RUNNING`, or
`FARYO_PC_MAX_RUNNING` (valid range `1`–`32`). Defaults are 8 for TXY and 4 for
HP/PC.

The workbench keeps live agents and resumable history separate. `Active
Sessions` includes every tmux pane currently running a recognized Codex process,
including panes started directly on the endpoint. Only sessions
created and stamped by Faryo expose the remote `Close` action; externally
started desktop tmux sessions remain openable but protected from remote close.
Cards use explicit lifecycle states: Starting, Running, Waiting, Exited,
Desktop, Resumable, and Archived. Detection follows the pane process tree and
Codex input readiness rather than trusting `pane_current_command`; a managed
shell whose Codex child exited remains visible briefly with a safe Close action.
`Session History` excludes those live sessions, scrolls independently, and uses
server-backed pagination with 10 records per page. Use Previous/Next or enter a
page number and press Enter/Go to jump directly through long histories. Search
matches only normalized session titles, explicit Codex rename metadata, and the
working-folder basename. Date and archive chips filter on Codex metadata; they
do not read conversation messages or rollout files. Filters are reflected in
the current URL for refresh/navigation but are not written to browser storage.
Resumable cards can be archived and archived cards restored. Gateway applies
login, route authorization and CSRF, then Owner invokes Codex App Server's
official thread lifecycle RPC. Faryo does not move rollout files, edit Codex
SQLite metadata, or expose thread hard deletion.

`/` is the single maintained Gateway home. The earlier `/projects` Project
Orchestration surface and its dedicated controller/downlink backend are retired;
authenticated requests to that old route return `404`. The generic handoff
package and Files-to-session flow remain available from the main workbench.

The workbench portal loads versioned `workbench.css`, `workbench.js` and a local
Preact card-list bundle; Python renders only the authenticated HTML shell and a
nonce-protected JSON map of allowed route labels. Preact is deliberately limited
to keyed file-package, launcher, active-session and history-session lists. The
generic sheet, directory picker, Attention controller, Owner transcript and
Markdown/TeX pipeline remain outside the framework. The generated bundle is
exact-pinned, hash/size/licence checked, has no production transitive dependency
and never loads from a CDN. See the
[pilot evaluation](../../docs/preact-pilot-evaluation.md).

The page keeps an in-memory Attention center for Waiting/Exited sessions.
Optional browser notifications require a direct user permission gesture, work
only while the page/PWA is open, and use a generic body without message, title,
path or raw identifier data.

The Settings menu separates `Sign out this device` from `Revoke signed-in
devices`; revocation invalidates every inner Faryo cookie for that account but
does not stop Codex or close tmux. `Security activity` shows recent body-free
control metadata from the private mode-600 audit. Targets are HMAC aliases;
message text, titles, paths, raw session IDs, tokens and client IP history are
not recorded.

`Start Codex` opens a dedicated launch-options picker. The picker defaults to
the latest eligible cwd, deduplicates shortcuts within Recent while keeping the
complete canonical child list in Folders. A real child remains in Folders even
when the same canonical path is also a configured Root/Location/Recent entry;
there is no automatic child-count cap. The remembered Hidden toggle is the only
automatic dot-directory visibility rule. The picker
uses `..` as the first Folders row for parent navigation, collapses long
breadcrumbs, filters the current page without recursive search, and keeps
`Start Codex here` fixed outside the scrolling list. On phone-sized viewports,
the backend and context controls start inside a compact `Session settings`
disclosure whose summary always shows the effective choices. The folder list
therefore owns the remaining height; opening the disclosure remains an explicit
way to change either setting. Desktop keeps the full controls visible.
Directory choices still come from Owner, carry its HMAC selection token, and
are revalidated by Owner before the Web-managed session starts. The same sheet offers Default,
372K, 1M and a bounded
custom K-token context window. Default sends no override. Custom values are
validated independently by Gateway and Owner, then become one-off Codex
`model_context_window` and 90% auto-compaction overrides. History-card body
clicks remain the zero-dialog default resume path; `Options` exposes the same
folder/context controls for an explicit resume.

The launcher is health-aware. Owner returns only a body-free App Server state,
and Gateway shows the launcher as ready, reconnecting or unavailable instead of
leaving the server-rendered `Loading launchers…` placeholder indefinitely. An
older Owner that does not yet publish this field remains usable during a rolling
upgrade; explicit `false` is the only value that disables Web-managed launch.

See [runbook.md](runbook.md) for Cloudflare Tunnel, first login, verification,
and rollback instructions. Internet-facing deployments that can steer agents
must also follow the [Gateway security hardening](../../docs/gateway-security-hardening.md)
checklist; a tunnel alone is not an authentication layer.
