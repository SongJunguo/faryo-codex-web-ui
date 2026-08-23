"""Deterministic projection of Codex thread notifications for the browser."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping

from appserver_protocol import agent_message_text, item_identity


HANDLED_NOTIFICATION_METHODS = frozenset({
    "error",
    "item/agentMessage/delta",
    "item/commandExecution/outputDelta",
    "item/completed",
    "item/fileChange/outputDelta",
    "item/plan/delta",
    "item/reasoning/summaryTextDelta",
    "item/started",
    "thread/goal/cleared",
    "thread/goal/updated",
    "thread/name/updated",
    "thread/started",
    "thread/status/changed",
    "thread/settings/updated",
    "thread/tokenUsage/updated",
    "turn/completed",
    "turn/error",
    "turn/started",
})

ACTIVITY_DETAIL_TEXT_CHARS = 96 * 1024
ACTIVITY_DETAIL_TOTAL_CHARS = 192 * 1024
ACTIVITY_CHANGE_LIMIT = 120


def _bounded_text(value: Any, limit: int = 2_000) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _path_label(value: Any) -> str:
    text = str(value or "").replace("\\", "/").rstrip("/")
    return text.rsplit("/", 1)[-1] if text else "file"


def _activity_status(item: Mapping[str, Any], *, final: bool = False) -> str:
    value = str(item.get("status") or "").strip().lower().replace("_", "")
    exit_code = item.get("exitCode", item.get("exit_code"))
    if item.get("type") == "commandExecution" and isinstance(exit_code, int) and exit_code != 0:
        return "failed"
    if value in {"failed", "error", "cancelled", "canceled"}:
        return "failed"
    if value in {"declined", "denied"}:
        return "declined"
    if value in {"pending", "waiting", "needsapproval", "awaitingapproval"}:
        return "waiting"
    if value in {"completed", "complete", "succeeded", "success"} or final:
        return "completed"
    return "running"


def activity_projection(item: Mapping[str, Any], *, final: bool = False) -> dict[str, Any] | None:
    """Return the small typed activity envelope allowed in history payloads."""

    item_type = str(item.get("type") or "")
    status = _activity_status(item, final=final)
    result: dict[str, Any]
    if item_type == "commandExecution":
        command = _bounded_text(item.get("command"), 520) or "Command"
        result = {
            "type": "command",
            "title": command,
            "summary": "Command finished" if status == "completed" else "Command failed" if status == "failed" else "Command running",
            "detailKind": "command_output",
            "detailAvailable": any(
                key in item for key in ("aggregatedOutput", "stdout", "stderr", "command", "cwd", "exitCode")
            ),
        }
        exit_code = item.get("exitCode", item.get("exit_code"))
        if isinstance(exit_code, int):
            result["exitCode"] = exit_code
        duration = item.get("durationMs", item.get("duration_ms"))
        if isinstance(duration, (int, float)) and duration >= 0:
            result["durationMs"] = int(duration)
    elif item_type == "fileChange":
        changes = [value for value in item.get("changes") or [] if isinstance(value, Mapping)]
        labels = [_path_label(change.get("path")) for change in changes[:3]]
        title = ", ".join(labels) or "Files"
        if len(changes) > len(labels):
            title += f" and {len(changes) - len(labels)} more"
        result = {
            "type": "file_change",
            "title": title,
            "summary": f"{len(changes)} file change{'s' if len(changes) != 1 else ''}",
            "changeCount": len(changes),
            "detailKind": "file_changes",
            "detailAvailable": bool(changes),
        }
    elif item_type == "webSearch":
        query = _bounded_text(item.get("query"), 360) or "Web search"
        result = {
            "type": "search",
            "title": query,
            "summary": "Search finished" if status == "completed" else "Searching",
            "detailKind": "search",
            "detailAvailable": bool(item.get("results")),
        }
    elif item_type in {"mcpToolCall", "dynamicToolCall"}:
        if item_type == "mcpToolCall":
            label = ".".join(
                part for part in (_bounded_text(item.get("server"), 100), _bounded_text(item.get("tool"), 140)) if part
            )
            activity_type = "mcp"
        else:
            label = ".".join(
                part
                for part in (_bounded_text(item.get("namespace"), 100), _bounded_text(item.get("tool"), 140))
                if part
            )
            activity_type = "tool"
        result = {
            "type": activity_type,
            "title": label or "Tool call",
            "summary": "Tool finished" if status == "completed" else "Tool failed" if status == "failed" else "Tool running",
            "detailKind": "tool_call",
            "detailAvailable": any(
                key in item for key in ("arguments", "input", "result", "output", "error", "content")
            ),
        }
    elif item_type == "imageView":
        result = {
            "type": "image",
            "title": _path_label(item.get("path")),
            "summary": "Image viewed",
            "detailKind": "none",
            "detailAvailable": False,
        }
    elif item_type == "contextCompaction":
        result = {
            "type": "compaction",
            "title": "Context compacted",
            "summary": "Context compacted",
            "detailKind": "none",
            "detailAvailable": False,
        }
    elif item_type == "error":
        result = {
            "type": "error",
            "title": _bounded_text(item.get("message"), 520) or "Codex error",
            "summary": "Codex error",
            "detailKind": "error",
            "detailAvailable": bool(item.get("message")),
        }
        status = "failed"
    elif item_type in {"plan", "todoList", "reasoning", "userMessage", "agentMessage"}:
        return None
    else:
        result = {
            "type": "unknown",
            "title": _bounded_text(item_type, 180) or "Codex activity",
            "summary": "Codex activity",
            "detailKind": "none",
            "detailAvailable": False,
        }
    result["status"] = status
    return result


def _detail_text(value: Any, limit: int = ACTIVITY_DETAIL_TEXT_CHARS) -> tuple[str, bool]:
    if value is None:
        return "", False
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        except (TypeError, ValueError):
            text = str(value)
    if limit <= 0:
        return "", bool(text)
    if len(text) <= limit:
        return text, False
    head = max(1, limit * 3 // 4)
    tail = max(1, limit - head)
    return f"{text[:head]}\n\n… detail truncated …\n\n{text[-tail:]}", True


def activity_detail(item: Mapping[str, Any], *, final: bool = False) -> dict[str, Any] | None:
    """Project one authenticated detail view with per-field and total bounds."""

    activity = activity_projection(
        item,
        final=final or _activity_status(item) in {"completed", "failed", "declined"},
    )
    if activity is None or activity.get("type") in {"unknown", "compaction", "image"}:
        return None
    detail: dict[str, Any] = {
        "type": activity["type"],
        "status": activity["status"],
        "title": activity["title"],
        "truncated": False,
    }
    item_type = str(item.get("type") or "")
    if item_type == "commandExecution":
        command, command_truncated = _detail_text(item.get("command"), 16 * 1024)
        output_value = item.get("aggregatedOutput")
        if output_value is None:
            stdout = str(item.get("stdout") or "")
            stderr = str(item.get("stderr") or "")
            output_value = stdout + (("\n" if stdout and stderr else "") + stderr)
        output, output_truncated = _detail_text(output_value)
        cwd, cwd_truncated = _detail_text(item.get("cwd"), 4096)
        detail.update({"command": command, "output": output, "cwd": cwd})
        for source, target in (("exitCode", "exitCode"), ("exit_code", "exitCode"), ("durationMs", "durationMs"), ("duration_ms", "durationMs")):
            if target not in detail and isinstance(item.get(source), (int, float)):
                detail[target] = int(item[source])
        detail["truncated"] = command_truncated or output_truncated or cwd_truncated
    elif item_type == "fileChange":
        changes: list[dict[str, Any]] = []
        used_chars = 0
        for raw in item.get("changes") or []:
            if not isinstance(raw, Mapping) or len(changes) >= ACTIVITY_CHANGE_LIMIT:
                detail["truncated"] = True
                break
            path, path_truncated = _detail_text(raw.get("path"), 4096)
            diff, diff_truncated = _detail_text(
                raw.get("diff", raw.get("unified_diff", raw.get("content"))),
                min(ACTIVITY_DETAIL_TEXT_CHARS, max(0, ACTIVITY_DETAIL_TOTAL_CHARS - used_chars)),
            )
            used_chars += len(path) + len(diff)
            changes.append({
                "path": path,
                "kind": _bounded_text(raw.get("kind", raw.get("type")), 80),
                "diff": diff,
                "truncated": path_truncated or diff_truncated,
            })
            if used_chars >= ACTIVITY_DETAIL_TOTAL_CHARS:
                detail["truncated"] = True
                break
        detail["changes"] = changes
        detail["truncated"] = bool(detail["truncated"] or any(change["truncated"] for change in changes))
    elif item_type == "webSearch":
        query, query_truncated = _detail_text(item.get("query"), 4096)
        results, results_truncated = _detail_text(item.get("results"))
        detail.update({"query": query, "results": results})
        detail["truncated"] = query_truncated or results_truncated
    else:
        arguments = item.get("arguments", item.get("input"))
        result_value = item.get("result", item.get("output", item.get("content")))
        error_value = item.get("error")
        arguments_text, arguments_truncated = _detail_text(arguments)
        result_text, result_truncated = _detail_text(result_value)
        error_text, error_truncated = _detail_text(error_value, 32 * 1024)
        detail.update({"arguments": arguments_text, "result": result_text, "error": error_text})
        detail["truncated"] = arguments_truncated or result_truncated or error_truncated
    return detail


def item_process_text(item: Mapping[str, Any], *, final: bool = False) -> str:
    item_type = str(item.get("type") or "")
    status = str(item.get("status") or "")
    completed = final or status.lower() in {"completed", "failed", "declined"}
    if item_type == "plan":
        text = str(item.get("text") or "").strip()
        return f"Updated Plan\n{text}" if text else "Updated Plan"
    if item_type == "reasoning":
        # Reasoning bodies are intentionally private, and an empty "Working"
        # block for every reasoning item only duplicates the single live turn
        # status already projected by the browser.
        return ""
    if item_type == "commandExecution":
        command = _bounded_text(item.get("command")) or "command"
        exit_code = item.get("exitCode", item.get("exit_code"))
        suffix = f" · exit {exit_code}" if completed and isinstance(exit_code, int) else ""
        return f"{'Ran' if completed else 'Running'} {command}{suffix}"
    if item_type == "fileChange":
        changes = item.get("changes") if isinstance(item.get("changes"), list) else []
        paths = [_path_label(change.get("path")) for change in changes if isinstance(change, Mapping)]
        if not paths:
            return "Edited files" if completed else "Editing files"
        prefix = "Edited" if completed else "Editing"
        return "\n".join(f"{prefix} {path}" for path in paths[:8])
    if item_type == "mcpToolCall":
        label = ".".join(
            part for part in (_bounded_text(item.get("server"), 120), _bounded_text(item.get("tool"), 120)) if part
        ) or "MCP tool"
        return f"Called {label}" if completed else f"Calling {label}"
    if item_type == "dynamicToolCall":
        label = ".".join(
            part
            for part in (_bounded_text(item.get("namespace"), 120), _bounded_text(item.get("tool"), 120))
            if part
        ) or "tool"
        return f"Called {label}" if completed else f"Calling {label}"
    if item_type == "webSearch":
        query = _bounded_text(item.get("query"), 240)
        return f"{'Searched' if completed else 'Searching the web'} {query}".rstrip()
    if item_type == "todoList":
        rows = []
        for entry in item.get("items") or []:
            if not isinstance(entry, Mapping):
                continue
            marker = "✓" if entry.get("completed") else "□"
            text = _bounded_text(entry.get("text"), 400)
            if text:
                rows.append(f"{marker} {text}")
        return "Updated Plan" + ("\n" + "\n".join(rows) if rows else "")
    if item_type == "contextCompaction":
        return "Context compacted"
    if item_type == "imageView":
        return f"Viewed Image {_path_label(item.get('path'))}"
    if item_type == "subAgentActivity":
        return "Working with a background agent"
    if item_type == "error":
        return "⚠ " + (_bounded_text(item.get("message"), 800) or "Codex error")
    return f"Codex activity · {_bounded_text(item_type, 180)}" if item_type else ""


def user_message_text(item: Mapping[str, Any]) -> str:
    if item.get("type") != "userMessage":
        return ""
    parts: list[str] = []
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    for value in content:
        if not isinstance(value, Mapping):
            continue
        if value.get("type") == "text" and isinstance(value.get("text"), str):
            parts.append(str(value["text"]))
        elif value.get("type") in {"image", "localImage"}:
            parts.append("[Image attached]")
    return "\n".join(parts)


def browser_item_key(item_id: str) -> str:
    return "appserver-item-" + hashlib.sha256(item_id.encode("utf-8")).hexdigest()[:16]


def browser_turn_key(turn_id: str) -> str:
    return "appserver-turn-" + hashlib.sha256(turn_id.encode("utf-8")).hexdigest()[:16]


def browser_question_key(item_id: str) -> str:
    """Return a privacy-safe browser identity for one user message."""

    return "appserver-question-" + hashlib.sha256(item_id.encode("utf-8")).hexdigest()[:16]


def message_block(
    item: Mapping[str, Any],
    *,
    item_id: str,
    turn_id: str,
    segment_key: str = "",
    text_override: str | None = None,
    revision: int = 0,
    final: bool = True,
) -> dict[str, Any] | None:
    """Project one App Server item without exposing raw protocol identity."""

    item_type = str(item.get("type") or "")
    if item_type == "userMessage":
        kind = "user"
        role = "user"
        text = user_message_text(item) if text_override is None else text_override
    elif item_type == "agentMessage":
        kind = "output"
        role = "assistant"
        text = agent_message_text(item) if text_override is None else text_override
    else:
        kind = "plan" if item_type in {"plan", "todoList"} else "process"
        role = "process"
        text = item_process_text(item, final=final) if text_override is None else text_override
    text = str(text or "").strip()
    if not item_id or not turn_id or not text:
        return None
    turn_key = browser_turn_key(turn_id)
    resolved_segment_key = str(segment_key or "").strip()
    if kind == "user":
        resolved_segment_key = browser_question_key(item_id)
    if not resolved_segment_key:
        resolved_segment_key = turn_key
    block: dict[str, Any] = {
        "id": browser_item_key(item_id),
        "turnKey": turn_key,
        "segmentKey": resolved_segment_key,
        "kind": kind,
        "role": role,
        "text": text,
        "revision": max(0, int(revision)),
        "final": bool(final),
    }
    if kind == "user":
        block["questionKey"] = resolved_segment_key
    elif kind == "process":
        activity = activity_projection(item, final=final)
        if activity is not None:
            block["activity"] = activity
    return block


@dataclass
class ItemProjection:
    id: str
    turn_id: str
    type: str
    text: str = ""
    revision: int = 0
    final: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "turnId": self.turn_id,
            "type": self.type,
            "text": self.text,
            "revision": self.revision,
            "final": self.final,
            "item": self.raw,
        }


@dataclass(frozen=True)
class ActorEvent:
    kind: str
    turn_id: str | None = None
    item_id: str | None = None
    revision: int = 0
    payload: dict[str, Any] = field(default_factory=dict)


class WebSessionActor:
    def __init__(self, *, session_id: str, thread_id: str) -> None:
        if not session_id or not thread_id:
            raise ValueError("session_id and thread_id are required")
        self.session_id = session_id
        self.thread_id = thread_id
        self.lifecycle = "loading"
        self.active_turn_id: str | None = None
        self.turns: dict[str, dict[str, Any]] = {}
        self.item_order: list[str] = []
        self.items: dict[str, ItemProjection] = {}
        self.token_usage: dict[str, Any] = {}
        self.goal: dict[str, Any] | None = None
        self.interaction: dict[str, Any] | None = None
        self.interaction_revision = 0
        self.durable_activity_required = False
        self.thread: dict[str, Any] = {"id": thread_id}
        self.revision = 0

    def apply(self, method: str, params: Mapping[str, Any]) -> list[ActorEvent]:
        event_thread_id = params.get("threadId")
        if isinstance(event_thread_id, str) and event_thread_id != self.thread_id:
            return []
        if method == "thread/started":
            thread = params.get("thread")
            if isinstance(thread, Mapping) and thread.get("id") == self.thread_id:
                self.thread = dict(thread)
                self.lifecycle = self._thread_status(thread.get("status"))
                return [self._event("session.snapshot", payload=self.snapshot())]
            return []
        if method == "thread/status/changed":
            self.lifecycle = self._thread_status(params.get("status"))
            return [self._event("session.lifecycle", payload={"lifecycle": self.lifecycle})]
        if method == "thread/name/updated":
            name = params.get("threadName")
            self.thread["name"] = name if isinstance(name, str) else None
            return [self._event("session.title", payload={"title": self.thread.get("name")})]
        if method == "thread/settings/updated":
            settings = params.get("threadSettings")
            if isinstance(settings, Mapping):
                self.thread.update(dict(settings))
                return [self._event("session.settings", payload={"threadSettings": dict(settings)})]
            return []
        if method == "turn/started":
            turn = params.get("turn")
            if not isinstance(turn, Mapping) or not isinstance(turn.get("id"), str):
                return []
            turn_id = str(turn["id"])
            self.turns[turn_id] = dict(turn)
            self.active_turn_id = turn_id
            self.lifecycle = "running"
            return [self._event("turn.started", turn_id=turn_id, payload={"turn": dict(turn)})]
        if method == "item/started":
            return self._item_started(params)
        if method == "item/agentMessage/delta":
            return self._agent_delta(params)
        if method == "item/plan/delta":
            return self._plan_delta(params)
        if method == "item/commandExecution/outputDelta":
            return self._activity_delta(params, "aggregatedOutput")
        if method == "item/fileChange/outputDelta":
            return self._activity_delta(params, "output")
        if method == "item/reasoning/summaryTextDelta":
            return []
        if method == "item/completed":
            return self._item_completed(params)
        if method == "turn/completed":
            return self._turn_completed(params)
        if method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage")
            if isinstance(usage, Mapping):
                self.token_usage = dict(usage)
                return [self._event("session.usage", turn_id=self._turn_id(params), payload={"tokenUsage": self.token_usage})]
            return []
        if method == "thread/goal/updated":
            goal = params.get("goal")
            if isinstance(goal, Mapping):
                self.goal = dict(goal)
                return [self._event("session.goal", turn_id=self._turn_id(params), payload={"goal": self.goal})]
            return []
        if method == "thread/goal/cleared":
            self.goal = None
            return [self._event("session.goal", turn_id=self._turn_id(params), payload={"goal": None})]
        if method in {"error", "turn/error"}:
            return [self._event("session.error", turn_id=self._turn_id(params), payload={"error": dict(params)})]
        return []

    def _item_started(self, params: Mapping[str, Any]) -> list[ActorEvent]:
        identity = item_identity(params)
        item = params.get("item")
        if identity is None or not isinstance(item, Mapping):
            return []
        _thread_id, turn_id, item_id = identity
        projection = self.items.get(item_id)
        if projection is None:
            projection = ItemProjection(
                id=item_id,
                turn_id=turn_id,
                type=str(item.get("type") or "unknown"),
                text=agent_message_text(item) or item_process_text(item),
                raw=dict(item),
            )
            self.items[item_id] = projection
            self.item_order.append(item_id)
        else:
            projection.raw = dict(item)
            projection.text = agent_message_text(item) or item_process_text(item)
        return [self._event("item.started", turn_id=turn_id, item_id=item_id, payload={"item": projection.public()})]

    def _agent_delta(self, params: Mapping[str, Any]) -> list[ActorEvent]:
        identity = item_identity(params)
        delta = params.get("delta")
        if identity is None or not isinstance(delta, str):
            return []
        _thread_id, turn_id, item_id = identity
        projection = self.items.get(item_id)
        if projection is None:
            projection = ItemProjection(id=item_id, turn_id=turn_id, type="agentMessage")
            self.items[item_id] = projection
            self.item_order.append(item_id)
        if projection.final:
            return []
        if projection.text == "Updated Plan":
            projection.text += "\n"
        projection.text += delta
        projection.revision += 1
        return [
            self._event(
                "item.delta",
                turn_id=turn_id,
                item_id=item_id,
                revision=projection.revision,
                payload={"deltaChars": len(delta), "textLength": len(projection.text)},
            )
        ]

    def _plan_delta(self, params: Mapping[str, Any]) -> list[ActorEvent]:
        identity = item_identity(params)
        delta = params.get("delta")
        if identity is None or not isinstance(delta, str):
            return []
        _thread_id, turn_id, item_id = identity
        projection = self.items.get(item_id)
        if projection is None:
            projection = ItemProjection(id=item_id, turn_id=turn_id, type="plan", text="Updated Plan\n")
            self.items[item_id] = projection
            self.item_order.append(item_id)
        if projection.final:
            return []
        projection.text += delta
        projection.revision += 1
        return [
            self._event(
                "item.delta",
                turn_id=turn_id,
                item_id=item_id,
                revision=projection.revision,
                payload={"deltaChars": len(delta), "textLength": len(projection.text)},
            )
        ]

    def _activity_delta(self, params: Mapping[str, Any], field_name: str) -> list[ActorEvent]:
        identity = item_identity(params)
        delta = params.get("delta")
        if identity is None or not isinstance(delta, str):
            return []
        _thread_id, turn_id, item_id = identity
        projection = self.items.get(item_id)
        if projection is None or projection.final:
            return []
        current = str(projection.raw.get(field_name) or "")
        # Live detail remains bounded even if a tool streams indefinitely.  A
        # final item may replace this preview with the authoritative output.
        projection.raw[field_name] = (current + delta)[-ACTIVITY_DETAIL_TEXT_CHARS:]
        projection.revision += 1
        return [
            self._event(
                "item.delta",
                turn_id=turn_id,
                item_id=item_id,
                revision=projection.revision,
                payload={"deltaChars": len(delta), "textLength": len(str(projection.raw[field_name]))},
            )
        ]

    def _item_completed(self, params: Mapping[str, Any]) -> list[ActorEvent]:
        identity = item_identity(params)
        item = params.get("item")
        if identity is None or not isinstance(item, Mapping):
            return []
        _thread_id, turn_id, item_id = identity
        projection = self.items.get(item_id)
        if projection is None:
            projection = ItemProjection(id=item_id, turn_id=turn_id, type=str(item.get("type") or "unknown"))
            self.items[item_id] = projection
            self.item_order.append(item_id)
        final_text = agent_message_text(item) or item_process_text(item, final=True)
        changed = not projection.final or projection.raw != dict(item) or projection.text != final_text
        projection.type = str(item.get("type") or projection.type)
        projection.raw = dict(item)
        if final_text:
            projection.text = final_text
        projection.final = True
        if changed:
            projection.revision += 1
        return [
            self._event(
                "item.final",
                turn_id=turn_id,
                item_id=item_id,
                revision=projection.revision,
                payload={"item": projection.public()},
            )
        ] if changed else []

    def _turn_completed(self, params: Mapping[str, Any]) -> list[ActorEvent]:
        turn = params.get("turn")
        if not isinstance(turn, Mapping) or not isinstance(turn.get("id"), str):
            return []
        turn_id = str(turn["id"])
        events: list[ActorEvent] = []
        items = turn.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, Mapping):
                    events.extend(self._item_completed({"threadId": self.thread_id, "turnId": turn_id, "item": item}))
        self.turns[turn_id] = dict(turn)
        if self.active_turn_id == turn_id:
            self.active_turn_id = None
        status = str(turn.get("status") or "completed")
        self.lifecycle = "idle" if status == "completed" else status
        events.append(self._event("turn.completed", turn_id=turn_id, payload={"turn": dict(turn), "lifecycle": self.lifecycle}))
        return events

    def snapshot(self) -> dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "threadId": self.thread_id,
            "lifecycle": self.lifecycle,
            "activeTurnId": self.active_turn_id,
            "thread": self.thread,
            "turns": list(self.turns.values()),
            "items": [self.items[item_id].public() for item_id in self.item_order],
            "tokenUsage": self.token_usage,
            "goal": self.goal,
            "interaction": self.interaction,
            "interactionRevision": f"appserver:{self.interaction_revision}",
            "durableActivityRequired": self.durable_activity_required,
            "revision": self.revision,
        }

    def require_durable_activity(self) -> None:
        """Mark thread/read as potentially incomplete after Owner recovery."""

        self.durable_activity_required = True

    def set_interaction(self, interaction: Mapping[str, Any]) -> ActorEvent:
        self.interaction = dict(interaction)
        self.interaction_revision += 1
        response_kind = str(interaction.get("responseKind") or "choice")
        self.lifecycle = "waiting_for_input" if response_kind == "questions" else "waiting_for_approval"
        return self._event(
            "session.interaction",
            turn_id=self.active_turn_id,
            payload={
                "interaction": self.interaction,
                "interactionRevision": f"appserver:{self.interaction_revision}",
                "lifecycle": self.lifecycle,
            },
        )

    def clear_interaction(self, interaction_id: str) -> ActorEvent | None:
        if self.interaction is None or self.interaction.get("id") != interaction_id:
            return None
        self.interaction = None
        self.interaction_revision += 1
        self.lifecycle = "running" if self.active_turn_id else "idle"
        return self._event(
            "session.interaction",
            turn_id=self.active_turn_id,
            payload={
                "interaction": None,
                "interactionRevision": f"appserver:{self.interaction_revision}",
                "lifecycle": self.lifecycle,
            },
        )

    def message_blocks(self) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        first_question_by_turn: dict[str, str] = {}
        for item_id in self.item_order:
            projection = self.items[item_id]
            if projection.type == "userMessage" and projection.turn_id:
                first_question_by_turn.setdefault(
                    projection.turn_id,
                    browser_question_key(projection.id),
                )
        current_turn_id = ""
        current_segment_key = ""
        for item_id in self.item_order:
            projection = self.items[item_id]
            if projection.turn_id != current_turn_id:
                current_turn_id = projection.turn_id
                current_segment_key = first_question_by_turn.get(current_turn_id, "")
            if projection.type == "userMessage":
                current_segment_key = browser_question_key(projection.id)
            block = message_block(
                projection.raw,
                item_id=projection.id,
                turn_id=projection.turn_id,
                segment_key=current_segment_key,
                text_override=None if projection.type == "userMessage" else projection.text,
                revision=projection.revision,
                final=projection.final,
            )
            if block is not None:
                blocks.append(block)
        return blocks

    def command_anchor_key(self) -> str:
        """Anchor a local command to the exact last visible browser item."""

        blocks = self.message_blocks()
        if blocks:
            return str(blocks[-1].get("id") or "")
        turn_id = self.active_turn_id or next(reversed(self.turns), "")
        return browser_turn_key(turn_id) if turn_id else ""

    def messages(self) -> list[tuple[str, str]]:
        return [(str(block["role"]), str(block["text"])) for block in self.message_blocks()]

    def hydrate(self, thread: Mapping[str, Any], turns: list[Mapping[str, Any]] | None = None) -> None:
        if thread.get("id") == self.thread_id:
            self.thread = dict(thread)
            self.lifecycle = self._thread_status(thread.get("status"))
        source_turns = turns if turns is not None else thread.get("turns")
        if not isinstance(source_turns, list):
            return
        for turn in source_turns:
            if not isinstance(turn, Mapping) or not isinstance(turn.get("id"), str):
                continue
            turn_id = str(turn["id"])
            self.turns[turn_id] = dict(turn)
            items = turn.get("items")
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, Mapping):
                        self._item_completed({"threadId": self.thread_id, "turnId": turn_id, "item": item})
            status = str(turn.get("status") or "")
            if status in {"inProgress", "in_progress", "active"}:
                self.active_turn_id = turn_id
                self.lifecycle = "running"

    def _event(
        self,
        kind: str,
        *,
        turn_id: str | None = None,
        item_id: str | None = None,
        revision: int = 0,
        payload: dict[str, Any] | None = None,
    ) -> ActorEvent:
        self.revision += 1
        return ActorEvent(kind, turn_id, item_id, revision, payload or {})

    @staticmethod
    def _turn_id(params: Mapping[str, Any]) -> str | None:
        value = params.get("turnId")
        return value if isinstance(value, str) else None

    @staticmethod
    def _thread_status(value: Any) -> str:
        if isinstance(value, str):
            return {"notLoaded": "unloaded", "idle": "idle", "systemError": "failed"}.get(value, value)
        if isinstance(value, Mapping):
            return "running" if value.get("type") == "active" or "activeFlags" in value else str(value.get("type") or "loading")
        return "loading"
