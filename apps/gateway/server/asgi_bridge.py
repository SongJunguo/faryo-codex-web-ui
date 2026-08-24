"""Starlette bridge-package create, append, and inject routes."""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
import secrets
import time
from typing import Any

from anyio import to_thread
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

import gateway_security
import owner_client


class BridgeOwnerError(Exception):
    def __init__(self, result: dict[str, Any], fallback: str) -> None:
        super().__init__(fallback)
        self.result = result
        self.fallback = fallback


def routes(legacy: Any, config: Any, client: owner_client.OwnerClient, support: Any) -> list[Route]:
    async def bridge_package_create(request: Request) -> Response:
        current = support.username(request)
        if not current:
            return support.json_response({"ok": False, "error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
        supplied_csrf = request.headers.get(legacy.CSRF_HEADER, "").strip()
        expected_csrf = gateway_security.csrf_token(config.cookie_secret, current, config.auth_epoch(current))
        if not supplied_csrf or not secrets.compare_digest(supplied_csrf, expected_csrf):
            return support.json_response({"ok": False, "error": "csrf required"}, HTTPStatus.FORBIDDEN)
        try:
            payload = await support.read_json_body(request, legacy.BRIDGE_PACKAGE_MAX_BYTES)
            package = await to_thread.run_sync(lambda: config.save_bridge_package(payload, current))
            return support.json_response({"ok": True, "package": package})
        except ValueError as exc:
            return support.json_response({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

    async def bridge_package_assets(request: Request) -> Response:
        current = support.username(request)
        if not current:
            return support.json_response({"ok": False, "error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
        supplied_csrf = request.headers.get(legacy.CSRF_HEADER, "").strip()
        expected_csrf = gateway_security.csrf_token(config.cookie_secret, current, config.auth_epoch(current))
        if not supplied_csrf or not secrets.compare_digest(supplied_csrf, expected_csrf):
            return support.json_response({"ok": False, "error": "csrf required"}, HTTPStatus.FORBIDDEN)
        try:
            payload = await support.read_json_body(request, legacy.BRIDGE_PACKAGE_MAX_BYTES)
            assets = config.bridge_asset_sources(payload)
            package_id = str(payload.get("package_id") or payload.get("packageId") or "")
            package = await to_thread.run_sync(
                lambda: config.append_bridge_package_assets(package_id, assets, current)
            )
            return support.json_response({"ok": True, "package": package})
        except ValueError as exc:
            return support.json_response({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

    async def bridge_inject(request: Request) -> Response:
        current = support.username(request)
        if not current:
            return support.json_response({"ok": False, "error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
        request_id = secrets.token_hex(8)
        started = time.monotonic()
        route = ""
        target = ""
        supplied_csrf = request.headers.get(legacy.CSRF_HEADER, "").strip()
        expected_csrf = gateway_security.csrf_token(config.cookie_secret, current, config.auth_epoch(current))
        if not supplied_csrf or not secrets.compare_digest(supplied_csrf, expected_csrf):
            status = HTTPStatus.FORBIDDEN
            response = support.json_response({"ok": False, "error": "csrf required"}, status)
        else:
            try:
                payload = await support.read_json_body(request, 65536)
                package_id = legacy.clean_package_id(
                    str(payload.get("package_id") or payload.get("packageId") or "")
                )
                route = str(payload.get("route") or "").strip()
                session = legacy.clean_session_id(str(payload.get("session") or ""))
                agent_session_id = legacy.clean_agent_session_id(
                    str(payload.get("agent_session_id") or "")
                )
                source = str(payload.get("source") or "")
                target = session or agent_session_id or package_id or ""
                if not package_id or route not in legacy.BACKENDS or (not session and not agent_session_id):
                    raise ValueError("package_id, route and session or agent_session_id are required")
                if agent_session_id and not source:
                    raise ValueError("source is required with agent_session_id")
                if not config.allowed_route(current, route):
                    status = HTTPStatus.FORBIDDEN
                    response = support.json_response({"ok": False, "error": "forbidden"}, status)
                else:
                    package = config.bridge_package(package_id, current)
                    if not package:
                        status = HTTPStatus.NOT_FOUND
                        response = support.json_response({"ok": False, "error": "package not found"}, status)
                    else:
                        target_package = package
                        assets = package.get("assets") if isinstance(package.get("assets"), list) else []
                        if assets:
                            delivered = []
                            for asset in assets:
                                if not isinstance(asset, dict):
                                    continue
                                path = Path(str(asset.get("path") or ""))
                                if not path.is_file() or config.bridge_root not in path.resolve().parents:
                                    raise ValueError("bridge asset is missing")
                                uploaded = await to_thread.run_sync(
                                    lambda path=path, asset=asset: client.attachment_request(
                                        route,
                                        path,
                                        str(asset.get("mime_type") or "application/octet-stream"),
                                        str(asset.get("file_name") or path.name),
                                        current,
                                    )
                                )
                                owner_path = str(uploaded.get("path") or "")
                                if not uploaded.get("ok") or not owner_path:
                                    raise BridgeOwnerError(uploaded, "Owner attachment upload failed")
                                delivered_asset = dict(asset)
                                delivered_asset["source_path"] = str(path)
                                delivered_asset["path"] = owner_path
                                delivered_asset["owner_path"] = owner_path
                                delivered.append(delivered_asset)
                            target_package = dict(package)
                            target_package["assets"] = delivered
                        target_session = session
                        if not target_session:
                            resume = await to_thread.run_sync(
                                lambda: client.json_request(
                                    route,
                                    "/api/agent/resume",
                                    {
                                        "agent_session_id": agent_session_id,
                                        "source": source,
                                        "max_running": config.max_running(route),
                                    },
                                    current,
                                )
                            )
                            if not resume.get("ok"):
                                status = support.upstream_status(resume)
                                response = support.json_response(
                                    support.forwarded_error(resume, status, "Owner resume failed"),
                                    status,
                                )
                                target_session = ""
                            else:
                                target_session = legacy.clean_session_id(
                                    str(resume.get("session") or "")
                                ) or ""
                                if not target_session:
                                    status = HTTPStatus.BAD_GATEWAY
                                    response = support.json_response(
                                        {"ok": False, "error": "owner did not return target session"},
                                        status,
                                    )
                        if target_session:
                            sent = await to_thread.run_sync(
                                lambda: client.json_request(
                                    route,
                                    "/api/send",
                                    {
                                        "session": target_session,
                                        "text": legacy.bridge_prompt_text(target_package),
                                    },
                                    current,
                                )
                            )
                            if not sent.get("ok"):
                                status = support.upstream_status(sent)
                                response = support.json_response(
                                    support.forwarded_error(sent, status, "Owner inject failed"),
                                    status,
                                )
                            else:
                                package["status"] = "injected"
                                package["target"] = {
                                    "route": route,
                                    "session": target_session,
                                    "agentSessionId": agent_session_id or "",
                                    "source": source,
                                }
                                config.update_bridge_package(package)
                                target = target_session
                                status = HTTPStatus.OK
                                response = support.json_response({
                                    "ok": True,
                                    "redirect": f"/{route}/?session={target_session}",
                                    "package": package,
                                })
            except BridgeOwnerError as exc:
                status = support.upstream_status(exc.result)
                response = support.json_response(
                    support.forwarded_error(exc.result, status, exc.fallback),
                    status,
                )
            except ValueError as exc:
                status = HTTPStatus.BAD_REQUEST
                response = support.json_response({"ok": False, "error": str(exc)}, status)
        response.headers["X-Faryo-Request-Id"] = request_id
        support.append_audit(
            username_value=current,
            route=route,
            action="file-inject",
            target=target,
            request_id=request_id,
            status=status,
            started=started,
        )
        return response

    return [
        Route("/api/bridge-packages", bridge_package_create, methods=["POST"]),
        Route("/api/bridge-package-assets", bridge_package_assets, methods=["POST"]),
        Route("/api/bridge-inject", bridge_inject, methods=["POST"]),
    ]
