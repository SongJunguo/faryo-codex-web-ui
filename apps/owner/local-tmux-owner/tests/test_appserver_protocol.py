from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from appserver_events import EventJournal
from appserver_capabilities import inspect_schema_directory
from appserver_history import conversation_history_page
from appserver_rollout import activity_blocks, activity_detail as rollout_activity_detail, clear_activity_index_cache
from appserver_protocol import (
    AppServerProtocolError,
    AppServerUnavailable,
    ProtocolMessageKind,
    decode_wire_message,
    parse_codex_version,
)
from appserver_session import WebSessionActor, activity_detail
from appserver_transport import AsyncCodexAppServerClient


class FakeSocket:
    def __init__(self, *, reject_experimental: bool = False) -> None:
        self.incoming: asyncio.Queue[str | BaseException] = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed = False
        self.reject_experimental = reject_experimental

    async def send(self, message: str) -> None:
        body = json.loads(message)
        self.sent.append(body)
        if body.get("method") == "initialize" and "id" in body:
            capabilities = (body.get("params") or {}).get("capabilities")
            if self.reject_experimental and isinstance(capabilities, dict):
                await self.incoming.put(json.dumps({
                    "id": body["id"],
                    "error": {"code": -32602, "message": "unsupported capability"},
                }))
            else:
                await self.incoming.put(json.dumps({"id": body["id"], "result": {"userAgent": "test/0"}}))

    async def recv(self) -> str:
        value = await self.incoming.get()
        if isinstance(value, BaseException):
            raise value
        return value

    async def close(self) -> None:
        self.closed = True


