#!/usr/bin/env python3
"""Gateway runtime constants, pure policies, HTML templates, and composition adapters."""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import socket
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse

GATEWAY_MODULE_DIR = Path(__file__).resolve().parent
if str(GATEWAY_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_MODULE_DIR))
import gateway_security
import bridge_packages
import gateway_config

SHARED_DIR = Path(__file__).resolve().parents[2] / "shared"
SHARED_STATIC_DIR = SHARED_DIR / "static"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))


def gateway_session_max_age(values: Any) -> int:
    raw = str(values.get("FARYO_GATEWAY_SESSION_HOURS", "720")).strip()
    try:
        hours = int(raw)
    except ValueError as exc:
        raise ValueError("FARYO_GATEWAY_SESSION_HOURS must be an integer from 1 to 720") from exc
    if not 1 <= hours <= 720:
        raise ValueError("FARYO_GATEWAY_SESSION_HOURS must be an integer from 1 to 720")
    return hours * 60 * 60


COOKIE_NAME = "__Host-faryo_auth"
LEGACY_COOKIE_NAME = "faryo_auth"
COOKIE_MAX_AGE = gateway_session_max_age(os.environ)
COOKIE_SAME_SITE = "Strict"
CSRF_HEADER = "X-Faryo-Csrf"
LOGIN_RATE_WINDOW_SECONDS = 10 * 60
LOGIN_RATE_BLOCK_SECONDS = 5 * 60
LOGIN_RATE_MAX_FAILURES = 8
ROUTE_DEFAULTS = {
    "hp": (18766, "Home workstation"),
    "txy": (8765, "Ubuntu 工作站"),
    "pc": (18765, "Windows PC"),
}


def backend_from_values(route: str, default_port: int, default_label: str, values: Any) -> tuple[str, int, str]:
    prefix = f"FARYO_{route.upper()}_OWNER"
    host = str(values.get(f"{prefix}_HOST", "127.0.0.1")).strip() or "127.0.0.1"
    port = int(values.get(f"{prefix}_PORT", str(default_port)))
    label = str(values.get(f"{prefix}_LABEL", default_label)).strip() or default_label
    return host, port, label


def owner_label_header_value(label: str) -> str:
    """Encode a user-facing Unicode label into an HTTP/1.1-safe header value."""
    return quote(label.strip()[:32], safe="-._~")


def configured_routes(values: Any) -> list[str]:
    raw = str(values.get("FARYO_GATEWAY_ROUTES", ",".join(ROUTE_DEFAULTS)))
    requested: list[str] = []
    unknown: list[str] = []
    for item in raw.split(","):
        route = item.strip().lower()
        if not route:
            continue
        if route not in ROUTE_DEFAULTS:
            unknown.append(route)
        elif route not in requested:
            requested.append(route)
    if unknown:
        raise ValueError("unsupported FARYO_GATEWAY_ROUTES: " + ", ".join(unknown))
    if not requested:
        raise ValueError("FARYO_GATEWAY_ROUTES has no valid route")
    return requested


def load_backends(values: Any) -> dict[str, tuple[str, int, str]]:
    backends: dict[str, tuple[str, int, str]] = {}
    for route in configured_routes(values):
        default_port, default_label = ROUTE_DEFAULTS[route]
        backends[route] = backend_from_values(route, default_port, default_label, values)
    return backends


BACKENDS = load_backends(os.environ)
SESSION_MAX_RUNNING_DEFAULTS = {"txy": 8, "hp": 4, "pc": 4}
SESSION_MAX_RUNNING_LIMIT = 32
WORKORDER_RECEIPT_WATCH_INTERVAL_SECONDS = 20
WORKORDER_RECEIPT_WATCH_ATTEMPTS = 90
NEW_SESSION_COMMANDS = {"codex"}
CONTEXT_WINDOW_MIN_K = 32
CONTEXT_WINDOW_MAX_K = 1050
HISTORY_PAGE_SIZE = 10
HISTORY_MAX_FETCH = 1000
HISTORY_QUERY_MAX_CHARS = 96
HISTORY_PERIODS = {"all", "today", "7d", "30d"}
HISTORY_ARCHIVE_FILTERS = {"active", "archived", "all"}
SESSION_STATES = {"starting", "running", "waiting", "exited", "desktop", "resumable", "archived"}
SESSION_STATE_PRIORITY = {"running": 6, "starting": 5, "waiting": 4, "desktop": 3, "exited": 2, "resumable": 1, "archived": 0}
PROXY_CONTROL_ACTIONS = {
    "/api/send": "send",
    "/api/interrupt": "interrupt",
    "/api/session/close": "close",
    "/api/interaction/start": "command",
    "/api/interaction/respond": "interaction",
}
DIRECT_CONTROL_ACTIONS = {
    "/api/agent/new": "start",
    "/api/agent/resume": "resume",
    "/api/session-history/archive": "archive",
    "/api/session-history/unarchive": "unarchive",
    "/api/bridge-inject": "file-inject",
    "/api/auth/revoke-all": "revoke-sessions",
}
STATIC_DIR = Path(__file__).resolve().parent / "static"
GATEWAY_STATIC_FILES = {
    "workbench.css": "text/css; charset=utf-8",
    "workbench.js": "text/javascript; charset=utf-8",
    "workbench-preact.js": "text/javascript; charset=utf-8",
    "workbench-preact.LICENSE.txt": "text/plain; charset=utf-8",
}


