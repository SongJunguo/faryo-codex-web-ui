"""Shared Gateway active/history/workbench aggregation service."""

from __future__ import annotations

from typing import Any, Callable

from faryo_cli import session_backend


class WorkbenchService:
    def __init__(
        self,
        legacy: Any,
        config: Any,
        owner: Any,
        *,
        owner_json_request_callback: Callable[..., dict[str, Any]] | None = None,
        owner_sessions_callback: Callable[..., dict[str, Any]] | None = None,
        max_running_callback: Callable[[str, str], int] | None = None,
        backend_status_callback: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.legacy = legacy
        self.config = config
        self.owner = owner
        self.owner_json_request_callback = owner_json_request_callback
        self.owner_sessions_callback = owner_sessions_callback
        self.max_running_callback = max_running_callback
        self.backend_status_callback = backend_status_callback or legacy.backend_status

    def session_item(self, item: dict[str, Any], route: str, result: dict[str, Any], limit_reached: bool) -> dict[str, Any]:
        updated_raw = item.get("updatedAt") or item.get("updated_at") or result.get("updatedAt") or ""
        tmux_session = str(item.get("tmuxSession") or item.get("session") or "")
        active = bool(tmux_session)
        cwd = str(item.get("cwd") or "")
        raw_state = str(item.get("state") or "").strip().lower()
        if raw_state not in self.legacy.SESSION_STATES:
            raw_state = ("running" if item.get("agentRunning") else "waiting") if active else ("archived" if item.get("archived") else "resumable")
        source = str(item.get("source") or "")
        runtime = (
            result.get("appServerRuntime")
            if isinstance(result.get("appServerRuntime"), dict)
            else {}
        )
        backend = session_backend.parse_backend(
            item.get("backend"),
            default=session_backend.backend_for_source(source),
        )
        return {
            "id": str(item.get("id") or ""),
            "title": self.legacy.display_session_title(item.get("title") or item.get("label") or item.get("id") or "Untitled session"),
            "gitLabel": str(item.get("gitLabel") or item.get("git_label") or ""),
            "route": route,
            "routeLabel": self.legacy.BACKENDS[route][2],
            "cwd": cwd,
            "cwdLabel": self.legacy.compact_path_label(cwd),
            "updatedAt": self.legacy.display_updated_at(updated_raw),
            "updatedTs": float(item.get("updatedTs") or self.legacy.parse_updated_ts(updated_raw)),
            "tmuxSession": tmux_session,
            "active": active,
            "managed": bool(item.get("managed")),
            "agentRunning": bool(active and item.get("agentRunning")),
            "state": raw_state,
            "archived": bool(item.get("archived")),
            "limitReached": bool(not active and limit_reached),
            "source": source,
            "backend": (backend or session_backend.CODEX_TUI).value,
            "appServerReady": bool(runtime.get("ready")) if runtime else None,
            "appServerWorkerState": str(item.get("appServerWorkerState") or "") or None,
        }

    def owner_sessions(
        self,
        route: str,
        username: str,
        history_page: int = 1,
        exact_page: bool = False,
        history_filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        page = max(1, history_page)
        history_limit = self.legacy.HISTORY_PAGE_SIZE if exact_page else min(self.legacy.HISTORY_PAGE_SIZE * page, self.legacy.HISTORY_MAX_FETCH)
        history_offset = (page - 1) * self.legacy.HISTORY_PAGE_SIZE if exact_page else 0
        max_running = self.max_running_callback(username, route) if self.max_running_callback else self.config.max_running(route)
        owner_json_request = self.owner_json_request_callback or self.owner.json_request
        result = owner_json_request(
            route,
            self.legacy.owner_history_query(history_limit, history_offset, history_filters),
            None,
            username,
            method="GET",
        )
        active_count = int(result.get("activeCount") or 0)
        limit_reached = active_count >= max_running
        raw_active = result.get("activeSessions", []) if result.get("ok") and isinstance(result.get("activeSessions"), list) else []
        raw_history = result.get("sessions", []) if result.get("ok") and isinstance(result.get("sessions"), list) else []
        runtime = result.get("appServerRuntime") if isinstance(result.get("appServerRuntime"), dict) else {}
        active_sessions = [self.session_item(item, route, result, limit_reached) for item in raw_active if isinstance(item, dict)]
        sessions = [self.session_item(item, route, result, limit_reached) for item in raw_history if isinstance(item, dict)]
        return {
            "activeSessions": active_sessions,
            "sessions": sessions,
            "historyTotal": int(result.get("historyTotal") or len(sessions)),
            "activeCount": active_count,
            "maxRunning": max_running,
            "canCreate": not limit_reached,
            "appServerReady": bool(runtime.get("ready")) if runtime else None,
            "appServerState": str(runtime.get("state") or "unknown") if runtime else "unknown",
        }

    def payload(self, username: str, history_page: int = 1, history_filters: dict[str, Any] | None = None) -> dict[str, Any]:
        requested_page = max(1, history_page)
        applied_filters = self.legacy.normalize_history_filters(history_filters)
        routes = self.config.user_routes(username)
        exact_page = len(routes) == 1
        owner_sessions = self.owner_sessions_callback or self.owner_sessions
        route_payloads = {route: owner_sessions(route, username, requested_page, exact_page, applied_filters) for route in routes}
        active_sessions = [item for route in routes for item in route_payloads[route]["activeSessions"]]
        active_sessions.sort(
            key=lambda item: (self.legacy.SESSION_STATE_PRIORITY.get(str(item.get("state") or ""), -1), float(item.get("updatedTs") or 0)),
            reverse=True,
        )
        sessions = [item for route in routes for item in route_payloads[route]["sessions"]]
        sessions.sort(key=lambda item: float(item.get("updatedTs") or 0), reverse=True)
        history_total = sum(int(route_payloads[route]["historyTotal"]) for route in routes)
        total_pages = max(1, (history_total + self.legacy.HISTORY_PAGE_SIZE - 1) // self.legacy.HISTORY_PAGE_SIZE)
        page = min(requested_page, total_pages)
        if exact_page and page != requested_page:
            route = routes[0]
            route_payloads[route] = owner_sessions(route, username, page, True, applied_filters)
            active_sessions = route_payloads[route]["activeSessions"]
            sessions = route_payloads[route]["sessions"]
        start = (page - 1) * self.legacy.HISTORY_PAGE_SIZE
        entries = []
        for item in [self.backend_status_callback(route) for route in routes]:
            route_payload = route_payloads[item["id"]]
            item.update({
                "activeCount": route_payload["activeCount"],
                "maxRunning": route_payload["maxRunning"],
                "canCreate": route_payload["canCreate"],
                # An older Owner does not publish runtime health.  Keep that
                # rolling-upgrade case usable instead of treating unknown as down.
                "appServerReady": route_payload.get("appServerReady"),
                "appServerState": str(route_payload.get("appServerState") or "unknown"),
            })
            entries.append(item)
        cwd_choices = {}
        for route in routes:
            choice_payload = route_payloads[route]
            cwd_choices[route] = self.legacy.agent_cwd_choices(
                [*choice_payload["activeSessions"], *choice_payload["sessions"]],
                self.config.workspace_root(username, route),
            )
        inbox = self.config.list_bridge_packages(username, "pending")[:1]
        return {
            "ok": True,
            "entries": entries,
            "activeSessions": active_sessions,
            "sessions": sessions[:self.legacy.HISTORY_PAGE_SIZE] if exact_page else sessions[start:start + self.legacy.HISTORY_PAGE_SIZE],
            "history": {
                "page": page,
                "pageSize": self.legacy.HISTORY_PAGE_SIZE,
                "total": history_total,
                "totalPages": total_pages,
                "hasPrevious": page > 1,
                "hasNext": page < total_pages,
                "filter": applied_filters,
            },
            "newSessionCommands": sorted(self.legacy.NEW_SESSION_COMMANDS),
            "agentCwdChoices": cwd_choices,
            "packages": inbox,
            "inbox": inbox,
            "updatedAt": self.legacy.now_ts(),
        }
