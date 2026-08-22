"""Read-only Owner routes shared by Codex TUI and App Server sessions."""

from __future__ import annotations

import html
from http import HTTPStatus
import json
import mimetypes
from typing import Any
from urllib.parse import urlencode

from anyio import to_thread
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

import codex_command_policy
import codex_history
import appserver_runtime
import runtime_diagnostics
import workspace_changes

def terminal_capture_response(core: Any, target: Any, lines: int, want_html: bool) -> dict[str, Any]:
    core.ensure_pane_width(target)
    profile = core.agent_profile_in_pane(target) or core.RUNTIME_PROFILE
    text = core.capture_text(target, lines, profile)
    terminal_text = text
    agent_running = bool(profile is not core.RUNTIME_PROFILE and not core.agent_ready_for_input(target, profile))
    capture_source = "tmux"
    thread_id = ""
    live_text = ""
    if profile is core.CODEX_PROFILE and not want_html:
        structured = core.codex_structured_capture(target, lines)
        if structured:
            text, thread_id, capture_source = structured
            if agent_running:
                live_text = core.codex_live_tail(terminal_text)
        elif core.codex_empty_managed_capture(target):
            text = ""
            capture_source = "codex-empty"
    payload = {
        "ok": True,
        "text": text,
        "agentRunning": agent_running,
        "queuedSendNowAvailable": bool(
            profile is core.CODEX_PROFILE
            and core.codex_queued_send_now_available(core.tmux_current_capture(target))
        ),
        "agentSource": profile.source,
        "agentProfile": profile.key,
        "captureSource": capture_source,
        "backend": core.session_backend.CODEX_TUI.value,
        "updatedAt": core.now_iso(),
    }
    if profile is core.CODEX_PROFILE:
        payload["commandEvents"] = core.command_timeline_events_for_config(target)
    if profile is core.CODEX_PROFILE:
        payload.update(core.interaction_snapshot(target))
    if thread_id:
        payload.update(core.codex_capture_session_metadata(thread_id))
    if live_text:
        payload["liveText"] = live_text
    if want_html:
        payload["html"] = core.capture_html(target, lines, profile)
    return payload


