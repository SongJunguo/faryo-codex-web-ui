"""Asynchronous, bidirectional Codex App Server client over a private socket."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import inspect
import json
from pathlib import Path
import random
from typing import Any, Protocol

from appserver_protocol import (
    AppServerProtocolError,
    AppServerRequestError,
    AppServerUnavailable,
    METHOD_NOT_FOUND_ERROR_CODE,
    OVERLOADED_ERROR_CODE,
    ProtocolMessageKind,
    decode_wire_message,
    error_from_response,
    error_message,
    response_message,
)


DEFAULT_RPC_TIMEOUT = 15.0
DEFAULT_CONNECT_TIMEOUT = 8.0
DEFAULT_NOTIFICATION_QUEUE = 512


def rpc_method_class(method: str) -> str:
    """Return a body-free, stable category for protocol diagnostics."""

    value = str(method or "")
    if value.startswith("turn/"):
        return "turn"
    if value.startswith("item/"):
        return "interaction"
    if value.startswith("thread/"):
        return "thread"
    if value.startswith(("account/", "model/", "permissionProfile/")):
        return "catalog"
    if value.startswith("server/") or value == "initialize":
        return "control"
    return "other"


class MessageSocket(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


Connector = Callable[[], Awaitable[MessageSocket]]
NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None] | None]
ServerRequestHandler = Callable[[dict[str, Any]], Awaitable[Any] | Any]
DisconnectHandler = Callable[[BaseException], Awaitable[None] | None]


async def unix_socket_connector(
    socket_path: Path,
    *,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
) -> MessageSocket:
    """Connect to Codex's Unix-domain WebSocket without exposing a TCP listener."""

    from websockets.asyncio.client import unix_connect

    return await unix_connect(
        path=str(socket_path),
        uri="ws://localhost/",
        open_timeout=connect_timeout,
        close_timeout=2,
        ping_interval=20,
        ping_timeout=20,
        max_queue=64,
        max_size=64 * 1024 * 1024,
        compression=None,
    )


