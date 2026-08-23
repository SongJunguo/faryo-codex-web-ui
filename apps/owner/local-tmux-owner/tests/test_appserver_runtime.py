from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from appserver_registry import WebSessionRegistry
from appserver_runtime import AppServerRuntime


class FakeRuntimeClient:
    def __init__(self, notification, disconnected) -> None:
        self.notification = notification
        self.disconnected = disconnected
        self.ready = False
        self.pending_count = 0
        self.thread_number = 0
        self.turn_number = 0
        self.server_handlers = {}
        self.server_results = []
        self.turn_start_calls = 0
        self.rpc_calls = []

    def register_server_request(self, method, handler):
        self.server_handlers[method] = handler

    async def connect(self):
        self.ready = True
        return {"platformFamily": "unix"}

    async def close(self):
        self.ready = False

    async def rpc(self, method, params):
        self.rpc_calls.append((method, dict(params)))
        if method == "account/rateLimits/read":
            return {
                "rateLimits": {
                    "primary": {"usedPercent": 20, "windowDurationMins": 300},
                    "secondary": {"usedPercent": 40, "windowDurationMins": 10080},
                }
            }
        if method == "account/usage/read":
            return {"summary": {"lifetimeTokens": 123456}}
        if method == "model/list":
            return {
                "data": [
                    {"id": "model-a", "model": "model-a", "displayName": "Model A", "description": "Current"},
                    {"id": "model-b", "model": "model-b", "displayName": "Model B", "description": "Next"},
                ],
                "nextCursor": None,
            }
        if method == "permissionProfile/list":
            return {
                "data": [
                    {"id": "standard", "allowed": True, "description": "Standard"},
                    {"id": "blocked", "allowed": False, "description": "Unavailable"},
                ],
                "nextCursor": None,
            }
        if method == "thread/settings/update":
            await self.notification(
                "thread/settings/updated",
                {
                    "threadId": params["threadId"],
                    "threadSettings": {
                        "model": params.get("model") or "model-a",
                        "serviceTier": params.get("serviceTier"),
                    },
                },
            )
            return {}
        if method == "thread/name/set":
            await self.notification(
                "thread/name/updated",
                {"threadId": params["threadId"], "threadName": params["name"]},
            )
            return {}
        if method == "thread/compact/start":
            return {}
        if method == "thread/goal/get":
            return {"goal": None}
        if method == "thread/start":
            self.thread_number += 1
            return {
                "thread": {
                    "id": f"thread_{self.thread_number}",
                    "turns": [],
                    "status": "idle",
                    "model": "model-a",
                }
            }
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"], "turns": [], "status": "idle"}}
        if method == "turn/start":
            self.turn_start_calls += 1
            thread_id = params["threadId"]
            self.turn_number += 1
            turn_id = f"turn_{self.turn_number}"
            client_id = params["clientUserMessageId"]
            prompt = params["input"][0]["text"]
            if prompt == "Slow send":
                await asyncio.sleep(0.08)
            elif prompt == "Very slow send":
                await asyncio.sleep(0.7)
            user_item = {
                "id": f"user_{self.turn_number}",
                "type": "userMessage",
                "clientId": client_id,
                "content": params["input"],
            }
            await self.notification(
                "turn/started",
                {"threadId": thread_id, "turn": {"id": turn_id, "items": [], "status": "inProgress"}},
            )
            await self.notification(
                "item/completed",
                {"threadId": thread_id, "turnId": turn_id, "completedAtMs": 1, "item": user_item},
            )
            if prompt == "Approval please":
                result = await self.server_handlers["item/commandExecution/requestApproval"]({
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "itemId": f"command_{self.turn_number}",
                    "command": "printf safe",
                    "cwd": "/workspace",
                    "reason": "Test approval",
                    "availableDecisions": ["accept", "decline", "cancel"],
                })
                self.server_results.append(result)
            if prompt == "Question please":
                result = await self.server_handlers["item/tool/requestUserInput"]({
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "itemId": f"question_{self.turn_number}",
                    "isBlocking": True,
                    "questions": [{
                        "id": "choice",
                        "header": "Choice",
                        "question": "Select a value",
                        "options": [{"label": "Alpha", "description": "First"}],
                        "isOther": True,
                        "isSecret": False,
                    }],
                })
                self.server_results.append(result)
            await self.notification(
                "item/agentMessage/delta",
                {"threadId": thread_id, "turnId": turn_id, "itemId": f"answer_{self.turn_number}", "delta": "Answer "},
            )
            await self.notification(
                "item/agentMessage/delta",
                {"threadId": thread_id, "turnId": turn_id, "itemId": f"answer_{self.turn_number}", "delta": "$x^2$."},
            )
            answer = {"id": f"answer_{self.turn_number}", "type": "agentMessage", "text": "Answer $x^2$."}
            await self.notification(
                "item/completed",
                {"threadId": thread_id, "turnId": turn_id, "completedAtMs": 2, "item": answer},
            )
            await self.notification(
                "turn/completed",
                {
                    "threadId": thread_id,
                    "turn": {"id": turn_id, "items": [user_item, answer], "status": "completed"},
                },
            )
            return {"turn": {"id": turn_id, "items": [], "status": "inProgress"}}
        if method == "turn/interrupt":
            return {}
        if method == "thread/unsubscribe":
            return {"status": "unsubscribed"}
        if method in {"thread/archive", "thread/unarchive"}:
            return {"threadId": params["threadId"]}
        raise AssertionError(f"unexpected method: {method}")


