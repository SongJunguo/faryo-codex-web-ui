"""Per-session App Server clients, worker recovery, and circuit isolation."""

from __future__ import annotations

import asyncio
from pathlib import Path
import time
from typing import Any, Awaitable, Callable, Mapping, Protocol

from appserver_protocol import AppServerRequestError, AppServerUnavailable
from appserver_registry import WebSessionRecord
from appserver_requests import APPROVAL_METHODS, USER_INPUT_METHOD, declined_response
from appserver_transport import AsyncCodexAppServerClient, unix_socket_connector


RECONNECT_MIN_SECONDS = 0.2
RECONNECT_MAX_SECONDS = 8.0
FAILURE_WINDOW_SECONDS = 60.0
FAILURE_LIMIT = 3
CIRCUIT_OPEN_SECONDS = 15.0
PROBE_INTERVAL_SECONDS = 5.0
PROBE_SILENCE_SECONDS = 30.0
PROBE_TIMEOUT_SECONDS = 5.0
PROBE_FAILURE_LIMIT = 2


class AppServerWorkerManager(Protocol):
    def socket_path(self, worker_id: str) -> Path: ...

    def start(self, worker_id: str, *, timeout: float = 12.0) -> Path: ...

    def stop(self, worker_id: str, *, timeout: float = 12.0) -> None: ...

    def restart(self, worker_id: str, *, timeout: float = 12.0) -> Path: ...


