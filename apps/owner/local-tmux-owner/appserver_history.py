"""Privacy-safe paged history projected from a live App Server snapshot."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Callable, Mapping

from appserver_session import browser_question_key, message_block
import codex_history


PUBLIC_BLOCK_KINDS = {"user", "output", "process", "plan"}
PUBLIC_ACTIVITY_TYPES = {"command", "file_change", "search", "mcp", "tool", "image", "compaction", "error", "unknown"}
PUBLIC_ACTIVITY_STATUSES = {"running", "waiting", "completed", "failed", "declined"}
_PROCESS_EXIT_RE = re.compile(r"\s+·\s+exit\s+-?\d+$", re.I)
_QUESTION_KEY_RE = re.compile(r"appserver-question-[0-9a-f]{16}\Z")
_QUESTION_REFERENCE_RE = re.compile(r"appserver-turn-[0-9a-f]{16}:[0-9a-f]{64}:[1-9][0-9]*\Z")
_DURABLE_ACTIVITY_TYPES = {"command", "file_change", "search", "mcp", "tool"}


def _public_activity(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    activity_type = str(value.get("type") or "")
    status = str(value.get("status") or "")
    if activity_type not in PUBLIC_ACTIVITY_TYPES or status not in PUBLIC_ACTIVITY_STATUSES:
        return None
    result: dict[str, Any] = {
        "type": activity_type,
        "status": status,
        "title": str(value.get("title") or "")[:520],
        "summary": str(value.get("summary") or "")[:360],
        "detailKind": str(value.get("detailKind") or "none")[:48],
        "detailAvailable": bool(value.get("detailAvailable")),
    }
    for key in ("exitCode", "durationMs", "changeCount"):
        if isinstance(value.get(key), (int, float)):
            result[key] = int(value[key])
    return result


def _public_block(value: Mapping[str, Any]) -> dict[str, Any] | None:
    kind = str(value.get("kind") or "")
    item_id = str(value.get("id") or "")
    turn_key = str(value.get("turnKey") or "")
    text = str(value.get("text") or "").strip()
    if kind not in PUBLIC_BLOCK_KINDS or not item_id or not turn_key or not text:
        return None
    try:
        revision = max(0, int(value.get("revision") or 0))
    except (TypeError, ValueError):
        revision = 0
    role = str(value.get("role") or "")
    if role not in {"user", "assistant", "process"}:
        role = "user" if kind == "user" else "assistant" if kind == "output" else "process"
    block: dict[str, Any] = {
        "id": item_id,
        "turnKey": turn_key,
        "kind": kind,
        "role": role,
        "text": text,
        "revision": revision,
        "final": value.get("final") is not False,
    }
    if kind == "user":
        question_key = str(value.get("questionKey") or "")
        if _QUESTION_KEY_RE.fullmatch(question_key) is None:
            question_key = browser_question_key(item_id)
        block["questionKey"] = question_key
        block["segmentKey"] = question_key
    else:
        segment_key = str(value.get("segmentKey") or "")
        block["segmentKey"] = segment_key if _QUESTION_KEY_RE.fullmatch(segment_key) else turn_key
    if kind == "process":
        activity = _public_activity(value.get("activity"))
        if activity is not None:
            block["activity"] = activity
    return block


def _project_message_blocks(
    values: list[Mapping[str, Any]],
    preview_chars: int,
    durable_activity: list[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    public_blocks = [
        block
        for value in values
        if (block := _public_block(value)) is not None
    ]
    first_question_by_turn: dict[str, str] = {}
    question_counts_by_turn: dict[str, dict[str, int]] = {}
    segment_by_question_reference: dict[str, str] = {}
    for block in public_blocks:
        if block["kind"] == "user":
            turn_key = str(block["turnKey"])
            question_key = str(block["questionKey"])
            first_question_by_turn.setdefault(turn_key, question_key)
            digest = hashlib.sha256(str(block["text"]).strip().encode("utf-8")).hexdigest()
            turn_counts = question_counts_by_turn.setdefault(turn_key, {})
            occurrence = int(turn_counts.get(digest) or 0) + 1
            turn_counts[digest] = occurrence
            segment_by_question_reference[f"{turn_key}:{digest}:{occurrence}"] = question_key
    grouped: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    current_turn_key = ""
    current_segment_key = ""
    segments_by_turn: dict[str, list[str]] = {}
    for block in public_blocks:
        turn_key = str(block["turnKey"])
        if turn_key != current_turn_key:
            current_turn_key = turn_key
            current_segment_key = first_question_by_turn.get(turn_key, turn_key)
        if block["kind"] == "user":
            current_segment_key = str(block["questionKey"])
        elif _QUESTION_KEY_RE.fullmatch(str(block.get("segmentKey") or "")):
            current_segment_key = str(block["segmentKey"])
        segment_key = current_segment_key or turn_key
        block["segmentKey"] = segment_key
        if segment_key not in grouped:
            grouped[segment_key] = []
            order.append(segment_key)
            segments_by_turn.setdefault(turn_key, []).append(segment_key)
        grouped[segment_key].append(block)
    durable_grouped: dict[str, list[dict[str, Any]]] = {}
    for value in durable_activity or []:
        block = _public_block(value)
        if block is None or block["kind"] != "process":
            continue
        turn_key = str(block["turnKey"])
        segment_key = str(block.get("segmentKey") or "")
        question_reference = str(value.get("questionReference") or "")
        if _QUESTION_REFERENCE_RE.fullmatch(question_reference):
            segment_key = segment_by_question_reference.get(question_reference, segment_key)
        candidates = segments_by_turn.get(turn_key, [])
        if segment_key not in grouped:
            segment_key = candidates[-1] if candidates else ""
        if segment_key:
            block["segmentKey"] = segment_key
            durable_grouped.setdefault(segment_key, []).append(block)
    projected: list[dict[str, Any]] = []
    for segment_key in order:
        blocks = _merge_turn_activity(grouped[segment_key], durable_grouped.get(segment_key, []))
        text = _turn_text(blocks)
        question = next(
            (str(block["text"]) for block in blocks if block["kind"] == "user"),
            "",
        )
        if text:
            projected.append({
                "id": segment_key,
                "key": segment_key,
                "preview": codex_history.history_preview(question, preview_chars),
                "text": text,
                "blocks": blocks,
            })
    return projected


def _process_fingerprint(block: Mapping[str, Any]) -> str:
    activity = _public_activity(block.get("activity"))
    if activity is not None:
        return "\0".join((str(activity["type"]), str(activity["title"])))
    text = " ".join(str(block.get("text") or "").split())
    text = _PROCESS_EXIT_RE.sub("", text)
    if text.startswith("Running "):
        text = "Ran " + text[len("Running "):]
    return text


def _process_type(block: Mapping[str, Any]) -> str:
    activity = _public_activity(block.get("activity"))
    if activity is not None:
        return str(activity["type"])
    text = str(block.get("text") or "").strip()
    if text.startswith(("Ran ", "Running ")):
        return "command"
    if text.startswith(("Edited ", "Editing ")):
        return "file_change"
    if text.startswith(("Searched ", "Searching the web ")):
        return "search"
    if text.startswith(("Called ", "Calling ")):
        return "tool"
    return "unknown"


def _process_status(block: Mapping[str, Any]) -> str:
    activity = _public_activity(block.get("activity"))
    if activity is not None:
        return str(activity["status"])
    return "running" if block.get("final") is False else "completed"


def _merge_turn_activity(
    current: list[dict[str, Any]],
    durable: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Restore durable activity once while retaining richer live revisions."""

    if not durable:
        return current
    live_process = [block for block in current if block["kind"] == "process"]
    live_positions = {str(block["id"]): index for index, block in enumerate(live_process)}
    live_by_id = {str(block["id"]): block for block in live_process}
    live_by_fingerprint: dict[str, list[dict[str, Any]]] = {}
    for block in live_process:
        live_by_fingerprint.setdefault(_process_fingerprint(block), []).append(block)
    merged_process: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    last_matched_live_position = -1
    for durable_block in durable:
        item_id = str(durable_block["id"])
        live = live_by_id.get(item_id)
        if live is None:
            fingerprint = _process_fingerprint(durable_block)
            candidates = live_by_fingerprint.get(fingerprint, [])
            live = next(
                (candidate for candidate in candidates if str(candidate["id"]) not in used_ids),
                None,
            )
        selected = live or durable_block
        merged_process.append(selected)
        if live is not None:
            live_id = str(live["id"])
            used_ids.add(live_id)
            last_matched_live_position = max(last_matched_live_position, live_positions.get(live_id, -1))
    durable_types = {_process_type(block) for block in durable}
    for index, block in enumerate(live_process):
        if str(block["id"]) in used_ids:
            continue
        activity_type = _process_type(block)
        status = _process_status(block)
        if (
            activity_type not in _DURABLE_ACTIVITY_TYPES
            or activity_type not in durable_types
            or status in {"running", "waiting", "failed", "declined"}
            or (last_matched_live_position >= 0 and index > last_matched_live_position)
        ):
            merged_process.append(block)

    first_process = next(
        (index for index, block in enumerate(current) if block["kind"] == "process"),
        None,
    )
    if first_process is None:
        first_process = next(
            (index for index, block in enumerate(current) if block["kind"] == "output"),
            len(current),
        )
    before = [block for block in current[:first_process] if block["kind"] != "process"]
    after = [block for block in current[first_process:] if block["kind"] != "process"]
    return [*before, *merged_process, *after]