class AsyncCodexAppServerClient:
    """One connection with one reader and many concurrent JSON-RPC requests."""

    def __init__(
        self,
        *,
        connector: Connector,
        client_version: str,
        notification_handler: NotificationHandler | None = None,
        disconnect_handler: DisconnectHandler | None = None,
        rpc_timeout: float = DEFAULT_RPC_TIMEOUT,
        notification_queue_size: int = DEFAULT_NOTIFICATION_QUEUE,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        if notification_queue_size < 1:
            raise ValueError("notification_queue_size must be positive")
        self.connector = connector
        self.client_version = client_version or "0"
        self.notification_handler = notification_handler
        self.disconnect_handler = disconnect_handler
        self.rpc_timeout = rpc_timeout
        self.random_value = random_value
        self.server_request_handlers: dict[str, ServerRequestHandler] = {}
        self.unknown_notifications: dict[str, int] = {}
        self._notification_queue_size = notification_queue_size
        self._socket: MessageSocket | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._notification_task: asyncio.Task[None] | None = None
        self._server_request_tasks: set[asyncio.Task[None]] = set()
        self._notification_queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue(
            maxsize=notification_queue_size,
        )
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._pending_classes: dict[int, str] = {}
        self._rpc_terminal_counts: dict[str, int] = {}
        self._last_rpc_terminal = {"class": "", "state": ""}
        self._next_request_id = 0
        self._send_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._closing = False
        self.initialize_result: dict[str, Any] = {}
        self.experimental_api = False

    @property
    def ready(self) -> bool:
        return self._ready.is_set() and self._socket is not None

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def rpc_diagnostics(self) -> dict[str, Any]:
        in_flight_classes: dict[str, int] = {}
        for method_class in self._pending_classes.values():
            in_flight_classes[method_class] = in_flight_classes.get(method_class, 0) + 1
        return {
            "inFlight": len(self._pending),
            "inFlightClasses": dict(sorted(in_flight_classes.items())),
            "terminalCounts": dict(sorted(self._rpc_terminal_counts.items())),
            "lastTerminal": dict(self._last_rpc_terminal),
        }

    def _finish_rpc(self, method_class: str, state: str) -> None:
        key = f"{method_class}.{state}"
        self._rpc_terminal_counts[key] = self._rpc_terminal_counts.get(key, 0) + 1
        self._last_rpc_terminal = {"class": method_class, "state": state}

    def register_server_request(self, method: str, handler: ServerRequestHandler) -> None:
        if not method:
            raise ValueError("server request method is required")
        self.server_request_handlers[method] = handler

    async def connect(self) -> dict[str, Any]:
        async with self._lifecycle_lock:
            if self.ready:
                return dict(self.initialize_result)
            self._closing = False
            if self._socket is not None or self._reader_task is not None or self._notification_task is not None:
                await self._close_locked(notify=False)
            self._notification_queue = asyncio.Queue(maxsize=self._notification_queue_size)
            socket = await self.connector()
            self._socket = socket
            self._notification_task = asyncio.create_task(
                self._notification_loop(),
                name="faryo-appserver-notifications",
            )
            self._reader_task = asyncio.create_task(
                self._reader_loop(socket),
                name="faryo-appserver-reader",
            )
            try:
                client_info = {
                    "name": "faryo",
                    "title": "Faryo",
                    "version": self.client_version,
                }
                try:
                    result = await self._rpc_once(
                        "initialize",
                        {
                            "clientInfo": client_info,
                            "capabilities": {"experimentalApi": True},
                        },
                        timeout=self.rpc_timeout,
                        require_ready=False,
                    )
                    self.experimental_api = True
                except AppServerRequestError:
                    result = await self._rpc_once(
                        "initialize",
                        {"clientInfo": client_info, "capabilities": None},
                        timeout=self.rpc_timeout,
                        require_ready=False,
                    )
                    self.experimental_api = False
                if not isinstance(result, dict):
                    raise AppServerProtocolError("initialize returned a non-object result")
                await self._send_json({"method": "initialized", "params": {}})
                self.initialize_result = dict(result)
                self._ready.set()
                return dict(result)
            except BaseException:
                await self._close_locked(notify=False)
                raise

    async def close(self) -> None:
        async with self._lifecycle_lock:
            self._closing = True
            await self._close_locked(notify=False)

    async def rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        overload_attempts: int = 3,
    ) -> Any:
        if not self.ready:
            raise AppServerUnavailable("Codex App Server is not connected")
        attempts = max(1, overload_attempts)
        for attempt in range(attempts):
            try:
                return await self._rpc_once(
                    method,
                    params or {},
                    timeout=self.rpc_timeout if timeout is None else timeout,
                    require_ready=True,
                )
            except AppServerRequestError as exc:
                if exc.code != OVERLOADED_ERROR_CODE or attempt + 1 >= attempts:
                    raise
                delay = min(1.5, 0.1 * (2**attempt)) * (0.75 + 0.5 * self.random_value())
                await asyncio.sleep(delay)
        raise AppServerUnavailable("Codex App Server retry loop ended unexpectedly")

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if not self.ready:
            raise AppServerUnavailable("Codex App Server is not connected")
        await self._send_json({"method": method, "params": params or {}})

    async def _rpc_once(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
        require_ready: bool,
    ) -> Any:
        if require_ready and not self.ready:
            raise AppServerUnavailable("Codex App Server is not connected")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        async with self._send_lock:
            socket = self._socket
            if socket is None:
                raise AppServerUnavailable("Codex App Server transport is closed")
            self._next_request_id += 1
            request_id = self._next_request_id
            self._pending[request_id] = future
            method_class = rpc_method_class(method)
            self._pending_classes[request_id] = method_class
            try:
                await socket.send(json.dumps(
                    {"id": request_id, "method": method, "params": params},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ))
            except BaseException as exc:
                self._pending.pop(request_id, None)
                self._pending_classes.pop(request_id, None)
                if not future.done():
                    future.cancel()
                self._finish_rpc(method_class, "write_failed")
                raise AppServerUnavailable("Codex App Server write failed") from exc
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=max(0.01, timeout))
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            self._pending_classes.pop(request_id, None)
            future.cancel()
            self._finish_rpc(method_class, "cancelled")
            raise
        except asyncio.TimeoutError as exc:
            self._pending.pop(request_id, None)
            self._pending_classes.pop(request_id, None)
            future.cancel()
            self._finish_rpc(method_class, "timed_out")
            raise AppServerUnavailable(f"Codex App Server request timed out: {method}") from exc

    async def _send_json(self, body: dict[str, Any]) -> None:
        async with self._send_lock:
            socket = self._socket
            if socket is None:
                raise AppServerUnavailable("Codex App Server transport is closed")
            try:
                await socket.send(json.dumps(body, ensure_ascii=False, separators=(",", ":")))
            except BaseException as exc:
                raise AppServerUnavailable("Codex App Server write failed") from exc

    async def _reader_loop(self, socket: MessageSocket) -> None:
        failure: BaseException = AppServerUnavailable("Codex App Server connection closed")
        try:
            while True:
                raw = await socket.recv()
                decoded = decode_wire_message(raw)
                body = decoded.body
                if decoded.kind in {ProtocolMessageKind.RESPONSE, ProtocolMessageKind.ERROR}:
                    request_id = body.get("id")
                    future = self._pending.pop(request_id, None)
                    method_class = self._pending_classes.pop(request_id, "other")
                    if future is None or future.done():
                        continue
                    if decoded.kind == ProtocolMessageKind.ERROR:
                        future.set_exception(error_from_response(body))
                        self._finish_rpc(method_class, "rejected")
                    else:
                        future.set_result(body.get("result"))
                        self._finish_rpc(method_class, "succeeded")
                    continue
                if decoded.kind == ProtocolMessageKind.NOTIFICATION:
                    method = str(body["method"])
                    params = body.get("params")
                    try:
                        self._notification_queue.put_nowait(
                            (method, params if isinstance(params, dict) else {})
                        )
                    except asyncio.QueueFull as exc:
                        raise AppServerProtocolError(
                            "Codex App Server notification queue overflowed"
                        ) from exc
                    continue
                task = asyncio.create_task(
                    self._handle_server_request(body),
                    name=f"faryo-appserver-request-{body.get('method', 'unknown')}",
                )
                self._server_request_tasks.add(task)
                task.add_done_callback(self._server_request_tasks.discard)
        except asyncio.CancelledError:
            return
        except BaseException as exc:
            failure = exc
        finally:
            if self._socket is socket:
                self._ready.clear()
                self._socket = None
            self._fail_pending(failure)
            notification = self._notification_task
            if notification is not None and notification is not asyncio.current_task():
                self._notification_task = None
                notification.cancel()
                await asyncio.gather(notification, return_exceptions=True)
            try:
                await socket.close()
            except Exception:
                pass
            if not self._closing and self.disconnect_handler is not None:
                await self._maybe_await(self.disconnect_handler(failure))

    async def _notification_loop(self) -> None:
        try:
            while True:
                message = await self._notification_queue.get()
                if message is None:
                    return
                method, params = message
                if self.notification_handler is None:
                    self.unknown_notifications[method] = self.unknown_notifications.get(method, 0) + 1
                    continue
                try:
                    await self._maybe_await(self.notification_handler(method, params))
                except Exception:
                    # A UI projection bug must not terminate the protocol reader.
                    self.unknown_notifications["__handler_error__"] = (
                        self.unknown_notifications.get("__handler_error__", 0) + 1
                    )
        except asyncio.CancelledError:
            return

    async def _handle_server_request(self, body: dict[str, Any]) -> None:
        request_id = body.get("id")
        method = str(body.get("method") or "")
        params = body.get("params")
        handler = self.server_request_handlers.get(method)
        if handler is None:
            reply = error_message(
                request_id,
                METHOD_NOT_FOUND_ERROR_CODE,
                "Faryo does not support this App Server request",
            )
        else:
            try:
                result = await self._maybe_await(handler(params if isinstance(params, dict) else {}))
                reply = response_message(request_id, result)
            except AppServerRequestError as exc:
                reply = error_message(request_id, exc.code, exc.message, exc.data)
            except Exception:
                reply = error_message(request_id, -32603, "Faryo could not complete this App Server request")
        try:
            await self._send_json(reply)
        except AppServerUnavailable:
            pass

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        return await value if inspect.isawaitable(value) else value

    def _fail_pending(self, failure: BaseException) -> None:
        for request_id, future in self._pending.items():
            if not future.done():
                future.set_exception(AppServerUnavailable(str(failure)))
                self._finish_rpc(self._pending_classes.get(request_id, "other"), "disconnected")
        self._pending.clear()
        self._pending_classes.clear()

    async def _close_locked(self, *, notify: bool) -> None:
        self._ready.clear()
        self.experimental_api = False
        socket = self._socket
        self._socket = None
        if socket is not None:
            try:
                await socket.close()
            except Exception:
                pass
        reader = self._reader_task
        self._reader_task = None
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        notification = self._notification_task
        self._notification_task = None
        if notification is not None:
            notification.cancel()
            await asyncio.gather(notification, return_exceptions=True)
        tasks = list(self._server_request_tasks)
        self._server_request_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._fail_pending(AppServerUnavailable("Codex App Server client closed"))
        if notify and self.disconnect_handler is not None:
            await self._maybe_await(self.disconnect_handler(AppServerUnavailable("client closed")))
