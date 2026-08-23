#!/usr/bin/env python3
"""Enabled-route and private runtime configuration regression tests."""

from __future__ import annotations

import importlib.util
import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import unquote


REPO_ROOT = Path(__file__).resolve().parents[4]
SERVER_PATH = REPO_ROOT / "apps" / "gateway" / "server" / "server.py"
WORKBENCH_JS = (SERVER_PATH.parent / "static" / "workbench.js").read_text(encoding="utf-8")
SESSION_MODEL = (SERVER_PATH.parents[1] / "ui" / "session-model.mjs").read_text(encoding="utf-8")
PREACT_WORKBENCH = (SERVER_PATH.parents[1] / "ui" / "preact-workbench.jsx").read_text(
    encoding="utf-8"
)

spec = importlib.util.spec_from_file_location("faryo_gateway_route_server", SERVER_PATH)
gateway = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(gateway)

import workbench_service


class GatewayRouteConfigTest(unittest.TestCase):
    def test_gateway_asset_revision_changes_with_asset_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            asset = Path(temp) / "workbench.js"
            asset.write_text("first", encoding="utf-8")
            first = gateway.gateway_asset_revision([asset])
            asset.write_text("second", encoding="utf-8")
            second = gateway.gateway_asset_revision([asset])

        self.assertRegex(first, r"^[0-9a-f]{12}$")
        self.assertNotEqual(first, second)

    def setUp(self) -> None:
        self.original_backends = dict(gateway.BACKENDS)

    def tearDown(self) -> None:
        gateway.BACKENDS.clear()
        gateway.BACKENDS.update(self.original_backends)

    def test_load_backends_includes_enabled_route_only(self) -> None:
        backends = gateway.load_backends({
            "FARYO_GATEWAY_ROUTES": "txy",
            "FARYO_TXY_OWNER_PORT": "9876",
        })

        self.assertEqual(list(backends), ["txy"])
        self.assertEqual(backends["txy"][1], 9876)

    def test_unknown_route_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported FARYO_GATEWAY_ROUTES"):
            gateway.load_backends({"FARYO_GATEWAY_ROUTES": "txy,unknown"})

    def test_gateway_session_lifetime_is_configurable_and_bounded(self) -> None:
        self.assertEqual(gateway.gateway_session_max_age({}), 720 * 60 * 60)
        self.assertEqual(
            gateway.gateway_session_max_age({"FARYO_GATEWAY_SESSION_HOURS": "24"}),
            24 * 60 * 60,
        )
        for value in ("0", "721", "invalid"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "integer from 1 to 720"):
                    gateway.gateway_session_max_age({"FARYO_GATEWAY_SESSION_HOURS": value})

    def test_unicode_owner_label_is_http_header_safe(self) -> None:
        encoded = gateway.owner_label_header_value("Ubuntu 工作站")

        self.assertEqual(unquote(encoded), "Ubuntu 工作站")
        self.assertTrue(encoded.isascii())
        self.assertNotIn(" ", encoded)

    def test_each_route_fetches_enough_history_for_the_requested_page(self) -> None:
        gateway.BACKENDS.clear()
        gateway.BACKENDS["txy"] = ("127.0.0.1", 8765, "TXY")
        owner_request = mock.Mock(return_value={
            "ok": True,
            "activeCount": 0,
            "activeSessions": [],
            "sessions": [],
            "historyTotal": 0,
        })
        max_running = mock.Mock(return_value=8)
        service = workbench_service.WorkbenchService(
            gateway.WorkbenchRuntime,
            mock.Mock(),
            mock.Mock(),
            owner_json_request_callback=owner_request,
            max_running_callback=max_running,
        )

        service.owner_sessions("txy", "tester", 1)
        self.assertEqual(
            owner_request.call_args.args[1],
            "/api/agent-sessions?view=split&limit=10&offset=0",
        )

        service.owner_sessions("txy", "tester", 2)
        self.assertEqual(
            owner_request.call_args.args[1],
            "/api/agent-sessions?view=split&limit=20&offset=0",
        )

        service.owner_sessions("txy", "tester", 40, True)
        self.assertEqual(
            owner_request.call_args.args[1],
            "/api/agent-sessions?view=split&limit=10&offset=390",
        )

    def test_workbench_projects_body_free_app_server_launcher_health(self) -> None:
        gateway.BACKENDS.clear()
        gateway.BACKENDS["txy"] = ("127.0.0.1", 8765, "Workstation")
        owner_request = mock.Mock(return_value={
            "ok": True,
            "activeCount": 0,
            "activeSessions": [],
            "sessions": [],
            "historyTotal": 0,
            "appServerRuntime": {
                "state": "reconnecting",
                "ready": False,
                "lastError": "ConnectionRefusedError",
            },
        })
        config = mock.Mock()
        config.user_routes.return_value = ["txy"]
        config.max_running.return_value = 8
        config.list_bridge_packages.return_value = []
        service = workbench_service.WorkbenchService(
            gateway.WorkbenchRuntime,
            config,
            mock.Mock(),
            owner_json_request_callback=owner_request,
            backend_status_callback=lambda route: {"id": route, "state": "online"},
        )

        result = service.payload("tester")

        self.assertFalse(result["entries"][0]["appServerReady"])
        self.assertEqual(result["entries"][0]["appServerState"], "reconnecting")
        self.assertNotIn("lastError", result["entries"][0])

    def test_workbench_keeps_active_sessions_above_ten_item_history_pages(self) -> None:
        gateway.BACKENDS.clear()
        gateway.BACKENDS.update({
            "txy": ("127.0.0.1", 8765, "TXY"),
            "hp": ("127.0.0.1", 8766, "HP"),
        })
        histories = {
            "txy": [{"id": f"txy-{index}", "updatedTs": 40 - index} for index in range(20)],
            "hp": [{"id": f"hp-{index}", "updatedTs": 39.5 - index} for index in range(20)],
        }

        def route_payload(route: str, _username: str, page: int, _exact_page: bool = False, _history_filters=None) -> dict:
            return {
                "activeSessions": [{"id": f"active-{route}", "tmuxSession": f"live-{route}", "updatedTs": 100}],
                "sessions": histories[route][:page * gateway.HISTORY_PAGE_SIZE],
                "historyTotal": len(histories[route]),
                "activeCount": 1,
                "maxRunning": 8,
                "canCreate": True,
            }

        owner_sessions = mock.Mock(side_effect=route_payload)
        config = mock.Mock()
        config.user_routes.return_value = ["txy", "hp"]
        config.list_bridge_packages.return_value = []
        config.mcp_user = "mcp-user"
        service = workbench_service.WorkbenchService(
            gateway.WorkbenchRuntime,
            config,
            mock.Mock(),
            owner_sessions_callback=owner_sessions,
            backend_status_callback=lambda route: {"id": route},
        )

        first = service.payload("tester", 1)
        second = service.payload("tester", 2)

        self.assertEqual(len(first["activeSessions"]), 2)
        self.assertEqual(len(first["sessions"]), 10)
        self.assertEqual(first["history"], {
            "page": 1,
            "pageSize": 10,
            "total": 40,
            "totalPages": 4,
            "hasPrevious": False,
            "hasNext": True,
            "filter": {"q": "", "period": "all", "archive": "active"},
        })
        self.assertEqual(len(second["activeSessions"]), 2)
        self.assertEqual(len(second["sessions"]), 10)
        self.assertEqual(second["history"]["page"], 2)
        self.assertTrue(set(item["id"] for item in first["sessions"]).isdisjoint(
            item["id"] for item in second["sessions"]
        ))

    def test_single_route_workbench_requests_only_the_exact_history_page(self) -> None:
        gateway.BACKENDS.clear()
        gateway.BACKENDS["txy"] = ("127.0.0.1", 8765, "Ubuntu 工作站")
        page_rows = [{"id": f"history-{index}", "updatedTs": 100 - index} for index in range(390, 400)]
        owner_sessions = mock.Mock(return_value={
            "activeSessions": [{"id": "active", "tmuxSession": "codex", "updatedTs": 200}],
            "sessions": page_rows,
            "historyTotal": 437,
            "activeCount": 1,
            "maxRunning": 8,
            "canCreate": True,
        })
        config = mock.Mock()
        config.user_routes.return_value = ["txy"]
        config.list_bridge_packages.return_value = []
        config.mcp_user = "controller"
        service = workbench_service.WorkbenchService(
            gateway.WorkbenchRuntime,
            config,
            mock.Mock(),
            owner_sessions_callback=owner_sessions,
            backend_status_callback=lambda _route: {"id": "txy"},
        )

        result = service.payload("tester", 40)

        owner_sessions.assert_called_once_with(
            "txy",
            "tester",
            40,
            True,
            {"q": "", "period": "all", "archive": "active"},
        )
        self.assertEqual(result["sessions"], page_rows)
        self.assertEqual(result["history"]["page"], 40)
        self.assertEqual(result["history"]["totalPages"], 44)

    def test_portal_has_separate_active_and_paginated_history_regions(self) -> None:
        gateway.BACKENDS.clear()
        gateway.BACKENDS["txy"] = ("127.0.0.1", 8765, "TXY")

        page = gateway.portal_html("tester", ["txy"])

        self.assertIn('id="activeSessionList"', page)
        self.assertIn('id="sessionList"', page)
        self.assertIn('id="historyPrev"', page)
        self.assertIn('id="historyJump"', page)
        self.assertIn('id="historyPageInput"', page)
        self.assertIn('id="historyPageTotal"', page)
        self.assertIn('id="historyNext"', page)
        self.assertIn('/api/workbench?${historyRequestQuery()}', WORKBENCH_JS)
        self.assertIn('id="historySearchInput"', page)
        self.assertIn('data-history-period="7d"', page)
        self.assertIn('data-history-archive="archived"', page)
        self.assertIn("/api/session-history/", WORKBENCH_JS)
        self.assertIn('archived ? "archive" : "unarchive"', WORKBENCH_JS)
        self.assertIn('class="mini-btn archive-session"', PREACT_WORKBENCH)
        self.assertIn('class="mini-btn restore-session"', PREACT_WORKBENCH)
        self.assertNotIn('/api/session-history/delete', WORKBENCH_JS)
        self.assertIn("view.active && item.managed", PREACT_WORKBENCH)
        self.assertIn("containers.launchers", PREACT_WORKBENCH)
        self.assertIn("item.disabled", PREACT_WORKBENCH)
        self.assertIn("entry.canCreate !== false", WORKBENCH_JS)
        self.assertIn('id="workstationPicker"', page)
        self.assertIn("bindWorkstationPicker", WORKBENCH_JS)
        self.assertNotIn("selectNewRoute", WORKBENCH_JS)
        self.assertIn('id="launchSettings"', page)
        self.assertIn('id="sessionBackendSummary"', page)
        self.assertIn('id="contextWindowSummary"', page)
        self.assertIn('launchSettings.open = !matchMedia("(max-width: 620px)").matches', WORKBENCH_JS)
        self.assertIn('id="sessionBackendPicker"', page)
        self.assertIn("Codex App Server", page)
        self.assertIn("Codex TUI (tmux)", page)
        self.assertIn("routeEntry?.appServerReady !== false", WORKBENCH_JS)

    def test_history_filters_are_bounded_encoded_and_forwarded(self) -> None:
        filters = gateway.history_filters_from_query({
            "q": ["  renamed 100%_项目  "],
            "period": ["7d"],
            "archive": ["all"],
        })
        self.assertEqual(filters, {
            "q": "renamed 100%_项目",
            "period": "7d",
            "archive": "all",
        })
        path = gateway.owner_history_query(10, 20, filters)
        self.assertEqual(
            path,
            "/api/agent-sessions?view=split&limit=10&offset=20&q=renamed+100%25_%E9%A1%B9%E7%9B%AE&period=7d&archive=all",
        )
        self.assertEqual(len(gateway.normalize_history_filters({"q": "x" * 200})["q"]), 96)

    def test_gateway_preserves_explicit_session_lifecycle_state(self) -> None:
        gateway.BACKENDS.clear()
        gateway.BACKENDS["txy"] = ("127.0.0.1", 8765, "Workstation")
        service = workbench_service.WorkbenchService(
            gateway.WorkbenchRuntime,
            mock.Mock(),
            mock.Mock(),
        )

        exited = service.session_item({
            "id": "thread-a",
            "tmuxSession": "faryo1",
            "state": "exited",
            "managed": True,
        }, "txy", {}, False)
        resumable = service.session_item({
            "id": "thread-b",
            "state": "resumable",
        }, "txy", {}, False)

        self.assertEqual(exited["state"], "exited")
        self.assertFalse(exited["agentRunning"])
        self.assertEqual(resumable["state"], "resumable")
        for label in ("Starting", "Running", "Waiting", "Exited", "Desktop"):
            self.assertIn(f'{label.lower()}: "{label}"', SESSION_MODEL)

    def test_portal_json_bootstrap_escapes_script_breakout(self) -> None:
        gateway.BACKENDS.clear()
        gateway.BACKENDS["txy"] = ("127.0.0.1", 8765, "</script><img src=x>")

        page = gateway.portal_html("tester", ["txy"])

        bootstrap = page.split('id="faryoRouteLabels"', 1)[1].split("</script>", 1)[0]
        self.assertNotIn("<img", bootstrap)
        self.assertIn("\\u003c/script\\u003e", bootstrap)

    def test_gateway_config_requires_token_for_enabled_route_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            auth = root / "gateway-auth.json"
            env = root / "faryo.env"
            auth.write_text(json.dumps({
                "users": {
                    "tester": {
                        "bcrypt_hash": "not-checked-during-load",
                        "routes": ["txy"],
                        "default_route": "txy",
                    }
                }
            }), encoding="utf-8")
            env.write_text(
                "FARYO_GATEWAY_ROUTES=txy\n"
                "FARYO_TXY_OWNER_TOKEN=enabled-token\n",
                encoding="utf-8",
            )

            config = gateway.GatewayConfig(
                auth,
                env,
                root / "portal",
                root / "state" / "cookie-secret",
            )

            self.assertEqual(config.owner_tokens, {"txy": "enabled-token"})
            self.assertEqual(list(gateway.BACKENDS), ["txy"])
            self.assertEqual(config.max_running("txy"), 8)

    def test_file_package_persists_and_accepts_more_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            auth = root / "gateway-auth.json"
            env = root / "faryo.env"
            auth.write_text(json.dumps({
                "users": {
                    "tester": {
                        "bcrypt_hash": "not-checked-during-load",
                        "routes": ["txy"],
                    }
                }
            }), encoding="utf-8")
            env.write_text(
                "FARYO_GATEWAY_ROUTES=txy\n"
                "FARYO_TXY_OWNER_TOKEN=enabled-token\n",
                encoding="utf-8",
            )
            config = gateway.GatewayConfig(auth, env, root / "portal", root / "state" / "cookie-secret")

            first = base64.b64encode(b"first file\n").decode("ascii")
            package = config.save_bridge_package({
                "title": "two files",
                "attachments": [{
                    "file_name": "first.md",
                    "mime_type": "text/markdown",
                    "data_url": f"data:text/markdown;base64,{first}",
                }],
            }, "tester")
            second = base64.b64encode(b"second file\n").decode("ascii")
            package = config.append_bridge_package_assets(package["id"], [{
                "file_name": "second.txt",
                "mime_type": "text/plain",
                "data_url": f"data:text/plain;base64,{second}",
            }], "tester")

            self.assertEqual(package["status"], "pending")
            self.assertEqual([asset["file_name"] for asset in package["assets"]], ["first.md", "second.txt"])
            self.assertEqual(Path(package["assets"][0]["path"]).read_bytes(), b"first file\n")
            self.assertEqual(Path(package["assets"][1]["path"]).read_bytes(), b"second file\n")
            self.assertEqual(config.list_bridge_packages("tester", "pending")[0]["id"], package["id"])

    def test_bridge_package_cleanup_uses_status_retention_and_skips_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            auth = root / "gateway-auth.json"
            env = root / "faryo.env"
            auth.write_text(json.dumps({"users": {"tester": {"bcrypt_hash": "unused", "routes": ["txy"]}}}), encoding="utf-8")
            env.write_text("FARYO_GATEWAY_ROUTES=txy\nFARYO_TXY_OWNER_TOKEN=enabled-token\n", encoding="utf-8")
            config = gateway.GatewayConfig(auth, env, root / "portal", root / "state" / "cookie-secret")
            now = 2_000_000_000

            def package(package_id: str, status: str, age: int) -> Path:
                package_dir = config.bridge_root / package_id
                package_dir.mkdir()
                (package_dir / "package.json").write_text(json.dumps({
                    "id": package_id,
                    "owner": "tester",
                    "status": status,
                    "updated_at": now - age,
                }), encoding="utf-8")
                return package_dir

            old_pending = package("1000000000-aaaaaaaa", "pending", gateway.BRIDGE_PENDING_RETENTION_SECONDS + 1)
            old_delivered = package("1000000001-bbbbbbbb", "injected", gateway.BRIDGE_DELIVERED_RETENTION_SECONDS + 1)
            recent = package("1000000002-cccccccc", "pending", gateway.BRIDGE_PENDING_RETENTION_SECONDS - 1)
            outside = root / "outside-package"
            outside.mkdir()
            (config.bridge_root / "1000000003-dddddddd").symlink_to(outside, target_is_directory=True)

            removed = config.cleanup_bridge_packages(now, force=True)

            self.assertEqual(removed, 2)
            self.assertFalse(old_pending.exists())
            self.assertFalse(old_delivered.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(outside.exists())

    def test_gateway_config_accepts_route_max_running_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            auth = root / "gateway-auth.json"
            env = root / "faryo.env"
            auth.write_text(json.dumps({
                "users": {
                    "tester": {
                        "bcrypt_hash": "not-checked-during-load",
                        "routes": ["txy"],
                    }
                }
            }), encoding="utf-8")
            env.write_text(
                "FARYO_GATEWAY_ROUTES=txy\n"
                "FARYO_TXY_OWNER_TOKEN=enabled-token\n"
                "FARYO_TXY_MAX_RUNNING=12\n",
                encoding="utf-8",
            )

            config = gateway.GatewayConfig(
                auth,
                env,
                root / "portal",
                root / "state" / "cookie-secret",
            )

            self.assertEqual(config.max_running("txy"), 12)

    def test_gateway_config_rejects_invalid_route_max_running(self) -> None:
        for invalid in ("0", "33", "many"):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                auth = root / "gateway-auth.json"
                env = root / "faryo.env"
                auth.write_text(json.dumps({
                    "users": {
                        "tester": {
                            "bcrypt_hash": "not-checked-during-load",
                            "routes": ["txy"],
                        }
                    }
                }), encoding="utf-8")
                env.write_text(
                    "FARYO_GATEWAY_ROUTES=txy\n"
                    "FARYO_TXY_OWNER_TOKEN=enabled-token\n"
                    f"FARYO_TXY_MAX_RUNNING={invalid}\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(ValueError, "FARYO_TXY_MAX_RUNNING"):
                    gateway.GatewayConfig(
                        auth,
                        env,
                        root / "portal",
                        root / "state" / "cookie-secret",
                    )

    def test_gateway_config_reads_shell_quoted_private_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            auth = root / "gateway-auth.json"
            env = root / "faryo.env"
            auth.write_text(json.dumps({
                "users": {
                    "tester": {
                        "bcrypt_hash": "not-checked-during-load",
                        "routes": ["txy"],
                    }
                }
            }), encoding="utf-8")
            env.write_text(
                "FARYO_GATEWAY_ROUTES='txy'\n"
                "FARYO_TXY_OWNER_TOKEN='quoted test token'\n",
                encoding="utf-8",
            )

            config = gateway.GatewayConfig(
                auth,
                env,
                root / "portal",
                root / "state" / "cookie-secret",
            )

            self.assertEqual(config.owner_tokens, {"txy": "quoted test token"})

    def test_enabled_route_still_requires_its_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            auth = root / "gateway-auth.json"
            env = root / "faryo.env"
            auth.write_text(json.dumps({
                "users": {
                    "tester": {
                        "bcrypt_hash": "not-checked-during-load",
                        "routes": ["txy"],
                    }
                }
            }), encoding="utf-8")
            env.write_text("FARYO_GATEWAY_ROUTES=txy\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "FARYO_TXY_OWNER_TOKEN"):
                gateway.GatewayConfig(
                    auth,
                    env,
                    root / "portal",
                    root / "state" / "cookie-secret",
                )

    def test_auth_generator_defaults_to_private_runtime_paths(self) -> None:
        module_path = REPO_ROOT / "apps" / "gateway" / "scripts" / "generate-gateway-auth-config.py"
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ,
            {"FARYO_HOME": temp},
            clear=False,
        ):
            generator_spec = importlib.util.spec_from_file_location("faryo_gateway_auth_generator", module_path)
            generator = importlib.util.module_from_spec(generator_spec)
            assert generator_spec and generator_spec.loader
            generator_spec.loader.exec_module(generator)

            self.assertEqual(generator.ENV_FILE, Path(temp) / "gateway" / "config" / "faryo.env")
            self.assertEqual(generator.AUTH_FILE, Path(temp) / "gateway" / "config" / "gateway-auth.json")

    def test_shell_loader_does_not_require_disabled_route_tokens(self) -> None:
        library = REPO_ROOT / "apps" / "gateway" / "scripts" / "_lib.sh"
        with tempfile.TemporaryDirectory() as temp:
            env_file = Path(temp) / "faryo.env"
            env_file.write_text(
                "FARYO_GATEWAY_ROUTES=txy\n"
                "FARYO_TXY_OWNER_TOKEN=enabled-token\n",
                encoding="utf-8",
            )
            process_env = {
                "HOME": os.environ.get("HOME", temp),
                "PATH": os.environ.get("PATH", ""),
                "FARYO_GATEWAY_ENV": str(env_file),
            }

            result = subprocess.run(
                ["bash", "-c", 'source "$1"; load_env', "bash", str(library)],
                env=process_env,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)

    def test_gateway_runner_exports_private_session_lifetime(self) -> None:
        script = REPO_ROOT / "apps" / "gateway" / "scripts" / "run-gateway.sh"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env_file = root / "faryo.env"
            python_stub = root / "python-stub"
            python_stub.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"${FARYO_GATEWAY_SESSION_HOURS:-missing}\"\n",
                encoding="utf-8",
            )
            python_stub.chmod(0o700)
            env_file.write_text(
                f"FARYO_PYTHON={python_stub}\n"
                "FARYO_GATEWAY_SESSION_HOURS=24\n",
                encoding="utf-8",
            )
            process_env = {
                "HOME": str(root),
                "PATH": os.environ.get("PATH", ""),
                "FARYO_GATEWAY_ENV": str(env_file),
            }

            result = subprocess.run(
                ["bash", str(script)],
                env=process_env,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "24")

    def test_auth_generator_reads_mode_separated_password_file(self) -> None:
        module_path = REPO_ROOT / "apps" / "gateway" / "scripts" / "generate-gateway-auth-config.py"
        generator_spec = importlib.util.spec_from_file_location("faryo_gateway_password_generator", module_path)
        generator = importlib.util.module_from_spec(generator_spec)
        assert generator_spec and generator_spec.loader
        generator_spec.loader.exec_module(generator)
        with tempfile.TemporaryDirectory() as temp:
            password_file = Path(temp) / "password"
            password_file.write_text("generic-test-password\n", encoding="utf-8")

            password = generator.gateway_password({"FARYO_GATEWAY_PASSWORD_FILE": str(password_file)})

            self.assertEqual(password, "generic-test-password")

    def test_local_initializer_preserves_existing_login_config(self) -> None:
        script = REPO_ROOT / "apps" / "gateway" / "scripts" / "init-local-gateway.sh"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            faryo_home = root / ".faryo"
            owner_env = faryo_home / "owner" / "config" / "faryo.env"
            gateway_env = faryo_home / "gateway" / "config" / "faryo.env"
            auth = faryo_home / "gateway" / "config" / "gateway-auth.json"
            owner_env.parent.mkdir(parents=True)
            auth.parent.mkdir(parents=True)
            owner_env.write_text(
                "FARYO_OWNER_TOKEN=generic-owner-token\n"
                "FARYO_OWNER_HOST=127.0.0.1\n"
                "FARYO_OWNER_PORT=8765\n",
                encoding="utf-8",
            )
            original_auth = '{"users":{"tester":{"bcrypt_hash":"preserve-me","routes":["txy"]}}}\n'
            auth.write_text(original_auth, encoding="utf-8")
            gateway_env.write_text(
                "FARYO_DEFAULT_WORKSPACE=/preserved/workspace\n"
                "FARYO_GATEWAY_SESSION_HOURS=720\n"
                "FARYO_TXY_OWNER_LABEL=Workstation\n",
                encoding="utf-8",
            )
            process_env = {
                "HOME": str(root),
                "PATH": os.environ.get("PATH", ""),
                "FARYO_HOME": str(faryo_home),
                "FARYO_OWNER_ENV": str(owner_env),
                "FARYO_GATEWAY_ENV": str(gateway_env),
                "GATEWAY_AUTH_CONFIG": str(auth),
                "FARYO_PYTHON": sys.executable,
            }

            result = subprocess.run(
                ["bash", str(script)],
                env=process_env,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(auth.read_text(encoding="utf-8"), original_auth)
            self.assertFalse((auth.parent / "initial-password").exists())
            self.assertEqual(auth.stat().st_mode & 0o777, 0o600)
            self.assertEqual(gateway_env.stat().st_mode & 0o777, 0o600)
            self.assertIn("FARYO_TXY_MAX_RUNNING=8\n", gateway_env.read_text(encoding="utf-8"))
            self.assertIn("FARYO_GATEWAY_SESSION_HOURS=720\n", gateway_env.read_text(encoding="utf-8"))
            self.assertIn("FARYO_DEFAULT_WORKSPACE=/preserved/workspace\n", gateway_env.read_text(encoding="utf-8"))
            self.assertIn("FARYO_TXY_OWNER_LABEL=Workstation\n", gateway_env.read_text(encoding="utf-8"))

    def test_local_initializer_uses_current_30_day_default(self) -> None:
        script = REPO_ROOT / "apps" / "gateway" / "scripts" / "init-local-gateway.sh"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            faryo_home = root / ".faryo"
            owner_env = faryo_home / "owner" / "config" / "faryo.env"
            gateway_env = faryo_home / "gateway" / "config" / "faryo.env"
            auth = faryo_home / "gateway" / "config" / "gateway-auth.json"
            owner_env.parent.mkdir(parents=True)
            owner_env.write_text(
                "FARYO_OWNER_TOKEN=generic-owner-token\n"
                "FARYO_OWNER_HOST=127.0.0.1\n"
                "FARYO_OWNER_PORT=8765\n",
                encoding="utf-8",
            )
            process_env = {
                "HOME": str(root),
                "PATH": os.environ.get("PATH", ""),
                "FARYO_HOME": str(faryo_home),
                "FARYO_OWNER_ENV": str(owner_env),
                "FARYO_GATEWAY_ENV": str(gateway_env),
                "GATEWAY_AUTH_CONFIG": str(auth),
                "FARYO_PYTHON": sys.executable,
            }

            result = subprocess.run(
                ["bash", str(script)],
                env=process_env,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("FARYO_GATEWAY_SESSION_HOURS=720\n", gateway_env.read_text(encoding="utf-8"))
            self.assertEqual(gateway_env.stat().st_mode & 0o777, 0o600)
            self.assertEqual(auth.stat().st_mode & 0o777, 0o600)
            self.assertEqual((auth.parent / "initial-password").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