class RegistryTest(unittest.TestCase):
    def test_registry_is_private_body_free_and_skips_reserved_tmux_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "private/registry.json"
            registry = WebSessionRegistry(path)
            record = registry.add(
                thread_id="thread_demo",
                cwd="/workspace",
                title="Demo",
                reserved=["faryo1", "faryo2"],
                now=10,
            )
            body = path.read_text(encoding="utf-8")
            loaded = WebSessionRegistry(path)

            self.assertEqual(record.name, "faryo3")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertNotIn("prompt", body.lower())
            self.assertEqual(loaded.get("faryo3").thread_id, "thread_demo")


class RuntimeTest(unittest.TestCase):
    @staticmethod
    def wait_for_interaction(runtime: AppServerRuntime, session: str) -> dict:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            interaction = runtime.capture(session)["snapshot"].get("interaction")
            if isinstance(interaction, dict):
                return interaction
            time.sleep(0.01)
        raise AssertionError("interaction did not become pending")

    def test_runtime_streams_and_converges_without_a_second_body_store(self) -> None:
        clients = []

        def factory(notification, disconnected):
            client = FakeRuntimeClient(notification, disconnected)
            clients.append(client)
            return client

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = AppServerRuntime(
                socket_path=root / "app.sock",
                registry_path=root / "registry.json",
                client_version="test",
                reserved_names=lambda: ["faryo1"],
                client_factory=factory,
            )
            runtime.start()
            self.assertTrue(runtime.wait_ready(2))
            started = runtime.start_session(cwd="/workspace", title="Demo", launch_id="launch_demo")
            duplicate_launch = runtime.start_session(cwd="/workspace", title="Demo", launch_id="launch_demo")
            self.assertEqual(started["session"], "faryo2")
            cursor = runtime.replay(None).latest.render()
            sent = runtime.send("faryo2", "Question", "client_1")
            duplicate = runtime.send("faryo2", "Question", "client_1")
            capture = runtime.capture("faryo2")
            replay = runtime.replay(cursor)
            closed = runtime.close_session("faryo2")
            resumed = runtime.resume_session(
                thread_id=started["threadId"],
                cwd="/workspace",
                title="Demo resumed",
            )
            status = runtime.status()
            lifecycle = runtime.thread_lifecycle("thread/archive", started["threadId"])
            runtime.stop()

        self.assertTrue(sent["accepted"])
        self.assertTrue(duplicate_launch["duplicate"])
        self.assertEqual(duplicate_launch["threadId"], started["threadId"])
        self.assertFalse(sent["duplicate"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(capture["messages"], [("user", "Question"), ("assistant", "Answer $x^2$.")])
        self.assertEqual([block["kind"] for block in capture["messageBlocks"]], ["user", "output"])
        self.assertTrue(capture["messageBlocks"][0]["questionKey"].startswith("appserver-question-"))
        self.assertNotIn(started["threadId"], repr(capture["messageBlocks"]))
        self.assertEqual(capture["snapshot"]["lifecycle"], "idle")
        self.assertTrue(closed["closed"])
        self.assertEqual(resumed["session"], "faryo2")
        self.assertFalse(resumed["duplicate"])
        self.assertIn("item.delta", [event.kind for event in replay.events])
        self.assertIn("item.final", [event.kind for event in replay.events])
        delta_events = [event for event in replay.events if event.kind == "item.delta"]
        self.assertEqual(len(delta_events), 1)
        self.assertEqual(delta_events[0].payload["batchCount"], 2)
        self.assertEqual(delta_events[0].payload["textLength"], len("Answer $x^2$."))
        self.assertNotIn("Answer", repr([event.payload for event in replay.events]))
        self.assertEqual(status["pendingRpcCount"], 0)
        self.assertEqual(len(clients), 1)
        self.assertTrue(lifecycle["ok"])
        self.assertEqual(lifecycle["result"]["threadId"], started["threadId"])

    def test_runtime_reassigns_persisted_names_that_now_belong_to_tmux(self) -> None:
        clients = []

        def factory(notification, disconnected):
            client = FakeRuntimeClient(notification, disconnected)
            clients.append(client)
            return client

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry_path = root / "registry.json"
            registry = WebSessionRegistry(registry_path)
            registry.add(
                name="faryo4",
                thread_id="thread_existing",
                cwd="/workspace",
                launch_id="launch_existing",
            )
            runtime = AppServerRuntime(
                socket_path=root / "app.sock",
                registry_path=registry_path,
                client_version="test",
                reserved_names=lambda: ["faryo1", "faryo3", "faryo4"],
                client_factory=factory,
            )
            runtime.start()
            self.assertTrue(runtime.wait_ready(2))
            records = runtime.session_records()
            runtime.stop()

        self.assertEqual([record["session"] for record in records], ["faryo2"])
        self.assertEqual(records[0]["threadId"], "thread_existing")

    def test_runtime_routes_approvals_and_user_input_without_terminal_keys(self) -> None:
        clients = []

        def factory(notification, disconnected):
            client = FakeRuntimeClient(notification, disconnected)
            clients.append(client)
            return client

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = AppServerRuntime(
                socket_path=root / "app.sock",
                registry_path=root / "registry.json",
                client_version="test",
                client_factory=factory,
            )
            runtime.start()
            self.assertTrue(runtime.wait_ready(2))
            session = runtime.start_session(cwd="/workspace")["session"]

            approval_result = {}
            approval_thread = threading.Thread(
                target=lambda: approval_result.update(runtime.send(session, "Approval please", "client_approval")),
            )
            approval_thread.start()
            interaction = self.wait_for_interaction(runtime, session)
            self.assertEqual(interaction["source"], "codex-app-server")
            self.assertEqual(interaction["details"]["command"], "printf safe")
            allow = next(option for option in interaction["options"] if option["label"] == "Allow once")
            response = runtime.respond_interaction(
                session,
                interaction_id=interaction["id"],
                option_id=allow["id"],
                client_request_id="request_approval_1",
            )
            approval_thread.join(2)
            self.assertFalse(approval_thread.is_alive())
            self.assertTrue(response["resolved"])
            self.assertEqual(clients[0].server_results[-1], {"decision": "accept"})

            question_thread = threading.Thread(
                target=lambda: runtime.send(session, "Question please", "client_question"),
            )
            question_thread.start()
            interaction = self.wait_for_interaction(runtime, session)
            self.assertEqual(interaction["responseKind"], "questions")
            runtime.respond_interaction(
                session,
                interaction_id=interaction["id"],
                answers={"choice": ["Alpha"]},
                client_request_id="request_question_1",
            )
            question_thread.join(2)
            self.assertFalse(question_thread.is_alive())
            self.assertEqual(
                clients[0].server_results[-1],
                {"answers": {"choice": {"answers": ["Alpha"]}}},
            )
            self.assertIsNone(runtime.capture(session)["snapshot"]["interaction"])
            runtime.stop()

    def test_concurrent_retry_joins_one_turn_start(self) -> None:
        clients = []

        def factory(notification, disconnected):
            client = FakeRuntimeClient(notification, disconnected)
            clients.append(client)
            return client

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = AppServerRuntime(
                socket_path=root / "app.sock",
                registry_path=root / "registry.json",
                client_version="test",
                client_factory=factory,
            )
            runtime.start()
            self.assertTrue(runtime.wait_ready(2))
            session = runtime.start_session(cwd="/workspace")["session"]
            results = []
            workers = [
                threading.Thread(
                    target=lambda: results.append(runtime.send(session, "Slow send", "client_retry_1")),
                )
                for _index in range(2)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(2)
            duplicate_after = runtime.send(session, "Slow send", "client_retry_1")
            runtime.stop()

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(clients[0].turn_start_calls, 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(bool(result["duplicate"]) for result in results), 1)
        self.assertTrue(duplicate_after["duplicate"])

    def test_slow_turn_start_returns_a_fast_idempotent_submitting_receipt(self) -> None:
        clients = []

        def factory(notification, disconnected):
            client = FakeRuntimeClient(notification, disconnected)
            clients.append(client)
            return client

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = AppServerRuntime(
                socket_path=root / "app.sock",
                registry_path=root / "registry.json",
                client_version="test",
                client_factory=factory,
            )
            runtime.start()
            self.assertTrue(runtime.wait_ready(2))
            session = runtime.start_session(cwd="/workspace")["session"]
            started_at = time.monotonic()
            first = runtime.send(session, "Very slow send", "client_slow_ack")
            elapsed = time.monotonic() - started_at
            duplicate = runtime.send(session, "Very slow send", "client_slow_ack")
            deadline = time.monotonic() + 2
            settled = duplicate
            while time.monotonic() < deadline:
                settled = runtime.send(session, "Very slow send", "client_slow_ack")
                if settled["deliveryState"] == "submitted":
                    break
                time.sleep(0.01)
            runtime.stop()

        self.assertLess(elapsed, 0.6)
        self.assertEqual(first["deliveryState"], "submitting")
        self.assertFalse(first["duplicate"])
        self.assertEqual(duplicate["deliveryState"], "submitting")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(settled["deliveryState"], "submitted")
        self.assertTrue(settled["duplicate"])
        self.assertEqual(clients[0].turn_start_calls, 1)

    def test_web_commands_use_app_server_apis_instead_of_terminal_menus(self) -> None:
        clients = []

        def factory(notification, disconnected):
            client = FakeRuntimeClient(notification, disconnected)
            clients.append(client)
            return client

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = AppServerRuntime(
                socket_path=root / "app.sock",
                registry_path=root / "registry.json",
                client_version="test",
                client_factory=factory,
            )
            runtime.start()
            self.assertTrue(runtime.wait_ready(2))
            session = runtime.start_session(cwd="/workspace")["session"]

            opened = runtime.begin_command(
                session,
                command="/model",
                client_request_id="command_model_1",
            )
            interaction = opened["interaction"]
            self.assertEqual(opened["commandEvent"]["status"], "waiting")
            model_b = next(option for option in interaction["options"] if option["label"] == "Model B")
            model_response = runtime.respond_interaction(
                session,
                interaction_id=interaction["id"],
                option_id=model_b["id"],
                client_request_id="command_model_response_1",
            )
            self.assertEqual(model_response["commandEvent"]["status"], "completed")
            self.assertIn("Model B", model_response["commandEvent"]["summary"])
            self.assertEqual(runtime.capture(session)["snapshot"]["thread"]["model"], "model-b")

            runtime.begin_command(session, command="/fast", client_request_id="command_fast_1")
            duplicate_fast = runtime.begin_command(
                session,
                command="/fast",
                client_request_id="command_fast_1",
            )
            self.assertEqual(runtime.capture(session)["snapshot"]["thread"]["serviceTier"], "fast")
            self.assertTrue(duplicate_fast["duplicate"])

            usage = runtime.begin_command(session, command="/usage", client_request_id="command_usage_1")
            self.assertIn("Weekly window", usage["interaction"]["prompt"])
            close = usage["interaction"]["options"][0]
            runtime.respond_interaction(
                session,
                interaction_id=usage["interaction"]["id"],
                option_id=close["id"],
                client_request_id="command_usage_response_1",
            )
            events = runtime.capture(session)["commandEvents"]
            runtime.stop()

        settings_calls = [params for method, params in clients[0].rpc_calls if method == "thread/settings/update"]
        self.assertTrue(any(params.get("model") == "model-b" for params in settings_calls))
        self.assertEqual(sum(params.get("serviceTier") == "fast" for params in settings_calls), 1)
        self.assertEqual([event["name"] for event in events], ["/model", "/fast"])
        self.assertEqual([event["status"] for event in events], ["completed", "completed"])


if __name__ == "__main__":
    unittest.main()
