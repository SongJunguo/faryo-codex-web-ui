from __future__ import annotations

import http.client
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import re
import socket
import sys
import tempfile
import threading
import time
from typing import Any
import unittest

import uvicorn
import bcrypt


REPO_ROOT = Path(__file__).resolve().parents[4]
SERVER_ROOT = REPO_ROOT / "apps" / "gateway" / "server"
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))
SERVER_PATH = SERVER_ROOT / "server.py"
spec = importlib.util.spec_from_file_location("faryo_gateway_contract_legacy", SERVER_PATH)
legacy = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(legacy)

import asgi_app
import gateway_security


class ContractConfig:
    def __init__(self) -> None:
        self.cookie_secret = b"contract-cookie-secret"
        self.users = {"tester": {"auth_epoch": 7, "routes": ["lab"]}}
        self.password = "contract-password-long"
        self.password_digest = bcrypt.hashpw(self.password.encode("utf-8"), bcrypt.gensalt())
        self.icp_record = ""
        self.mcp_user = "mcp"
        self.audit_calls: list[dict[str, Any]] = []
        self.packages: dict[str, dict[str, Any]] = {}
        self.bridge_root = Path("/nonexistent")
        self.mcp_token = "contract-mcp-token"
        self.mcp_cors_origin = "https://client.invalid"

    def auth_epoch(self, username: str) -> int:
        return int(self.users[username]["auth_epoch"])

    def user(self, username: str) -> dict[str, Any] | None:
        return self.users.get(username)

    def password_hash(self, username: str) -> bytes:
        return self.password_digest

    def set_password(self, username: str, password: str) -> None:
        self.password_digest = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
        self.users[username]["auth_epoch"] += 1

    def control_activity(self, username: str, limit: int = 30) -> list[dict[str, Any]]:
        return [{"time": "2026-01-01T00:00:00Z", "route": "lab", "action": "send", "target": "t_fixture", "result": "success"}][:limit]

    def list_bridge_packages(self, username: str, status: str | None = None) -> list[dict[str, Any]]:
        return list(self.packages.values())

    def user_routes(self, username: str) -> list[str]:
        return list(self.users[username]["routes"])

    def allowed_route(self, username: str, route: str) -> bool:
        return route in self.user_routes(username)

    def owner_token(self, route: str) -> str:
        return "contract-owner-token"

    def max_running(self, route: str) -> int:
        return 8

    def file_inbox_root(self, username: str, route: str) -> None:
        return None

    def workspace_root(self, username: str, route: str) -> None:
        return None

    def append_control_audit(self, **values: Any) -> None:
        self.audit_calls.append(values)

    def revoke_sessions(self, username: str) -> None:
        self.users[username]["auth_epoch"] += 1

    def save_bridge_package(self, payload: dict[str, Any], username: str) -> dict[str, Any]:
        package = {"id": "1-deadbeef", "owner": username, "title": payload.get("title") or "fixture", "status": "pending", "assets": []}
        self.packages[package["id"]] = package
        return package

    def bridge_asset_sources(self, payload: dict[str, Any]) -> list[Any]:
        return list(payload.get("attachments") or [])

    def append_bridge_package_assets(self, package_id: str, assets: list[Any], username: str) -> dict[str, Any]:
        package = self.packages[package_id]
        package["assets"].extend(assets)
        return package

    def bridge_package(self, package_id: str, username: str) -> dict[str, Any] | None:
        return self.packages.get(package_id)

    def update_bridge_package(self, package: dict[str, Any]) -> None:
        self.packages[str(package["id"])] = package


