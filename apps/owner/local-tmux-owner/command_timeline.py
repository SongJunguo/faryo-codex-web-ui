"""Privacy-bounded lifecycle storage for browser-issued Codex commands.

This is presentation metadata, not a transcript.  It deliberately stores no
prompt/answer bodies and exposes no raw Codex thread identity to the browser.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
from typing import Any, Mapping


SCHEMA_VERSION = 1
COMMAND_STATUSES = frozenset({"running", "waiting", "completed", "failed"})
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
COMMAND_RE = re.compile(r"^/[a-z][a-z-]*$")
NON_DURABLE_COMMANDS = frozenset({"/diff", "/help", "/mcp", "/status", "/usage"})
MAX_EVENTS = 512
MAX_EVENTS_PER_OWNER = 96
MAX_FILE_BYTES = 768 * 1024
MAX_SUMMARY_CHARS = 360
MAX_TITLE_CHARS = 240


class CommandTimelineError(RuntimeError):
    pass


def command_parts(invocation: object) -> tuple[str, str] | None:
    line = str(invocation or "").strip()
    if not line or len(line) > 4096 or any(value in line for value in ("\n", "\r", "\x00")):
        return None
    name, _separator, argument = line.partition(" ")
    name = name.lower()
    return (name, argument.strip()) if COMMAND_RE.fullmatch(name) else None


def command_is_durable(invocation: object) -> bool:
    parts = command_parts(invocation)
    if parts is None:
        return False
    name, argument = parts
    if name == "/goal":
        return bool(argument)
    return name not in NON_DURABLE_COMMANDS


def _safe_argument(name: str, argument: str) -> dict[str, str]:
    if not argument:
        return {}
    if name == "/rename":
        return {"title": argument[:MAX_TITLE_CHARS]}
    if name in {"/model", "/permissions"} and _safe_selection(argument):
        return {"selection": argument}
    if name == "/goal":
        # A goal objective is intentionally never persisted here.  Only a
        # finite control verb is safe display metadata.
        verb = argument.split(None, 1)[0].lower()
        return {"action": verb} if verb in {"clear", "pause", "resume"} else {}
    return {}


def _safe_selection(value: object) -> bool:
    text = str(value or "")
    return bool(text and len(text) <= 160 and not any(character in text for character in ("\n", "\r", "\x00")))


def command_label(name: str) -> str:
    return {
        "/compact": "Compact context",
        "/fast": "Response speed",
        "/goal": "Goal",
        "/model": "Model",
        "/permissions": "Permissions",
        "/rename": "Conversation title",
    }.get(name, name.removeprefix("/").replace("-", " ").title() or "Codex command")


def command_summary(
    name: str,
    status: str,
    metadata: Mapping[str, Any] | None = None,
    *,
    error: str = "",
) -> str:
    values = metadata if isinstance(metadata, Mapping) else {}
    if status == "failed":
        # Arbitrary protocol/terminal errors may contain paths or other private
        # context.  The durable row records only the typed outcome; the live
        # HTTP error remains available to the initiating browser request.
        return f"{command_label(name)} failed"
    if status == "running":
        return {
            "/compact": "Compacting context…",
            "/fast": "Changing response speed…",
            "/goal": "Updating goal…",
            "/model": "Opening model selection…",
            "/permissions": "Opening permissions selection…",
            "/rename": "Renaming conversation…",
        }.get(name, f"Running {name}…")
    if status == "waiting":
        return {
            "/model": "Choose a model",
            "/permissions": "Choose a permissions profile",
        }.get(name, f"{command_label(name)} is waiting for input")
    if values.get("action") == "cancel":
        return f"{command_label(name)} selection cancelled"
    if name == "/rename":
        title = str(values.get("title") or "").strip()
        return f"Renamed conversation to “{title}”" if title else "Renamed conversation"
    if name == "/model":
        selection = str(values.get("selection") or "").strip()
        return f"Switched model to {selection}" if selection else "Model selection completed"
    if name == "/permissions":
        selection = str(values.get("selection") or "").strip()
        return f"Permissions set to {selection}" if selection else "Permissions selection completed"
    if name == "/fast":
        enabled = values.get("enabled")
        if isinstance(enabled, bool):
            return "Fast responses enabled" if enabled else "Fast responses disabled"
        return "Response speed updated"
    if name == "/compact":
        return "Context compaction requested"
    if name == "/goal":
        action = str(values.get("action") or "").strip()
        return f"Goal {action} requested" if action else "Goal updated"
    return f"Completed {name}"


def _clean_metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    if isinstance(value.get("title"), str):
        result["title"] = str(value["title"])[:MAX_TITLE_CHARS]
    if isinstance(value.get("selection"), str) and _safe_selection(value["selection"]):
        result["selection"] = str(value["selection"])
    if value.get("action") in {"clear", "pause", "resume", "cancel"}:
        result["action"] = str(value["action"])
    if isinstance(value.get("enabled"), bool):
        result["enabled"] = bool(value["enabled"])
    return result


def _valid_owner_key(value: object) -> str:
    key = str(value or "").strip()
    if not key or len(key) > 320 or "\x00" in key:
        raise CommandTimelineError("invalid command timeline owner")
    return key


def _valid_event(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    event_id = str(value.get("id") or "")
    owner_key = str(value.get("ownerKey") or "")
    request_id = str(value.get("requestId") or "")
    digest = str(value.get("digest") or "")
    name = str(value.get("name") or "")
    status = str(value.get("status") or "")
    if (
        not re.fullmatch(r"cmd_[A-Za-z0-9_-]{16,80}", event_id)
        or not owner_key
        or len(owner_key) > 320
        or not REQUEST_ID_RE.fullmatch(request_id)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or not COMMAND_RE.fullmatch(name)
        or status not in COMMAND_STATUSES
    ):
        return None
    try:
        started_at = max(0, int(value.get("startedAt") or 0))
        completed_at = max(0, int(value.get("completedAt") or 0))
    except (TypeError, ValueError):
        return None
    return {
        "id": event_id,
        "ownerKey": owner_key,
        "requestId": request_id,
        "digest": digest,
        "name": name,
        "label": command_label(name),
        "status": status,
        "summary": str(value.get("summary") or "")[:MAX_SUMMARY_CHARS],
        "anchorKey": str(value.get("anchorKey") or "")[:160],
        "interactionId": str(value.get("interactionId") or "")[:160],
        "metadata": _clean_metadata(value.get("metadata")),
        "startedAt": started_at,
        "completedAt": completed_at,
    }


class CommandTimelineStore:
    def __init__(
        self,
        path: Path,
        *,
        max_events: int = MAX_EVENTS,
        max_events_per_owner: int = MAX_EVENTS_PER_OWNER,
        clock: Any = time.time,
    ) -> None:
        self.path = path
        self.max_events = max(1, int(max_events))
        self.max_events_per_owner = max(1, int(max_events_per_owner))
        self.clock = clock
        self.lock = threading.RLock()
        self.events: list[dict[str, Any]] = []
        self.load_errors = 0
        self._load()

    def _load(self) -> None:
        with self.lock:
            try:
                stat = self.path.lstat()
                if self.path.is_symlink() or stat.st_size > MAX_FILE_BYTES:
                    raise OSError
                value = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return
            except (OSError, json.JSONDecodeError):
                self.load_errors += 1
                return
            if not isinstance(value, Mapping) or value.get("schemaVersion") != SCHEMA_VERSION:
                self.load_errors += 1
                return
            self.events = [event for item in value.get("events") or [] if (event := _valid_event(item)) is not None]
            self._trim()
            changed = False
            timestamp = int(self.clock() * 1000)
            for event in self.events:
                if event["status"] in {"running", "waiting"}:
                    event["status"] = "failed"
                    event["summary"] = f"{event['label']} interrupted by service restart"
                    event["completedAt"] = timestamp
                    event["interactionId"] = ""
                    changed = True
            if changed:
                self._save()

    def _trim(self) -> None:
        counts: dict[str, int] = {}
        selected: list[dict[str, Any]] = []
        for event in reversed(self.events):
            owner = str(event["ownerKey"])
            count = counts.get(owner, 0)
            if count >= self.max_events_per_owner:
                continue
            counts[owner] = count + 1
            selected.append(event)
            if len(selected) >= self.max_events:
                break
        self.events = list(reversed(selected))

    def _save(self) -> None:
        body = json.dumps(
            {"schemaVersion": SCHEMA_VERSION, "events": self.events},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n"
        if len(body.encode("utf-8")) > MAX_FILE_BYTES:
            self.events = self.events[-max(1, self.max_events // 2):]
            body = json.dumps(
                {"schemaVersion": SCHEMA_VERSION, "events": self.events},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
            self._fsync_parent()
        finally:
            temporary.unlink(missing_ok=True)

    def _fsync_parent(self) -> None:
        try:
            descriptor = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def begin(
        self,
        *,
        owner_key: object,
        request_id: object,
        invocation: object,
        anchor_key: str = "",
    ) -> tuple[dict[str, Any] | None, bool]:
        parts = command_parts(invocation)
        clean_request_id = str(request_id or "").strip()
        if parts is None or not REQUEST_ID_RE.fullmatch(clean_request_id):
            raise CommandTimelineError("invalid command timeline event")
        if not command_is_durable(invocation):
            return None, False
        key = _valid_owner_key(owner_key)
        name, argument = parts
        digest = hashlib.sha256(str(invocation).strip().encode("utf-8")).hexdigest()
        with self.lock:
            existing = next(
                (
                    event
                    for event in reversed(self.events)
                    if event["ownerKey"] == key and event["requestId"] == clean_request_id
                ),
                None,
            )
            if existing is not None:
                if existing["digest"] != digest:
                    raise CommandTimelineError("client request id was already used for another command")
                return dict(existing), True
            metadata = _safe_argument(name, argument)
            timestamp = int(self.clock() * 1000)
            event = {
                "id": f"cmd_{secrets.token_urlsafe(18)}",
                "ownerKey": key,
                "requestId": clean_request_id,
                "digest": digest,
                "name": name,
                "label": command_label(name),
                "status": "running",
                "summary": command_summary(name, "running", metadata),
                "anchorKey": str(anchor_key or "")[:160],
                "interactionId": "",
                "metadata": metadata,
                "startedAt": timestamp,
                "completedAt": 0,
            }
            self.events.append(event)
            self._trim()
            self._save()
            return dict(event), False

    def update(
        self,
        event_id: str,
        *,
        status: str,
        metadata: Mapping[str, Any] | None = None,
        interaction_id: str | None = None,
        error: str = "",
    ) -> dict[str, Any] | None:
        if status not in COMMAND_STATUSES:
            raise CommandTimelineError("invalid command timeline status")
        with self.lock:
            event = next((item for item in reversed(self.events) if item["id"] == event_id), None)
            if event is None:
                return None
            combined = {**event.get("metadata", {}), **_clean_metadata(metadata)}
            event["metadata"] = combined
            event["status"] = status
            event["summary"] = command_summary(event["name"], status, combined, error=error)[:MAX_SUMMARY_CHARS]
            if interaction_id is not None:
                event["interactionId"] = str(interaction_id)[:160]
            if status in {"completed", "failed"}:
                event["completedAt"] = int(self.clock() * 1000)
                event["interactionId"] = ""
            self._save()
            return dict(event)

    def event_for_interaction(self, owner_key: object, interaction_id: str) -> dict[str, Any] | None:
        key = _valid_owner_key(owner_key)
        with self.lock:
            event = next(
                (
                    item
                    for item in reversed(self.events)
                    if item["ownerKey"] == key and item.get("interactionId") == interaction_id
                ),
                None,
            )
            return dict(event) if event is not None else None

    def public_events(self, owner_key: object) -> list[dict[str, Any]]:
        key = _valid_owner_key(owner_key)
        with self.lock:
            selected = [dict(item) for item in self.events if item["ownerKey"] == key]
        return [self.public_event(item) for item in selected]

    @staticmethod
    def public_event(event: Mapping[str, Any]) -> dict[str, Any]:
        status = str(event.get("status") or "failed")
        return {
            "id": str(event.get("id") or ""),
            "kind": "command",
            "name": str(event.get("name") or ""),
            "label": str(event.get("label") or "Codex command"),
            "summary": str(event.get("summary") or "")[:MAX_SUMMARY_CHARS],
            "status": status,
            "anchorKey": str(event.get("anchorKey") or ""),
            "startedAt": max(0, int(event.get("startedAt") or 0)),
            "completedAt": max(0, int(event.get("completedAt") or 0)),
            "final": status in {"completed", "failed"},
        }
