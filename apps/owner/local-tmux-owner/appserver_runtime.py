"""Long-lived Codex App Server session orchestration."""

from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError
import hashlib
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Mapping

from appserver_events import EventJournal, ReplayResult
from appserver_commands import AppServerCommandError, AppServerCommandService
from appserver_protocol import AppServerError, AppServerUnavailable
from appserver_registry import WebSessionRecord, WebSessionRegistry, new_worker_id
import appserver_session_supervisor
from appserver_requests import (
    CLIENT_REQUEST_RE,
    AppServerInteractionBroker,
    AppServerInteractionError,
    declined_response,
)
from appserver_session import (
    HANDLED_NOTIFICATION_METHODS,
    ActorEvent,
    WebSessionActor,
    activity_detail,
    browser_item_key,
    user_message_text,
)
from appserver_transport import AsyncCodexAppServerClient, unix_socket_connector
import command_timeline


RUNTIME_CALL_TIMEOUT = 20.0
RUNTIME_RECONNECT_MIN_SECONDS = 0.2
RUNTIME_RECONNECT_MAX_SECONDS = 8.0
MAX_SEND_CHARS = 120_000
CLIENT_MESSAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
SEND_RECEIPT_TTL_SECONDS = 48 * 60 * 60
SEND_ACK_WAIT_SECONDS = 0.35
DELTA_PUBLISH_INTERVAL_SECONDS = 0.04
TURN_INTERRUPT_SETTLE_SECONDS = 1.0
COMPAT_RPC_METHODS = {
    "account/rateLimits/read",
    "thread/goal/get",
    "thread/read",
}


class AppServerRuntimeError(RuntimeError):
    pass


