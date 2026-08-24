"""Versioned, privacy-safe browser error envelopes shared by Faryo services."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping


ERROR_CONTRACT_VERSION = 1
ERROR_FORWARD_FIELDS = {
    "error",
    "errorCode",
    "errorTitle",
    "retryable",
    "recovery",
    "transportError",
    "httpStatus",
    "updatedAt",
}
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]+")
_SECRET_RE = re.compile(
    r"(?i)\b(token|secret|password|cookie|authorization)\s*[=:]\s*([^\s,;]+)"
)
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}\b")
_POSIX_PATH_RE = re.compile(r"(?<![A-Za-z0-9_.-])(?:~|/(?:home|root|tmp|var|run|etc))(?:/[^\s,;:]+)+")
_WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:\\(?:[^\s,;:]+\\)*[^\s,;:]+")


@dataclass(frozen=True)
class ErrorDefinition:
    title: str
    message: str
    recovery: str = ""
    retryable: bool = False


ERROR_DEFINITIONS: dict[str, ErrorDefinition] = {
    "auth_required": ErrorDefinition(
        "Sign-in required",
        "Your Faryo sign-in is no longer valid.",
        "Refresh this page and sign in again.",
    ),
    "csrf_required": ErrorDefinition(
        "Session check failed",
        "Faryo could not verify this browser action.",
        "Refresh the page and retry the action.",
    ),
    "forbidden": ErrorDefinition(
        "Permission denied",
        "This account is not allowed to perform that action.",
    ),
    "not_found": ErrorDefinition(
        "No longer available",
        "The requested item is no longer available.",
        "Refresh the page to load the current state.",
    ),
    "invalid_request": ErrorDefinition(
        "Check the request",
        "Faryo could not use the supplied values.",
    ),
    "request_too_large": ErrorDefinition(
        "Request too large",
        "The request is larger than Faryo accepts.",
        "Reduce the text or attachment size and retry.",
    ),
    "client_outdated": ErrorDefinition(
        "Page update required",
        "This page is using an older Faryo browser protocol.",
        "Reload the page normally; a hard refresh is not required.",
    ),
    "thread_in_use": ErrorDefinition(
        "Conversation still open",
        "This conversation is still open in another Codex client.",
        "Close that Codex CLI, IDE, or Faryo session and retry.",
    ),
    "backend_conflict": ErrorDefinition(
        "Session routing conflict",
        "Faryo found conflicting backend ownership for this session.",
        "Return Home and reload so Faryo can rebuild the current session map.",
    ),
    "conflict": ErrorDefinition(
        "Action blocked",
        "The current state does not allow that action.",
        "Refresh the page, check the current session state, and retry.",
    ),
    "selection_expired": ErrorDefinition(
        "Folder selection expired",
        "The selected working directory can no longer be verified.",
        "Open the folder picker again and repeat the selection.",
    ),
    "directory_required": ErrorDefinition(
        "Choose a working directory",
        "This conversation needs an available working directory before it can resume.",
        "Open Resume options and choose a folder.",
    ),
    "agent_limit": ErrorDefinition(
        "Agent limit reached",
        "This workstation is already running the configured number of Codex sessions.",
        "Close an unused running session and retry.",
    ),
    "appserver_reconnecting": ErrorDefinition(
        "Codex reconnecting",
        "Codex App Server is reconnecting.",
        "Wait a moment and retry the action.",
        True,
    ),
    "codex_not_ready": ErrorDefinition(
        "Codex not ready",
        "Codex has not finished preparing this session.",
        "Wait for the session status to become ready and retry.",
        True,
    ),
    "rate_limited": ErrorDefinition(
        "Temporarily limited",
        "The service is temporarily rate limited.",
        "Wait until the indicated reset time and retry.",
        True,
    ),
    "timeout": ErrorDefinition(
        "Request timed out",
        "Faryo did not receive a definitive result in time.",
        "Check the current session state before retrying the action.",
        True,
    ),
    "network_unavailable": ErrorDefinition(
        "Connection unavailable",
        "The browser could not reach Faryo.",
        "Check this device's network connection and retry.",
        True,
    ),
    "upstream_unavailable": ErrorDefinition(
        "Faryo temporarily unavailable",
        "The workstation service is restarting or temporarily unavailable.",
        "Wait a moment and retry; your conversation history is retained.",
        True,
    ),
    "invalid_response": ErrorDefinition(
        "Invalid server response",
        "Faryo received a response it could not safely interpret.",
        "Reload the page and retry. If it persists, check Faryo service health.",
        True,
    ),
    "internal_error": ErrorDefinition(
        "Something went wrong",
        "Faryo encountered an internal error.",
        "Retry once. If it persists, inspect the privacy-safe service diagnostics.",
        True,
    ),
}
CANONICAL_MESSAGE_CODES = {
    "thread_in_use",
    "backend_conflict",
    "upstream_unavailable",
    "invalid_response",
    "internal_error",
}


def sanitize_public_message(value: Any, *, limit: int = 400) -> str:
    text = _CONTROL_RE.sub(" ", str(value or "")).strip()
    text = _SECRET_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    text = _UUID_RE.sub("<private-id>", text)
    text = _POSIX_PATH_RE.sub("<private-path>", text)
    text = _WINDOWS_PATH_RE.sub("<private-path>", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def infer_error_code(status: int, message: Any) -> str:
    value = str(message or "").lower()
    if status == 401 or "unauthorized" in value or "sign-in expired" in value:
        return "auth_required"
    if "csrf" in value:
        return "csrf_required"
    if "unsupported" in value and ("envelope" in value or "protocol" in value):
        return "client_outdated"
    if "request too large" in value:
        return "request_too_large"
    if "working directory" in value and ("expired" in value or "selection" in value and "invalid" in value):
        return "selection_expired"
    if "working directory" in value and ("required" in value or "unavailable" in value):
        return "directory_required"
    if "agent limit" in value or "running agent limit" in value:
        return "agent_limit"
    if "multiple backends" in value or "conflicting backend" in value:
        return "backend_conflict"
    if (
        any(marker in value for marker in ("active", "loaded", "owned", "in use", "already open", "descendant"))
        and any(subject in value for subject in ("thread", "session", "conversation", "agent"))
    ):
        return "thread_in_use"
    if "app server" in value and any(marker in value for marker in ("reconnect", "unavailable", "not started")):
        return "appserver_reconnecting"
    if "codex" in value and any(marker in value for marker in ("not ready", "did not become ready", "still stopping")):
        return "codex_not_ready"
    if status in {408, 504} or "timed out" in value or "timeout" in value:
        return "timeout"
    if "network" in value or "could not reach" in value:
        return "network_unavailable"
    if status == 429:
        return "rate_limited"
    if status == 413:
        return "request_too_large"
    if status == 403:
        return "forbidden"
    if status == 404:
        return "not_found"
    if status == 409:
        return "conflict"
    if status in {502, 503}:
        return "upstream_unavailable"
    if status >= 500:
        return "internal_error"
    return "invalid_request"


def error_payload(
    message: Any = "",
    *,
    status: int = 500,
    code: str = "",
    title: str = "",
    retryable: bool | None = None,
    recovery: str = "",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    selected_code = code if code in ERROR_DEFINITIONS else infer_error_code(int(status), message)
    definition = ERROR_DEFINITIONS[selected_code]
    supplied_message = sanitize_public_message(message)
    if selected_code in CANONICAL_MESSAGE_CODES:
        supplied_message = ""
    # Unknown server-side exceptions must never become public diagnostics.
    if int(status) >= 500 and selected_code in {"internal_error", "upstream_unavailable", "invalid_response"}:
        supplied_message = ""
    payload: dict[str, Any] = {
        "ok": False,
        "errorContractVersion": ERROR_CONTRACT_VERSION,
        "errorCode": selected_code,
        "errorTitle": sanitize_public_message(title, limit=96) or definition.title,
        "error": supplied_message or definition.message,
        "retryable": definition.retryable if retryable is None else bool(retryable),
        "recovery": sanitize_public_message(recovery) or definition.recovery,
    }
    if extra:
        for key in ERROR_FORWARD_FIELDS:
            if key in extra and key not in payload:
                payload[key] = extra[key]
    return payload


def normalize_error_payload(value: Mapping[str, Any], status: int) -> dict[str, Any]:
    incoming = dict(value)
    selected = error_payload(
        incoming.get("error"),
        status=int(status),
        code=str(incoming.get("errorCode") or ""),
        title=str(incoming.get("errorTitle") or ""),
        retryable=incoming.get("retryable") if isinstance(incoming.get("retryable"), bool) else None,
        recovery=str(incoming.get("recovery") or ""),
        extra=incoming,
    )
    return selected


def forward_error_payload(
    value: Mapping[str, Any] | None,
    *,
    status: int,
    fallback: str,
) -> dict[str, Any]:
    incoming = dict(value or {})
    if not incoming.get("error"):
        incoming["error"] = fallback
    return normalize_error_payload(incoming, status)