class OwnerReadRoutes:
    def __init__(self, core: Any, support: Any, streams: Any) -> None:
        self.core = core
        self.support = support
        self.streams = streams

    def routes(self) -> list[Route]:
        return [
            Route("/api/events", self.streams.response, methods=["GET"]),
            Route("/api/{path:path}", self.api, methods=["GET"]),
            Route("/health", self.health, methods=["GET"]),
            Route("/", self.index, methods=["GET"]),
            Route("/index.html", self.index, methods=["GET"]),
            Route("/{asset:path}", self.static, methods=["GET"]),
        ]

    async def api(self, request: Request) -> Response:
        self.support.require_token(request)
        path = f"/api/{request.path_params['path']}"
        session = request.query_params.get("session", "")
        runtime = self.support.runtime
        core = self.core

        if path == "/api/status":
            if runtime.has_session(session):
                payload = await to_thread.run_sync(
                    lambda: core.web_status_payload(runtime, session),
                    abandon_on_cancel=True,
                )
            else:
                target = self.support.target(session)
                payload = await to_thread.run_sync(
                    lambda: (core.ensure_pane_width(target), core.status_payload(target))[1],
                    abandon_on_cancel=True,
                )
            header_label = core.clean_owner_label(request.headers.get("X-Faryo-Owner-Label"))
            if header_label:
                payload["ownerLabel"] = header_label
            return self.support.json_response(payload)

        if path == "/api/interaction":
            if runtime.has_session(session):
                capture = await to_thread.run_sync(lambda: runtime.capture(session), abandon_on_cancel=True)
                snapshot = capture.get("snapshot") or {}
                payload = {
                    "ok": True,
                    "interaction": snapshot.get("interaction"),
                    "interactionRevision": snapshot.get("interactionRevision") or "none",
                    "updatedAt": core.now_iso(),
                }
            else:
                target = self.support.target(session)
                payload = {
                    "ok": True,
                    **await to_thread.run_sync(lambda: core.interaction_snapshot(target), abandon_on_cancel=True),
                    "updatedAt": core.now_iso(),
                }
            return self.support.json_response(payload)

        if path == "/api/goal":
            if runtime.has_session(session):
                capture = await to_thread.run_sync(lambda: runtime.capture(session), abandon_on_cancel=True)
                goal = (capture.get("snapshot") or {}).get("goal")
                payload = {"ok": True, **codex_history.goal_details(goal), "updatedAt": core.now_iso()}
            else:
                target = self.support.target(session)
                payload = {
                    "ok": True,
                    **await to_thread.run_sync(lambda: core.goal_details_for_config(target), abandon_on_cancel=True),
                    "updatedAt": core.now_iso(),
                }
            return self.support.json_response(payload)

        if path == "/api/activity-detail":
            if not runtime.has_session(session):
                raise core.OwnerError("activity detail is unavailable", HTTPStatus.NOT_FOUND)
            try:
                payload = await to_thread.run_sync(
                    lambda: core.web_activity_detail(runtime, session, request.query_params.get("item", "")),
                    abandon_on_cancel=True,
                )
            except (appserver_runtime.AppServerRuntimeError, core.OwnerError) as exc:
                raise core.OwnerError(str(exc), HTTPStatus.NOT_FOUND) from exc
            return self.support.json_response({"ok": True, **payload, "updatedAt": core.now_iso()})

        if path == "/api/command-catalog":
            return self.support.json_response({
                "ok": True,
                **codex_command_policy.default_catalog().public_value(),
                "updatedAt": core.now_iso(),
            })

        if path in {"/api/capabilities", "/api/diagnostics"}:
            return await self._diagnostics(path)

        if path == "/api/workspace-changes":
            if runtime.has_session(session):
                capture = await to_thread.run_sync(lambda: runtime.capture(session), abandon_on_cancel=True)
                cwd = str((capture.get("record") or {}).get("cwd") or "")
            else:
                target = self.support.target(session)
                cwd = await to_thread.run_sync(lambda: core.get_pane_cwd(target), abandon_on_cancel=True)
            if not cwd:
                raise core.OwnerError("workspace unavailable", HTTPStatus.NOT_FOUND)
            try:
                changes = await to_thread.run_sync(
                    lambda: workspace_changes.collect_workspace_changes(cwd, self.support.workspace_root(request)),
                    abandon_on_cancel=True,
                )
            except workspace_changes.WorkspaceChangesError as exc:
                status = {
                    "workspace-out-of-scope": HTTPStatus.FORBIDDEN,
                    "workspace-unavailable": HTTPStatus.NOT_FOUND,
                    "not-a-git-worktree": HTTPStatus.NOT_FOUND,
                }.get(exc.code, HTTPStatus.BAD_GATEWAY)
                raise core.OwnerError("workspace changes unavailable", status) from exc
            return self.support.json_response({"ok": True, **changes, "updatedAt": core.now_iso()})

        if path == "/api/agent-sessions":
            return await self._agent_sessions(request)

        if path == "/api/conversation-history":
            return await self._conversation_history(request, session)

        if path == "/api/directories":
            payload = await to_thread.run_sync(
                lambda: core.directory_browser_payload(
                    self.support.config,
                    request.query_params.get("path", ""),
                    self.support.workspace_root(request),
                    show_hidden=request.query_params.get("showHidden", "0").strip().lower()
                    in {"1", "true", "yes", "on"},
                ),
                abandon_on_cancel=True,
            )
            return self.support.json_response(payload)

        if path == "/api/capture":
            return await self._capture(request, session)

        if path == "/api/local-image":
            target = self.support.target(session)
            path_value = request.query_params.get("path", "")
            resolved = await to_thread.run_sync(
                lambda: core.resolve_local_image_path(
                    path_value,
                    target,
                    self.support.workspace_root(request),
                ),
                abandon_on_cancel=True,
            )
            return self.support.file_response(
                resolved,
                core.IMAGE_CONTENT_TYPES.get(resolved.suffix.lower(), "application/octet-stream"),
            )

        if path == "/api/local-file":
            target = self.support.target(session)
            resolved = await to_thread.run_sync(
                lambda: core.resolve_local_path(
                    request.query_params.get("path", ""),
                    target,
                    core.LOCAL_FILE_SUFFIXES,
                    self.support.workspace_root(request),
                ),
                abandon_on_cancel=True,
            )
            return self.support.file_response(
                resolved,
                core.LOCAL_FILE_CONTENT_TYPES[resolved.suffix.lower()],
                download=request.query_params.get("download", "") in {"1", "true", "yes"},
            )

        if path == "/api/local-file/view":
            return await self._local_file_view(request, session)

        return self.support.json_response(
            {"ok": False, "error": "not found", "updatedAt": core.now_iso()},
            HTTPStatus.NOT_FOUND,
        )

    async def _diagnostics(self, path: str) -> Response:
        core = self.core

        def build() -> dict[str, Any]:
            try:
                configured = bool(core.codex_app_server_argv("app-server"))
            except Exception:
                configured = False
            capabilities = runtime_diagnostics.capability_payload(
                core.release_version(),
                configured,
                core.AGENT_STATE_DB.is_file(),
            )
            capabilities["appServerRuntime"] = self.support.runtime.status()
            if path == "/api/capabilities":
                return {"ok": True, **capabilities, "updatedAt": core.now_iso()}
            sessions = core.tmux_sessions(self.support.config)
            managed_count = sum(core.managed_session(self.support.config, name) for name in sessions)
            recognized_count = sum(
                core.agent_profile_in_pane(
                    core.Config(name, self.support.config.token, self.support.config.pane_width)
                )
                is not None
                for name in sessions
            )
            try:
                receipt_count = sum(
                    1
                    for item in core.SEND_DELIVERY_ROOT.iterdir()
                    if item.is_file() and item.suffix == ".json"
                )
            except OSError:
                receipt_count = 0
            with core._codex_thread_cache_lock:
                cache_count = len(core._codex_thread_cache)
            diagnostics = runtime_diagnostics.diagnostics_payload(
                capabilities,
                tmux_sessions=len(sessions),
                managed_sessions=managed_count,
                recognized_agents=recognized_count,
                delivery_receipts=receipt_count,
                thread_cache_entries=cache_count,
            )
            return {"ok": True, **diagnostics, "updatedAt": core.now_iso()}

        return self.support.json_response(await to_thread.run_sync(build, abandon_on_cancel=True))

    async def _agent_sessions(self, request: Request) -> Response:
        core = self.core
        try:
            limit = max(
                1,
                min(
                    int(request.query_params.get("limit", str(core.AGENT_SESSION_LIST_LIMIT))),
                    core.AGENT_SESSION_QUERY_LIMIT,
                ),
            )
            offset = max(0, int(request.query_params.get("offset", "0")))
        except ValueError as exc:
            raise core.OwnerError("invalid agent session pagination") from exc
        history_root = self.support.history_root(request)
        split = request.query_params.get("view", "") == "split"

        def build() -> dict[str, Any]:
            web_items = core.web_agent_session_items(self.support.runtime, history_root)
            web_thread_ids = {str(item.get("id") or "") for item in web_items}
            if split:
                terminal_active, terminal_ids = core.active_agent_session_items(
                    self.support.config,
                    history_root,
                )
                excluded_ids = terminal_ids | web_thread_ids
                sessions, history_total = core.codex_history_page(
                    self.support.config,
                    limit,
                    offset,
                    history_root,
                    excluded_ids,
                    core.clean_agent_history_query(request.query_params.get("q", "")),
                    core.clean_agent_history_period(request.query_params.get("period", "all")),
                    core.clean_agent_history_archive(request.query_params.get("archive", "active")),
                )
                payload = {
                    "activeSessions": sorted(
                        [*web_items, *terminal_active],
                        key=lambda item: float(item.get("updatedTs") or 0),
                        reverse=True,
                    ),
                    "sessions": sessions,
                    "historyTotal": history_total,
                    "historyOffset": offset,
                    "historyLimit": limit,
                    "historyFilter": {
                        "q": core.clean_agent_history_query(request.query_params.get("q", "")),
                        "period": core.clean_agent_history_period(request.query_params.get("period", "all")),
                        "archive": core.clean_agent_history_archive(request.query_params.get("archive", "active")),
                    },
                }
            else:
                terminal_items = [
                    item
                    for item in core.agent_session_items(self.support.config, history_root)
                    if str(item.get("id") or "") not in web_thread_ids
                ]
                items = [*web_items, *terminal_items]
                items.sort(key=lambda item: float(item.get("updatedTs") or 0), reverse=True)
                payload = {"sessions": items[offset:offset + limit]}
            return {
                "ok": True,
                **payload,
                "activeCount": core.active_agent_count(self.support.config) + len(web_items),
                # Body-free runtime health lets Gateway distinguish a healthy
                # Owner from a private App Server transport that is reconnecting.
                "appServerRuntime": self.support.runtime.status(),
                "updatedAt": core.now_iso(),
            }

        return self.support.json_response(await to_thread.run_sync(build, abandon_on_cancel=True))

    async def _conversation_history(self, request: Request, session: str) -> Response:
        core = self.core
        try:
            limit = int(request.query_params.get("limit", str(core.CODEX_HISTORY_PAGE_TURNS)))
            raw_around = request.query_params.get("around", "")
            around = int(raw_around) if raw_around != "" else None
        except ValueError as exc:
            raise core.OwnerError("invalid conversation history pagination") from exc
        cursor = request.query_params.get("cursor", "")
        if self.support.runtime.has_session(session):
            # Keep one identity domain for the lifetime of an App Server
            # session. Switching to rollout JSONL as soon as its file appears
            # changes question keys while live item blocks still use App Server
            # turn identities, leaving every visible rail marker unloaded.
            # The actor snapshot is hydrated from thread/read after reconnects;
            # JSONL remains the durable source once the Web session is closed.
            capture = await to_thread.run_sync(
                lambda: self.support.runtime.capture(session),
                abandon_on_cancel=True,
            )
            payload = core.web_conversation_history_page(
                capture,
                limit=limit,
                cursor=cursor,
                around=around,
            )
        else:
            target = self.support.target(session)
            payload = await to_thread.run_sync(
                lambda: core.codex_history_page_for_config(
                    target,
                    limit=limit,
                    cursor=cursor,
                    around=around,
                ),
                abandon_on_cancel=True,
            )
        return self.support.json_response(payload)

    async def _capture(self, request: Request, session: str) -> Response:
        core = self.core
        try:
            lines = int(request.query_params.get("lines", str(core.CAPTURE_DEFAULT_LINES)))
        except ValueError:
            lines = core.CAPTURE_DEFAULT_LINES
        lines = max(40, min(lines, core.CAPTURE_MAX_LINES))
        if self.support.runtime.has_session(session):
            payload = await to_thread.run_sync(
                lambda: core.web_capture_payload(self.support.runtime, session, lines),
                abandon_on_cancel=True,
            )
        else:
            target = self.support.target(session)
            want_html = request.query_params.get("format", "") == "html" or request.query_params.get(
                "html", ""
            ).lower() in {"1", "true", "yes"}
            payload = await to_thread.run_sync(
                lambda: terminal_capture_response(core, target, lines, want_html),
                abandon_on_cancel=True,
            )
        return self.support.json_response(payload)

    async def _local_file_view(self, request: Request, session: str) -> Response:
        core = self.core
        target = self.support.target(session)
        resolved = await to_thread.run_sync(
            lambda: core.resolve_local_path(
                request.query_params.get("path", ""),
                target,
                core.LOCAL_FILE_SUFFIXES,
                self.support.workspace_root(request),
            ),
            abandon_on_cancel=True,
        )
        query = {"path": str(resolved)}
        if token := request.query_params.get("token"):
            query["token"] = token
        if session:
            query["session"] = session
        raw_url = f"../local-file?{urlencode(query)}"
        download_url = f"../local-file?{urlencode({**query, 'download': '1'})}"
        title = html.escape(resolved.name)
        if resolved.suffix.lower() in core.EXTERNAL_VIEWER_SUFFIXES:
            body = f"<section class='notice'><p>This file type opens best in the browser or a local app.</p><p><a class='pill' href='{raw_url}'>Open file</a> <a class='pill' href='{download_url}' download>Download</a></p></section>"
        else:
            text = await to_thread.run_sync(
                lambda: resolved.read_text(encoding="utf-8", errors="replace"),
                abandon_on_cancel=True,
            )
            if resolved.suffix.lower() == ".json":
                try:
                    text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
                except json.JSONDecodeError:
                    pass
            body = f"<pre>{html.escape(text)}</pre>"
        document = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#F6F7F9"><title>{title}</title><style>
