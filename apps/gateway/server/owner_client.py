"""Authenticated bounded requests from Gateway to one configured Owner."""

from __future__ import annotations

import http.client
import json
from dataclasses import dataclass
from pathlib import Path
import secrets
import socket
import threading
from typing import Any, Callable, Mapping

from faryo_cli import error_contract


INTERNAL_HEADER_NAMES = {
    "host",
    "content-length",
    "x-owner-token",
    "x-faryo-owner-label",
    "x-faryo-user",
    "x-faryo-history-scope",
    "x-faryo-file-inbox-root",
    "x-faryo-workspace-root",
    "x-faryo-csrf",
}


@dataclass(frozen=True)
class OwnerResponse:
    status: int
    reason: str
    headers: list[tuple[str, str]]
    body: bytes


class OwnerTransportError(Exception):
    pass


class OwnerStream:
    def __init__(self, connection: http.client.HTTPConnection, response: http.client.HTTPResponse) -> None:
        self.connection = connection
        self.response = response
        self.status = response.status
        self.reason = response.reason
        self.headers = response.getheaders()
        self._close_lock = threading.Lock()
        self._closed = False
        response_raw = getattr(getattr(response, "fp", None), "raw", None)
        self._upstream_socket = getattr(connection, "sock", None) or getattr(response_raw, "_sock", None)

    def _read(self, operation: Callable[[], bytes]) -> bytes:
        with self._close_lock:
            if self._closed:
                return b""
        try:
            return operation()
        except (OSError, ValueError):
            with self._close_lock:
                if self._closed:
                    return b""
            raise

    def read(self, size: int | None = None) -> bytes:
        return self._read(lambda: self.response.read() if size is None else self.response.read(size))

    def readline(self) -> bytes:
        return self._read(self.response.readline)

    def set_timeout(self, value: float | None) -> None:
        with self._close_lock:
            if self._closed:
                return
            upstream_socket = self._upstream_socket
        if upstream_socket is not None:
            try:
                upstream_socket.settimeout(value)
            except OSError:
                return

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        upstream_socket = self._upstream_socket
        if upstream_socket is not None:
            try:
                upstream_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.connection.close()


class OwnerClient:
    def __init__(
        self,
        backends: Mapping[str, tuple[str, int, str]],
        config: Any,
        *,
        encode_label: Callable[[str], str],
    ) -> None:
        self.backends = backends
        self.config = config
        self.encode_label = encode_label

    def headers(self, route: str, username: str) -> dict[str, str]:
        host, port, label = self.backends[route]
        headers = {
            "Host": f"{host}:{port}",
            "X-Faryo-Owner-Label": self.encode_label(label),
            "X-Owner-Token": self.config.owner_token(route),
            "X-Faryo-User": username,
        }
        if username != self.config.mcp_user:
            headers["X-Faryo-History-Scope"] = "workspace"
        if file_root := self.config.file_inbox_root(username, route):
            headers["X-Faryo-File-Inbox-Root"] = file_root
        if workspace_root := self.config.workspace_root(username, route):
            headers["X-Faryo-Workspace-Root"] = workspace_root
        return headers

    def json_request(
        self,
        route: str,
        path: str,
        payload: dict[str, Any] | None,
        username: str,
        *,
        method: str = "POST",
        timeout: float = 10,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        host, port, _label = self.backends[route]
        headers = self.headers(route, username)
        body = None
        if extra_headers:
            headers.update({key: value for key, value in extra_headers.items() if value})
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers.update({"Content-Type": "application/json; charset=utf-8", "Content-Length": str(len(body))})
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            response_body = response.read()
        except (OSError, UnicodeError) as exc:
            return error_contract.error_payload(
                str(exc),
                status=502,
                code="upstream_unavailable",
                retryable=True,
                extra={"transportError": True, "httpStatus": 502},
            )
        finally:
            connection.close()
        try:
            result = json.loads(response_body.decode("utf-8"))
        except Exception:
            result = error_contract.error_payload(
                "Owner returned an invalid response",
                status=response.status if response.status >= 400 else 502,
                code="invalid_response",
                retryable=True,
                extra={"httpStatus": response.status if response.status >= 400 else 502},
            )
        if response.status >= 400 and isinstance(result, dict):
            result.update({"ok": False, "httpStatus": response.status})
        if isinstance(result, dict):
            return result
        return error_contract.error_payload(
            "Owner returned an invalid response",
            status=502,
            code="invalid_response",
            retryable=True,
            extra={"httpStatus": 502},
        )

    def raw_request(
        self,
        route: str,
        method: str,
        path: str,
        body: bytes | None,
        username: str,
        *,
        forwarded_headers: Mapping[str, str] | None = None,
        timeout: float = 20,
    ) -> OwnerResponse:
        stream = self.open_stream(
            route,
            method,
            path,
            body,
            username,
            forwarded_headers=forwarded_headers,
            timeout=timeout,
        )
        try:
            return OwnerResponse(stream.status, stream.reason, stream.headers, stream.read())
        finally:
            stream.close()

    def open_stream(
        self,
        route: str,
        method: str,
        path: str,
        body: bytes | None,
        username: str,
        *,
        forwarded_headers: Mapping[str, str] | None = None,
        timeout: float = 20,
    ) -> OwnerStream:
        host, port, _label = self.backends[route]
        headers = self.headers(route, username)
        if forwarded_headers:
            headers.update({key: value for key, value in forwarded_headers.items() if key.lower() not in INTERNAL_HEADER_NAMES})
        if body is not None:
            headers["Content-Length"] = str(len(body))
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            connection.request(method, path, body=body, headers=headers)
            return OwnerStream(connection, connection.getresponse())
        except (OSError, UnicodeError) as exc:
            connection.close()
            raise OwnerTransportError("upstream unavailable") from exc

    def attachment_request(self, route: str, path: Path, mime_type: str, filename: str, username: str) -> dict[str, Any]:
        host, port, _label = self.backends[route]
        boundary = "----FaryoBoundary" + secrets.token_hex(12)
        safe_name = Path(filename).name.replace('"', "_").replace("\r", "_").replace("\n", "_") or path.name
        data = path.read_bytes()
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("utf-8")
        headers = self.headers(route, username)
        headers.update({"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(len(body))})
        connection = http.client.HTTPConnection(host, port, timeout=20)
        try:
            connection.request("POST", "/api/attachment", body=body, headers=headers)
            response = connection.getresponse()
            response_body = response.read()
        except OSError as exc:
            return error_contract.error_payload(
                str(exc),
                status=502,
                code="upstream_unavailable",
                retryable=True,
                extra={"transportError": True, "httpStatus": 502},
            )
        finally:
            connection.close()
        try:
            result = json.loads(response_body.decode("utf-8"))
        except Exception:
            result = error_contract.error_payload(
                "Owner returned an invalid response",
                status=response.status if response.status >= 400 else 502,
                code="invalid_response",
                retryable=True,
                extra={"httpStatus": response.status if response.status >= 400 else 502},
            )
        if response.status >= 400 and isinstance(result, dict):
            result.update({"ok": False, "httpStatus": response.status})
        if isinstance(result, dict):
            return result
        return error_contract.error_payload(
            "Owner returned an invalid response",
            status=502,
            code="invalid_response",
            retryable=True,
            extra={"httpStatus": 502},
        )
