# Local Installation and Lifecycle

Faryo's maintained production path is Ubuntu/Linux, tmux, and Codex CLI. The
operator uses one `faryo` command; Conda, pip requirements, service templates,
ports, and helper scripts are implementation details.

## Runtime shape

```text
systemd --user
├── faryo-appserver.service  read-only Codex control plane on a private Unix socket
├── faryo-appserver-worker@<opaque>.service
│                             one official App Server per structured Web session
├── faryo-owner.service      Starlette/Uvicorn control API on 127.0.0.1:8765
└── faryo-gateway.service    authenticated browser UI on 127.0.0.1:8780

tmux
└── existing sessions        Codex TUI (tmux) compatibility backend
```

Owner is the private workstation-side service that reads Codex history and
coordinates App Server session actors, SSE, attachments and the tmux adapter.
Gateway is the browser-facing login, navigation, and reverse-proxy layer. The
two Web services use separate ports so the privileged local control API never
needs to become the public entry. Both bind only to loopback by default. App
Server exposes no TCP port; Owner reaches mode-`700` Unix sockets locally. The
control process performs only account, model, history and lifecycle reads; it
never resumes a Faryo Web thread.

Restarting Owner or Gateway does not stop the control plane, session workers or
resize Codex tmux sessions. `faryo restart` deliberately keeps workers alive, so
an active Codex App Server turn can continue while the Web layers update.
`faryo stop` also stops every worker and therefore should not be used during an
active App Server turn; it still preserves rollout history, registry data and
every tmux session.

## Requirements

- Python 3.10+ with `venv` and ensurepip
- tmux, curl, systemd user services, and Codex CLI
- a current Chromium-family browser

Python 3.10 is the compatibility floor because current Starlette, Uvicorn,
AnyIO, and Click pins support it and Faryo uses Python 3.10 union type syntax.
Python 3.9 and older would require dependency and source compatibility branches.
Python 3.11+ uses `tomllib`; Python 3.10 receives the exact-pinned `tomli`
backport. Faryo does not use uv and does not modify system Python or Conda base.

## Verified release installation

Download these two assets from the same tagged GitHub Release:

- `install-faryo.sh`
- `install-faryo.sh.sha256`

Review and verify the script before executing it:

```bash
sha256sum --check install-faryo.sh.sha256
less install-faryo.sh
bash install-faryo.sh --version v1.12.0 --workspace /path/to/workspace
```

The script then downloads `faryo-v1.12.0.tar.gz` and its checksum, accepts only a
bounded single-root regular-file archive, and invokes the same `faryo install`
path used by source developers. It does not execute sudo, install apt packages,
create a tunnel, or change Cloudflare settings.

The generated `~/.local/bin/faryo` entry uses the selected private Python in
isolated mode. It ignores ambient `PYTHONPATH`/`PYTHONHOME`, and installation
health requires the CLI to report the exact version being prepared.

When upgrading a pre-v1.5 deployment that still has the dedicated
`local-tmux-owner` service session/keepalive timer, explicitly approve only that
supervisor migration:

```bash
bash install-faryo.sh --version v1.12.0 --workspace /path/to/workspace --migrate-owner
```

The migration records and compares every existing agent tmux geometry. It stops
only the named legacy Owner service session, never `faryo1`, `faryo2`, or other
Codex sessions, and restores the old supervisor if health checks fail.

If `/usr/bin/python3` is not compatible, select another existing interpreter:

```bash
bash install-faryo.sh --python /path/to/python3.13 --workspace /path/to/workspace
```

## Codex discovery and automatic updates

Faryo resolves Codex again for every managed new or resumed session. NVM's
current `default` alias is preferred, including recursive `lts/*` aliases. This
lets a future NVM default or Node version replace an older installation without
editing Faryo's private config. A legacy `FARYO_CODEX_BIN` remains only as a
fallback unless the operator deliberately adds:

```bash
FARYO_CODEX_BIN_PINNED=1
```

