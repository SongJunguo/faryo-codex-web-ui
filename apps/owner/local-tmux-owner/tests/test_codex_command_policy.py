import json
import sys
import tempfile
import unittest
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import codex_command_policy as policy


class CodexCommandPolicyTest(unittest.TestCase):
    def test_fallback_is_a_versioned_snapshot_not_a_protocol_constant(self):
        catalog = policy.load_catalog(runtime_path=None)
        self.assertEqual("0.149.1", catalog.tested_codex_version)
        self.assertEqual("fallback", catalog.source)
        self.assertFalse(catalog.drifted)
        self.assertGreater(len(catalog.entries), 40)
        self.assertEqual("menu", policy.command_behavior("/model", catalog))
        self.assertEqual("/rename Anonymous title", policy.command_invocation("/rename Anonymous title", catalog))
        self.assertEqual("argument", policy.command_behavior("/rename Anonymous title", catalog))
        self.assertTrue(policy.command_entry("/goal clear", catalog).available_during_task)
        self.assertIsNone(policy.command_invocation("/unknown text", catalog))
        self.assertIsNone(policy.command_invocation("/rename bad\n/status", catalog))

    def test_runtime_inventory_adds_unknown_commands_conservatively(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "catalog.json"
            runtime.write_text(
                json.dumps({"schemaVersion": 1, "observedCodexVersion": "0.149.1", "commands": ["/model", "/future-command"]}),
                encoding="utf-8",
            )
            catalog = policy.load_catalog(runtime_path=runtime)
        self.assertEqual("runtime", catalog.source)
        self.assertTrue(catalog.drifted)
        self.assertEqual(("/future-command",), catalog.added)
        self.assertIn("/usage", catalog.removed)
        self.assertEqual("unclassified", policy.command_behavior("/future-command", catalog))
        self.assertEqual("menu", policy.command_behavior("/model", catalog))
        self.assertTrue(policy.command_entry("/model", catalog).available_during_task)
        self.assertFalse(policy.command_entry("/future-command", catalog).available_during_task)

    def test_version_drift_never_inherits_busy_task_capability(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "catalog.json"
            runtime.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "observedCodexVersion": "0.next",
                        "commands": [
                            {"command": "/goal", "availableDuringTask": True},
                            "/future-command",
                        ],
                    }
                ),
                encoding="utf-8",
            )
            catalog = policy.load_catalog(runtime_path=runtime)
        self.assertTrue(catalog.drifted)
        self.assertFalse(policy.command_entry("/goal clear", catalog).available_during_task)
        self.assertFalse(policy.command_entry("/future-command", catalog).available_during_task)

    def test_corrupt_or_symlink_runtime_catalog_falls_back(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.json"
            source.write_text("{}", encoding="utf-8")
            link = root / "catalog.json"
            link.symlink_to(source)
            catalog = policy.load_catalog(runtime_path=link)
        self.assertEqual("fallback", catalog.source)
        self.assertIsNone(policy.exact_command("/not-in-catalog", catalog))

    def test_runtime_catalog_can_reload_without_restarting_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "catalog.json"
            runtime.write_text(
                json.dumps({"schemaVersion": 1, "observedCodexVersion": "0.next", "commands": ["/future-command"]}),
                encoding="utf-8",
            )
            previous = policy.default_catalog()
            try:
                loaded = policy.reload_default_catalog(runtime_path=runtime)
                self.assertEqual("runtime", loaded.source)
                self.assertEqual("/future-command", policy.exact_command("/future-command"))
            finally:
                with policy._CATALOG_LOCK:
                    policy._DEFAULT_CATALOG = previous


if __name__ == "__main__":
    unittest.main()
