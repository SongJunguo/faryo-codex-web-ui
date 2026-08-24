"""Shared Starlette response, auth, audit, and bounded-body support."""

from __future__ import annotations

from http import HTTPStatus
import json
import secrets
import time
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

import gateway_security
import owner_client
from faryo_cli import browser_contract
from faryo_cli import error_contract


class SecurityHeadersMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_security(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                nonce = str(scope.get("state", {}).get("csp_nonce") or "")
                for name, value in gateway_security.browser_security_headers(nonce).items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_security)


class AsgiSupport:
    def __init__(self, legacy: Any, config: Any) -> None:
        self.legacy = legacy
        self.config = config

    def codec(self) -> gateway_security.SessionCookieCodec:
        return gateway_security.SessionCookieCodec(
            self.config.cookie_secret,
            name=self.legacy.COOKIE_NAME,
            max_age=self.legacy.COOKIE_MAX_AGE,
            same_site=self.legacy.COOKIE_SAME_SITE,
        )

    def username(self, request: Request) -> str | None:
        return self.codec().username(request.headers.get("cookie", ""), self.config.users, self.config.auth_epoch)

    def html_page(self, request: Request, value: str, status: int = HTTPStatus.OK) -> HTMLResponse:
        nonce = secrets.token_urlsafe(18)
        request.scope.setdefault("state", {})["csp_nonce"] = nonce
        body = value.replace(self.legacy.CSP_NONCE_PLACEHOLDER, nonce)
        return HTMLResponse(body, status_code=status, headers={"Cache-Control": "no-store"})

    @staticmethod
    def json_response(value: dict[str, Any], status: int = HTTPStatus.OK) -> Response:
        if value.get("ok") is False:
            value = error_contract.normalize_error_payload(value, int(status))
        payload = browser_contract.wrap_response(value) if "ok" in value else dict(value)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return Response(body, status_code=status, headers={"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"})

    @staticmethod
    def forwarded_error(value: dict[str, Any] | None, status: int, fallback: str) -> dict[str, Any]:
        return error_contract.forward_error_payload(value, status=int(status), fallback=fallback)

    @staticmethod
    def upstream_status(value: dict[str, Any] | None, fallback: int = HTTPStatus.BAD_GATEWAY) -> int:
        try:
            status = int((value or {}).get("httpStatus") or fallback)
        except (TypeError, ValueError):
            return int(fallback)
        return status if 100 <= status <= 599 else int(fallback)

    def forwarded_request_headers(self, request: Request) -> dict[str, str]:
        return {
            key: value for key, value in request.headers.items()
            if key.lower() not in self.legacy.HOP_BY_HOP_HEADERS and key.lower() not in owner_client.INTERNAL_HEADER_NAMES
        }

    def forwarded_response_headers(self, headers: list[tuple[str, str]]) -> dict[str, str]:
        forwarded: dict[str, str] = {}
        for key, value in headers:
            lower = key.lower()
            if lower in self.legacy.HOP_BY_HOP_HEADERS or lower in self.legacy.UPSTREAM_SECURITY_HEADERS or lower == "content-length":
                continue
            forwarded[key] = value
        return forwarded

    def append_audit(
        self,
        *,
        username_value: str,
        route: str,
        action: str,
        target: str,
        request_id: str,
        status: int,
        started: float,
        idempotent: bool = False,
    ) -> None:
        writer = getattr(self.config, "append_control_audit", None)
        if not callable(writer):
            return
        try:
            writer(
                username=username_value,
                route=route,
                action=action,
                target=target,
                request_id=request_id,
                status=int(status),
                duration_ms=round((time.monotonic() - started) * 1000),
                idempotent=idempotent,
            )
        except Exception:
            return

    @staticmethod
    async def read_json_body(request: Request, max_bytes: int) -> dict[str, Any]:
        body = await request.body()
        if not body:
            raise ValueError("empty JSON body")
        if len(body) > max_bytes:
            raise ValueError("request too large")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise ValueError("invalid JSON object")
        browser_contract.require_supported_version(payload)
        return payload
