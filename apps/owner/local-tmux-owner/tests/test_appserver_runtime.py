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
from appserver_protocol import AppServerUnavailable
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
        self.turn_steer_calls = 0
        self.rpc_calls = []
        self.label = ""

    def register_server_request(self, method, handler):
        self.server_handlers[method] = handler

    async def connect(self):
        self.ready = True
        return {"platformFamily": "unix"}

    async def close(self):
        self.ready = False

    async def rpc(self, method, params, **_options):
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
        if method == "server/diagnostics":
            return {"process": {"residentMemoryBytes": 1}, "gauges": []}
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
            thread_id = f"thread_{self.label}_{self.thread_number}" if self.label else f"thread_{self.thread_number}"
            return {
                "thread": {
                    "id": thread_id,
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
        if method == "turn/steer":
            self.turn_steer_calls += 1
            return {"turnId": params["expectedTurnId"]}
        if method == "turn/interrupt":
            await self.notification(
                "turn/completed",
                {
                    "threadId": params["threadId"],
                    "turn": {
                        "id": params["turnId"],
                        "items": [],
                        "status": "interrupted",
                    },
                },
            )
            return {}
        if method == "thread/read":
            return {"thread": {"id": params["threadId"], "turns": [], "status": "idle"}}
        if method == "thread/unsubscribe":
            return {"status": "unsubscribed"}
        if method in {"thread/archive", "thread/unarchive"}:
            return {"threadId": params["threadId"]}
        raise AssertionError(f"unexpected method: {method}")


class TrackingWorkerManager:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.started: list[str] = []
        self.restarted: list[str] = []
        self.stopped: list[str] = []

    def socket_path(self, worker_id: str) -> Path:
        return self.root / f"{worker_id}.sock"

    def start(self, worker_id: str, *, timeout: float = 12.0) -> Path:
        del timeout
        self.started.append(worker_id)
        return self.socket_path(worker_id)

    def restart(self, worker_id: str, *, timeout: float = 12.0) -> Path:
        del timeout
        self.restarted.append(worker_id)
        return self.socket_path(worker_id)

    def stop(self, worker_id: str, *, timeout: float = 12.0) -> None:
        del timeout
        self.stopped.append(worker_id)


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
            self.assertRegex(loaded.get("faryo3").worker_id, r"^[a-f0-9]{24}$")
            self.assertNotIn(loaded.get("faryo3").worker_id, repr(loaded.get("faryo3").public()))

    def test_registry_migrates_v1_records_to_opaque_worker_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "private/registry.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                '{"schemaVersion":1,"sessions":['
                '{"name":"faryo1","thread_id":"thread_a","cwd":"/workspace/a"},'
                '{"name":"faryo2","thread_id":"thread_b","cwd":"/workspace/b"}'
                ']}\n',
                encoding="utf-8",
            )

            registry = WebSessionRegistry(path)
            migrated = path.read_text(encoding="utf-8")

        workers = [record.worker_id for record in registry.values()]
        self.assertEqual(len(workers), 2)
        self.assertEqual(len(set(workers)), 2)
        self.assertTrue(all(len(worker) == 24 for worker in workers))
        self.assertIn('"schemaVersion":2', migrated)


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

    def test_compatibility_reads_reuse_the_resident_client(self) -> None:
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
            response = runtime.compat_rpc("account/rateLimits/read", {}, timeout=2)
            rejected = runtime.compat_rpc("thread/resume", {"threadId": "thread_demo"}, timeout=2)
            runtime.stop()

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["rateLimits"]["secondary"]["usedPercent"], 40)
        self.assertFalse(rejected["ok"])
        self.assertIn("unsupported", rejected["error"])
        self.assertEqual(
            [call[0] for call in clients[0].rpc_calls].count("account/rateLimits/read"),
            2,
        )

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
        self.assertEqual(len(clients), 3)
        self.assertFalse(any(method in {"thread/start", "thread/resume"} for method, _params in clients[0].rpc_calls))
        self.assertTrue(any(method == "thread/start" for method, _params in clients[1].rpc_calls))
        self.assertTrue(any(method == "thread/resume" for method, _params in clients[2].rpc_calls))
        self.assertTrue(lifecycle["ok"])
        self.assertEqual(lifecycle["result"]["threadId"], started["threadId"])

    def test_blocked_worker_does_not_delay_another_session(self) -> None:
        blocked_started = threading.Event()
        release_blocked = threading.Event()
        clients = []

        class BlockingRuntimeClient(FakeRuntimeClient):
            async def rpc(self, method, params, **options):
                if method == "turn/start" and params["input"][0]["text"] == "Block forever":
                    self.turn_start_calls += 1
                    blocked_started.set()
                    while not release_blocked.is_set():
                        await asyncio.sleep(0.01)
                    return await super().rpc(method, params, **options)
                return await super().rpc(method, params, **options)

        def factory(notification, disconnected):
            client = BlockingRuntimeClient(notification, disconnected)
            client.label = str(len(clients))
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
            first = runtime.start_session(cwd="/workspace/a", launch_id="launch_worker_a")["session"]
            second = runtime.start_session(cwd="/workspace/b", launch_id="launch_worker_b")["session"]

            first_receipt = runtime.send(first, "Block forever", "client_worker_a_block")
            self.assertTrue(blocked_started.wait(1))
            started_at = time.monotonic()
            second_receipt = runtime.send(second, "Independent", "client_worker_b_send")
            second_elapsed = time.monotonic() - started_at
            second_capture = runtime.capture(second)
            workers = [record.worker_id for record in runtime.registry.values()]

            release_blocked.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                settled = runtime.send(first, "Block forever", "client_worker_a_block")
                if settled["deliveryState"] == "submitted":
                    break
                time.sleep(0.01)
            runtime.stop()

        self.assertEqual(first_receipt["deliveryState"], "submitting")
        self.assertEqual(second_receipt["deliveryState"], "submitted")
        self.assertLess(second_elapsed, 0.25)
        self.assertEqual(second_capture["messages"][-1], ("assistant", "Answer $x^2$."))
        self.assertEqual(len(set(workers)), 2)
        self.assertEqual(clients[1].turn_start_calls, 2)
        self.assertEqual(clients[2].turn_start_calls, 1)

    def test_worker_disconnect_replaces_only_that_session_generation(self) -> None:
        clients = []

        def factory(notification, disconnected):
            client = FakeRuntimeClient(notification, disconnected)
            client.label = str(len(clients))
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
            first = runtime.start_session(cwd="/workspace/a", launch_id="launch_reconnect_a")["session"]
            second = runtime.start_session(cwd="/workspace/b", launch_id="launch_reconnect_b")["session"]
            first_before = runtime.registry.get(first).worker_generation
            second_before = runtime.registry.get(second).worker_generation
            second_client = runtime.session_clients[second]
            failed_client = runtime.session_clients[first]

            future = asyncio.run_coroutine_threadsafe(
                failed_client.disconnected(RuntimeError("fixture disconnect")),
                runtime.loop,
            )
            future.result(1)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if (
                    runtime.registry.get(first).worker_state == "ready"
                    and runtime.registry.get(first).worker_generation > first_before
                ):
                    break
                time.sleep(0.01)
            first_after = runtime.registry.get(first)
            second_after = runtime.registry.get(second)
            first_replaced = runtime.session_clients[first] is not failed_client
            second_unchanged = runtime.session_clients[second] is second_client
            runtime.stop()

        self.assertGreater(first_after.worker_generation, first_before)
        self.assertEqual(second_after.worker_generation, second_before)
        self.assertTrue(first_replaced)
        self.assertTrue(second_unchanged)

    def test_timed_out_rpc_restarts_only_its_worker_and_fences_late_events(self) -> None:
        clients = []

        class TimeoutRuntimeClient(FakeRuntimeClient):
            async def rpc(self, method, params, **options):
                if method == "turn/start":
                    raise AppServerUnavailable("Codex App Server request timed out: turn/start")
                return await super().rpc(method, params, **options)

        def factory(notification, disconnected):
            client = (
                TimeoutRuntimeClient(notification, disconnected)
                if len(clients) == 1
                else FakeRuntimeClient(notification, disconnected)
            )
            client.label = str(len(clients))
            clients.append(client)
            return client

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager = TrackingWorkerManager(root / "workers")
            runtime = AppServerRuntime(
                socket_path=root / "control.sock",
                registry_path=root / "registry.json",
                client_version="test",
                client_factory=factory,
                worker_manager=manager,
            )
            runtime.start()
            self.assertTrue(runtime.wait_ready(2))
            first = runtime.start_session(cwd="/workspace/a", launch_id="launch_timeout_a")["session"]
            second = runtime.start_session(cwd="/workspace/b", launch_id="launch_timeout_b")["session"]
            first_record = runtime.registry.get(first)
            second_record = runtime.registry.get(second)
            first_generation = first_record.worker_generation
            second_generation = second_record.worker_generation
            old_client = runtime.session_clients[first]
            second_client = runtime.session_clients[second]

            with self.assertRaisesRegex(RuntimeError, "timed out"):
                runtime.send(first, "Ambiguous", "client_timeout_same_id")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if runtime.registry.get(first).worker_generation > first_generation:
                    break
                time.sleep(0.01)
            recovered = runtime.send(first, "Ambiguous", "client_timeout_same_id")
            unaffected = runtime.send(second, "Unaffected", "client_timeout_other")
            before_messages = list(runtime.capture(first)["messages"])
            late = asyncio.run_coroutine_threadsafe(
                old_client.notification(
                    "item/completed",
                    {
                        "threadId": first_record.thread_id,
                        "turnId": "late_turn",
                        "item": {"id": "late_item", "type": "agentMessage", "text": "late stale output"},
                    },
                ),
                runtime.loop,
            )
            late.result(1)
            after_messages = runtime.capture(first)["messages"]
            first_after = runtime.registry.get(first)
            second_after = runtime.registry.get(second)
            second_unchanged = runtime.session_clients[second] is second_client
            ignored = runtime.status()["ignoredNotificationCount"]
            runtime.stop()

        self.assertEqual(recovered["deliveryState"], "submitted")
        self.assertEqual(unaffected["deliveryState"], "submitted")
        self.assertEqual(manager.restarted, [first_record.worker_id])
        self.assertGreater(first_after.worker_generation, first_generation)
        self.assertEqual(second_after.worker_generation, second_generation)
        self.assertTrue(second_unchanged)
        self.assertEqual(after_messages, before_messages)
        self.assertGreaterEqual(ignored, 1)

    def test_interrupt_timeout_force_recovers_only_the_active_worker(self) -> None:
        clients = []

        class InterruptTimeoutClient(FakeRuntimeClient):
            async def rpc(self, method, params, **options):
                if method == "turn/interrupt":
                    raise AppServerUnavailable("Codex App Server request timed out: turn/interrupt")
                return await super().rpc(method, params, **options)

        def factory(notification, disconnected):
            client = (
                InterruptTimeoutClient(notification, disconnected)
                if len(clients) == 1
                else FakeRuntimeClient(notification, disconnected)
            )
            client.label = str(len(clients))
            clients.append(client)
            return client

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager = TrackingWorkerManager(root / "workers")
            runtime = AppServerRuntime(
                socket_path=root / "control.sock",
                registry_path=root / "registry.json",
                client_version="test",
                client_factory=factory,
                worker_manager=manager,
            )
            runtime.start()
            self.assertTrue(runtime.wait_ready(2))
            first = runtime.start_session(cwd="/workspace/a", launch_id="launch_interrupt_a")["session"]
            second = runtime.start_session(cwd="/workspace/b", launch_id="launch_interrupt_b")["session"]
            first_record = runtime.registry.get(first)
            second_record = runtime.registry.get(second)
            first_generation = first_record.worker_generation
            second_generation_before = second_record.worker_generation
            second_client = runtime.session_clients[second]
            actor = runtime.actors[first]
            runtime.loop.call_soon_threadsafe(
                actor.apply,
                "turn/started",
                {
                    "threadId": first_record.thread_id,
                    "turn": {"id": "turn_hung", "items": [], "status": "inProgress"},
                },
            )
            deadline = time.monotonic() + 1
            while runtime.capture(first)["snapshot"]["activeTurnId"] != "turn_hung":
                if time.monotonic() >= deadline:
                    raise AssertionError("active turn did not settle")
                time.sleep(0.01)

            started_at = time.monotonic()
            interrupted = runtime.interrupt(first)
            elapsed = time.monotonic() - started_at
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if runtime.registry.get(first).worker_generation > first_generation:
                    break
                time.sleep(0.01)
            next_turn = runtime.send(first, "After recovery", "client_after_forced_recovery")
            other_turn = runtime.send(second, "Still healthy", "client_healthy_during_recovery")
            second_unchanged = runtime.session_clients[second] is second_client
            second_generation = runtime.registry.get(second).worker_generation
            runtime.stop()

        self.assertTrue(interrupted["forcedRecovery"])
        self.assertFalse(interrupted["settled"])
        self.assertLess(elapsed, 0.5)
        self.assertEqual(manager.restarted, [first_record.worker_id])
        self.assertEqual(next_turn["deliveryState"], "submitted")
        self.assertEqual(other_turn["deliveryState"], "submitted")
        self.assertTrue(second_unchanged)
        self.assertEqual(second_generation, second_generation_before)

    def test_silent_worker_probe_requires_two_failures_and_isolates_recovery(self) -> None:
        clients = []

        class ProbeTimeoutClient(FakeRuntimeClient):
            async def rpc(self, method, params, **options):
                if method == "server/diagnostics":
                    raise AppServerUnavailable("Codex App Server request timed out: server/diagnostics")
                return await super().rpc(method, params, **options)

        def factory(notification, disconnected):
            client = (
                ProbeTimeoutClient(notification, disconnected)
                if len(clients) == 1
                else FakeRuntimeClient(notification, disconnected)
            )
            client.label = str(len(clients))
            clients.append(client)
            return client

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager = TrackingWorkerManager(root / "workers")
            runtime = AppServerRuntime(
                socket_path=root / "control.sock",
                registry_path=root / "registry.json",
                client_version="test",
                client_factory=factory,
                worker_manager=manager,
            )
            runtime.start()
            self.assertTrue(runtime.wait_ready(2))
            first = runtime.start_session(cwd="/workspace/a", launch_id="launch_probe_a")["session"]
            second = runtime.start_session(cwd="/workspace/b", launch_id="launch_probe_b")["session"]
            first_record = runtime.registry.get(first)
            first_generation = first_record.worker_generation
            second_generation = runtime.registry.get(second).worker_generation
            second_client = runtime.session_clients[second]
            for name, turn_id in ((first, "turn_probe_a"), (second, "turn_probe_b")):
                actor = runtime.actors[name]
                record = runtime.registry.get(name)
                runtime.loop.call_soon_threadsafe(
                    actor.apply,
                    "turn/started",
                    {
                        "threadId": record.thread_id,
                        "turn": {"id": turn_id, "items": [], "status": "inProgress"},
                    },
                )
            deadline = time.monotonic() + 1
            while not all(runtime.capture(name)["snapshot"]["activeTurnId"] for name in (first, second)):
                if time.monotonic() >= deadline:
                    raise AssertionError("probe turns did not settle")
                time.sleep(0.01)

            first_probe = asyncio.run_coroutine_threadsafe(
                runtime.session_supervisor.probe_once(force=True),
                runtime.loop,
            )
            first_probe.result(1)
            state_after_first = runtime.registry.get(first).worker_state
            restarts_after_first = list(manager.restarted)
            second_probe = asyncio.run_coroutine_threadsafe(
                runtime.session_supervisor.probe_once(force=True),
                runtime.loop,
            )
            second_probe.result(1)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if runtime.registry.get(first).worker_generation > first_generation:
                    break
                time.sleep(0.01)
            first_after = runtime.registry.get(first)
            second_after = runtime.registry.get(second)
            second_unchanged = runtime.session_clients[second] is second_client
            runtime.stop()

        self.assertEqual(state_after_first, "degraded")
        self.assertEqual(restarts_after_first, [])
        self.assertEqual(manager.restarted, [first_record.worker_id])
        self.assertGreater(first_after.worker_generation, first_generation)
        self.assertEqual(second_after.worker_generation, second_generation)
        self.assertTrue(second_unchanged)

    def test_control_plane_reconnect_does_not_reconnect_healthy_session_workers(self) -> None:
        clients = []

        def factory(notification, disconnected):
            client = FakeRuntimeClient(notification, disconnected)
            client.label = str(len(clients))
            clients.append(client)
            return client

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager = TrackingWorkerManager(root / "workers")
            runtime = AppServerRuntime(
                socket_path=root / "control.sock",
                registry_path=root / "registry.json",
                client_version="test",
                client_factory=factory,
                worker_manager=manager,
            )
            runtime.start()
            self.assertTrue(runtime.wait_ready(2))
            session = runtime.start_session(cwd="/workspace", launch_id="launch_control_reconnect")["session"]
            session_client = runtime.session_clients[session]
            generation = runtime.registry.get(session).worker_generation
            starts = list(manager.started)
            disconnected = asyncio.run_coroutine_threadsafe(
                clients[0].disconnected(AppServerUnavailable("control transport closed")),
                runtime.loop,
            )
            disconnected.result(1)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if runtime.status()["ready"] and runtime.status()["reconnectCount"] >= 1:
                    break
                time.sleep(0.01)
            status = runtime.status()
            same_client = runtime.session_clients[session] is session_client
            same_generation = runtime.registry.get(session).worker_generation
            runtime.stop()

        self.assertTrue(status["ready"])
        self.assertEqual(status["reconnectCount"], 1)
        self.assertTrue(same_client)
        self.assertEqual(same_generation, generation)
        self.assertEqual(manager.started, starts)
        self.assertEqual(
            [method for method, _params in clients[0].rpc_calls if method in {"thread/start", "thread/resume"}],
            [],
        )

    def test_owner_restart_capture_returns_loading_before_worker_hydration(self) -> None:
        release_resume = threading.Event()
        clients = []

        class SlowResumeClient(FakeRuntimeClient):
            async def rpc(self, method, params, **options):
                if method == "thread/resume":
                    while not release_resume.is_set():
                        await asyncio.sleep(0.01)
                return await super().rpc(method, params, **options)

        def factory(notification, disconnected):
            client = SlowResumeClient(notification, disconnected)
            clients.append(client)
            return client

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry_path = root / "registry.json"
            registry = WebSessionRegistry(registry_path)
            record = registry.add(
                name="faryo1",
                worker_id="a" * 24,
                thread_id="thread_restore",
                cwd="/workspace",
            )
            manager = TrackingWorkerManager(root / "workers")
            runtime = AppServerRuntime(
                socket_path=root / "control.sock",
                registry_path=registry_path,
                client_version="test",
                client_factory=factory,
                worker_manager=manager,
            )
            runtime.start()
            self.assertTrue(runtime.wait_ready(2))
            loading = runtime.capture(record.name)
            with self.assertRaisesRegex(RuntimeError, "reconnecting"):
                runtime.send(record.name, "Too early", "client_before_hydration")
            release_resume.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if runtime.registry.get(record.name).worker_state == "ready":
                    break
                time.sleep(0.01)
            hydrated = runtime.capture(record.name)
            runtime.stop()

        self.assertEqual(loading["snapshot"]["lifecycle"], "loading")
        self.assertEqual(loading["record"]["workerState"], "starting")
        self.assertEqual(hydrated["snapshot"]["lifecycle"], "idle")
        self.assertEqual(hydrated["record"]["workerState"], "ready")

    def test_active_turn_send_steers_and_explicit_close_interrupts(self) -> None:
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
            actor = runtime.actors[session]
            runtime.loop.call_soon_threadsafe(
                actor.apply,
                "turn/started",
                {
                    "threadId": actor.thread_id,
                    "turn": {"id": "turn_active", "items": [], "status": "inProgress"},
                },
            )
            deadline = time.monotonic() + 1
            while runtime.capture(session)["snapshot"]["activeTurnId"] != "turn_active":
                if time.monotonic() >= deadline:
                    raise AssertionError("active turn did not settle")
                time.sleep(0.01)

            steered = runtime.send(session, "Steer now", "client_steer_1")
            with self.assertRaisesRegex(RuntimeError, "must be interrupted"):
                runtime.close_session(session)
            closed = runtime.close_session(session, interrupt=True)
            runtime.stop()

        self.assertEqual(steered["deliveryState"], "steered")
        self.assertEqual(steered["turnId"], "turn_active")
        self.assertEqual(clients[1].turn_start_calls, 0)
        self.assertEqual(clients[1].turn_steer_calls, 1)
        self.assertTrue(closed["closed"])
        self.assertTrue(closed["interrupted"])
        self.assertEqual(closed["unsubscribeStatus"], "unsubscribed")
        self.assertEqual(closed["writerRelease"], "immediate")

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
            self.assertEqual(clients[1].server_results[-1], {"decision": "accept"})

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
                clients[1].server_results[-1],
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
        self.assertEqual(clients[1].turn_start_calls, 1)
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
        self.assertEqual(clients[1].turn_start_calls, 1)

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

        settings_calls = [params for method, params in clients[1].rpc_calls if method == "thread/settings/update"]
        self.assertTrue(any(params.get("model") == "model-b" for params in settings_calls))
        self.assertEqual(sum(params.get("serviceTier") == "fast" for params in settings_calls), 1)
        self.assertEqual([event["name"] for event in events], ["/model", "/fast"])
        self.assertEqual([event["status"] for event in events], ["completed", "completed"])


if __name__ == "__main__":
    unittest.main()