class AppServerRuntime:
    def __init__(
        self,
        *,
        socket_path: Path,
        registry_path: Path,
        client_version: str,
        reserved_names: Callable[[], list[str]] | None = None,
        namespace_lock: threading.RLock | None = None,
        command_store: command_timeline.CommandTimelineStore | None = None,
        client_factory: Callable[[Callable[..., Any], Callable[..., Any]], Any] | None = None,
        session_client_factory: Callable[[Callable[..., Any], Callable[..., Any]], Any] | None = None,
        worker_manager: appserver_session_supervisor.AppServerWorkerManager | None = None,
        journal_max_events: int = 4096,
        journal_max_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self.socket_path = socket_path
        self.registry = WebSessionRegistry(registry_path)
        self.client_version = client_version
        self.reserved_names = reserved_names or (lambda: [])
        self.namespace_lock = namespace_lock or threading.RLock()
        self.command_timeline = command_store or command_timeline.CommandTimelineStore(
            registry_path.with_name("command-timeline.json")
        )
        self.client_factory = client_factory
        self.session_client_factory = session_client_factory or client_factory
        self.worker_manager = worker_manager or (
            appserver_session_supervisor.FactoryWorkerManager(
                registry_path.with_name("appserver-workers")
            )
            if client_factory is not None
            else None
        )
        if self.worker_manager is None:
            raise ValueError("worker_manager is required for production App Server sessions")
        self.journal = EventJournal(max_events=journal_max_events, max_bytes=journal_max_bytes)
        self.actors: dict[str, WebSessionActor] = {}
        self.client: AsyncCodexAppServerClient | None = None
        self.session_supervisor = appserver_session_supervisor.AppServerSessionSupervisor(
            client_version=self.client_version,
            worker_manager=self.worker_manager,
            record=self.registry.get,
            notification=self._session_notification,
            server_request=self._server_request_for,
            state_changed=lambda name, state, increment: self._update_worker_state(
                name,
                state,
                increment_generation=increment,
            ),
            hydrated=self._session_hydrated,
            error_observed=self._session_error_observed,
            probe_required=self._worker_probe_required,
            client_factory=self.session_client_factory,
        )
        self.session_clients = self.session_supervisor.clients
        self.session_reconnect_tasks = self.session_supervisor.reconnect_tasks
        self.pending_session_notifications: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.stop_async: asyncio.Event | None = None
        self.disconnected_async: asyncio.Event | None = None
        self.launch_lock: asyncio.Lock | None = None
        self.condition = threading.Condition()
        self.state = "stopped"
        self.last_error = ""
        self.reconnect_count = 0
        self.started_at = 0.0
        self.rate_limits: dict[str, Any] = {}
        self.ignored_notifications: dict[str, int] = {}
        self.send_tasks: dict[str, tuple[str, str, asyncio.Task[dict[str, Any]]]] = {}
        self.session_send_locks: dict[str, asyncio.Lock] = {}
        self.send_receipts: dict[str, dict[str, Any]] = {}
        self.command_receipts: dict[str, dict[str, Any]] = {}
        self.pending_delta_events: dict[tuple[str, str], tuple[WebSessionRecord, ActorEvent]] = {}
        self.delta_flush_handles: dict[tuple[str, str], asyncio.TimerHandle] = {}
        self.interactions = AppServerInteractionBroker(
            on_open=self._interaction_opened,
            on_close=self._interaction_closed,
        )
        self.commands = AppServerCommandService(
            on_open=self._interaction_opened,
            on_close=self._interaction_closed,
        )

    def start(self) -> None:
        with self.condition:
            if self.thread is not None and self.thread.is_alive():
                return
            with self.namespace_lock:
                self.registry.reassign_conflicts(self.reserved_names())
            self.state = "connecting"
            self.last_error = ""
            self.session_supervisor.begin()
            self.started_at = time.monotonic()
            self.thread = threading.Thread(target=self._thread_main, name="faryo-appserver-runtime", daemon=True)
            self.thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        loop = self.loop
        stop = self.stop_async
        self.session_supervisor.request_stop()
        if loop is not None and stop is not None:
            loop.call_soon_threadsafe(stop.set)
        thread = self.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.1, timeout))
        with self.condition:
            self.state = "stopped"
            self.condition.notify_all()

    def ready(self) -> bool:
        with self.condition:
            return self.state == "ready"

    def wait_ready(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        with self.condition:
            while self.state not in {"ready", "stopped"} and time.monotonic() < deadline:
                self.condition.wait(max(0.01, deadline - time.monotonic()))
            return self.state == "ready"

    def status(self) -> dict[str, Any]:
        with self.condition:
            worker_states = {
                state: sum(1 for record in self.registry.values() if record.worker_state == state)
                for state in ("starting", "ready", "reconnecting", "degraded", "stopping", "exited")
            }
            return {
                "state": self.state,
                "ready": self.state == "ready",
                "reconnectCount": self.reconnect_count,
                "sessionCount": len(self.registry.values()),
                "loadedSessionCount": len(self.actors),
                "pendingRpcCount": (
                    (self.client.pending_count if self.client is not None else 0)
                    + self.session_supervisor.pending_count
                ),
                "workerStates": worker_states,
                "workerRpc": self.session_supervisor.rpc_diagnostics,
                "controlRpc": dict(getattr(self.client, "rpc_diagnostics", {}) or {}),
                "openCircuitCount": self.session_supervisor.open_circuit_count,
                "experimentalApi": bool(getattr(self.client, "experimental_api", False)) if self.client is not None else False,
                "lastError": self.last_error,
                "eventCursor": self.journal.latest.render(),
                "eventCount": len(self.journal.events),
                "eventBytes": self.journal.total_bytes,
                "ignoredNotificationCount": sum(self.ignored_notifications.values()),
            }

    def has_session(self, name: str) -> bool:
        return self.registry.get(name) is not None

    def has_thread(self, thread_id: str) -> bool:
        return self.registry.by_thread(thread_id) is not None

    def thread_loaded(self, thread_id: str, timeout: float = RUNTIME_CALL_TIMEOUT) -> bool:
        return self._submit(self._thread_loaded(thread_id), timeout)

    def compat_rpc(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float = RUNTIME_CALL_TIMEOUT,
    ) -> dict[str, Any]:
        """Serve bounded read compatibility calls over the resident socket.

        Owner historically spawned a second stdio App Server for these calls.
        Reusing the supervised socket process avoids another model-refresh
        worker and keeps every App Server read behind one connection manager.
        """

        try:
            result = self._submit(self._compat_rpc(method, params), timeout)
        except AppServerRuntimeError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "result": result}

    def session_records(self) -> list[dict[str, Any]]:
        return [record.public() for record in sorted(self.registry.values(), key=lambda item: item.updated_at, reverse=True)]

    def start_session(
        self,
        *,
        cwd: str,
        title: str = "",
        model: str = "",
        service_tier: str = "",
        context_window_k: int = 0,
        launch_id: str = "",
        timeout: float = RUNTIME_CALL_TIMEOUT,
    ) -> dict[str, Any]:
        # The namespace lock is intentionally held by the calling thread while
        # the App Server RPC completes.  The event-loop coroutine must not try
        # to reacquire this cross-backend lock from a different thread.
        with self.namespace_lock:
            return self._submit(
                self._start_session(cwd, title, model, service_tier, context_window_k, launch_id),
                timeout,
            )

    def resume_session(
        self,
        *,
        thread_id: str,
        cwd: str,
        title: str = "",
        model: str = "",
        service_tier: str = "",
        context_window_k: int = 0,
        timeout: float = RUNTIME_CALL_TIMEOUT,
    ) -> dict[str, Any]:
        with self.namespace_lock:
            return self._submit(
                self._resume_session(thread_id, cwd, title, model, service_tier, context_window_k),
                timeout,
            )

    def send(self, name: str, text: str, client_message_id: str, timeout: float = RUNTIME_CALL_TIMEOUT) -> dict[str, Any]:
        return self._submit(self._send(name, text, client_message_id), timeout)

    def interrupt(self, name: str, timeout: float = RUNTIME_CALL_TIMEOUT) -> dict[str, Any]:
        return self._submit(self._interrupt(name), timeout)

    def close_session(
        self,
        name: str,
        *,
        interrupt: bool = False,
        timeout: float = RUNTIME_CALL_TIMEOUT,
    ) -> dict[str, Any]:
        with self.namespace_lock:
            return self._submit(self._close_session(name, interrupt=interrupt), timeout)

    def respond_interaction(
        self,
        name: str,
        *,
        interaction_id: str,
        option_id: str = "",
        answers: Mapping[str, Any] | None = None,
        action: str = "",
        client_request_id: str,
        timeout: float = RUNTIME_CALL_TIMEOUT,
    ) -> dict[str, Any]:
        return self._submit(
            self._respond_interaction(
                name,
                interaction_id=interaction_id,
                option_id=option_id,
                answers=answers,
                action=action,
                client_request_id=client_request_id,
            ),
            timeout,
        )

    def begin_command(
        self,
        name: str,
        *,
        command: str,
        client_request_id: str,
        confirmed: bool = False,
        timeout: float = RUNTIME_CALL_TIMEOUT,
    ) -> dict[str, Any]:
        return self._submit(
            self._begin_command(
                name,
                command=command,
                client_request_id=client_request_id,
                confirmed=confirmed,
            ),
            timeout,
        )

    def capture(self, name: str, timeout: float = 5.0) -> dict[str, Any]:
        return self._submit(self._capture(name), timeout)

    def activity_detail(self, name: str, item_id: str, timeout: float = 5.0) -> dict[str, Any]:
        return self._submit(self._activity_detail(name, item_id), timeout)

    def thread_lifecycle(self, method: str, thread_id: str, timeout: float = 5.0) -> dict[str, Any]:
        return self._submit(self._thread_lifecycle(method, thread_id), timeout)

    def replay(self, cursor: str | None) -> ReplayResult:
        with self.condition:
            return self.journal.replay(cursor)

    def wait_for_events(self, cursor: str | None, timeout: float) -> ReplayResult:
        with self.condition:
            if not cursor:
                return self.journal.replay(None)
            initial_sequence = self.journal.latest.sequence
            parsed_result = self.journal.replay(cursor)
            if parsed_result.status != "replay" or parsed_result.events:
                return parsed_result
            self.condition.wait_for(
                lambda: self.journal.latest.sequence > initial_sequence or self.state in {"stopped"},
                timeout=max(0.01, timeout),
            )
            return self.journal.replay(cursor)

    def _submit(self, coroutine: Any, timeout: float) -> Any:
        loop = self.loop
        if loop is None or not loop.is_running():
            coroutine.close()
            raise AppServerRuntimeError("Codex App Server runtime is not started")
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        try:
            return future.result(timeout=max(0.01, timeout))
        except FutureTimeoutError as exc:
            future.cancel()
            raise AppServerRuntimeError("Codex App Server operation timed out") from exc
        except AppServerRuntimeError:
            raise
        except Exception as exc:
            raise AppServerRuntimeError(str(exc) or "Codex App Server operation failed") from exc

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        finally:
            self.loop = None
            with self.condition:
                if self.state != "stopped":
                    self.state = "stopped"
                self.condition.notify_all()

    async def _run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.stop_async = asyncio.Event()
        self.launch_lock = asyncio.Lock()
        self.session_supervisor.start_monitor()
        delay = RUNTIME_RECONNECT_MIN_SECONDS
        while not self.stop_async.is_set():
            self.disconnected_async = asyncio.Event()

            async def disconnected(error: BaseException) -> None:
                self._set_state("reconnecting", self._error_class(error))
                if self.disconnected_async is not None:
                    self.disconnected_async.set()

            client = (
                self.client_factory(self._notification, disconnected)
                if self.client_factory is not None
                else AsyncCodexAppServerClient(
                    connector=lambda: unix_socket_connector(self.socket_path),
                    client_version=self.client_version,
                    notification_handler=self._notification,
                    disconnect_handler=disconnected,
                )
            )
            self.client = client
            self._set_state("connecting")
            try:
                await client.connect()
                try:
                    rate_limits = await client.rpc("account/rateLimits/read", {})
                    self.rate_limits = dict(rate_limits) if isinstance(rate_limits, Mapping) else {}
                except AppServerError:
                    self.rate_limits = {}
                await self._restore_sessions()
                self._set_state("ready")
                delay = RUNTIME_RECONNECT_MIN_SECONDS
                stop_wait = asyncio.create_task(self.stop_async.wait())
                disconnect_wait = asyncio.create_task(self.disconnected_async.wait())
                done, pending = await asyncio.wait(
                    {stop_wait, disconnect_wait},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if stop_wait in done and stop_wait.result():
                    break
                self.reconnect_count += 1
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._set_state("reconnecting", self._error_class(exc))
                self.reconnect_count += 1
            finally:
                self._flush_all_deltas()
                await client.close()
                if self.client is client:
                    self.client = None
            if self.stop_async.is_set():
                break
            try:
                await asyncio.wait_for(self.stop_async.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(RUNTIME_RECONNECT_MAX_SECONDS, delay * 1.8)
        await self.session_supervisor.close_connections()
        self._set_state("stopped")

    async def _restore_sessions(self) -> None:
        self.session_supervisor.restore_all(self.registry.values())

    async def _session_hydrated(self, name: str, thread: Mapping[str, Any]) -> None:
        record = self.registry.get(name)
        if record is None:
            return
        actor = self.actors.get(name)
        if actor is None:
            actor = WebSessionActor(session_id=name, thread_id=record.thread_id)
            actor.require_durable_activity()
            self.actors[name] = actor
        actor.hydrate(thread)
        self._publish(record, ActorEvent("session.snapshot", payload=actor.snapshot()))
        await self._flush_pending_session_notifications(name)

    def _session_error_observed(self, error: BaseException) -> None:
        with self.condition:
            self.last_error = self._error_class(error)
            self.condition.notify_all()

    def _worker_probe_required(self, name: str) -> bool:
        actor = self.actors.get(name)
        return bool(actor is not None and (actor.active_turn_id or actor.interaction is not None))

    async def _session_notification(
        self,
        name: str,
        source_client: AsyncCodexAppServerClient,
        method: str,
        params: dict[str, Any],
    ) -> None:
        if self.session_clients.get(name) is not source_client:
            with self.condition:
                key = "__stale_worker_notification__"
                self.ignored_notifications[key] = self.ignored_notifications.get(key, 0) + 1
            return
        record = self.registry.get(name)
        if record is None:
            pending = self.pending_session_notifications.setdefault(name, [])
            if len(pending) < 128:
                pending.append((method, dict(params)))
            return
        thread_id = str(params.get("threadId") or "")
        if not thread_id:
            thread = params.get("thread")
            thread_id = str(thread.get("id") or "") if isinstance(thread, Mapping) else ""
        if thread_id and thread_id != record.thread_id:
            with self.condition:
                key = "__cross_session_notification__"
                self.ignored_notifications[key] = self.ignored_notifications.get(key, 0) + 1
            return
        await self._notification(method, params)

    async def _flush_pending_session_notifications(self, name: str) -> None:
        client = self.session_clients.get(name)
        if client is None:
            self.pending_session_notifications.pop(name, None)
            return
        for method, params in self.pending_session_notifications.pop(name, []):
            await self._session_notification(name, client, method, params)

    def _update_worker_state(
        self,
        name: str,
        state: str,
        *,
        increment_generation: bool = False,
    ) -> None:
        try:
            self.registry.update_worker_state(
                name,
                state,
                increment_generation=increment_generation,
            )
        except ValueError:
            return

    async def _notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "account/rateLimits/updated":
            snapshot = params.get("rateLimits")
            if isinstance(snapshot, Mapping):
                self.rate_limits = {"rateLimits": dict(snapshot)}
                for record in self.registry.values():
                    self._publish(record, ActorEvent("session.rate_limits", payload={"rateLimits": dict(snapshot)}))
            return
        if method not in HANDLED_NOTIFICATION_METHODS:
            with self.condition:
                self.ignored_notifications[method] = self.ignored_notifications.get(method, 0) + 1
            return
        thread_id = str(params.get("threadId") or "")
        if not thread_id:
            thread = params.get("thread")
            thread_id = str(thread.get("id") or "") if isinstance(thread, Mapping) else ""
        record = self.registry.by_thread(thread_id)
        if record is None:
            return
        actor = self.actors.get(record.name)
        if actor is None:
            actor = WebSessionActor(session_id=record.name, thread_id=record.thread_id)
            actor.require_durable_activity()
            self.actors[record.name] = actor
        for event in actor.apply(method, params):
            if event.kind == "item.delta" and event.item_id:
                self._queue_delta(record, event)
                continue
            if event.item_id:
                self._flush_delta((record.name, event.item_id))
            self._publish(record, event)
        if method == "thread/name/updated":
            name = params.get("threadName")
            self.registry.update_metadata(record.name, title=str(name or ""))
        elif method == "thread/settings/updated":
            settings = params.get("threadSettings")
            if isinstance(settings, Mapping) and isinstance(settings.get("model"), str):
                self.registry.update_metadata(record.name, model=str(settings["model"]))
        elif method in {"turn/started", "turn/completed"}:
            self.registry.touch(record.name)

    async def _server_request_for(
        self,
        name: str,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        record = self.registry.get(name)
        thread_id = str(params.get("threadId") or "")
        if (
            record is None
            or (thread_id and thread_id != record.thread_id)
        ):
            return declined_response(method, params)
        actor = self.actors.get(record.name)
        if actor is not None and actor.interaction is not None:
            return declined_response(method, params)
        return await self.interactions.request(record.name, method, params)

    async def _start_session(
        self,
        cwd: str,
        title: str,
        model: str,
        service_tier: str,
        context_window_k: int,
        launch_id: str,
    ) -> dict[str, Any]:
        lock = self.launch_lock
        if lock is None:
            raise AppServerRuntimeError("Codex App Server runtime is not ready")
        async with lock:
            return await self._start_session_locked(cwd, title, model, service_tier, context_window_k, launch_id)

    async def _start_session_locked(
        self,
        cwd: str,
        title: str,
        model: str,
        service_tier: str,
        context_window_k: int,
        launch_id: str,
    ) -> dict[str, Any]:
        existing = self.registry.by_launch(launch_id)
        if existing is not None:
            actor = self.actors.get(existing.name)
            return {
                "session": existing.name,
                "threadId": existing.thread_id,
                "state": actor.lifecycle if actor is not None else "loading",
                "backend": existing.backend,
                "duplicate": True,
            }
        selected_name = self.registry.next_name(self.reserved_names())
        worker_id = new_worker_id(record.worker_id for record in self.registry.values())
        client: AsyncCodexAppServerClient | None = None
        params: dict[str, Any] = {"cwd": cwd, "serviceName": "faryo"}
        if model:
            params["model"] = model
        if service_tier:
            params["serviceTier"] = service_tier
        if context_window_k:
            params["config"] = {"model_context_window": context_window_k * 1000}
        try:
            client = await self.session_supervisor.open_client(selected_name, worker_id)
            result = await client.rpc("thread/start", params)
            thread = result.get("thread") if isinstance(result, dict) else None
            thread_id = str(thread.get("id") or "") if isinstance(thread, Mapping) else ""
            if not thread_id:
                raise AppServerRuntimeError("Codex App Server did not return a thread id")
            record = self.registry.add(
                name=selected_name,
                worker_id=worker_id,
                thread_id=thread_id,
                cwd=cwd,
                title=title,
                model=model,
                launch_id=launch_id,
                reserved=self.reserved_names(),
            )
        except BaseException:
            await self._discard_unregistered_worker(selected_name, worker_id, client)
            raise
        actor = WebSessionActor(session_id=record.name, thread_id=thread_id)
        if isinstance(thread, Mapping):
            actor.hydrate(thread)
        self.actors[record.name] = actor
        self._update_worker_state(record.name, "ready", increment_generation=True)
        self._publish(record, ActorEvent("session.snapshot", payload=actor.snapshot()))
        await self._flush_pending_session_notifications(record.name)
        return {
            "session": record.name,
            "threadId": thread_id,
            "state": actor.lifecycle,
            "backend": record.backend,
            "duplicate": False,
        }

    async def _discard_unregistered_worker(
        self,
        name: str,
        worker_id: str,
        client: AsyncCodexAppServerClient | None,
    ) -> None:
        self.pending_session_notifications.pop(name, None)
        await self.session_supervisor.discard_unregistered(
            name,
            worker_id,
            client,
        )

    async def _send(self, name: str, text: str, client_message_id: str) -> dict[str, Any]:
        record, actor = self._require_session(name)
        value = text.strip()
        if not value:
            raise AppServerRuntimeError("message is empty")
        if len(text) > MAX_SEND_CHARS:
            raise AppServerRuntimeError(f"text too long: {len(text)} > {MAX_SEND_CHARS}")
        if not CLIENT_MESSAGE_RE.fullmatch(client_message_id):
            raise AppServerRuntimeError("invalid client message id")
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        self._prune_send_receipts()
        receipt = self.send_receipts.get(client_message_id)
        if receipt is not None:
            if receipt["session"] != name or receipt["digest"] != digest:
                raise AppServerRuntimeError("client message id was already used for different content")
            return {**receipt["result"], "duplicate": True}
        for projection in actor.items.values():
            if projection.type == "userMessage" and projection.raw.get("clientId") == client_message_id:
                if user_message_text(projection.raw).strip() != value:
                    raise AppServerRuntimeError("client message id was already used for different content")
                result = self._delivery_receipt(name, client_message_id, "", duplicate=True)
                self._remember_send_receipt(client_message_id, name, digest, result)
                return result
        existing = self.send_tasks.get(client_message_id)
        if existing is not None:
            existing_name, existing_digest, task = existing
            if existing_name != name or existing_digest != digest:
                raise AppServerRuntimeError("client message id was already used for different content")
            if task.done():
                result = await asyncio.shield(task)
                return {**result, "duplicate": True}
            return self._delivery_receipt(
                name,
                client_message_id,
                "",
                duplicate=True,
                delivery_state="submitting",
            )
        else:
            task = asyncio.create_task(
                self._perform_send(record, actor, value, client_message_id),
                name=f"faryo-send-{client_message_id[:24]}",
            )
            self.send_tasks[client_message_id] = (name, digest, task)
            task.add_done_callback(
                lambda completed, message_id=client_message_id: self._send_task_completed(message_id, completed)
            )
        try:
            return await asyncio.wait_for(
                asyncio.shield(task),
                timeout=SEND_ACK_WAIT_SECONDS,
            )
        except asyncio.TimeoutError:
            return self._delivery_receipt(
                name,
                client_message_id,
                "",
                delivery_state="submitting",
            )

    async def _perform_send(
        self,
        record: WebSessionRecord,
        actor: WebSessionActor,
        text: str,
        client_message_id: str,
    ) -> dict[str, Any]:
        lock = self.session_send_locks.setdefault(record.name, asyncio.Lock())
        async with lock:
            active_turn_id = actor.active_turn_id
            params: dict[str, Any] = {
                "threadId": record.thread_id,
                "input": [{"type": "text", "text": text}],
                "clientUserMessageId": client_message_id,
            }
            if active_turn_id:
                params["expectedTurnId"] = active_turn_id
                result = await self._session_rpc(record.name, "turn/steer", params)
                turn_id = str(result.get("turnId") or "") if isinstance(result, Mapping) else ""
                delivery_state = "steered"
            else:
                result = await self._session_rpc(record.name, "turn/start", params)
                turn = result.get("turn") if isinstance(result, dict) else None
                turn_id = str(turn.get("id") or "") if isinstance(turn, Mapping) else ""
                delivery_state = "submitted"
        self.registry.touch(record.name)
        return self._delivery_receipt(
            record.name,
            client_message_id,
            turn_id,
            delivery_state=delivery_state,
        )

    @staticmethod
    def _delivery_receipt(
        name: str,
        client_message_id: str,
        turn_id: str,
        *,
        duplicate: bool = False,
        delivery_state: str = "submitted",
    ) -> dict[str, Any]:
        return {
            "accepted": True,
            "deliveryId": client_message_id,
            "delivery": "accepted",
            "deliveryState": delivery_state,
            "session": name,
            "enterAttempts": 0,
            "clientMessageId": client_message_id,
            "turnId": turn_id,
            "duplicate": duplicate,
        }

    def _send_task_completed(self, client_message_id: str, task: asyncio.Task[dict[str, Any]]) -> None:
        entry = self.send_tasks.get(client_message_id)
        if entry is None or entry[2] is not task:
            return
        name, digest, _task = entry
        self.send_tasks.pop(client_message_id, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            self._remember_send_receipt(client_message_id, name, digest, task.result())

    def _remember_send_receipt(
        self,
        client_message_id: str,
        name: str,
        digest: str,
        result: dict[str, Any],
    ) -> None:
        self.send_receipts[client_message_id] = {
            "session": name,
            "digest": digest,
            "result": dict(result),
            "updatedAt": time.monotonic(),
        }

    def _prune_send_receipts(self) -> None:
        cutoff = time.monotonic() - SEND_RECEIPT_TTL_SECONDS
        for key in [key for key, value in self.send_receipts.items() if value["updatedAt"] < cutoff]:
            self.send_receipts.pop(key, None)

    async def _resume_session(
        self,
        thread_id: str,
        cwd: str,
        title: str,
        model: str,
        service_tier: str,
        context_window_k: int,
    ) -> dict[str, Any]:
        lock = self.launch_lock
        if lock is None:
            raise AppServerRuntimeError("Codex App Server runtime is not ready")
        async with lock:
            return await self._resume_session_locked(
                thread_id,
                cwd,
                title,
                model,
                service_tier,
                context_window_k,
            )

    async def _resume_session_locked(
        self,
        thread_id: str,
        cwd: str,
        title: str,
        model: str,
        service_tier: str,
        context_window_k: int,
    ) -> dict[str, Any]:
        clean_thread_id = str(thread_id or "").strip()
        if not clean_thread_id:
            raise AppServerRuntimeError("Codex thread id is required")
        existing = self.registry.by_thread(clean_thread_id)
        if existing is not None:
            actor = self.actors.get(existing.name)
            return {
                "session": existing.name,
                "threadId": existing.thread_id,
                "state": actor.lifecycle if actor is not None else "loading",
                "backend": existing.backend,
                "duplicate": True,
            }
        params: dict[str, Any] = {"threadId": clean_thread_id}
        if cwd:
            params["cwd"] = cwd
        if model:
            params["model"] = model
        if service_tier:
            params["serviceTier"] = service_tier
        if context_window_k:
            params["config"] = {"model_context_window": context_window_k * 1000}
        selected_name = self.registry.next_name(self.reserved_names())
        worker_id = new_worker_id(record.worker_id for record in self.registry.values())
        client: AsyncCodexAppServerClient | None = None
        try:
            client = await self.session_supervisor.open_client(selected_name, worker_id)
            result = await client.rpc("thread/resume", params)
            thread = result.get("thread") if isinstance(result, dict) else None
            resumed_id = str(thread.get("id") or "") if isinstance(thread, Mapping) else ""
            if resumed_id != clean_thread_id:
                raise AppServerRuntimeError("Codex App Server resumed an unexpected thread")
            thread_cwd = str(thread.get("cwd") or cwd) if isinstance(thread, Mapping) else cwd
            if not thread_cwd:
                raise AppServerRuntimeError("resumed Codex thread has no working directory")
            record = self.registry.add(
                name=selected_name,
                worker_id=worker_id,
                thread_id=clean_thread_id,
                cwd=thread_cwd,
                title=title or (str(thread.get("name") or "") if isinstance(thread, Mapping) else ""),
                model=model or (str(thread.get("model") or "") if isinstance(thread, Mapping) else ""),
                reserved=self.reserved_names(),
            )
        except BaseException:
            await self._discard_unregistered_worker(selected_name, worker_id, client)
            raise
        actor = WebSessionActor(session_id=record.name, thread_id=clean_thread_id)
        actor.require_durable_activity()
        if isinstance(thread, Mapping):
            actor.hydrate(thread)
        self.actors[record.name] = actor
        self._update_worker_state(record.name, "ready", increment_generation=True)
        self._publish(record, ActorEvent("session.snapshot", payload=actor.snapshot()))
        await self._flush_pending_session_notifications(record.name)
        return {
            "session": record.name,
            "threadId": record.thread_id,
            "state": actor.lifecycle,
            "backend": record.backend,
            "duplicate": False,
        }

    async def _interrupt(self, name: str) -> dict[str, Any]:
        record, actor = self._require_session(name)
        if not actor.active_turn_id:
            return {"interrupted": False, "reason": "idle"}
        turn_id = actor.active_turn_id
        try:
            await self._session_rpc(
                name,
                "turn/interrupt",
                {"threadId": record.thread_id, "turnId": turn_id},
                timeout=3.0,
                overload_attempts=1,
            )
        except AppServerUnavailable:
            return {
                "interrupted": True,
                "turnId": turn_id,
                "settled": False,
                "forcedRecovery": True,
            }
        deadline = time.monotonic() + TURN_INTERRUPT_SETTLE_SECONDS
        while actor.active_turn_id == turn_id and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        if actor.active_turn_id == turn_id:
            try:
                result = await self._session_rpc(
                    name,
                    "thread/read",
                    {"threadId": record.thread_id},
                    timeout=3.0,
                    overload_attempts=1,
                )
                thread = result.get("thread") if isinstance(result, Mapping) else None
                if isinstance(thread, Mapping):
                    actor.hydrate(thread)
                    self._publish(record, ActorEvent("session.snapshot", payload=actor.snapshot()))
            except AppServerUnavailable:
                pass
        forced_recovery = actor.active_turn_id == turn_id
        if forced_recovery:
            client = self.session_clients.get(name)
            if client is not None:
                self.session_supervisor.request_restart(
                    name,
                    client,
                    AppServerUnavailable("Codex turn did not settle after interrupt"),
                )
        return {
            "interrupted": True,
            "turnId": turn_id,
            "settled": not forced_recovery,
            "forcedRecovery": forced_recovery,
        }

    async def _capture(self, name: str) -> dict[str, Any]:
        record = self.registry.get(name)
        if record is None:
            raise AppServerRuntimeError("Codex App Server session is unavailable")
        actor = self.actors.get(name)
        if actor is None:
            # Control-plane readiness intentionally does not wait for every
            # session worker.  Return an immediate loading projection while
            # the supervisor reconnects and hydrates the authoritative actor.
            actor = WebSessionActor(session_id=name, thread_id=record.thread_id)
            actor.require_durable_activity()
            self.actors[name] = actor
        snapshot = actor.snapshot()
        snapshot["rateLimits"] = dict(self.rate_limits)
        return {
            "record": record.public(),
            "snapshot": snapshot,
            "messages": actor.messages(),
            "messageBlocks": actor.message_blocks(),
            "commandEvents": self.command_timeline.public_events(self._command_owner_key(record)),
        }

    async def _activity_detail(self, name: str, item_id: str) -> dict[str, Any]:
        _record, actor = self._require_session(name)
        public_id = str(item_id or "").strip()
        if not re.fullmatch(r"appserver-item-[0-9a-f]{16}", public_id):
            raise AppServerRuntimeError("invalid activity item")
        projection = next(
            (
                actor.items[raw_id]
                for raw_id in actor.item_order
                if browser_item_key(raw_id) == public_id
            ),
            None,
        )
        if projection is None:
            raise AppServerRuntimeError("activity detail is unavailable")
        detail = activity_detail(projection.raw, final=projection.final)
        if detail is None:
            raise AppServerRuntimeError("activity detail is unavailable")
        return {"item": public_id, "detail": detail}

    async def _thread_lifecycle(self, method: str, thread_id: str) -> dict[str, Any]:
        if method not in {"thread/archive", "thread/unarchive"}:
            raise AppServerRuntimeError("unsupported Codex thread lifecycle action")
        clean_thread_id = str(thread_id or "").strip()
        if not clean_thread_id or len(clean_thread_id) > 160 or "\x00" in clean_thread_id:
            raise AppServerRuntimeError("invalid Codex thread id")
        result = await self._require_control_client().rpc(method, {"threadId": clean_thread_id})
        return {"ok": True, "result": result if isinstance(result, Mapping) else {}}

    async def _compat_rpc(self, method: str, params: dict[str, Any]) -> Any:
        selected_method = str(method or "").strip()
        if selected_method not in COMPAT_RPC_METHODS:
            raise AppServerRuntimeError("unsupported App Server compatibility request")
        if not isinstance(params, dict):
            raise AppServerRuntimeError("invalid App Server compatibility request")
        return await self._require_control_client().rpc(selected_method, params)

    async def _thread_loaded(self, thread_id: str) -> bool:
        clean_thread_id = str(thread_id or "").strip()
        if not clean_thread_id:
            raise AppServerRuntimeError("Codex thread id is required")
        if self.registry.by_thread(clean_thread_id) is not None:
            return True
        result = await self._require_control_client().rpc("thread/read", {"threadId": clean_thread_id})
        thread = result.get("thread") if isinstance(result, Mapping) else None
        status = thread.get("status") if isinstance(thread, Mapping) else None
        if isinstance(status, Mapping):
            status = status.get("type")
        return str(status or "") not in {"", "notLoaded", "unloaded"}

    async def _close_session(self, name: str, *, interrupt: bool = False) -> dict[str, Any]:
        record, actor = self._require_session(name)
        interrupted: dict[str, Any] = {"interrupted": False}
        if actor.active_turn_id:
            if not interrupt:
                raise AppServerRuntimeError("active Codex App Server sessions must be interrupted before closing")
            pending = self.interactions.snapshot(name)
            if pending is not None:
                try:
                    self.interactions.cancel(
                        name,
                        client_request_id=f"close_{time.time_ns()}",
                    )
                except AppServerInteractionError as exc:
                    raise AppServerRuntimeError("pending interaction could not be cancelled") from exc
                await asyncio.sleep(0)
            interrupted = await self._interrupt(name)
            if actor.active_turn_id and not interrupted.get("forcedRecovery"):
                raise AppServerRuntimeError("Codex is still stopping; retry close in a moment")
        if any(entry[0] == name for entry in self.send_tasks.values()):
            raise AppServerRuntimeError("submitting Codex App Server sessions cannot be closed")
        if self.interactions.snapshot(name) is not None:
            raise AppServerRuntimeError("pending interactions must be resolved before closing")
        client = self.session_clients.get(name)
        unsubscribe_status = "unavailable"
        if client is not None and client.ready:
            try:
                unsubscribed = await client.rpc(
                    "thread/unsubscribe",
                    {"threadId": record.thread_id},
                    timeout=2.0,
                    overload_attempts=1,
                )
                unsubscribe_status = str(unsubscribed.get("status") or "") if isinstance(unsubscribed, Mapping) else ""
            except AppServerError:
                pass
        try:
            await self.session_supervisor.stop_worker(
                name,
                record.worker_id,
                client=client,
            )
        except AppServerUnavailable as exc:
            raise AppServerRuntimeError("Codex App Server worker did not stop") from exc
        self._publish(record, ActorEvent("session.closed", payload={"session": name}))
        self.actors.pop(name, None)
        self.session_send_locks.pop(name, None)
        self.pending_session_notifications.pop(name, None)
        self.registry.remove(name)
        return {
            "closed": True,
            "session": name,
            "threadId": record.thread_id,
            "interrupted": bool(interrupt and interrupted.get("interrupted")),
            "unsubscribeStatus": unsubscribe_status,
            "writerRelease": "immediate",
        }

    async def _respond_interaction(
        self,
        name: str,
        *,
        interaction_id: str,
        option_id: str,
        answers: Mapping[str, Any] | None,
        action: str,
        client_request_id: str,
    ) -> dict[str, Any]:
        record, actor = self._require_session(name)
        timeline_event = self.command_timeline.event_for_interaction(
            self._command_owner_key(record),
            interaction_id,
        )
        selected_label = ""
        if timeline_event is not None and isinstance(actor.interaction, Mapping):
            selected = next(
                (
                    option
                    for option in actor.interaction.get("options") or []
                    if isinstance(option, Mapping) and str(option.get("id") or "") == option_id
                ),
                None,
            )
            selected_label = str(selected.get("label") or "") if isinstance(selected, Mapping) else ""
        try:
            local = await self.commands.respond(
                session=name,
                interaction_id=interaction_id,
                option_id=option_id,
                action=action,
                rpc=lambda method, params: self._session_rpc(name, method, params),
            )
            if local is not None:
                if timeline_event is not None:
                    metadata: dict[str, Any] = {}
                    if selected_label:
                        metadata["selection"] = selected_label
                    if action == "cancel":
                        metadata["action"] = "cancel"
                    updated = self.command_timeline.update(
                        str(timeline_event["id"]),
                        status="completed",
                        metadata=metadata,
                    )
                    local["commandEvent"] = self.command_timeline.public_event(updated) if updated is not None else None
                    self._publish_command_event(record, updated)
                await asyncio.sleep(0)
                return local
            if action == "cancel":
                result = self.interactions.cancel(name, client_request_id=client_request_id)
            else:
                result = self.interactions.respond(
                    name,
                    interaction_id=interaction_id,
                    option_id=option_id,
                    answers=answers,
                    client_request_id=client_request_id,
                )
            await asyncio.sleep(0)
            return result
        except (AppServerCommandError, AppServerInteractionError) as exc:
            raise AppServerRuntimeError(str(exc)) from exc

    async def _begin_command(
        self,
        name: str,
        *,
        command: str,
        client_request_id: str,
        confirmed: bool,
    ) -> dict[str, Any]:
        request_id = str(client_request_id or "").strip()
        if not CLIENT_REQUEST_RE.fullmatch(request_id):
            raise AppServerRuntimeError("invalid client request id")
        invocation = str(command or "").strip()
        identity = (name, invocation, bool(confirmed))
        cutoff = time.monotonic() - SEND_RECEIPT_TTL_SECONDS
        for key in [key for key, value in self.command_receipts.items() if value["updatedAt"] < cutoff]:
            self.command_receipts.pop(key, None)
        existing = self.command_receipts.get(request_id)
        if existing is not None:
            if existing["identity"] != identity:
                raise AppServerRuntimeError("client request id was already used for another command")
            return {**existing["result"], "duplicate": True}
        record, actor = self._require_session(name)
        if actor.interaction is not None:
            raise AppServerRuntimeError("another Codex interaction is already pending")
        owner_key = self._command_owner_key(record)
        try:
            timeline_event, timeline_duplicate = self.command_timeline.begin(
                owner_key=owner_key,
                request_id=request_id,
                invocation=invocation,
                anchor_key=actor.command_anchor_key(),
            )
        except command_timeline.CommandTimelineError as exc:
            raise AppServerRuntimeError(str(exc)) from exc
        if timeline_duplicate and timeline_event is not None:
            if timeline_event.get("status") == "failed":
                raise AppServerRuntimeError("the previous command attempt failed")
            public_event = self.command_timeline.public_event(timeline_event)
            return {
                "ok": True,
                "session": name,
                "interaction": actor.interaction,
                "interactionRevision": f"appserver:{actor.interaction_revision}",
                "changed": False,
                "resolved": timeline_event.get("status") == "completed",
                "commandState": timeline_event.get("status"),
                "commandEvent": public_event,
                "duplicate": True,
            }
        try:
            result = await self.commands.begin(
                session=name,
                thread_id=record.thread_id,
                cwd=record.cwd,
                thread=actor.thread,
                command=invocation,
                rpc=lambda method, params: self._session_rpc(name, method, params),
            )
        except Exception as exc:
            if timeline_event is not None:
                updated = self.command_timeline.update(
                    str(timeline_event["id"]),
                    status="failed",
                    error=str(exc),
                )
                self._publish_command_event(record, updated)
            if isinstance(exc, AppServerCommandError):
                raise AppServerRuntimeError(str(exc)) from exc
            raise
        if timeline_event is not None:
            interaction = result.get("interaction")
            status = "waiting" if isinstance(interaction, Mapping) else "completed"
            updated = self.command_timeline.update(
                str(timeline_event["id"]),
                status=status,
                metadata=self._command_result_metadata(invocation, actor),
                interaction_id=str(interaction.get("id") or "") if isinstance(interaction, Mapping) else "",
            )
            result["commandState"] = status
            result["commandEvent"] = self.command_timeline.public_event(updated) if updated is not None else None
            self._publish_command_event(record, updated)
        self.command_receipts[request_id] = {
            "identity": identity,
            "result": dict(result),
            "updatedAt": time.monotonic(),
        }
        return result

    @staticmethod
    def _command_owner_key(record: WebSessionRecord) -> str:
        return f"thread:{record.thread_id}"

    @staticmethod
    def _command_result_metadata(invocation: str, actor: WebSessionActor) -> dict[str, Any]:
        parts = command_timeline.command_parts(invocation)
        if parts is None:
            return {}
        name, argument = parts
        if name == "/fast":
            return {"enabled": str(actor.thread.get("serviceTier") or "") == "fast"}
        if name == "/model":
            selection = argument or str(actor.thread.get("model") or "")
            return {"selection": selection} if selection else {}
        if name == "/permissions":
            current = actor.thread.get("activePermissionProfile")
            selection = argument or (str(current.get("id") or "") if isinstance(current, Mapping) else "")
            return {"selection": selection} if selection else {}
        return {}

    def _publish_command_event(
        self,
        record: WebSessionRecord,
        event: Mapping[str, Any] | None,
    ) -> None:
        if event is None:
            return
        self._publish(
            record,
            ActorEvent(
                "command.changed",
                payload={"commandId": str(event.get("id") or ""), "status": str(event.get("status") or "")},
            ),
        )

    def _require_control_client(self) -> AsyncCodexAppServerClient:
        client = self.client
        if client is None or not client.ready:
            raise AppServerUnavailable("Codex App Server control plane is reconnecting")
        return client

    def _require_session_client(self, name: str) -> AsyncCodexAppServerClient:
        return self.session_supervisor.client(name)

    async def _session_rpc(
        self,
        name: str,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
        overload_attempts: int = 3,
    ) -> Any:
        return await self.session_supervisor.rpc(
            name,
            method,
            params,
            timeout=timeout,
            overload_attempts=overload_attempts,
        )

    def _require_session(self, name: str) -> tuple[WebSessionRecord, WebSessionActor]:
        record = self.registry.get(name)
        actor = self.actors.get(name)
        if record is None or actor is None:
            raise AppServerRuntimeError("Codex App Server session is unavailable")
        return record, actor

    def _publish(self, record: WebSessionRecord, event: ActorEvent) -> None:
        with self.condition:
            self.journal.publish(
                session_id=record.name,
                thread_id=record.thread_id,
                turn_id=event.turn_id,
                item_id=event.item_id,
                kind=event.kind,
                revision=event.revision,
                payload=self._journal_payload(event),
            )
            self.condition.notify_all()

    @staticmethod
    def _journal_payload(event: ActorEvent) -> dict[str, Any]:
        """Keep the replay journal body-free; snapshots remain authoritative in the actor."""

        if event.kind == "item.delta":
            return {
                "batchCount": int(event.payload.get("batchCount") or 1),
                "deltaChars": int(event.payload.get("deltaChars") or 0),
                "textLength": int(event.payload.get("textLength") or 0),
            }
        return {}

    def _queue_delta(self, record: WebSessionRecord, event: ActorEvent) -> None:
        if not event.item_id:
            return
        key = (record.name, event.item_id)
        previous = self.pending_delta_events.get(key)
        batch_count = 1
        delta_chars = int(event.payload.get("deltaChars") or 0)
        if previous is not None:
            previous_event = previous[1]
            batch_count += int(previous_event.payload.get("batchCount") or 1)
            delta_chars += int(previous_event.payload.get("deltaChars") or 0)
        batched = ActorEvent(
            kind="item.delta",
            turn_id=event.turn_id,
            item_id=event.item_id,
            revision=event.revision,
            payload={
                "batchCount": batch_count,
                "deltaChars": delta_chars,
                "textLength": int(event.payload.get("textLength") or 0),
            },
        )
        self.pending_delta_events[key] = (record, batched)
        if key in self.delta_flush_handles:
            return
        loop = self.loop
        if loop is None or not loop.is_running():
            self._flush_delta(key)
            return
        self.delta_flush_handles[key] = loop.call_later(
            DELTA_PUBLISH_INTERVAL_SECONDS,
            self._flush_delta,
            key,
        )

    def _flush_delta(self, key: tuple[str, str]) -> None:
        handle = self.delta_flush_handles.pop(key, None)
        if handle is not None:
            handle.cancel()
        pending = self.pending_delta_events.pop(key, None)
        if pending is not None:
            self._publish(*pending)

    def _flush_all_deltas(self) -> None:
        for key in list(self.pending_delta_events):
            self._flush_delta(key)

    def _interaction_opened(self, name: str, interaction: dict[str, Any]) -> None:
        record = self.registry.get(name)
        actor = self.actors.get(name)
        if record is None or actor is None:
            return
        self._publish(record, actor.set_interaction(interaction))

    def _interaction_closed(self, name: str, interaction_id: str) -> None:
        record = self.registry.get(name)
        actor = self.actors.get(name)
        if record is None or actor is None:
            return
        event = actor.clear_interaction(interaction_id)
        if event is not None:
            self._publish(record, event)

    def _set_state(self, state: str, error: str = "") -> None:
        with self.condition:
            self.state = state
            self.last_error = error
            self.condition.notify_all()

    @staticmethod
    def _error_class(error: BaseException) -> str:
        return error.__class__.__name__
