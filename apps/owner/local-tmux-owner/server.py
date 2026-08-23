#!/usr/bin/env python3
"""Local Tmux Owner HTTP bridge for one or more tmux-backed Codex sessions.

This server intentionally exposes only bounded status/capture, reliable text
delivery, interrupt, and versioned Codex interactions. It does not expose
arbitrary terminal navigation keys.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import io
import json
import mmap
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable, NamedTuple

OWNER_MODULE_DIR = Path(__file__).resolve().parent
if str(OWNER_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(OWNER_MODULE_DIR))
import attachment_storage
import path_policy
import tmux_runtime
import delivery_store
import delivery_service
import codex_history
import codex_app_server
import appserver_runtime
import appserver_history
import appserver_rollout
import session_catalog
import session_launch
import codex_tui_interactions
import interaction_service
import command_timeline
from faryo_cli import codex_runtime, session_backend

SHARED_DIR = Path(__file__).resolve().parents[2] / "shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))
from urllib.parse import unquote

try:
    from rich.console import Console as RichConsole
    from rich.text import Text as RichText
except ImportError:  # pragma: no cover - runtime fallback for minimal environments
    RichConsole = None
    RichText = None

APP_DIR = OWNER_MODULE_DIR
STATIC_DIR = APP_DIR / "static"
SHARED_STATIC_DIR = SHARED_DIR / "static"
RELEASE_FILE = APP_DIR.parent / "RELEASE"
AGENT_STATE_DB = Path(os.environ.get("FARYO_CODEX_STATE_DB", str(Path.home() / ".codex" / "state_5.sqlite"))).expanduser()
CODEX_SESSION_INDEX = Path(os.environ.get("FARYO_CODEX_SESSION_INDEX", str(Path.home() / ".codex" / "session_index.jsonl"))).expanduser()
DEFAULT_SESSION = "__faryo_no_default__"
DEFAULT_PORT = 8765
SHARED_STATIC_FILES = {
    "appearance.css": "text/css; charset=utf-8",
    "appearance.js": "text/javascript; charset=utf-8",
}
# Faryo must not change a terminal UI's geometry by default.  A positive
# --pane-width remains an explicit compatibility opt-in for terminal-only
# capture, but Codex always follows its real tmux clients.
DEFAULT_PANE_WIDTH = 0
FALLBACK_OWNER_LABEL = "TMUX"
MAX_SEND_CHARS = 120_000
PASTE_READY_TIMEOUT = 1.2
PASTE_READY_POLL_INTERVAL = 0.05
PASTE_READY_MIN_PROBE_CHARS = 8
PASTE_SETTLE_SECONDS = 0.12
SEND_ACCEPT_TIMEOUT = 2.2
SEND_ACCEPT_RETRY_DELAY = 0.18
SEND_KEY_MAX_ATTEMPTS = 3
SEND_DELIVERY_TTL_SECONDS = 48 * 60 * 60
SEND_DELIVERY_CLEANUP_INTERVAL_SECONDS = 60 * 60
CAPTURE_COMPACT_LINES = 320
CAPTURE_FULL_LINES = 800
CAPTURE_DEFAULT_LINES = CAPTURE_FULL_LINES
CAPTURE_MAX_LINES = CAPTURE_FULL_LINES
CODEX_LIVE_TAIL_LINES = 180
EVENT_STREAM_MAX_SECONDS = 75
EVENT_STREAM_MAX_CONNECTIONS = 16
EVENT_STREAM_HEARTBEAT_SECONDS = 10
RATE_LIMIT_CACHE_TTL = 120.0
CODEX_TRANSCRIPT_CACHE_TTL = 5.0
# Markdown source line count is a poor proxy for browser cost: one formula-heavy
# answer can contain hundreds of short lines while remaining only a few KB.  A
# soft line budget must therefore never make the conversation look as if all
# prior turns disappeared.  Keep a useful recent turn window, with a separate
# hard character ceiling for mobile payload safety.
CODEX_TRANSCRIPT_PAGE_TURNS = 12
CODEX_TRANSCRIPT_MIN_TURNS = CODEX_TRANSCRIPT_PAGE_TURNS
CODEX_TRANSCRIPT_CHAR_BUDGET = 512 * 1024
CODEX_ROLLOUT_CACHE_LINE_BUDGET = CAPTURE_MAX_LINES * 2
CODEX_ROLLOUT_CACHE_CHAR_BUDGET = 4 * 1024 * 1024
CODEX_ROLLOUT_CACHE_MIN_TURNS = CODEX_TRANSCRIPT_MIN_TURNS
CODEX_ROLLOUT_CACHE_MAX_PATHS = 16
CODEX_ROLLOUT_TAIL_SCAN_BYTES = 16 * 1024 * 1024
CODEX_ROLLOUT_MAX_CATCHUP_BYTES = 8 * 1024 * 1024
CODEX_HISTORY_PAGE_TURNS = CODEX_TRANSCRIPT_PAGE_TURNS
CODEX_HISTORY_MAX_PAGE_TURNS = 24
CODEX_HISTORY_PAGE_CHAR_BUDGET = 2 * 1024 * 1024
CODEX_HISTORY_PREVIEW_CHARS = 88
CODEX_HISTORY_INDEX_MAX_PATHS = CODEX_ROLLOUT_CACHE_MAX_PATHS
THREAD_COLUMNS = "id, title, rollout_path, tokens_used, model, reasoning_effort, cwd, updated_at, source, thread_source, archived"
INTERACTIVE_CODEX_THREAD_SOURCES = {"cli", "vscode", "appServer"}
INTERACTIVE_TOP_LEVEL_THREAD_SQL = (
    "source IN ('cli', 'vscode', 'appServer') "
    "AND (thread_source = 'user' OR thread_source IS NULL)"
)
AGENT_SESSION_LIST_LIMIT = 20
AGENT_SESSION_QUERY_LIMIT = 1000
AGENT_HISTORY_QUERY_MAX_CHARS = 96
AGENT_HISTORY_PERIODS = {"all", "today", "7d", "30d"}
AGENT_HISTORY_ARCHIVE_FILTERS = {"active", "archived", "all"}
EMPTY_MANAGED_SESSION_TTL_SECONDS = 60
MAX_MANAGED_AGENT_IDLE_SECONDS = 24 * 60 * 60
AGENT_START_READY_TIMEOUT = 15.0
AGENT_START_READY_STABLE_SECONDS = 0.75
AGENT_START_STATE_GRACE_SECONDS = 5.0
CODEX_UPDATE_START_READY_TIMEOUT = 220.0
AGENT_ARCHIVE_VERIFY_TIMEOUT = 3.0
CONTEXT_WINDOW_MIN_K = 32
CONTEXT_WINDOW_MAX_K = 1050
CONTEXT_WINDOW_COMPACT_PERCENT = 90
RUNTIME_LOCK = threading.RLock()
AGENT_START_MONITOR_LOCK = threading.Lock()
AGENT_START_MONITORS: dict[str, int] = {}
RELEASE_VERSION_CACHE: str | None = None
FARYO_OWNER_DATA = Path(os.environ.get("FARYO_OWNER_DATA", str(Path.home() / ".faryo" / "owner" / "data"))).expanduser()
CODEX_UPDATE_STATE_DIR = Path(
    os.environ.get(
        "FARYO_CODEX_UPDATE_STATE_DIR",
        str(FARYO_OWNER_DATA.parent / "state"),
    )
).expanduser()
CODEX_UPDATE_PREFLIGHT = APP_DIR / "codex_update_preflight.py"
CODEX_UPDATE_SESSION_STATES = {"pending", "current", "updated", "failed", "reconciled"}
FILE_INBOX_ROOT = Path(os.environ.get("FARYO_OWNER_INBOX_DIR", str(FARYO_OWNER_DATA / "inbox"))).expanduser()
CACHE_ROOT = Path(os.environ.get("FARYO_OWNER_CACHE_DIR", str(FARYO_OWNER_DATA / "cache"))).expanduser()
LOGS_ROOT = Path(os.environ.get("FARYO_OWNER_LOGS_DIR", str(FARYO_OWNER_DATA / "logs"))).expanduser()
SEND_DELIVERY_ROOT = Path(os.environ.get("FARYO_OWNER_DELIVERY_DIR", str(FARYO_OWNER_DATA / "send-deliveries"))).expanduser()
APP_SERVER_SOCKET = Path(
    os.environ.get(
        "FARYO_CODEX_APP_SERVER_SOCKET",
        str(FARYO_OWNER_DATA.parent / "runtime/codex-app-server.sock"),
    )
).expanduser()
APP_SERVER_REGISTRY = Path(
    os.environ.get(
        "FARYO_CODEX_APP_SERVER_REGISTRY",
        str(FARYO_OWNER_DATA.parent / "state/appserver-sessions.json"),
    )
).expanduser()
COMMAND_TIMELINE_PATH = Path(
    os.environ.get(
        "FARYO_COMMAND_TIMELINE",
        str(FARYO_OWNER_DATA.parent / "state/command-timeline.json"),
    )
).expanduser()
MAX_ATTACHMENT_UPLOAD_BYTES = attachment_storage.DEFAULT_MAX_UPLOAD_BYTES
IMAGE_SUFFIXES = attachment_storage.IMAGE_SUFFIXES
IMAGE_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".heic": "image/heic",
    ".heif": "image/heif",
}
LOCAL_FILE_CONTENT_TYPES = {
    ".md": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".rtf": "application/rtf",
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".bash": "text/plain; charset=utf-8",
    ".c": "text/plain; charset=utf-8",
    ".cc": "text/plain; charset=utf-8",
    ".cfg": "text/plain; charset=utf-8",
    ".cpp": "text/plain; charset=utf-8",
    ".css": "text/plain; charset=utf-8",
    ".go": "text/plain; charset=utf-8",
    ".h": "text/plain; charset=utf-8",
    ".hpp": "text/plain; charset=utf-8",
    ".html": "text/plain; charset=utf-8",
    ".ini": "text/plain; charset=utf-8",
    ".java": "text/plain; charset=utf-8",
    ".js": "text/plain; charset=utf-8",
    ".jsx": "text/plain; charset=utf-8",
    ".lean": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".py": "text/plain; charset=utf-8",
    ".rs": "text/plain; charset=utf-8",
    ".sh": "text/plain; charset=utf-8",
    ".sql": "text/plain; charset=utf-8",
    ".tex": "text/plain; charset=utf-8",
    ".toml": "text/plain; charset=utf-8",
    ".ts": "text/plain; charset=utf-8",
    ".tsx": "text/plain; charset=utf-8",
    ".xml": "text/plain; charset=utf-8",
    ".yaml": "text/plain; charset=utf-8",
    ".yml": "text/plain; charset=utf-8",
    ".zsh": "text/plain; charset=utf-8",
}
LOCAL_FILE_SUFFIXES = set(LOCAL_FILE_CONTENT_TYPES)
EXTERNAL_VIEWER_SUFFIXES = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".odp", ".ods", ".rtf"}
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
ANSI_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f]")
ANSI_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")
HTML_CODE_RE = re.compile(r"<code[^>]*>(.*)</code>", re.S)
RICH_PRE_RE = re.compile(r"^\s*<pre\b[^>]*>(.*)</pre>\s*$", re.S)
STYLE_ATTR_RE = re.compile(r'\sstyle="([^"]*)"')
SEPARATOR_RE = re.compile(r"^[\s─━═\-—_]{20,}$")
SEPARATOR_OUTPUT_RE = re.compile(r"^\s*(?:[└│]\s*)?(?:\d+:)?[\s─━═\-—_]{4,}$")
LONG_SEPARATOR_RE = re.compile(r"[─━═]{20,}")
AGENT_BOUNDARY_RE = re.compile(r"^[\s─━═\-—_]*(Worked for .*?)[\s─━═\-—_]*$", re.I)
AGENT_PLACEHOLDER_RE = re.compile(r"^\s*[›>]\s*Write tests for @filename\s*$", re.I)
USER_PROMPT_RE = re.compile(r"^\s*›\s+")
# Codex uses `›` while idle and `»` while a turn or background startup is
# active.  Both glyphs identify the live composer; historical user messages
# continue to use `›` and are matched separately by USER_PROMPT_RE.
AGENT_INPUT_PROMPT_RE = re.compile(r"^\s*[›>»](?:\s|$)")
AGENT_META_RE = re.compile(r"^\s*((?:gpt|o\d)[\w.\- ]*)\s*·\s+(.+?)\s*$", re.I)
NO_AGENT_META_RE = re.compile(r"a^")
ROLLOUT_THREAD_ID_RE = re.compile(r"rollout-.*-(?P<id>[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.jsonl", re.I)
REASONING_EFFORT_SUFFIX_RE = re.compile(r"\b(?P<effort>low|medium|high|xhigh|max|ultra)\s*$", re.I)
FAST_MODEL_SUFFIX_RE = re.compile(r"\s+fast\s*$", re.I)
FAST_STATUS_RE = re.compile(r"\bFast(?:\s+mode)?(?:\s+(?:is|set\s+to))?\s+(?P<state>on|off|true|false|enabled|disabled)\b", re.I)
SHELL_PREP_RE = re.compile(r"^(?:pwd|clear|ls(?:\s+[-\w./~]+)*|cd(?:\s+[-\w./~]+)?)$")
FAST_CONFIG_KEYS = {
    "auto-fast",
    "codex-auto-fast",
}
SESSION_GIT_PREFIXES = ("🌿", "✏️", "✏", "⚠️")
SESSION_GIT_ROOT_OPTION = "@faryo_git_root"
SESSION_CONTEXT_WINDOW_OPTION = "@faryo_context_window_k"
SESSION_TITLE_NOISE_RE = re.compile(r"^(?:📁 |Ctx |(?:gpt|o\d)[\w.\- ]+\s+(?:low|medium|high|xhigh)$)", re.I)
class AgentProfile(NamedTuple):
    key: str
    command: str
    source: str
    input_prompt_re: Any = AGENT_INPUT_PROMPT_RE
    user_prompt_re: Any = USER_PROMPT_RE
    meta_re: Any = AGENT_META_RE
    boundary_re: Any = AGENT_BOUNDARY_RE
    placeholder_re: Any = AGENT_PLACEHOLDER_RE


CODEX_PROFILE = AgentProfile("codex", "codex", "codex-cli")
RUNTIME_PROFILE = AgentProfile("runtime", "", "runtime", NO_AGENT_META_RE, NO_AGENT_META_RE, NO_AGENT_META_RE, NO_AGENT_META_RE, NO_AGENT_META_RE)
AGENT_PROFILES = (CODEX_PROFILE,)
AGENT_LAUNCH_COMMANDS = {profile.command for profile in AGENT_PROFILES}
AGENT_SOURCE_BY_COMMAND = {profile.command: profile.source for profile in AGENT_PROFILES}
BLACK_VALUES = {"#000", "#000000", "black", "rgb(0,0,0)", "rgb(0, 0, 0)"}
# Explicit terminal white is the light-theme twin of BLACK_VALUES: drop it so
# terminal text inherits the theme foreground instead of vanishing on light
# backgrounds.
WHITE_VALUES = {"#fff", "#ffffff", "white", "rgb(255,255,255)", "rgb(255, 255, 255)"}
USER_INPUT_COLOR = "var(--user-input-color, #CAD2FF)"
LOW_CONTRAST_TERMINAL_VALUES = {"#000080", "#0000aa", "#0000cd", "#0000ff", "blue"}
_rate_limit_cache: dict[str, Any] | None = None
_rate_limit_cache_at = 0.0
_rate_limit_lock = threading.Lock()
_rate_limit_refreshing = False
_codex_thread_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_codex_thread_cache_lock = threading.Lock()
_codex_rollout_cache: dict[str, dict[str, Any]] = {}
_codex_rollout_cache_lock = threading.Lock()
_codex_rollout_path_locks: dict[str, threading.Lock] = {}
_codex_history_cache: dict[str, dict[str, Any]] = {}
_codex_history_cache_lock = threading.Lock()
_codex_history_path_locks: dict[str, threading.Lock] = {}
_command_catalog_refresh_lock = threading.Lock()
_command_catalog_refreshing = False
_delivery_store = delivery_store.DeliveryStore(
    SEND_DELIVERY_ROOT,
    ttl_seconds=SEND_DELIVERY_TTL_SECONDS,
    cleanup_interval_seconds=SEND_DELIVERY_CLEANUP_INTERVAL_SECONDS,
)
_command_timeline_store = command_timeline.CommandTimelineStore(COMMAND_TIMELINE_PATH)


def short_path(path: str | None) -> str | None:
    if not path:
        return path
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + "/"):
        return "~/" + path[len(home) + 1:]
    return path


def git_status(cwd: str | None) -> dict[str, Any] | None:
    if not cwd:
        return None
    top = run_cmd(["git", "-C", cwd, "rev-parse", "--show-toplevel"], timeout=2)
    if top.returncode != 0 or not top.stdout.strip():
        return None
    try:
        git_root = Path(top.stdout.strip()).expanduser().resolve()
    except OSError:
        return None
    if git_root == Path.home().resolve():
        return None
    res = run_cmd(["git", "-C", cwd, "status", "--short", "--branch"], timeout=2)
    if res.returncode != 0:
        return None
    lines = [line for line in res.stdout.splitlines() if line.strip()]
    head, body = (lines[0] if lines else ""), lines[1:]
    branch = head[3:].split("...", 1)[0].strip() if head.startswith("## ") else "git"
    detached = branch.startswith("HEAD ")
    changed = sum(line[:2] != "??" for line in body)
    untracked = sum(line[:2] == "??" for line in body)
    insertions = deletions = 0
    diff_res = run_cmd(["git", "-C", cwd, "diff", "--numstat", "HEAD", "--"], timeout=2)
    if diff_res.returncode != 0:
        diff_res = run_cmd(["git", "-C", cwd, "diff", "--numstat", "--"], timeout=2)
    if diff_res.returncode == 0:
        for row in diff_res.stdout.splitlines():
            parts = row.split("\t", 2)
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                insertions += int(parts[0])
                deletions += int(parts[1])
    marks = [label for label in (f"+{insertions}" if insertions else "", f"-{deletions}" if deletions else "", f"?{untracked}" if untracked else "", *(f"{arrow}{n}" for word, arrow in (("ahead", "↑"), ("behind", "↓")) for n in re.findall(rf"{word} (\d+)", head))) if label]
    if not detached and branch != "main":
        main_res = run_cmd(["git", "-C", cwd, "rev-list", "--left-right", "--count", "origin/main...HEAD"], timeout=2)
        if main_res.returncode == 0:
            behind_main, ahead_main = [int(value) for value in main_res.stdout.split()[:2]]
            marks.extend(label for label in (f"m+{ahead_main}" if ahead_main else "", f"m-{behind_main}" if behind_main else "") if label)
    clean = not changed and not untracked
    mark_text = (" " + " ".join(marks)) if marks else ""
    if detached:
        return {"state": "error", "label": f"⚠️ DETACHED{mark_text}", "title": "\n".join(lines[:12])}
    return {"state": "clean" if clean else "dirty", "label": f"{'🌿' if clean else '✏️'}{mark_text} {branch}", "title": "\n".join(lines[:12])}


def session_title_topic(value: Any, fallback: str = "Untitled session") -> str:
    labels = {owner_label(), "TXY", "HP", "PC", FALLBACK_OWNER_LABEL}
    lines = [line.strip() for line in str(value or "").replace("\r", "\n").split("\n") if line.strip()]
    topic = next((line for line in lines if line not in labels and not line.startswith(SESSION_GIT_PREFIXES) and not SESSION_TITLE_NOISE_RE.match(line)), "")
    return topic or fallback


def session_index_title(value: Any) -> str:
    return " ".join(str(value or "").replace("\r", "\n").split())


def session_git_label(cwd: str | None, cache: dict[str, str], active: bool = True) -> str:
    if not cwd or not active:
        return ""
    if cwd not in cache:
        cache[cwd] = str((git_status(cwd) or {}).get("label") or "")
    return cache[cwd]


def session_git_cwd(config: Config, session: str | None, cwd: str | None) -> str | None:
    return (tmux_session_option(config, session, SESSION_GIT_ROOT_OPTION) or cwd) if session else cwd


def git_root_for_cwd(cwd: Path) -> str:
    result = run_cmd(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"], timeout=2)
    if result.returncode != 0 or not result.stdout.strip():
        return ""
    try:
        root = Path(result.stdout.strip()).expanduser().resolve()
    except OSError:
        return ""
    return "" if root == Path.home().resolve() else str(root)


def env_value(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value is not None:
            return value
    return default


def default_owner_label() -> str:
    hostname = socket.gethostname().strip().lower()
    if not hostname:
        return FALLBACK_OWNER_LABEL
    if "hp" in hostname:
        return "HP"
    if hostname == "sl" or hostname.startswith("sl-") or hostname.endswith("-sl") or "-sl-" in hostname:
        return "PC"
    if "cloud" in hostname or "txy" in hostname:
        return "TXY"
    return (hostname.split(".", 1)[0][:16] or FALLBACK_OWNER_LABEL).upper()


def owner_label() -> str:
    label = env_value("FARYO_OWNER_LABEL", default="").strip()
    return label or default_owner_label()


def clean_owner_label(label: str | None) -> str | None:
    if not label:
        return None
    decoded = unquote(label)
    cleaned = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", decoded).strip()
    return cleaned[:32] or None


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec="seconds")


class OwnerError(Exception):
    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


class Config:
    def __init__(self, session: str, token: str, pane_width: int):
        self.session = session
        self.token = token
        self.pane_width = pane_width


run_cmd = tmux_runtime.run_command

def tmux(config: Config, args: list[str], *, timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    return tmux_runtime.run_tmux(
        args,
        timeout=timeout,
        environment=codex_runtime.sanitized_agent_environment(),
    )


def parse_tmux_global_environment(output: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in output.splitlines():
        if not line or line.startswith("-") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            values[name] = value
    return values


def scrub_tmux_global_environment(config: Config) -> tuple[str, ...]:
    """Keep a persistent tmux server from carrying Faryo service internals."""

    result = tmux(config, ["show-environment", "-g"], timeout=2)
    if result.returncode != 0:
        return ()
    current = parse_tmux_global_environment(result.stdout)
    cleaned = codex_runtime.sanitized_agent_environment(current)
    changed: list[str] = []
    for name in sorted(current):
        if name in cleaned and cleaned[name] == current[name]:
            continue
        if name == "PYTHONPATH" and cleaned.get(name):
            update = tmux(
                config,
                ["set-environment", "-g", name, cleaned[name]],
                timeout=2,
            )
        else:
            update = tmux(
                config,
                ["set-environment", "-gu", name],
                timeout=2,
            )
        if update.returncode == 0:
            changed.append(name)
    return tuple(changed)


def tmux_target(config: Config) -> str:
    return config.session


def tmux_sessions(config: Config) -> list[str]:
    res = tmux(config, ["list-sessions", "-F", "#{session_name}"], timeout=2)
    if res.returncode != 0: return [config.session]
    return [line for line in res.stdout.splitlines() if line and line != "local-tmux-owner"]


def active_codex_thread_state(config: Config) -> tuple[dict[str, str], set[str]]:
    active: dict[str, str] = {}
    superseded: set[str] = set()
    for name in tmux_sessions(config):
        target = target_config(config, name)
        if agent_profile_in_pane(target) is not CODEX_PROFILE:
            continue
        # The live fd scan wins over the id recorded at dispatch: /new inside
        # a resumed session rotates the thread id, and the frozen id would
        # keep the Running badge on a transcript the pane no longer writes.
        cwd = get_pane_cwd(target)
        threads = active_agent_threads(target, cwd)
        if threads:
            thread_id = str(threads[0].get("id") or "")
            if thread_id: active[thread_id] = name
            superseded.update(str(row.get("id") or "") for row in threads[1:] if row.get("id"))
            continue
        session_id = tmux_session_option(config, name, "@faryo_agent_session_id")
        if tmux_session_option(config, name, "@faryo_agent_source") == CODEX_PROFILE.source and session_id:
            active[session_id] = name
    return active, superseded

def active_codex_thread_map(config: Config) -> dict[str, str]:
    active, _superseded = active_codex_thread_state(config)
    return active

def tmux_session_option(config: Config, session: str, key: str, value: str | None = None) -> str:
    if value is not None:
        tmux(config, ["set-option", "-q", "-t", session, key, value], timeout=2); return value
    res = tmux(config, ["show-options", "-qv", "-t", session, key], timeout=2); return res.stdout.strip() if res.returncode == 0 else ""

_session_catalog = session_catalog.SessionCatalog(
    session_catalog.CatalogBindings(
        state_db=lambda: AGENT_STATE_DB,
        session_index=lambda: CODEX_SESSION_INDEX,
        thread_columns=THREAD_COLUMNS,
        interactive_top_level_sql=INTERACTIVE_TOP_LEVEL_THREAD_SQL,
        interactive_sources=frozenset(INTERACTIVE_CODEX_THREAD_SOURCES),
        history_query_max_chars=AGENT_HISTORY_QUERY_MAX_CHARS,
        history_periods=frozenset(AGENT_HISTORY_PERIODS),
        history_archive_filters=frozenset(AGENT_HISTORY_ARCHIVE_FILTERS),
        config_factory=lambda name, token, width: Config(name, token, width),
        short_path=lambda value: short_path(value),
        session_index_title=lambda value: session_index_title(value),
        session_title_topic=lambda value, fallback: session_title_topic(value, fallback),
        tmux_session_option=lambda config, name, key, value=None: tmux_session_option(config, name, key, value),
        session_git_label=lambda cwd, cache, active=True: session_git_label(cwd, cache, active),
        session_git_cwd=lambda config, name, cwd: session_git_cwd(config, name, cwd),
        managed_session=lambda config, name: managed_session(config, name),
        agent_profile_in_pane=lambda config: agent_profile_in_pane(config),
        agent_session_lifecycle=lambda config, name, profile, managed=None: agent_session_lifecycle(
            config,
            name,
            profile,
            managed,
        ),
        active_codex_thread_state=lambda config: active_codex_thread_state(config),
        active_agent_thread=lambda config, cwd: active_agent_thread(config, cwd),
        tmux_sessions=lambda config: tmux_sessions(config),
        get_pane_cwd=lambda config: get_pane_cwd(config),
        session_created_ts=lambda config: session_created_ts(config),
        iso_from_ts=lambda value: iso_from_ts(value),
    )
)

parse_sqlite_timestamp = _session_catalog.parse_sqlite_timestamp
agent_state_rows = _session_catalog.agent_state_rows
codex_rows = _session_catalog.codex_rows
codex_count = _session_catalog.codex_count
codex_session_index_titles = _session_catalog.codex_session_index_titles
codex_thread_title = _session_catalog.codex_thread_title
codex_capture_session_metadata = _session_catalog.codex_capture_session_metadata
capture_event_digest = _session_catalog.capture_event_digest
path_under_root = _session_catalog.path_under_root
codex_session_item = _session_catalog.codex_session_item
clean_agent_history_query = _session_catalog.clean_agent_history_query
clean_agent_history_period = _session_catalog.clean_agent_history_period
clean_agent_history_archive = _session_catalog.clean_agent_history_archive
agent_history_period_cutoff = _session_catalog.agent_history_period_cutoff
agent_history_text_matches = _session_catalog.agent_history_text_matches
interactive_top_level_thread = _session_catalog.interactive_top_level_thread
codex_history_filter = _session_catalog.codex_history_filter
codex_history_page = _session_catalog.codex_history_page
codex_history_items = _session_catalog.codex_history_items
active_agent_session_items = _session_catalog.active_agent_session_items
agent_session_page = _session_catalog.agent_session_page
agent_session_items = _session_catalog.agent_session_items
codex_thread_record = _session_catalog.codex_thread_record
thread_record_archived = _session_catalog.thread_record_archived
codex_thread_by_id = _session_catalog.codex_thread_by_id
codex_thread_lifecycle_error_status = _session_catalog.codex_thread_lifecycle_error_status


def change_codex_thread_archive_state(
    config: Config,
    thread_id: str,
    archived: bool,
    history_root: str | None = None,
    lifecycle_rpc: Callable[[str, str, float], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    clean_id = clean_agent_session_id(thread_id)
    if not clean_id:
        raise OwnerError("invalid agent session id")
    thread = codex_thread_record(clean_id)
    if not thread or (history_root is not None and not path_under_root(str(thread.get("cwd") or ""), history_root)):
        raise OwnerError("agent session not found", HTTPStatus.NOT_FOUND)
    if thread_record_archived(thread) == archived:
        return {"agentSessionId": clean_id, "archived": archived, "duplicate": True}
    active, superseded = active_codex_thread_state(config)
    if clean_id in active or clean_id in superseded:
        raise OwnerError("active agent sessions cannot be archived", HTTPStatus.CONFLICT)
    method = "thread/archive" if archived else "thread/unarchive"
    response = (
        lifecycle_rpc(method, clean_id, 5.0)
        if lifecycle_rpc is not None
        else codex_app_server_rpc(method, {"threadId": clean_id}, timeout=5.0)
    )
    if not response.get("ok"):
        raise OwnerError(
            "Codex thread lifecycle request failed",
            codex_thread_lifecycle_error_status(str(response.get("error") or "")),
        )
    deadline = time.monotonic() + AGENT_ARCHIVE_VERIFY_TIMEOUT
    while time.monotonic() < deadline:
        current = codex_thread_record(clean_id)
        if current and thread_record_archived(current) == archived:
            with _codex_thread_cache_lock:
                _codex_thread_cache.pop(clean_id, None)
            return {"agentSessionId": clean_id, "archived": archived, "duplicate": False}
        time.sleep(0.05)
    raise OwnerError("Codex thread state did not settle", HTTPStatus.BAD_GATEWAY)


_codex_app_server_launch_version = ""
_codex_installation_reconcile_lock = threading.Lock()


def codex_app_server_argv(*args: str) -> list[str]:
    global _codex_app_server_launch_version
    argv = codex_cli_argv(*args)
    _codex_app_server_launch_version = installed_codex_version()
    return argv


_codex_app_server_client = codex_app_server.CodexAppServerClient(
    argv=codex_app_server_argv,
    client_version=lambda: release_version() or "0",
    environment=codex_runtime.codex_environment,
)


def agent_tail_ignorable(line: str, profile: AgentProfile) -> bool:
    return agent_meta_line(line, profile)


def agent_ready_for_input(config: Config, profile: AgentProfile = CODEX_PROFILE) -> bool:
    res = tmux(config, ["capture-pane", "-p", "-J", "-t", tmux_target(config), "-S", "-40"], timeout=3)
    if res.returncode != 0: return False
    text = CONTROL_RE.sub("", res.stdout.replace("\r\n", "\n").replace("\r", "\n"))
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if any("esc to interrupt" in line.lower() for line in lines[-12:]): return False
    while lines and agent_tail_ignorable(lines[-1].strip(), profile):
        lines.pop()
    return bool(lines and profile.input_prompt_re.match(lines[-1]))


FARYO_MANAGED_SESSION_RE = re.compile(r"^faryo([1-9][0-9]*)$")
clean_tmux_session_name = tmux_runtime.clean_tmux_session_name
clean_agent_session_id = tmux_runtime.clean_agent_session_id
clean_client_message_id = tmux_runtime.clean_client_message_id
clean_client_launch_id = tmux_runtime.clean_client_launch_id


# Keep the Owner module as the compatibility/composition surface while the
# terminal lifecycle policy has one implementation owner.  Late-bound calls
# inside SessionLaunchService deliberately continue to honor test adapters and
# deployment-specific runtime hooks patched on this module.
_session_launch = session_launch.SessionLaunchService(sys.modules[__name__])
agent_launch_executable = _session_launch.agent_launch_executable
codex_cli_argv = _session_launch.codex_cli_argv
codex_auto_update_enabled = _session_launch.codex_auto_update_enabled
codex_context_window_args = _session_launch.codex_context_window_args
managed_codex_launch_argv = _session_launch.managed_codex_launch_argv
agent_start_ready_timeout = _session_launch.agent_start_ready_timeout
installed_codex_version = _session_launch.installed_codex_version
refresh_command_catalog = _session_launch.refresh_command_catalog
refresh_command_catalog_if_needed = _session_launch.refresh_command_catalog_if_needed
agent_login_shell = _session_launch.agent_login_shell
next_faryo_session_name = _session_launch.next_faryo_session_name
managed_launch_session = _session_launch.managed_launch_session
wait_for_agent_runtime_ready = _session_launch.wait_for_agent_runtime_ready
_monitor_agent_runtime = _session_launch.monitor_agent_runtime
ensure_agent_start_monitor = _session_launch.ensure_agent_start_monitor
start_agent_runtime = _session_launch.start_agent_runtime
start_agent_runtime_async = _session_launch.start_agent_runtime_async
codex_resume_directory_requirement = _session_launch.codex_resume_directory_requirement
resume_codex_thread_session = _session_launch.resume_codex_thread_session
resume_agent_session = _session_launch.resume_agent_session
target_config = _session_launch.target_config
managed_session = _session_launch.managed_session
agent_session_lifecycle = _session_launch.agent_session_lifecycle
session_idle_seconds = _session_launch.session_idle_seconds
session_created_ts = _session_launch.session_created_ts
iso_from_ts = _session_launch.iso_from_ts
cleanup_managed_sessions = _session_launch.cleanup_managed_sessions
active_agent_count = _session_launch.active_agent_count
bounded_max_running = _session_launch.bounded_max_running
bounded_context_window_k = _session_launch.bounded_context_window_k
close_shell_session = _session_launch.close_shell_session
clean_agent_launch_command = _session_launch.clean_agent_launch_command


def has_session(config: Config) -> bool:
    res = tmux(config, ["has-session", "-t", tmux_target(config)], timeout=2)
    return res.returncode == 0


def get_pane_pid(config: Config) -> int | None:
    res = tmux(config, ["display-message", "-p", "-t", tmux_target(config), "#{pane_pid}"], timeout=2)
    if res.returncode != 0:
        return None
    text = res.stdout.strip()
    return int(text) if text.isdigit() else None


def get_pane_width(config: Config) -> int | None:
    res = tmux(config, ["display-message", "-p", "-t", tmux_target(config), "#{pane_width}"], timeout=2)
    if res.returncode != 0:
        return None
    text = res.stdout.strip()
    return int(text) if text.isdigit() else None


def ensure_pane_width(config: Config) -> None:
    if config.pane_width <= 0 or not has_session(config):
        return
    # Codex compact chat is sourced from App Server, so widening its live TUI
    # no longer improves transcript fidelity.  More importantly,
    # resize-window switches tmux to manual sizing: a narrower attached client
    # would then view a wide Codex screen and lines would appear not to wrap.
    # Leave Codex windows under tmux/client size control.
    if codex_cli_in_pane(config):
        return
    current_width = get_pane_width(config)
    if current_width is not None and current_width >= config.pane_width:
        return
    res = tmux(config, ["resize-window", "-t", tmux_target(config), "-x", str(config.pane_width)], timeout=3)
    if res.returncode != 0:
        raise OwnerError(res.stderr.strip() or "tmux resize-window failed", HTTPStatus.INTERNAL_SERVER_ERROR)


def get_pane_current_command(config: Config) -> str | None:
    res = tmux(config, ["display-message", "-p", "-t", tmux_target(config), "#{pane_current_command}"], timeout=2)
    if res.returncode != 0:
        return None
    return res.stdout.strip() or None


def get_pane_cwd(config: Config) -> str | None:
    res = tmux(config, ["display-message", "-p", "-t", tmux_target(config), "#{pane_current_path}"], timeout=2)
    if res.returncode != 0:
        return None
    return res.stdout.strip() or None


process_table = tmux_runtime.process_table
descendants = tmux_runtime.descendants


def agent_in_pane(config: Config) -> bool:
    return agent_profile_in_pane(config) is not None


def agent_session_running(config: Config, session: str | None) -> bool:
    if not session:
        return False
    try:
        target = target_config(config, session)
        profile = agent_profile_in_pane(target)
        return bool(profile and not agent_ready_for_input(target, profile))
    except OwnerError:
        return False


def agent_profile_in_pane(config: Config) -> AgentProfile | None:
    pane_pid = get_pane_pid(config)
    if pane_pid is None:
        return None
    pane_cmd = get_pane_current_command(config) or ""
    children = descendants(pane_pid, process_table())
    for profile in AGENT_PROFILES:
        if agent_profile_matches_cmd(profile, pane_cmd) or any(agent_profile_matches_cmd(profile, cmd) for _pid, cmd in children):
            return profile
    return None


def agent_profile_matches_cmd(profile: AgentProfile, cmd: str) -> bool:
    return profile is CODEX_PROFILE and is_codex_cli_cmd(cmd)


def codex_cli_in_pane(config: Config) -> bool:
    pane_pid = get_pane_pid(config)
    if pane_pid is None:
        return False
    pane_cmd = get_pane_current_command(config) or ""
    if agent_profile_matches_cmd(CODEX_PROFILE, pane_cmd):
        return True
    return any(agent_profile_matches_cmd(CODEX_PROFILE, cmd) for _pid, cmd in descendants(pane_pid, process_table()))


def is_codex_cli_cmd(cmd: str) -> bool:
    lowered = cmd.lower()
    if "playwright-mcp" in lowered:
        return False
    return "codex" in lowered and ("@openai/codex" in lowered or "/codex" in lowered or "bin/codex" in lowered or lowered.strip() == "codex")


def clean_capture(text: str, *, strip_input_tail: bool = True, profile: AgentProfile = CODEX_PROFILE) -> str:
    text = CONTROL_RE.sub("", text)
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    normalized: list[str] = []
    blank_count = 0
    for line in lines:
        if profile.placeholder_re.match(line):
            blank_count += 1
            if blank_count <= 1:
                normalized.append("")
            continue

        match = profile.boundary_re.match(line.strip())
        if match:
            normalized.append(match.group(1).strip())
            blank_count = 0
            continue
        if SEPARATOR_RE.match(line.strip()) or SEPARATOR_OUTPUT_RE.match(line) or LONG_SEPARATOR_RE.search(line):
            continue

        if not line.strip():
            blank_count += 1
            if blank_count <= 1:
                normalized.append("")
            continue

        blank_count = 0
        normalized.append(line)
    lines = strip_agent_input_tail(normalized, lambda line: line, profile) if strip_input_tail else normalized
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def agent_meta_line(line: str, profile: AgentProfile = CODEX_PROFILE) -> tuple[str, str] | None:
    match = profile.meta_re.match(line)
    if not match:
        return None
    return match.group(1).strip(), match.group(2).strip()


def strip_agent_input_tail(lines: list[str], plain: Callable[[str], str], profile: AgentProfile = CODEX_PROFILE) -> list[str]:
    end = len(lines)
    while end and not plain(lines[end - 1]).strip():
        end -= 1
    if not end:
        return lines

    scan_end = end
    while scan_end and agent_tail_ignorable(plain(lines[scan_end - 1]).strip(), profile):
        scan_end -= 1
        while scan_end and not plain(lines[scan_end - 1]).strip():
            scan_end -= 1

    prompt_index: int | None = None
    search_start = max(0, scan_end - 12)
    for index in range(scan_end - 1, search_start - 1, -1):
        line = plain(lines[index])
        if profile.input_prompt_re.match(line):
            prompt_index = index
            break

    if prompt_index is None:
        return lines
    if any(plain(line).strip() for line in lines[prompt_index + 1 : scan_end]):
        return lines
    return lines[:prompt_index]


def strip_agent_meta_lines(text: str, profile: AgentProfile = CODEX_PROFILE) -> str:
    lines = [line for line in text.split("\n") if not agent_meta_line(line, profile)]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def latest_agent_meta(text: str, profile: AgentProfile = CODEX_PROFILE) -> tuple[str, str] | None:
    for line in reversed(text.splitlines()):
        meta = agent_meta_line(line, profile)
        if meta:
            return meta
    return None


def meta_cwd_path(value: str | None) -> str | None:
    return str(value).split(" · ", 1)[0].strip() if value else None


def reasoning_effort_from_model_status(text: str | None) -> str | None:
    if not text:
        return None
    match = REASONING_EFFORT_SUFFIX_RE.search(text.strip())
    return match.group("effort").lower() if match else None


def codex_model_status(value: str | None) -> tuple[str | None, str | None, str]:
    """Split the TUI model row into model label, effort, and session speed."""
    if not value:
        return None, None, "off"
    model = FAST_MODEL_SUFFIX_RE.sub("", str(value).strip()).strip()
    fast_status = "on" if model != str(value).strip() else "off"
    return model or None, reasoning_effort_from_model_status(model), fast_status


def normalize_fast_state(value: str | bool | None) -> str | None:
    if isinstance(value, bool):
        return "on" if value else "off"
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"on", "true", "enabled", "yes", "1"}:
        return "on"
    if normalized in {"off", "false", "disabled", "no", "0"}:
        return "off"
    return None


def latest_fast_status(text: str) -> str | None:
    status = None
    for match in FAST_STATUS_RE.finditer(text):
        status = normalize_fast_state(match.group("state"))
    return status


def find_fast_config_value(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = str(key).replace("_", "-").lower()
            if normalized_key in FAST_CONFIG_KEYS:
                state = normalize_fast_state(child)
                if state:
                    return state
            state = find_fast_config_value(child)
            if state:
                return state
    elif isinstance(value, list):
        for child in value:
            state = find_fast_config_value(child)
            if state:
                return state
    return None


def configured_fast_status() -> str | None:
    config_path = Path.home() / ".codex" / "config.toml"
    try:
        with config_path.open("rb") as fh:
            config = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return find_fast_config_value(config)


def rollout_thread_id_from_path(value: str) -> str | None:
    match = ROLLOUT_THREAD_ID_RE.search(Path(value).name) or ROLLOUT_THREAD_ID_RE.search(value)
    return match.group("id") if match else None


def proc_rollout_thread_ids(pid: int) -> list[str]:
    fd_dir = Path("/proc") / str(pid) / "fd"
    thread_ids: list[str] = []
    try:
        entries = list(fd_dir.iterdir())
    except OSError:
        return thread_ids
    for entry in entries:
        try:
            target = os.readlink(entry).removesuffix(" (deleted)")
        except OSError:
            continue
        if thread_id := rollout_thread_id_from_path(target):
            thread_ids.append(thread_id)
    return thread_ids


def lsof_rollout_thread_ids(pid: int) -> list[str]:
    if not shutil.which("lsof"):
        return []
    res = run_cmd(["lsof", "-nP", "-p", str(pid)], timeout=2)
    if res.returncode != 0:
        return []
    return [thread_id for line in res.stdout.splitlines() if (thread_id := rollout_thread_id_from_path(line))]


def open_rollout_thread_ids(pid: int) -> list[str]:
    return proc_rollout_thread_ids(pid) or lsof_rollout_thread_ids(pid)


def active_agent_threads(config: Config, cwd: str | None) -> list[dict[str, Any]]:
    pane_pid = get_pane_pid(config)
    if pane_pid is None or not AGENT_STATE_DB.exists():
        return []

    process_ids: list[int] = []
    if agent_profile_matches_cmd(CODEX_PROFILE, get_pane_current_command(config) or ""):
        process_ids.append(pane_pid)
    process_ids.extend(pid for pid, cmd in descendants(pane_pid, process_table()) if agent_profile_matches_cmd(CODEX_PROFILE, cmd))

    thread_ids = list(dict.fromkeys(tid for pid in process_ids for tid in open_rollout_thread_ids(pid)))
    if not thread_ids:
        return []

    placeholders = ",".join("?" for _ in thread_ids)
    rows = agent_state_rows(f"SELECT {THREAD_COLUMNS} FROM threads WHERE id IN ({placeholders})", tuple(thread_ids))

    interactive_rows = [dict(row) for row in rows if interactive_top_level_thread(dict(row))]
    matches = [row for row in interactive_rows if cwd is None or row["cwd"] == cwd]
    return sorted(matches or interactive_rows, key=lambda row: parse_sqlite_timestamp(row.get("updated_at")), reverse=True)

def active_agent_thread(config: Config, cwd: str | None) -> dict[str, Any] | None:
    threads = active_agent_threads(config, cwd)
    if threads:
        thread = threads[0]
        thread_id = str(thread.get("id") or "")
        if thread_id and has_session(config):
            tmux_session_option(config, config.session, "@faryo_agent_source", CODEX_PROFILE.source)
            tmux_session_option(config, config.session, "@faryo_agent_session_id", thread_id)
        return thread

    # Codex may close the rollout file while idle. Reuse the last thread id
    # observed while the pane was active so structured clients do not fall
    # back to the lossy terminal screen between turns.
    if not codex_cli_in_pane(config):
        return None
    thread_id = tmux_session_option(config, config.session, "@faryo_agent_session_id")
    thread = codex_thread_by_id(thread_id) if thread_id else None
    if not thread:
        return None
    thread_cwd = str(thread.get("cwd") or "")
    return thread if cwd is None or not thread_cwd or thread_cwd == cwd else None


def latest_context_usage(history_path: str | None) -> dict[str, int | float] | None:
    state = codex_rollout_state(history_path)
    usage = state.get("contextUsage") if state else None
    return dict(usage) if isinstance(usage, dict) else None


def codex_context_usage_from_info(latest_info: Any) -> dict[str, int | float] | None:
    if not isinstance(latest_info, dict):
        return None
    try:
        last_usage = latest_info.get("last_token_usage")
        if not isinstance(last_usage, dict):
            return None
        input_tokens = int(last_usage.get("input_tokens") or 0)
        output_tokens = int(last_usage.get("output_tokens") or 0)
        used_tokens = int(last_usage.get("total_tokens") or (input_tokens + output_tokens))
        context_window = int(latest_info.get("model_context_window") or 0)
    except (TypeError, ValueError):
        return None
    if used_tokens <= 0 or context_window <= 0:
        return None

    return {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "usedTokens": used_tokens,
        "contextWindow": context_window,
        "contextWindowSource": "agent-reported",
        "percent": round((used_tokens / context_window) * 100, 1),
    }


def codex_rollout_context_usage(event: Any) -> dict[str, int | float] | None:
    if not isinstance(event, dict):
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    return codex_context_usage_from_info(payload.get("info"))


def send_app_server_message(process: subprocess.Popen[str], message: dict[str, Any]) -> bool:
    return _codex_app_server_client.send(process, message)


def read_app_server_message(process: subprocess.Popen[str], deadline: float) -> dict[str, Any] | None:
    return _codex_app_server_client.read(process, deadline)


def _stop_codex_app_server_locked() -> None:
    _codex_app_server_client.stop_locked()


def stop_codex_app_server() -> None:
    _codex_app_server_client.stop()


def reconcile_codex_installation() -> bool:
    """Restart version-bound helpers after the preflight replaced Codex."""
    global _codex_app_server_launch_version
    with _codex_installation_reconcile_lock:
        installed = installed_codex_version()
        if not installed or installed == _codex_app_server_launch_version:
            return False
        stop_codex_app_server()
        _codex_app_server_launch_version = installed
        refresh_command_catalog_if_needed()
        return True


def reconcile_managed_codex_update(config: Config, name: str) -> bool:
    if tmux_session_option(config, name, "@faryo_codex_update") != "updated":
        return False
    changed = reconcile_codex_installation()
    tmux_session_option(config, name, "@faryo_codex_update", "reconciled")
    return changed


def _start_codex_app_server_locked(timeout: float) -> subprocess.Popen[str] | None:
    return _codex_app_server_client.start_locked(timeout)


def codex_app_server_rpc(method: str, params: dict[str, Any], timeout: float = 2.5) -> dict[str, Any]:
    return _codex_app_server_client.rpc(method, params, timeout)


def codex_app_server_request(method: str, params: dict[str, Any], timeout: float = 2.5) -> dict[str, Any] | None:
    response = codex_app_server_rpc(method, params, timeout)
    result = response.get("result") if response.get("ok") else None
    return result if isinstance(result, dict) else None


def cached_codex_thread(thread_id: str) -> dict[str, Any] | None:
    now = time.monotonic()
    with _codex_thread_cache_lock:
        cached = _codex_thread_cache.get(thread_id)
        if cached and now - cached[0] < CODEX_TRANSCRIPT_CACHE_TTL:
            return cached[1]

    # Never hold the cache lock during an app-server round trip. A large
    # thread/read can otherwise block structured capture for every session.
    result = codex_app_server_request("thread/read", {"threadId": thread_id, "includeTurns": True})
    thread = result.get("thread") if isinstance(result, dict) else None
    if not isinstance(thread, dict):
        # A stale structured transcript is preferable to a lossy tmux fallback
        # while the app-server is restarting or temporarily busy.
        return cached[1] if cached else None
    with _codex_thread_cache_lock:
        _codex_thread_cache[thread_id] = (time.monotonic(), thread)
    return thread


codex_user_message_text = codex_history.user_message_text
turn_exceeds_recent_budget = codex_history.turn_exceeds_recent_budget


def codex_thread_transcript(thread: dict[str, Any], max_lines: int) -> str:
    return codex_history.thread_transcript(
        thread,
        max_lines,
        page_turns=CODEX_TRANSCRIPT_PAGE_TURNS,
        char_budget=CODEX_TRANSCRIPT_CHAR_BUDGET,
        min_turns=CODEX_TRANSCRIPT_MIN_TURNS,
    )


codex_rollout_message = codex_history.rollout_message


def codex_history_preview(text: str, max_chars: int = CODEX_HISTORY_PREVIEW_CHARS) -> str:
    return codex_history.history_preview(text, max_chars)


codex_history_revision = codex_history.history_revision
codex_history_cursor = codex_history.history_cursor


def decode_codex_history_cursor(cursor: str, revision: str) -> int:
    try:
        return codex_history.decode_history_cursor(cursor, revision)
    except codex_history.HistoryCursorError as exc:
        raise OwnerError(str(exc), HTTPStatus.CONFLICT if exc.expired else HTTPStatus.BAD_REQUEST) from exc


def store_codex_history_cache(key: str, state: dict[str, Any]) -> None:
    with _codex_history_cache_lock:
        _codex_history_cache.pop(key, None)
        _codex_history_cache[key] = state
        while len(_codex_history_cache) > CODEX_HISTORY_INDEX_MAX_PATHS:
            _codex_history_cache.pop(next(iter(_codex_history_cache)))


def cached_codex_history_state(key: str) -> dict[str, Any] | None:
    with _codex_history_cache_lock:
        state = _codex_history_cache.pop(key, None)
        if state is not None:
            _codex_history_cache[key] = state
        return state


def copy_codex_history_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "identity": state.get("identity"),
        "revision": state.get("revision"),
        "offset": int(state.get("offset") or 0),
        "turns": [
            {
                "index": int(turn.get("index") or 0),
                "key": str(turn.get("key") or ""),
                "preview": str(turn.get("preview") or ""),
                "records": [tuple(record) for record in turn.get("records") or []],
            }
            for turn in state.get("turns") or []
        ],
    }


def append_codex_history_index(path: Path, state: dict[str, Any], target_size: int) -> None:
    offset = int(state.get("offset") or 0)
    complete_offset = offset
    turns = state.setdefault("turns", [])
    revision = str(state.get("revision") or "")
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            while handle.tell() < target_size:
                start = handle.tell()
                raw_line = handle.readline(target_size - start)
                if not raw_line.endswith(b"\n"):
                    break
                complete_offset = handle.tell()
                message = parse_codex_rollout_event(raw_line.rstrip(b"\n"))["message"]
                if message is None:
                    continue
                role, text = message
                if role == "user":
                    index = len(turns)
                    turns.append({
                        "index": index,
                        "key": f"q-{revision}-{index:x}",
                        "preview": codex_history_preview(text),
                        "records": [],
                    })
                if turns:
                    turns[-1]["records"].append((start, complete_offset))
    except OSError:
        return
    state["offset"] = complete_offset


def codex_history_state(history_path: str | None) -> dict[str, Any] | None:
    if not history_path:
        return None
    path = Path(history_path).expanduser()
    key = str(path)
    with _codex_history_cache_lock:
        path_lock = _codex_history_path_locks.setdefault(key, threading.Lock())
    with path_lock:
        cached = cached_codex_history_state(key)
        try:
            stat = path.stat()
        except OSError:
            return copy_codex_history_state(cached) if cached else None
        identity = (stat.st_dev, stat.st_ino)
        offset = int(cached.get("offset") or 0) if cached else 0
        reset_existing = cached is not None and (cached.get("identity") != identity or stat.st_size < offset)
        if cached is None or reset_existing:
            revision_seed = (*identity, stat.st_mtime_ns, stat.st_size) if reset_existing else identity
            cached = {
                "identity": identity,
                "revision": codex_history_revision(revision_seed),
                "offset": 0,
                "turns": [],
            }
        if stat.st_size > int(cached.get("offset") or 0):
            append_codex_history_index(path, cached, stat.st_size)
        store_codex_history_cache(key, cached)
        return copy_codex_history_state(cached)


def codex_history_turn_text(handle: Any, turn: dict[str, Any]) -> str:
    blocks: list[str] = []
    for start, end in turn.get("records") or []:
        try:
            handle.seek(int(start))
            raw_line = handle.read(max(0, int(end) - int(start))).rstrip(b"\n")
        except OSError:
            continue
        message = parse_codex_rollout_event(raw_line)["message"]
        if message is None:
            continue
        role, text = message
        blocks.append(f"› {text}" if role == "user" else f"• {text}")
    return "\n\n".join(blocks).strip()


def codex_conversation_history_page(
    history_path: str | None,
    *,
    limit: int = CODEX_HISTORY_PAGE_TURNS,
    cursor: str = "",
    around: int | None = None,
) -> dict[str, Any]:
    state = codex_history_state(history_path)
    if not state or not history_path:
        raise OwnerError("structured conversation history is unavailable", HTTPStatus.NOT_FOUND)
    revision = str(state.get("revision") or "")
    turns = list(state.get("turns") or [])
    total = len(turns)
    page_limit = max(1, min(int(limit), CODEX_HISTORY_MAX_PAGE_TURNS))
    if cursor and around is not None:
        raise OwnerError("choose either a history cursor or an around index")
    if around is not None:
        if around < 0 or around >= total:
            raise OwnerError("conversation history index out of range")
        start = max(0, around - page_limit // 2)
        end = min(total, start + page_limit)
        start = max(0, end - page_limit)
    else:
        end = decode_codex_history_cursor(cursor, revision) if cursor else total
        end = max(0, min(total, end))
        start = max(0, end - page_limit)

    selected = turns[start:end]
    path = Path(history_path).expanduser()
    rendered: list[dict[str, Any]] = []
    try:
        with path.open("rb") as handle:
            for turn in selected:
                rendered.append({
                    "index": int(turn["index"]),
                    "key": str(turn["key"]),
                    "preview": str(turn["preview"]),
                    "text": codex_history_turn_text(handle, turn),
                })
    except OSError as exc:
        raise OwnerError("structured conversation history is unavailable", HTTPStatus.NOT_FOUND) from exc

    target_index = around if around is not None else max(start, end - 1)
    while len(rendered) > 1 and sum(len(item["text"]) for item in rendered) > CODEX_HISTORY_PAGE_CHAR_BUDGET:
        if around is None:
            rendered.pop(0)
        elif abs(rendered[0]["index"] - target_index) >= abs(rendered[-1]["index"] - target_index):
            rendered.pop(0)
        else:
            rendered.pop()
    if rendered:
        start = int(rendered[0]["index"])
        end = int(rendered[-1]["index"]) + 1

    return {
        "ok": True,
        "source": "codex-jsonl",
        "revision": revision,
        "totalTurns": total,
        "start": start,
        "end": end,
        "hasOlder": start > 0,
        "hasNewer": end < total,
        "olderCursor": codex_history_cursor(revision, start) if start > 0 else "",
        "newerCursor": codex_history_cursor(revision, min(total, end + page_limit)) if end < total else "",
        "questions": [
            {"index": int(turn["index"]), "key": str(turn["key"]), "preview": str(turn["preview"])}
            for turn in turns
        ],
        "turns": rendered,
        "pageChars": sum(len(item["text"]) for item in rendered),
        "oversized": any(len(item["text"]) > CODEX_HISTORY_PAGE_CHAR_BUDGET for item in rendered),
        "updatedAt": now_iso(),
    }


def codex_history_page_for_config(
    config: Config,
    *,
    limit: int = CODEX_HISTORY_PAGE_TURNS,
    cursor: str = "",
    around: int | None = None,
) -> dict[str, Any]:
    cwd = get_pane_cwd(config)
    thread = active_agent_thread(config, cwd)
    history_path = str(thread.get("rollout_path") or "") if thread else ""
    return codex_conversation_history_page(history_path, limit=limit, cursor=cursor, around=around)


def bounded_codex_rollout_messages(messages: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return codex_history.bounded_rollout_messages(
        messages,
        page_turns=CODEX_TRANSCRIPT_PAGE_TURNS,
        line_budget=CODEX_ROLLOUT_CACHE_LINE_BUDGET,
        char_budget=CODEX_ROLLOUT_CACHE_CHAR_BUDGET,
        min_turns=CODEX_ROLLOUT_CACHE_MIN_TURNS,
    )


def store_codex_rollout_cache(key: str, state: dict[str, Any]) -> None:
    """Store a most-recently-used bounded set of rollout states."""
    with _codex_rollout_cache_lock:
        _codex_rollout_cache.pop(key, None)
        _codex_rollout_cache[key] = state
        while len(_codex_rollout_cache) > CODEX_ROLLOUT_CACHE_MAX_PATHS:
            _codex_rollout_cache.pop(next(iter(_codex_rollout_cache)))


def cached_codex_rollout_state(key: str) -> dict[str, Any] | None:
    with _codex_rollout_cache_lock:
        state = _codex_rollout_cache.pop(key, None)
        if state is not None:
            _codex_rollout_cache[key] = state
        return state


def parse_codex_rollout_event(raw_line: bytes) -> dict[str, Any]:
    try:
        event = json.loads(raw_line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"message": None, "contextUsage": None, "goalStatus": None, "goalCallId": None, "goalOutput": None}
    return {
        "message": codex_rollout_message(event),
        "contextUsage": codex_rollout_context_usage(event),
        "goalStatus": codex_history.direct_goal_snapshot(event),
        "goalCallId": codex_history.goal_tool_call_id(event),
        "goalOutput": codex_history.goal_tool_output(event),
    }


def goal_status_before_newer_user_turn(snapshot: dict[str, Any], newer_user_turn: bool) -> dict[str, Any]:
    if newer_user_turn and snapshot.get("status") == "complete":
        return {"status": "none"}
    return snapshot


def initial_codex_rollout_state(path: Path, identity: tuple[int, int]) -> dict[str, Any]:
    """Build a bounded state by scanning complete JSONL records from the tail."""
    messages_reversed: list[tuple[str, str]] = []
    context_usage: dict[str, int | float] | None = None
    line_budget = 0
    char_budget = 0
    turn_count = 0
    complete_end = 0
    goal_status: dict[str, Any] | None = None
    goal_call_ids: set[str] = set()
    pending_goal_outputs: dict[str, dict[str, Any]] = {}
    newer_user_turn = False
    try:
        with path.open("rb") as fh:
            if os.fstat(fh.fileno()).st_size <= 0:
                return {"identity": identity, "offset": 0, "messages": [], "contextUsage": None, "goalStatus": None, "goalCallIds": set()}
            with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
                size = len(mapped)
                if size <= 0:
                    return {"identity": identity, "offset": 0, "messages": [], "contextUsage": None, "goalStatus": None, "goalCallIds": set()}
                if mapped[size - 1] == 0x0A:
                    complete_end = size
                else:
                    final_newline = mapped.rfind(b"\n", 0, size)
                    if final_newline < 0:
                        return {"identity": identity, "offset": 0, "messages": [], "contextUsage": None, "goalStatus": None, "goalCallIds": set()}
                    complete_end = final_newline + 1

                scan_floor = max(0, complete_end - CODEX_ROLLOUT_TAIL_SCAN_BYTES)
                cursor = complete_end
                while cursor > scan_floor:
                    line_end = cursor - 1 if mapped[cursor - 1] == 0x0A else cursor
                    previous_newline = mapped.rfind(b"\n", scan_floor, line_end)
                    if previous_newline < 0:
                        if scan_floor > 0:
                            break
                        line_start = 0
                    else:
                        line_start = previous_newline + 1
                    raw_line = mapped[line_start:line_end]
                    cursor = line_start
                    if not raw_line:
                        continue
                    signals = parse_codex_rollout_event(raw_line)
                    message = signals["message"]
                    usage = signals["contextUsage"]
                    if context_usage is None and usage is not None:
                        context_usage = usage
                    if message is not None and message[0] == "user":
                        newer_user_turn = True
                    if goal_status is None:
                        direct_goal = signals["goalStatus"]
                        goal_output = signals["goalOutput"]
                        goal_call_id = signals["goalCallId"]
                        if direct_goal is not None:
                            goal_status = goal_status_before_newer_user_turn(direct_goal, newer_user_turn)
                        elif goal_output is not None:
                            output_call_id, snapshot = goal_output
                            pending_goal_outputs[output_call_id] = snapshot
                            if len(pending_goal_outputs) > 64:
                                pending_goal_outputs.pop(next(iter(pending_goal_outputs)))
                        elif goal_call_id:
                            snapshot = pending_goal_outputs.pop(goal_call_id, None)
                            if snapshot is not None:
                                goal_status = goal_status_before_newer_user_turn(snapshot, newer_user_turn)
                            else:
                                goal_call_ids.add(goal_call_id)
                    if message is not None:
                        messages_reversed.append(message)
                        line_budget += message[1].count("\n") + 1
                        char_budget += len(message[1])
                        if message[0] == "user":
                            turn_count += 1
                        if (
                            message[0] == "user"
                            and context_usage is not None
                            and goal_status is not None
                            and (
                                turn_count >= CODEX_TRANSCRIPT_PAGE_TURNS
                                or char_budget >= CODEX_ROLLOUT_CACHE_CHAR_BUDGET
                                or (
                                    line_budget >= CODEX_ROLLOUT_CACHE_LINE_BUDGET
                                    and turn_count >= CODEX_ROLLOUT_CACHE_MIN_TURNS
                                )
                            )
                        ):
                            break
    except (OSError, ValueError):
        return {"identity": identity, "offset": 0, "messages": [], "contextUsage": None, "goalStatus": None, "goalCallIds": set()}

    messages = bounded_codex_rollout_messages(list(reversed(messages_reversed)))
    return {
        "identity": identity,
        "offset": complete_end,
        "messages": messages,
        "contextUsage": context_usage,
        "goalStatus": goal_status,
        "goalCallIds": goal_call_ids,
    }


def codex_rollout_state(history_path: str | None) -> dict[str, Any] | None:
    """Return a bounded, incrementally updated state for a durable rollout."""
    if not history_path:
        return None
    path = Path(history_path).expanduser()
    key = str(path)
    with _codex_rollout_cache_lock:
        path_lock = _codex_rollout_path_locks.setdefault(key, threading.Lock())

    # One large conversation never serializes unrelated session reads.
    with path_lock:
        cached = cached_codex_rollout_state(key)
        try:
            stat = path.stat()
        except OSError:
            return cached

        identity = (stat.st_dev, stat.st_ino)
        offset = int(cached.get("offset") or 0) if cached else 0
        rebuild = (
            cached is None
            or cached.get("identity") != identity
            or stat.st_size < offset
            or stat.st_size - offset > CODEX_ROLLOUT_MAX_CATCHUP_BYTES
        )
        if rebuild:
            cached = initial_codex_rollout_state(path, identity)
            store_codex_rollout_cache(key, cached)
            return cached

        if stat.st_size == offset:
            store_codex_rollout_cache(key, cached)
            return cached

        try:
            with path.open("rb") as fh:
                fh.seek(offset)
                chunk = fh.read(stat.st_size - offset)
        except OSError:
            return cached

        # Leave a partial final record unread until Codex appends its newline.
        complete_end = chunk.rfind(b"\n")
        if complete_end < 0:
            store_codex_rollout_cache(key, cached)
            return cached

        messages = list(cached.get("messages") or [])
        context_usage = cached.get("contextUsage")
        goal_status = cached.get("goalStatus")
        goal_call_ids = set(cached.get("goalCallIds") or set())
        for raw_line in chunk[:complete_end].splitlines():
            signals = parse_codex_rollout_event(raw_line)
            message = signals["message"]
            usage = signals["contextUsage"]
            if message is not None:
                messages.append(message)
                if message[0] == "user" and isinstance(goal_status, dict) and goal_status.get("status") == "complete":
                    goal_status = {"status": "none"}
            if usage is not None:
                context_usage = usage
            if signals["goalStatus"] is not None:
                goal_status = signals["goalStatus"]
            if goal_call_id := signals["goalCallId"]:
                goal_call_ids.add(goal_call_id)
            if goal_output := signals["goalOutput"]:
                output_call_id, snapshot = goal_output
                if output_call_id in goal_call_ids:
                    goal_status = snapshot
                    goal_call_ids.discard(output_call_id)
            if len(goal_call_ids) > 64:
                goal_call_ids = set(sorted(goal_call_ids)[-64:])
        cached = {
            "identity": identity,
            "offset": offset + complete_end + 1,
            "messages": bounded_codex_rollout_messages(messages),
            "contextUsage": context_usage,
            "goalStatus": goal_status,
            "goalCallIds": goal_call_ids,
        }
        store_codex_rollout_cache(key, cached)
        return cached


def codex_rollout_messages(history_path: str | None) -> list[tuple[str, str]]:
    state = codex_rollout_state(history_path)
    return list(state.get("messages") or []) if state else []


def latest_goal_status(history_path: str | None) -> dict[str, Any] | None:
    state = codex_rollout_state(history_path)
    goal_status = state.get("goalStatus") if state else None
    return dict(goal_status) if isinstance(goal_status, dict) else None


def goal_details_for_config(config: Config) -> dict[str, Any]:
    if not has_session(config):
        raise OwnerError("tmux session not found", HTTPStatus.NOT_FOUND)
    thread = active_agent_thread(config, get_pane_cwd(config))
    thread_id = str((thread or {}).get("id") or "")
    if not thread_id:
        raise OwnerError("Codex thread is unavailable", HTTPStatus.NOT_FOUND)
    result = codex_app_server_request(
        "thread/goal/get",
        {"threadId": thread_id},
        timeout=3.0,
    )
    if not isinstance(result, dict) or "goal" not in result:
        raise OwnerError("Goal details are temporarily unavailable", HTTPStatus.BAD_GATEWAY)
    return codex_history.goal_details(result.get("goal"))


def codex_message_transcript(messages: list[tuple[str, str]], max_lines: int) -> str:
    return codex_history.message_transcript(
        messages,
        max_lines,
        page_turns=CODEX_TRANSCRIPT_PAGE_TURNS,
        char_budget=CODEX_TRANSCRIPT_CHAR_BUDGET,
        min_turns=CODEX_TRANSCRIPT_MIN_TURNS,
    )


def codex_message_blocks(blocks: list[dict[str, Any]], max_lines: int) -> list[dict[str, Any]]:
    messages = [(str(block.get("role") or "process"), str(block.get("text") or "")) for block in blocks]
    selected = codex_history.bounded_rollout_messages(
        messages,
        page_turns=CODEX_TRANSCRIPT_PAGE_TURNS,
        line_budget=max_lines,
        char_budget=CODEX_TRANSCRIPT_CHAR_BUDGET,
        min_turns=CODEX_TRANSCRIPT_MIN_TURNS,
    )
    return [dict(block) for block in blocks[-len(selected):]] if selected else []


def appserver_context_usage(token_usage: Any) -> dict[str, int | float] | None:
    if not isinstance(token_usage, dict):
        return None
    last = token_usage.get("last")
    if not isinstance(last, dict):
        return None
    try:
        input_tokens = int(last.get("inputTokens") or 0)
        output_tokens = int(last.get("outputTokens") or 0)
        used_tokens = int(last.get("totalTokens") or input_tokens + output_tokens)
        context_window = int(token_usage.get("modelContextWindow") or 0)
    except (TypeError, ValueError):
        return None
    if used_tokens < 0 or context_window <= 0:
        return None
    return {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "usedTokens": used_tokens,
        "contextWindow": context_window,
        "contextWindowSource": "agent-reported",
        "percent": round((used_tokens / context_window) * 100, 1),
    }


def web_capture_payload_from_state(capture: dict[str, Any], lines: int) -> dict[str, Any]:
    record = capture.get("record") or {}
    snapshot = capture.get("snapshot") or {}
    messages = capture.get("messages") or []
    message_blocks = capture.get("messageBlocks") or []
    snapshot_items = snapshot.get("items") if isinstance(snapshot.get("items"), list) else []
    streaming_item = next(
        (
            item
            for item in reversed(snapshot_items)
            if isinstance(item, dict)
            and item.get("type") == "agentMessage"
            and not bool(item.get("final"))
        ),
        None,
    )
    lifecycle = str(snapshot.get("lifecycle") or "loading")
    running = lifecycle in {"running", "waiting_for_approval", "waiting_for_input"}
    return {
        "ok": True,
        "text": codex_message_transcript(messages, lines),
        "messageBlocks": codex_message_blocks(message_blocks, lines),
        "commandEvents": list(capture.get("commandEvents") or []),
        "agentRunning": running,
        "queuedSendNowAvailable": False,
        "agentSource": "codex-app-server",
        "agentProfile": "codex",
        "captureSource": "codex-app-server",
        "streaming": running,
        "sessionId": record.get("threadId"),
        "sessionTitle": record.get("title") or record.get("threadId"),
        "contextUsage": appserver_context_usage(snapshot.get("tokenUsage")),
        "goalStatus": codex_history.goal_snapshot(snapshot.get("goal"), allow_none=True),
        "interaction": snapshot.get("interaction"),
        "interactionRevision": str(snapshot.get("interactionRevision") or "none"),
        "streamRevision": int(snapshot.get("revision") or 0),
        "streamItemId": str(streaming_item.get("id") or "") if streaming_item else "",
        "streamTurnId": str(streaming_item.get("turnId") or "") if streaming_item else "",
        "streamItemRevision": int(streaming_item.get("revision") or 0) if streaming_item else 0,
        "backend": session_backend.APP_SERVER.value,
        "updatedAt": now_iso(),
    }


def web_capture_payload(
    runtime: appserver_runtime.AppServerRuntime,
    session: str,
    lines: int,
) -> dict[str, Any]:
    try:
        capture = runtime.capture(session)
    except appserver_runtime.AppServerRuntimeError as exc:
        raise OwnerError(str(exc), HTTPStatus.BAD_GATEWAY) from exc
    return web_capture_payload_from_state(capture, lines)


def web_activity_detail(
    runtime: appserver_runtime.AppServerRuntime,
    session: str,
    item_id: str,
) -> dict[str, Any]:
    try:
        return runtime.activity_detail(session, item_id)
    except appserver_runtime.AppServerRuntimeError:
        # Some completed tool items are intentionally omitted by thread/read
        # after reconnect.  The Codex rollout remains authoritative for those
        # items; project only the requested bounded detail.
        capture = runtime.capture(session)
        record = capture.get("record") if isinstance(capture.get("record"), dict) else {}
        thread_id = str(record.get("threadId") or "")
        thread = codex_thread_by_id(thread_id) if thread_id else None
        history_path = str(thread.get("rollout_path") or "") if isinstance(thread, dict) else ""
        if history_path and rollout_thread_id_from_path(history_path) == thread_id:
            detail = appserver_rollout.activity_detail(history_path, item_id)
            if detail is not None:
                return {"item": item_id, "detail": detail, "source": "codex-rollout"}
        raise OwnerError("activity detail is unavailable", HTTPStatus.NOT_FOUND) from None


def web_conversation_history_page(
    capture: dict[str, Any],
    *,
    limit: int,
    cursor: str = "",
    around: int | None = None,
) -> dict[str, Any]:
    record = capture.get("record") if isinstance(capture.get("record"), dict) else {}
    snapshot = capture.get("snapshot") if isinstance(capture.get("snapshot"), dict) else {}
    thread_id = str(record.get("threadId") or "")
    durable_activity: list[dict[str, Any]] = []
    if snapshot.get("durableActivityRequired") is True and thread_id:
        thread = codex_thread_by_id(thread_id)
        history_path = str(thread.get("rollout_path") or "") if thread else ""
    else:
        history_path = ""
    if history_path and rollout_thread_id_from_path(history_path) == thread_id:
        turn_ids = [
            str(turn.get("id") or "")
            for turn in (snapshot.get("turns") or [])
            if isinstance(turn, dict) and turn.get("id")
        ]
        durable_activity = appserver_rollout.activity_blocks(history_path, turn_ids)
    try:
        return appserver_history.conversation_history_page(
            snapshot,
            thread_id=thread_id,
            message_blocks=[
                item
                for item in (capture.get("messageBlocks") or [])
                if isinstance(item, dict)
            ],
            durable_activity=durable_activity,
            limit=limit,
            cursor=cursor,
            around=around,
            max_page_turns=CODEX_HISTORY_MAX_PAGE_TURNS,
            page_char_budget=CODEX_HISTORY_PAGE_CHAR_BUDGET,
            preview_chars=CODEX_HISTORY_PREVIEW_CHARS,
            updated_at=now_iso,
        )
    except codex_history.HistoryCursorError as exc:
        raise OwnerError(
            str(exc),
            HTTPStatus.CONFLICT if exc.expired else HTTPStatus.BAD_REQUEST,
        ) from exc


def web_status_payload(runtime: appserver_runtime.AppServerRuntime, session: str) -> dict[str, Any]:
    try:
        state = runtime.capture(session)
    except appserver_runtime.AppServerRuntimeError as exc:
        raise OwnerError(str(exc), HTTPStatus.BAD_GATEWAY) from exc
    capture = web_capture_payload_from_state(state, CAPTURE_COMPACT_LINES)
    record = state.get("record") or {}
    snapshot = state.get("snapshot") or {}
    lifecycle = str(snapshot.get("lifecycle") or "loading")
    running = bool(capture.get("agentRunning"))
    agent_state = {
        "idle": "waiting",
        "loading": "starting",
        "unloaded": "exited",
        "failed": "exited",
    }.get(lifecycle, "pending_interaction" if lifecycle.startswith("waiting_for_") else lifecycle)
    cwd = str(record.get("cwd") or "")
    thread = snapshot.get("thread") if isinstance(snapshot.get("thread"), dict) else {}
    model = str(record.get("model") or thread.get("model") or "Codex")
    return {
        "ok": True,
        "tmuxAlive": False,
        "targetAlive": True,
        "releaseVersion": release_version(),
        "session": session,
        "ownerLabel": owner_label(),
        "paneWidth": None,
        "cwd": cwd,
        "displayCwd": short_path(cwd),
        "shortCwd": short_path(cwd),
        "model": model,
        "reasoningEffort": thread.get("reasoningEffort"),
        "fastStatus": "on" if str(thread.get("serviceTier") or "") == "fast" else "off",
        "codexUpdateStatus": "",
        "gitStatus": git_status(cwd),
        "sessionTitle": record.get("title") or record.get("threadId"),
        "sessionId": record.get("threadId"),
        "contextUsage": capture.get("contextUsage"),
        "goalStatus": capture.get("goalStatus"),
        "interaction": capture.get("interaction"),
        "interactionRevision": capture.get("interactionRevision"),
        "weeklyRateLimit": rate_limit_from_response(snapshot.get("rateLimits") or {}),
        "agentRunning": running,
        "agentState": agent_state,
        "launchError": "" if runtime.ready() else "Codex App Server is reconnecting.",
        "queuedSendNowAvailable": False,
        "paneCommand": None,
        "agentSource": "codex-app-server",
        "agentProfile": "codex",
        "backend": session_backend.APP_SERVER.value,
        "updatedAt": now_iso(),
    }


def web_agent_session_items(
    runtime: appserver_runtime.AppServerRuntime,
    history_root: str | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    git_labels: dict[str, str] = {}
    for record in runtime.session_records():
        cwd = str(record.get("cwd") or "")
        if history_root is not None and not path_under_root(cwd, history_root):
            continue
        session = str(record.get("session") or "")
        try:
            snapshot = runtime.capture(session).get("snapshot") or {}
        except appserver_runtime.AppServerRuntimeError:
            snapshot = {}
        lifecycle = str(snapshot.get("lifecycle") or "loading")
        state = {
            "idle": "waiting",
            "loading": "starting",
            "unloaded": "exited",
            "failed": "exited",
        }.get(lifecycle, "pending_interaction" if lifecycle.startswith("waiting_for_") else lifecycle)
        updated_ts = int(record.get("updatedAt") or 0)
        items.append({
            "id": record.get("threadId"),
            "title": record.get("title") or record.get("threadId") or session,
            "gitLabel": session_git_label(cwd, git_labels),
            "cwd": short_path(cwd),
            "createdAt": iso_from_ts(int(record.get("createdAt") or 0)),
            "updatedAt": iso_from_ts(updated_ts),
            "updatedTs": updated_ts,
            "rolloutPath": "",
            "model": record.get("model") or "",
            "reasoningEffort": "",
            "source": "codex-app-server",
            "tmuxSession": session,
            "active": True,
            "managed": True,
            "agentRunning": lifecycle == "running",
            "state": state,
            "archived": False,
            "backend": session_backend.APP_SERVER.value,
        })
    return items


def codex_rollout_transcript(history_path: str | None, max_lines: int) -> str:
    return codex_message_transcript(codex_rollout_messages(history_path), max_lines)


def codex_structured_capture(config: Config, lines: int) -> tuple[str, str, str] | None:
    cwd = get_pane_cwd(config)
    thread = active_agent_thread(config, cwd)
    thread_id = str(thread.get("id") or "") if thread else ""
    if not thread_id:
        return None

    # The rollout is the durable source that Codex itself writes. Reading it
    # incrementally avoids repeated full thread/read calls for large histories
    # and preserves the original Markdown and TeX verbatim.
    history_path = str(thread.get("rollout_path") or "") if thread else ""
    if text := codex_rollout_transcript(history_path, lines):
        return text, thread_id, "codex-jsonl"

    # Older/imported sessions may not expose a rollout path; retain the Codex
    # app-server as a structured compatibility fallback.
    stored = cached_codex_thread(thread_id)
    if stored is None:
        return None
    text = codex_thread_transcript(stored, lines)
    # A newly created thread legitimately has zero turns until the first user
    # message.  An empty, successfully read thread is still structured state;
    # only a failed read should expose the lossy terminal fallback.
    return text, thread_id, "codex-app-server"


def codex_empty_managed_capture(config: Config) -> bool:
    """Recognize a new managed 0-turn TUI before Codex allocates a thread id."""
    return bool(
        managed_session(config, config.session)
        and tmux_session_option(config, config.session, "@faryo_launch_id")
        and not tmux_session_option(config, config.session, "@faryo_agent_session_id")
        and agent_ready_for_input(config, CODEX_PROFILE)
    )


def rate_limit_from_response(result: dict[str, Any]) -> dict[str, Any] | None:
    snapshots = result.get("rateLimitsByLimitId")
    snapshot = snapshots.get("codex") if isinstance(snapshots, dict) else None
    if not isinstance(snapshot, dict):
        snapshot = result.get("rateLimits")
    if not isinstance(snapshot, dict):
        return None

    primary = snapshot.get("primary")
    secondary = snapshot.get("secondary")
    weekly = secondary if isinstance(secondary, dict) else None
    if weekly and weekly.get("windowDurationMins") not in (None, 10080):
        weekly = None
    if weekly is None and isinstance(primary, dict):
        weekly = primary
    if not isinstance(weekly, dict):
        return None

    try:
        used_percent = float(weekly["usedPercent"])
    except (KeyError, TypeError, ValueError):
        return None

    return {
        "usedPercent": round(used_percent, 1),
        "windowDurationMins": weekly.get("windowDurationMins"),
        "resetsAt": weekly.get("resetsAt"),
        "limitId": snapshot.get("limitId"),
        "planType": snapshot.get("planType"),
    }


def fetch_weekly_rate_limit(timeout: float = 6.0) -> dict[str, Any] | None:
    # Reuse the Owner's serialized app-server channel instead of maintaining a
    # second short-lived protocol client. Structured history uses JSONL in the
    # normal path, so this low-frequency request does not contend with capture.
    result = codex_app_server_request("account/rateLimits/read", {}, timeout=timeout)
    return rate_limit_from_response(result) if isinstance(result, dict) else None


def refresh_weekly_rate_limit_cache() -> None:
    global _rate_limit_cache, _rate_limit_cache_at, _rate_limit_refreshing
    fresh = None
    try:
        fresh = fetch_weekly_rate_limit()
    except Exception:
        # Quota is optional status data. A transient CLI/subprocess failure
        # must not poison the singleton refresh flag forever.
        pass
    finally:
        with _rate_limit_lock:
            if fresh is not None:
                _rate_limit_cache = fresh
                _rate_limit_cache_at = time.monotonic()
            _rate_limit_refreshing = False


def cached_weekly_rate_limit() -> dict[str, Any] | None:
    global _rate_limit_refreshing

    now = time.monotonic()
    launch_refresh = False
    with _rate_limit_lock:
        if _rate_limit_cache is not None and now - _rate_limit_cache_at < RATE_LIMIT_CACHE_TTL:
            return _rate_limit_cache
        if not _rate_limit_refreshing:
            _rate_limit_refreshing = True
            launch_refresh = True
        cached = _rate_limit_cache
    if launch_refresh:
        try:
            threading.Thread(target=refresh_weekly_rate_limit_cache, name="faryo-codex-rate-limit", daemon=True).start()
        except Exception:
            with _rate_limit_lock:
                _rate_limit_refreshing = False
    return cached


def ansi_plain(line: str) -> str:
    if RichText is None:
        return line
    return RichText.from_ansi(line).plain


def clean_ansi_capture(text: str, profile: AgentProfile = CODEX_PROFILE) -> str:
    text = ANSI_CONTROL_RE.sub("", text)
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    normalized: list[str] = []
    blank_count = 0
    for line in lines:
        plain = ansi_plain(line).rstrip()
        if profile.placeholder_re.match(plain):
            blank_count += 1
            if blank_count <= 1:
                normalized.append("")
            continue

        match = profile.boundary_re.match(plain.strip())
        if match:
            normalized.append(match.group(1).strip())
            blank_count = 0
            continue
        if SEPARATOR_RE.match(plain.strip()) or SEPARATOR_OUTPUT_RE.match(plain) or LONG_SEPARATOR_RE.search(plain):
            continue

        if not plain.strip():
            blank_count += 1
            if blank_count <= 1:
                normalized.append("")
            continue

        blank_count = 0
        normalized.append(line)
    lines = strip_agent_input_tail(normalized, ansi_plain, profile)
    while lines and not ansi_plain(lines[0]).strip():
        lines.pop(0)
    while lines and not ansi_plain(lines[-1]).strip():
        lines.pop()
    return "\n".join(lines)


def sanitize_style_attr(match: re.Match[str]) -> str:
    kept: list[str] = []
    for raw_decl in match.group(1).split(";"):
        if ":" not in raw_decl:
            continue
        prop, value = raw_decl.split(":", 1)
        prop = prop.strip().lower()
        value = value.strip()
        normalized_value = value.lower().replace(" ", "")
        if prop in {"background", "background-color", "text-decoration-color"}:
            continue
        if prop == "color" and normalized_value in BLACK_VALUES | WHITE_VALUES:
            continue
        if prop == "color" and normalized_value in LOW_CONTRAST_TERMINAL_VALUES:
            value = USER_INPUT_COLOR
        kept.append(f"{prop}: {value}")
    return f' style="{"; ".join(kept)}"' if kept else ""


def sanitize_rich_html(html_fragment: str) -> str:
    match = RICH_PRE_RE.match(html_fragment)
    if match:
        html_fragment = match.group(1)
    return STYLE_ATTR_RE.sub(sanitize_style_attr, html_fragment)


def force_line_color(html_line: str, color: str) -> str:
    def replace_style(match: re.Match[str]) -> str:
        kept: list[str] = []
        for raw_decl in match.group(1).split(";"):
            if ":" not in raw_decl:
                continue
            prop, value = raw_decl.split(":", 1)
            prop = prop.strip().lower()
            value = value.strip()
            if prop == "color":
                kept.append("color: inherit")
            else:
                kept.append(f"{prop}: {value}")
        return f' style="{"; ".join(kept)}"'

    normalized = STYLE_ATTR_RE.sub(replace_style, html_line)
    return f'<span style="color: {color}">{normalized or " "}</span>'


def color_user_input_lines(html_fragment: str, plain_text: str, profile: AgentProfile = CODEX_PROFILE) -> str:
    html_lines = html_fragment.split("\n")
    text_lines = plain_text.split("\n")
    if len(html_lines) != len(text_lines):
        return html_fragment
    in_user_input = False
    out: list[str] = []
    for html_line, plain_line in zip(html_lines, text_lines, strict=True):
        if profile.user_prompt_re.match(plain_line):
            in_user_input = True
        elif not plain_line.strip():
            in_user_input = False

        out.append(force_line_color(html_line, USER_INPUT_COLOR) if in_user_input else html_line)
    return "\n".join(out)


def strip_agent_meta_html_lines(html_fragment: str, plain_text: str, profile: AgentProfile = CODEX_PROFILE) -> str:
    html_lines = html_fragment.split("\n")
    text_lines = plain_text.split("\n")
    if len(html_lines) != len(text_lines):
        return html_fragment
    return "\n".join(html_line for html_line, plain_line in zip(html_lines, text_lines, strict=True) if not agent_meta_line(plain_line, profile))


def capture_text(config: Config, lines: int = CAPTURE_DEFAULT_LINES, profile: AgentProfile = CODEX_PROFILE) -> str:
    if not has_session(config):
        raise OwnerError(f"tmux session not found: {config.session}", HTTPStatus.NOT_FOUND)
    safe_lines = max(20, min(lines, CAPTURE_MAX_LINES))
    res = tmux(config, ["capture-pane", "-p", "-J", "-t", tmux_target(config), "-S", f"-{safe_lines}"], timeout=3)
    if res.returncode != 0:
        raise OwnerError(res.stderr.strip() or "tmux capture failed", HTTPStatus.INTERNAL_SERVER_ERROR)
    return strip_agent_meta_lines(clean_capture(res.stdout, profile=profile), profile)


def codex_live_tail(text: str, max_lines: int = CODEX_LIVE_TAIL_LINES) -> str:
    lines = text.splitlines()
    user_starts = [index for index, line in enumerate(lines) if CODEX_PROFILE.user_prompt_re.match(line)]
    selected = lines[max(user_starts):] if user_starts else lines
    selected = selected[-max(1, max_lines):]
    redacted = [
        re.sub(r"(?i)(\bAccount:\s*).*$", r"\1<redacted>", line)
        for line in selected
    ]
    return "\n".join(redacted).strip()


def capture_html(config: Config, lines: int = CAPTURE_DEFAULT_LINES, profile: AgentProfile = CODEX_PROFILE) -> str | None:
    if RichConsole is None or RichText is None:
        return None
    if not has_session(config):
        raise OwnerError(f"tmux session not found: {config.session}", HTTPStatus.NOT_FOUND)
    safe_lines = max(20, min(lines, CAPTURE_MAX_LINES))
    res = tmux(config, ["capture-pane", "-p", "-e", "-J", "-t", tmux_target(config), "-S", f"-{safe_lines}"], timeout=3)
    if res.returncode != 0:
        raise OwnerError(res.stderr.strip() or "tmux capture failed", HTTPStatus.INTERNAL_SERVER_ERROR)
    ansi_text = clean_ansi_capture(res.stdout, profile)
    console = RichConsole(record=True, file=io.StringIO(), force_terminal=False, color_system="truecolor", width=4096)
    console.print(RichText.from_ansi(ansi_text), no_wrap=True, end="")
    match = HTML_CODE_RE.search(console.export_html(inline_styles=True, clear=False))
    if not match:
        return None
    plain_text = ansi_plain(ansi_text)
    html = color_user_input_lines(sanitize_rich_html(match.group(1)), plain_text, profile)
    return strip_agent_meta_html_lines(html, plain_text, profile)


def compact_capture_for_probe(text: str) -> str:
    return " ".join(clean_capture(text, strip_input_tail=False).split())


def tmux_capture_compact(config: Config, lines: int = 100) -> str:
    res = tmux(config, ["capture-pane", "-p", "-J", "-t", tmux_target(config), "-S", f"-{lines}"], timeout=2)
    return compact_capture_for_probe(res.stdout) if res.returncode == 0 else ""


def tmux_cursor_position(config: Config) -> tuple[int, int] | None:
    res = tmux(config, ["display-message", "-p", "-t", tmux_target(config), "#{cursor_x}\t#{cursor_y}"], timeout=2)
    if res.returncode != 0:
        return None
    try:
        x, y = res.stdout.strip().split("\t", 1)
        return int(x), int(y)
    except (TypeError, ValueError):
        return None


def tmux_current_capture(config: Config) -> str:
    res = tmux(config, ["capture-pane", "-p", "-J", "-t", tmux_target(config)], timeout=2)
    return CONTROL_RE.sub("", res.stdout.replace("\r\n", "\n").replace("\r", "\n")) if res.returncode == 0 else ""


def tmux_current_ansi_capture(config: Config) -> str:
    res = tmux(config, ["capture-pane", "-p", "-e", "-J", "-t", tmux_target(config)], timeout=2)
    return ANSI_CONTROL_RE.sub("", res.stdout.replace("\r\n", "\n").replace("\r", "\n")) if res.returncode == 0 else ""


def paste_tail_probe(text: str) -> str:
    compacted = " ".join(text.split())
    if len(compacted) <= PASTE_READY_MIN_PROBE_CHARS:
        return compacted
    return compacted[-min(80, len(compacted)):]


def last_agent_prompt_block_from_text(text: str, profile: AgentProfile = CODEX_PROFILE) -> str:
    lines = text.splitlines()
    prompt_index = next((index for index in range(len(lines) - 1, -1, -1) if profile.input_prompt_re.match(lines[index].strip())), None)
    if prompt_index is None:
        return ""
    return compact_capture_for_probe("\n".join(lines[prompt_index:]))


def last_agent_prompt_block(config: Config, profile: AgentProfile = CODEX_PROFILE) -> str:
    return last_agent_prompt_block_from_text(tmux_current_capture(config), profile)


def ansi_visible_cells(text: str) -> list[tuple[str, bool]]:
    """Return visible characters paired with the active ANSI dim state."""
    cells: list[tuple[str, bool]] = []
    dim = False
    cursor = 0
    for match in ANSI_SGR_RE.finditer(text):
        cells.extend((char, dim) for char in text[cursor:match.start()])
        raw_codes = match.group(1)
        codes = [0] if raw_codes == "" else [int(value or 0) for value in raw_codes.split(";")]
        for code in codes:
            if code == 0:
                dim = False
            elif code == 2:
                dim = True
            elif code == 22:
                dim = False
        cursor = match.end()
    cells.extend((char, dim) for char in text[cursor:])
    return cells


def ansi_prompt_has_real_text(line: str, profile: AgentProfile = CODEX_PROFILE) -> bool | None:
    """Distinguish a real Codex draft from its dim rotating placeholder."""
    cells = ansi_visible_cells(line)
    plain = "".join(char for char, _dim in cells)
    match = profile.input_prompt_re.match(plain)
    if not match:
        return None
    for char, dim in cells[match.end():]:
        if char.isspace():
            continue
        return not dim
    return False


def codex_composer_has_draft(config: Config) -> bool:
    ansi_capture = tmux_current_ansi_capture(config)
    for line in reversed(ansi_capture.splitlines()):
        has_text = ansi_prompt_has_real_text(line)
        if has_text is not None:
            return has_text
    # Minimal environments without styled capture retain the old conservative
    # cursor fallback. Wrapped and multiline drafts are handled by the ANSI
    # path used by the deployed Owner.
    cursor = tmux_cursor_position(config)
    return bool(cursor and cursor[0] > 2)


def codex_submission_key(config: Config) -> str:
    """Queue a new web message while Codex works; otherwise submit it."""
    # Codex 0.147 gives the active working composer its own `»` glyph.  Reading
    # that glyph is safer than searching the surrounding screen for status
    # text: completed output can still contain an old "esc to interrupt" line.
    for line in reversed(tmux_current_capture(config).splitlines()):
        stripped = line.strip()
        if CODEX_PROFILE.input_prompt_re.match(stripped):
            return "Tab" if stripped.startswith("»") else "Enter"
    return "Enter"


def codex_composer_contains_text(config: Config, text: str) -> bool:
    probe = paste_tail_probe(text)
    prompt = last_agent_prompt_block(config, CODEX_PROFILE)
    return bool(probe and probe in prompt)


def codex_queued_followup_count(capture: str, text: str) -> int:
    lines = capture.splitlines()
    marker = next((index for index in range(len(lines) - 1, -1, -1) if "queued follow-up inputs" in lines[index].lower()), None)
    probe = paste_tail_probe(text)
    if marker is None or not probe:
        return 0
    queued_lines: list[str] = []
    for line in lines[marker + 1:]:
        if CODEX_PROFILE.input_prompt_re.match(line.strip()):
            break
        queued_lines.append(line)
    return compact_capture_for_probe("\n".join(queued_lines)).count(probe)


def codex_queued_send_now_available(capture: str) -> bool:
    """Whether Codex explicitly advertises Esc as queued-message send-now."""

    compact = " ".join(CONTROL_RE.sub("", str(capture or "")).split()).casefold()
    return (
        "messages to be submitted after next tool call" in compact
        and "press esc to interrupt and send immediately" in compact
    )


def release_version() -> str:
    global RELEASE_VERSION_CACHE
    if RELEASE_VERSION_CACHE is not None:
        return RELEASE_VERSION_CACHE
    try:
        for line in RELEASE_FILE.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key == "version":
                RELEASE_VERSION_CACHE = value.strip()
                return RELEASE_VERSION_CACHE
    except OSError:
        pass
    RELEASE_VERSION_CACHE = ""
    return RELEASE_VERSION_CACHE


def wait_for_paste_tail(
    config: Config,
    text: str,
    baseline: str,
    baseline_cursor: tuple[int, int] | None = None,
) -> bool:
    probe = paste_tail_probe(text)
    if not probe:
        return True
    baseline_cursor = baseline_cursor or tmux_cursor_position(config)
    deadline = time.monotonic() + PASTE_READY_TIMEOUT
    while time.monotonic() < deadline:
        captured = tmux_capture_compact(config)
        if captured.count(probe) > baseline.count(probe):
            return True
        cursor = tmux_cursor_position(config)
        if cursor and baseline_cursor and cursor != baseline_cursor and cursor[0] > 2:
            return True
        time.sleep(PASTE_READY_POLL_INTERVAL)
    return False


def codex_rollout_submission_probe(config: Config) -> tuple[Path, int] | None:
    """Return the active rollout and its current EOF for exact delivery checks."""
    try:
        thread = active_agent_thread(config, get_pane_cwd(config))
        path_value = str(thread.get("rollout_path") or "") if thread else ""
        path = Path(path_value).expanduser()
        return (path, path.stat().st_size) if path_value and path.is_file() else None
    except OSError:
        return None


def codex_rollout_probe_state(probe: tuple[Path, int] | None) -> dict[str, int]:
    if probe is None:
        return {}
    path, offset = probe
    try:
        stat = path.stat()
    except OSError:
        return {}
    return {
        "rolloutDevice": int(stat.st_dev),
        "rolloutInode": int(stat.st_ino),
        "rolloutOffset": int(offset),
    }


def codex_rollout_probe_from_state(config: Config, state: dict[str, Any]) -> tuple[Path, int] | None:
    try:
        expected_device = int(state.get("rolloutDevice"))
        expected_inode = int(state.get("rolloutInode"))
        offset = int(state.get("rolloutOffset"))
    except (TypeError, ValueError):
        return None
    current = codex_rollout_submission_probe(config)
    if current is None or offset < 0:
        return None
    path, _current_offset = current
    try:
        stat = path.stat()
    except OSError:
        return None
    if int(stat.st_dev) != expected_device or int(stat.st_ino) != expected_inode:
        return None
    return path, offset


def codex_rollout_user_message(event: Any) -> str:
    if not isinstance(event, dict) or event.get("type") != "response_item":
        return ""
    payload = event.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "message" or payload.get("role") != "user":
        return ""
    values: list[str] = []
    for item in payload.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "input_text":
            values.append(str(item.get("text") or ""))
    return "\n".join(values).replace("\r\n", "\n").replace("\r", "\n").strip()


def codex_rollout_has_user_message(probe: tuple[Path, int] | None, text: str) -> bool:
    if probe is None:
        return False
    path, offset = probe
    expected = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    try:
        with path.open("rb") as fh:
            if offset > path.stat().st_size:
                return False
            fh.seek(offset)
            for raw_line in fh:
                try:
                    event = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if codex_rollout_user_message(event) == expected:
                    return True
    except OSError:
        return False
    return False


def wait_for_codex_submission(
    config: Config,
    text: str,
    timeout: float = SEND_ACCEPT_TIMEOUT,
    rollout_probe: tuple[Path, int] | None = None,
    queued_baseline: int | None = 0,
    allow_composer_disappearance: bool = True,
) -> str | None:
    probe = paste_tail_probe(text)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if codex_rollout_has_user_message(rollout_probe, text):
            return "recorded"
        capture = tmux_current_capture(config)
        if queued_baseline is not None and codex_queued_followup_count(capture, text) > queued_baseline:
            return "queued"
        prompt = last_agent_prompt_block_from_text(capture, CODEX_PROFILE)
        # A submitted message remains visible in transcript history, so the
        # pane as a whole is not a valid confirmation signal.  The active
        # composer is: once its exact tail disappears, Enter/Tab was accepted.
        # This also works while Codex is starting MCP servers, where rollout
        # persistence can lag several seconds behind the TUI.
        if allow_composer_disappearance and prompt and (not probe or probe not in prompt):
            return "submitted"
        time.sleep(PASTE_READY_POLL_INTERVAL)
    return None


def status_payload(config: Config) -> dict[str, Any]:
    tmux_alive = has_session(config)
    profile = agent_profile_in_pane(config) if tmux_alive else None
    capture_profile = profile or RUNTIME_PROFILE
    model = None
    reasoning_effort = None
    fast_status = None
    meta_cwd = None
    queued_send_now = False
    if tmux_alive:
        try:
            raw_text = clean_capture(
                tmux(config, ["capture-pane", "-p", "-J", "-t", tmux_target(config), "-S", "-80"], timeout=3).stdout,
                strip_input_tail=False,
                profile=capture_profile,
            )
            queued_send_now = bool(
                profile is CODEX_PROFILE
                and codex_queued_send_now_available(raw_text)
            )
            meta = latest_agent_meta(raw_text, capture_profile)
            fast_status = latest_fast_status(raw_text)
            if meta:
                model, reasoning_effort, fast_status = codex_model_status(meta[0])
                meta_cwd = meta_cwd_path(meta[1])
        except Exception:
            pass
    if fast_status is None:
        fast_status = configured_fast_status()
    if fast_status is None:
        fast_status = "off"
    cwd = get_pane_cwd(config) if tmux_alive else None
    thread = active_agent_thread(config, cwd) if tmux_alive else None
    history_path = str(thread.get("rollout_path") or "") if thread else ""
    context_usage = latest_context_usage(history_path)
    goal_status = latest_goal_status(history_path)
    weekly_rate_limit = None
    if tmux_alive and profile is CODEX_PROFILE:
        try:
            weekly_rate_limit = cached_weekly_rate_limit()
        except Exception:
            weekly_rate_limit = None
    managed = bool(tmux_alive and managed_session(config, config.session))
    agent_state, agent_running = agent_session_lifecycle(config, config.session, profile, managed) if tmux_alive else ("exited", False)
    codex_update_status = (
        tmux_session_option(config, config.session, "@faryo_codex_update")
        if tmux_alive
        else ""
    )
    interaction_state = {"interaction": None, "interactionRevision": "none"}
    if tmux_alive and profile is CODEX_PROFILE:
        interaction_state = interaction_snapshot(config)
        if interaction_state.get("interaction") is not None:
            agent_state = "pending_interaction"
            agent_running = False
    target_alive = tmux_alive
    session_title = codex_thread_title(thread, str(thread.get("id") or "Untitled session")) if thread else None
    return {
        "ok": tmux_alive,
        "tmuxAlive": tmux_alive,
        "targetAlive": target_alive,
        "releaseVersion": release_version(),
        "session": config.session,
        "ownerLabel": owner_label(),
        "paneWidth": get_pane_width(config) if tmux_alive else None,
        "cwd": cwd,
        "displayCwd": meta_cwd or short_path(cwd),
        "shortCwd": short_path(cwd) or meta_cwd,
        "model": model,
        "reasoningEffort": reasoning_effort,
        "fastStatus": fast_status,
        "codexUpdateStatus": codex_update_status,
        "gitStatus": git_status(session_git_cwd(config, config.session, cwd)),
        "sessionTitle": session_title,
        "sessionId": thread.get("id") if thread else None,
        "contextUsage": context_usage,
        "goalStatus": goal_status,
        **interaction_state,
        "weeklyRateLimit": weekly_rate_limit,
        "agentRunning": agent_running,
        "agentState": agent_state,
        "launchError": (
            "Codex did not become ready. Return Home and close this session."
            if managed and tmux_session_option(config, config.session, "@faryo_start_error")
            else ""
        ),
        "queuedSendNowAvailable": queued_send_now,
        "paneCommand": get_pane_current_command(config) if tmux_alive else None,
        "agentSource": profile.source if profile else "",
        "agentProfile": profile.key if profile else "",
        "backend": session_backend.CODEX_TUI.value,
        "updatedAt": now_iso(),
    }


def save_uploaded_attachment(file_item: Any, root_override: str | None = None) -> tuple[Path, int, str]:
    root_value = root_override or env_value("FARYO_OWNER_INBOX_DIR", "FARYO_OWNER_FILE_INBOX", default=str(FILE_INBOX_ROOT))
    try:
        return attachment_storage.save_uploaded_attachment(file_item, Path(root_value).expanduser())
    except attachment_storage.AttachmentStorageError as exc:
        raise OwnerError(str(exc), exc.status) from exc


def clean_local_path(value: str | None) -> str:
    try:
        return path_policy.clean_local_path(value)
    except path_policy.PathPolicyError as exc:
        raise OwnerError(str(exc), exc.status) from exc


def resolve_local_path(path_value: str | None, config: Config, suffixes: set[str], workspace_root: str | None = None) -> Path:
    bases = [get_pane_cwd(config), workspace_root, str(FILE_INBOX_ROOT)]
    try:
        return path_policy.resolve_local_file(path_value, bases, suffixes)
    except path_policy.PathPolicyError as exc:
        raise OwnerError(str(exc), exc.status) from exc


def resolve_local_image_path(path_value: str | None, config: Config, workspace_root: str | None = None) -> Path:
    return resolve_local_path(path_value, config, IMAGE_SUFFIXES, workspace_root)


def start_directory_roots(workspace_root: str | None = None) -> list[Path]:
    values = [part for part in os.environ.get("FARYO_START_DIRECTORY_ROOTS", "").split(os.pathsep) if part.strip()]
    return path_policy.start_directory_roots(values, workspace_root)


def resolve_start_directory(path_value: str | None, workspace_root: str | None = None) -> tuple[Path, list[Path]]:
    roots = start_directory_roots(workspace_root)
    try:
        path = path_policy.resolve_start_directory(path_value, roots)
    except path_policy.PathPolicyError as exc:
        raise OwnerError(str(exc), exc.status) from exc
    return path, roots


def directory_selection_token(config: Config, path: Path) -> str:
    return path_policy.directory_selection_token(config.token, path)


def directory_browser_payload(
    config: Config,
    path_value: str | None,
    workspace_root: str | None = None,
    *,
    show_hidden: bool = False,
) -> dict[str, Any]:
    path, roots = resolve_start_directory(path_value, workspace_root)
    try:
        parent, child_paths, truncated = path_policy.list_start_directories(
            path,
            roots,
            show_hidden=show_hidden,
        )
    except path_policy.PathPolicyError as exc:
        raise OwnerError(str(exc), exc.status) from exc
    directories = [{"name": child.name, "path": str(child), "displayPath": short_path(str(child))} for child in child_paths]
    return {
        "ok": True,
        "path": str(path),
        "displayPath": short_path(str(path)) or str(path),
        "selectionToken": directory_selection_token(config, path),
        "parent": str(parent) if parent else "",
        "parentDisplayPath": short_path(str(parent)) if parent else "",
        "directories": directories,
        "roots": [{"path": str(root), "displayPath": short_path(str(root)) or str(root)} for root in roots],
        "showHidden": bool(show_hidden),
        "truncated": truncated,
        "updatedAt": now_iso(),
    }


def compact_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def clean_session_title(value: Any) -> str:
    return compact_text(value)[:48]


class DeliveryRuntime:
    @property
    def delivery_store(self) -> delivery_store.DeliveryStore:
        return _delivery_store

    @property
    def delivery_ttl_seconds(self) -> float:
        return SEND_DELIVERY_TTL_SECONDS

    @property
    def max_send_chars(self) -> int:
        return MAX_SEND_CHARS

    @property
    def agent_launch_commands(self) -> set[str]:
        return AGENT_LAUNCH_COMMANDS

    @property
    def paste_settle_seconds(self) -> float:
        return PASTE_SETTLE_SECONDS

    @property
    def send_key_max_attempts(self) -> int:
        return SEND_KEY_MAX_ATTEMPTS

    @property
    def send_accept_retry_delay(self) -> float:
        return SEND_ACCEPT_RETRY_DELAY

    @staticmethod
    def owner_error(message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> OwnerError:
        return OwnerError(message, status)

    @staticmethod
    def clean_client_message_id(value: str | None) -> str | None:
        return clean_client_message_id(value)

    @staticmethod
    def shell_prep_matches(value: str) -> bool:
        return bool(SHELL_PREP_RE.fullmatch(value))

    @staticmethod
    def has_session(config: Config) -> bool:
        return has_session(config)

    @staticmethod
    def agent_in_pane(config: Config) -> bool:
        return agent_in_pane(config)

    @staticmethod
    def tmux(config: Config, args: list[str], timeout: float = 10) -> subprocess.CompletedProcess[str]:
        return tmux(config, args, timeout=timeout)

    @staticmethod
    def tmux_target(config: Config) -> str:
        return tmux_target(config)

    @staticmethod
    def agent_profile_in_pane(config: Config) -> AgentProfile | None:
        return agent_profile_in_pane(config)

    @staticmethod
    def is_codex_profile(profile: AgentProfile | None) -> bool:
        return profile is CODEX_PROFILE

    @staticmethod
    def codex_composer_has_draft(config: Config) -> bool:
        return codex_composer_has_draft(config)

    @staticmethod
    def codex_rollout_submission_probe(config: Config) -> tuple[Path, int] | None:
        return codex_rollout_submission_probe(config)

    @staticmethod
    def codex_rollout_probe_from_state(config: Config, state: dict[str, Any]) -> tuple[Path, int] | None:
        return codex_rollout_probe_from_state(config, state)

    @staticmethod
    def codex_rollout_probe_state(probe: tuple[Path, int] | None) -> dict[str, int]:
        return codex_rollout_probe_state(probe)

    @staticmethod
    def codex_queued_followup_count(capture: str, text: str) -> int:
        return codex_queued_followup_count(capture, text)

    @staticmethod
    def tmux_current_capture(config: Config) -> str:
        return tmux_current_capture(config)

    @staticmethod
    def tmux_capture_compact(config: Config) -> str:
        return tmux_capture_compact(config)

    @staticmethod
    def tmux_cursor_position(config: Config) -> tuple[int, int] | None:
        return tmux_cursor_position(config)

    @staticmethod
    def wait_for_paste_tail(
        config: Config,
        text: str,
        baseline: str,
        baseline_cursor: tuple[int, int] | None,
    ) -> bool:
        return wait_for_paste_tail(config, text, baseline, baseline_cursor)

    @staticmethod
    def sleep(seconds: float) -> None:
        time.sleep(seconds)

    @staticmethod
    def codex_composer_contains_text(config: Config, text: str) -> bool:
        return codex_composer_contains_text(config, text)

    @staticmethod
    def wait_for_codex_submission(config: Config, text: str, **kwargs: Any) -> str | None:
        return wait_for_codex_submission(config, text, **kwargs)

    @staticmethod
    def codex_submission_key(config: Config) -> str:
        return codex_submission_key(config)


_delivery_service = delivery_service.DeliveryService(DeliveryRuntime())
_send_delivery_lock = _delivery_service.lock
_send_session_locks = _delivery_service.session_locks
_send_message_locks = _delivery_service.message_locks
_send_deliveries = _delivery_service.deliveries


def scoped_send_delivery_lock(registry: dict[str, dict[str, Any]], key: str):
    return _delivery_service.scoped_lock(registry, key)


def send_session_delivery_lock(session: str):
    return _delivery_service.session_lock(session)


def send_message_delivery_lock(delivery_id: str):
    return _delivery_service.message_lock(delivery_id)


def send_delivery_record_path(delivery_id: str) -> Path | None:
    return _delivery_service.record_path(delivery_id)


def cleanup_persisted_send_deliveries(now_epoch: float | None = None, *, force: bool = False) -> None:
    _delivery_service.cleanup_persisted(now_epoch, force=force)


def persist_send_delivery(delivery_id: str, state: dict[str, Any]) -> bool:
    return _delivery_service.persist(delivery_id, state)


def load_persisted_send_delivery(delivery_id: str, now_epoch: float | None = None) -> dict[str, Any] | None:
    return _delivery_service.load(delivery_id, now_epoch)


def remember_accepted_send_delivery(delivery_id: str, state: dict[str, Any]) -> None:
    _delivery_service.remember_accepted(delivery_id, state)


def remember_pasted_send_delivery(delivery_id: str, state: dict[str, Any]) -> None:
    _delivery_service.remember_pasted(delivery_id, state)


def prune_send_deliveries(now: float | None = None) -> None:
    _delivery_service.prune(now)


def send_delivery_receipt(delivery_id: str, config: Config, state: str, enter_attempts: int, *, duplicate: bool = False) -> dict[str, Any]:
    return _delivery_service.receipt(
        delivery_id,
        config,
        state,
        enter_attempts,
        duplicate=duplicate,
    )


def send_text(config: Config, text: str, client_message_id: str | None = None) -> dict[str, Any]:
    return _delivery_service.send(config, text, client_message_id)


def send_key(config: Config, key: str) -> None:
    if not has_session(config):
        raise OwnerError(f"tmux session not found: {config.session}", HTTPStatus.NOT_FOUND)
    res = tmux(config, ["send-keys", "-t", tmux_target(config), key], timeout=3)
    if res.returncode != 0:
        raise OwnerError(res.stderr.strip() or f"tmux send {key} failed", HTTPStatus.INTERNAL_SERVER_ERROR)


def interrupt_agent(config: Config) -> dict[str, bool]:
    profile = agent_profile_in_pane(config)
    interrupted = bool(profile and not agent_ready_for_input(config, profile))
    queued_send_now = bool(
        interrupted
        and profile is CODEX_PROFILE
        and codex_queued_send_now_available(tmux_current_capture(config))
    )
    if interrupted:
        send_key(config, "Escape")
    return {
        "interrupted": interrupted,
        "queuedFollowupExpedited": queued_send_now,
    }


class InteractionRuntime:
    """Explicit adapter from the generic interaction service to this Owner."""

    @staticmethod
    def has_session(config: Config) -> bool:
        return has_session(config)

    @staticmethod
    def is_codex(config: Config) -> bool:
        return agent_profile_in_pane(config) is CODEX_PROFILE

    @staticmethod
    def capture(config: Config) -> str:
        return tmux_current_capture(config)

    @staticmethod
    def ready_for_input(config: Config) -> bool:
        return agent_ready_for_input(config, CODEX_PROFILE)

    @staticmethod
    def composer_has_draft(config: Config) -> bool:
        return codex_composer_has_draft(config)

    @staticmethod
    def composer_contains(config: Config, text: str) -> bool:
        return codex_composer_contains_text(config, text)

    @staticmethod
    def command_completion_ready(config: Config, command: str) -> bool:
        pattern = re.compile(rf"^\s{{2,}}{re.escape(command)}\s{{2,}}\S", re.I)
        return any(pattern.match(line) for line in tmux_current_capture(config).splitlines())

    @staticmethod
    def turn_running(config: Config) -> bool:
        lines = tmux_current_capture(config).splitlines()
        return any(
            "esc to interrupt" in line.lower() or line.lstrip().startswith("»")
            for line in lines[-12:]
        )

    @staticmethod
    def session_lock(session: str):
        return send_session_delivery_lock(session)

    @staticmethod
    def command_owner_key(config: Config) -> str:
        # A TUI interaction is owned by the concrete tmux session for its whole
        # lifetime.  Thread discovery may briefly disappear during resume or
        # compaction, so using it here would strand a waiting menu event.
        return f"tui:{config.session}"

    @staticmethod
    def command_anchor_key(config: Config) -> str:
        """Return the latest durable TUI turn key used by history pagination."""
        thread = active_agent_thread(config, get_pane_cwd(config))
        history_path = str(thread.get("rollout_path") or "") if thread else ""
        state = codex_history_state(history_path)
        turns = state.get("turns") if isinstance(state, dict) else None
        if not isinstance(turns, list) or not turns:
            return ""
        latest = turns[-1]
        return str(latest.get("key") or "") if isinstance(latest, dict) else ""

    @staticmethod
    def send_literal(config: Config, text: str) -> None:
        result = tmux(
            config,
            ["send-keys", "-t", tmux_target(config), "-l", text],
            timeout=3,
        )
        if result.returncode != 0:
            raise OwnerError(
                result.stderr.strip() or "tmux command input failed",
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    @staticmethod
    def send_key(config: Config, key: str) -> None:
        send_key(config, key)

    @staticmethod
    def sleep(seconds: float) -> None:
        time.sleep(seconds)

    @staticmethod
    def monotonic() -> float:
        return time.monotonic()


_interaction_service = interaction_service.InteractionService(
    InteractionRuntime(),
    codex_tui_interactions.detect_interaction,
    command_timeline=_command_timeline_store,
)


def command_timeline_store() -> command_timeline.CommandTimelineStore:
    return _command_timeline_store


def command_timeline_events_for_config(config: Config) -> list[dict[str, Any]]:
    return _command_timeline_store.public_events(InteractionRuntime.command_owner_key(config))


def interaction_snapshot(config: Config) -> dict[str, Any]:
    return _interaction_service.snapshot(config)


def interaction_snapshot_from_capture(config: Config, capture: str) -> dict[str, Any]:
    return _interaction_service.snapshot_from_capture(config, capture)


def begin_codex_command(config: Config, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return _interaction_service.begin_command(
            config,
            command=payload.get("command"),
            client_request_id=payload.get("clientRequestId") or payload.get("client_request_id"),
            confirmed=bool(payload.get("confirmed")),
        )
    except interaction_service.InteractionServiceError as exc:
        raise OwnerError(str(exc), exc.status) from exc


def respond_codex_interaction(config: Config, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return _interaction_service.respond(
            config,
            interaction_id=payload.get("interactionId") or payload.get("interaction_id"),
            action=payload.get("action"),
            option_id=payload.get("optionId") or payload.get("option_id"),
            client_request_id=payload.get("clientRequestId") or payload.get("client_request_id"),
        )
    except interaction_service.InteractionServiceError as exc:
        raise OwnerError(str(exc), exc.status) from exc



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local Tmux Owner mobile web bridge")
    parser.add_argument("--host", default=env_value("FARYO_OWNER_HOST", default="127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(env_value("FARYO_OWNER_PORT", default=str(DEFAULT_PORT))))
    parser.add_argument("--session", default=env_value("FARYO_OWNER_DIRECT_SESSION", default=DEFAULT_SESSION))
    parser.add_argument("--token", default=env_value("FARYO_OWNER_TOKEN", default=""))
    parser.add_argument("--pane-width", type=int, default=int(env_value("FARYO_OWNER_PANE_WIDTH", default=str(DEFAULT_PANE_WIDTH))))
    return parser.parse_args()


def main() -> int:
    """Compatibility entrypoint; production transport lives in Uvicorn."""

    from run_owner_asgi import main as run_asgi

    return run_asgi()


if __name__ == "__main__":
    raise SystemExit(main())
