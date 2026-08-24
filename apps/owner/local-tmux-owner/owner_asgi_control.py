"""Authenticated Owner mutation routes."""

from __future__ import annotations

import hmac
from http import HTTPStatus
from pathlib import Path
from typing import Any

from anyio import to_thread
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

import appserver_runtime
from faryo_cli import session_backend


class OwnerControlRoutes:
    def __init__(self, core: Any, support: Any) -> None:
        self.core = core
        self.support = support

    def routes(self) -> list[Route]:
        return [Route("/api/{path:path}", self.api, methods=["POST"])]

    async def api(self, request: Request) -> Response:
        self.support.require_token(request)
        path = f"/api/{request.path_params['path']}"
        if path == "/api/attachment":
            return await self._attachment(request)
        payload = await self.support.read_json(request)
        if path == "/api/agent/new":
            return await self._agent_new(request, payload)
        if path == "/api/agent/cleanup-idle":
            idle_seconds = max(
                60,
                min(
                    int(payload.get("idle_seconds") or payload.get("idleSeconds") or 0),
                    self.core.MAX_MANAGED_AGENT_IDLE_SECONDS,
                ),
            )
            await to_thread.run_sync(
                lambda: self.core.cleanup_managed_sessions(self.support.config, idle_seconds),
                abandon_on_cancel=True,
            )
            return self._ok()
        if path in {"/api/agent-session/archive", "/api/agent-session/unarchive"}:
            archived = path.endswith("/archive")
            thread_id = str(payload.get("agent_session_id") or payload.get("agentSessionId") or "")
            if self.support.runtime.registry.by_thread(thread_id) is not None:
                raise self.core.OwnerError("active agent sessions cannot be archived", HTTPStatus.CONFLICT)

            def lifecycle_rpc(method: str, selected_thread_id: str, timeout: float) -> dict[str, Any]:
                try:
                    return self.support.runtime.thread_lifecycle(method, selected_thread_id, timeout)
                except appserver_runtime.AppServerRuntimeError as exc:
                    return {"ok": False, "error": str(exc)}

            result = await to_thread.run_sync(
                lambda: self.core.change_codex_thread_archive_state(
                    self.support.config,
                    thread_id,
                    archived,
                    self.support.history_root(request),
                    lifecycle_rpc,
                ),
                abandon_on_cancel=True,
            )
            return self._ok(result)
        if path == "/api/session/close":
            return await self._close(payload)
        if path == "/api/agent/resume":
            return await self._resume(request, payload)

        session = str(payload.get("session") or "")
        if self.support.runtime.has_session(session):
            return await self._web_action(path, payload, session)
        target = self.support.target(session)
        await to_thread.run_sync(lambda: self.core.ensure_pane_width(target), abandon_on_cancel=True)
        if path == "/api/interaction/start":
            result = await to_thread.run_sync(
                lambda: self.core.begin_codex_command(target, payload),
                abandon_on_cancel=True,
            )
            return self._ok(result)
        if path == "/api/interaction/respond":
            result = await to_thread.run_sync(
                lambda: self.core.respond_codex_interaction(target, payload),
                abandon_on_cancel=True,
            )
            return self._ok(result)
        if path == "/api/send":
            receipt = await to_thread.run_sync(
                lambda: self.core.send_text(
                    target,
                    str(payload.get("text", "")),
                    str(payload.get("clientMessageId") or ""),
                ),
                abandon_on_cancel=True,
            )
            return self._ok(receipt)
        if path == "/api/interrupt":
            result = await to_thread.run_sync(
                lambda: self.core.interrupt_agent(target),
                abandon_on_cancel=True,
            )
            return self._ok(result)
        return self.support.json_response(
            {"ok": False, "error": "not found", "updatedAt": self.core.now_iso()},
            HTTPStatus.NOT_FOUND,
        )

    async def _attachment(self, request: Request) -> Response:
        form = await self.support.read_multipart_form(request)
        if "file" not in form:
            raise self.core.OwnerError("missing file field: file")
        file_item = form["file"]
        if isinstance(file_item, list):
            file_item = file_item[0] if file_item else None
        if file_item is None or not getattr(file_item, "filename", ""):
            raise self.core.OwnerError("missing file field: file")
        path, size, kind = await to_thread.run_sync(
            lambda: self.core.save_uploaded_attachment(file_item, self.support.inbox_root(request)),
            abandon_on_cancel=True,
        )
        return self._ok({"path": str(path), "bytes": size, "kind": kind})

    async def _agent_new(self, request: Request, payload: dict[str, Any]) -> Response:
        core = self.core
        workspace_root = self.support.workspace_root(request)
        raw_cwd = core.compact_text(payload.get("cwd"))
        if raw_cwd:
            cwd, _roots = await to_thread.run_sync(
                lambda: core.resolve_start_directory(raw_cwd, workspace_root),
                abandon_on_cancel=True,
            )
            cwd_token = str(payload.get("cwd_token") or payload.get("cwdToken") or "").strip()
            if cwd_token and not hmac.compare_digest(
                cwd_token,
                core.directory_selection_token(self.support.config, cwd),
            ):
                raise core.OwnerError("working directory selection expired", HTTPStatus.CONFLICT)
        else:
            default_cwd = await to_thread.run_sync(
                lambda: core.get_pane_cwd(self.support.config),
                abandon_on_cancel=True,
            )
            cwd = Path(workspace_root or default_cwd or str(Path.home())).expanduser()
            cwd = cwd if cwd.is_dir() else Path.home()
        command = core.clean_agent_launch_command(str(payload.get("command") or ""))
        if not command:
            raise core.OwnerError("invalid launch command")
        raw_launch_id = str(payload.get("client_launch_id") or payload.get("clientLaunchId") or "").strip()
        launch_id = core.clean_client_launch_id(raw_launch_id)
        if raw_launch_id and not launch_id:
            raise core.OwnerError("invalid client launch id")
        title = core.clean_session_title(payload.get("title"))
        context_window_k = core.bounded_context_window_k(payload)
        backend = session_backend.parse_backend(
            payload.get("backend"),
            default=session_backend.APP_SERVER,
        )
        if backend is None:
            raise core.OwnerError("choose a supported Codex backend")
        if backend is session_backend.APP_SERVER:
            if command != "codex":
                raise core.OwnerError("Codex App Server sessions only support Codex")
            try:
                result = await to_thread.run_sync(
                    lambda: self.support.runtime.start_session(
                        cwd=str(cwd),
                        title=title,
                        model=str(payload.get("model") or ""),
                        service_tier=str(payload.get("serviceTier") or payload.get("service_tier") or ""),
                        context_window_k=context_window_k,
                        launch_id=launch_id or "",
                    ),
                    abandon_on_cancel=True,
                )
            except appserver_runtime.AppServerRuntimeError as exc:
                raise core.OwnerError(str(exc), HTTPStatus.SERVICE_UNAVAILABLE) from exc
            return self._ok({"accepted": True, **result})
        existing_launch = core.managed_launch_session(self.support.config, launch_id or "") if launch_id else ""
        name = await to_thread.run_sync(
            lambda: core.start_agent_runtime_async(
                self.support.config,
                cwd,
                command,
                [],
                core.bounded_max_running(payload),
                title=title,
                launch_id=launch_id or "",
                context_window_k=context_window_k,
                reserved_names=lambda: [
                    str(record.get("session") or "")
                    for record in self.support.runtime.session_records()
                ],
            ),
            abandon_on_cancel=True,
        )
        launch_state, _running = await to_thread.run_sync(
            lambda: core.agent_session_lifecycle(self.support.config, name),
            abandon_on_cancel=True,
        )
        return self._ok({
            "accepted": True,
            "state": launch_state or "starting",
            "session": name,
            "duplicate": bool(existing_launch and existing_launch == name),
        })

    async def _close(self, payload: dict[str, Any]) -> Response:
        session = str(payload.get("session") or "")
        if self.support.runtime.has_session(session):
            interrupt = payload.get("interrupt") is True
            try:
                result = await to_thread.run_sync(
                    lambda: self.support.runtime.close_session(session, interrupt=interrupt),
                    abandon_on_cancel=True,
                )
            except appserver_runtime.AppServerRuntimeError as exc:
                raise self.core.OwnerError(str(exc), HTTPStatus.CONFLICT) from exc
            return self._ok(result)
        await to_thread.run_sync(
            lambda: self.core.close_shell_session(self.support.config, session),
            abandon_on_cancel=True,
        )
        return self._ok()

    async def _resume(self, request: Request, payload: dict[str, Any]) -> Response:
        core = self.core
        agent_session_id = core.clean_agent_session_id(str(payload.get("agent_session_id") or ""))
        source = str(payload.get("source") or "")
        if not agent_session_id:
            raise core.OwnerError("missing agent session id")
        if not source:
            raise core.OwnerError("missing agent source")
        backend = session_backend.parse_backend(
            payload.get("backend"),
            default=session_backend.backend_for_source(source),
        )
        if backend is None:
            raise core.OwnerError("choose a supported Codex backend")
        context_window_k = core.bounded_context_window_k(payload)
        raw_cwd = core.compact_text(payload.get("cwd"))
        selected_cwd: Path | None = None
        if raw_cwd:
            selected_cwd, _roots = await to_thread.run_sync(
                lambda: core.resolve_start_directory(raw_cwd, self.support.workspace_root(request)),
                abandon_on_cancel=True,
            )
            cwd_token = str(payload.get("cwd_token") or payload.get("cwdToken") or "").strip()
            if not cwd_token or not hmac.compare_digest(
                cwd_token,
                core.directory_selection_token(self.support.config, selected_cwd),
            ):
                raise core.OwnerError("working directory selection expired", HTTPStatus.CONFLICT)
        else:
            requirement = await to_thread.run_sync(
                lambda: core.codex_resume_directory_requirement(
                    self.support.config,
                    agent_session_id,
                    self.support.history_root(request),
                ),
                abandon_on_cancel=True,
            )
            if requirement is not None:
                return self._ok(requirement)
        if backend is session_backend.APP_SERVER:
            return await self._resume_web(request, payload, agent_session_id, selected_cwd, context_window_k)

        def resume_terminal_owned() -> str:
            with core.RUNTIME_LOCK:
                if self.support.runtime.has_thread(agent_session_id):
                    raise core.OwnerError(
                        "this Codex thread is already owned by Codex App Server",
                        HTTPStatus.CONFLICT,
                    )
                try:
                    remote_app_server = self.support.runtime.thread_loaded(agent_session_id)
                except appserver_runtime.AppServerRuntimeError:
                    remote_app_server = False
                return core.resume_agent_session(
                    self.support.config,
                    agent_session_id,
                    "codex-cli",
                    core.bounded_max_running(payload),
                    self.support.history_root(request),
                    selected_cwd,
                    True,
                    context_window_k,
                    remote_app_server,
                )

        session = await to_thread.run_sync(
            resume_terminal_owned,
            abandon_on_cancel=True,
        )
        launch_state, _running = await to_thread.run_sync(
            lambda: core.agent_session_lifecycle(self.support.config, session),
            abandon_on_cancel=True,
        )
        return self._ok({"accepted": True, "state": launch_state or "starting", "session": session})

    async def _resume_web(
        self,
        request: Request,
        payload: dict[str, Any],
        thread_id: str,
        selected_cwd: Path | None,
        context_window_k: int,
    ) -> Response:
        core = self.core
        history_root = self.support.history_root(request)

        def resume_app_server_owned() -> dict[str, Any]:
            with core.RUNTIME_LOCK:
                thread = core.codex_thread_by_id(thread_id)
                if thread is None or (
                    history_root is not None
                    and not core.path_under_root(str(thread.get("cwd") or ""), history_root)
                ):
                    raise core.OwnerError("agent session not found", HTTPStatus.NOT_FOUND)
                active_threads, superseded_threads = core.active_codex_thread_state(
                    self.support.config
                )
                if thread_id in active_threads or thread_id in superseded_threads:
                    raise core.OwnerError(
                        "this Codex thread is already owned by Codex TUI (tmux)",
                        HTTPStatus.CONFLICT,
                    )
                recorded_cwd = str(thread.get("cwd") or "")
                resume_cwd = str(selected_cwd or recorded_cwd)
                if not resume_cwd or not Path(resume_cwd).is_dir():
                    return {
                        "requiresWorkingDirectory": True,
                        "reason": "recorded-directory-unavailable",
                        "recordedDisplayCwd": core.short_path(recorded_cwd)
                        or "Unavailable directory",
                    }
                result = self.support.runtime.resume_session(
                    thread_id=thread_id,
                    cwd=resume_cwd,
                    title=core.codex_thread_title(
                        thread,
                        core.short_path(resume_cwd) or thread_id,
                    ),
                    model=str(thread.get("model") or ""),
                    service_tier=str(
                        payload.get("serviceTier") or payload.get("service_tier") or ""
                    ),
                    context_window_k=context_window_k,
                )
                return {"accepted": True, **result}

        try:
            result = await to_thread.run_sync(
                resume_app_server_owned,
                abandon_on_cancel=True,
            )
        except appserver_runtime.AppServerRuntimeError as exc:
            raise core.OwnerError(str(exc), HTTPStatus.SERVICE_UNAVAILABLE) from exc
        return self._ok(result)

    async def _web_action(self, path: str, payload: dict[str, Any], session: str) -> Response:
        core = self.core
        try:
            if path == "/api/interaction/respond":
                answers = payload.get("answers")
                if answers is not None and not isinstance(answers, dict):
                    raise core.OwnerError("interaction answers must be an object")
                result = await to_thread.run_sync(
                    lambda: self.support.runtime.respond_interaction(
                        session,
                        interaction_id=str(payload.get("interactionId") or payload.get("interaction_id") or ""),
                        option_id=str(payload.get("optionId") or payload.get("option_id") or ""),
                        answers=answers,
                        action=str(payload.get("action") or ""),
                        client_request_id=str(payload.get("clientRequestId") or payload.get("client_request_id") or ""),
                    ),
                    abandon_on_cancel=True,
                )
                return self._ok(result)
            if path == "/api/send":
                result = await to_thread.run_sync(
                    lambda: self.support.runtime.send(
                        session,
                        str(payload.get("text") or ""),
                        str(payload.get("clientMessageId") or ""),
                    ),
                    abandon_on_cancel=True,
                )
                return self._ok(result)
            if path == "/api/interrupt":
                result = await to_thread.run_sync(
                    lambda: self.support.runtime.interrupt(session),
                    abandon_on_cancel=True,
                )
                return self._ok(result)
        except appserver_runtime.AppServerRuntimeError as exc:
            raise core.OwnerError(str(exc), HTTPStatus.BAD_GATEWAY) from exc
        if path == "/api/interaction/start":
            try:
                result = await to_thread.run_sync(
                    lambda: self.support.runtime.begin_command(
                        session,
                        command=str(payload.get("command") or ""),
                        client_request_id=str(payload.get("clientRequestId") or payload.get("client_request_id") or ""),
                        confirmed=bool(payload.get("confirmed")),
                    ),
                    abandon_on_cancel=True,
                )
            except appserver_runtime.AppServerRuntimeError as exc:
                raise core.OwnerError(str(exc), HTTPStatus.CONFLICT) from exc
            return self._ok(result)
        return self.support.json_response(
            {"ok": False, "error": "not found", "updatedAt": core.now_iso()},
            HTTPStatus.NOT_FOUND,
        )

    def _ok(self, values: dict[str, Any] | None = None) -> Response:
        return self.support.json_response({
            "ok": True,
            **(values or {}),
            "updatedAt": self.core.now_iso(),
        })
