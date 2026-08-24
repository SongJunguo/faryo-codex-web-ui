"""Version-tolerant primitives for the Codex App Server JSON-RPC protocol."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import re
from typing import Any, Mapping


OVERLOADED_ERROR_CODE = -32001
METHOD_NOT_FOUND_ERROR_CODE = -32601
INVALID_MESSAGE_ERROR_CODE = -32600
CODEX_VERSION_RE = re.compile(r"(?:codex-cli\s+)?(?P<version>[0-9]+(?:\.[0-9]+){2})")

REQUIRED_REQUEST_METHODS = frozenset({
    "initialize",
    "thread/read",
    "thread/resume",
    "thread/start",
    "turn/interrupt",
    "turn/start",
    "turn/steer",
})
REQUIRED_NOTIFICATION_METHODS = frozenset({
    "item/agentMessage/delta",
    "item/completed",
    "item/started",
    "turn/completed",
    "turn/started",
})


class ProtocolMessageKind(str, Enum):
    RESPONSE = "response"
    ERROR = "error"
    NOTIFICATION = "notification"
    SERVER_REQUEST = "server_request"


class AppServerError(RuntimeError):
    """Base error for the Faryo App Server adapter."""


class AppServerProtocolError(AppServerError):
    """The peer sent a message that cannot be handled safely."""


class AppServerUnavailable(AppServerError):
    """The private App Server transport isn't ready."""


class AppServerRequestError(AppServerError):
    """A JSON-RPC request returned an error response."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@dataclass(frozen=True)
class DecodedMessage:
    kind: ProtocolMessageKind
    body: dict[str, Any]


def parse_codex_version(output: str) -> str:
    match = CODEX_VERSION_RE.search(output.strip())
    return match.group("version") if match else ""


def decode_wire_message(raw: str | bytes | Mapping[str, Any]) -> DecodedMessage:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AppServerProtocolError("App Server sent non-UTF-8 data") from exc
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AppServerProtocolError("App Server sent invalid JSON") from exc
    else:
        value = dict(raw)
    if not isinstance(value, dict):
        raise AppServerProtocolError("App Server message must be an object")

    has_id = "id" in value
    method = value.get("method")
    if isinstance(method, str) and method:
        return DecodedMessage(
            ProtocolMessageKind.SERVER_REQUEST if has_id else ProtocolMessageKind.NOTIFICATION,
            value,
        )
    if has_id and isinstance(value.get("error"), dict):
        return DecodedMessage(ProtocolMessageKind.ERROR, value)
    if has_id and "result" in value:
        return DecodedMessage(ProtocolMessageKind.RESPONSE, value)
    raise AppServerProtocolError("App Server message has no supported JSON-RPC shape")


def error_from_response(body: Mapping[str, Any]) -> AppServerRequestError:
    error = body.get("error")
    if not isinstance(error, Mapping):
        return AppServerRequestError(INVALID_MESSAGE_ERROR_CODE, "App Server returned an invalid error")
    try:
        code = int(error.get("code", 0))
    except (TypeError, ValueError):
        code = 0
    message = str(error.get("message") or "App Server request failed")
    return AppServerRequestError(code, message, error.get("data"))


def response_message(request_id: Any, result: Any) -> dict[str, Any]:
    return {"id": request_id, "result": result}


def error_message(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"id": request_id, "error": error}


def item_identity(params: Mapping[str, Any]) -> tuple[str, str, str] | None:
    thread_id = params.get("threadId")
    turn_id = params.get("turnId")
    item = params.get("item")
    item_id = params.get("itemId")
    if not isinstance(item_id, str) and isinstance(item, Mapping):
        item_id = item.get("id")
    if all(isinstance(value, str) and value for value in (thread_id, turn_id, item_id)):
        return str(thread_id), str(turn_id), str(item_id)
    return None


def agent_message_text(item: Mapping[str, Any]) -> str:
    return str(item.get("text") or "") if item.get("type") == "agentMessage" else ""
