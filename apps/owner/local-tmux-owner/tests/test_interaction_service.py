import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import codex_tui_interactions
import interaction_service
from command_timeline import CommandTimelineStore


MODEL_A = """
Select Model and Effort
› 1. model-a (current)  First model
  2. model-b            Second model
Press enter to confirm or esc to go back
"""
MODEL_B = MODEL_A.replace("› 1.", "  1.").replace("  2.", "› 2.")
REASONING = """
Select Reasoning Level for model-b
› 1. Low  Fast
  2. High  Deep
Press enter to confirm or esc to go back
"""


class Config:
    def __init__(self, session="fixture-session", token="fixture-secret"):
        self.session = session
        self.token = token


class Runtime:
    def __init__(self):
        self.screen = MODEL_A
        self.keys = []
        self.literals = []
        self.now = 0.0
        self.ready = False
        self.ready_sequence = []
        self.draft = False
        self.command_text = ""
        self.on_key = None
        self.codex = True
        self.anchor_key = "q-anonymous-2"

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    @contextmanager
    def session_lock(self, _session):
        yield

    def has_session(self, _config):
        return True

    def is_codex(self, _config):
        return self.codex

    def capture(self, _config):
        return self.screen

    def send_key(self, _config, key):
        self.keys.append(key)
        if self.on_key:
            self.on_key(key)

    def send_literal(self, _config, text):
        self.literals.append(text)
        self.command_text = text
        self.screen = f"› {text}"

    def ready_for_input(self, _config):
        if self.ready_sequence:
            return self.ready_sequence.pop(0)
        return self.ready

    def composer_has_draft(self, _config):
        return self.draft

    def composer_contains(self, _config, text):
        return self.command_text == text

    def command_completion_ready(self, _config, _command):
        return True

    def turn_running(self, _config):
        return False

    def command_owner_key(self, _config):
        return "thread:anonymous"

    def command_anchor_key(self, _config):
        return self.anchor_key