class ProtocolTest(unittest.TestCase):
    def tearDown(self) -> None:
        clear_activity_index_cache()

    def test_classifies_all_bidirectional_shapes(self) -> None:
        self.assertEqual(decode_wire_message('{"id":1,"result":{}}').kind, ProtocolMessageKind.RESPONSE)
        self.assertEqual(
            decode_wire_message('{"id":1,"error":{"code":-1,"message":"no"}}').kind,
            ProtocolMessageKind.ERROR,
        )
        self.assertEqual(
            decode_wire_message('{"method":"item/started","params":{}}').kind,
            ProtocolMessageKind.NOTIFICATION,
        )
        self.assertEqual(
            decode_wire_message('{"id":7,"method":"item/requestApproval","params":{}}').kind,
            ProtocolMessageKind.SERVER_REQUEST,
        )
        with self.assertRaises(AppServerProtocolError):
            decode_wire_message("[]")

    def test_parses_codex_cli_version_without_freezing_launcher_path(self) -> None:
        self.assertEqual(parse_codex_version("codex-cli 0.149.0"), "0.149.0")
        self.assertEqual(parse_codex_version("unrecognized"), "")

    def test_schema_probe_reports_missing_methods_without_crashing(self) -> None:
        with self.subTest("missing files"):
            report = inspect_schema_directory(Path(__file__).parent / "fixtures/not-present")
            self.assertFalse(report.compatible)
            self.assertEqual(len(report.errors), 2)

    def test_fixture_delta_and_final_converge_to_one_item(self) -> None:
        actor = WebSessionActor(session_id="session_demo", thread_id="thread_demo")
        fixture = Path(__file__).parent / "fixtures/appserver-0.149.0.jsonl"
        kinds: list[str] = []
        for line in fixture.read_text(encoding="utf-8").splitlines()[1:]:
            message = json.loads(line)
            kinds.extend(event.kind for event in actor.apply(message["method"], message.get("params") or {}))
        self.assertIn("item.delta", kinds)
        self.assertIn("item.final", kinds)
        self.assertEqual(actor.item_order, ["item_demo"])
        item = actor.items["item_demo"]
        self.assertEqual(item.text, "Hello $x^2$.")
        self.assertTrue(item.final)
        self.assertEqual(actor.lifecycle, "idle")

    def test_tool_activity_is_bounded_and_hidden_reasoning_is_not_exposed(self) -> None:
        actor = WebSessionActor(session_id="session_demo", thread_id="thread_demo")
        actor.apply(
            "turn/started",
            {"threadId": "thread_demo", "turn": {"id": "turn_demo", "status": "inProgress"}},
        )
        actor.apply(
            "item/started",
            {
                "threadId": "thread_demo",
                "turnId": "turn_demo",
                "item": {
                    "id": "command_demo",
                    "type": "commandExecution",
                    "command": "python -m pytest",
                    "status": "inProgress",
                },
            },
        )
        actor.apply(
            "item/completed",
            {
                "threadId": "thread_demo",
                "turnId": "turn_demo",
                "item": {
                    "id": "command_demo",
                    "type": "commandExecution",
                    "command": "python -m pytest",
                    "status": "completed",
                    "exitCode": 0,
                    "aggregatedOutput": "private command output is not projected",
                },
            },
        )
        actor.apply(
            "item/completed",
            {
                "threadId": "thread_demo",
                "turnId": "turn_demo",
                "item": {
                    "id": "reasoning_demo",
                    "type": "reasoning",
                    "text": "hidden reasoning body",
                },
            },
        )
        actor.apply(
            "item/completed",
            {
                "threadId": "thread_demo",
                "turnId": "turn_demo",
                "item": {
                    "id": "change_demo",
                    "type": "fileChange",
                    "status": "completed",
                    "changes": [{"path": "/workspace/src/app.py", "kind": "update", "diff": "private diff"}],
                },
            },
        )
        messages = actor.messages()
        blocks = actor.message_blocks()
        rendered = "\n".join(text for _role, text in messages)

        self.assertIn(("process", "Ran python -m pytest · exit 0"), messages)
        self.assertIn(("process", "Edited app.py"), messages)
        self.assertNotIn(("process", "Working"), messages)
        self.assertNotIn("hidden reasoning body", rendered)
        self.assertNotIn("private command output", rendered)
        self.assertNotIn("private diff", rendered)
        self.assertEqual([block["kind"] for block in blocks], ["process", "process"])
        self.assertTrue(all(str(block["id"]).startswith("appserver-item-") for block in blocks))
        self.assertEqual(blocks[0]["activity"]["type"], "command")
        self.assertEqual(blocks[0]["activity"]["status"], "completed")
        self.assertEqual(blocks[0]["activity"]["exitCode"], 0)
        self.assertTrue(blocks[0]["activity"]["detailAvailable"])
        self.assertEqual(blocks[1]["activity"]["type"], "file_change")
        command_detail = activity_detail(actor.items["command_demo"].raw)
        file_detail = activity_detail(actor.items["change_demo"].raw)
        self.assertIn("private command output", command_detail["output"])
        self.assertIn("private diff", file_detail["changes"][0]["diff"])

    def test_one_protocol_turn_projects_each_user_message_as_a_conversation_segment(self) -> None:
        actor = WebSessionActor(session_id="session_demo", thread_id="thread_demo")
        actor.apply(
            "turn/started",
            {"threadId": "thread_demo", "turn": {"id": "turn_shared", "status": "inProgress"}},
        )
        items = [
            {"id": "user_one", "type": "userMessage", "content": [{"type": "text", "text": "First question"}]},
            {"id": "command_one", "type": "commandExecution", "command": "true", "status": "completed", "exitCode": 0},
            {"id": "answer_one", "type": "agentMessage", "text": "First answer"},
            {"id": "user_two", "type": "userMessage", "content": [{"type": "text", "text": "Second question"}]},
            {"id": "change_two", "type": "fileChange", "status": "completed", "changes": [{"path": "/workspace/demo.txt", "kind": "update"}]},
            {"id": "answer_two", "type": "agentMessage", "text": "Second answer"},
        ]
        for item in items:
            actor.apply(
                "item/completed",
                {"threadId": "thread_demo", "turnId": "turn_shared", "item": item},
            )

        blocks = actor.message_blocks()
        first_key = blocks[0]["questionKey"]
        second_key = blocks[3]["questionKey"]
        history = conversation_history_page(
            actor.snapshot(),
            thread_id="thread_demo",
            message_blocks=blocks,
            limit=12,
            max_page_turns=24,
            page_char_budget=100_000,
            preview_chars=80,
            updated_at=lambda: "now",
        )

        self.assertNotEqual(first_key, second_key)
        self.assertTrue(first_key.startswith("appserver-question-"))
        self.assertEqual([block["segmentKey"] for block in blocks[:3]], [first_key] * 3)
        self.assertEqual([block["segmentKey"] for block in blocks[3:]], [second_key] * 3)
        self.assertEqual(actor.command_anchor_key(), blocks[-1]["id"])
        self.assertEqual(history["totalTurns"], 2)
        self.assertEqual([question["key"] for question in history["questions"]], [first_key, second_key])
        self.assertEqual(
            [[block["kind"] for block in turn["blocks"]] for turn in history["turns"]],
            [["user", "process", "output"], ["user", "process", "output"]],
        )

    def test_unknown_activity_survives_as_a_safe_generic_card(self) -> None:
        actor = WebSessionActor(session_id="session_demo", thread_id="thread_demo")
        actor.apply(
            "item/completed",
            {
                "threadId": "thread_demo",
                "turnId": "turn_demo",
                "item": {
                    "id": "future_demo",
                    "type": "futureCodexItem",
                    "status": "completed",
                    "secretField": "not projected",
                },
            },
        )
        blocks = actor.message_blocks()
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["activity"]["type"], "unknown")
        self.assertFalse(blocks[0]["activity"]["detailAvailable"])
        self.assertNotIn("secretField", repr(blocks[0]))

    def test_durable_activity_recovery_is_explicit_not_the_live_default(self) -> None:
        actor = WebSessionActor(session_id="session_demo", thread_id="thread_demo")
        self.assertFalse(actor.snapshot()["durableActivityRequired"])
        actor.require_durable_activity()
        self.assertTrue(actor.snapshot()["durableActivityRequired"])

    def test_event_journal_replay_gap_reset_and_byte_bound(self) -> None:
        journal = EventJournal(max_events=3, max_bytes=800, epoch="epoch")
        first = journal.publish(session_id="s", thread_id="t", kind="one", payload={"n": 1})
        journal.publish(session_id="s", thread_id="t", kind="two", payload={"n": 2})
        third = journal.publish(session_id="s", thread_id="t", kind="three", payload={"n": 3})
        self.assertEqual([event.kind for event in journal.replay(first.id).events], ["two", "three"])
        self.assertEqual(journal.replay(third.id).status, "replay")
        self.assertEqual(journal.replay("other:1").status, "reset")
        for number in range(4, 12):
            journal.publish(session_id="s", thread_id="t", kind="next", payload={"n": number})
        self.assertLessEqual(len(tuple(journal)), 3)
        self.assertLessEqual(journal.total_bytes, 800)
        self.assertEqual(journal.replay(first.id).status, "gap")

    def test_live_snapshot_history_pages_until_jsonl_becomes_authoritative(self) -> None:
        snapshot = {
            "revision": 999,
            "turns": [
                {
                    "id": f"turn_{index}",
                    "status": "completed",
                    "items": [
                        {
                            "id": f"user_{index}",
                            "type": "userMessage",
                            "content": [{"type": "text", "text": f"Question {index}"}],
                        },
                        {"id": f"answer_{index}", "type": "agentMessage", "text": f"Answer {index}"},
                    ],
                }
                for index in range(30)
            ],
        }
        options = {
            "thread_id": "thread_demo",
            "limit": 5,
            "max_page_turns": 24,
            "page_char_budget": 100_000,
            "preview_chars": 80,
            "updated_at": lambda: "now",
        }
        latest = conversation_history_page(snapshot, **options)
        older = conversation_history_page(snapshot, cursor=latest["olderCursor"], **options)
        around = conversation_history_page(snapshot, around=3, **options)

        self.assertEqual(latest["source"], "codex-app-server")
        self.assertEqual(latest["totalTurns"], 30)
        self.assertEqual([turn["index"] for turn in latest["turns"]], [25, 26, 27, 28, 29])
        self.assertEqual([turn["index"] for turn in older["turns"]], [20, 21, 22, 23, 24])
        self.assertIn("Question 3", around["turns"][3 - around["start"]]["text"])
        self.assertEqual(len(latest["questions"]), 30)
        self.assertEqual([block["kind"] for block in latest["turns"][0]["blocks"]], ["user", "output"])
        self.assertEqual(latest["turns"][0]["blocks"][0]["questionKey"], latest["turns"][0]["key"])

        structured = conversation_history_page(
            {"turns": [{"id": "turn_raw", "items": []}]},
            message_blocks=[
                {
                    "id": "appserver-item-user",
                    "turnKey": "appserver-turn-safe",
                    "questionKey": "ignored-client-value",
                    "kind": "user",
                    "role": "user",
                    "text": "Anonymous structured question",
                    "revision": 1,
                    "final": True,
                },
                {
                    "id": "appserver-item-answer",
                    "turnKey": "appserver-turn-safe",
                    "kind": "output",
                    "role": "assistant",
                    "text": "Anonymous structured answer",
                    "revision": 2,
                    "final": True,
                },
            ],
            **options,
        )
        self.assertEqual(structured["totalTurns"], 1)
        self.assertEqual(
            [block["role"] for block in structured["turns"][0]["blocks"]],
            ["user", "assistant"],
        )
        self.assertEqual(
            structured["turns"][0]["blocks"][0]["questionKey"],
            structured["questions"][0]["key"],
        )

    def test_rollout_restores_commands_searches_and_file_changes_by_turn(self) -> None:
        turn_id = "turn_anonymous"
        metadata = {"turn_id": turn_id, "create_time": 1}
        events = [
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": turn_id},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "id": "question_one",
                    "content": [{"type": "input_text", "text": "Anonymous question"}],
                    "internal_chat_message_metadata_passthrough": metadata,
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "id": "command_anonymous",
                    "call_id": "call_command",
                    "status": "completed",
                    "input": "const result = await tools.exec_command({cmd: 'git status'}); text(result.output);",
                    "internal_chat_message_metadata_passthrough": metadata,
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "id": "question_two",
                    "content": [{"type": "input_text", "text": "Anonymous follow-up"}],
                    "internal_chat_message_metadata_passthrough": metadata,
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call_command",
                    "output": [{"type": "text", "text": "anonymous command output"}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "id": "web_wrapper_anonymous",
                    "call_id": "call_web",
                    "status": "completed",
                    "input": "const result = await tools.web__run({search_query:[{q:'example'}]}); text(result);",
                    "internal_chat_message_metadata_passthrough": metadata,
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "web_search_end",
                    "call_id": "search_anonymous",
                    "query": "anonymous documentation",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "id": "patch_wrapper_anonymous",
                    "call_id": "call_patch",
                    "status": "completed",
                    "input": "await tools.apply_patch('anonymous patch');",
                    "internal_chat_message_metadata_passthrough": metadata,
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "patch_apply_end",
                    "call_id": "patch_anonymous",
                    "turn_id": turn_id,
                    "success": True,
                    "changes": {
                        "/workspace/src/app.py": {
                            "type": "update",
                            "unified_diff": "@@ -1 +1 @@\n-old\n+new\n",
                        },
                    },
                },
            },
            {
                "type": "response_item",
                "payload": {"type": "reasoning", "text": "private reasoning"},
            },
            {
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": turn_id},
            },
        ]
        with tempfile.TemporaryDirectory() as root:
            rollout = Path(root) / "rollout-anonymous.jsonl"
            rollout.write_text(
                "".join(json.dumps(event) + "\n" for event in events),
                encoding="utf-8",
            )
            blocks = activity_blocks(str(rollout), [turn_id])
            command_detail = rollout_activity_detail(str(rollout), blocks[0]["id"])
            patch_detail = rollout_activity_detail(str(rollout), blocks[2]["id"])
            history = conversation_history_page(
                {"turns": []},
                thread_id="thread_anonymous",
                message_blocks=[
                    {
                        "id": "user-one",
                        "turnKey": blocks[0]["turnKey"],
                        "questionKey": "appserver-question-1111111111111111",
                        "segmentKey": "appserver-question-1111111111111111",
                        "kind": "user",
                        "text": "Anonymous question",
                    },
                    {
                        "id": "user-two",
                        "turnKey": blocks[0]["turnKey"],
                        "questionKey": "appserver-question-2222222222222222",
                        "segmentKey": "appserver-question-2222222222222222",
                        "kind": "user",
                        "text": "Anonymous follow-up",
                    },
                ],
                durable_activity=blocks,
                limit=12,
                max_page_turns=24,
                page_char_budget=100_000,
                preview_chars=80,
                updated_at=lambda: "now",
            )

        self.assertEqual([block["kind"] for block in blocks], ["process", "process", "process"])
        self.assertEqual(blocks[0]["text"], "Ran git status")
        self.assertEqual(blocks[1]["text"], "Searched anonymous documentation")
        self.assertEqual(blocks[2]["text"], "Edited app.py")
        self.assertEqual(command_detail["output"], "anonymous command output")
        self.assertIn("+new", patch_detail["changes"][0]["diff"])
        self.assertNotIn("private reasoning", "\n".join(block["text"] for block in blocks))
        self.assertTrue(all(block["turnKey"].startswith("appserver-turn-") for block in blocks))
        self.assertTrue(all(block["id"].startswith("appserver-item-") for block in blocks))
        self.assertTrue(
            all(
                re.fullmatch(
                    r"appserver-turn-[0-9a-f]{16}:[0-9a-f]{64}:[1-9][0-9]*",
                    block["questionReference"],
                )
                for block in blocks
            )
        )
        self.assertNotEqual(blocks[0]["questionReference"], blocks[1]["questionReference"])
        self.assertIn("Ran git status", [block["text"] for block in history["turns"][0]["blocks"]])
        self.assertNotIn("Searched anonymous documentation", [block["text"] for block in history["turns"][0]["blocks"]])
        self.assertIn("Searched anonymous documentation", [block["text"] for block in history["turns"][1]["blocks"]])
        self.assertTrue(all("questionReference" not in block for turn in history["turns"] for block in turn["blocks"]))

    def test_rollout_activity_index_waits_for_complete_records_and_catches_up(self) -> None:
        event = {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "id": "command_incremental",
                "status": "completed",
                "input": "await tools.exec_command({cmd: 'true'});",
                "internal_chat_message_metadata_passthrough": {"turn_id": "turn_incremental"},
            },
        }
        encoded = json.dumps(event).encode("utf-8")
        with tempfile.TemporaryDirectory() as root:
            rollout = Path(root) / "rollout-incremental.jsonl"
            rollout.write_bytes(encoded)
            self.assertEqual(activity_blocks(str(rollout), ["turn_incremental"]), [])
            with rollout.open("ab") as handle:
                handle.write(b"\n")
            blocks = activity_blocks(str(rollout), ["turn_incremental"])
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["text"], "Ran true")

    def test_rollout_question_references_are_scoped_by_protocol_turn(self) -> None:
        events = []
        for index in (1, 2):
            turn_id = f"turn_same_question_{index}"
            events.extend([
                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "id": f"question_{index}",
                        "content": [{"type": "input_text", "text": "Same anonymous question"}],
                        "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "exec",
                        "id": f"command_{index}",
                        "input": "await tools.exec_command({cmd: 'true'});",
                        "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
                    },
                },
                {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn_id}},
            ])
        with tempfile.TemporaryDirectory() as root:
            rollout = Path(root) / "rollout-same-question.jsonl"
            rollout.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
            blocks = activity_blocks(str(rollout), ["turn_same_question_1", "turn_same_question_2"])

        self.assertEqual(len(blocks), 2)
        self.assertNotEqual(blocks[0]["questionReference"], blocks[1]["questionReference"])

    def test_history_merges_durable_activity_once_and_keeps_live_exit_status(self) -> None:
        turn_key = "appserver-turn-anonymous"
        options = {
            "thread_id": "thread_anonymous",
            "limit": 12,
            "max_page_turns": 24,
            "page_char_budget": 100_000,
            "preview_chars": 80,
            "updated_at": lambda: "now",
        }
        history = conversation_history_page(
            {"turns": []},
            message_blocks=[
                {
                    "id": "user-anonymous",
                    "turnKey": turn_key,
                    "kind": "user",
                    "role": "user",
                    "text": "Anonymous question",
                    "final": True,
                },
                {
                    "id": "command-live",
                    "turnKey": turn_key,
                    "kind": "process",
                    "role": "process",
                    "text": "Ran git status · exit 0",
                    "final": True,
                },
                {
                    "id": "search-same-id",
                    "turnKey": turn_key,
                    "kind": "process",
                    "role": "process",
                    "text": "Searched anonymous docs",
                    "final": True,
                },
                {
                    "id": "answer-anonymous",
                    "turnKey": turn_key,
                    "kind": "output",
                    "role": "assistant",
                    "text": "Anonymous answer",
                    "final": True,
                },
            ],
            durable_activity=[
                {
                    "id": "command-durable",
                    "turnKey": turn_key,
                    "kind": "process",
                    "role": "process",
                    "text": "Ran git status",
                    "final": True,
                },
                {
                    "id": "search-same-id",
                    "turnKey": turn_key,
                    "kind": "process",
                    "role": "process",
                    "text": "Searched anonymous docs",
                    "final": True,
                },
                {
                    "id": "edit-durable",
                    "turnKey": turn_key,
                    "kind": "process",
                    "role": "process",
                    "text": "Edited app.py",
                    "final": True,
                },
            ],
            **options,
        )
        blocks = history["turns"][0]["blocks"]
        self.assertEqual([block["kind"] for block in blocks], ["user", "process", "process", "process", "output"])
        self.assertEqual(sum(block["text"].startswith("Ran git status") for block in blocks), 1)
        self.assertIn("Ran git status · exit 0", [block["text"] for block in blocks])
        self.assertEqual(sum(block["text"] == "Searched anonymous docs" for block in blocks), 1)
        self.assertIn("Edited app.py", [block["text"] for block in blocks])

    def test_history_keeps_durable_activity_in_its_user_message_segment(self) -> None:
        turn_key = "appserver-turn-shared"
        first_key = "appserver-question-1111111111111111"
        second_key = "appserver-question-2222222222222222"
        history = conversation_history_page(
            {"turns": []},
            thread_id="thread_anonymous",
            message_blocks=[
                {"id": "user-one", "turnKey": turn_key, "questionKey": first_key, "segmentKey": first_key, "kind": "user", "text": "First"},
                {"id": "answer-one", "turnKey": turn_key, "segmentKey": first_key, "kind": "output", "text": "First answer"},
                {"id": "user-two", "turnKey": turn_key, "questionKey": second_key, "segmentKey": second_key, "kind": "user", "text": "Second"},
                {"id": "answer-two", "turnKey": turn_key, "segmentKey": second_key, "kind": "output", "text": "Second answer"},
            ],
            durable_activity=[
                {"id": "command-one", "turnKey": turn_key, "segmentKey": first_key, "kind": "process", "text": "Ran first"},
                {"id": "change-two", "turnKey": turn_key, "segmentKey": second_key, "kind": "process", "text": "Edited second.txt"},
            ],
            limit=12,
            max_page_turns=24,
            page_char_budget=100_000,
            preview_chars=80,
            updated_at=lambda: "now",
        )

        self.assertEqual(history["totalTurns"], 2)
        self.assertIn("Ran first", [block["text"] for block in history["turns"][0]["blocks"]])
        self.assertNotIn("Edited second.txt", [block["text"] for block in history["turns"][0]["blocks"]])
        self.assertIn("Edited second.txt", [block["text"] for block in history["turns"][1]["blocks"]])

    def test_durable_recovery_suppresses_unmatched_completed_wrapper_duplicates(self) -> None:
        turn_key = "appserver-turn-recovered"
        question_key = "appserver-question-3333333333333333"
        history = conversation_history_page(
            {"turns": []},
            thread_id="thread_recovered",
            message_blocks=[
                {
                    "id": "user-recovered",
                    "turnKey": turn_key,
                    "questionKey": question_key,
                    "segmentKey": question_key,
                    "kind": "user",
                    "text": "Recovered question",
                },
                {
                    "id": "wrapper-live",
                    "turnKey": turn_key,
                    "segmentKey": question_key,
                    "kind": "process",
                    "text": "Ran wrapper command",
                    "final": True,
                    "activity": {"type": "command", "status": "completed", "title": "wrapper command"},
                },
            ],
            durable_activity=[
                {
                    "id": "command-durable",
                    "turnKey": turn_key,
                    "segmentKey": question_key,
                    "kind": "process",
                    "text": "Ran actual command",
                    "final": True,
                    "activity": {"type": "command", "status": "completed", "title": "actual command"},
                },
            ],
            limit=12,
            max_page_turns=24,
            page_char_budget=100_000,
            preview_chars=80,
            updated_at=lambda: "now",
        )
        process = [block["text"] for block in history["turns"][0]["blocks"] if block["kind"] == "process"]

        self.assertEqual(process, ["Ran actual command"])


class TransportTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.socket = FakeSocket()
        self.notifications: list[tuple[str, dict]] = []

        async def connector():
            return self.socket

        async def notification(method: str, params: dict) -> None:
            self.notifications.append((method, params))

        self.client = AsyncCodexAppServerClient(
            connector=connector,
            client_version="test",
            notification_handler=notification,
            rpc_timeout=0.5,
            random_value=lambda: 0.5,
        )
        await self.client.connect()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def test_notification_between_request_and_response_is_not_dropped(self) -> None:
        task = asyncio.create_task(self.client.rpc("thread/read", {"threadId": "thread_demo"}))
        await asyncio.sleep(0)
        request = self.socket.sent[-1]
        await self.socket.incoming.put(json.dumps({"method": "turn/started", "params": {"threadId": "thread_demo"}}))
        await self.socket.incoming.put(json.dumps({"id": request["id"], "result": {"thread": {"id": "thread_demo"}}}))
        result = await task
        await asyncio.sleep(0)
        self.assertEqual(result["thread"]["id"], "thread_demo")
        self.assertEqual(self.notifications[0][0], "turn/started")

    async def test_initialize_retries_without_experimental_capability_when_rejected(self) -> None:
        socket = FakeSocket(reject_experimental=True)

        async def connector():
            return socket

        client = AsyncCodexAppServerClient(
            connector=connector,
            client_version="test",
            rpc_timeout=0.5,
        )
        try:
            result = await client.connect()
            initialize = [message for message in socket.sent if message.get("method") == "initialize"]
            self.assertEqual(result, {"userAgent": "test/0"})
            self.assertEqual(len(initialize), 2)
            self.assertEqual(initialize[0]["params"]["capabilities"], {"experimentalApi": True})
            self.assertIsNone(initialize[1]["params"]["capabilities"])
            self.assertFalse(client.experimental_api)
        finally:
            await client.close()

    async def test_server_request_is_answered_and_unknown_request_fails_closed(self) -> None:
        self.client.register_server_request("item/requestApproval", lambda params: {"decision": "decline"})
        await self.socket.incoming.put(json.dumps({"id": "srv-1", "method": "item/requestApproval", "params": {}}))
        await self.socket.incoming.put(json.dumps({"id": "srv-2", "method": "future/request", "params": {}}))
        for _attempt in range(20):
            if any(message.get("id") == "srv-2" for message in self.socket.sent):
                break
            await asyncio.sleep(0.01)
        supported = next(message for message in self.socket.sent if message.get("id") == "srv-1")
        unknown = next(message for message in self.socket.sent if message.get("id") == "srv-2")
        self.assertEqual(supported["result"], {"decision": "decline"})
        self.assertEqual(unknown["error"]["code"], -32601)

    async def test_overload_response_retries_only_after_explicit_rejection(self) -> None:
        task = asyncio.create_task(self.client.rpc("thread/read", {}, overload_attempts=2))
        await asyncio.sleep(0)
        first = self.socket.sent[-1]
        await self.socket.incoming.put(json.dumps({
            "id": first["id"],
            "error": {"code": -32001, "message": "Server overloaded; retry later."},
        }))
        for _attempt in range(50):
            requests = [message for message in self.socket.sent if message.get("method") == "thread/read"]
            if len(requests) == 2:
                break
            await asyncio.sleep(0.01)
        second = requests[-1]
        await self.socket.incoming.put(json.dumps({"id": second["id"], "result": {"ok": True}}))
        self.assertEqual(await task, {"ok": True})

    async def test_disconnect_fails_pending_request(self) -> None:
        task = asyncio.create_task(self.client.rpc("thread/read", {}))
        await asyncio.sleep(0)
        await self.socket.incoming.put(ConnectionError("lost"))
        with self.assertRaises(AppServerUnavailable):
            await task
        self.assertFalse(self.client.ready)
        self.assertEqual(self.client.pending_count, 0)

    async def test_slow_notification_consumer_disconnects_instead_of_unbounded_buffering(self) -> None:
        socket = FakeSocket()
        release = asyncio.Event()
        disconnected = asyncio.Event()

        async def connector():
            return socket

        async def slow_notification(_method: str, _params: dict) -> None:
            await release.wait()

        async def on_disconnect(_error: BaseException) -> None:
            disconnected.set()

        client = AsyncCodexAppServerClient(
            connector=connector,
            client_version="test",
            notification_handler=slow_notification,
            disconnect_handler=on_disconnect,
            notification_queue_size=1,
            rpc_timeout=0.5,
        )
        await client.connect()
        await socket.incoming.put(json.dumps({"method": "one", "params": {}}))
        await asyncio.sleep(0)
        await socket.incoming.put(json.dumps({"method": "two", "params": {}}))
        await socket.incoming.put(json.dumps({"method": "three", "params": {}}))
        await asyncio.wait_for(disconnected.wait(), timeout=0.5)
        release.set()
        await client.close()

        self.assertFalse(client.ready)
        self.assertLessEqual(client._notification_queue.qsize(), 1)


if __name__ == "__main__":
    unittest.main()
