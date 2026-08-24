"""Starlette Codex session start and resume routes."""

from __future__ import annotations

from http import HTTPStatus
import json
import secrets
import time
from typing import Any

from anyio import sleep, to_thread
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

import gateway_security
import owner_client
from faryo_cli import browser_contract, session_backend


def routes(legacy: Any, config: Any, client: owner_client.OwnerClient, support: Any) -> list[Route]:
    async def agent_resume(request: Request) -> Response:
        current = support.username(request)
        if not current:
            return support.json_response({"ok": False, "error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
        request_id = secrets.token_hex(8)
        started = time.monotonic()
        route = ""
        target = ""
        supplied_csrf = request.headers.get(legacy.CSRF_HEADER, "").strip()
        expected_csrf = gateway_security.csrf_token(config.cookie_secret, current, config.auth_epoch(current))
        if not supplied_csrf or not secrets.compare_digest(supplied_csrf, expected_csrf):
            status = HTTPStatus.FORBIDDEN
            response = support.json_response({"ok": False, "error": "csrf required"}, status)
        else:
            try:
                body = await request.body()
                if not body:
                    raise ValueError("empty JSON body")
                if len(body) > 65536:
                    raise ValueError("request too large")
                payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("invalid JSON object")
                browser_contract.require_supported_version(payload)
                route = str(payload.get("route") or "").strip()
                target = legacy.clean_agent_session_id(str(payload.get("agent_session_id") or "")) or ""
                source = str(payload.get("source") or "")
                backend = session_backend.parse_backend(
                    payload.get("backend"),
                    default=session_backend.backend_for_source(source),
                )
                requested_cwd = str(payload.get("cwd") or "").strip()
                cwd_token = str(payload.get("cwd_token") or payload.get("cwdToken") or "").strip()
                context_window_k = legacy.clean_context_window_k(
                    payload.get("context_window_k", payload.get("contextWindowK"))
                )
                if route not in legacy.BACKENDS or not target or not source:
                    raise ValueError("route, agent_session_id and source are required")
                if backend is None:
                    raise ValueError("choose a supported Codex backend")
                if not config.allowed_route(current, route):
                    status = HTTPStatus.FORBIDDEN
                    response = support.json_response({"ok": False, "error": "forbidden"}, status)
                else:
                    if requested_cwd:
                        expected_cwd_token = legacy.owner_directory_selection_token(
                            config.owner_token(route),
                            requested_cwd,
                        )
                        if not cwd_token or not secrets.compare_digest(cwd_token, expected_cwd_token):
                            raise ValueError("working directory selection is invalid or expired")
                    owner_payload = {
                        "agent_session_id": target,
                        "source": source,
                        "backend": backend.value,
                        "max_running": config.max_running(route),
                    }
                    if context_window_k:
                        owner_payload["context_window_k"] = context_window_k
                    if requested_cwd:
                        owner_payload.update({"cwd": requested_cwd, "cwd_token": cwd_token})
                    result = await to_thread.run_sync(
                        lambda: client.json_request(
                            route,
                            "/api/agent/resume",
                            owner_payload,
                            current,
                            timeout=20,
                        )
                    )
                    session = ""
                    if result.get("ok") and result.get("requiresWorkingDirectory"):
                        status = HTTPStatus.OK
                        response = support.json_response({
                            "ok": True,
                            "requiresWorkingDirectory": True,
                            "reason": str(result.get("reason") or "recorded-directory-unavailable"),
                            "recordedDisplayCwd": str(result.get("recordedDisplayCwd") or "Unavailable directory"),
                        })
                    else:
                        session = legacy.clean_session_id(str(result.get("session") or "")) if result.get("ok") else ""
                    if not result.get("requiresWorkingDirectory") and not session:
                        status = support.upstream_status(result)
                        response = support.json_response(
                            support.forwarded_error(result, status, "Owner resume failed"),
                            status,
                        )
                    elif session:
                        status = HTTPStatus.OK
                        response = support.json_response({
                            "ok": True,
                            "redirect": f"/{route}/?session={session}",
                            "session": session,
                        })
            except (UnicodeDecodeError, json.JSONDecodeError):
                status = HTTPStatus.BAD_REQUEST
                response = support.json_response({"ok": False, "error": "invalid JSON body"}, status)
            except ValueError as exc:
                status = HTTPStatus.BAD_REQUEST
                response = support.json_response({"ok": False, "error": str(exc)}, status)
        response.headers["X-Faryo-Request-Id"] = request_id
        support.append_audit(
            username_value=current,
            route=route,
            action="resume",
            target=target,
            request_id=request_id,
            status=status,
            started=started,
        )
        return response

    async def agent_new(request: Request) -> Response:
        current = support.username(request)
        if not current:
            return support.json_response({"ok": False, "error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
        request_id = secrets.token_hex(8)
        started = time.monotonic()
        route = ""
        target = ""
        idempotent = False
        supplied_csrf = request.headers.get(legacy.CSRF_HEADER, "").strip()
        expected_csrf = gateway_security.csrf_token(config.cookie_secret, current, config.auth_epoch(current))
        if not supplied_csrf or not secrets.compare_digest(supplied_csrf, expected_csrf):
            status = HTTPStatus.FORBIDDEN
            response = support.json_response({"ok": False, "error": "csrf required"}, status)
        else:
            try:
                body = await request.body()
                if not body:
                    raise ValueError("empty JSON body")
                if len(body) > 4096:
                    raise ValueError("request too large")
                payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("invalid JSON object")
                browser_contract.require_supported_version(payload)
                route = str(payload.get("route") or "").strip()
                command = legacy.clean_agent_launch_command(str(payload.get("command") or ""))
                requested_cwd = str(payload.get("cwd") or "").strip().rstrip("/")
                cwd_token = str(payload.get("cwd_token") or payload.get("cwdToken") or "").strip()
                raw_launch_id = str(payload.get("client_launch_id") or payload.get("clientLaunchId") or "").strip()
                launch_id = legacy.clean_client_launch_id(raw_launch_id)
                context_window_k = legacy.clean_context_window_k(
                    payload.get("context_window_k", payload.get("contextWindowK"))
                )
                backend = session_backend.parse_backend(
                    payload.get("backend"),
                    default=session_backend.APP_SERVER,
                )
                if backend is None:
                    raise ValueError("choose a supported Codex backend")
                target = launch_id or ""
                if route not in legacy.BACKENDS or not command:
                    raise ValueError("route and command are required")
                if raw_launch_id and not launch_id:
                    raise ValueError("invalid client launch id")
                if not config.allowed_route(current, route):
                    status = HTTPStatus.FORBIDDEN
                    response = support.json_response({"ok": False, "error": "forbidden"}, status)
                elif current != config.mcp_user and command != "codex":
                    status = HTTPStatus.FORBIDDEN
                    response = support.json_response({"ok": False, "error": "forbidden command"}, status)
                else:
                    launch = {
                        "command": command,
                        "backend": backend.value,
                        "max_running": config.max_running(route),
                        **({"client_launch_id": launch_id} if launch_id else {}),
                        **({"context_window_k": context_window_k} if context_window_k else {}),
                    }
                    history_result = await to_thread.run_sync(
                        lambda: client.json_request(
                            route,
                            legacy.owner_history_query(legacy.HISTORY_PAGE_SIZE, 0),
                            None,
                            current,
                            method="GET",
                        )
                    )
                    recent_sessions = [
                        item
                        for key in ("activeSessions", "sessions")
                        for item in (
                            history_result.get(key) if isinstance(history_result.get(key), list) else []
                        )
                        if isinstance(item, dict)
                    ]
                    if requested_cwd:
                        expected_cwd_token = legacy.owner_directory_selection_token(
                            config.owner_token(route),
                            requested_cwd,
                        )
                        if not cwd_token or not secrets.compare_digest(cwd_token, expected_cwd_token):
                            raise ValueError("working directory selection is invalid or expired")
                    selected_cwd = requested_cwd or legacy.select_recent_agent_cwd(
                        recent_sessions,
                        config.workspace_root(current, route),
                    )
                    selected_launch = (
                        {**launch, "cwd": selected_cwd, "cwd_token": cwd_token}
                        if selected_cwd
                        else launch
                    )

                    async def start(values: dict[str, Any]) -> dict[str, Any]:
                        return await to_thread.run_sync(
                            lambda: client.json_request(
                                route,
                                "/api/agent/new",
                                values,
                                current,
                                timeout=20,
                            )
                        )

                    result = await start(selected_launch)
                    if launch_id and result.get("transportError"):
                        await sleep(0.25)
                        result = await start(selected_launch)
                    if selected_cwd and not requested_cwd and not result.get("ok"):
                        result = await start(launch)
                    session = legacy.clean_session_id(str(result.get("session") or "")) if result.get("ok") else ""
                    if not session:
                        status = support.upstream_status(result)
                        response = support.json_response(
                            support.forwarded_error(result, status, "Owner new session failed"),
                            status,
                        )
                    else:
                        target = session
                        idempotent = bool(result.get("duplicate"))
                        status = HTTPStatus.OK
                        response = support.json_response({
                            "ok": True,
                            "redirect": f"/{route}/?session={session}",
                            "session": session,
                            "clientLaunchId": launch_id,
                        })
            except (UnicodeDecodeError, json.JSONDecodeError):
                status = HTTPStatus.BAD_REQUEST
                response = support.json_response({"ok": False, "error": "invalid JSON body"}, status)
            except ValueError as exc:
                status = HTTPStatus.BAD_REQUEST
                response = support.json_response({"ok": False, "error": str(exc)}, status)
        response.headers["X-Faryo-Request-Id"] = request_id
        support.append_audit(
            username_value=current,
            route=route,
            action="start",
            target=target,
            request_id=request_id,
            status=status,
            started=started,
            idempotent=idempotent,
        )
        return response

    return [
        Route("/api/agent/resume", agent_resume, methods=["POST"]),
        Route("/api/agent/new", agent_new, methods=["POST"]),
    ]
