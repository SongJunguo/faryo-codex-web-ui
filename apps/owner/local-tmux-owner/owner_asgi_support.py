"""Shared auth, bounded-body, and response support for the Owner ASGI app."""

from __future__ import annotations

from email import policy
from email.parser import BytesParser
from http import HTTPStatus
import json
from pathlib import Path
import secrets
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import FileResponse, Response

import owner_http
import session_namespace
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
                for name, value in owner_http.browser_security_headers().items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_security)


class OwnerAsgiSupport:
    def __init__(self, core: Any, config: Any, runtime: Any) -> None:
        self.core = core
        self.config = config
        self.runtime = runtime
        self.session_namespace = session_namespace.SessionNamespace(
            terminal_names=lambda: core.tmux_sessions(config),
            app_server_names=lambda: (
                str(record.get("session") or "")
                for record in runtime.session_records()
            ),
        )

    @staticmethod
    def json_response(value: dict[str, Any], status: int = HTTPStatus.OK) -> Response:
        if value.get("ok") is False:
            value = error_contract.normalize_error_payload(value, int(status))
        body = json.dumps(
            browser_contract.wrap_response(value),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return Response(
            body,
            status_code=int(status),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Cache-Control": "no-store",
            },
        )

    def require_token(self, request: Request) -> None:
        supplied = request.headers.get("X-Owner-Token") or request.query_params.get("token")
        if not supplied or not secrets.compare_digest(supplied, self.config.token):
            raise self.core.OwnerError("unauthorized", HTTPStatus.UNAUTHORIZED)

    async def bounded_body(self, request: Request, max_bytes: int) -> bytes:
        try:
            declared = int(request.headers.get("content-length", "0") or "0")
        except ValueError as exc:
            raise self.core.OwnerError("invalid content length") from exc
        if declared > max_bytes:
            raise self.core.OwnerError("request too large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > max_bytes:
                raise self.core.OwnerError("request too large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        return bytes(body)

    async def read_json(self, request: Request, max_bytes: int = 1_000_000) -> dict[str, Any]:
        raw = await self.bounded_body(request, max_bytes)
        try:
            value = json.loads(raw.decode("utf-8", errors="strict") if raw else "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise self.core.OwnerError("invalid json") from exc
        if not isinstance(value, dict):
            raise self.core.OwnerError("json body must be an object")
        try:
            browser_contract.require_supported_version(value)
        except browser_contract.BrowserContractError as exc:
            raise self.core.OwnerError(str(exc), HTTPStatus.CONFLICT) from exc
        return value

    async def read_multipart_form(self, request: Request) -> dict[str, Any]:
        content_type = request.headers.get("content-type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            raise self.core.OwnerError("expected multipart/form-data")
        raw = await self.bounded_body(
            request,
            self.core.MAX_ATTACHMENT_UPLOAD_BYTES + 1_000_000,
        )
        if not raw:
            raise self.core.OwnerError("empty request")
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + content_type.encode("utf-8") + b"\r\n\r\n" + raw
        )
        form: dict[str, Any] = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            item = owner_http.MultipartFile(
                part.get_filename() or "",
                part.get_content_type(),
                part.get_payload(decode=True) or b"",
            )
            if name in form:
                form[name] = form[name] + [item] if isinstance(form[name], list) else [form[name], item]
            else:
                form[name] = item
        return form

    def workspace_root(self, request: Request) -> str | None:
        value = request.headers.get("X-Faryo-Workspace-Root")
        return value.strip() if value and value.strip() else None

    def history_root(self, request: Request) -> str | None:
        if request.headers.get("X-Faryo-History-Scope", "").strip().lower() != "workspace":
            return None
        return self.workspace_root(request) or ""

    @staticmethod
    def inbox_root(request: Request) -> str | None:
        value = request.headers.get("X-Faryo-File-Inbox-Root")
        return value.strip() if value and value.strip() else None

    def target(self, session: str | None) -> Any:
        if self.session_owner(session) == session_namespace.APP_SERVER_OWNER:
            raise self.core.OwnerError(
                "session belongs to Codex App Server",
                HTTPStatus.CONFLICT,
            )
        return self.core.target_config(self.config, session)

    def session_owner(self, session: str | None) -> str | None:
        try:
            return self.session_namespace.owner(session)
        except session_namespace.SessionNamespaceConflict as exc:
            raise self.core.OwnerError(
                "session name is owned by multiple backends; return Home and reopen the session",
                HTTPStatus.CONFLICT,
            ) from exc

    def is_app_server_session(self, session: str | None) -> bool:
        return self.session_owner(session) == session_namespace.APP_SERVER_OWNER

    def app_server_session_names(self) -> list[str]:
        return self.session_namespace.reserved_for_terminal()

    def ensure_unambiguous_session_namespace(self) -> None:
        if self.session_namespace.collisions():
            raise self.core.OwnerError(
                "session names are owned by multiple backends; reload after Owner recovery",
                HTTPStatus.CONFLICT,
            )

    @staticmethod
    def file_response(path: Path, content_type: str, *, download: bool = False) -> FileResponse:
        return FileResponse(
            path,
            media_type=content_type,
            filename=path.name if download else None,
            content_disposition_type="attachment" if download else "inline",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    def error_response(self, error: BaseException) -> Response:
        status = getattr(error, "status", HTTPStatus.INTERNAL_SERVER_ERROR)
        known = isinstance(error, self.core.OwnerError)
        message = str(error) if known else ""
        payload = error_contract.error_payload(
            message,
            status=int(status),
            code=str(getattr(error, "code", "")) if known else "internal_error",
            title=str(getattr(error, "title", "")) if known else "",
            retryable=getattr(error, "retryable", None) if known else None,
            recovery=str(getattr(error, "recovery", "")) if known else "",
            extra={"updatedAt": self.core.now_iso()},
        )
        return self.json_response(
            payload,
            int(status),
        )
