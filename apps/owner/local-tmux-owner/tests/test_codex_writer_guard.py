from __future__ import annotations

import fcntl
import os
from pathlib import Path
import sys
import tempfile
import unittest


APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import codex_writer_guard


class CodexWriterGuardTest(unittest.TestCase):
    def test_missing_lock_is_available_without_creating_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = codex_writer_guard.writer_lock_path("thread-demo", root=root)

            probe = codex_writer_guard.probe_thread_writer("thread-demo", root=root)

            self.assertTrue(probe.available)
            self.assertFalse(path.exists())

    def test_live_flock_is_held_and_unlocked_file_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = codex_writer_guard.writer_lock_path("thread-demo", root=root)
            path.parent.mkdir(parents=True)
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertTrue(
                    codex_writer_guard.probe_thread_writer("thread-demo", root=root).held
                )
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                self.assertTrue(
                    codex_writer_guard.probe_thread_writer("thread-demo", root=root).available
                )
            finally:
                os.close(descriptor)

    def test_invalid_identifier_fails_to_unknown_without_path_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            probe = codex_writer_guard.probe_thread_writer("../private", root=Path(temp))

        self.assertEqual(probe.state, "unknown")


if __name__ == "__main__":
    unittest.main()