def gateway_asset_revision(paths: list[Path] | None = None) -> str:
    digest = hashlib.sha256()
    if paths is None:
        paths = [
            *(STATIC_DIR / name for name in sorted(GATEWAY_STATIC_FILES)),
            SHARED_STATIC_DIR / "appearance.css",
            SHARED_STATIC_DIR / "appearance.js",
            STATIC_DIR / "icons/favicon.png",
            STATIC_DIR / "icons/faryo-mark.png",
            STATIC_DIR / "icons/pwa-light-192.png",
            STATIC_DIR / "icons/pwa-light-512.png",
        ]
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"missing")
    return digest.hexdigest()[:12]


GATEWAY_ASSET_REVISION = gateway_asset_revision()
BRIDGE_PACKAGE_MAX_BYTES = 120 * 1024 * 1024
BRIDGE_ASSET_MAX_BYTES = 20 * 1024 * 1024
BRIDGE_ASSET_LIMIT = bridge_packages.BRIDGE_ASSET_LIMIT
BRIDGE_PENDING_RETENTION_SECONDS = bridge_packages.BRIDGE_PENDING_RETENTION_SECONDS
BRIDGE_DELIVERED_RETENTION_SECONDS = bridge_packages.BRIDGE_DELIVERED_RETENTION_SECONDS
BRIDGE_CLEANUP_INTERVAL_SECONDS = bridge_packages.BRIDGE_CLEANUP_INTERVAL_SECONDS
BRIDGE_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.presentation": ".odp",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "text/markdown": ".md",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "application/json": ".json",
    "application/rtf": ".rtf",
}
BRIDGE_SUFFIX_MIME = {suffix: mime for mime, suffix in BRIDGE_MIME_EXT.items()}
BRIDGE_SUFFIX_MIME[".jpeg"] = "image/jpeg"
MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_SERVER_VERSION = "1.0.6"
MCP_TOOL_NAME = "create_faryo_handoff_package"
MCP_ATTACHMENT_SCHEMA = {"anyOf": [{"type": "object", "additionalProperties": True}, {"type": "string"}]}
MCP_TOOL_SCHEMAS = {MCP_TOOL_NAME: {"type": "object", "properties": {"title": {"type": "string"}, "intent": {"type": "string"}, "context": {"type": "string"}, "prompt": {"type": "string"}, "attachment": MCP_ATTACHMENT_SCHEMA, "attachments": {"type": "array", "items": MCP_ATTACHMENT_SCHEMA}, "image": MCP_ATTACHMENT_SCHEMA, "images": {"type": "array", "items": MCP_ATTACHMENT_SCHEMA}}, "required": ["title", "intent", "context", "prompt"]}}
PWA_MANIFEST = {
    "id": "/",
    "name": "Faryo",
    "short_name": "Faryo",
    "description": "Self-hosted mobile and desktop workbench for Codex sessions",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "theme_color": "#F6F7F9",
    "background_color": "#F6F7F9",
    "icons": [
        {"src": f"/icons/pwa-light-192.png?v={GATEWAY_ASSET_REVISION}", "sizes": "192x192", "type": "image/png"},
        {"src": f"/icons/pwa-light-512.png?v={GATEWAY_ASSET_REVISION}", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
    ],
}
PWA_SW = """self.addEventListener('install',()=>self.skipWaiting());
self.addEventListener('activate',(event)=>{event.waitUntil(caches.keys().then((keys)=>Promise.all(keys.map((key)=>caches.delete(key)))).then(()=>self.clients.claim()));});
self.addEventListener('fetch',()=>{});
"""

OWNER_STATIC_FILES = {"appearance.css", "appearance.js", "app.js", "style.css", "index.html", "event-stream.js", "internal-annotations.js", "local-file-view.js", "stable-blocks.js", "question-navigator.js", "live-scroll.js", "compact-rules-codex.js", "codex-commands.js", "copy-fidelity.js", "clipboard-images.js", "immersive-mode.js", "keyboard-layout.js", "composer-layout.js", "owner-ui.js", "owner-ui.LICENSE.txt"}
OWNER_STATIC_PREFIXES = ("icons/", "pet/", "owner/", "vendor/katex/", "vendor/markdown-ast/", "vendor/diff-review/")
SHARED_STATIC_FILES = {
    "appearance.css": "text/css; charset=utf-8",
    "appearance.js": "text/javascript; charset=utf-8",
}
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
UPSTREAM_SECURITY_HEADERS = {
    "content-security-policy",
    "permissions-policy",
    "referrer-policy",
    "x-content-type-options",
    "x-frame-options",
}
LOGIN_LIMITER = gateway_security.LoginRateLimiter(
    window_seconds=LOGIN_RATE_WINDOW_SECONDS,
    block_seconds=LOGIN_RATE_BLOCK_SECONDS,
    max_failures=LOGIN_RATE_MAX_FAILURES,
)
CSP_NONCE_PLACEHOLDER = "__FARYO_CSP_NONCE__"


def html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def backend_status(route: str, timeout: float = 1.8) -> dict[str, Any]:
    host, port, label = BACKENDS[route]
    started = time.monotonic()
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        resp.read()
        elapsed_ms = round((time.monotonic() - started) * 1000)
    except OSError:
        return {
            "id": route,
            "label": label,
            "state": "offline",
            "stateText": "Off",
            "detail": "Owner backend is unreachable",
        }
    finally:
        try:
            conn.close()  # type: ignore[possibly-undefined]
        except Exception:
            pass
    if resp.status == 200:
        state = "slow" if elapsed_ms > 1500 else "online"
        return {
            "id": route,
            "label": label,
            "state": state,
            "stateText": f"{elapsed_ms}ms",
            "detail": f"{elapsed_ms} ms",
        }
    return {
        "id": route,
        "label": label,
        "state": "error",
        "stateText": f"E{resp.status}",
        "detail": f"health {resp.status}",
    }


def now_ts() -> int:
    return int(time.time())


def parse_updated_ts(value: Any) -> float:
    if isinstance(value, (int, float)): return float(value)
    try: return float(str(value or "").strip())
    except ValueError: pass
    try: return time.mktime(time.strptime(str(value).replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z"))
    except ValueError: return 0.0


def display_updated_at(value: Any) -> str:
    ts = parse_updated_ts(value)
    if ts <= 0: return str(value or "")
    local = time.localtime(ts)
    fmt = "%H:%M" if time.strftime("%Y-%m-%d", local) == time.strftime("%Y-%m-%d", time.localtime()) else "%m-%d %H:%M"
    return time.strftime(fmt, local)


def compact_path_label(value: Any) -> str:
    text = str(value or "").replace("\\", "/").rstrip("/")
    return text.split("/")[-1] if text and text != "~" else text


def display_session_title(value: Any) -> str:
    return " ".join(str(value or "").replace("\r", "\n").split()) or "Untitled session"


def normalize_history_filters(values: dict[str, Any] | None = None) -> dict[str, str]:
    raw = values or {}
    query = " ".join(str(raw.get("q") or "").replace("\x00", "").split())[:HISTORY_QUERY_MAX_CHARS]
    period = str(raw.get("period") or "all").strip().lower()
    archive = str(raw.get("archive") or "active").strip().lower()
    return {
        "q": query,
        "period": period if period in HISTORY_PERIODS else "all",
        "archive": archive if archive in HISTORY_ARCHIVE_FILTERS else "active",
    }


def history_filters_from_query(query: dict[str, list[str]]) -> dict[str, str]:
    return normalize_history_filters({
        "q": query.get("q", [""])[0],
        "period": query.get("period", ["all"])[0],
        "archive": query.get("archive", ["active"])[0],
    })


def owner_history_query(limit: int, offset: int, filters: dict[str, Any] | None = None) -> str:
    applied = normalize_history_filters(filters)
    params: list[tuple[str, str]] = [
        ("view", "split"),
        ("limit", str(limit)),
        ("offset", str(offset)),
    ]
    if applied["q"]:
        params.append(("q", applied["q"]))
    if applied["period"] != "all":
        params.append(("period", applied["period"]))
    if applied["archive"] != "active":
        params.append(("archive", applied["archive"]))
    return "/api/agent-sessions?" + urlencode(params)


def control_target_from_json(raw: bytes | None) -> str:
    if not raw:
        return ""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in ("session", "agent_session_id", "agentSessionId", "client_launch_id", "clientLaunchId", "package_id", "packageId"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value[:160]
    return ""


def clean_session_title(value: Any) -> str:
    return display_session_title(value)[:48]

def clean_re(value: str | None, pattern: str) -> str | None:
    value = (value or "").strip(); return value if re.fullmatch(pattern, value) else None


def clean_package_id(value: str | None) -> str | None: return clean_re(value, r"[0-9]+-[a-f0-9]{8}")
def clean_session_id(value: str | None) -> str | None: return clean_re(value, r"[A-Za-z0-9_.:-]{1,80}")
def clean_agent_session_id(value: str | None) -> str | None: return clean_re(value, r"[A-Za-z0-9_.:-]{1,120}")
def clean_client_launch_id(value: str | None) -> str | None: return clean_re(value, r"[A-Za-z0-9_.:-]{8,128}")
def clean_agent_launch_command(value: str | None) -> str | None:
    command = Path(str(value or "").strip()).name.lower()
    return command if command in NEW_SESSION_COMMANDS else None


def clean_context_window_k(value: Any) -> int:
    raw = str(value if value is not None else "").strip()
    if not raw or raw == "0":
        return 0
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,4}", raw):
        raise ValueError(
            f"context window must be a whole number from {CONTEXT_WINDOW_MIN_K} to {CONTEXT_WINDOW_MAX_K} K"
        )
    context_window_k = int(raw)
    if not CONTEXT_WINDOW_MIN_K <= context_window_k <= CONTEXT_WINDOW_MAX_K:
        raise ValueError(
            f"context window must be a whole number from {CONTEXT_WINDOW_MIN_K} to {CONTEXT_WINDOW_MAX_K} K"
        )
    return context_window_k


def equivalent_owner_path(value: str, other: str) -> bool:
    left = str(value or "").strip().rstrip("/")
    right = str(other or "").strip().rstrip("/")
    if not left or not right:
        return False
    return left == right or (left.startswith("~/") and right.endswith(left[1:])) or (right.startswith("~/") and left.endswith(right[1:]))


def agent_cwd_choices(sessions: list[dict[str, Any]], workspace_root: str | None, limit: int = 8) -> list[dict[str, Any]]:
    choices: list[dict[str, Any]] = []
    root = str(workspace_root or "").strip().rstrip("/")
    for item in sorted(sessions, key=lambda entry: float(entry.get("updatedTs") or 0), reverse=True):
        cwd = str(item.get("cwd") or "").strip().rstrip("/")
        if not cwd or cwd == "~" or any(equivalent_owner_path(cwd, choice["value"]) for choice in choices):
            continue
        choices.append({
            "value": cwd,
            "label": compact_path_label(cwd) or cwd,
            "path": cwd,
            "kind": "workspace" if root and equivalent_owner_path(cwd, root) else "recent",
        })
        if len(choices) >= max(1, limit):
            break
    if root and not any(equivalent_owner_path(root, choice["value"]) for choice in choices):
        choices.append({"value": root, "label": compact_path_label(root) or root, "path": root, "kind": "workspace"})
    return choices


def owner_directory_selection_token(owner_token: str, path: str) -> str:
    return hmac.new(owner_token.encode("utf-8"), f"cwd:{path}".encode("utf-8"), hashlib.sha256).hexdigest()


def select_recent_agent_cwd(sessions: list[dict[str, Any]], workspace_root: str | None) -> str:
    return next((choice["value"] for choice in agent_cwd_choices(sessions, workspace_root) if choice["kind"] == "recent"), "")


def blocked_asset_ip(ip: Any) -> bool:
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified


def blocked_asset_host(hostname: str | None) -> bool:
    host = (hostname or "").strip().lower()
    if not host or host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        return blocked_asset_ip(ip)
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return True
    for info in infos:
        try:
            resolved_ip = ipaddress.ip_address(info[4][0])
        except (IndexError, ValueError):
            return True
        if blocked_asset_ip(resolved_ip):
            return True
    return False


def normalize_bridge_asset_payload(value: Any) -> dict[str, str] | None:
    if isinstance(value, str):
        raw = value.strip()
        if raw.startswith("data:"): return {"data_url": raw, "base64_data": "", "asset_url": "", "mime_type": "application/octet-stream", "file_name": "faryo-attachment"}
        if raw.startswith("https://"): return {"data_url": "", "base64_data": "", "asset_url": raw, "mime_type": "application/octet-stream", "file_name": Path(urlparse(raw).path).name or "faryo-attachment"}
        if len(raw) > 100 and re.fullmatch(r"[A-Za-z0-9+/=_-]+", raw): return {"data_url": "", "base64_data": raw, "asset_url": "", "mime_type": "image/png", "file_name": "faryo-image.png"}
        return None
    if not isinstance(value, dict): return None
    data_url = str(value.get("data_url") or value.get("dataUrl") or "").strip(); base64_data = str(value.get("base64_data") or value.get("base64Data") or value.get("b64_json") or "").strip()
    raw_data = value.get("data") or value.get("content")
    if isinstance(raw_data, str) and not data_url and not base64_data:
        if raw_data.strip().startswith("data:"): data_url = raw_data.strip()
        else: base64_data = raw_data.strip()
    asset_url = str(value.get("asset_url") or value.get("assetUrl") or value.get("image_url") or value.get("imageUrl") or value.get("url") or value.get("download_url") or value.get("downloadUrl") or "").strip()
    if not data_url and not base64_data and not asset_url: return None
    mime_type = str(value.get("mime_type") or value.get("mimeType") or value.get("type") or "application/octet-stream").split(";", 1)[0].strip().lower()
    file_name = Path(str(value.get("file_name") or value.get("fileName") or value.get("name") or "faryo-attachment")).name
    return {"data_url": data_url, "base64_data": base64_data, "asset_url": asset_url, "mime_type": mime_type, "file_name": file_name}


def bridge_mime_type(mime_type: str, file_name: str) -> str:
    mime_type = (mime_type or "application/octet-stream").strip().lower()
    suffix = Path(file_name or "").suffix.lower()
    if mime_type in BRIDGE_MIME_EXT:
        return mime_type
    if suffix in BRIDGE_SUFFIX_MIME:
        return BRIDGE_SUFFIX_MIME[suffix]
    return mime_type


def bridge_asset_bytes_from_payload(asset: dict[str, str]) -> tuple[str, bytes]:
    mime_type = bridge_mime_type(asset.get("mime_type") or "", asset.get("file_name") or "")
    if asset.get("data_url"):
        header, sep, payload = asset["data_url"].partition(",")
        if not sep or ";base64" not in header: raise ValueError("invalid attachment data_url")
        mime_type = (header.removeprefix("data:").split(";", 1)[0] or mime_type).strip().lower(); data = base64.b64decode(payload, validate=True)
        mime_type = bridge_mime_type(mime_type, asset.get("file_name") or "")
    elif asset.get("base64_data"):
        data = base64.b64decode(asset.get("base64_data") or "", validate=True)
    else:
        parsed = urlparse(asset.get("asset_url") or "")
        if parsed.scheme != "https": raise ValueError("attachment url must be https")
        if blocked_asset_host(parsed.hostname): raise ValueError("attachment url host is not allowed")
        with urllib.request.urlopen(urllib.request.Request(parsed.geturl(), headers={"User-Agent": "Faryo-Bridge/0.1"}), timeout=8) as resp:
            mime_type = (resp.headers.get_content_type() or mime_type).strip().lower(); data = resp.read(BRIDGE_ASSET_MAX_BYTES + 1)
        mime_type = bridge_mime_type(mime_type, asset.get("file_name") or Path(parsed.path).name)
    if mime_type not in BRIDGE_MIME_EXT: raise ValueError(f"unsupported attachment type: {mime_type or 'unknown'}")
    if len(data) > BRIDGE_ASSET_MAX_BYTES: raise ValueError("attachment is too large")
    return mime_type, data


def bridge_prompt_text(package: dict[str, Any]) -> str:
    parts = ["# Faryo Handoff Package", f"Title: {package.get('title') or 'Untitled handoff'}", f"Source: {package.get('source') or 'Faryo'}", "", "## Intent", str(package.get("intent") or ""), "", "## Context", str(package.get("context") or ""), "", "## Request", str(package.get("prompt") or "")]
    assets = package.get("assets") if isinstance(package.get("assets"), list) else []
    if assets: parts.extend(["", "## Attachments"] + [f"- {asset.get('file_name')}: {asset.get('path')}" for asset in assets if isinstance(asset, dict)])
    return "\n".join(parts).strip() + "\n"


GATEWAY_CONFIG_RUNTIME = gateway_config.GatewayConfigRuntime(
    backends=BACKENDS,
    load_backends=load_backends,
    route_max_defaults=SESSION_MAX_RUNNING_DEFAULTS,
    route_max_limit=SESSION_MAX_RUNNING_LIMIT,
    clean_package_id=clean_package_id,
    normalize_bridge_asset=normalize_bridge_asset_payload,
    bridge_asset_bytes=bridge_asset_bytes_from_payload,
    bridge_mime_extensions=BRIDGE_MIME_EXT,
    now_ts=now_ts,
)


class GatewayConfig(gateway_config.GatewayConfig):
    def __init__(self, auth_config: Path, owner_env: Path, portal_dir: Path, secret_file: Path) -> None:
        super().__init__(auth_config, owner_env, portal_dir, secret_file, GATEWAY_CONFIG_RUNTIME)


class WorkbenchRuntime:
    BACKENDS = BACKENDS
    SESSION_STATES = SESSION_STATES
    SESSION_STATE_PRIORITY = SESSION_STATE_PRIORITY
    HISTORY_PAGE_SIZE = HISTORY_PAGE_SIZE
    HISTORY_MAX_FETCH = HISTORY_MAX_FETCH
    NEW_SESSION_COMMANDS = NEW_SESSION_COMMANDS
    display_session_title = staticmethod(display_session_title)
    compact_path_label = staticmethod(compact_path_label)
    display_updated_at = staticmethod(display_updated_at)
    parse_updated_ts = staticmethod(parse_updated_ts)
    owner_history_query = staticmethod(owner_history_query)
    normalize_history_filters = staticmethod(normalize_history_filters)
    backend_status = staticmethod(backend_status)
    agent_cwd_choices = staticmethod(agent_cwd_choices)
    now_ts = staticmethod(now_ts)


def portal_html(username: str, routes: list[str]) -> str:
    safe_user = html_escape(username)
    safe_routes = [route for route in routes if route in BACKENDS]
    chips = []
    for route in safe_routes:
        _host, _port, label = BACKENDS[route]
        chips.append(f'<div class="route-chip" id="route-{route}"><span class="dot"></span><strong>{html_escape(label)}</strong><span class="route-state">…</span></div>')
    chips_html = "\n".join(chips) or '<div class="empty-state">No endpoints available</div>'
    labels_json = json.dumps({route: BACKENDS[route][2] for route in safe_routes}, ensure_ascii=False, separators=(",", ":"))
    labels_json = labels_json.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    asset_version = GATEWAY_ASSET_REVISION
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><title>Faryo</title><meta name="theme-color" content="#F6F7F9" media="(prefers-color-scheme: light)"><meta name="theme-color" content="#0F1115" media="(prefers-color-scheme: dark)"><link rel="manifest" href="/manifest.json"><link rel="icon" href="/icons/favicon.png?v={asset_version}" type="image/png"><link rel="apple-touch-icon" href="/icons/pwa-light-192.png?v={asset_version}"><script src="/appearance.js?v={asset_version}"></script><link rel="stylesheet" href="/appearance.css?v={asset_version}"><link rel="stylesheet" href="/workbench.css?v={asset_version}">
</head><body><div class="shell">
<header><a class="brand" href="/" aria-label="Faryo home"><img class="brand-logo" src="/icons/faryo-mark.png?v={asset_version}" alt=""><div><h1>Faryo</h1><div class="subtitle">{safe_user} · Carry work forward</div></div></a><div class="settings" id="settings"><button class="settings-trigger" type="button" aria-label="Settings"><span class="settings-icon">⚙</span></button><div class="settings-menu" aria-label="Settings panel"><button id="installApp" class="settings-row install-row" type="button" hidden><span><strong>Install app</strong><small>Add Faryo to home screen</small></span><em>↗</em></button><div class="menu-title">Appearance</div><button id="themeBtn" class="settings-row appearance-btn" type="button"><span><strong>Theme</strong><small>System</small></span><em>↻</em></button><button id="fontBtn" class="settings-row appearance-btn" type="button"><span><strong>Font</strong><small>Default</small></span><em>↻</em></button><button id="sizeBtn" class="settings-row appearance-btn" type="button"><span><strong>Size</strong><small>Normal</small></span><em>↻</em></button><div class="menu-title">Attention</div><button id="attentionCenter" class="settings-row" type="button"><span><strong>Attention</strong><small id="attentionSummary">Nothing needs attention</small></span><em id="attentionCount">0</em></button><button id="notificationControl" class="settings-row" type="button"><span><strong>Notifications</strong><small id="notificationState">Off · page-open only</small></span><em>◉</em></button><div class="menu-title">Security</div><button id="securityActivity" class="settings-row" type="button"><span><strong>Security activity</strong><small>Body-free control audit</small></span><em>›</em></button><button id="revokeSessions" class="settings-row danger-row" type="button"><span><strong>Revoke signed-in devices</strong><small>Keep Codex and tmux running</small></span><em>!</em></button><div class="menu-title">Account</div><a class="settings-row" href="/password"><span><strong>Change password</strong></span><em>›</em></a><a class="settings-row" href="/logout"><span><strong>Sign out this device</strong></span><em>›</em></a></div></div></header>
<nav class="routes" aria-label="Endpoint status">{chips_html}</nav><div class="handoff-strip"><section class="handoff" id="handoffBox" aria-label="Files to session"><div class="handoff-head"><div><div class="eyebrow">Transfer</div><h2>Files to session <span class="count" id="packageCount">· Empty</span></h2></div><button class="mini-btn primary-btn" id="newPackage" type="button">Choose files</button></div><input id="packageInput" type="file" accept="image/*,.pdf,.doc,.docx,.ppt,.pptx,.xls,.xlsx,.odt,.odp,.ods,.md,.txt,.csv,.json,.rtf" multiple hidden><input id="packageAssetInput" type="file" accept="image/*,.pdf,.doc,.docx,.ppt,.pptx,.xls,.xlsx,.odt,.odp,.ods,.md,.txt,.csv,.json,.rtf" multiple hidden><div class="package-list" id="packageList"><div class="empty-state">Choose files, then send them to a session.</div></div></section><section class="new-session-panel" aria-labelledby="newSessionTitle"><div class="new-session-head"><div class="eyebrow">Launch</div><h2 id="newSessionTitle">New session</h2></div><div class="new-session-slot" id="newSessionSlot"><div class="empty-state">Loading launchers…</div></div></section></div>
<main><section class="session-section active-section" aria-labelledby="activeSessionsTitle"><div class="section-head"><h2 id="activeSessionsTitle">Active Sessions</h2><span class="count" id="activeSessionCount">Loading</span></div><section class="sessions" id="activeSessionList"><div class="empty-state">Loading active sessions...</div></section></section><section class="session-section history-section" aria-labelledby="sessionHistoryTitle"><div class="section-head"><h2 id="sessionHistoryTitle">Session History</h2><span class="count" id="historyCount">Loading</span></div><div class="history-tools"><form class="history-search" id="historySearchForm" role="search"><span aria-hidden="true">⌕</span><label class="visually-hidden" for="historySearchInput">Search session title or folder</label><input id="historySearchInput" type="search" inputmode="search" autocomplete="off" spellcheck="false" maxlength="96" placeholder="Search title or folder"><button class="history-search-clear" id="historySearchClear" type="button" aria-label="Clear history search" hidden>×</button></form><div class="history-filter-row" aria-label="Session history filters"><button class="history-filter-chip" type="button" data-history-period="all" aria-pressed="true">All time</button><button class="history-filter-chip" type="button" data-history-period="today" aria-pressed="false">Today</button><button class="history-filter-chip" type="button" data-history-period="7d" aria-pressed="false">7 days</button><button class="history-filter-chip" type="button" data-history-period="30d" aria-pressed="false">30 days</button><span class="history-filter-separator" aria-hidden="true"></span><button class="history-filter-chip" type="button" data-history-archive="active" aria-pressed="true">Current</button><button class="history-filter-chip" type="button" data-history-archive="archived" aria-pressed="false">Archived</button><button class="history-filter-chip" type="button" data-history-archive="all" aria-pressed="false">Any status</button></div></div><section class="sessions history-list" id="sessionList"><div class="empty-state">Loading history...</div></section><nav class="history-pager" aria-label="Session history pages"><button class="mini-btn" id="historyPrev" type="button">Prev</button><form class="history-jump" id="historyJump"><label for="historyPageInput">Page</label><input class="history-page-input" id="historyPageInput" type="number" min="1" max="1" step="1" inputmode="numeric" value="1" aria-label="History page"><span>of <span id="historyPageTotal">1</span></span><button class="mini-btn" type="submit">Go</button></form><button class="mini-btn" id="historyNext" type="button">Next</button></nav></section></main>
</div><div class="modal" id="modal"><div class="sheet"><div class="sheet-heading"><div class="sheet-heading-copy"><h3 id="modalTitle"></h3><p id="modalBody"></p></div></div><div id="directoryToolbar" class="directory-toolbar" hidden><fieldset id="workstationPicker" class="workstation-picker" hidden><legend>Workstation</legend><div id="workstationControls" class="workstation-controls"></div><small id="workstationHelp"></small></fieldset><nav id="directoryBreadcrumb" class="directory-breadcrumb" aria-label="Current folder"></nav><div class="directory-filter-row"><label class="directory-search"><span class="visually-hidden">Filter folders</span><span aria-hidden="true">⌕</span><input id="directorySearch" type="search" inputmode="search" autocomplete="off" spellcheck="false" placeholder="Filter folders"></label><button id="directoryHiddenToggle" class="directory-hidden-toggle" type="button" aria-pressed="false"><span aria-hidden="true">.</span> Hidden</button></div><details id="launchSettings" class="launch-settings" open><summary class="launch-settings-summary"><span class="launch-settings-summary-copy"><strong>Session settings</strong><small><span id="sessionBackendSummary">App Server</span><span aria-hidden="true"> · </span><span id="contextWindowSummary">Default context</span></small></span><span class="launch-settings-chevron" aria-hidden="true">⌄</span></summary><div class="launch-settings-body"><fieldset id="sessionBackendPicker" class="session-backend-picker"><legend>Codex backend</legend><div class="session-backend-controls"><button type="button" data-session-backend="APP_SERVER" aria-pressed="true"><strong>Codex App Server</strong><small>Structured streaming web session</small></button><button type="button" data-session-backend="CODEX_TUI" aria-pressed="false"><strong>Codex TUI (tmux)</strong><small>Terminal compatibility session</small></button></div><small id="sessionBackendHelp">App Server is recommended for the best web experience.</small></fieldset><fieldset id="contextWindowPicker" class="context-window-picker"><legend>Context window</legend><div class="context-window-controls"><button type="button" data-context-window-k="0" aria-pressed="true">Default</button><button type="button" data-context-window-k="372" aria-pressed="false">372K</button><button type="button" data-context-window-k="1000" aria-pressed="false">1M</button><label class="context-window-custom"><span class="visually-hidden">Custom context window in K tokens</span><input id="contextWindowCustom" type="number" min="32" max="1050" step="1" inputmode="numeric" placeholder="Custom"><span>K</span></label></div><small id="contextWindowHelp">Inherit this workstation's Codex settings.</small><small id="contextWindowError" class="context-window-error" role="alert" hidden></small></fieldset></div></details></div><div class="choice-list" id="modalChoices"></div><div class="modal-actions" id="modalActions"></div></div></div><script id="faryoRouteLabels" type="application/json" nonce="{CSP_NONCE_PLACEHOLDER}">{labels_json}</script><script src="/workbench-preact.js?v={asset_version}"></script><script src="/workbench.js?v={asset_version}"></script></body></html>'''


AUTH_CSS = """*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:var(--bg);color:var(--text);font-family:var(--app-font)}main{width:min(100%,420px)}.auth-brand{display:flex;align-items:center;gap:12px;margin-bottom:8px}.auth-logo{width:48px;height:48px;border-radius:13px;flex:0 0 auto}h1{margin:0 0 8px;font-size:26px;letter-spacing:0}p{margin:0 0 22px;color:var(--muted);line-height:1.5}label{display:block;margin:12px 0 7px;color:var(--muted);font-size:14px}input{width:100%;height:52px;border:1px solid var(--line);border-radius:8px;padding:0 13px;background:var(--panel);color:var(--text);font:inherit;outline:none}input:focus{border-color:var(--accent)}.password-row{position:relative}.password-row input{padding-right:58px}.toggle{position:absolute;right:6px;top:6px;display:grid;place-items:center;width:40px;height:40px;min-height:40px;border:0;border-radius:8px;background:var(--toggle-bg);color:var(--text)}.toggle svg{width:21px;height:21px;stroke:currentColor;stroke-width:2;fill:none;stroke-linecap:round;stroke-linejoin:round}.toggle .eye-off,.toggle.is-visible .eye{display:none}.toggle.is-visible .eye-off{display:block}.submit{width:100%;height:52px;margin-top:18px;border:0;border-radius:8px;background:var(--accent);color:var(--on-accent);font-weight:700;font-size:16px}.secondary{display:block;margin-top:14px;color:var(--muted);text-align:center;text-decoration:none}.error{min-height:20px;margin-top:12px;color:var(--danger);font-size:14px}.icp{margin:26px 0 0;text-align:center;font-size:13px}.icp a{color:var(--muted);text-decoration:none}"""
AUTH_SCRIPT = """document.querySelectorAll('.password-row').forEach((row)=>{const input=row.querySelector('input');const toggle=row.querySelector('button');toggle.addEventListener('click',()=>{const visible=input.type==='text';input.type=visible?'password':'text';toggle.classList.toggle('is-visible',!visible);toggle.setAttribute('aria-label',visible?'Show password':'Hide password');toggle.title=visible?'Show password':'Hide password';});});"""
EYE_BUTTON = """<button class="toggle" type="button" aria-label="Show password" title="Show password"><svg class="eye" viewBox="0 0 24 24"><path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6-10-6-10-6z"/><circle cx="12" cy="12" r="3"/></svg><svg class="eye-off" viewBox="0 0 24 24"><path d="M3 3l18 18"/><path d="M10.7 5.2A10.8 10.8 0 0 1 12 5c6.5 0 10 7 10 7a17.7 17.7 0 0 1-3.2 4.1"/><path d="M6.6 6.6C3.6 8.6 2 12 2 12s3.5 7 10 7a10.5 10.5 0 0 0 4.2-.9"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/></svg></button>"""


def password_field(field_id: str, name: str, label: str, autocomplete: str, minlength: int | None = None) -> str:
    min_attr = f' minlength="{minlength}"' if minlength else ""
    return f"""<label for="{field_id}">{label}</label><div class="password-row"><input id="{field_id}" name="{name}" type="password" autocomplete="{autocomplete}" autocapitalize="none" spellcheck="false"{min_attr} required>{EYE_BUTTON}</div>"""


def icp_footer(record: str) -> str:
    if not record:
        return ""
    return f'<p class="icp"><a href="https://beian.miit.gov.cn/" target="_blank" rel="noopener noreferrer">{html_escape(record)}</a></p>'


def auth_page(title: str, heading: str, intro: str, action: str, autocomplete: str, body: str, error: str, csrf: str = "", icp: str = "") -> str:
    csrf_input = f'<input type="hidden" name="csrf" value="{html_escape(csrf)}">' if csrf else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><title>{title}</title><meta name="theme-color" content="#F6F7F9" media="(prefers-color-scheme: light)"><meta name="theme-color" content="#0F1115" media="(prefers-color-scheme: dark)"><link rel="icon" href="/icons/favicon.png?v={GATEWAY_ASSET_REVISION}" type="image/png"><link rel="apple-touch-icon" href="/icons/pwa-light-192.png?v={GATEWAY_ASSET_REVISION}"><script src="/appearance.js?v={GATEWAY_ASSET_REVISION}"></script><link rel="stylesheet" href="/appearance.css?v={GATEWAY_ASSET_REVISION}"><style nonce="{CSP_NONCE_PLACEHOLDER}">{AUTH_CSS}</style></head>
<body><main><div class="auth-brand"><img class="auth-logo" src="/icons/faryo-mark.png?v={GATEWAY_ASSET_REVISION}" alt=""><div><h1>{heading}</h1><p>{intro}</p></div></div><form method="post" action="{action}" autocomplete="{autocomplete}">{csrf_input}{body}<div class="error">{html_escape(error)}</div></form>{icp_footer(icp)}</main><script nonce="{CSP_NONCE_PLACEHOLDER}">{AUTH_SCRIPT}</script></body></html>"""


def login_html(next_target: str, error: str = "", icp: str = "") -> str:
    body = (
        f'<input type="hidden" name="next" value="{html_escape(next_target)}">'
        '<label for="username">Username</label><input id="username" name="username" autocomplete="username" autocapitalize="none" spellcheck="false" required>'
        + password_field("password", "password", "Password", "current-password")
        + '<button class="submit" type="submit">Sign in</button>'
    )
    return auth_page("Faryo Sign In", "Faryo", "Enter your gateway username and password.", "/login", "on", body, error, icp=icp)


def password_html(csrf: str = "", error: str = "", icp: str = "") -> str:
    body = (
        password_field("current_password", "current_password", "Current password", "current-password")
        + password_field("new_password", "new_password", "New password", "new-password", 16)
        + password_field("confirm_password", "confirm_password", "Confirm new password", "new-password", 16)
        + '<button class="submit" type="submit">Save password</button><a class="secondary" href="/">Back to Faryo</a>'
    )
    return auth_page("Faryo Change Password", "Change password", "Update the gateway password. Changes take effect immediately.", "/password", "off", body, error, csrf, icp)