class OwnerContractFixture(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
        self.__class__.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
        payload = json.loads(body.decode("utf-8")) if body and self.path != "/api/attachment" else {}
        result = {"ok": True, "session": payload.get("session") or "", "duplicate": False}
        if self.path == "/api/agent-session/archive":
            result["archived"] = True
        elif self.path == "/api/agent-session/unarchive":
            result["archived"] = False
        elif self.path == "/api/agent/resume":
            if payload.get("agent_session_id") == "thread-needs-cwd" and not payload.get("cwd"):
                result.update({
                    "session": "",
                    "requiresWorkingDirectory": True,
                    "reason": "recorded-directory-unavailable",
                    "recordedDisplayCwd": "~/moved-project",
                })
            else:
                result["session"] = "faryo3"
        elif self.path == "/api/agent/new":
            result["session"] = "faryo4"
        elif self.path == "/api/attachment":
            result["path"] = "/inbox/fixture.png"
        data = json.dumps(result).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self.__class__.requests.append({"path": self.path, "headers": dict(self.headers), "body": b""})
        if self.path.startswith("/api/events"):
            data = b"event: status\ndata: first\n\nevent: status\ndata: second\n\n"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
        elif self.path.startswith("/api/"):
            data = json.dumps({"ok": True, "path": self.path}).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
        elif self.path.startswith("/?session="):
            data = b"<!doctype html><title>Owner fixture</title>"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
        else:
            data = b"export const fixture = true;\n"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/javascript")
            self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class AsgiReadContractTest(unittest.TestCase):
    maxDiff = None
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = ContractConfig()
        OwnerContractFixture.requests.clear()
        cls.owner_server = ThreadingHTTPServer(("127.0.0.1", 0), OwnerContractFixture)
        cls.owner_thread = threading.Thread(target=cls.owner_server.serve_forever, daemon=True)
        cls.owner_thread.start()
        cls.original_backends = dict(legacy.BACKENDS)
        legacy.BACKENDS["lab"] = ("127.0.0.1", cls.owner_server.server_address[1], "Lab")
        cls.asgi_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cls.asgi_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        cls.asgi_socket.bind(("127.0.0.1", 0))
        cls.asgi_socket.listen(128)
        cls.asgi_base = ("127.0.0.1", cls.asgi_socket.getsockname()[1])
        cls.asgi_server = uvicorn.Server(uvicorn.Config(
            asgi_app.create_app(legacy, cls.config),
            log_level="error",
            access_log=False,
            lifespan="off",
        ))
        cls.asgi_thread = threading.Thread(target=cls.asgi_server.run, kwargs={"sockets": [cls.asgi_socket]}, daemon=True)
        cls.asgi_thread.start()
        for _attempt in range(100):
            if cls.asgi_server.started:
                break
            time.sleep(0.02)
        if not cls.asgi_server.started:
            raise RuntimeError("ASGI contract server did not start")

        codec = gateway_security.SessionCookieCodec(
            cls.config.cookie_secret,
            name=legacy.COOKIE_NAME,
            max_age=legacy.COOKIE_MAX_AGE,
            same_site=legacy.COOKIE_SAME_SITE,
        )
        cls.cookie = codec.issue("tester", cls.config.auth_epoch("tester")).split(";", 1)[0]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.asgi_server.should_exit = True
        cls.asgi_thread.join(timeout=5)
        cls.owner_server.shutdown()
        cls.owner_server.server_close()
        cls.owner_thread.join(timeout=2)
        legacy.BACKENDS.clear()
        legacy.BACKENDS.update(cls.original_backends)

    def request(
        self,
        base: tuple[str, int],
        path: str,
        *,
        authenticated: bool = False,
        method: str = "GET",
        body: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        headers = {"Cookie": self.cookie} if authenticated else {}
        headers.update(extra_headers or {})
        connection = http.client.HTTPConnection(*base, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.getheaders(), response.read()
        finally:
            connection.close()

    @staticmethod
    def selected_headers(headers: list[tuple[str, str]]) -> dict[str, list[str]]:
        selected = {
            "access-control-allow-headers",
            "access-control-allow-methods",
            "access-control-allow-origin",
            "allow",
            "cache-control",
            "content-security-policy",
            "content-type",
            "location",
            "permissions-policy",
            "referrer-policy",
            "set-cookie",
            "strict-transport-security",
            "x-content-type-options",
            "x-frame-options",
            "vary",
        }
        result: dict[str, list[str]] = {}
        for name, value in headers:
            lower = name.lower()
            if lower not in selected:
                continue
            normalized = re.sub(r"nonce-[A-Za-z0-9_-]+", "nonce-<value>", value)
            if lower == "set-cookie":
                normalized = re.sub(r"(__Host-faryo_auth=)[^;]+", r"\1<signed>", normalized)
            result.setdefault(lower, []).append(normalized)
        return result

    @staticmethod
    def normalized_body(path: str, body: bytes) -> Any:
        if path in {"/manifest.json", "/api/csrf"}:
            return json.loads(body.decode("utf-8"))
        text = body.decode("utf-8", errors="replace")
        return re.sub(r"nonce=\"[A-Za-z0-9_-]+\"", 'nonce="<value>"', text)

    def assert_contract(self, path: str, *, authenticated: bool = False) -> None:
        clean_path = path.split("?", 1)[0]
        expected = {
            ("/manifest.json", False): HTTPStatus.OK,
            ("/sw.js", False): HTTPStatus.OK,
            ("/workbench.css", False): HTTPStatus.OK,
            ("/workbench-preact.js", False): HTTPStatus.OK,
            ("/workbench-preact.LICENSE.txt", False): HTTPStatus.OK,
            ("/appearance.js", False): HTTPStatus.OK,
            ("/icons/faryo-mark.png", False): HTTPStatus.OK,
            ("/favicon.ico", False): HTTPStatus.OK,
            ("/login", False): HTTPStatus.OK,
            ("/projects", False): HTTPStatus.SEE_OTHER,
            ("/projects", True): HTTPStatus.NOT_FOUND,
            ("/api/csrf", False): HTTPStatus.UNAUTHORIZED,
            ("/api/csrf", True): HTTPStatus.OK,
            ("/api/not-real", False): HTTPStatus.UNAUTHORIZED,
            ("/api/not-real", True): HTTPStatus.NOT_FOUND,
            ("/password", True): HTTPStatus.OK,
            ("/", True): HTTPStatus.OK,
            ("/logout", True): HTTPStatus.SEE_OTHER,
            ("/lab/api/status", True): HTTPStatus.OK,
            ("/lab/api/status", False): HTTPStatus.UNAUTHORIZED,
            ("/lab/api/events", True): HTTPStatus.OK,
            ("/lab/", True): HTTPStatus.OK,
            ("/lab/", False): HTTPStatus.SEE_OTHER,
            ("/lab/app.js", True): HTTPStatus.OK,
            ("/lab/owner-ui.js", True): HTTPStatus.OK,
            ("/lab/owner-ui.LICENSE.txt", True): HTTPStatus.OK,
            ("/lab/owner/changes-panel.mjs", True): HTTPStatus.OK,
        }[(clean_path, authenticated)]
        status, headers, body = self.request(self.asgi_base, path, authenticated=authenticated)
        self.assertEqual(status, expected, path)
        selected = self.selected_headers(headers)
        self.assertEqual(selected["x-content-type-options"], ["nosniff"])
        self.assertEqual(selected["strict-transport-security"], ["max-age=31536000"])
        if status == HTTPStatus.SEE_OTHER:
            self.assertTrue(selected.get("location"), path)
        if status == HTTPStatus.OK:
            self.assertTrue(body, path)
        if clean_path == "/manifest.json":
            self.assertEqual(json.loads(body), legacy.PWA_MANIFEST)
        elif clean_path == "/api/csrf" and authenticated:
            self.assertEqual(json.loads(body)["csrf"], gateway_security.csrf_token(
                self.config.cookie_secret,
                "tester",
                self.config.auth_epoch("tester"),
            ))
        elif clean_path == "/lab/api/events":
            self.assertEqual(body, b"event: status\ndata: first\n\nevent: status\ndata: second\n\n")

    def test_public_read_contracts_match(self) -> None:
        for path in (
            "/manifest.json",
            "/sw.js",
            "/workbench.css",
            "/workbench-preact.js",
            "/workbench-preact.LICENSE.txt",
            "/appearance.js",
            "/icons/faryo-mark.png",
            "/favicon.ico",
            "/login?next=%2F",
        ):
            with self.subTest(path=path):
                self.assert_contract(path)

    def test_generic_options_contract_matches(self) -> None:
        for path in ("/api/csrf", "/not-real"):
            with self.subTest(path=path):
                status, headers, body = self.request(self.asgi_base, path, method="OPTIONS")
                self.assertEqual(status, HTTPStatus.NO_CONTENT)
                self.assertEqual(body, b"")
                selected = self.selected_headers(headers)
                self.assertEqual(selected["x-content-type-options"], ["nosniff"])
                self.assertNotIn("access-control-allow-origin", selected)

    def test_asgi_head_support_is_read_only_and_keeps_auth_boundaries(self) -> None:
        cases = (
            ("/manifest.json", False, HTTPStatus.OK),
            ("/", False, HTTPStatus.SEE_OTHER),
            ("/api/csrf", False, HTTPStatus.UNAUTHORIZED),
            ("/api/csrf", True, HTTPStatus.OK),
        )
        for path, authenticated, expected in cases:
            with self.subTest(path=path, authenticated=authenticated):
                status, headers, body = self.request(
                    self.asgi_base,
                    path,
                    authenticated=authenticated,
                    method="HEAD",
                )
                self.assertEqual(status, expected)
                self.assertEqual(body, b"")
                selected = self.selected_headers(headers)
                self.assertEqual(selected["x-content-type-options"], ["nosniff"])
                self.assertEqual(selected["strict-transport-security"], ["max-age=31536000"])

    def test_unknown_page_keeps_the_inner_login_boundary(self) -> None:
        self.assert_contract("/projects")
        self.assert_contract("/projects", authenticated=True)

    def test_unknown_direct_api_get_and_post_contracts_match(self) -> None:
        self.assert_contract("/api/not-real")
        self.assert_contract("/api/not-real", authenticated=True)
        body = json.dumps({"fixture": True}).encode("utf-8")
        asgi_unauthorized = self.request(self.asgi_base, "/api/not-real", method="POST", body=body)
        self.assertEqual(asgi_unauthorized[0], HTTPStatus.UNAUTHORIZED)
        unauthorized = json.loads(asgi_unauthorized[2])
        self.assertFalse(unauthorized["ok"])
        self.assertEqual(unauthorized["error"], "unauthorized")
        self.assertEqual(unauthorized["envelopeVersion"], 1)
        self.assertEqual(unauthorized["errorContractVersion"], 1)
        self.assertEqual(unauthorized["errorCode"], "auth_required")
        self.assertFalse(unauthorized["retryable"])
        self.assertTrue(unauthorized["recovery"])
        asgi_csrf = self.request(self.asgi_base, "/api/not-real", authenticated=True, method="POST", body=body)
        self.assertEqual(asgi_csrf[0], HTTPStatus.FORBIDDEN)
        csrf_failure = json.loads(asgi_csrf[2])
        self.assertFalse(csrf_failure["ok"])
        self.assertEqual(csrf_failure["error"], "csrf required")
        self.assertEqual(csrf_failure["envelopeVersion"], 1)
        self.assertEqual(csrf_failure["errorContractVersion"], 1)
        self.assertEqual(csrf_failure["errorCode"], "csrf_required")
        self.assertFalse(csrf_failure["retryable"])
        self.assertTrue(csrf_failure["recovery"])
        csrf = gateway_security.csrf_token(self.config.cookie_secret, "tester", self.config.auth_epoch("tester"))
        headers = {legacy.CSRF_HEADER: csrf, "Content-Type": "application/json"}
        asgi_missing = self.request(
            self.asgi_base,
            "/api/not-real",
            authenticated=True,
            method="POST",
            body=body,
            extra_headers=headers,
        )
        self.assertEqual(asgi_missing[0], HTTPStatus.NOT_FOUND)
        missing = json.loads(asgi_missing[2])
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["error"], "not found")
        self.assertEqual(missing["envelopeVersion"], 1)
        self.assertEqual(missing["errorContractVersion"], 1)
        self.assertEqual(missing["errorCode"], "not_found")
        self.assertFalse(missing["retryable"])
        self.assertTrue(missing["recovery"])

    def test_login_success_and_failure_contracts_match(self) -> None:
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        valid = legacy.urlencode({"username": "tester", "password": self.config.password, "next": "/"}).encode("utf-8")
        asgi_success = self.request(self.asgi_base, "/login", method="POST", body=valid, extra_headers=headers)
        self.assertEqual(asgi_success[0], HTTPStatus.SEE_OTHER)
        selected = self.selected_headers(asgi_success[1])
        self.assertEqual(selected["location"], ["/"])
        self.assertEqual(len(selected["set-cookie"]), 2)
        self.assertTrue(any("HttpOnly" in value and "Secure" in value and "SameSite=Strict" in value for value in selected["set-cookie"]))

        invalid = legacy.urlencode({"username": "tester", "password": "wrong", "next": "/"}).encode("utf-8")
        asgi_failure = self.request(self.asgi_base, "/login", method="POST", body=invalid, extra_headers=headers)
        self.assertEqual(asgi_failure[0], HTTPStatus.OK)
        self.assertIn("Invalid username or password", asgi_failure[2].decode("utf-8"))

    def test_password_page_validation_and_success_contracts_match(self) -> None:
        self.config.users["tester"]["auth_epoch"] = 7
        self.config.password_digest = bcrypt.hashpw(self.config.password.encode("utf-8"), bcrypt.gensalt())
        self.assert_contract("/password", authenticated=True)
        csrf = gateway_security.csrf_token(self.config.cookie_secret, "tester", 7)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        invalid = legacy.urlencode({
            "csrf": csrf,
            "current_password": "wrong",
            "new_password": "replacement-password",
            "confirm_password": "replacement-password",
        }).encode("utf-8")
        asgi_invalid = self.request(self.asgi_base, "/password", authenticated=True, method="POST", body=invalid, extra_headers=headers)
        self.assertEqual(asgi_invalid[0], HTTPStatus.OK)
        self.assertIn("Current password is incorrect", asgi_invalid[2].decode("utf-8"))

        valid = legacy.urlencode({
            "csrf": csrf,
            "current_password": self.config.password,
            "new_password": "replacement-password",
            "confirm_password": "replacement-password",
        }).encode("utf-8")
        original_digest = bcrypt.hashpw(self.config.password.encode("utf-8"), bcrypt.gensalt())
        self.config.password_digest = original_digest
        self.config.users["tester"]["auth_epoch"] = 7
        asgi_success = self.request(self.asgi_base, "/password", authenticated=True, method="POST", body=valid, extra_headers=headers)
        self.assertEqual(asgi_success[0], HTTPStatus.SEE_OTHER)
        selected = self.selected_headers(asgi_success[1])
        self.assertEqual(selected["location"], ["/?password=changed"])
        self.assertEqual(len(selected["set-cookie"]), 2)
        self.config.password_digest = bcrypt.hashpw(self.config.password.encode("utf-8"), bcrypt.gensalt())
        self.config.users["tester"]["auth_epoch"] = 7

    def test_security_activity_and_bridge_package_reads_match(self) -> None:
        self.config.packages["1-deadbeef"] = {"id": "1-deadbeef", "owner": "tester", "title": "fixture", "status": "pending", "assets": []}
        for path in ("/api/security-activity?limit=1", "/api/bridge-packages"):
            asgi_result = self.request(self.asgi_base, path, authenticated=True)
            self.assertEqual(asgi_result[0], HTTPStatus.OK)
            payload = json.loads(asgi_result[2])
            self.assertTrue(payload["ok"])
            if path.startswith("/api/security-activity"):
                self.assertEqual(payload["entries"][0]["action"], "send")
            else:
                self.assertEqual(payload["packages"][0]["id"], "1-deadbeef")

    def test_gateway_status_and_workbench_contracts_match(self) -> None:
        for path in ("/api/gateway-status", "/api/workbench?page=1&period=7d&archive=all&q=fixture"):
            asgi_result = self.request(self.asgi_base, path, authenticated=True)
            self.assertEqual(asgi_result[0], HTTPStatus.OK)
            payload = json.loads(asgi_result[2])
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["entries"][0]["id"], "lab")
            if path.startswith("/api/workbench"):
                self.assertEqual(payload["history"]["filter"], {
                    "q": "fixture",
                    "period": "7d",
                    "archive": "all",
                })

    def test_bridge_package_asset_read_contract_matches(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            self.config.bridge_root = Path(temp)
            package_dir = self.config.bridge_root / "1-deadbeef"
            package_dir.mkdir()
            asset = package_dir / "fixture.png"
            asset.write_bytes(b"png fixture")
            self.config.packages["1-deadbeef"] = {"id": "1-deadbeef", "owner": "tester", "assets": []}
            asgi_result = self.request(self.asgi_base, "/bridge/packages/1-deadbeef/fixture.png", authenticated=True)
        self.assertEqual(asgi_result[0], HTTPStatus.OK)
        self.assertEqual(asgi_result[2], b"png fixture")
        selected = self.selected_headers(asgi_result[1])
        self.assertEqual(selected["content-type"], ["image/png"])
        self.assertEqual(selected["cache-control"], ["private, no-store"])

    def test_csrf_contract_matches_with_and_without_authentication(self) -> None:
        self.assert_contract("/api/csrf")
        self.assert_contract("/api/csrf", authenticated=True)

    def test_authenticated_home_and_logout_contracts_match(self) -> None:
        self.assert_contract("/", authenticated=True)
        self.assert_contract("/logout", authenticated=True)

    def test_proxy_control_post_and_audit_contract_match(self) -> None:
        body = json.dumps({"session": "fixture-session", "text": "anonymous"}).encode("utf-8")
        csrf = gateway_security.csrf_token(self.config.cookie_secret, "tester", self.config.auth_epoch("tester"))
        headers = {legacy.CSRF_HEADER: csrf, "Content-Type": "application/json"}
        self.config.audit_calls.clear()
        OwnerContractFixture.requests.clear()
        asgi_result = self.request(
            self.asgi_base, "/lab/api/send", authenticated=True, method="POST", body=body, extra_headers=headers,
        )
        self.assertEqual(asgi_result[0], HTTPStatus.OK)
        self.assertTrue(json.loads(asgi_result[2])["ok"])
        self.assertEqual(len(OwnerContractFixture.requests), 1)
        for forwarded in OwnerContractFixture.requests:
            self.assertEqual(forwarded["headers"]["X-Owner-Token"], "contract-owner-token")
            self.assertNotIn(legacy.CSRF_HEADER, forwarded["headers"])
        self.assertEqual(len(self.config.audit_calls), 1)
        self.assertEqual(self.config.audit_calls[0]["action"], "send")
        self.assertEqual(self.config.audit_calls[0]["target"], "fixture-session")

    def test_proxy_control_rejects_missing_csrf_equally(self) -> None:
        body = json.dumps({"session": "fixture-session"}).encode("utf-8")
        asgi_result = self.request(self.asgi_base, "/lab/api/interaction/respond", authenticated=True, method="POST", body=body)
        self.assertEqual(asgi_result[0], HTTPStatus.FORBIDDEN)
        failure = json.loads(asgi_result[2])
        self.assertFalse(failure["ok"])
        self.assertEqual(failure["error"], "csrf required")
        self.assertEqual(failure["envelopeVersion"], 1)
        self.assertEqual(failure["errorContractVersion"], 1)
        self.assertEqual(failure["errorCode"], "csrf_required")
        self.assertFalse(failure["retryable"])
        self.assertTrue(failure["recovery"])

    def test_unmapped_owner_api_post_is_proxied_without_control_audit(self) -> None:
        body = json.dumps({"session": "fixture-session"}).encode("utf-8")
        csrf = gateway_security.csrf_token(self.config.cookie_secret, "tester", self.config.auth_epoch("tester"))
        headers = {legacy.CSRF_HEADER: csrf, "Content-Type": "application/json"}
        self.config.audit_calls.clear()
        OwnerContractFixture.requests.clear()
        asgi_result = self.request(
            self.asgi_base,
            "/lab/api/custom-action",
            authenticated=True,
            method="POST",
            body=body,
            extra_headers=headers,
        )
        self.assertEqual(asgi_result[0], HTTPStatus.OK)
        self.assertTrue(json.loads(asgi_result[2])["ok"])
        self.assertEqual(len(OwnerContractFixture.requests), 1)
        self.assertEqual(self.config.audit_calls, [])

    def test_owner_json_get_contract_matches(self) -> None:
        self.assert_contract("/lab/api/status?session=fixture", authenticated=True)

    def test_owner_sse_bytes_and_headers_match(self) -> None:
        self.assert_contract("/lab/api/events?session=fixture", authenticated=True)

    def test_owner_get_requires_authentication_equally(self) -> None:
        self.assert_contract("/lab/api/status")

    def test_owner_page_and_static_resource_contracts_match(self) -> None:
        for path in (
            "/lab/?session=fixture",
            "/lab/app.js",
            "/lab/owner-ui.js",
            "/lab/owner-ui.LICENSE.txt",
            "/lab/owner/changes-panel.mjs",
        ):
            with self.subTest(path=path):
                self.assert_contract(path, authenticated=True)

    def test_owner_page_redirects_to_login_without_authentication(self) -> None:
        self.assert_contract("/lab/?session=fixture")

    def test_unknown_owner_resource_is_not_proxied(self) -> None:
        asgi_result = self.request(
            self.asgi_base,
            "/lab/private.txt",
            authenticated=True,
        )
        self.assertEqual(asgi_result[0], HTTPStatus.NOT_FOUND)
        headers = self.selected_headers(asgi_result[1])
        self.assertEqual(headers.get("cache-control"), ["no-store"])

    def test_session_history_archive_restore_and_audit_contract_match(self) -> None:
        csrf = gateway_security.csrf_token(self.config.cookie_secret, "tester", self.config.auth_epoch("tester"))
        headers = {legacy.CSRF_HEADER: csrf, "Content-Type": "application/json"}
        body = json.dumps({"route": "lab", "agent_session_id": "thread-fixture"}).encode("utf-8")
        for path, action, archived in (
            ("/api/session-history/archive", "archive", True),
            ("/api/session-history/unarchive", "unarchive", False),
        ):
            with self.subTest(path=path):
                self.config.audit_calls.clear()
                asgi_result = self.request(self.asgi_base, path, authenticated=True, method="POST", body=body, extra_headers=headers)
                self.assertEqual(asgi_result[0], HTTPStatus.OK)
                self.assertEqual(json.loads(asgi_result[2])["archived"], archived)
                self.assertEqual(len(self.config.audit_calls), 1)
                self.assertEqual(self.config.audit_calls[0]["action"], action)
                self.assertEqual(self.config.audit_calls[0]["target"], "thread-fixture")

    def test_session_history_validation_contract_matches(self) -> None:
        csrf = gateway_security.csrf_token(self.config.cookie_secret, "tester", self.config.auth_epoch("tester"))
        headers = {legacy.CSRF_HEADER: csrf, "Content-Type": "application/json"}
        body = json.dumps({"route": "lab"}).encode("utf-8")
        asgi_result = self.request(self.asgi_base, "/api/session-history/archive", authenticated=True, method="POST", body=body, extra_headers=headers)
        self.assertEqual(asgi_result[0], HTTPStatus.BAD_REQUEST)
        self.assertEqual(json.loads(asgi_result[2])["error"], "route and agent_session_id are required")

    def test_revoke_sessions_and_audit_contract_match(self) -> None:
        body = json.dumps({"confirm": "revoke"}).encode("utf-8")
        csrf = gateway_security.csrf_token(self.config.cookie_secret, "tester", 7)
        headers = {legacy.CSRF_HEADER: csrf, "Content-Type": "application/json"}
        self.config.users["tester"]["auth_epoch"] = 7
        self.config.audit_calls.clear()
        asgi_result = self.request(
            self.asgi_base, "/api/auth/revoke-all", authenticated=True, method="POST", body=body, extra_headers=headers,
        )
        self.assertEqual(asgi_result[0], HTTPStatus.OK)
        self.assertTrue(json.loads(asgi_result[2])["signedOut"])
        self.assertEqual(len(self.config.audit_calls), 1)
        self.assertEqual(self.config.audit_calls[0]["action"], "revoke-sessions")
        self.config.users["tester"]["auth_epoch"] = 7

    def test_revoke_requires_explicit_confirmation_equally(self) -> None:
        body = json.dumps({"confirm": "no"}).encode("utf-8")
        csrf = gateway_security.csrf_token(self.config.cookie_secret, "tester", 7)
        headers = {legacy.CSRF_HEADER: csrf, "Content-Type": "application/json"}
        self.config.users["tester"]["auth_epoch"] = 7
        asgi_result = self.request(
            self.asgi_base, "/api/auth/revoke-all", authenticated=True, method="POST", body=body, extra_headers=headers,
        )
        self.assertEqual(asgi_result[0], HTTPStatus.BAD_REQUEST)
        self.assertEqual(json.loads(asgi_result[2])["error"], "explicit revoke confirmation is required")

    def test_agent_resume_and_audit_contract_match(self) -> None:
        body = json.dumps({
            "route": "lab",
            "agent_session_id": "thread-fixture",
            "source": "codex-cli",
            "context_window_k": 1000,
        }).encode("utf-8")
        csrf = gateway_security.csrf_token(self.config.cookie_secret, "tester", self.config.auth_epoch("tester"))
        headers = {legacy.CSRF_HEADER: csrf, "Content-Type": "application/json"}
        self.config.audit_calls.clear()
        asgi_result = self.request(
            self.asgi_base, "/api/agent/resume", authenticated=True, method="POST", body=body, extra_headers=headers,
        )
        self.assertEqual(asgi_result[0], HTTPStatus.OK)
        self.assertEqual(json.loads(asgi_result[2])["session"], "faryo3")
        self.assertEqual(self.config.audit_calls[0]["action"], "resume")
        self.assertEqual(self.config.audit_calls[0]["target"], "thread-fixture")
        owner_payload = json.loads(OwnerContractFixture.requests[-1]["body"])
        self.assertEqual(owner_payload["context_window_k"], 1000)
        self.assertEqual(owner_payload["backend"], "terminal-managed")

    def test_agent_resume_returns_directory_preflight_then_accepts_signed_choice(self) -> None:
        csrf = gateway_security.csrf_token(self.config.cookie_secret, "tester", self.config.auth_epoch("tester"))
        headers = {legacy.CSRF_HEADER: csrf, "Content-Type": "application/json"}
        initial = json.dumps({"route": "lab", "agent_session_id": "thread-needs-cwd", "source": "codex-cli"}).encode("utf-8")
        response = self.request(
            self.asgi_base, "/api/agent/resume", authenticated=True, method="POST", body=initial, extra_headers=headers,
        )
        first = json.loads(response[2])
        self.assertEqual(response[0], HTTPStatus.OK)
        self.assertTrue(first["requiresWorkingDirectory"])
        self.assertNotIn("session", first)

        cwd = "/workspace/fixture"
        token = legacy.owner_directory_selection_token(self.config.owner_token("lab"), cwd)
        selected = json.dumps({
            "route": "lab",
            "agent_session_id": "thread-needs-cwd",
            "source": "codex-cli",
            "cwd": cwd,
            "cwd_token": token,
        }).encode("utf-8")
        response = self.request(
            self.asgi_base, "/api/agent/resume", authenticated=True, method="POST", body=selected, extra_headers=headers,
        )
        self.assertEqual(response[0], HTTPStatus.OK)
        self.assertEqual(json.loads(response[2])["session"], "faryo3")
        owner_payload = json.loads(OwnerContractFixture.requests[-1]["body"])
        self.assertEqual(owner_payload["cwd"], cwd)
        self.assertEqual(owner_payload["cwd_token"], token)

    def test_agent_resume_rejects_unsigned_directory_choice(self) -> None:
        csrf = gateway_security.csrf_token(self.config.cookie_secret, "tester", self.config.auth_epoch("tester"))
        headers = {legacy.CSRF_HEADER: csrf, "Content-Type": "application/json"}
        body = json.dumps({
            "route": "lab",
            "agent_session_id": "thread-needs-cwd",
            "source": "codex-cli",
            "cwd": "/workspace/fixture",
            "cwd_token": "wrong",
        }).encode("utf-8")
        response = self.request(
            self.asgi_base, "/api/agent/resume", authenticated=True, method="POST", body=body, extra_headers=headers,
        )
        self.assertEqual(response[0], HTTPStatus.BAD_REQUEST)
        self.assertEqual(json.loads(response[2])["error"], "working directory selection is invalid or expired")

    def test_agent_new_and_audit_contract_match(self) -> None:
        body = json.dumps({
            "route": "lab",
            "command": "codex",
            "client_launch_id": "launch-fixture",
            "context_window_k": 272,
        }).encode("utf-8")
        csrf = gateway_security.csrf_token(self.config.cookie_secret, "tester", self.config.auth_epoch("tester"))
        headers = {legacy.CSRF_HEADER: csrf, "Content-Type": "application/json"}
        self.config.audit_calls.clear()
        asgi_result = self.request(
            self.asgi_base, "/api/agent/new", authenticated=True, method="POST", body=body, extra_headers=headers,
        )
        self.assertEqual(asgi_result[0], HTTPStatus.OK)
        launch_result = json.loads(asgi_result[2])
        self.assertEqual(launch_result["session"], "faryo4")
        self.assertEqual(launch_result["clientLaunchId"], "launch-fixture")
        self.assertEqual(launch_result["redirect"], "/lab/?session=faryo4")
        self.assertEqual(self.config.audit_calls[0]["action"], "start")
        self.assertEqual(self.config.audit_calls[0]["target"], "faryo4")
        owner_payload = json.loads(OwnerContractFixture.requests[-1]["body"])
        self.assertEqual(owner_payload["context_window_k"], 272)
        self.assertEqual(owner_payload["backend"], "web-managed")

    def test_agent_resume_forwards_explicit_backend_choice(self) -> None:
        body = json.dumps({
            "route": "lab",
            "agent_session_id": "thread-fixture",
            "source": "codex-cli",
            "backend": "web-managed",
        }).encode("utf-8")
        csrf = gateway_security.csrf_token(
            self.config.cookie_secret,
            "tester",
            self.config.auth_epoch("tester"),
        )
        response = self.request(
            self.asgi_base,
            "/api/agent/resume",
            authenticated=True,
            method="POST",
            body=body,
            extra_headers={legacy.CSRF_HEADER: csrf, "Content-Type": "application/json"},
        )

        self.assertEqual(response[0], HTTPStatus.OK)
        owner_payload = json.loads(OwnerContractFixture.requests[-1]["body"])
        self.assertEqual(owner_payload["backend"], "web-managed")

    def test_agent_resume_rejects_explicit_future_browser_envelope(self) -> None:
        body = json.dumps({
            "route": "lab",
            "agent_session_id": "thread-fixture",
            "source": "codex-cli",
            "envelopeVersion": 2,
        }).encode("utf-8")
        csrf = gateway_security.csrf_token(
            self.config.cookie_secret,
            "tester",
            self.config.auth_epoch("tester"),
        )
        before = len(OwnerContractFixture.requests)
        response = self.request(
            self.asgi_base,
            "/api/agent/resume",
            authenticated=True,
            method="POST",
            body=body,
            extra_headers={legacy.CSRF_HEADER: csrf, "Content-Type": "application/json"},
        )

        self.assertEqual(response[0], HTTPStatus.BAD_REQUEST)
        self.assertIn("envelope", json.loads(response[2])["error"])
        self.assertEqual(len(OwnerContractFixture.requests), before)

    def test_agent_launch_rejects_invalid_context_window_before_owner(self) -> None:
        body = json.dumps({
            "route": "lab",
            "command": "codex",
            "client_launch_id": "launch-fixture",
            "context_window_k": 1051,
        }).encode("utf-8")
        csrf = gateway_security.csrf_token(self.config.cookie_secret, "tester", self.config.auth_epoch("tester"))
        headers = {legacy.CSRF_HEADER: csrf, "Content-Type": "application/json"}
        before = len(OwnerContractFixture.requests)
        response = self.request(
            self.asgi_base, "/api/agent/new", authenticated=True, method="POST", body=body, extra_headers=headers,
        )
        self.assertEqual(response[0], HTTPStatus.BAD_REQUEST)
        self.assertIn("context window", json.loads(response[2])["error"])
        self.assertEqual(len(OwnerContractFixture.requests), before)

    def test_agent_new_rejects_invalid_cwd_token_equally(self) -> None:
        body = json.dumps({
            "route": "lab",
            "command": "codex",
            "cwd": "/workspace/fixture",
            "cwd_token": "invalid",
            "client_launch_id": "launch-fixture",
        }).encode("utf-8")
        csrf = gateway_security.csrf_token(self.config.cookie_secret, "tester", self.config.auth_epoch("tester"))
        headers = {legacy.CSRF_HEADER: csrf, "Content-Type": "application/json"}
        asgi_result = self.request(
            self.asgi_base, "/api/agent/new", authenticated=True, method="POST", body=body, extra_headers=headers,
        )
        self.assertEqual(asgi_result[0], HTTPStatus.BAD_REQUEST)
        self.assertEqual(json.loads(asgi_result[2])["error"], "working directory selection is invalid or expired")

    def test_bridge_package_create_and_empty_asset_append_match(self) -> None:
        csrf = gateway_security.csrf_token(self.config.cookie_secret, "tester", self.config.auth_epoch("tester"))
        headers = {legacy.CSRF_HEADER: csrf, "Content-Type": "application/json"}
        create_body = json.dumps({"title": "fixture"}).encode("utf-8")
        asgi_create = self.request(
            self.asgi_base, "/api/bridge-packages", authenticated=True, method="POST", body=create_body, extra_headers=headers,
        )
        self.assertEqual(asgi_create[0], HTTPStatus.OK)
        self.assertEqual(json.loads(asgi_create[2])["package"]["id"], "1-deadbeef")

        append_body = json.dumps({"package_id": "1-deadbeef", "attachments": []}).encode("utf-8")
        asgi_append = self.request(
            self.asgi_base, "/api/bridge-package-assets", authenticated=True, method="POST", body=append_body, extra_headers=headers,
        )
        self.assertEqual(asgi_append[0], HTTPStatus.OK)
        self.assertEqual(json.loads(asgi_append[2])["package"]["assets"], [])

    def test_bridge_inject_without_assets_and_audit_match(self) -> None:
        csrf = gateway_security.csrf_token(self.config.cookie_secret, "tester", self.config.auth_epoch("tester"))
        headers = {legacy.CSRF_HEADER: csrf, "Content-Type": "application/json"}
        body = json.dumps({"package_id": "1-deadbeef", "route": "lab", "session": "faryo4"}).encode("utf-8")
        base_package = {"id": "1-deadbeef", "owner": "tester", "title": "fixture", "status": "pending", "assets": []}
        self.config.packages["1-deadbeef"] = dict(base_package)
        self.config.audit_calls.clear()
        asgi_result = self.request(
            self.asgi_base, "/api/bridge-inject", authenticated=True, method="POST", body=body, extra_headers=headers,
        )
        self.assertEqual(asgi_result[0], HTTPStatus.OK)
        self.assertEqual(json.loads(asgi_result[2])["redirect"], "/lab/?session=faryo4")
        self.assertEqual(self.config.audit_calls[0]["action"], "file-inject")
        self.assertEqual(self.config.audit_calls[0]["target"], "faryo4")

    def test_bridge_inject_with_real_asset_upload_matches(self) -> None:
        csrf = gateway_security.csrf_token(self.config.cookie_secret, "tester", self.config.auth_epoch("tester"))
        headers = {legacy.CSRF_HEADER: csrf, "Content-Type": "application/json"}
        body = json.dumps({"package_id": "1-deadbeef", "route": "lab", "session": "faryo4"}).encode("utf-8")
        with tempfile.TemporaryDirectory() as temp:
            self.config.bridge_root = Path(temp)
            asset_path = self.config.bridge_root / "fixture.png"
            asset_path.write_bytes(b"png fixture")
            base_package = {
                "id": "1-deadbeef",
                "owner": "tester",
                "title": "fixture",
                "status": "pending",
                "assets": [{"path": str(asset_path), "file_name": "fixture.png", "mime_type": "image/png"}],
            }
            self.config.packages["1-deadbeef"] = json.loads(json.dumps(base_package))
            OwnerContractFixture.requests.clear()
            asgi_result = self.request(
                self.asgi_base, "/api/bridge-inject", authenticated=True, method="POST", body=body, extra_headers=headers,
            )
        self.assertEqual(asgi_result[0], HTTPStatus.OK)
        self.assertEqual(json.loads(asgi_result[2])["redirect"], "/lab/?session=faryo4")
        uploads = [request for request in OwnerContractFixture.requests if request["path"] == "/api/attachment"]
        self.assertEqual(len(uploads), 1)
        for upload in uploads:
            self.assertIn(b'name="file"', upload["body"])
            self.assertEqual(upload["headers"]["X-Owner-Token"], "contract-owner-token")

    def test_mcp_auth_options_initialize_notification_batch_and_tool_contracts_match(self) -> None:
        cors_headers = {"Origin": self.config.mcp_cors_origin}
        asgi_options = self.request(self.asgi_base, "/mcp", method="OPTIONS", extra_headers=cors_headers)
        self.assertEqual(asgi_options[0], HTTPStatus.NO_CONTENT)
        selected = self.selected_headers(asgi_options[1])
        self.assertEqual(selected["access-control-allow-origin"], [self.config.mcp_cors_origin])
        self.assertEqual(selected["access-control-allow-methods"], ["POST, OPTIONS"])

        asgi_denied = self.request(self.asgi_base, "/mcp")
        self.assertEqual(asgi_denied[0], HTTPStatus.UNAUTHORIZED)
        self.assertEqual(json.loads(asgi_denied[2])["error"]["code"], -32001)

        asgi_delete_denied = self.request(self.asgi_base, "/mcp", method="DELETE")
        self.assertEqual(asgi_delete_denied[0], HTTPStatus.UNAUTHORIZED)
        self.assertEqual(json.loads(asgi_delete_denied[2])["error"]["message"], "unauthorized")

        headers = {
            "Authorization": f"Bearer {self.config.mcp_token}",
            "Content-Type": "application/json",
            "Origin": self.config.mcp_cors_origin,
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "gateway.invalid",
        }
        asgi_delete = self.request(self.asgi_base, "/mcp", method="DELETE", extra_headers=headers)
        self.assertEqual(asgi_delete[0], HTTPStatus.METHOD_NOT_ALLOWED)
        self.assertEqual(self.selected_headers(asgi_delete[1])["allow"], ["POST, OPTIONS"])
        payloads = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": legacy.MCP_PROTOCOL_VERSION}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            [
                {"jsonrpc": "2.0", "id": 3, "method": "ping"},
                {"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
            ],
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": legacy.MCP_TOOL_NAME, "arguments": {"title": "fixture", "intent": "handoff", "context": "context", "prompt": "prompt"}},
            },
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                body = json.dumps(payload).encode("utf-8")
                self.config.packages.clear()
                asgi_result = self.request(self.asgi_base, "/mcp", method="POST", body=body, extra_headers=headers)
                self.assertEqual(asgi_result[0], HTTPStatus.OK)
                response = json.loads(asgi_result[2])
                if isinstance(payload, list):
                    self.assertEqual([item["id"] for item in response], [3, 4])
                else:
                    self.assertEqual(response["id"], payload["id"])

        notification = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode("utf-8")
        asgi_notification = self.request(self.asgi_base, "/mcp", method="POST", body=notification, extra_headers=headers)
        self.assertEqual(asgi_notification[0], HTTPStatus.ACCEPTED)
        self.assertEqual(asgi_notification[2], b"")


if __name__ == "__main__":
    unittest.main()
