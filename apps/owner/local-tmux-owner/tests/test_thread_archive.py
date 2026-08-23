#!/usr/bin/env python3
"""Codex App Server archive/unarchive contract tests."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import server


class ThreadArchiveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = server.Config("owner", "anonymous-token", 0)
        self.base = {
            "id": "thread-a",
            "cwd": "/workspace/project",
            "archived": 0,
            "source": "cli",
            "thread_source": "user",
        }

    def test_archive_uses_app_server_and_verifies_metadata(self) -> None:
        with (
            mock.patch.object(server, "codex_thread_record", side_effect=[self.base, {**self.base, "archived": 1}]),
            mock.patch.object(server, "active_codex_thread_state", return_value=({}, set())),
            mock.patch.object(server, "codex_app_server_rpc", return_value={"ok": True, "result": {}}) as rpc,
        ):
            result = server.change_codex_thread_archive_state(self.config, "thread-a", True, "/workspace")

        rpc.assert_called_once_with("thread/archive", {"threadId": "thread-a"}, timeout=5.0)
        self.assertEqual(result, {"agentSessionId": "thread-a", "archived": True, "duplicate": False})

    def test_unarchive_uses_formal_rpc(self) -> None:
        archived = {**self.base, "archived": 1}
        with (
            mock.patch.object(server, "codex_thread_record", side_effect=[archived, self.base]),
            mock.patch.object(server, "active_codex_thread_state", return_value=({}, set())),
            mock.patch.object(server, "codex_app_server_rpc", return_value={"ok": True, "result": {"thread": self.base}}) as rpc,
        ):
            result = server.change_codex_thread_archive_state(self.config, "thread-a", False, "/workspace")

        rpc.assert_called_once_with("thread/unarchive", {"threadId": "thread-a"}, timeout=5.0)
        self.assertFalse(result["archived"])
        self.assertFalse(result["duplicate"])

    def test_archive_can_use_the_owner_persistent_runtime_rpc(self) -> None:
        lifecycle = mock.Mock(return_value={"ok": True, "result": {}})
        with (
            mock.patch.object(server, "codex_thread_record", side_effect=[self.base, {**self.base, "archived": 1}]),
            mock.patch.object(server, "active_codex_thread_state", return_value=({}, set())),
            mock.patch.object(server, "codex_app_server_rpc") as legacy_rpc,
        ):
            result = server.change_codex_thread_archive_state(
                self.config,
                "thread-a",
                True,
                "/workspace",
                lifecycle,
            )

        lifecycle.assert_called_once_with("thread/archive", "thread-a", 5.0)
        legacy_rpc.assert_not_called()
        self.assertTrue(result["archived"])

    def test_target_state_is_idempotent_without_rpc(self) -> None:
        with (
            mock.patch.object(server, "codex_thread_record", return_value={**self.base, "archived": 1}),
            mock.patch.object(server, "codex_app_server_rpc") as rpc,
        ):
            result = server.change_codex_thread_archive_state(self.config, "thread-a", True, "/workspace")

        self.assertTrue(result["duplicate"])
        rpc.assert_not_called()

    def test_active_thread_is_rejected_before_rpc(self) -> None:
        with (
            mock.patch.object(server, "codex_thread_record", return_value=self.base),
            mock.patch.object(server, "active_codex_thread_state", return_value=({"thread-a": "faryo1"}, set())),
            mock.patch.object(server, "codex_app_server_rpc") as rpc,
        ):
            with self.assertRaises(server.OwnerError) as raised:
                server.change_codex_thread_archive_state(self.config, "thread-a", True, "/workspace")

        self.assertEqual(raised.exception.status, server.HTTPStatus.CONFLICT)
        rpc.assert_not_called()

    def test_workspace_scope_hides_thread_before_rpc(self) -> None:
        with (
            mock.patch.object(server, "codex_thread_record", return_value=self.base),
            mock.patch.object(server, "path_under_root", return_value=False),
            mock.patch.object(server, "codex_app_server_rpc") as rpc,
        ):
            with self.assertRaises(server.OwnerError) as raised:
                server.change_codex_thread_archive_state(self.config, "thread-a", True, "/other")

        self.assertEqual(raised.exception.status, server.HTTPStatus.NOT_FOUND)
        rpc.assert_not_called()

    def test_app_server_error_is_sanitized_and_mapped(self) -> None:
        with (
            mock.patch.object(server, "codex_thread_record", return_value=self.base),
            mock.patch.object(server, "active_codex_thread_state", return_value=({}, set())),
            mock.patch.object(server, "codex_app_server_rpc", return_value={
                "ok": False,
                "code": -32600,
                "error": "thread is owned by another process at /private/path",
            }),
        ):
            with self.assertRaises(server.OwnerError) as raised:
                server.change_codex_thread_archive_state(self.config, "thread-a", True, "/workspace")

        self.assertEqual(raised.exception.status, server.HTTPStatus.CONFLICT)
        self.assertEqual(str(raised.exception), "Codex thread lifecycle request failed")
        self.assertNotIn("/private", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
