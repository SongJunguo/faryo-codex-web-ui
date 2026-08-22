from __future__ import annotations

import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import uvicorn


APP_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]
for value in (str(APP_DIR), str(REPO_ROOT / "src")):
    if value not in sys.path:
        sys.path.insert(0, value)

import owner_asgi
import run_owner_asgi
import server


class FakeRegistry:
    def __init__(self):
        self.thread_records = {}

    def get(self, name):
        return None

    def by_thread(self, thread_id):
        return self.thread_records.get(thread_id)


class FakeRuntime:
    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.started = False
        self.stopped = False
        self.sessions = set()
        self.registry = FakeRegistry()
        self.sent = []
        self.resumed = []
        self.detail_items = []

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def has_session(self, name):
        return name in self.sessions

    def has_thread(self, thread_id):
        return self.registry.by_thread(thread_id) is not None

    def start_session(self, **_values):
        self.sessions.add("faryo1")
        return {
            "session": "faryo1",
            "threadId": "thread_demo",
            "state": "idle",
            "backend": "web-managed",
            "duplicate": False,
        }

    def capture(self, name):
        if name not in self.sessions:
            raise RuntimeError("missing")
        return {
            "record": {
                "session": name,
                "threadId": "thread_demo",
                "cwd": self.cwd,
                "title": "Demo",
                "model": "test-model",
            },
            "snapshot": {
                "lifecycle": "idle",
                "revision": 1,
                "thread": {"id": "thread_demo", "model": "test-model"},
                "tokenUsage": {},
                "goal": None,
                "interaction": None,
                "interactionRevision": "appserver:0",
                "rateLimits": {},
            },
            "messages": [],
            "messageBlocks": [],
            "commandEvents": [],
        }

    def activity_detail(self, name, item_id):
        if name not in self.sessions:
            raise RuntimeError("missing")
        self.detail_items.append((name, item_id))
        return {
            "item": item_id,
            "detail": {
                "type": "command",
                "status": "completed",
                "title": "anonymous command",
                "command": "printf anonymous",
                "output": "anonymous output",
                "truncated": False,
            },
        }

    def resume_session(self, **values):
        self.resumed.append(values)
        self.sessions.add("faryo2")
        return {
            "session": "faryo2",
            "threadId": values["thread_id"],
            "state": "idle",
            "backend": "web-managed",
            "duplicate": False,
        }

    def send(self, session, text, client_message_id):
        self.sent.append((session, text, client_message_id))
        return {
            "accepted": True,
            "deliveryId": client_message_id,
            "delivery": "accepted",
            "deliveryState": "submitted",
            "session": session,
            "duplicate": False,
        }

    def status(self):
        return {"state": "ready", "ready": True}

    def ready(self):
        return True

    def session_records(self):
        return [
            {
                "session": name,
                "threadId": f"thread-{name}",
                "cwd": self.cwd,
            }
            for name in sorted(self.sessions)
        ]


def free_port() -> int:
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