class InteractionServiceTest(unittest.TestCase):
    def setUp(self):
        self.runtime = Runtime()
        self.config = Config()
        self.service = interaction_service.InteractionService(
            self.runtime,
            codex_tui_interactions.detect_interaction,
            transition_timeout=0.2,
            poll_interval=0.01,
        )

    def test_snapshot_uses_opaque_ids_and_stable_generation(self):
        first = self.service.snapshot(self.config)
        same = self.service.snapshot(self.config)

        self.assertEqual(first, same)
        self.assertTrue(first["interaction"]["id"].startswith("ix_"))
        self.assertNotIn(self.config.session, first["interaction"]["id"])
        self.assertNotIn("model-a", first["interaction"]["options"][0]["id"])
        self.assertEqual(1, first["interaction"]["generation"])

    def test_selection_change_advances_generation(self):
        first = self.service.snapshot(self.config)
        self.runtime.screen = MODEL_B
        second = self.service.snapshot(self.config)

        self.assertNotEqual(first["interaction"]["id"], second["interaction"]["id"])
        self.assertEqual(2, second["interaction"]["generation"])

    def test_option_response_navigates_and_enters_then_returns_next_stage(self):
        current = self.service.snapshot(self.config)["interaction"]
        target = current["options"][1]["id"]

        def transition(key):
            if key == "Down":
                self.runtime.screen = MODEL_B
            elif key == "Enter":
                self.runtime.screen = REASONING

        self.runtime.on_key = transition
        result = self.service.respond(
            self.config,
            interaction_id=current["id"],
            option_id=target,
            client_request_id="response-0001",
        )

        self.assertEqual(["Down", "Enter"], self.runtime.keys)
        self.assertEqual("reasoning_select", result["interaction"]["kind"])
        self.assertTrue(result["changed"])
        self.assertFalse(result["resolved"])

    def test_stale_response_never_sends_a_key(self):
        current = self.service.snapshot(self.config)["interaction"]
        self.runtime.screen = MODEL_B

        with self.assertRaises(interaction_service.InteractionServiceError) as raised:
            self.service.respond(
                self.config,
                interaction_id=current["id"],
                action="choose",
                client_request_id="response-0002",
            )

        self.assertEqual(409, raised.exception.status)
        self.assertEqual([], self.runtime.keys)

    def test_duplicate_response_returns_receipt_without_another_key(self):
        current = self.service.snapshot(self.config)["interaction"]
        self.runtime.on_key = lambda key: setattr(self.runtime, "screen", "› Ask Codex") if key == "Escape" else None
        first = self.service.respond(
            self.config,
            interaction_id=current["id"],
            action="cancel",
            client_request_id="response-0003",
        )
        duplicate = self.service.respond(
            self.config,
            interaction_id=current["id"],
            action="cancel",
            client_request_id="response-0003",
        )

        self.assertTrue(first["resolved"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(["Escape"], self.runtime.keys)

    def test_exact_command_submits_enter_once_and_returns_pending_menu(self):
        self.runtime.screen = "› Ask Codex"
        self.runtime.ready = True

        def transition(key):
            if key == "Enter":
                self.runtime.command_text = ""
                self.runtime.ready = False
                self.runtime.screen = MODEL_A

        self.runtime.on_key = transition
        result = self.service.begin_command(
            self.config,
            command="/model",
            client_request_id="command-0001",
        )

        self.assertEqual(["/model"], self.runtime.literals)
        self.assertEqual(["Enter"], self.runtime.keys)
        self.assertEqual("pending", result["commandState"])
        self.assertEqual("model_select", result["interaction"]["kind"])

    def test_transient_not_ready_frame_is_rechecked_without_retrying_enter(self):
        self.runtime.screen = "› Ask Codex"
        self.runtime.ready_sequence = [False, False, True]

        def transition(key):
            if key == "Enter":
                self.runtime.command_text = ""
                self.runtime.screen = MODEL_A

        self.runtime.on_key = transition
        result = self.service.begin_command(
            self.config,
            command="/model",
            client_request_id="command-ready-race-1",
        )

        self.assertEqual("pending", result["commandState"])
        self.assertEqual(["/model"], self.runtime.literals)
        self.assertEqual(["Enter"], self.runtime.keys)

    def test_persistently_not_ready_command_sends_no_text_or_key(self):
        self.runtime.screen = "› Ask Codex"
        self.runtime.ready = False

        with self.assertRaises(interaction_service.InteractionServiceError) as raised:
            self.service.begin_command(
                self.config,
                command="/model",
                client_request_id="command-not-ready-1",
            )

        self.assertEqual(409, raised.exception.status)
        self.assertEqual([], self.runtime.literals)
        self.assertEqual([], self.runtime.keys)

    def test_verified_goal_control_executes_while_a_task_is_running(self):
        self.runtime.screen = "Working · esc to interrupt"
        self.runtime.ready = False
        self.runtime.turn_running = lambda _config: True

        def transition(key):
            if key == "Enter":
                self.runtime.command_text = ""
                self.runtime.screen = "Working · esc to interrupt"

        self.runtime.on_key = transition
        result = self.service.begin_command(
            self.config,
            command="/goal clear",
            client_request_id="command-goal-running-1",
        )

        self.assertEqual("running", result["commandState"])
        self.assertEqual(["/goal clear"], self.runtime.literals)
        self.assertEqual(["Enter"], self.runtime.keys)

    def test_inline_command_returns_completed_without_rollout_evidence(self):
        self.runtime.screen = "› Ask Codex"
        self.runtime.ready = True

        def transition(key):
            if key == "Enter":
                self.runtime.command_text = ""
                self.runtime.screen = "gpt-example · Context 1% used\n› Ask Codex"

        self.runtime.on_key = transition
        result = self.service.begin_command(
            self.config,
            command="/status",
            client_request_id="command-0002",
        )

        self.assertEqual("completed", result["commandState"])
        self.assertEqual(["Enter"], self.runtime.keys)

    def test_argument_command_uses_the_same_single_submit_path(self):
        self.runtime.screen = "› Ask Codex"
        self.runtime.ready = True

        def transition(key):
            if key == "Enter":
                self.runtime.command_text = ""
                self.runtime.screen = "› Ask Codex"

        self.runtime.on_key = transition
        result = self.service.begin_command(
            self.config,
            command="/rename Anonymous title",
            client_request_id="command-arg-0001",
        )

        self.assertEqual(["/rename Anonymous title"], self.runtime.literals)
        self.assertEqual(["Enter"], self.runtime.keys)
        self.assertEqual("completed", result["commandState"])

    def test_same_request_id_cannot_be_reused_for_another_command(self):
        self.runtime.screen = "› Ask Codex"
        self.runtime.ready = True
        self.runtime.on_key = lambda key: (
            setattr(self.runtime, "command_text", ""),
            setattr(self.runtime, "screen", "› Ask Codex"),
        ) if key == "Enter" else None
        self.service.begin_command(
            self.config,
            command="/status",
            client_request_id="command-reuse-1",
        )

        with self.assertRaises(interaction_service.InteractionServiceError) as raised:
            self.service.begin_command(
                self.config,
                command="/usage",
                client_request_id="command-reuse-1",
            )

        self.assertEqual(409, raised.exception.status)
        self.assertEqual(["Enter"], self.runtime.keys)

    def test_dangerous_command_requires_explicit_confirmation(self):
        self.runtime.screen = "› Ask Codex"
        self.runtime.ready = True

        with self.assertRaises(interaction_service.InteractionServiceError) as raised:
            self.service.begin_command(
                self.config,
                command="/delete",
                client_request_id="command-0003",
            )

        self.assertEqual(409, raised.exception.status)
        self.assertEqual([], self.runtime.keys)
        self.assertEqual([], self.runtime.literals)

    def test_confirmed_exit_completes_when_codex_process_leaves(self):
        self.runtime.screen = "› Ask Codex"
        self.runtime.ready = True

        def transition(key):
            if key == "Enter":
                self.runtime.command_text = ""
                self.runtime.codex = False
                self.runtime.ready = False
                self.runtime.screen = "$ shell"

        self.runtime.on_key = transition
        result = self.service.begin_command(
            self.config,
            command="/exit",
            client_request_id="command-exit-01",
            confirmed=True,
        )

        self.assertEqual("completed", result["commandState"])
        self.assertEqual(["Enter"], self.runtime.keys)

    def test_tui_command_timeline_waits_and_completes_without_becoming_a_message(self):
        with tempfile.TemporaryDirectory() as temp:
            store = CommandTimelineStore(Path(temp) / "commands.json")
            service = interaction_service.InteractionService(
                self.runtime,
                codex_tui_interactions.detect_interaction,
                command_timeline=store,
                transition_timeout=0.2,
                poll_interval=0.01,
            )
            self.runtime.screen = "› Ask Codex"
            self.runtime.ready = True

            def open_model(key):
                if key == "Enter":
                    self.runtime.command_text = ""
                    self.runtime.ready = False
                    self.runtime.screen = MODEL_A

            self.runtime.on_key = open_model
            opened = service.begin_command(
                self.config,
                command="/model",
                client_request_id="command-timeline-model-1",
            )
            self.assertEqual(opened["commandEvent"]["status"], "waiting")
            self.runtime.on_key = lambda key: setattr(self.runtime, "screen", "› Ask Codex") if key == "Escape" else None
            closed = service.respond(
                self.config,
                interaction_id=opened["interaction"]["id"],
                action="cancel",
                client_request_id="command-timeline-close-1",
            )

            self.assertEqual(closed["commandEvent"]["status"], "completed")
            self.assertIn("cancelled", closed["commandEvent"]["summary"])
            events = store.public_events("thread:anonymous")
            self.assertEqual([event["name"] for event in events], ["/model"])
            self.assertEqual(events[0]["anchorKey"], "q-anonymous-2")


if __name__ == "__main__":
    unittest.main()
