from __future__ import annotations

import unittest

from faryo_cli import error_contract


class ErrorContractTest(unittest.TestCase):
    def test_thread_conflict_has_specific_recovery_without_private_values(self) -> None:
        payload = error_contract.error_payload(
            "thread 01a00000-0000-0000-0000-000000000000 is owned at /home/user/private/work",
            status=409,
        )

        self.assertEqual(payload["errorCode"], "thread_in_use")
        self.assertEqual(payload["errorTitle"], "Conversation still open")
        self.assertIn("another Codex client", payload["error"])
        self.assertIn("Close", payload["recovery"])
        self.assertNotIn("01a00000", repr(payload))
        self.assertNotIn("/home/user", repr(payload))

    def test_unknown_server_error_never_exposes_exception_or_secret(self) -> None:
        payload = error_contract.error_payload(
            "failed at /home/user/private with token=private-value",
            status=500,
        )

        self.assertEqual(payload["errorCode"], "internal_error")
        self.assertEqual(payload["error"], "Faryo encountered an internal error.")
        self.assertNotIn("private", repr(payload))

    def test_existing_error_string_remains_compatible_and_gains_metadata(self) -> None:
        payload = error_contract.normalize_error_payload(
            {"ok": False, "error": "working directory selection expired"},
            409,
        )

        self.assertEqual(payload["error"], "working directory selection expired")
        self.assertEqual(payload["errorCode"], "selection_expired")
        self.assertFalse(payload["retryable"])

    def test_forwarding_keeps_only_reviewed_error_fields(self) -> None:
        payload = error_contract.forward_error_payload(
            {
                "ok": False,
                "error": "Codex App Server is reconnecting",
                "retryable": True,
                "privateDetail": "/home/user/private",
            },
            status=503,
            fallback="Owner failed",
        )

        self.assertEqual(payload["errorCode"], "appserver_reconnecting")
        self.assertTrue(payload["retryable"])
        self.assertNotIn("privateDetail", payload)

    def test_http_status_matrix_has_stable_codes_and_retry_semantics(self) -> None:
        cases = (
            (401, "unauthorized", "auth_required", False),
            (403, "csrf required", "csrf_required", False),
            (404, "session not found", "not_found", False),
            (413, "request too large", "request_too_large", False),
            (429, "rate limited", "rate_limited", True),
            (502, "private upstream exception", "upstream_unavailable", True),
            (504, "request timed out", "timeout", True),
            (500, "private stack detail", "internal_error", True),
        )
        for status, message, code, retryable in cases:
            with self.subTest(status=status):
                payload = error_contract.error_payload(message, status=status)
                self.assertEqual(payload["errorContractVersion"], 1)
                self.assertEqual(payload["errorCode"], code)
                self.assertEqual(payload["retryable"], retryable)
                self.assertTrue(payload["errorTitle"])
                self.assertNotIn("stack detail", payload["error"])


if __name__ == "__main__":
    unittest.main()