class OwnerAsgiTest(unittest.TestCase):
    def test_startup_log_url_never_contains_authentication_material(self) -> None:
        value = run_owner_asgi.public_listen_url("127.0.0.1", 8765)

        self.assertEqual(value, "http://127.0.0.1:8765/")
        self.assertNotIn("token", value.lower())
        self.assertNotIn("?", value)

    def request(self, method: str, path: str, body: dict | None = None, *, token: bool = False):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {}
        encoded = None
        if body is not None:
            encoded = json.dumps(body).encode("utf-8")
            headers.update({"Content-Type": "application/json", "Content-Length": str(len(encoded))})
        if token:
            headers["X-Owner-Token"] = "fixture-owner-token"
        connection.request(method, path, body=encoded, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        result = (
            response.status,
            {name.lower(): value for name, value in response.getheaders()},
            payload,
        )
        connection.close()
        return result

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.cwd = self.temp.name
        subprocess.run(
            ["git", "init", "-q", self.cwd],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.runtime = FakeRuntime(self.cwd)
        self.config = server.Config(server.DEFAULT_SESSION, "fixture-owner-token", 0)
        self.app = owner_asgi.create_app(server, self.config, self.runtime)
        self.port = free_port()
        self.uvicorn = uvicorn.Server(uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=self.port,
            access_log=False,
            lifespan="on",
            log_level="error",
        ))
        self.pane_cwd = mock.patch.object(server, "get_pane_cwd", return_value=self.cwd)
        self.pane_cwd.start()
        self.thread = threading.Thread(target=self.uvicorn.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.uvicorn.started:
                break
            time.sleep(0.01)
        if not self.uvicorn.started:
            raise RuntimeError("Owner ASGI fixture did not start")

    def tearDown(self) -> None:
        self.uvicorn.should_exit = True
        self.thread.join(4)
        self.pane_cwd.stop()
        self.temp.cleanup()

    def test_security_auth_web_session_and_static_contract(self) -> None:
        status, headers, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["ok"], True)
        self.assertEqual(json.loads(body)["envelopeVersion"], 1)
        self.assertEqual(headers.get("x-frame-options"), "DENY")
        self.assertEqual(headers.get("cache-control"), "no-store")

        status, _headers, body = self.request("GET", "/api/status?session=faryo1")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"], "unauthorized")

        status, headers, body = self.request(
            "GET",
            "/api/events?session=missing-session",
            token=True,
        )
        self.assertEqual(status, 404)
        self.assertEqual(headers.get("content-type"), "application/json; charset=utf-8")
        self.assertIn("not found", json.loads(body)["error"])

        status, _headers, body = self.request(
            "POST",
            "/api/agent/new",
            {
                "command": "codex",
                "backend": "web-managed",
                "client_launch_id": "launch-fixture-1",
            },
            token=True,
        )
        started = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(started["session"], "faryo1")

        status, _headers, body = self.request("GET", "/api/status?session=faryo1", token=True)
        web_status = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(web_status["backend"], "web-managed")
        self.assertEqual(web_status["sessionId"], "thread_demo")

        for path in ("/api/capabilities", "/api/diagnostics", "/api/workspace-changes?session=faryo1"):
            status, headers, body = self.request("GET", path, token=True)
            self.assertEqual(status, 200, path)
            self.assertEqual(headers.get("cache-control"), "no-store", path)
            self.assertTrue(json.loads(body)["ok"], path)

        status, headers, body = self.request(
            "GET",
            "/api/activity-detail?session=faryo1&item=appserver-item-0123456789abcdef",
            token=True,
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertEqual(json.loads(body)["detail"]["output"], "anonymous output")
        self.assertEqual(self.runtime.detail_items, [("faryo1", "appserver-item-0123456789abcdef")])

        status, _headers, body = self.request(
            "POST",
            "/api/send",
            {"session": "faryo1", "text": "hello", "clientMessageId": "client-fixture-1"},
            token=True,
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["deliveryState"], "submitted")
        self.assertEqual(self.runtime.sent, [("faryo1", "hello", "client-fixture-1")])

        status, _headers, body = self.request(
            "POST",
            "/api/send",
            {
                "session": "faryo1",
                "text": "future",
                "clientMessageId": "client-fixture-2",
                "envelopeVersion": 2,
            },
            token=True,
        )
        self.assertEqual(status, 409)
        self.assertIn("envelope", json.loads(body)["error"])
        self.assertEqual(len(self.runtime.sent), 1)

        status, headers, body = self.request("GET", "/event-stream.js", token=True)
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers.get("content-type", ""))
        self.assertIn(b"createParser", body)

        status, headers, body = self.request("GET", "/not-present.js")
        self.assertEqual(status, 404)
        self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertEqual(json.loads(body)["error"], "file not found")
        self.assertTrue(self.runtime.started)

    def test_resume_can_switch_from_tui_history_to_app_server(self) -> None:
        thread = {
            "id": "thread_switch_app",
            "cwd": self.cwd,
            "title": "Anonymous thread",
            "model": "test-model",
        }
        with (
            mock.patch.object(server, "codex_thread_by_id", return_value=thread),
            mock.patch.object(server, "active_codex_thread_state", return_value=({}, set())),
        ):
            status, _headers, body = self.request(
                "POST",
                "/api/agent/resume",
                {
                    "agent_session_id": "thread_switch_app",
                    "source": "codex-cli",
                    "backend": "web-managed",
                },
                token=True,
            )

        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["session"], "faryo2")
        self.assertEqual(self.runtime.resumed[0]["thread_id"], "thread_switch_app")

    def test_active_app_server_history_keeps_app_server_question_identity(self) -> None:
        self.runtime.sessions.add("faryo1")
        capture = {
            "record": {
                "session": "faryo1",
                "threadId": "thread_demo",
                "cwd": self.cwd,
                "title": "Demo",
            },
            "snapshot": {
                "lifecycle": "idle",
                "revision": 3,
                "turns": [
                    {
                        "id": "turn_demo",
                        "status": "completed",
                        "items": [
                            {
                                "id": "user_demo",
                                "type": "userMessage",
                                "content": [{"type": "text", "text": "Anonymous question"}],
                            },
                            {
                                "id": "answer_demo",
                                "type": "agentMessage",
                                "text": "Anonymous answer",
                            },
                        ],
                    }
                ],
            },
            "messages": [("user", "Anonymous question"), ("assistant", "Anonymous answer")],
            "messageBlocks": [
                {
                    "id": "appserver-item-user",
                    "turnKey": "appserver-turn-demo",
                    "questionKey": "appserver-turn-demo",
                    "kind": "user",
                    "role": "user",
                    "text": "Anonymous question",
                    "revision": 1,
                    "final": True,
                },
                {
                    "id": "appserver-item-answer",
                    "turnKey": "appserver-turn-demo",
                    "kind": "output",
                    "role": "assistant",
                    "text": "Anonymous answer",
                    "revision": 2,
                    "final": True,
                },
            ],
        }
        with (
            mock.patch.object(self.runtime, "capture", return_value=capture),
            mock.patch.object(
                server,
                "codex_thread_by_id",
                return_value={"rollout_path": "/private/authoritative.jsonl"},
            ) as rollout_lookup,
            mock.patch.object(
                server,
                "rollout_thread_id_from_path",
                return_value="thread_demo",
            ),
            mock.patch.object(
                server.appserver_rollout,
                "activity_blocks",
                return_value=[
                    {
                        "id": "appserver-item-command",
                        "turnKey": "appserver-turn-demo",
                        "kind": "process",
                        "role": "process",
                        "text": "Ran anonymous check",
                        "revision": 0,
                        "final": True,
                    },
                ],
            ) as activity_lookup,
        ):
            status, _headers, body = self.request(
                "GET",
                "/api/conversation-history?session=faryo1&limit=12",
                token=True,
            )

        payload = json.loads(body)
        blocks = payload["turns"][0]["blocks"]
        self.assertEqual(status, 200)
        self.assertEqual(payload["source"], "codex-app-server")
        self.assertEqual([block["role"] for block in blocks], ["user", "process", "assistant"])
        self.assertEqual(blocks[0]["questionKey"], payload["questions"][0]["key"])
        rollout_lookup.assert_called_once_with("thread_demo")
        activity_lookup.assert_called_once_with("/private/authoritative.jsonl", ["turn_demo"])

    def test_resume_can_switch_from_app_server_history_to_tui(self) -> None:
        with (
            mock.patch.object(server, "codex_resume_directory_requirement", return_value=None),
            mock.patch.object(server, "resume_agent_session", return_value="faryo3") as resume,
            mock.patch.object(server, "agent_session_lifecycle", return_value=("starting", False)),
        ):
            status, _headers, body = self.request(
                "POST",
                "/api/agent/resume",
                {
                    "agent_session_id": "thread_switch_tui",
                    "source": "codex-app-server",
                    "backend": "terminal-managed",
                },
                token=True,
            )

        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["session"], "faryo3")
        self.assertEqual(resume.call_args.args[2], "codex-cli")

    def test_new_tui_launch_reserves_existing_app_server_session_names(self) -> None:
        self.runtime.sessions.add("faryo4")
        with (
            mock.patch.object(server, "start_agent_runtime_async", return_value="faryo5") as start,
            mock.patch.object(server, "agent_session_lifecycle", return_value=("starting", False)),
        ):
            status, _headers, body = self.request(
                "POST",
                "/api/agent/new",
                {
                    "command": "codex",
                    "backend": "terminal-managed",
                    "client_launch_id": "launch-tui-namespace",
                },
                token=True,
            )

        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["session"], "faryo5")
        reserved = start.call_args.kwargs["reserved_names"]
        self.assertEqual(reserved(), ["faryo4"])

    def test_resume_rejects_second_writer(self) -> None:
        self.runtime.registry.thread_records["thread_busy"] = object()
        with mock.patch.object(server, "codex_resume_directory_requirement", return_value=None):
            status, _headers, body = self.request(
                "POST",
                "/api/agent/resume",
                {
                    "agent_session_id": "thread_busy",
                    "source": "codex-app-server",
                    "backend": "terminal-managed",
                },
                token=True,
            )

        self.assertEqual(status, 409)
        self.assertIn("Codex App Server", json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main()
