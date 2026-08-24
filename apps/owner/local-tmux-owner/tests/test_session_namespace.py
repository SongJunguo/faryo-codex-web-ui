from __future__ import annotations

from pathlib import Path
import sys
import unittest


APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import session_namespace


class SessionNamespaceTest(unittest.TestCase):
    def test_next_name_uses_the_union_of_both_backends(self) -> None:
        self.assertEqual(
            session_namespace.next_name(
                ["faryo1", "faryo3", "desktop"],
                ["faryo2", "faryo4", "faryo0"],
            ),
            "faryo5",
        )

    def test_owner_is_explicit_and_unknown_names_are_not_guessed(self) -> None:
        namespace = session_namespace.SessionNamespace(
            terminal_names=lambda: ["faryo1", "desktop"],
            app_server_names=lambda: ["faryo2"],
        )

        self.assertEqual(namespace.owner("faryo1"), session_namespace.TERMINAL_OWNER)
        self.assertEqual(namespace.owner("faryo2"), session_namespace.APP_SERVER_OWNER)
        self.assertIsNone(namespace.owner("faryo3"))
        self.assertIsNone(namespace.owner("desktop"))

    def test_duplicate_backend_ownership_fails_closed(self) -> None:
        namespace = session_namespace.SessionNamespace(
            terminal_names=lambda: ["faryo2"],
            app_server_names=lambda: ["faryo2"],
        )

        with self.assertRaises(session_namespace.SessionNamespaceConflict):
            namespace.owner("faryo2")
        self.assertEqual(namespace.collisions(), {"faryo2"})

    def test_backend_reservations_are_sorted_and_filtered(self) -> None:
        namespace = session_namespace.SessionNamespace(
            terminal_names=lambda: ["faryo3", "faryo1", "shell"],
            app_server_names=lambda: ["faryo4", "faryo2", "invalid"],
        )

        self.assertEqual(namespace.reserved_for_app_server(), ["faryo1", "faryo3"])
        self.assertEqual(namespace.reserved_for_terminal(), ["faryo2", "faryo4"])


if __name__ == "__main__":
    unittest.main()
