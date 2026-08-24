import sys
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import server


class CodexTranscriptTest(unittest.TestCase):
    def setUp(self):
        with server._codex_rollout_cache_lock:
            server._codex_rollout_cache.clear()
        with server._codex_history_cache_lock:
            server._codex_history_cache.clear()
            server._codex_history_path_locks.clear()

    def tearDown(self):
        with server._rate_limit_lock:
            server._rate_limit_cache = None
            server._rate_limit_cache_at = 0.0
            server._rate_limit_refreshing = False

    def test_context_usage_uses_agent_reported_total_and_window(self):
        event = {
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 45_659,
                        "output_tokens": 1_761,
                        "total_tokens": 47_420,
                    },
                    "model_context_window": 258_400,
                },
            },
        }
        with tempfile.TemporaryDirectory() as root:
            history = Path(root) / "rollout.jsonl"
            history.write_text(json.dumps(event) + "\n", encoding="utf-8")

            usage = server.latest_context_usage(str(history))

        self.assertEqual(usage["usedTokens"], 47_420)
        self.assertEqual(usage["contextWindow"], 258_400)
        self.assertEqual(usage["contextWindowSource"], "agent-reported")
        self.assertEqual(usage["percent"], 18.4)

    def test_configured_codex_executable_wins_over_service_path(self):
        with tempfile.TemporaryDirectory() as root:
            executable = Path(root) / "codex"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
            with mock.patch.dict(
                server.os.environ,
                {
                    "FARYO_CODEX_BIN": str(executable),
                    "FARYO_CODEX_BIN_PINNED": "1",
                },
                clear=False,
            ):
                with mock.patch.object(server.shutil, "which", return_value="/usr/bin/codex"):
                    self.assertEqual(server.agent_launch_executable("codex"), str(executable))

    def test_invalid_configured_codex_executable_fails_before_tmux(self):
        with mock.patch.dict(
            server.os.environ,
            {
                "FARYO_CODEX_BIN": "/missing/codex",
                "FARYO_CODEX_BIN_PINNED": "1",
            },
            clear=False,
        ):
            with self.assertRaises(server.OwnerError) as raised:
                server.agent_launch_executable("codex")

        self.assertEqual(raised.exception.status, server.HTTPStatus.BAD_GATEWAY)

    def test_login_shell_falls_back_to_bash_when_zsh_is_missing(self):
        def which(command):
            return {"zsh": None, "bash": "/bin/bash", "sh": "/bin/sh"}.get(command)

        with mock.patch.dict(server.os.environ, {"FARYO_AGENT_SHELL": "", "SHELL": ""}, clear=False):
            with mock.patch.object(server.shutil, "which", side_effect=which):
                self.assertEqual(server.agent_login_shell(), "/bin/bash")

    def test_codex_executable_falls_back_to_service_path(self):
        with mock.patch.dict(server.os.environ, {"FARYO_CODEX_BIN_PINNED": "0"}, clear=False):
            server.os.environ.pop("FARYO_CODEX_BIN", None)
            with mock.patch.object(
                server.codex_runtime,
                "resolve_codex",
                return_value="/usr/bin/codex",
            ):
                self.assertEqual(server.agent_launch_executable("codex"), "/usr/bin/codex")

    def test_app_server_uses_the_node_next_to_a_configured_codex_script(self):
        with tempfile.TemporaryDirectory() as root:
            version = Path(root) / "versions" / "node" / "v1"
            node = version / "bin" / "node"
            script = version / "lib" / "node_modules" / "pkg" / "bin" / "codex.js"
            node.parent.mkdir(parents=True)
            script.parent.mkdir(parents=True)
            node.write_text("runtime", encoding="utf-8")
            script.write_text("cli", encoding="utf-8")
            node.chmod(0o755)
            with mock.patch.object(server, "agent_launch_executable", return_value=str(script)):
                command = server.codex_app_server_argv("app-server", "--listen", "stdio://")

        self.assertEqual(command, [str(node), str(script), "app-server", "--listen", "stdio://"])

    def test_codex_update_restarts_version_bound_helpers_once(self):
        previous = server._codex_app_server_launch_version
        server._codex_app_server_launch_version = "0.148.0"
        try:
            with (
                mock.patch.object(server, "installed_codex_version", return_value="0.149.0"),
                mock.patch.object(server, "stop_codex_app_server") as stop,
                mock.patch.object(server, "refresh_command_catalog_if_needed") as refresh,
            ):
                self.assertTrue(server.reconcile_codex_installation())
                self.assertFalse(server.reconcile_codex_installation())
        finally:
            server._codex_app_server_launch_version = previous

        stop.assert_called_once_with()
        refresh.assert_called_once_with()

    def test_managed_update_marker_is_reconciled_and_consumed(self):
        values = iter(["updated"])

        def option(_config, _name, key, value=None):
            if value is not None:
                self.assertEqual((key, value), ("@faryo_codex_update", "reconciled"))
                return ""
            return next(values)

        with (
            mock.patch.object(server, "tmux_session_option", side_effect=option),
            mock.patch.object(server, "reconcile_codex_installation", return_value=True) as reconcile,
        ):
            self.assertTrue(server.reconcile_managed_codex_update(mock.sentinel.config, "faryo1"))

        reconcile.assert_called_once_with()

    def test_preserves_original_latex_from_agent_messages(self):
        formula = (
            "A generic bound gives\n\n"
            "\\[\n"
            "\\|w(s)\\|\\le C.\n"
            "\\]\n\n"
            "\\[\n"
            "q(s)=\\begin{cases}\n"
            "a,&0\\le s<s_0,\\\\\n"
            "b,&s\\ge s_0.\n"
            "\\end{cases}\n"
            "\\]"
        )
        thread = {
            "turns": [{
                "items": [
                    {"type": "userMessage", "content": [{"type": "text", "text": "Render generic notation"}]},
                    {"type": "agentMessage", "phase": "final_answer", "text": formula},
                ]
            }]
        }

        transcript = server.codex_thread_transcript(thread, 320)

        self.assertIn("› Render generic notation", transcript)
        self.assertIn("\\|w(s)\\|\\le C.", transcript)
        self.assertIn("a,&0\\le s<s_0,\\\\", transcript)
        self.assertIn("\\begin{cases}", transcript)

    def test_line_budget_keeps_the_latest_turn_intact(self):
        thread = {
            "turns": [
                {"items": [
                    {"type": "userMessage", "content": [{"type": "text", "text": "old"}]},
                    {"type": "agentMessage", "text": "old answer"},
                ]},
                {"items": [
                    {"type": "userMessage", "content": [{"type": "text", "text": "new"}]},
                    {"type": "agentMessage", "text": "\\[\nx^2+y^2\n\\]"},
                ]},
            ]
        }

        with mock.patch.object(server, "CODEX_TRANSCRIPT_MIN_TURNS", 1):
            transcript = server.codex_thread_transcript(thread, 4)

        self.assertNotIn("old answer", transcript)
        self.assertIn("› new", transcript)
        self.assertIn("\\[\nx^2+y^2\n\\]", transcript)

    def test_formula_heavy_turn_does_not_hide_recent_conversation_history(self):
        messages = []
        for index in range(14):
            messages.extend((
                ("user", f"question {index}"),
                ("assistant", "\n".join(f"formula row {row}" for row in range(100))),
            ))

        transcript = server.codex_message_transcript(messages, 20)

        self.assertNotIn("› question 1\n", transcript)
        self.assertIn("› question 2\n", transcript)
        self.assertIn("› question 13\n", transcript)
        self.assertEqual(transcript.count("› question "), server.CODEX_TRANSCRIPT_MIN_TURNS)

    def test_recent_history_keeps_a_hard_character_ceiling(self):
        messages = []
        for index in range(6):
            messages.extend((("user", f"question {index}"), ("assistant", "x" * 100)))

        with mock.patch.object(server, "CODEX_TRANSCRIPT_CHAR_BUDGET", 250):
            transcript = server.codex_message_transcript(messages, 1)

        self.assertNotIn("› question 3\n", transcript)
        self.assertIn("› question 4\n", transcript)
        self.assertIn("› question 5\n", transcript)
        self.assertLessEqual(transcript.count("x"), 200)

    def test_rollout_cache_collects_minimum_turns_beyond_soft_line_budget(self):
        events = []
        for index in range(12):
            events.extend((
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": f"question {index}"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "line one\nline two"}],
                    },
                },
            ))
        events.append({
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"total_tokens": 10},
                    "model_context_window": 1_000,
                },
            },
        })
        with tempfile.TemporaryDirectory() as root:
            history = Path(root) / "rollout.jsonl"
            history.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
            with mock.patch.object(server, "CODEX_ROLLOUT_CACHE_LINE_BUDGET", 4):
                state = server.codex_rollout_state(str(history))

        self.assertEqual(
            sum(1 for role, _text in state["messages"] if role == "user"),
            server.CODEX_ROLLOUT_CACHE_MIN_TURNS,
        )

    def test_rollout_goal_status_is_incremental_and_objective_free(self):
        def goal_call(call_id, method):
            return {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": call_id,
                    "input": f"const result = await tools.{method}({{}}); text(result);",
                },
            }

        def goal_output(call_id, status, tokens):
            return {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": call_id,
                    "output": [{
                        "type": "input_text",
                        "text": json.dumps({
                            "goal": {
                                "threadId": "private-thread",
                                "objective": "SECRET_GOAL_OBJECTIVE",
                                "status": status,
                                "tokensUsed": tokens,
                                "timeUsedSeconds": 12,
                            },
                        }),
                    }],
                },
            }

        with tempfile.TemporaryDirectory() as root:
            history = Path(root) / "rollout.jsonl"
            events = [
                goal_call("goal-create", "create_goal"),
                goal_output("goal-create", "active", 10),
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "fixture question"}],
                    },
                },
            ]
            history.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

            first = server.latest_goal_status(str(history))
            with history.open("a", encoding="utf-8") as fh:
                for event in (
                    goal_call("goal-complete", "update_goal"),
                    goal_output("goal-complete", "complete", 20),
                ):
                    fh.write(json.dumps(event) + "\n")
            second = server.latest_goal_status(str(history))
            with history.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "next task after clear"}],
                    },
                }) + "\n")
            cleared_by_next_turn = server.latest_goal_status(str(history))

        self.assertEqual(first, {"status": "active", "tokensUsed": 10, "timeUsedSeconds": 12})
        self.assertEqual(second, {"status": "complete", "tokensUsed": 20, "timeUsedSeconds": 12})
        self.assertEqual(cleared_by_next_turn, {"status": "none"})
        self.assertNotIn("SECRET_GOAL_OBJECTIVE", json.dumps(first) + json.dumps(second))

    def test_initial_tail_hides_completed_goal_before_newer_user_turn(self):
        events = [
            {
                "type": "response_item",
                "payload": {"type": "thread_goal_updated", "goal": {"status": "complete", "objective": "private"}},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "new work"}],
                },
            },
        ]
        with tempfile.TemporaryDirectory() as root:
            history = Path(root) / "rollout.jsonl"
            history.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
            status = server.latest_goal_status(str(history))

        self.assertEqual(status, {"status": "none"})

    def test_rollout_direct_goal_clear_hides_older_status(self):
        events = [
            {
                "type": "response_item",
                "payload": {"type": "thread_goal_updated", "goal": {"status": "active", "objective": "private"}},
            },
            {"type": "response_item", "payload": {"type": "thread_goal_updated", "goal": None}},
        ]
        with tempfile.TemporaryDirectory() as root:
            history = Path(root) / "rollout.jsonl"
            history.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
            status = server.latest_goal_status(str(history))

        self.assertEqual(status, {"status": "none"})

    def test_rollout_transcript_is_incremental_and_preserves_markdown_math(self):
        events = [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Show the model"}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "## Result\n\n\\[q(t)=\\begin{cases}a,&t<1,\\\\b,&t\\ge1.\\end{cases}\\]"}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "internal instructions"}],
                },
            },
        ]
        with tempfile.TemporaryDirectory() as root:
            history = Path(root) / "rollout.jsonl"
            history.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

            first = server.codex_rollout_transcript(str(history), 320)
            with history.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Next question"}],
                    },
                }) + "\n")
            second = server.codex_rollout_transcript(str(history), 320)

        self.assertIn("› Show the model", first)
        self.assertIn("## Result", first)
        self.assertIn("\\begin{cases}", first)
        self.assertNotIn("internal instructions", first)
        self.assertEqual(second.count("› Show the model"), 1)
        self.assertIn("› Next question", second)

    def test_full_history_page_indexes_every_question_and_pages_backward(self):
        events = []
        for index in range(40):
            events.extend((
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": f"question {index}"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": f"answer {index}\n\\[x_{{{index}}}=u\\]"}],
                    },
                },
                {"type": "response_item", "payload": {"type": "function_call", "name": "ignored"}},
            ))
        with tempfile.TemporaryDirectory() as root:
            history = Path(root) / "rollout.jsonl"
            history.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

            latest = server.codex_conversation_history_page(str(history), limit=12)
            previous = server.codex_conversation_history_page(str(history), limit=12, cursor=latest["olderCursor"])
            around = server.codex_conversation_history_page(str(history), limit=12, around=0)

        self.assertEqual(latest["totalTurns"], 40)
        self.assertEqual((latest["start"], latest["end"]), (28, 40))
        self.assertEqual(len(latest["questions"]), 40)
        self.assertEqual(len({item["key"] for item in latest["questions"]}), 40)
        self.assertEqual([item["index"] for item in latest["turns"]], list(range(28, 40)))
        self.assertIn("\\[x_{39}=u\\]", latest["turns"][-1]["text"])
        self.assertEqual([block["kind"] for block in latest["turns"][-1]["blocks"]], ["user", "output"])
        self.assertEqual(latest["turns"][-1]["blocks"][0]["questionKey"], latest["turns"][-1]["key"])
        self.assertEqual((previous["start"], previous["end"]), (16, 28))
        self.assertEqual((around["start"], around["end"]), (0, 12))
        self.assertEqual(around["questions"][0]["preview"], "question 0")
        self.assertNotIn(str(history), json.dumps(latest))

    def test_full_history_preserves_literal_tui_prompts_inside_one_user_block(self):
        quoted_tui = """Investigate this transcript:
› >_ OpenAI Codex (v0.000.0)
› Ask Codex to do anything
› ? for shortcuts
› Do you trust the contents of this directory?
› Press enter to continue

Keep the formula \\(x^2+y^2\\) in the same message."""
        events = [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": quoted_tui}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "One structured answer."}],
                },
            },
        ]
        with tempfile.TemporaryDirectory() as root:
            history = Path(root) / "rollout.jsonl"
            history.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
            page = server.codex_conversation_history_page(str(history), limit=12)

        self.assertEqual(page["totalTurns"], 1)
        self.assertEqual(len(page["turns"]), 1)
        blocks = page["turns"][0]["blocks"]
        self.assertEqual([block["kind"] for block in blocks], ["user", "output"])
        self.assertEqual(blocks[0]["text"], quoted_tui)
        self.assertEqual(blocks[0]["questionKey"], page["questions"][0]["key"])
        self.assertEqual(len({block["id"] for block in blocks}), 2)

    def test_full_history_index_waits_for_complete_records_and_expires_old_cursor(self):
        def message(role, text):
            return {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": role,
                    "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
                },
            }

        with tempfile.TemporaryDirectory() as root:
            history = Path(root) / "rollout.jsonl"
            initial = [message("user", "one"), message("assistant", "answer one"), message("user", "two")]
            history.write_text("\n".join(json.dumps(event) for event in initial) + "\n", encoding="utf-8")
            first = server.codex_conversation_history_page(str(history))
            old_cursor = server.codex_history_cursor(first["revision"], 1)

            pending = json.dumps(message("user", "three"))
            split_at = len(pending) // 2
            with history.open("a", encoding="utf-8") as handle:
                handle.write(pending[:split_at])
            partial = server.codex_conversation_history_page(str(history))
            with history.open("a", encoding="utf-8") as handle:
                handle.write(pending[split_at:] + "\n" + json.dumps(message("assistant", "answer three")) + "\n")
            appended = server.codex_conversation_history_page(str(history))

            replacement = Path(root) / "replacement.jsonl"
            replacement.write_text(json.dumps(message("user", "replacement")) + "\n", encoding="utf-8")
            replacement.replace(history)
            replaced = server.codex_conversation_history_page(str(history))
            with self.assertRaises(server.OwnerError) as expired:
                server.codex_conversation_history_page(str(history), cursor=old_cursor)

        self.assertEqual(first["totalTurns"], 2)
        self.assertEqual(partial["totalTurns"], 2)
        self.assertEqual(appended["totalTurns"], 3)
        self.assertEqual(first["revision"], appended["revision"])
        self.assertEqual(replaced["totalTurns"], 1)
        self.assertNotEqual(first["revision"], replaced["revision"])
        self.assertEqual(expired.exception.status, server.HTTPStatus.CONFLICT)

    def test_rollout_parser_waits_for_a_complete_jsonl_record(self):
        event = {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "complete after append"}],
            },
        }
        encoded = json.dumps(event)
        split_at = len(encoded) // 2
        with tempfile.TemporaryDirectory() as root:
            history = Path(root) / "rollout.jsonl"
            history.write_text(encoded[:split_at], encoding="utf-8")
            self.assertEqual(server.codex_rollout_transcript(str(history), 320), "")
            with history.open("a", encoding="utf-8") as fh:
                fh.write(encoded[split_at:] + "\n")
            transcript = server.codex_rollout_transcript(str(history), 320)

        self.assertEqual(transcript, "• complete after append")

    def test_large_rollout_initializes_from_a_bounded_tail(self):
        old_event = {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "old prefix must not be cached"}],
            },
        }
        usage_event = {
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
                    "model_context_window": 1_000,
                },
            },
        }
        latest_event = {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "latest bounded tail"}],
            },
        }
        with tempfile.TemporaryDirectory() as root:
            history = Path(root) / "rollout.jsonl"
            prefix = (json.dumps(old_event) + "\n") * 40
            history.write_text(prefix + json.dumps(usage_event) + "\n" + json.dumps(latest_event) + "\n", encoding="utf-8")
            with mock.patch.object(server, "CODEX_ROLLOUT_TAIL_SCAN_BYTES", 640):
                state = server.codex_rollout_state(str(history))

        transcript = server.codex_message_transcript(state["messages"], 320)
        self.assertLess(transcript.count("old prefix"), 40)
        self.assertIn("latest bounded tail", transcript)
        self.assertEqual(state["contextUsage"]["usedTokens"], 150)

    def test_rollout_cache_evicts_the_least_recent_path(self):
        event = {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "bounded"}],
            },
        }
        with tempfile.TemporaryDirectory() as root, mock.patch.object(server, "CODEX_ROLLOUT_CACHE_MAX_PATHS", 2):
            paths = []
            for index in range(3):
                path = Path(root) / f"rollout-{index}.jsonl"
                path.write_text(json.dumps(event) + "\n", encoding="utf-8")
                paths.append(str(path))
                server.codex_rollout_messages(str(path))

            with server._codex_rollout_cache_lock:
                keys = list(server._codex_rollout_cache)

        self.assertEqual(keys, paths[1:])

    def test_large_unread_gap_rebuilds_from_the_latest_tail(self):
        def message(text):
            return {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                },
            }

        with tempfile.TemporaryDirectory() as root:
            history = Path(root) / "rollout.jsonl"
            history.write_text(json.dumps(message("before gap")) + "\n", encoding="utf-8")
            self.assertIn("before gap", server.codex_rollout_transcript(str(history), 320))
            with history.open("a", encoding="utf-8") as fh:
                for _ in range(20):
                    fh.write(json.dumps({"type": "ignored", "padding": "x" * 48}) + "\n")
                fh.write(json.dumps(message("after gap")) + "\n")
            with mock.patch.object(server, "CODEX_ROLLOUT_MAX_CATCHUP_BYTES", 128), \
                 mock.patch.object(server, "CODEX_ROLLOUT_TAIL_SCAN_BYTES", 256):
                transcript = server.codex_rollout_transcript(str(history), 320)

        self.assertNotIn("before gap", transcript)
        self.assertIn("after gap", transcript)

    def test_structured_capture_prefers_the_durable_rollout(self):
        event = {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "structured answer"}],
            },
        }
        with tempfile.TemporaryDirectory() as root:
            history = Path(root) / "rollout.jsonl"
            history.write_text(json.dumps(event) + "\n", encoding="utf-8")
            thread = {"id": "thread-id", "rollout_path": str(history)}
            with mock.patch.object(server, "get_pane_cwd", return_value=str(root)), \
                 mock.patch.object(server, "active_agent_thread", return_value=thread), \
                 mock.patch.object(server, "cached_codex_thread") as app_server_read:
                capture = server.codex_structured_capture(mock.sentinel.config, 320)

        self.assertEqual(capture, ("• structured answer", "thread-id", "codex-jsonl"))
        app_server_read.assert_not_called()

    def test_structured_capture_preserves_a_valid_empty_thread(self):
        thread = {"id": "thread-id", "rollout_path": ""}
        stored = {"id": "thread-id", "turns": []}
        with mock.patch.object(server, "get_pane_cwd", return_value="/workspace"), \
             mock.patch.object(server, "active_agent_thread", return_value=thread), \
             mock.patch.object(server, "cached_codex_thread", return_value=stored):
            capture = server.codex_structured_capture(mock.sentinel.config, 320)

        self.assertEqual(capture, ("", "thread-id", "codex-app-server"))

    def test_structured_capture_uses_terminal_only_when_structured_reads_fail(self):
        thread = {"id": "thread-id", "rollout_path": ""}
        with mock.patch.object(server, "get_pane_cwd", return_value="/workspace"), \
             mock.patch.object(server, "active_agent_thread", return_value=thread), \
             mock.patch.object(server, "cached_codex_thread", return_value=None):
            capture = server.codex_structured_capture(mock.sentinel.config, 320)

        self.assertIsNone(capture)

    def test_new_managed_ready_tui_is_an_empty_structured_conversation(self):
        def option(_config, _session, key, _value=None):
            return {
                "@faryo_launch_id": "web-anonymous-launch",
                "@faryo_agent_session_id": "",
            }.get(key, "")

        with (
            mock.patch.object(server, "managed_session", return_value=True),
            mock.patch.object(server, "tmux_session_option", side_effect=option),
            mock.patch.object(server, "agent_ready_for_input", return_value=True),
        ):
            self.assertTrue(server.codex_empty_managed_capture(server.Config("faryo1", "token", 0)))

    def test_known_thread_never_masks_a_real_structured_read_failure(self):
        def option(_config, _session, key, _value=None):
            return {
                "@faryo_launch_id": "web-anonymous-launch",
                "@faryo_agent_session_id": "thread-known",
            }.get(key, "")

        with (
            mock.patch.object(server, "managed_session", return_value=True),
            mock.patch.object(server, "tmux_session_option", side_effect=option),
            mock.patch.object(server, "agent_ready_for_input", return_value=True),
        ):
            self.assertFalse(server.codex_empty_managed_capture(server.Config("faryo1", "token", 0)))

    def test_stale_app_server_thread_survives_a_transient_read_failure(self):
        thread = {"turns": [{"items": [{"type": "agentMessage", "text": "cached"}]}]}
        with server._codex_thread_cache_lock:
            server._codex_thread_cache["thread-id"] = (
                time.monotonic() - server.CODEX_TRANSCRIPT_CACHE_TTL - 1,
                thread,
            )
        with mock.patch.object(server, "codex_app_server_request", return_value=None):
            result = server.cached_codex_thread("thread-id")

        self.assertIs(result, thread)

    def test_codex_rate_limit_cache_starts_only_one_background_refresh(self):
        started = []

        class FakeThread:
            def __init__(self, *, target, name, daemon):
                self.target = target
                self.name = name
                self.daemon = daemon

            def start(self):
                started.append(self)

        with mock.patch.object(server.threading, "Thread", FakeThread):
            self.assertIsNone(server.cached_weekly_rate_limit())
            self.assertIsNone(server.cached_weekly_rate_limit())

        self.assertEqual(len(started), 1)
        self.assertEqual(started[0].name, "faryo-codex-rate-limit")

    def test_codex_rate_limit_fetch_reuses_the_shared_app_server(self):
        response = {
            "rateLimits": {
                "secondary": {
                    "usedPercent": 42,
                    "windowDurationMins": 10_080,
                    "resetsAt": 1_800_000_000,
                },
                "limitId": "codex",
                "planType": "example",
            },
        }
        with mock.patch.object(server, "codex_app_server_request", return_value=response) as request:
            result = server.fetch_weekly_rate_limit(timeout=7.0)

        request.assert_called_once_with("account/rateLimits/read", {}, timeout=7.0)
        self.assertEqual(result["usedPercent"], 42.0)
        self.assertEqual(result["windowDurationMins"], 10_080)

    def test_goal_details_use_formal_app_server_rpc_only_on_demand(self):
        config = server.Config("faryo1", "fixture-token", 0)
        result = {
            "goal": {
                "threadId": "thread-id",
                "objective": "Anonymous objective",
                "status": "active",
                "tokensUsed": 12,
                "timeUsedSeconds": 34,
                "createdAt": 100,
                "updatedAt": 200,
            }
        }
        with (
            mock.patch.object(server, "has_session", return_value=True),
            mock.patch.object(server, "get_pane_cwd", return_value="/workspace"),
            mock.patch.object(server, "active_agent_thread", return_value={"id": "thread-id"}),
            mock.patch.object(server, "codex_app_server_request", return_value=result) as request,
        ):
            details = server.goal_details_for_config(config)

        request.assert_called_once_with("thread/goal/get", {"threadId": "thread-id"}, timeout=3.0)
        self.assertEqual(details["objective"], "Anonymous objective")
        self.assertNotIn("threadId", details)

    def test_rate_limit_refresh_failure_does_not_wedge_future_attempts(self):
        with server._rate_limit_lock:
            server._rate_limit_refreshing = True
        with mock.patch.object(server, "fetch_weekly_rate_limit", side_effect=RuntimeError("transient")):
            server.refresh_weekly_rate_limit_cache()
        with server._rate_limit_lock:
            self.assertFalse(server._rate_limit_refreshing)
            self.assertIsNone(server._rate_limit_cache)

    def test_live_tail_starts_at_the_latest_turn_and_redacts_account(self):
        capture = (
            "› old question\n\n"
            "• old answer\n\n"
            "› current question\n"
            "│ Account: person@example.com (Plus)\n"
            "• Ran command\n"
            "• Working (2s • esc to interrupt)"
        )

        tail = server.codex_live_tail(capture)

        self.assertNotIn("old question", tail)
        self.assertIn("› current question", tail)
        self.assertIn("Account: <redacted>", tail)
        self.assertNotIn("person@example.com", tail)

    def test_live_shell_tail_keeps_the_complete_current_turn(self):
        capture = (
            "› previous question\n"
            "│ Account: person@example.com (Plus)\n"
            "• Running sleep 4\n"
            "• Working (1s • esc to interrupt)"
        )

        tail = server.codex_live_tail(capture)

        self.assertIn("› previous question", tail)
        self.assertIn("Account: <redacted>", tail)
        self.assertIn("• Running sleep 4", tail)

    def test_live_tail_keeps_at_most_the_configured_long_window(self):
        capture = "› current question\n" + "\n".join(f"• activity {index}" for index in range(240))

        tail = server.codex_live_tail(capture)

        self.assertEqual(len(tail.splitlines()), server.CODEX_LIVE_TAIL_LINES)
        self.assertIn("activity 239", tail)


if __name__ == "__main__":
    unittest.main()
