"""Durable App Server activity recovered from Codex rollout JSONL.

Codex's ``thread/read`` response intentionally omits some completed command
items after a reconnect.  The rollout remains the durable source for those
items.  This module keeps a small offset index and projects only public,
bounded activity summaries; command output and reasoning bodies are never
returned.
"""

from __future__ import annotations

from collections import OrderedDict
import json
from pathlib import Path
import re
import threading
from typing import Any, Iterable, Mapping

from appserver_session import message_block


ACTIVITY_INDEX_MAX_PATHS = 16
_SPECIALIZED_TOOL_RE = re.compile(r"\btools\.([A-Za-z0-9_]+)\s*\(")
_SPECIALIZED_ONLY = frozenset({"apply_patch", "web__run"})

_index_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
_index_locks: dict[str, threading.Lock] = {}
_cache_lock = threading.Lock()


def _event_payload(event: Any) -> Mapping[str, Any] | None:
    if not isinstance(event, Mapping):
        return None
    payload = event.get("payload")
    return payload if isinstance(payload, Mapping) else None


def _custom_turn_id(payload: Mapping[str, Any], active_turn_id: str) -> str:
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    if isinstance(metadata, Mapping):
        turn_id = str(metadata.get("turn_id") or "").strip()
        if turn_id:
            return turn_id
    return active_turn_id


def _activity_turn_id(event: Mapping[str, Any], active_turn_id: str) -> str:
    payload = _event_payload(event)
    if payload is None:
        return ""
    outer_type = str(event.get("type") or "")
    payload_type = str(payload.get("type") or "")
    if outer_type == "response_item" and payload_type == "custom_tool_call":
        return _custom_turn_id(payload, active_turn_id)
    if outer_type == "event_msg" and payload_type in {"patch_apply_end", "web_search_end"}:
        return str(payload.get("turn_id") or active_turn_id).strip()
    return ""


def _next_active_turn(event: Mapping[str, Any], active_turn_id: str) -> str:
    payload = _event_payload(event)
    if payload is None or event.get("type") != "event_msg":
        return active_turn_id
    payload_type = str(payload.get("type") or "")
    turn_id = str(payload.get("turn_id") or "").strip()
    if payload_type == "task_started" and turn_id:
        return turn_id
    if payload_type in {"task_complete", "turn_aborted"} and (not turn_id or turn_id == active_turn_id):
        return ""
    return active_turn_id


def _empty_state(identity: tuple[int, int]) -> dict[str, Any]:
    return {
        "identity": identity,
        "offset": 0,
        "activeTurnId": "",
        "records": {},
    }


def _cache_state(key: str, state: dict[str, Any]) -> None:
    with _cache_lock:
        _index_cache.pop(key, None)
        _index_cache[key] = state
        while len(_index_cache) > ACTIVITY_INDEX_MAX_PATHS:
            _index_cache.popitem(last=False)


def _cached_state(key: str) -> dict[str, Any] | None:
    with _cache_lock:
        state = _index_cache.pop(key, None)
        if state is not None:
            _index_cache[key] = state
        return state


def _path_lock(key: str) -> threading.Lock:
    with _cache_lock:
        return _index_locks.setdefault(key, threading.Lock())


def _update_index(path: Path) -> dict[str, Any] | None:
    key = str(path)
    with _path_lock(key):
        state = _cached_state(key)
        try:
            stat = path.stat()
        except OSError:
            return state
        identity = (int(stat.st_dev), int(stat.st_ino))
        offset = int(state.get("offset") or 0) if state else 0
        if state is None or state.get("identity") != identity or stat.st_size < offset:
            state = _empty_state(identity)
            offset = 0
        if stat.st_size <= offset:
            _cache_state(key, state)
            return state

        active_turn_id = str(state.get("activeTurnId") or "")
        records = state.setdefault("records", {})
        complete_offset = offset
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                while handle.tell() < stat.st_size:
                    start = handle.tell()
                    raw_line = handle.readline(stat.st_size - start)
                    if not raw_line.endswith(b"\n"):
                        break
                    complete_offset = handle.tell()
                    try:
                        event = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(event, Mapping):
                        continue
                    active_turn_id = _next_active_turn(event, active_turn_id)
                    turn_id = _activity_turn_id(event, active_turn_id)
                    if turn_id:
                        records.setdefault(turn_id, []).append((start, complete_offset))
        except OSError:
            return state
        state["offset"] = complete_offset
        state["activeTurnId"] = active_turn_id
        _cache_state(key, state)
        return state


