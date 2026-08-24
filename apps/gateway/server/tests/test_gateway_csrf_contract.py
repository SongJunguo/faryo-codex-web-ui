#!/usr/bin/env python3
"""Gateway CSRF security-boundary regression tests."""

from __future__ import annotations

import http.client
import importlib.util
import json
import re
import socket
import threading
import time
import unittest
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

import uvicorn

REPO_ROOT = Path(__file__).resolve().parents[4]
SERVER_PATH = REPO_ROOT / "apps" / "gateway" / "server" / "server.py"

spec = importlib.util.spec_from_file_location("faryo_gateway_server", SERVER_PATH)
gateway = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(gateway)

import asgi_agents
import asgi_app
import gateway_security
import owner_client


class StubConfig:
    def __init__(self, owner_route: str):
        self.cookie_secret = b"test-cookie-secret"
        self.mcp_user = "tester"
        self.users = {"tester": {"auth_epoch": 7, "routes": [owner_route]}}
        self.owner_tokens = {owner_route: "owner-token"}
        self.mcp_token = ""
        self.mcp_cors_origin = ""
        self.icp_record = ""
        self.bridge_root = Path("/nonexistent")
        self.bridge_create_calls = 0
        self.audit_calls: list[dict[str, Any]] = []
        self.updated_packages: list[dict[str, Any]] = []

    def auth_epoch(self, username: str) -> int:
        return int(self.users[username].get("auth_epoch") or 0)

    def user_routes(self, username: str) -> list[str]:
        return list(self.users[username]["routes"])

    def allowed_route(self, username: str, route: str) -> bool:
        return route in self.user_routes(username)

    def owner_token(self, route: str) -> str:
        return self.owner_tokens[route]

    def file_inbox_root(self, username: str, route: str) -> None:
        return None

    def workspace_root(self, username: str, route: str) -> None:
        return None

    def max_running(self, route: str) -> int:
        return 8

    def save_bridge_package(self, payload: dict[str, Any], username: str) -> dict[str, Any]:
        self.bridge_create_calls += 1
        return {"id": "pkg-test", "owner": username, "title": payload.get("title") or "test", "status": "pending"}

    def list_bridge_packages(self, username: str, status: str | None = None) -> list[dict[str, Any]]:
        return []

    def bridge_asset_sources(self, payload: dict[str, Any]) -> list[Any]:
        return []

    def append_bridge_package_assets(self, package_id: str, assets: list[Any], username: str) -> dict[str, Any]:
        return {"id": package_id, "owner": username, "assets": assets, "status": "pending"}

    def append_control_audit(self, **values: Any) -> None:
        self.audit_calls.append(values)

    def control_activity(self, username: str, limit: int = 30) -> list[dict[str, Any]]:
        return [{"time": "2026-01-01T00:00:00Z", "route": self.users[username]["routes"][0], "action": "send", "target": "t_deadbeefdeadbeef", "result": "success", "http": 200, "durationMs": 1, "idempotent": False}][:limit]

    def revoke_sessions(self, username: str) -> None:
        self.users[username]["auth_epoch"] += 1

    def bridge_package(self, package_id: str, username: str) -> dict[str, Any] | None:
        return {"id": package_id, "owner": username, "title": "fixture", "status": "pending", "assets": []}

    def update_bridge_package(self, package: dict[str, Any]) -> None:
        self.updated_packages.append(package)


class OwnerHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
        self.__class__.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
        payload = {"ok": True, "path": self.path}
        status = HTTPStatus.OK
        try:
            request_payload = json.loads(body.decode("utf-8")) if body else {}
        except json.JSONDecodeError:
            request_payload = {}
        if self.path.startswith("/api/agent-session/") and request_payload.get("agent_session_id") == "thread-active":
            payload = {"ok": False, "error": "active agent sessions cannot be archived"}
            status = HTTPStatus.CONFLICT
        if self.path == "/api/agent-session/archive":
            if status == HTTPStatus.OK:
                payload.update({"archived": True, "duplicate": False})
        elif self.path == "/api/agent-session/unarchive":
            if status == HTTPStatus.OK:
                payload.update({"archived": False, "duplicate": False})
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class GatewayCsrfContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.route = "lab"
        OwnerHandler.requests.clear()
        self.owner = ThreadingHTTPServer(("127.0.0.1", 0), OwnerHandler)
        self.owner_thread = threading.Thread(target=self.owner.serve_forever, daemon=True)
        self.owner_thread.start()
        self.original_backends = dict(gateway.BACKENDS)
        gateway.BACKENDS[self.route] = ("127.0.0.1", self.owner.server_address[1], "Lab")
        self.config = StubConfig(self.route)
        self.gateway_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.gateway_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.gateway_socket.bind(("127.0.0.1", 0))
        self.gateway_socket.listen(128)
        self.base = ("127.0.0.1", self.gateway_socket.getsockname()[1])
        self.server = uvicorn.Server(uvicorn.Config(
            asgi_app.create_app(gateway, self.config),
            log_level="error",
            access_log=False,
            lifespan="off",
        ))
        self.gateway_thread = threading.Thread(
            target=self.server.run,
            kwargs={"sockets": [self.gateway_socket]},
            daemon=True,
        )
        self.gateway_thread.start()
        for _attempt in range(100):
            if self.server.started:
                break
            time.sleep(0.02)
        if not self.server.started:
            raise RuntimeError("ASGI CSRF test server did not start")
        self.cookie = self.auth_cookie("tester")

    def tearDown(self) -> None:
        self.server.should_exit = True
        self.gateway_thread.join(timeout=5)
        self.owner.shutdown()
        self.owner.server_close()
        self.owner_thread.join(timeout=2)
        gateway.BACKENDS.clear()
        gateway.BACKENDS.update(self.original_backends)

    def auth_cookie(self, username: str) -> str:
        codec = gateway_security.SessionCookieCodec(
            self.config.cookie_secret,
            name=gateway.COOKIE_NAME,
            max_age=gateway.COOKIE_MAX_AGE,
            same_site=gateway.COOKIE_SAME_SITE,
        )
        return codec.issue(username, self.config.auth_epoch(username)).split(";", 1)[0]

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        include_cookie: bool = True,
    ) -> tuple[int, dict[str, Any]]:
        payload = json.dumps(body or {}).encode("utf-8") if body is not None else None
        headers = {"Cookie": self.cookie} if include_cookie else {}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        headers.update(extra_headers or {})
        conn = http.client.HTTPConnection(*self.base, timeout=5)
        try:
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, json.loads(raw.decode("utf-8") or "{}")
        finally:
            conn.close()

    def csrf_token(self) -> str:
        status, data = self.request("GET", "/api/csrf")
        self.assertEqual(status, HTTPStatus.OK)
        return str(data["csrf"])

    def test_gateway_responses_include_browser_security_headers(self) -> None:
        conn = http.client.HTTPConnection(*self.base, timeout=5)
        try:
            conn.request("GET", "/api/csrf", headers={"Cookie": self.cookie})
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, HTTPStatus.OK)
            self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
            self.assertEqual(resp.getheader("X-Frame-Options"), "DENY")
            self.assertEqual(resp.getheader("Referrer-Policy"), "no-referrer")
            self.assertEqual(
                resp.getheader("Permissions-Policy"),
                "camera=(), microphone=(), geolocation=(), fullscreen=(self)",
            )
            self.assertEqual(resp.getheader("Strict-Transport-Security"), "max-age=31536000")
            csp = resp.getheader("Content-Security-Policy") or ""
            self.assertIn("default-src 'self'", csp)
            self.assertIn("script-src-attr 'none'", csp)
            self.assertIn("object-src 'none'", csp)
        finally:
            conn.close()

    def test_pwa_manifest_is_root_scoped_and_standalone(self) -> None:
        conn = http.client.HTTPConnection(*self.base, timeout=5)
        try:
            conn.request("GET", "/manifest.json")
            resp = conn.getresponse()
            payload = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(resp.status, HTTPStatus.OK)
            self.assertEqual(payload["id"], "/")
            self.assertEqual(payload["scope"], "/")
            self.assertEqual(payload["start_url"], "/")
            self.assertEqual(payload["display"], "standalone")
            self.assertIn("Codex sessions", payload["description"])
        finally:
            conn.close()

    def test_html_page_csp_nonce_matches_inline_portal_assets(self) -> None:
        conn = http.client.HTTPConnection(*self.base, timeout=5)
        try:
            conn.request("GET", "/", headers={"Cookie": self.cookie})
            resp = conn.getresponse()
            body = resp.read().decode("utf-8")
            self.assertEqual(resp.status, HTTPStatus.OK)
            csp = resp.getheader("Content-Security-Policy") or ""
            match = re.search(r"script-src 'self' 'nonce-([^']+)'", csp)
            self.assertIsNotNone(match)
            nonce = match.group(1)
            asset_version = gateway.GATEWAY_ASSET_REVISION
            self.assertIn(f'<script id="faryoRouteLabels" type="application/json" nonce="{nonce}">', body)
            self.assertIn(f'<script src="/workbench-preact.js?v={asset_version}"></script>', body)
            self.assertIn(f'<script src="/workbench.js?v={asset_version}"></script>', body)
            self.assertIn(f'<link rel="stylesheet" href="/workbench.css?v={asset_version}">', body)
            self.assertNotIn("<style nonce=", body)
            self.assertNotIn(gateway.CSP_NONCE_PLACEHOLDER, body)
        finally:
            conn.close()

    def test_portal_exposes_explicit_file_transfer_and_launcher_controls(self) -> None:
        conn = http.client.HTTPConnection(*self.base, timeout=5)
        try:
            conn.request("GET", "/", headers={"Cookie": self.cookie})
            resp = conn.getresponse()
            body = resp.read().decode("utf-8")
        finally:
            conn.close()

        self.assertEqual(resp.status, HTTPStatus.OK)
        self.assertIn("Files to session", body)
        self.assertIn("Choose files", body)
        script = (gateway.STATIC_DIR / "workbench.js").read_text(encoding="utf-8")
        component_source = (gateway.STATIC_DIR.parents[1] / "ui" / "preact-workbench.jsx").read_text(
            encoding="utf-8"
        )
        self.assertIn("Send to…", component_source)
        self.assertIn("Start ${item.label}", component_source)
        self.assertIn("bindWorkstationPicker", script)
        self.assertIn("routes: entries", script)
        self.assertNotIn("selectNewRoute", script)
        self.assertIn('data-context-window-k="0" aria-pressed="true">Default', body)
        self.assertIn('data-context-window-k="372" aria-pressed="false">372K', body)
        self.assertIn('data-context-window-k="1000" aria-pressed="false">1M', body)
        self.assertNotIn('data-context-window-k="272"', body)
        self.assertNotIn("No handoff package", body)
        self.assertIn('class="brand" href="/" aria-label="Faryo home"', body)
        self.assertNotIn('href="/projects"', body)

    def test_external_workbench_assets_are_served_with_explicit_types(self) -> None:
        for path, content_type in (
            ("/workbench.css", "text/css; charset=utf-8"),
            ("/workbench.js", "text/javascript; charset=utf-8"),
            ("/workbench-preact.js", "text/javascript; charset=utf-8"),
            ("/workbench-preact.LICENSE.txt", "text/plain; charset=utf-8"),
        ):
            with self.subTest(path=path):
                conn = http.client.HTTPConnection(*self.base, timeout=5)
                try:
                    conn.request("GET", path)
                    resp = conn.getresponse()
                    body = resp.read()
                finally:
                    conn.close()
                self.assertEqual(resp.status, HTTPStatus.OK)
                self.assertEqual(resp.getheader("Content-Type"), content_type)
                self.assertTrue(body)

    def test_retired_projects_page_is_not_routable(self) -> None:
        connection = http.client.HTTPConnection(*self.base, timeout=5)
        try:
            connection.request("GET", "/projects", headers={"Cookie": self.cookie})
            response = connection.getresponse()
            body = response.read().decode("utf-8")
        finally:
            connection.close()

        self.assertEqual(response.status, HTTPStatus.NOT_FOUND)
        self.assertNotIn("Import Project", body)

    def test_authenticated_send_audit_never_receives_message_body(self) -> None:
        csrf = self.csrf_token()
        status, _data = self.request(
            "POST",
            f"/{self.route}/api/send",
            {"session": "session-a", "text": "private prompt body", "clientMessageId": "web-audit-123"},
            {gateway.CSRF_HEADER: csrf},
        )

        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(len(self.config.audit_calls), 1)
        audit = self.config.audit_calls[0]
        self.assertEqual((audit["route"], audit["action"], audit["target"], audit["status"]), (self.route, "send", "session-a", 200))
        self.assertNotIn("private prompt body", repr(audit))

    def test_csrf_denial_is_audited_without_reading_request_body(self) -> None:
        status, data = self.request(
            "POST",
            f"/{self.route}/api/interrupt",
            {"session": "private-session"},
        )

        self.assertEqual(status, HTTPStatus.FORBIDDEN)
        self.assertEqual(data["errorCode"], "csrf_required")
        self.assertEqual(data["errorContractVersion"], 1)
        self.assertIn("Refresh", data["recovery"])
        self.assertEqual(len(self.config.audit_calls), 1)
        audit = self.config.audit_calls[0]
        self.assertEqual((audit["action"], audit["target"], audit["status"]), ("interrupt", "", 403))
        self.assertNotIn("private-session", repr(audit))

    def test_audit_writer_failure_does_not_change_control_response(self) -> None:
        csrf = self.csrf_token()
        with mock.patch.object(self.config, "append_control_audit", side_effect=OSError("audit unavailable")):
            status, data = self.request(
                "POST",
                f"/{self.route}/api/interaction/respond",
                {"session": "session-a", "interactionId": "ix_fixture", "action": "cancel", "clientRequestId": "response-fixture"},
                {gateway.CSRF_HEADER: csrf},
            )

        self.assertEqual(status, HTTPStatus.OK)
        self.assertTrue(data["ok"])

    def test_security_activity_requires_login_and_revoke_requires_csrf(self) -> None:
        unauthenticated, _data = self.request("GET", "/api/security-activity", include_cookie=False)
        self.assertEqual(unauthenticated, HTTPStatus.UNAUTHORIZED)
        status, activity = self.request("GET", "/api/security-activity?limit=1")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(activity["entries"][0]["target"], "t_deadbeefdeadbeef")

        denied, _data = self.request("POST", "/api/auth/revoke-all", {"confirm": "revoke"})
        self.assertEqual(denied, HTTPStatus.FORBIDDEN)
        csrf = self.csrf_token()
        accepted, payload = self.request(
            "POST",
            "/api/auth/revoke-all",
            {"confirm": "revoke"},
            {gateway.CSRF_HEADER: csrf},
        )
        self.assertEqual(accepted, HTTPStatus.OK)
        self.assertTrue(payload["signedOut"])
        self.assertEqual(self.config.audit_calls[-1]["action"], "revoke-sessions")

    def test_proxy_control_action_matrix_is_audited(self) -> None:
        csrf = self.csrf_token()
        cases = (
            ("send", {"session": "session-a", "text": "anonymous"}),
            ("interrupt", {"session": "session-a"}),
            ("session/close", {"session": "session-a"}),
            ("interaction/start", {"session": "session-a", "command": "/model", "clientRequestId": "request-fixture"}),
            ("interaction/respond", {"session": "session-a", "interactionId": "ix_fixture", "action": "cancel", "clientRequestId": "response-fixture"}),
        )
        expected = ("send", "interrupt", "close", "command", "interaction")
        self.config.audit_calls.clear()

        for (tail, body), action in zip(cases, expected, strict=True):
            status, _data = self.request(
                "POST",
                f"/{self.route}/api/{tail}",
                body,
                {gateway.CSRF_HEADER: csrf},
            )
            self.assertEqual(status, HTTPStatus.OK)
            self.assertEqual(self.config.audit_calls[-1]["action"], action)
            self.assertEqual(self.config.audit_calls[-1]["target"], "session-a")

    def test_direct_control_actions_are_audited_on_success_and_failure(self) -> None:
        csrf = self.csrf_token()
        self.config.audit_calls.clear()
        cases = (
            ("/api/agent/new", {"route": self.route, "command": "codex", "client_launch_id": "web-launch-audit"}, "start", HTTPStatus.BAD_GATEWAY),
            ("/api/agent/resume", {"route": self.route, "agent_session_id": "thread-a", "source": "codex-cli"}, "resume", HTTPStatus.BAD_GATEWAY),
            ("/api/session-history/archive", {"route": self.route, "agent_session_id": "thread-a"}, "archive", HTTPStatus.OK),
            ("/api/session-history/unarchive", {"route": self.route, "agent_session_id": "thread-a"}, "unarchive", HTTPStatus.OK),
            ("/api/bridge-inject", {"package_id": "123-aabbccdd", "route": self.route, "session": "session-a"}, "file-inject", HTTPStatus.OK),
        )

        for path, body, action, expected_status in cases:
            status, _data = self.request("POST", path, body, {gateway.CSRF_HEADER: csrf})
            self.assertEqual(status, expected_status)
            self.assertEqual(self.config.audit_calls[-1]["action"], action)
            self.assertEqual(self.config.audit_calls[-1]["route"], self.route)

    def test_archive_conflict_preserves_owner_http_status_and_audit(self) -> None:
        csrf = self.csrf_token()

        status, data = self.request(
            "POST",
            "/api/session-history/archive",
            {"route": self.route, "agent_session_id": "thread-active"},
            {gateway.CSRF_HEADER: csrf},
        )

        self.assertEqual(status, HTTPStatus.CONFLICT)
        self.assertEqual(data["errorCode"], "thread_in_use")
        self.assertEqual(data["errorTitle"], "Conversation still open")
        self.assertIn("Close", data["recovery"])
        self.assertEqual(self.config.audit_calls[-1]["action"], "archive")
        self.assertEqual(self.config.audit_calls[-1]["status"], HTTPStatus.CONFLICT)

    def test_auth_cookie_defaults_to_thirty_days_host_only_and_strict(self) -> None:
        codec = gateway_security.SessionCookieCodec(
            self.config.cookie_secret,
            name=gateway.COOKIE_NAME,
            max_age=gateway.COOKIE_MAX_AGE,
            same_site=gateway.COOKIE_SAME_SITE,
        )
        cookie = codec.issue("tester", self.config.auth_epoch("tester"))

        self.assertTrue(cookie.startswith(f"{gateway.COOKIE_NAME}="))
        self.assertIn("Max-Age=2592000", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertNotIn("Domain=", cookie)

    def test_auth_cookie_honors_configured_twenty_four_hour_lifetime(self) -> None:
        codec = gateway_security.SessionCookieCodec(
            self.config.cookie_secret,
            name=gateway.COOKIE_NAME,
            max_age=24 * 60 * 60,
            same_site=gateway.COOKIE_SAME_SITE,
        )
        cookie = codec.issue("tester", self.config.auth_epoch("tester"))

        self.assertIn("Max-Age=86400", cookie)

    def test_gateway_state_change_requires_csrf(self) -> None:
        status, data = self.request("POST", "/api/bridge-packages", {"title": "blocked"})
        self.assertEqual(status, HTTPStatus.FORBIDDEN)
        self.assertEqual(data.get("error"), "csrf required")
        self.assertEqual(self.config.bridge_create_calls, 0)

    def test_gateway_state_change_accepts_valid_csrf(self) -> None:
        csrf = self.csrf_token()
        status, data = self.request("POST", "/api/bridge-packages", {"title": "allowed"}, {gateway.CSRF_HEADER: csrf})
        self.assertEqual(status, HTTPStatus.OK)
        self.assertTrue(data.get("ok"))
        self.assertEqual(self.config.bridge_create_calls, 1)

    def test_start_codex_retries_transport_failure_with_the_same_launch_id(self) -> None:
        csrf = self.csrf_token()
        launch_id = "web-generic-launch-123"
        responses = [
            {"ok": True, "activeSessions": [], "sessions": []},
            {"ok": False, "error": "owner restarting", "transportError": True, "retryable": True},
            {"ok": True, "session": "faryo1"},
        ]
        with (
            mock.patch.object(owner_client.OwnerClient, "json_request", side_effect=responses) as owner_request,
            mock.patch.object(asgi_agents, "sleep", new=mock.AsyncMock()) as sleep,
        ):
            status, data = self.request(
                "POST",
                "/api/agent/new",
                {"route": self.route, "command": "codex", "client_launch_id": launch_id},
                {gateway.CSRF_HEADER: csrf},
            )

        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(data.get("session"), "faryo1")
        self.assertEqual(data.get("clientLaunchId"), launch_id)
        self.assertEqual(data.get("redirect"), f"/{self.route}/?session=faryo1")
        self.assertEqual(owner_request.call_count, 3)
        first_launch = owner_request.call_args_list[1].args[2]
        retried_launch = owner_request.call_args_list[2].args[2]
        self.assertEqual(first_launch["client_launch_id"], launch_id)
        self.assertEqual(retried_launch, first_launch)
        sleep.assert_called_once_with(0.25)

    def test_new_session_preserves_owner_error_contract_and_status(self) -> None:
        csrf = self.csrf_token()
        responses = [
            {"ok": True, "activeSessions": [], "sessions": []},
            {
                "ok": False,
                "httpStatus": 409,
                "errorContractVersion": 1,
                "errorCode": "agent_limit",
                "errorTitle": "Agent limit reached",
                "error": "This workstation is already running the configured number of Codex sessions.",
                "recovery": "Close an unused running session and retry.",
                "retryable": False,
            },
        ]
        with mock.patch.object(owner_client.OwnerClient, "json_request", side_effect=responses):
            status, data = self.request(
                "POST",
                "/api/agent/new",
                {"route": self.route, "command": "codex", "client_launch_id": "web-contract-launch-123"},
                {gateway.CSRF_HEADER: csrf},
            )

        self.assertEqual(status, HTTPStatus.CONFLICT)
        self.assertEqual(data["errorCode"], "agent_limit")
        self.assertEqual(data["errorTitle"], "Agent limit reached")
        self.assertIn("Close", data["recovery"])
        self.assertFalse(data["retryable"])

    def test_owner_proxy_post_requires_gateway_csrf(self) -> None:
        status, data = self.request("POST", f"/{self.route}/api/send", {"text": "blocked"})
        self.assertEqual(status, HTTPStatus.FORBIDDEN)
        self.assertEqual(data.get("error"), "csrf required")
        self.assertEqual(OwnerHandler.requests, [])

    def test_owner_proxy_post_accepts_gateway_csrf_and_strips_it_upstream(self) -> None:
        csrf = self.csrf_token()
        status, data = self.request("POST", f"/{self.route}/api/send", {"text": "hello"}, {gateway.CSRF_HEADER: csrf})
        self.assertEqual(status, HTTPStatus.OK)
        self.assertTrue(data.get("ok"))
        self.assertEqual(len(OwnerHandler.requests), 1)
        self.assertEqual(OwnerHandler.requests[0]["path"], "/api/send")
        self.assertNotIn(gateway.CSRF_HEADER, OwnerHandler.requests[0]["headers"])

    def test_login_rate_key_ignores_spoofable_forwarded_for(self) -> None:
        self.assertEqual(
            gateway_security.login_rate_key("127.0.0.1", "203.0.113.7"),
            "203.0.113.7",
        )
        self.assertEqual(
            gateway_security.login_rate_key("198.51.100.10", "203.0.113.7"),
            "198.51.100.10",
        )

if __name__ == "__main__":
    unittest.main()
