"""Bounded asynchronous SSE streams for Codex TUI and App Server sessions."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator
from uuid import uuid4

from anyio import to_thread
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from faryo_cli import browser_contract


def event_frame(event: str, payload: dict[str, Any], event_id: str = "") -> bytes:
    data = json.dumps(
        browser_contract.wrap_response(payload),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    prefix = f"id: {event_id}\n" if event_id else ""
    return f"{prefix}event: {event}\ndata: {data}\n\n".encode("utf-8")


def terminal_capture_payload(core: Any, config: Any, lines: int) -> tuple[dict[str, Any], int, bool]:
    core.ensure_pane_width(config)
    profile = core.agent_profile_in_pane(config)
    capture_profile = profile or core.RUNTIME_PROFILE
    text = core.capture_text(config, lines, capture_profile)
    terminal_text = text
    capture_source = "tmux"
    thread_id = ""
    live_text = ""
    agent_running = bool(profile and not core.agent_ready_for_input(config, capture_profile))
    if profile is core.CODEX_PROFILE:
        structured = core.codex_structured_capture(config, lines)
        if structured:
            text, thread_id, capture_source = structured
            if agent_running:
                live_text = core.codex_live_tail(terminal_text)
        elif core.codex_empty_managed_capture(config):
            text = ""
            capture_source = "codex-empty"
    session_metadata = core.codex_capture_session_metadata(thread_id)
    current_terminal = core.tmux_current_capture(config)
    queued_send_now = bool(
        profile is core.CODEX_PROFILE
        and core.codex_queued_send_now_available(current_terminal)
    )
    interaction_state = (
        core.interaction_snapshot_from_capture(config, current_terminal)
        if profile is core.CODEX_PROFILE
        else {"interaction": None, "interactionRevision": "none"}
    )
    command_events = core.command_timeline_events_for_config(config) if profile is core.CODEX_PROFILE else []
    command_revision = ",".join(
        f"{event.get('id')}:{event.get('status')}:{event.get('completedAt')}"
        for event in command_events[-8:]
    )
    digest = core.capture_event_digest(
        text,
        live_text,
        session_metadata,
        f"{interaction_state.get('interactionRevision') or ''}:{command_revision}",
        queued_send_now,
    )
    payload = {
        "ok": True,
        "text": text,
        "agentRunning": agent_running,
        "queuedSendNowAvailable": queued_send_now,
        "agentSource": capture_profile.source,
        "agentProfile": capture_profile.key,
        "captureSource": capture_source,
        "backend": core.session_backend.CODEX_TUI.value,
        **interaction_state,
        "updatedAt": core.now_iso(),
    }
    if command_events:
        payload["commandEvents"] = command_events
    if thread_id:
        payload.update(session_metadata)
    if live_text:
        payload["liveText"] = live_text
    return payload, digest, agent_running


class OwnerEventStreams:
    def __init__(self, core: Any, support: Any) -> None:
        self.core = core
        self.support = support
        self.slots = asyncio.Semaphore(core.EVENT_STREAM_MAX_CONNECTIONS)
        self.active: dict[str, asyncio.Event] = {}

    def close_active_streams(self) -> int:
        streams = list(self.active.values())
        for event in streams:
            event.set()
        return len(streams)

    async def response(self, request: Request) -> Response:
        self.support.require_token(request)
        try:
            await asyncio.wait_for(self.slots.acquire(), timeout=0.01)
        except asyncio.TimeoutError:
            return self.support.json_response(
                {"ok": False, "error": "too many event streams", "updatedAt": self.core.now_iso()},
                429,
            )
        stream_id = uuid4().hex
        stopped = asyncio.Event()
        self.active[stream_id] = stopped
        session = request.query_params.get("session", "")
        try:
            lines = int(request.query_params.get("lines", str(self.core.CAPTURE_COMPACT_LINES)))
        except ValueError:
            lines = self.core.CAPTURE_COMPACT_LINES
        lines = max(40, min(lines, self.core.CAPTURE_MAX_LINES))
        cursor = request.headers.get("Last-Event-ID") or request.query_params.get("cursor", "")
        web_managed = self.support.runtime.has_session(session)
        # Resolve terminal targets before StreamingResponse sends its headers.
        # Unknown/bookmarked history ids must produce a normal bounded 404,
        # not raise inside the body iterator after a 200 SSE response started.
        terminal_target = None if web_managed else self.support.target(session)

        async def stream() -> AsyncIterator[bytes]:
            try:
                yield b": opened\n\n"
                if web_managed:
                    async for frame in self._web_stream(session, lines, cursor, stopped):
                        yield frame
                else:
                    async for frame in self._terminal_stream(terminal_target, lines, stopped):
                        yield frame
            finally:
                self.active.pop(stream_id, None)
                self.slots.release()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    async def _web_stream(
        self,
        session: str,
        lines: int,
        initial_cursor: str,
        stopped: asyncio.Event,
    ) -> AsyncIterator[bytes]:
        cursor = initial_cursor
        deadline = time.monotonic() + self.core.EVENT_STREAM_MAX_SECONDS
        while time.monotonic() < deadline and not stopped.is_set():
            result = await to_thread.run_sync(
                lambda selected_cursor=cursor: self.support.runtime.wait_for_events(
                    selected_cursor,
                    self.core.EVENT_STREAM_HEARTBEAT_SECONDS,
                ),
                abandon_on_cancel=True,
            )
            if result.status != "replay" or result.events or not cursor:
                if result.status in {"gap", "reset"}:
                    yield event_frame(
                        "stream-state",
                        {"status": result.status, "updatedAt": self.core.now_iso()},
                        result.latest.render(),
                    )
                try:
                    capture = await to_thread.run_sync(
                        lambda: self.core.web_capture_payload(
                            self.support.runtime,
                            session,
                            lines,
                        ),
                        abandon_on_cancel=True,
                    )
                except self.core.OwnerError:
                    return
                yield event_frame("capture", capture, result.latest.render())
                cursor = result.latest.render()
            else:
                yield b": keepalive\n\n"

    async def _terminal_stream(
        self,
        target: Any,
        lines: int,
        stopped: asyncio.Event,
    ) -> AsyncIterator[bytes]:
        last_hash: int | None = None
        last_running: bool | None = None
        last_write = time.monotonic()
        deadline = time.monotonic() + self.core.EVENT_STREAM_MAX_SECONDS
        while time.monotonic() < deadline and not stopped.is_set():
            try:
                payload, digest, agent_running = await to_thread.run_sync(
                    lambda: terminal_capture_payload(self.core, target, lines),
                    abandon_on_cancel=True,
                )
            except self.core.OwnerError:
                return
            if digest != last_hash or agent_running != last_running:
                last_hash = digest
                last_running = agent_running
                yield event_frame("capture", payload)
                last_write = time.monotonic()
            elif time.monotonic() - last_write >= self.core.EVENT_STREAM_HEARTBEAT_SECONDS:
                yield b": keepalive\n\n"
                last_write = time.monotonic()
            try:
                await asyncio.wait_for(stopped.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
