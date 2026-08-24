"""Starlette control proxy, history lifecycle, and session-revoke routes."""

from __future__ import annotations

from http import HTTPStatus
import json
import secrets
import time
from typing import Any

from anyio import to_thread
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

import gateway_security
import owner_client
from faryo_cli import browser_contract


def routes(legacy: Any, config: Any, client: owner_client.OwnerClient, support: Any) -> list[Route]:
    async def owner_control(request: Request) -> Response:
        current = support.username(request)
        if not current:
            return support.json_response({"ok": False, "error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
        route = str(request.path_params["route"])
        upstream_path = "/api/" + str(request.path_params["tail"])
        action = legacy.PROXY_CONTROL_ACTIONS.get(upstream_path)
        if route not in legacy.BACKENDS:
            return support.json_response({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
        request_id = secrets.token_hex(8) if action else ""
        started = time.monotonic()
        status = HTTPStatus.BAD_GATEWAY
        target = ""
        idempotent = False
        if not config.allowed_route(current, route):
            status = HTTPStatus.FORBIDDEN
            response = support.json_response({"ok": False, "error": "forbidden"}, status)
        else:
            supplied_csrf = request.headers.get(legacy.CSRF_HEADER, "").strip()
            expected_csrf = gateway_security.csrf_token(config.cookie_secret, current, config.auth_epoch(current))
            if not supplied_csrf or not secrets.compare_digest(supplied_csrf, expected_csrf):
                status = HTTPStatus.FORBIDDEN
                response = support.json_response({"ok": False, "error": "csrf required"}, status)
            else:
                body = await request.body()
                target = legacy.control_target_from_json(body)
                forwarded = support.forwarded_request_headers(request)
                path = upstream_path + (f"?{request.url.query}" if request.url.query else "")
                try:
                    upstream = await to_thread.run_sync(
                        lambda: client.raw_request(
                            route,
                            request.method,
                            path,
                            body,
                            current,
                            forwarded_headers=forwarded,
                        )
                    )
                    status = upstream.status
                    try:
                        result = json.loads(upstream.body.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        result = {}
                    if isinstance(result, dict):
                        target = str(result.get("session") or target)
                        idempotent = bool(result.get("duplicate") or result.get("idempotent"))
                    if isinstance(result, dict) and result.get("ok") is False:
                        response = support.json_response(
                            support.forwarded_error(result, status, f"Owner {action or 'action'} failed"),
                            status,
                        )
                    elif status >= 400 and (not isinstance(result, dict) or not result):
                        response = support.json_response(
                            support.forwarded_error({}, status, "Owner returned an invalid response"),
                            status,
                        )
                    else:
                        response = Response(
                            upstream.body,
                            status_code=status,
                            headers=support.forwarded_response_headers(upstream.headers),
                        )
                except owner_client.OwnerTransportError:
                    status = HTTPStatus.BAD_GATEWAY
                    response = support.json_response({"ok": False, "error": "upstream unavailable"}, status)
        if action:
            response.headers["X-Faryo-Request-Id"] = request_id
            support.append_audit(
                username_value=current,
                route=route,
                action=action,
                target=target,
                request_id=request_id,
                status=status,
                started=started,
                idempotent=idempotent,
            )
        return response

    async def session_history_lifecycle(request: Request) -> Response:
        current = support.username(request)
        if not current:
            return support.json_response({"ok": False, "error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
        request_id = secrets.token_hex(8)
        started = time.monotonic()
        archived = request.url.path == "/api/session-history/archive"
        action = "archive" if archived else "unarchive"
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
                route = str(payload.get("route") or "").strip().lower()
                target = legacy.clean_agent_session_id(
                    str(payload.get("agent_session_id") or payload.get("agentSessionId") or "")
                ) or ""
                if route not in legacy.BACKENDS or not target:
                    raise ValueError("route and agent_session_id are required")
                if not config.allowed_route(current, route):
                    status = HTTPStatus.FORBIDDEN
                    response = support.json_response({"ok": False, "error": "forbidden"}, status)
                else:
                    result = await to_thread.run_sync(
                        lambda: client.json_request(
                            route,
                            f"/api/agent-session/{action}",
                            {"agent_session_id": target},
                            current,
                            timeout=10,
                        )
                    )
                    if not result.get("ok"):
                        status = support.upstream_status(result)
                        response = support.json_response(
                            support.forwarded_error(result, status, f"Owner {action} failed"),
                            status,
                        )
                    else:
                        idempotent = bool(result.get("duplicate"))
                        status = HTTPStatus.OK
                        response = support.json_response({
                            "ok": True,
                            "agentSessionId": target,
                            "archived": bool(result.get("archived")),
                            "duplicate": idempotent,
                        })
            except (UnicodeDecodeError, json.JSONDecodeError):
                status = HTTPStatus.BAD_REQUEST
                response = support.json_response({"ok": False, "error": "invalid JSON body"}, status)
            except (TypeError, ValueError) as exc:
                status = HTTPStatus.BAD_REQUEST
                response = support.json_response({"ok": False, "error": str(exc)}, status)
        response.headers["X-Faryo-Request-Id"] = request_id
        support.append_audit(
            username_value=current,
            route=route,
            action=action,
            target=target,
            request_id=request_id,
            status=status,
            started=started,
            idempotent=idempotent,
        )
        return response

    async def revoke_sessions(request: Request) -> Response:
        current = support.username(request)
        if not current:
            return support.json_response({"ok": False, "error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
        request_id = secrets.token_hex(8)
        started = time.monotonic()
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
                if payload.get("confirm") != "revoke":
                    raise ValueError("explicit revoke confirmation is required")
                config.revoke_sessions(current)
                status = HTTPStatus.OK
                response = support.json_response({"ok": True, "signedOut": True})
            except (UnicodeDecodeError, json.JSONDecodeError):
                status = HTTPStatus.BAD_REQUEST
                response = support.json_response({"ok": False, "error": "invalid JSON body"}, status)
            except ValueError as exc:
                status = HTTPStatus.BAD_REQUEST
                response = support.json_response({"ok": False, "error": str(exc)}, status)
        response.headers["X-Faryo-Request-Id"] = request_id
        support.append_audit(
            username_value=current,
            route="",
            action="revoke-sessions",
            target=current,
            request_id=request_id,
            status=status,
            started=started,
        )
        return response

    return [
        Route("/{route}/api/{tail:path}", owner_control, methods=["POST"]),
        Route("/api/session-history/archive", session_history_lifecycle, methods=["POST"]),
        Route("/api/session-history/unarchive", session_history_lifecycle, methods=["POST"]),
        Route("/api/auth/revoke-all", revoke_sessions, methods=["POST"]),
    ]


def direct_api_fallback_route(legacy: Any, config: Any, support: Any) -> Route:
    async def direct_api_fallback(request: Request) -> Response:
        current = support.username(request)
        if not current:
            return support.json_response({"ok": False, "error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
        supplied_csrf = request.headers.get(legacy.CSRF_HEADER, "").strip()
        expected_csrf = gateway_security.csrf_token(config.cookie_secret, current, config.auth_epoch(current))
        if not supplied_csrf or not secrets.compare_digest(supplied_csrf, expected_csrf):
            return support.json_response({"ok": False, "error": "csrf required"}, HTTPStatus.FORBIDDEN)
        return support.json_response({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)

    return Route("/api/{tail:path}", direct_api_fallback, methods=["POST"])
