from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from command_timeline import CommandTimelineError, CommandTimelineStore


class CommandTimelineTest(unittest.TestCase):
    def test_command_lifecycle_is_private_atomic_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "private/command-timeline.json"
            store = CommandTimelineStore(path, clock=lambda: 10.0)
            event, duplicate = store.begin(
                owner_key="thread:anonymous",
                request_id="request_rename_1",
                invocation="/rename Anonymous title",
                anchor_key="appserver-turn-safe",
            )
            repeated, repeated_duplicate = store.begin(
                owner_key="thread:anonymous",
                request_id="request_rename_1",
                invocation="/rename Anonymous title",
            )
            completed = store.update(str(event["id"]), status="completed")
            public = store.public_events("thread:anonymous")

            self.assertFalse(duplicate)
            self.assertTrue(repeated_duplicate)
            self.assertEqual(repeated["id"], event["id"])
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(public[0]["summary"], "Renamed conversation to “Anonymous title”")
            self.assertNotIn("ownerKey", public[0])
            self.assertNotIn("requestId", public[0])
            self.assertNotIn("digest", public[0])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_sensitive_goal_body_and_read_only_panels_are_not_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "command-timeline.json"
            store = CommandTimelineStore(path)
            event, _duplicate = store.begin(
                owner_key="thread:anonymous",
                request_id="request_goal_1",
                invocation="/goal update private objective text",
            )
            usage, usage_duplicate = store.begin(
                owner_key="thread:anonymous",
                request_id="request_usage_1",
                invocation="/usage",
            )
            body = path.read_text(encoding="utf-8")

            self.assertIsNotNone(event)
            self.assertIsNone(usage)
            self.assertFalse(usage_duplicate)
            self.assertNotIn("private objective text", body)
            self.assertNotIn("/goal update", body)
            self.assertNotIn("/usage", body)

    def test_request_id_reuse_with_another_command_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = CommandTimelineStore(Path(temp) / "commands.json")
            store.begin(
                owner_key="thread:anonymous",
                request_id="request_shared_1",
                invocation="/fast",
            )
            with self.assertRaises(CommandTimelineError):
                store.begin(
                    owner_key="thread:anonymous",
                    request_id="request_shared_1",
                    invocation="/rename Different",
                )

    def test_restart_marks_unrestorable_local_interaction_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "commands.json"
            store = CommandTimelineStore(path, clock=lambda: 10.0)
            event, _duplicate = store.begin(
                owner_key="thread:anonymous",
                request_id="request_model_1",
                invocation="/model",
            )
            store.update(str(event["id"]), status="waiting", interaction_id="interaction_local")

            restored = CommandTimelineStore(path, clock=lambda: 20.0)
            public = restored.public_events("thread:anonymous")

            self.assertEqual(public[0]["status"], "failed")
            self.assertIn("service restart", public[0]["summary"])
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["events"][0]["interactionId"], "")

    def test_symlink_source_is_rejected_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target.json"
            target.write_text('{"private":"unchanged"}\n', encoding="utf-8")
            path = root / "commands.json"
            path.symlink_to(target)
            store = CommandTimelineStore(path)
            self.assertEqual(store.load_errors, 1)
            self.assertEqual(store.public_events("thread:anonymous"), [])
            self.assertEqual(target.read_text(encoding="utf-8"), '{"private":"unchanged"}\n')


if __name__ == "__main__":
    unittest.main()