body{{margin:0;background:#F6F7F9;color:#202228;font:16px/1.58 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;-webkit-text-size-adjust:100%}}
header{{position:sticky;top:0;z-index:2;display:flex;align-items:center;gap:8px;padding:calc(env(safe-area-inset-top) + 8px) 10px 8px;background:rgba(255,255,255,.96);border-bottom:1px solid #DDE1E8;backdrop-filter:blur(12px)}}
h1{{min-width:0;flex:1;margin:0;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
button,.pill{{min-height:34px;padding:0 10px;border:1px solid #DDE1E8;border-radius:999px;background:#FFFFFF;color:#202228;font:600 14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;text-decoration:none}}
main{{padding:14px 14px calc(env(safe-area-inset-bottom) + 22px)}}
pre{{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;font:15px/1.58 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}}
.notice{{padding:12px;border:1px solid #DDE1E8;border-radius:12px;background:#FFFFFF}}
@media (prefers-color-scheme: dark){{body{{background:#0F1115;color:#ECEEF3}}header{{background:rgba(23,26,32,.96);border-color:#2C313B}}button,.pill,.notice{{background:#171A20;color:#ECEEF3;border-color:#2C313B}}}}
</style></head><body><header><button id="backButton" type="button">Back</button><h1>{title}</h1><a class="pill" href="{raw_url}">Raw</a><a class="pill" href="{download_url}" download>Download</a></header><main>{body}</main><script src="../../local-file-view.js"></script></body></html>"""
        return HTMLResponse(document, headers={"Cache-Control": "no-store"})

    async def health(self, _request: Request) -> Response:
        return self.support.json_response({"ok": True, "updatedAt": self.core.now_iso()})

    async def index(self, _request: Request) -> Response:
        try:
            source = await to_thread.run_sync(
                lambda: (self.core.STATIC_DIR / "index.html").read_text(encoding="utf-8"),
                abandon_on_cancel=True,
            )
        except OSError as exc:
            raise self.core.OwnerError("file not found", HTTPStatus.NOT_FOUND) from exc
        version = html.escape(self.core.release_version() or "unknown", quote=True)
        body = source.replace("__FARYO_RELEASE_VERSION__", version).replace(
            "__FARYO_RELEASE_NUMBER__",
            version.removeprefix("v"),
        )
        return HTMLResponse(body, headers={"Cache-Control": "no-store"})

    async def static(self, request: Request) -> Response:
        raw = str(request.path_params.get("asset") or "")
        if raw in self.core.SHARED_STATIC_FILES:
            return self.support.file_response(
                self.core.SHARED_STATIC_DIR / raw,
                self.core.SHARED_STATIC_FILES[raw],
            )
        if not raw or "\x00" in raw:
            raise self.core.OwnerError("file not found", HTTPStatus.NOT_FOUND)
        root = self.core.STATIC_DIR.resolve()
        try:
            path = (root / raw).resolve(strict=True)
            if not path.is_file() or not path.is_relative_to(root):
                raise OSError
        except OSError as exc:
            raise self.core.OwnerError("file not found", HTTPStatus.NOT_FOUND) from exc
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix == ".js" or path.suffix == ".mjs":
            content_type = "text/javascript; charset=utf-8"
        elif path.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        return self.support.file_response(path, content_type)
