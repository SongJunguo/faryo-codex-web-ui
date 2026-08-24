"""Starlette application factory for the Faryo Owner."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from starlette.applications import Starlette
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request

import appserver_runtime
import owner_asgi_control
import owner_asgi_events
import owner_asgi_read
import owner_asgi_support
from faryo_cli.appserver_workers import WorkerServiceManager
from faryo_cli.diagnostics import Layout
from faryo_cli.operations import systemctl


def create_app(core: Any, config: Any, runtime: Any | None = None) -> Starlette:
    web_runtime = runtime or appserver_runtime.AppServerRuntime(
        socket_path=core.APP_SERVER_SOCKET,
        registry_path=core.APP_SERVER_REGISTRY,
        client_version=core.release_version() or "0",
        reserved_names=lambda: core.tmux_sessions(config),
        namespace_lock=core.RUNTIME_LOCK,
        command_store=core.command_timeline_store(),
        worker_manager=WorkerServiceManager(Layout.from_environment(), systemctl),
    )
    support = owner_asgi_support.OwnerAsgiSupport(core, config, web_runtime)
    streams = owner_asgi_events.OwnerEventStreams(core, support)
    reads = owner_asgi_read.OwnerReadRoutes(core, support, streams)
    controls = owner_asgi_control.OwnerControlRoutes(core, support)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        web_runtime.start()
        compat_rpc = getattr(web_runtime, "compat_rpc", None)
        if callable(compat_rpc):
            core.configure_codex_app_server_rpc(compat_rpc)
        try:
            yield
        finally:
            streams.close_active_streams()
            if callable(compat_rpc):
                core.configure_codex_app_server_rpc(None)
            web_runtime.stop()
            core.stop_codex_app_server()

    async def owner_error(_request: Request, exc: BaseException):
        return support.error_response(exc)

    async def unhandled_error(_request: Request, exc: BaseException):
        return support.error_response(exc)

    app = Starlette(
        routes=[*controls.routes(), *reads.routes()],
        lifespan=lifespan,
        exception_handlers={
            core.OwnerError: owner_error,
            Exception: unhandled_error,
        },
    )
    app.state.config = config
    app.state.web_runtime = web_runtime
    app.state.close_event_streams = streams.close_active_streams
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)
    app.add_middleware(owner_asgi_support.SecurityHeadersMiddleware)
    return app