def _custom_tool_block(payload: Mapping[str, Any], turn_id: str) -> dict[str, Any] | None:
    name = str(payload.get("name") or "").strip()
    source = str(payload.get("input") or "").strip()
    item_id = str(payload.get("id") or payload.get("call_id") or "").strip()
    if not item_id or not turn_id:
        return None
    if name == "exec":
        specialized = set(_SPECIALIZED_TOOL_RE.findall(source))
        if specialized and specialized.issubset(_SPECIALIZED_ONLY):
            # Richer, stable web-search and patch-completion events are indexed
            # separately, using the same public ids as thread/read.
            return None
        item = {
            "id": item_id,
            "type": "commandExecution",
            "command": source or "command",
            "status": str(payload.get("status") or "completed"),
        }
    else:
        item = {
            "id": item_id,
            "type": "dynamicToolCall",
            "namespace": "tool",
            "tool": name or "call",
            "status": str(payload.get("status") or "completed"),
        }
    return message_block(item, item_id=item_id, turn_id=turn_id, final=True)


def _patch_block(payload: Mapping[str, Any], turn_id: str) -> dict[str, Any] | None:
    item_id = str(payload.get("call_id") or "").strip()
    changes_value = payload.get("changes")
    changes: list[dict[str, str]] = []
    if isinstance(changes_value, Mapping):
        changes = [{"path": str(path)} for path in changes_value if str(path).strip()]
    elif isinstance(changes_value, list):
        changes = [
            {"path": str(change.get("path") or "")}
            for change in changes_value
            if isinstance(change, Mapping) and str(change.get("path") or "").strip()
        ]
    item = {
        "id": item_id,
        "type": "fileChange",
        "changes": changes,
        "status": "completed" if payload.get("success") is not False else "failed",
    }
    return message_block(item, item_id=item_id, turn_id=turn_id, final=True)


def _web_search_block(payload: Mapping[str, Any], turn_id: str) -> dict[str, Any] | None:
    item_id = str(payload.get("call_id") or "").strip()
    item = {
        "id": item_id,
        "type": "webSearch",
        "query": str(payload.get("query") or ""),
        "status": "completed",
    }
    return message_block(item, item_id=item_id, turn_id=turn_id, final=True)


def _project_record(raw_line: bytes, expected_turn_id: str) -> dict[str, Any] | None:
    try:
        event = json.loads(raw_line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    payload = _event_payload(event)
    if payload is None:
        return None
    outer_type = str(event.get("type") or "")
    payload_type = str(payload.get("type") or "")
    if outer_type == "response_item" and payload_type == "custom_tool_call":
        turn_id = _custom_turn_id(payload, expected_turn_id)
        return _custom_tool_block(payload, turn_id) if turn_id == expected_turn_id else None
    if outer_type == "event_msg" and payload_type == "patch_apply_end":
        turn_id = str(payload.get("turn_id") or expected_turn_id).strip()
        return _patch_block(payload, turn_id) if turn_id == expected_turn_id else None
    if outer_type == "event_msg" and payload_type == "web_search_end":
        return _web_search_block(payload, expected_turn_id)
    return None


def activity_blocks(history_path: str | None, turn_ids: Iterable[str]) -> list[dict[str, Any]]:
    """Return chronological, bounded activity blocks for selected App Server turns."""

    if not history_path:
        return []
    selected = [str(turn_id).strip() for turn_id in turn_ids if str(turn_id).strip()]
    if not selected:
        return []
    path = Path(history_path).expanduser()
    state = _update_index(path)
    if state is None:
        return []
    records = state.get("records")
    if not isinstance(records, Mapping):
        return []
    blocks: list[dict[str, Any]] = []
    try:
        with path.open("rb") as handle:
            for turn_id in selected:
                for start, end in records.get(turn_id) or []:
                    handle.seek(int(start))
                    raw_line = handle.read(max(0, int(end) - int(start))).rstrip(b"\n")
                    block = _project_record(raw_line, turn_id)
                    if block is not None:
                        blocks.append(block)
    except (OSError, TypeError, ValueError):
        return []
    return blocks


def clear_activity_index_cache() -> None:
    """Test hook for deterministic inode/truncation coverage."""

    with _cache_lock:
        _index_cache.clear()
        _index_locks.clear()