class FactoryWorkerManager:
    """Socket identity shim used only with injected in-memory test clients."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def socket_path(self, worker_id: str) -> Path:
        return self.root / f"{worker_id}.sock"

    def start(self, worker_id: str, *, timeout: float = 12.0) -> Path:
        del timeout
        return self.socket_path(worker_id)

    def stop(self, worker_id: str, *, timeout: float = 12.0) -> None:
        del worker_id, timeout

    def restart(self, worker_id: str, *, timeout: float = 12.0) -> Path:
        del timeout
        return self.socket_path(worker_id)


NotificationCallback = Callable[
    [str, AsyncCodexAppServerClient, str, dict[str, Any]],
    Awaitable[None],
]
ServerRequestCallback = Callable[
    [str, str, dict[str, Any]],
    Awaitable[dict[str, Any]],
]
StateCallback = Callable[[str, str, bool], None]
HydrateCallback = Callable[[str, Mapping[str, Any]], Awaitable[None]]
ErrorCallback = Callable[[BaseException], None]
RecordCallback = Callable[[str], WebSessionRecord | None]
ProbeRequiredCallback = Callable[[str], bool]


class AppServerSessionSupervisor:
    def __init__(
        self,
        *,
        client_version: str,
        worker_manager: AppServerWorkerManager,
        record: RecordCallback,
        notification: NotificationCallback,
        server_request: ServerRequestCallback,
        state_changed: StateCallback,
        hydrated: HydrateCallback,
        error_observed: ErrorCallback,
        probe_required: ProbeRequiredCallback,
        client_factory: Callable[[Callable[..., Any], Callable[..., Any]], Any] | None = None,
    ) -> None:
        self.client_version = client_version
        self.worker_manager = worker_manager
        self.record = record
        self.notification = notification
        self.server_request = server_request
        self.state_changed = state_changed
        self.hydrated = hydrated
        self.error_observed = error_observed
        self.probe_required = probe_required
        self.client_factory = client_factory
        self.clients: dict[str, AsyncCodexAppServerClient] = {}
        self.reconnect_tasks: dict[str, asyncio.Task[None]] = {}
        self.restart_requested: set[str] = set()
        self.failures: dict[str, list[float]] = {}
        self.circuit_until: dict[str, float] = {}
        self.stopping = False
        self.monitor_task: asyncio.Task[None] | None = None
        self.last_event_at: dict[str, float] = {}
        self.last_probe_at: dict[str, float] = {}
        self.probe_failures: dict[str, int] = {}
        self.probe_unsupported: set[str] = set()

    def begin(self) -> None:
        self.stopping = False

    def request_stop(self) -> None:
        self.stopping = True

    def start_monitor(self) -> None:
        if self.monitor_task is not None and not self.monitor_task.done():
            return
        self.monitor_task = asyncio.create_task(
            self._monitor(),
            name="faryo-appserver-worker-monitor",
        )

    @property
    def pending_count(self) -> int:
        return sum(int(getattr(client, "pending_count", 0)) for client in self.clients.values())

    @property
    def open_circuit_count(self) -> int:
        now = time.monotonic()
        return sum(1 for deadline in self.circuit_until.values() if deadline > now)

    @property
    def rpc_diagnostics(self) -> dict[str, Any]:
        in_flight = 0
        in_flight_classes: dict[str, int] = {}
        terminal_counts: dict[str, int] = {}
        for client in self.clients.values():
            diagnostics = getattr(client, "rpc_diagnostics", {})
            if not isinstance(diagnostics, Mapping):
                continue
            in_flight += int(diagnostics.get("inFlight") or 0)
            for key, value in dict(diagnostics.get("inFlightClasses") or {}).items():
                in_flight_classes[str(key)] = in_flight_classes.get(str(key), 0) + int(value or 0)
            for key, value in dict(diagnostics.get("terminalCounts") or {}).items():
                terminal_counts[str(key)] = terminal_counts.get(str(key), 0) + int(value or 0)
        return {
            "inFlight": in_flight,
            "inFlightClasses": dict(sorted(in_flight_classes.items())),
            "terminalCounts": dict(sorted(terminal_counts.items())),
        }

    def client(self, name: str) -> AsyncCodexAppServerClient:
        if self.circuit_until.get(name, 0.0) > time.monotonic():
            raise AppServerUnavailable("Codex App Server worker is reconnecting after repeated failures")
        record = self.record(name)
        if record is None or record.worker_state not in {"ready", "degraded"}:
            raise AppServerUnavailable("Codex App Server worker is reconnecting")
        client = self.clients.get(name)
        if client is None or not client.ready:
            raise AppServerUnavailable("Codex App Server worker is reconnecting")
        return client

    def _make_client(self, name: str, worker_id: str) -> AsyncCodexAppServerClient:
        holder: dict[str, AsyncCodexAppServerClient] = {}

        async def notification(method: str, params: dict[str, Any]) -> None:
            client = holder.get("client")
            if client is not None:
                if self.clients.get(name) is client:
                    self.last_event_at[name] = time.monotonic()
                    if self.probe_failures.pop(name, 0):
                        self.state_changed(name, "ready", False)
                await self.notification(name, client, method, params)

        async def disconnected(error: BaseException) -> None:
            client = holder.get("client")
            if client is not None:
                self.disconnected(name, client, error)

        client = (
            self.client_factory(notification, disconnected)
            if self.client_factory is not None
            else AsyncCodexAppServerClient(
                connector=lambda: unix_socket_connector(self.worker_manager.socket_path(worker_id)),
                client_version=self.client_version,
                notification_handler=notification,
                disconnect_handler=disconnected,
            )
        )
        holder["client"] = client
        if hasattr(client, "register_server_request"):
            for method in sorted({*APPROVAL_METHODS, USER_INPUT_METHOD}):
                client.register_server_request(
                    method,
                    lambda params, selected=method, source=client: self._handle_server_request(
                        name,
                        source,
                        selected,
                        params,
                    ),
                )
        return client

    async def _handle_server_request(
        self,
        name: str,
        source_client: AsyncCodexAppServerClient,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        if self.clients.get(name) is not source_client:
            return declined_response(method, params)
        return await self.server_request(name, method, params)

    async def open_client(
        self,
        name: str,
        worker_id: str,
        *,
        restart: bool = False,
    ) -> AsyncCodexAppServerClient:
        action = self.worker_manager.restart if restart else self.worker_manager.start
        await asyncio.to_thread(action, worker_id, timeout=12.0)
        client = self._make_client(name, worker_id)
        try:
            await client.connect()
        except BaseException:
            await client.close()
            raise
        previous = self.clients.get(name)
        self.clients[name] = client
        if previous is not None and previous is not client:
            await previous.close()
        return client

    def restore_all(self, records: list[WebSessionRecord]) -> None:
        for record in records:
            client = self.clients.get(record.name)
            if client is not None and client.ready:
                continue
            existing = self.reconnect_tasks.get(record.name)
            if existing is not None and not existing.done():
                continue
            self.state_changed(record.name, "starting", False)
            task = asyncio.create_task(
                self._restore_record(record),
                name=f"faryo-worker-restore-{record.name}",
            )
            self.reconnect_tasks[record.name] = task
            task.add_done_callback(
                lambda completed, selected=record.name: self._task_done(selected, completed)
            )

    async def _restore_record(self, record: WebSessionRecord) -> None:
        try:
            client = await self.open_client(record.name, record.worker_id)
            await self._resume(record, client)
            return
        except Exception:
            self.state_changed(record.name, "degraded", False)
        await self._recover(record.name, self.clients.get(record.name))

    async def _resume(
        self,
        record: WebSessionRecord,
        client: AsyncCodexAppServerClient,
    ) -> None:
        result = await client.rpc("thread/resume", {"threadId": record.thread_id})
        thread = result.get("thread") if isinstance(result, dict) else None
        resumed_id = str(thread.get("id") or "") if isinstance(thread, Mapping) else ""
        if resumed_id != record.thread_id:
            raise AppServerUnavailable("Codex App Server resumed an unexpected thread")
        await self.hydrated(record.name, thread)
        self.last_event_at[record.name] = time.monotonic()
        self.probe_failures.pop(record.name, None)
        self.state_changed(record.name, "ready", True)
        self.failures.pop(record.name, None)
        self.circuit_until.pop(record.name, None)

    def disconnected(
        self,
        name: str,
        client: AsyncCodexAppServerClient,
        error: BaseException,
    ) -> None:
        if self.stopping or self.clients.get(name) is not client:
            return
        self.state_changed(name, "reconnecting", False)
        self._schedule_recovery(name, client, error)

    def request_restart(
        self,
        name: str,
        client: AsyncCodexAppServerClient,
        error: BaseException,
    ) -> None:
        if self.stopping or self.clients.get(name) is not client:
            return
        self.state_changed(name, "reconnecting", False)
        self._schedule_recovery(name, client, error, restart=True)

    def _schedule_recovery(
        self,
        name: str,
        client: AsyncCodexAppServerClient,
        error: BaseException,
        *,
        restart: bool = False,
    ) -> None:
        if restart:
            self.restart_requested.add(name)
        existing = self.reconnect_tasks.get(name)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._recover(name, client),
            name=f"faryo-worker-reconnect-{name}",
        )
        self.reconnect_tasks[name] = task
        task.add_done_callback(lambda completed, selected=name: self._task_done(selected, completed))
        self.error_observed(error)

    def _task_done(self, name: str, task: asyncio.Task[None]) -> None:
        if self.reconnect_tasks.get(name) is task:
            self.reconnect_tasks.pop(name, None)

    async def _recover(
        self,
        name: str,
        failed_client: AsyncCodexAppServerClient | None,
    ) -> None:
        delay = RECONNECT_MIN_SECONDS
        attempts = 0
        while not self.stopping:
            record = self.record(name)
            if record is None:
                return
            circuit_delay = max(0.0, self.circuit_until.get(name, 0.0) - time.monotonic())
            if circuit_delay:
                await asyncio.sleep(circuit_delay)
                if self.stopping:
                    return
            try:
                restart = name in self.restart_requested
                self.restart_requested.discard(name)
                client = await self.open_client(name, record.worker_id, restart=restart)
                if client is failed_client:
                    raise AppServerUnavailable("App Server worker client was not replaced")
                await self._resume(record, client)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempts += 1
                self.error_observed(exc)
                circuit_open = self._record_failure(name)
                if attempts >= 3 or circuit_open:
                    self.state_changed(name, "degraded", False)
                await asyncio.sleep(delay)
                delay = min(RECONNECT_MAX_SECONDS, delay * 1.8)

    def _record_failure(self, name: str) -> bool:
        now = time.monotonic()
        cutoff = now - FAILURE_WINDOW_SECONDS
        failures = [value for value in self.failures.get(name, []) if value >= cutoff]
        failures.append(now)
        self.failures[name] = failures
        if len(failures) < FAILURE_LIMIT:
            return False
        self.circuit_until[name] = now + CIRCUIT_OPEN_SECONDS
        self.failures[name] = []
        return True

    async def rpc(
        self,
        name: str,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
        overload_attempts: int = 3,
    ) -> Any:
        client = self.client(name)
        try:
            result = await client.rpc(
                method,
                params,
                timeout=timeout,
                overload_attempts=overload_attempts,
            )
            if self.probe_failures.pop(name, 0):
                self.state_changed(name, "ready", False)
            return result
        except AppServerUnavailable as exc:
            message = str(exc).lower()
            if any(marker in message for marker in ("timed out", "write failed", "transport", "closed")):
                self.request_restart(name, client, exc)
            raise

    async def _monitor(self) -> None:
        try:
            while not self.stopping:
                await asyncio.sleep(PROBE_INTERVAL_SECONDS)
                await self.probe_once()
        except asyncio.CancelledError:
            return

    async def probe_once(self, *, force: bool = False) -> None:
        now = time.monotonic()
        candidates = []
        for name, client in list(self.clients.items()):
            if (
                name in self.probe_unsupported
                or not client.ready
                or not self.probe_required(name)
            ):
                continue
            if not force and now - self.last_event_at.get(name, now) < PROBE_SILENCE_SECONDS:
                continue
            if not force and now - self.last_probe_at.get(name, 0.0) < PROBE_INTERVAL_SECONDS:
                continue
            self.last_probe_at[name] = now
            candidates.append(self._probe(name, client))
        if candidates:
            await asyncio.gather(*candidates, return_exceptions=True)

    async def _probe(self, name: str, client: AsyncCodexAppServerClient) -> None:
        try:
            await client.rpc(
                "server/diagnostics",
                {},
                timeout=PROBE_TIMEOUT_SECONDS,
                overload_attempts=1,
            )
        except AppServerRequestError:
            # Any explicit JSON-RPC response proves that the worker transport is
            # responsive, even when this Codex version lacks diagnostics.
            self.probe_unsupported.add(name)
            self.probe_failures.pop(name, None)
            return
        except Exception as exc:
            if self.clients.get(name) is not client:
                return
            failures = self.probe_failures.get(name, 0) + 1
            self.probe_failures[name] = failures
            self.state_changed(name, "degraded", False)
            self.error_observed(exc)
            if failures >= PROBE_FAILURE_LIMIT:
                self.request_restart(name, client, exc)
            return
        if self.clients.get(name) is client:
            if self.probe_failures.pop(name, 0):
                self.state_changed(name, "ready", False)

    async def discard_unregistered(
        self,
        name: str,
        worker_id: str,
        client: AsyncCodexAppServerClient | None,
    ) -> None:
        if self.clients.get(name) is client:
            self.clients.pop(name, None)
        if client is not None:
            await client.close()
        try:
            await asyncio.to_thread(self.worker_manager.stop, worker_id, timeout=12.0)
        except Exception:
            pass

    async def stop_worker(
        self,
        name: str,
        worker_id: str,
        *,
        client: AsyncCodexAppServerClient | None = None,
    ) -> None:
        self.state_changed(name, "stopping", False)
        reconnect = self.reconnect_tasks.pop(name, None)
        if reconnect is not None:
            reconnect.cancel()
            await asyncio.gather(reconnect, return_exceptions=True)
        selected_client = client or self.clients.get(name)
        if selected_client is not None and self.clients.get(name) is selected_client:
            self.clients.pop(name, None)
        if selected_client is not None:
            await selected_client.close()
        try:
            await asyncio.to_thread(self.worker_manager.stop, worker_id, timeout=12.0)
        except Exception as exc:
            self.state_changed(name, "degraded", False)
            raise AppServerUnavailable("Codex App Server worker did not stop") from exc
        self.restart_requested.discard(name)
        self.failures.pop(name, None)
        self.circuit_until.pop(name, None)
        self.last_event_at.pop(name, None)
        self.last_probe_at.pop(name, None)
        self.probe_failures.pop(name, None)
        self.probe_unsupported.discard(name)

    async def close_connections(self) -> None:
        self.stopping = True
        monitor = self.monitor_task
        self.monitor_task = None
        if monitor is not None:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        tasks = list(self.reconnect_tasks.values())
        self.reconnect_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        clients = list(self.clients.values())
        self.clients.clear()
        self.restart_requested.clear()
        if clients:
            await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)