Before the TUI starts, Faryo checks for an official Codex update. For an
npm-based Codex it reuses the npm beside the dynamically discovered Node,
updates only `@openai/codex`, verifies the resulting version, and then starts a
fresh process. The check is serialized across simultaneous launches and cached
for one hour in a mode-600 state file. A failed or timed-out update does not
abort the requested conversation.

To opt out and retain Codex's own interactive update behaviour, add this to the
private Owner environment and restart Owner:

```bash
FARYO_CODEX_AUTO_UPDATE=0
systemctl --user restart faryo-owner.service
```

Do not add a fixed NVM path unless pinning is intentional.

## Installed state

```text
~/.local/bin/faryo
~/.local/share/faryo/
├── current -> versions/<active-version>
├── versions/<version>/
│   ├── app/
│   ├── .venv/
│   └── install-manifest.json
└── state/

~/.config/systemd/user/
├── faryo-appserver.service
├── faryo-appserver-worker@.service
├── faryo-owner.service
└── faryo-gateway.service

~/.faryo/                     persistent private state
├── owner/config + data
└── gateway/config + state
```

Each service unit pins the exact active version directory. Update and rollback
atomically change `current`, rewrite the fixed units and worker template,
preserve running workers where possible, restart the Web layers, and pass a
health gate. This
avoids a half-written symlink or package update changing a running service
unexpectedly.

The first upgrade from the shared v1.11.x App Server topology is deliberately
deferred while a structured Web turn or interaction is active. When idle, the
installer stops Owner, restarts the old shared process once to release its
writers, starts Owner on the per-session topology, and verifies health and tmux
process identity. A failed migration restores the previous registry schema,
unit files and services. Rolling back to v1.11.x follows the inverse idle-only
transition and stops only exact validated Faryo worker unit names.

The private venv contains the installed Faryo package, so service units do not
export a source `PYTHONPATH`. Owner also removes service-only Faryo/Gateway
values before starting or resuming a managed Codex process.

Program versions are replaceable. `~/.faryo` is not: it contains tokens, login
state, attachment data, delivery metadata, and other private runtime state.

## Everyday commands

```bash
faryo doctor              # read-only dependency, permission, bind, and health checks
faryo status --json       # privacy-safe machine-readable service summary
faryo start               # start control plane and both Web services; restore workers
faryo stop                # stop Faryo services/workers; preserve history and all tmux sessions
faryo restart             # keep workers alive; restart and check both Web services
faryo open                # open the loopback Gateway without exposing Owner token
faryo logs appserver      # bounded private-runtime journal
faryo logs owner          # bounded user journal
faryo logs gateway
```

The diagnostic JSON intentionally excludes paths, email addresses, domains,
tokens, session names, prompts, and conversation content.

## Update and rollback

```bash
faryo update                    # latest stable release
faryo update --version v1.12.0  # exact release
faryo rollback                  # previous healthy installed version
```

Update downloads only approved GitHub HTTPS assets, enforces compressed and
extracted size bounds, verifies the exact asset name and SHA-256, validates
release/package versions, builds an independent private venv, and switches only
after preparation. Service health failure restores config, links, units, and the
previous services. It never rolls back private conversation or attachment data.

For a reviewed offline asset:

```bash
faryo update --version v1.12.0 \
  --archive ./faryo-v1.12.0.tar.gz \
  --checksum ./faryo-v1.12.0.tar.gz.sha256
```

## Uninstall

```bash
faryo uninstall
```

This disables and removes only Faryo's exact user units and versioned program
directory. It leaves `~/.faryo` and all ordinary Codex tmux sessions intact.

Private data deletion is deliberately harder and cannot be inferred from a
normal uninstall:

```bash
faryo uninstall --purge-data --yes
```

This is irreversible. Back up anything needed from `~/.faryo` first.

## First login and remote access

The fresh local username is `faryo`; its generated password is stored in the
mode-`600` file `~/.faryo/gateway/config/initial-password`. Change it from
`/password`, verify the replacement, then remove the stale plaintext file.

Local installation creates no public ingress. Remote access remains a separate,
explicit operation described in the [Gateway runbook](../apps/gateway/runbook.md)
and [security guide](gateway-security-hardening.md). Never expose Owner directly.