def _turn_blocks(turn: Mapping[str, Any]) -> list[dict[str, Any]]:
    turn_id = str(turn.get("id") or "")
    items = [item for item in (turn.get("items") or []) if isinstance(item, Mapping)]
    first_question_key = next(
        (
            browser_question_key(str(item.get("id") or ""))
            for item in items
            if item.get("type") == "userMessage" and item.get("id")
        ),
        "",
    )
    segment_key = first_question_key
    blocks: list[dict[str, Any]] = []
    for item in items:
        if item.get("type") == "userMessage" and item.get("id"):
            segment_key = browser_question_key(str(item["id"]))
        block = message_block(
            item,
            item_id=str(item.get("id") or ""),
            turn_id=turn_id,
            segment_key=segment_key,
            final=True,
        )
        if block is not None:
            blocks.append(block)
    return blocks


def _turn_text(blocks: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        f"{'›' if block['kind'] == 'user' else '•'} {block['text']}"
        for block in blocks
    )


def _revision(thread_id: str, turns: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256(thread_id.encode("utf-8"))
    for turn in turns:
        digest.update(b"\0")
        digest.update(str(turn["id"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(turn["text"]).encode("utf-8"))
    return digest.hexdigest()[:16]


def conversation_history_page(
    snapshot: Mapping[str, Any],
    *,
    thread_id: str,
    message_blocks: list[Mapping[str, Any]] | None = None,
    durable_activity: list[Mapping[str, Any]] | None = None,
    limit: int,
    cursor: str = "",
    around: int | None = None,
    max_page_turns: int,
    page_char_budget: int,
    preview_chars: int,
    updated_at: Callable[[], str],
) -> dict[str, Any]:
    structured_values = [item for item in (message_blocks or []) if isinstance(item, Mapping)]
    projected = _project_message_blocks(structured_values, preview_chars, durable_activity)
    if not projected:
        fallback_blocks: list[dict[str, Any]] = []
        for raw_turn in snapshot.get("turns") or []:
            if not isinstance(raw_turn, Mapping):
                continue
            fallback_blocks.extend(_turn_blocks(raw_turn))
        projected = _project_message_blocks(fallback_blocks, preview_chars, durable_activity)

    revision = _revision(thread_id, projected)
    total = len(projected)
    page_limit = max(1, min(int(limit), max_page_turns))
    if cursor and around is not None:
        raise codex_history.HistoryCursorError("choose either a history cursor or an around index")
    if around is not None:
        if around < 0 or around >= total:
            raise codex_history.HistoryCursorError("conversation history index out of range")
        start = max(0, around - page_limit // 2)
        end = min(total, start + page_limit)
        start = max(0, end - page_limit)
    else:
        end = codex_history.decode_history_cursor(cursor, revision) if cursor else total
        end = max(0, min(total, end))
        start = max(0, end - page_limit)

    selected = [dict(turn, index=index) for index, turn in enumerate(projected[start:end], start=start)]
    target_index = around if around is not None else max(start, end - 1)
    while len(selected) > 1 and sum(len(item["text"]) for item in selected) > page_char_budget:
        if around is None:
            selected.pop(0)
        elif abs(selected[0]["index"] - target_index) >= abs(selected[-1]["index"] - target_index):
            selected.pop(0)
        else:
            selected.pop()
    if selected:
        start = int(selected[0]["index"])
        end = int(selected[-1]["index"]) + 1

    turns = [
        {
            "index": int(item["index"]),
            "key": str(item["key"]),
            "preview": str(item["preview"]),
            "text": str(item["text"]),
            "blocks": list(item["blocks"]),
        }
        for item in selected
    ]
    return {
        "ok": True,
        "source": "codex-app-server",
        "revision": revision,
        "totalTurns": total,
        "start": start,
        "end": end,
        "hasOlder": start > 0,
        "hasNewer": end < total,
        "olderCursor": codex_history.history_cursor(revision, start) if start > 0 else "",
        "newerCursor": codex_history.history_cursor(revision, min(total, end + page_limit)) if end < total else "",
        "questions": [
            {"index": index, "key": str(turn["key"]), "preview": str(turn["preview"])}
            for index, turn in enumerate(projected)
        ],
        "turns": turns,
        "pageChars": sum(len(item["text"]) for item in turns),
        "oversized": any(len(item["text"]) > page_char_budget for item in turns),
        "updatedAt": updated_at(),
    }
