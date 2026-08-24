from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
from io import StringIO
import io
import json
import os
from pathlib import Path
import subprocess
import stat
import sys
import tarfile
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from faryo_cli import appserver_workers, application, cli, codex_runtime, diagnostics, installer, maintenance, migration, operations, runtime, updates


class FaryoCliTest(unittest.TestCase):
    def layout(self, root: Path, *, unsafe: bool = False) -> diagnostics.Layout:
        faryo_home = root / ".faryo"
        owner_env = faryo_home / "owner/config/faryo.env"
        gateway_env = faryo_home / "gateway/config/faryo.env"
        gateway_auth = faryo_home / "gateway/config/gateway-auth.json"
        owner_env.parent.mkdir(parents=True)
        gateway_env.parent.mkdir(parents=True)
        owner_env.write_text(
            "FARYO_OWNER_HOST=127.0.0.1\n"
            "FARYO_OWNER_PORT=8765\n"
            "FARYO_OWNER_TOKEN=private-owner-token\n"
            f"FARYO_PYTHON={sys.executable}\n"
            "FARYO_CODEX_BIN=/private/bin/codex\n",
            encoding="utf-8",
        )
        gateway_env.write_text(
            "GATEWAY_HOST=127.0.0.1\n"
            "GATEWAY_PORT=8780\n"
            "FARYO_GATEWAY_ROUTES=txy\n"
            "FARYO_GATEWAY_SESSION_HOURS=720\n"
            f"FARYO_PYTHON={sys.executable}\n"
            "FARYO_TXY_OWNER_TOKEN=private-owner-token\n",
            encoding="utf-8",
        )
        gateway_auth.write_text('{"users":{"private@example.invalid":{}}}\n', encoding="utf-8")
        for path in (owner_env, gateway_env, gateway_auth):
            path.chmod(0o644 if unsafe else 0o600)
        return diagnostics.Layout(root, faryo_home, owner_env, gateway_env, gateway_auth, ROOT)

    def report(self, layout: diagnostics.Layout) -> dict:
        def version(command, *_args, **_kwargs):
            return {"tmux": "tmux 3.5", "codex": "codex-cli 0.test"}.get(command)

        def state(name):
            return {
                "faryo-owner.service": "inactive",
                "faryo-gateway.service": "active",
                "faryo-owner-keepalive.timer": "active",
            }.get(name, "inactive")

        with (
            mock.patch.object(diagnostics, "command_version", side_effect=version),
            mock.patch.object(diagnostics, "resolve_codex", return_value="/fixture/codex"),
            mock.patch.object(diagnostics, "argv_version", return_value="codex-cli 0.test"),
            mock.patch.object(diagnostics, "systemd_user_available", return_value=True),
            mock.patch.object(diagnostics, "appserver_worker_service_counts", return_value=(0, 0, 0)),
            mock.patch.object(diagnostics, "service_state", side_effect=state),
            mock.patch.object(diagnostics, "http_status", side_effect=lambda _host, port, _path: 200 if port in {8765, 8780} else None),
            mock.patch.object(diagnostics, "tmux_session_exists", return_value=True),
            mock.patch.object(diagnostics, "tmux_session_count", return_value=4),
            mock.patch.object(diagnostics.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"),
        ):
            return diagnostics.build_report(layout)

    def test_doctor_report_is_privacy_safe_and_marks_legacy_supervision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            report = self.report(self.layout(Path(temp)))

        encoded = json.dumps(report, ensure_ascii=False).lower()
        by_id = {item["id"]: item for item in report["checks"]}
        self.assertTrue(report["ok"])
        self.assertEqual(report["counts"]["error"], 0)
        self.assertEqual(report["runtime"]["tmuxSessions"], 4)
        self.assertEqual(by_id["codex-discovery"]["detail"], "dynamic per launch")
        self.assertEqual(by_id["codex-auto-update"]["detail"], "enabled; not checked")
        self.assertTrue(diagnostics.compact_status(report)["legacyOwner"])
        for forbidden in ("private-owner-token", "private@example.invalid", str(Path(temp)).lower(), "/private/bin/codex"):
            self.assertNotIn(forbidden, encoded)

    def test_unsafe_private_files_are_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            report = self.report(self.layout(Path(temp), unsafe=True))

        self.assertFalse(report["ok"])
        failed = {item["id"] for item in report["checks"] if item["status"] == "error"}
        self.assertTrue({"owner-config", "gateway-config", "gateway-auth"}.issubset(failed))

    def test_worker_diagnostics_count_only_valid_faryo_instances(self) -> None:
        output = (
            f"faryo-appserver-worker@{'a' * 24}.service loaded active running Worker A\n"
            f"faryo-appserver-worker@{'b' * 24}.service loaded failed failed Worker B\n"
            "faryo-owner.service loaded active running Owner\n"
            "faryo-appserver-worker@../../bad.service loaded active running Invalid\n"
        )
        with (
            mock.patch.object(diagnostics.shutil, "which", return_value="/usr/bin/systemctl"),
            mock.patch.object(
                diagnostics,
                "run_command",
                return_value=subprocess.CompletedProcess(["systemctl"], 0, output, ""),
            ),
        ):
            counts = diagnostics.appserver_worker_service_counts()

        self.assertEqual(counts, (2, 1, 1))

    def test_cli_json_and_human_output_have_stable_exit_codes(self) -> None:
        report = {
            "schemaVersion": 1,
            "ok": True,
            "checks": [{"id": "python", "status": "ok", "detail": "Python test"}],
            "counts": {"ok": 1, "warn": 0, "error": 0},
            "services": {"owner": "active", "gateway": "active", "legacyKeepalive": "inactive"},
            "runtime": {"environment": "venv", "tmuxSessions": 3},
        }
        with mock.patch.object(cli, "build_report", return_value=report):
            output = StringIO()
            with redirect_stdout(output):
                code = cli.main(["doctor", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["counts"]["ok"], 1)

            output = StringIO()
            with redirect_stdout(output):
                code = cli.main(["status"])
            self.assertEqual(code, 0)
            self.assertIn("Owner:  active", output.getvalue())

    def test_public_help_hides_internal_service_entry_points(self) -> None:
        help_text = cli.parser().format_help()
        self.assertNotIn("internal", help_text)
        self.assertNotIn("SUPPRESS", help_text)
        for command in ("install", "update", "rollback", "uninstall", "doctor"):
            self.assertIn(command, help_text)

    def test_source_root_discovery_uses_application_markers(self) -> None:
        self.assertEqual(diagnostics.discover_source_root({"FARYO_INSTALL_ROOT": str(ROOT)}), ROOT)
        with tempfile.TemporaryDirectory() as temp:
            self.assertIsNone(diagnostics.discover_source_root({"FARYO_INSTALL_ROOT": temp}))

    def test_installed_cli_discovers_its_managed_current_application(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            app = home / ".local/share/faryo/versions/v1.5.0/app"
            for relative in (
                "apps/owner/local-tmux-owner/server.py",
                "apps/owner/local-tmux-owner/run_owner_asgi.py",
                "apps/gateway/server/run_asgi.py",
            ):
                path = app / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n", encoding="utf-8")
            current = home / ".local/share/faryo/current"
            current.symlink_to(Path("versions/v1.5.0"))

            installed_module = home / ".local/share/faryo/current/.venv/lib/python3.10/site-packages/faryo_cli/diagnostics.py"
            self.assertEqual(diagnostics.discover_source_root({"HOME": str(home)}, module_file=installed_module), current / "app")

    def test_source_checkout_precedes_managed_current_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            managed = home / ".local/share/faryo/current/app"
            for relative in (
                "apps/owner/local-tmux-owner/server.py",
                "apps/owner/local-tmux-owner/run_owner_asgi.py",
                "apps/gateway/server/run_asgi.py",
            ):
                path = managed / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n", encoding="utf-8")
            for root in (managed, ROOT):
                self.assertTrue((root / "apps/owner/local-tmux-owner/server.py").is_file())
                self.assertTrue((root / "apps/owner/local-tmux-owner/run_owner_asgi.py").is_file())
                self.assertTrue((root / "apps/gateway/server/run_asgi.py").is_file())
            module_file = ROOT / "src/faryo_cli/diagnostics.py"
            self.assertEqual(diagnostics.discover_source_root({"HOME": str(home)}, module_file=module_file), ROOT)

    def test_codex_resolution_supports_configured_and_nvm_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            older = home / ".nvm/versions/node/v22.0.0/bin/codex"
            latest = home / ".nvm/versions/node/v24.0.0/bin/codex"
            for path in (older, latest):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("#!/bin/sh\n", encoding="utf-8")
                path.chmod(0o700)
            alias = home / ".nvm/alias/default"
            alias.parent.mkdir(parents=True)
            alias.write_text("24\n", encoding="utf-8")

            with mock.patch.object(diagnostics.shutil, "which", return_value=None):
                self.assertEqual(diagnostics.resolve_codex("", home), str(latest))
                self.assertEqual(diagnostics.resolve_codex(str(older), home), str(latest))
                with mock.patch.dict(os.environ, {"FARYO_CODEX_BIN_PINNED": "1"}):
                    self.assertEqual(diagnostics.resolve_codex(str(older), home), str(older))

    def test_codex_resolution_follows_recursive_nvm_default_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            older = home / ".nvm/versions/node/v22.0.0/bin/codex"
            selected = home / ".nvm/versions/node/v24.1.0/bin/codex"
            for path in (older, selected):
                path.parent.mkdir(parents=True)
                path.write_text("#!/bin/sh\n", encoding="utf-8")
                path.chmod(0o700)
            alias = home / ".nvm/alias"
            (alias / "lts").mkdir(parents=True)
            (alias / "default").write_text("lts/*\n", encoding="utf-8")
            (alias / "lts/*").write_text("lts/example\n", encoding="utf-8")
            (alias / "lts/example").write_text("v24.1.0\n", encoding="utf-8")

            self.assertEqual(
                codex_runtime.resolve_codex("", home, {"HOME": str(home), "PATH": "/usr/bin"}),
                str(selected),
            )

    def test_codex_javascript_launcher_uses_sibling_node(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "versions/node/v24.0.0"
            node = root / "bin/node"
            launcher = root / "bin/codex"
            script = root / "lib/node_modules/@openai/codex/bin/codex.js"
            node.parent.mkdir(parents=True)
            script.parent.mkdir(parents=True)
            node.write_text("runtime", encoding="utf-8")
            script.write_text("cli", encoding="utf-8")
            node.chmod(0o700)
            script.chmod(0o700)
            launcher.symlink_to(Path("../lib/node_modules/@openai/codex/bin/codex.js"))

            self.assertEqual(
                diagnostics.codex_argv(str(launcher), "--version"),
                [str(node), str(script), "--version"],
            )

    def test_agent_environment_removes_faryo_internals_and_keeps_user_python_paths(self) -> None:
        root = "/opt/faryo/versions/v1.5.3/app"
        environment = codex_runtime.sanitized_agent_environment({
            "HOME": "/home/example",
            "PATH": "/usr/bin",
            "FARYO_INSTALL_ROOT": root,
            "FARYO_PYTHON": "/opt/faryo/versions/v1.5.3/.venv/bin/python",
            "FARYO_OWNER_TOKEN": "private",
            "GATEWAY_AUTH_CONFIG": "/private/auth.json",
            "PYTHONPATH": f"{root}/src:/workspace/python",
            "PWD": root,
            "OLDPWD": "/workspace/previous",
        })

        self.assertEqual(environment["HOME"], "/home/example")
        self.assertEqual(environment["PYTHONPATH"], "/workspace/python")
        self.assertNotIn("PWD", environment)
        self.assertEqual(environment["OLDPWD"], "/workspace/previous")
        self.assertFalse(any(name.startswith(("FARYO_", "GATEWAY_")) for name in environment))

    def test_legacy_start_is_idempotent_and_keeps_gateway_managed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            layout = self.layout(Path(temp))
            actions = []

            def exists(name):
                return name in {"faryo-gateway.service", "faryo-owner-keepalive.timer"}

            with (
                mock.patch.object(operations, "unit_exists", side_effect=exists),
                mock.patch.object(operations, "control_service", side_effect=lambda name, action: actions.append((name, action))),
                mock.patch.object(operations, "http_status", return_value=200),
                mock.patch.object(operations, "run_legacy_owner") as legacy,
                mock.patch.object(operations, "wait_for_health") as wait,
            ):
                result = operations.service_operation("start", layout)

        self.assertEqual(result, "started")
        self.assertEqual(actions, [
            ("faryo-owner-keepalive.timer", "start"),
            ("faryo-gateway.service", "start"),
        ])
        legacy.assert_not_called()
        wait.assert_called_once_with(layout)

    def test_direct_stop_never_touches_tmux_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            layout = self.layout(Path(temp))
            actions = []
            with (
                mock.patch.object(operations, "unit_exists", return_value=True),
                mock.patch.object(operations, "control_service", side_effect=lambda name, action: actions.append((name, action))),
                mock.patch.object(
                    operations,
                    "systemctl",
                    return_value=subprocess.CompletedProcess(["systemctl"], 0, "", ""),
                ),
                mock.patch.object(operations, "run_legacy_owner") as legacy,
            ):
                result = operations.service_operation("stop", layout)

        self.assertEqual(result, "stopped")
        self.assertEqual(actions, [
            ("faryo-gateway.service", "stop"),
            ("faryo-owner.service", "stop"),
            ("faryo-appserver.service", "stop"),
        ])
        legacy.assert_not_called()

    def test_open_prints_only_loopback_gateway_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            layout = self.layout(Path(temp))
            self.assertEqual(operations.open_gateway(layout, print_only=True), "http://127.0.0.1:8780/")

    def test_cli_service_failures_are_bounded(self) -> None:
        output = StringIO()
        with mock.patch.object(cli, "service_operation", side_effect=operations.OperationError("bounded failure")):
            with redirect_stdout(output), mock.patch("sys.stderr", new=StringIO()) as error:
                code = cli.main(["restart"])
        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("bounded failure", error.getvalue())

    def test_direct_owner_spec_keeps_token_out_of_argv(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            layout = self.layout(Path(temp))
            spec = runtime.owner_process(layout)

        self.assertIn("private-owner-token", spec.environment.values())
        self.assertNotIn("private-owner-token", " ".join(spec.argv))
        self.assertTrue(spec.argv[1].endswith("run_owner_asgi.py"))
        self.assertEqual(spec.argv[-4:], ["--host", "127.0.0.1", "--port", "8765"])
        self.assertEqual(spec.environment["FARYO_PYTHON"], sys.executable)

    def test_direct_gateway_spec_uses_private_files_without_token_argv(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            layout = self.layout(Path(temp))
            spec = runtime.gateway_process(layout)

        self.assertIn("--auth-config", spec.argv)
        self.assertIn("--owner-env", spec.argv)
        self.assertEqual(spec.argv[spec.argv.index("--owner-env") + 1], str(layout.gateway_env))
        self.assertNotIn("private-owner-token", " ".join(spec.argv))
        self.assertEqual(spec.environment["FARYO_GATEWAY_SESSION_HOURS"], "720")

        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); import server; print(','.join(server.BACKENDS))",
                str(ROOT / "apps/gateway/server"),
            ],
            env=spec.environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stdout.strip(), "txy")

    def test_appserver_spec_resolves_codex_per_start_and_keeps_socket_private(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            layout = self.layout(Path(temp))
            with (
                mock.patch.object(codex_runtime, "resolve_codex", return_value="/runtime/bin/codex") as resolve,
                mock.patch.object(codex_runtime, "codex_argv", return_value=["/runtime/bin/node", "/runtime/codex.js", "app-server"]),
            ):
                spec = runtime.appserver_process(layout)
            socket = runtime.appserver_socket_path(layout, diagnostics.read_env(layout.owner_env))
            runtime.prepare_appserver_runtime(layout, socket)
            socket_parent_mode = socket.parent.stat().st_mode & 0o777

        resolve.assert_called_once()
        self.assertEqual(spec.cwd, layout.home)
        self.assertEqual(spec.argv[:2], ["/runtime/bin/node", "/runtime/codex.js"])
        self.assertNotIn("private-owner-token", " ".join(spec.argv))
        self.assertFalse(any(name.startswith(("FARYO_", "GATEWAY_")) for name in spec.environment))
        self.assertEqual(socket.name, "codex-app-server.sock")
        self.assertEqual(socket_parent_mode, 0o700)

    def test_appserver_socket_cannot_escape_private_runtime_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            layout = self.layout(Path(temp))
            values = diagnostics.read_env(layout.owner_env)
            values["FARYO_CODEX_APP_SERVER_SOCKET"] = str(layout.home / "public.sock")
            with self.assertRaisesRegex(operations.OperationError, "must remain"):
                runtime.appserver_socket_path(layout, values)

    def test_appserver_worker_spec_uses_one_validated_private_socket(self) -> None:
        worker_id = "a" * 24
        with tempfile.TemporaryDirectory() as temp:
            layout = self.layout(Path(temp))
            with (
                mock.patch.object(codex_runtime, "resolve_codex", return_value="/runtime/bin/codex"),
                mock.patch.object(
                    codex_runtime,
                    "codex_argv",
                    side_effect=lambda executable, *args: [executable, *args],
                ),
            ):
                spec = runtime.appserver_worker_process(worker_id, layout)
            socket_path = appserver_workers.worker_socket_path(layout, worker_id)

        self.assertEqual(spec.argv[-2:], ["--listen", f"unix://{socket_path}"])
        self.assertEqual(socket_path.name, f"{worker_id}.sock")
        self.assertNotIn("private-owner-token", " ".join(spec.argv))
        self.assertFalse(any(name.startswith(("FARYO_", "GATEWAY_")) for name in spec.environment))

    def test_appserver_worker_spec_rejects_unit_name_injection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            layout = self.layout(Path(temp))
            with (
                mock.patch.object(
                    codex_runtime,
                    "resolve_codex",
                    side_effect=AssertionError("Codex discovery must follow worker validation"),
                ),
                self.assertRaisesRegex(operations.OperationError, "worker id is invalid"),
            ):
                runtime.appserver_worker_process("../faryo-owner", layout)

    def test_direct_runtime_rejects_non_loopback_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            layout = self.layout(Path(temp))
            body = layout.owner_env.read_text(encoding="utf-8").replace("127.0.0.1", "0.0.0.0")
            layout.owner_env.write_text(body, encoding="utf-8")
            layout.owner_env.chmod(0o600)
            with self.assertRaisesRegex(operations.OperationError, "must remain loopback"):
                runtime.owner_process(layout)

    def test_internal_owner_command_executes_only_validated_spec(self) -> None:
        spec = runtime.ProcessSpec([sys.executable, "server.py"], ROOT, {})
        with (
            mock.patch.object(cli, "owner_process", return_value=spec),
            mock.patch.object(cli, "exec_process") as execute,
        ):
            code = cli.main(["internal", "run-owner"])
        self.assertEqual(code, 0)
        execute.assert_called_once_with(spec)

    def test_internal_appserver_prepares_private_runtime_before_exec(self) -> None:
        spec = runtime.ProcessSpec(["codex", "app-server"], ROOT, {})
        layout = diagnostics.Layout(Path("/tmp/fixture"), Path("/tmp/fixture/.faryo"), Path("/tmp/owner.env"), Path("/tmp/gateway.env"), Path("/tmp/auth.json"), ROOT)
        socket = Path("/tmp/fixture/.faryo/owner/runtime/codex-app-server.sock")
        with (
            mock.patch.object(cli.Layout, "from_environment", return_value=layout),
            mock.patch.object(cli, "read_env", return_value={}),
            mock.patch.object(cli, "appserver_socket_path", return_value=socket),
            mock.patch.object(cli, "appserver_process", return_value=spec),
            mock.patch.object(cli, "prepare_appserver_runtime") as prepare,
            mock.patch.object(cli, "exec_process") as execute,
        ):
            code = cli.main(["internal", "run-appserver"])
        self.assertEqual(code, 0)
        prepare.assert_called_once_with(layout, socket)
        execute.assert_called_once_with(spec)

    def test_internal_worker_command_executes_only_validated_spec(self) -> None:
        spec = runtime.ProcessSpec(["codex", "app-server"], ROOT, {})
        worker_id = "b" * 24
        with (
            mock.patch.object(cli, "appserver_worker_process", return_value=spec) as worker_process,
            mock.patch.object(cli, "exec_process") as execute,
        ):
            code = cli.main(["internal", "run-appserver-worker", worker_id])
        self.assertEqual(code, 0)
        worker_process.assert_called_once_with(worker_id)
        execute.assert_called_once_with(spec)

    def test_service_units_use_unified_cli_and_no_legacy_owner_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            layout = self.layout(Path(temp))
            owner = installer.rendered_unit("owner", layout, sys.executable)
            gateway = installer.rendered_unit("gateway", layout, sys.executable)
            worker = installer.rendered_unit("appserver-worker", layout, sys.executable)

        self.assertIn("-m faryo_cli internal run-owner", owner)
        self.assertIn("-m faryo_cli internal run-gateway", gateway)
        self.assertNotIn("start-web-owner.sh", owner)
        self.assertNotIn("run-gateway.sh", gateway)
        self.assertNotIn("PYTHONPATH=", owner + gateway)
        self.assertNotIn("@FARYO_", owner + gateway)
        self.assertIn("internal run-appserver-worker %i", worker)
        self.assertIn("KillMode=mixed", worker)
        self.assertNotIn("@FARYO_", worker)

    def test_service_unit_preserves_private_venv_python_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            layout = self.layout(root)
            target = root / "runtime/python3.10"
            venv_python = root / "version/.venv/bin/python"
            target.parent.mkdir(parents=True)
            venv_python.parent.mkdir(parents=True)
            target.write_text("python", encoding="utf-8")
            target.chmod(0o700)
            venv_python.symlink_to(target)

            unit = installer.rendered_unit("owner", layout, str(venv_python))

        self.assertIn(f'ExecStart="{venv_python}" -m faryo_cli', unit)
        self.assertIn("KillMode=process", unit)
        self.assertNotIn("KillMode=mixed", unit)

    def test_atomic_unit_install_backs_up_existing_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            layout = self.layout(root)
            xdg = root / "config"
            unit_dir = xdg / "systemd/user"
            unit_dir.mkdir(parents=True)
            old = unit_dir / "faryo-owner.service"
            old.write_text("old unit\n", encoding="utf-8")
            with (
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}, clear=False),
                mock.patch.object(installer, "systemctl") as systemctl,
            ):
                installed = installer.install_user_units(layout, components=("owner",), python=sys.executable)

            backup = root / ".local/share/faryo/state/unit-backups/faryo-owner.service.previous"
            self.assertEqual(installed, ["faryo-owner.service"])
            self.assertEqual(backup.read_text(encoding="utf-8"), "old unit\n")
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            self.assertEqual(old.stat().st_mode & 0o777, 0o644)
            systemctl.assert_called_once_with("daemon-reload")

    def test_unit_path_rejects_control_characters(self) -> None:
        with self.assertRaisesRegex(operations.OperationError, "control characters"):
            installer.unit_escape("bad\npath")
        self.assertEqual(installer.unit_path_escape("/path/with space"), "/path/with\\x20space")

    def test_owner_migration_stops_only_legacy_supervision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            layout = self.layout(Path(temp))
            calls = []
            with (
                mock.patch.object(migration, "unit_exists", return_value=True),
                mock.patch.object(migration, "legacy_owner_exists", return_value=True),
                mock.patch.object(migration, "service_state", return_value="inactive"),
                mock.patch.object(migration, "tmux_geometry", side_effect=[{"faryo1": (145, 44)}, {"faryo1": (145, 44)}]),
                mock.patch.object(migration, "stop_legacy_owner") as stop_legacy,
                mock.patch.object(migration, "wait_owner") as wait_owner,
                mock.patch.object(migration, "systemctl", side_effect=lambda *args, **kwargs: calls.append((args, kwargs))),
            ):
                result = migration.migrate_owner(layout)

        self.assertEqual(result, "migrated")
        stop_legacy.assert_called_once_with()
        wait_owner.assert_called_once_with(layout)
        self.assertIn((("enable", "faryo-owner.service"), {}), calls)
        self.assertIn((("start", "faryo-owner.service"), {}), calls)
        self.assertIn((("disable", "faryo-owner-keepalive.timer"), {"check": False}), calls)

    def test_owner_migration_restores_legacy_on_health_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            layout = self.layout(Path(temp))
            with (
                mock.patch.object(migration, "unit_exists", return_value=True),
                mock.patch.object(migration, "legacy_owner_exists", return_value=True),
                mock.patch.object(migration, "service_state", return_value="inactive"),
                mock.patch.object(migration, "tmux_geometry", return_value={"faryo1": (145, 44)}),
                mock.patch.object(migration, "stop_legacy_owner"),
                mock.patch.object(migration, "wait_owner", side_effect=operations.OperationError("not healthy")),
                mock.patch.object(migration, "restore_legacy") as restore,
                mock.patch.object(migration, "systemctl"),
            ):
                with self.assertRaisesRegex(operations.OperationError, "not healthy"):
                    migration.migrate_owner(layout)

        restore.assert_called_once_with(layout)

    def test_owner_migration_rejects_existing_geometry_change(self) -> None:
        with self.assertRaisesRegex(operations.OperationError, "geometry changed"):
            migration.verify_geometry({"faryo1": (145, 44)}, {"faryo1": (500, 44)})

    def test_install_requires_explicit_legacy_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            layout = self.layout(Path(temp))
            with (
                mock.patch.object(migration, "legacy_owner_exists", return_value=True),
                mock.patch.object(runtime, "appserver_process"),
            ):
                with self.assertRaisesRegex(operations.OperationError, "requires --migrate-owner"):
                    installer.install_services(layout, python=sys.executable)

    def test_install_starts_direct_services_after_atomic_units(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            layout = self.layout(root)
            xdg = root / "config"
            actions = []
            with (
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}, clear=False),
                mock.patch.object(migration, "legacy_owner_exists", return_value=False),
                mock.patch.object(migration, "tmux_process_snapshot", return_value={"faryo1": (145, 44, 100)}),
                mock.patch.object(runtime, "appserver_process"),
                mock.patch.object(installer, "systemctl", side_effect=lambda *args, **kwargs: actions.append((args, kwargs))),
                mock.patch.object(operations, "control_service", side_effect=lambda name, action: actions.append(((name, action), {}))),
                mock.patch.object(operations, "wait_for_health") as wait,
            ):
                result = installer.install_services(layout, python=sys.executable)

            self.assertEqual(result, "installed")
            self.assertTrue((xdg / "systemd/user/faryo-owner.service").is_file())
            self.assertTrue((xdg / "systemd/user/faryo-gateway.service").is_file())
            self.assertTrue((xdg / "systemd/user/faryo-appserver.service").is_file())
            self.assertTrue((xdg / "systemd/user/faryo-appserver-worker@.service").is_file())
            self.assertIn((("faryo-appserver.service", "start"), {}), actions)
            self.assertIn((("faryo-owner.service", "restart"), {}), actions)
            self.assertIn((("faryo-gateway.service", "restart"), {}), actions)
            wait.assert_called_once_with(layout)

    def test_install_migrates_shared_appserver_only_after_idle_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            layout = self.layout(root)
            registry = installer.appserver_registry_path(layout)
            registry.parent.mkdir(parents=True, exist_ok=True)
            registry.write_text(
                '{"schemaVersion":1,"sessions":[{"name":"faryo1","thread_id":"fixture-thread","cwd":"/workspace"}]}\n',
                encoding="utf-8",
            )
            registry.chmod(0o600)
            xdg = root / "config"
            actions = []
            with (
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}, clear=False),
                mock.patch.object(migration, "legacy_owner_exists", return_value=False),
                mock.patch.object(migration, "tmux_process_snapshot", return_value={"faryo1": (145, 44, 100)}),
                mock.patch.object(runtime, "appserver_process"),
                mock.patch.object(installer, "require_idle_appserver_transition") as preflight,
                mock.patch.object(installer, "systemctl", side_effect=lambda *args, **kwargs: actions.append((args, kwargs))),
                mock.patch.object(operations, "control_service", side_effect=lambda name, action: actions.append(((name, action), {}))),
                mock.patch.object(operations, "wait_for_health"),
            ):
                result = installer.install_services(layout, python=sys.executable)

        self.assertEqual(result, "installed")
        preflight.assert_called_once_with(layout)
        self.assertIn((("faryo-owner.service", "stop"), {}), actions)
        self.assertIn((("faryo-appserver.service", "restart"), {}), actions)
        self.assertIn((("faryo-owner.service", "start"), {}), actions)
        self.assertNotIn((("faryo-owner.service", "restart"), {}), actions)

    def test_install_refuses_shared_topology_migration_during_active_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            layout = self.layout(root)
            registry = installer.appserver_registry_path(layout)
            registry.parent.mkdir(parents=True, exist_ok=True)
            registry.write_text(
                '{"schemaVersion":1,"sessions":[{"name":"faryo1","thread_id":"fixture-thread","cwd":"/workspace"}]}\n',
                encoding="utf-8",
            )
            registry.chmod(0o600)
            xdg = root / "config"
            with (
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}, clear=False),
                mock.patch.object(migration, "legacy_owner_exists", return_value=False),
                mock.patch.object(runtime, "appserver_process"),
                mock.patch.object(installer, "active_appserver_session_count", return_value=1),
                mock.patch.object(installer, "systemctl") as systemctl,
            ):
                with self.assertRaisesRegex(operations.OperationError, "wait for them to become idle"):
                    installer.install_services(layout, python=sys.executable)

        systemctl.assert_not_called()
        self.assertFalse((xdg / "systemd/user/faryo-appserver-worker@.service").exists())

    def test_install_downgrades_worker_topology_only_after_idle_preflight(self) -> None:
        worker_unit = f"faryo-appserver-worker@{'a' * 24}.service"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            layout = self.layout(root)
            registry = installer.appserver_registry_path(layout)
            registry.parent.mkdir(parents=True, exist_ok=True)
            registry.write_text(
                json.dumps(
                    {
                        "schemaVersion": 2,
                        "sessions": [
                            {
                                "name": "faryo1",
                                "thread_id": "fixture-thread",
                                "cwd": "/workspace",
                                "worker_id": "a" * 24,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            registry.chmod(0o600)
            xdg = root / "config"
            unit_dir = xdg / "systemd/user"
            unit_dir.mkdir(parents=True)
            worker_template = unit_dir / installer.UNIT_NAMES["appserver-worker"]
            worker_template.write_text("previous worker template\n", encoding="utf-8")
            actions = []
            with (
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}, clear=False),
                mock.patch.object(installer, "source_supports_worker_units", return_value=False),
                mock.patch.object(migration, "legacy_owner_exists", return_value=False),
                mock.patch.object(migration, "tmux_process_snapshot", return_value={}),
                mock.patch.object(runtime, "appserver_process"),
                mock.patch.object(installer, "require_idle_appserver_transition") as preflight,
                mock.patch.object(appserver_workers, "listed_worker_units", return_value=[worker_unit]),
                mock.patch.object(
                    installer,
                    "systemctl",
                    side_effect=lambda *args, **kwargs: actions.append((args, kwargs)),
                ),
                mock.patch.object(
                    operations,
                    "control_service",
                    side_effect=lambda name, action: actions.append(((name, action), {})),
                ),
                mock.patch.object(operations, "wait_for_health"),
            ):
                result = installer.install_services(layout, python=sys.executable)

            rewritten = json.loads(registry.read_text(encoding="utf-8"))
            worker_template_exists = worker_template.exists()

        self.assertEqual(result, "installed")
        preflight.assert_called_once_with(layout)
        self.assertEqual(rewritten["schemaVersion"], 1)
        self.assertIn((("stop", worker_unit), {"check": False}), actions)
        self.assertFalse(worker_template_exists)

    def test_failed_topology_upgrade_restores_schema_and_unit_set(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            layout = self.layout(root)
            registry = installer.appserver_registry_path(layout)
            registry.parent.mkdir(parents=True, exist_ok=True)
            registry.write_text(
                '{"schemaVersion":1,"sessions":[{"name":"faryo1","thread_id":"fixture-thread","cwd":"/workspace"}]}\n',
                encoding="utf-8",
            )
            registry.chmod(0o600)
            xdg = root / "config"
            with (
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}, clear=False),
                mock.patch.object(migration, "legacy_owner_exists", return_value=False),
                mock.patch.object(migration, "tmux_process_snapshot", return_value={}),
                mock.patch.object(runtime, "appserver_process"),
                mock.patch.object(installer, "require_idle_appserver_transition"),
                mock.patch.object(installer, "systemctl"),
                mock.patch.object(operations, "control_service"),
                mock.patch.object(
                    operations,
                    "wait_for_health",
                    side_effect=operations.OperationError("not healthy"),
                ),
            ):
                with self.assertRaisesRegex(operations.OperationError, "not healthy"):
                    installer.install_services(layout, python=sys.executable)

            rewritten = json.loads(registry.read_text(encoding="utf-8"))
            worker_template_exists = (
                xdg / "systemd/user" / installer.UNIT_NAMES["appserver-worker"]
            ).exists()

        self.assertEqual(rewritten["schemaVersion"], 1)
        self.assertFalse(worker_template_exists)

    def test_failed_topology_downgrade_restores_schema_and_worker_template(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            layout = self.layout(root)
            registry = installer.appserver_registry_path(layout)
            registry.parent.mkdir(parents=True, exist_ok=True)
            registry.write_text(
                json.dumps(
                    {
                        "schemaVersion": 2,
                        "sessions": [
                            {
                                "name": "faryo1",
                                "thread_id": "fixture-thread",
                                "cwd": "/workspace",
                                "worker_id": "b" * 24,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            registry.chmod(0o600)
            xdg = root / "config"
            unit_dir = xdg / "systemd/user"
            unit_dir.mkdir(parents=True)
            worker_template = unit_dir / installer.UNIT_NAMES["appserver-worker"]
            worker_template.write_text("previous worker template\n", encoding="utf-8")
            with (
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}, clear=False),
                mock.patch.object(installer, "source_supports_worker_units", return_value=False),
                mock.patch.object(migration, "legacy_owner_exists", return_value=False),
                mock.patch.object(migration, "tmux_process_snapshot", return_value={}),
                mock.patch.object(runtime, "appserver_process"),
                mock.patch.object(installer, "require_idle_appserver_transition"),
                mock.patch.object(appserver_workers, "listed_worker_units", return_value=[]),
                mock.patch.object(installer, "systemctl"),
                mock.patch.object(operations, "control_service"),
                mock.patch.object(
                    operations,
                    "wait_for_health",
                    side_effect=operations.OperationError("not healthy"),
                ),
            ):
                with self.assertRaisesRegex(operations.OperationError, "not healthy"):
                    installer.install_services(layout, python=sys.executable)

            rewritten = json.loads(registry.read_text(encoding="utf-8"))
            restored_template = worker_template.read_text(encoding="utf-8")

        self.assertEqual(rewritten["schemaVersion"], 2)
        self.assertEqual(restored_template, "previous worker template\n")

    def test_registry_schema_rewrite_is_body_preserving_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state/appserver-sessions.json"
            path.parent.mkdir(parents=True)
            original = {
                "schemaVersion": 2,
                "sessions": [{"name": "faryo1", "thread_id": "fixture-thread", "cwd": "/workspace", "worker_id": "a" * 24}],
            }
            path.write_text(json.dumps(original), encoding="utf-8")

            installer.rewrite_registry_schema(path, 1)
            rewritten = json.loads(path.read_text(encoding="utf-8"))
            mode = path.stat().st_mode & 0o777

        self.assertEqual(rewritten["schemaVersion"], 1)
        self.assertEqual(rewritten["sessions"], original["sessions"])
        self.assertEqual(mode, 0o600)

    def test_install_restores_previous_units_when_health_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            layout = self.layout(root)
            xdg = root / "config"
            unit_dir = xdg / "systemd/user"
            unit_dir.mkdir(parents=True)
            gateway = unit_dir / "faryo-gateway.service"
            gateway.write_text("old gateway unit\n", encoding="utf-8")
            with (
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}, clear=False),
                mock.patch.object(migration, "legacy_owner_exists", return_value=False),
                mock.patch.object(migration, "tmux_process_snapshot", return_value={"faryo1": (145, 44, 100)}),
                mock.patch.object(runtime, "appserver_process"),
                mock.patch.object(installer, "systemctl"),
                mock.patch.object(operations, "control_service"),
                mock.patch.object(operations, "wait_for_health", side_effect=operations.OperationError("not healthy")),
            ):
                with self.assertRaisesRegex(operations.OperationError, "not healthy"):
                    installer.install_services(layout, python=sys.executable)

            self.assertEqual(gateway.read_text(encoding="utf-8"), "old gateway unit\n")
            self.assertFalse((unit_dir / "faryo-owner.service").exists())

    def test_failed_service_upgrade_restarts_both_previous_services(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            layout = self.layout(root)
            xdg = root / "config"
            unit_dir = xdg / "systemd/user"
            unit_dir.mkdir(parents=True)
            for name in installer.UNIT_NAMES.values():
                (unit_dir / name).write_text(f"old {name}\n", encoding="utf-8")
            calls = []
            with (
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}, clear=False),
                mock.patch.object(migration, "legacy_owner_exists", return_value=False),
                mock.patch.object(migration, "tmux_process_snapshot", return_value={"faryo1": (145, 44, 100)}),
                mock.patch.object(runtime, "appserver_process"),
                mock.patch.object(installer, "systemctl", side_effect=lambda *args, **kwargs: calls.append((args, kwargs))),
                mock.patch.object(operations, "control_service"),
                mock.patch.object(operations, "wait_for_health", side_effect=operations.OperationError("not healthy")),
            ):
                with self.assertRaisesRegex(operations.OperationError, "not healthy"):
                    installer.install_services(layout, python=sys.executable)

            self.assertIn((("restart", "faryo-owner.service"), {"check": False}), calls)
            self.assertIn((("restart", "faryo-gateway.service"), {"check": False}), calls)
            self.assertIn((("start", "faryo-appserver.service"), {"check": False}), calls)

    def test_owner_restart_rejects_recreated_or_missing_tmux_sessions(self) -> None:
        before = {"faryo1": (145, 44, 101), "faryo2": (145, 44, 202)}
        with self.assertRaisesRegex(operations.OperationError, "tmux sessions changed"):
            migration.verify_process_snapshot(before, {"faryo1": (145, 44, 999)})

    def test_missing_tmux_server_is_an_empty_snapshot_for_fresh_install(self) -> None:
        result = subprocess.CompletedProcess(["tmux"], 1, "", "no server running on /tmp/tmux-fixture/default")
        with (
            mock.patch.object(migration.shutil, "which", return_value="/usr/bin/tmux"),
            mock.patch.object(migration, "run_command", return_value=result),
        ):
            self.assertEqual(migration.tmux_process_snapshot(), {})

    def test_runtime_python_update_preserves_private_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "faryo.env"
            path.write_text("FARYO_OWNER_TOKEN=private-token\nFARYO_PYTHON=/old/python\n", encoding="utf-8")
            path.chmod(0o600)

            application.replace_env_value(path, "FARYO_PYTHON", "/new venv/bin/python")

            body = path.read_text(encoding="utf-8")
            self.assertIn("FARYO_OWNER_TOKEN=private-token", body)
            self.assertIn("FARYO_PYTHON='/new venv/bin/python'", body)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_version_activation_and_restore_are_atomic_and_relative(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ,
            {"FARYO_PROGRAM_HOME": str(Path(temp) / "program")},
            clear=False,
        ):
            root = Path(temp)
            layout = self.layout(root)
            program = application.ProgramLayout.from_layout(layout)
            first = program.versions / "v1.4.1"
            second = program.versions / "v1.5.0"
            for version in (first, second):
                cli_path = version / ".venv/bin/faryo"
                cli_path.parent.mkdir(parents=True)
                cli_path.write_text("cli", encoding="utf-8")

            self.assertIsNone(application.activate_version(first, layout))
            previous = application.activate_version(second, layout)
            self.assertEqual(previous, first)
            self.assertEqual(program.current.resolve(), second)
            self.assertEqual(program.bin_path.resolve(), second / ".venv/bin/faryo")
            self.assertEqual((program.state / "previous-version").read_text(encoding="utf-8"), "v1.4.1\n")

            application.restore_activation(previous, layout)
            self.assertEqual(program.current.resolve(), first)
            self.assertEqual(program.bin_path.resolve(), first / ".venv/bin/faryo")

    def test_repeated_activation_preserves_the_real_previous_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ,
            {"FARYO_PROGRAM_HOME": str(Path(temp) / "program")},
            clear=False,
        ):
            layout = self.layout(Path(temp))
            program = application.ProgramLayout.from_layout(layout)
            previous = program.versions / "v1.4.1"
            current = program.versions / "v1.5.0"
            for version in (previous, current):
                cli_path = version / ".venv/bin/faryo"
                cli_path.parent.mkdir(parents=True)
                cli_path.write_text("cli", encoding="utf-8")
            application.activate_version(previous, layout)
            application.activate_version(current, layout)
            marker = program.state / "previous-version"
            self.assertEqual(marker.read_text(encoding="utf-8"), "v1.4.1\n")

            self.assertEqual(application.activate_version(current, layout), current)
            self.assertEqual(marker.read_text(encoding="utf-8"), "v1.4.1\n")

    def test_prepare_version_builds_venv_at_its_final_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ,
            {"FARYO_PROGRAM_HOME": str(Path(temp) / "program")},
            clear=False,
        ):
            layout = self.layout(Path(temp))
            program = application.ProgramLayout.from_layout(layout)

            def fake_source(_source, destination):
                release = destination / "apps/owner/RELEASE"
                release.parent.mkdir(parents=True)
                release.write_text("test\n", encoding="utf-8")
                return "fixture-revision"

            def fake_venv(path, _python):
                cli_path = application.venv_cli(path)
                cli_path.parent.mkdir(parents=True)
                cli_path.write_text("cli", encoding="utf-8")

            with (
                mock.patch.object(application, "copy_source", side_effect=fake_source),
                mock.patch.object(application, "create_private_venv", side_effect=fake_venv) as create,
                mock.patch.object(application, "private_venv_version", return_value="3.10.12"),
                mock.patch.object(application, "select_bootstrap_python", return_value=sys.executable),
                mock.patch.object(
                    application,
                    "run_binary",
                    return_value=subprocess.CompletedProcess(
                        [],
                        0,
                        stdout=f"Faryo {application.__version__}\n".encode(),
                    ),
                ),
            ):
                prepared = application.prepare_version(layout, bootstrap_python=sys.executable)

            self.assertEqual(prepared, program.versions / application.version_name())
            create.assert_called_once_with(prepared, sys.executable)
            self.assertFalse((prepared / ".installing").exists())
            self.assertEqual(list(program.versions.glob(".stage-*")), [])

    def test_hardened_cli_ignores_ambient_pythonpath(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            version = Path(temp) / "versions/v1.6.0"
            cli = application.venv_cli(version)
            cli.parent.mkdir(parents=True)
            cli.write_text(
                "#!/old/python\nfrom faryo_cli.cli import main\n",
                encoding="utf-8",
            )

            application.harden_venv_cli(version)

            lines = cli.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                lines[0],
                f"#!{application.venv_python(version)} -I",
            )
            self.assertTrue(cli.stat().st_mode & stat.S_IXUSR)

    def test_cli_health_uses_isolated_python_and_exact_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            version = Path(temp) / "versions/v1.6.0"
            cli = application.venv_cli(version)
            cli.parent.mkdir(parents=True)
            cli.write_text("cli", encoding="utf-8")
            with mock.patch.object(
                application,
                "run_binary",
                return_value=subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=b"Faryo 1.6.0\n",
                ),
            ) as run:
                self.assertTrue(application.installed_cli_matches_version(version))

            self.assertEqual(
                run.call_args.args[0],
                [
                    str(application.venv_python(version)),
                    "-I",
                    str(cli),
                    "--version",
                ],
            )

    def test_prepare_version_cleans_failed_bounded_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ,
            {"FARYO_PROGRAM_HOME": str(Path(temp) / "program")},
            clear=False,
        ):
            layout = self.layout(Path(temp))
            with (
                mock.patch.object(application, "copy_source", return_value="fixture-revision"),
                mock.patch.object(application, "create_private_venv", side_effect=operations.OperationError("venv failed")),
            ):
                with self.assertRaisesRegex(operations.OperationError, "venv failed"):
                    application.prepare_version(layout, bootstrap_python=sys.executable)

            versions = application.ProgramLayout.from_layout(layout).versions
            self.assertFalse((versions / application.version_name()).exists())
            self.assertEqual(list(versions.glob(".stage-*")), [])

    def test_incomplete_version_cleanup_requires_bounded_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            versions = Path(temp) / "versions"
            version = versions / "v1.5.0"
            version.mkdir(parents=True)
            with self.assertRaisesRegex(operations.OperationError, "installation marker"):
                application.remove_incomplete_version(version, versions)
            self.assertTrue(version.exists())

    def test_versioned_install_restores_activation_and_configs_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            layout = self.layout(Path(temp))
            version = Path(temp) / "program/versions/v1.5.0"
            python = version / ".venv/bin/python"
            faryo = version / ".venv/bin/faryo"
            faryo.parent.mkdir(parents=True)
            python.write_text("python", encoding="utf-8")
            faryo.write_text("faryo", encoding="utf-8")
            before = layout.owner_env.read_text(encoding="utf-8")
            with (
                mock.patch.object(application, "prepare_version", return_value=version),
                mock.patch.object(application, "activate_version", return_value=None),
                mock.patch.object(application, "restore_activation") as restore,
                mock.patch("faryo_cli.installer.install_services", side_effect=operations.OperationError("service failed")),
            ):
                with self.assertRaisesRegex(operations.OperationError, "service failed"):
                    application.install_versioned_application(layout)

            self.assertEqual(layout.owner_env.read_text(encoding="utf-8"), before)
            restore.assert_called_once_with(None, layout)

    def test_fresh_install_initializes_private_config_in_selected_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            faryo_home = root / ".faryo"
            layout = diagnostics.Layout(
                root,
                faryo_home,
                faryo_home / "owner/config/faryo.env",
                faryo_home / "gateway/config/faryo.env",
                faryo_home / "gateway/config/gateway-auth.json",
                ROOT,
            )
            version = root / ".local/share/faryo/versions/v1.5.0"
            version.mkdir(parents=True)
            (version / "app").symlink_to(ROOT, target_is_directory=True)
            python = version / ".venv/bin/python"
            python.parent.mkdir(parents=True)
            python.symlink_to(sys.executable)
            codex = root / "bin/codex"
            codex.parent.mkdir(parents=True)
            codex.write_text("#!/bin/sh\n", encoding="utf-8")
            codex.chmod(0o700)
            workspace = root / "workspace"
            workspace.mkdir()

            with mock.patch.object(diagnostics, "resolve_codex", return_value=str(codex)):
                created = application.initialize_private_config(layout, version, workspace=str(workspace))

            self.assertTrue(created)
            owner = diagnostics.read_env(layout.owner_env)
            gateway = diagnostics.read_env(layout.gateway_env)
            self.assertEqual(owner["FARYO_START_DIRECTORY_ROOTS"], str(workspace))
            self.assertEqual(owner["FARYO_CODEX_BIN"], "")
            self.assertEqual(owner["FARYO_CODEX_BIN_PINNED"], "0")
            self.assertEqual(owner["FARYO_CODEX_AUTO_UPDATE"], "1")
            self.assertEqual(gateway["FARYO_GATEWAY_SESSION_HOURS"], "720")
            self.assertEqual(gateway["FARYO_DEFAULT_WORKSPACE"], str(workspace))
            self.assertEqual(gateway["FARYO_PYTHON"], str(python))
            self.assertTrue((layout.gateway_env.parent / "initial-password").is_file())
            for path in (layout.owner_env, layout.gateway_env, layout.gateway_auth):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            for path in (layout.faryo_home, layout.owner_env.parent, layout.gateway_env.parent):
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_existing_private_config_is_never_regenerated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            layout = self.layout(Path(temp))
            with mock.patch.object(application.subprocess, "run") as run:
                self.assertFalse(application.initialize_private_config(layout, Path(temp) / "version"))
            run.assert_not_called()

    def test_fresh_private_files_are_removed_when_install_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            faryo_home = root / ".faryo"
            layout = diagnostics.Layout(
                root,
                faryo_home,
                faryo_home / "owner/config/faryo.env",
                faryo_home / "gateway/config/faryo.env",
                faryo_home / "gateway/config/gateway-auth.json",
                ROOT,
            )
            version = root / ".local/share/faryo/versions/v1.5.0"
            python = version / ".venv/bin/python"
            python.parent.mkdir(parents=True)
            python.write_text("python", encoding="utf-8")

            def initialize(selected, _version, **_kwargs):
                for path in application.private_install_paths(selected):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("generated\n", encoding="utf-8")
                    path.chmod(0o600)
                return True

            with (
                mock.patch.object(application, "prepare_version", return_value=version),
                mock.patch.object(application, "initialize_private_config", side_effect=initialize),
                mock.patch.object(application, "activate_version", return_value=None),
                mock.patch.object(application, "restore_activation"),
                mock.patch("faryo_cli.installer.install_services", side_effect=operations.OperationError("service failed")),
            ):
                with self.assertRaisesRegex(operations.OperationError, "service failed"):
                    application.install_versioned_application(layout, version="v1.5.0")

            self.assertTrue(all(not path.exists() for path in application.private_install_paths(layout)))

    def test_bootstrap_python_prefers_supported_system_runtime(self) -> None:
        with (
            mock.patch.object(application.Path, "is_file", return_value=True),
            mock.patch.object(application.os, "access", return_value=True),
            mock.patch.object(application, "usable_bootstrap_python", side_effect=lambda value: value == "/usr/bin/python3"),
        ):
            self.assertEqual(application.select_bootstrap_python(), "/usr/bin/python3")

    def test_explicit_unsupported_bootstrap_is_rejected(self) -> None:
        with (
            mock.patch.object(application.Path, "is_file", return_value=True),
            mock.patch.object(application.os, "access", return_value=True),
            mock.patch.object(application, "usable_bootstrap_python", side_effect=lambda value: value == sys.executable),
        ):
            with self.assertRaisesRegex(operations.OperationError, "Python 3.10"):
                application.select_bootstrap_python("/old/python")

    def test_version_switch_updates_runtime_and_records_previous(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ,
            {"FARYO_PROGRAM_HOME": str(Path(temp) / ".local/share/faryo")},
            clear=False,
        ):
            layout = self.layout(Path(temp))
            program = application.ProgramLayout.from_layout(layout)
            current = program.versions / "v1.4.1"
            target = program.versions / "v1.5.0"
            for version in (current, target):
                for name in ("python", "faryo"):
                    path = version / f".venv/bin/{name}"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(name, encoding="utf-8")
            application.activate_version(current, layout)
            with (
                mock.patch.object(maintenance, "prepared_version_is_healthy", return_value=True),
                mock.patch.object(installer, "install_services") as install_services,
            ):
                result = maintenance.switch_version(target, layout)

            self.assertEqual(result, "v1.5.0")
            self.assertEqual(program.current.resolve(), target)
            self.assertEqual((program.state / "previous-version").read_text(encoding="utf-8"), "v1.4.1\n")
            self.assertIn(str(target / ".venv/bin/python"), layout.owner_env.read_text(encoding="utf-8"))
            install_services.assert_called_once()

    def test_failed_version_switch_restores_config_activation_and_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ,
            {"FARYO_PROGRAM_HOME": str(Path(temp) / ".local/share/faryo")},
            clear=False,
        ):
            layout = self.layout(Path(temp))
            program = application.ProgramLayout.from_layout(layout)
            current = program.versions / "v1.4.1"
            target = program.versions / "v1.5.0"
            for version in (current, target):
                for name in ("python", "faryo"):
                    path = version / f".venv/bin/{name}"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(name, encoding="utf-8")
            application.activate_version(current, layout)
            marker = program.state / "previous-version"
            marker.write_text("v1.3.0\n", encoding="utf-8")
            before = layout.owner_env.read_text(encoding="utf-8")
            with (
                mock.patch.object(maintenance, "prepared_version_is_healthy", return_value=True),
                mock.patch.object(installer, "install_services", side_effect=operations.OperationError("not healthy")),
            ):
                with self.assertRaisesRegex(operations.OperationError, "not healthy"):
                    maintenance.switch_version(target, layout)

            self.assertEqual(program.current.resolve(), current)
            self.assertEqual(marker.read_text(encoding="utf-8"), "v1.3.0\n")
            self.assertEqual(layout.owner_env.read_text(encoding="utf-8"), before)

    def test_uninstall_preserves_private_data_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            layout = self.layout(Path(temp))
            program = application.ProgramLayout.from_layout(layout)
            managed_cli = program.root / "current/.venv/bin/faryo"
            managed_cli.parent.mkdir(parents=True)
            managed_cli.write_text("cli", encoding="utf-8")
            program.bin_path.parent.mkdir(parents=True)
            program.bin_path.symlink_to(managed_cli)
            private = layout.faryo_home / "owner/data/private.txt"
            private.parent.mkdir(parents=True, exist_ok=True)
            private.write_text("private", encoding="utf-8")
            with mock.patch.object(installer, "uninstall_user_services"):
                result = maintenance.uninstall_application(layout)

            self.assertEqual(result, "uninstalled; private data preserved")
            self.assertFalse(program.root.exists())
            self.assertFalse(program.bin_path.exists())
            self.assertEqual(private.read_text(encoding="utf-8"), "private")

    def test_private_data_purge_requires_explicit_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            layout = self.layout(Path(temp))
            with self.assertRaisesRegex(operations.OperationError, "requires --yes"):
                maintenance.uninstall_application(layout, purge_data=True)

    def test_service_uninstall_removes_only_exact_faryo_units(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            layout = self.layout(root)
            xdg = root / "config"
            unit_dir = xdg / "systemd/user"
            unit_dir.mkdir(parents=True)
            expected = [*installer.UNIT_NAMES.values(), *installer.LEGACY_UNIT_NAMES]
            for name in [*expected, "unrelated.service"]:
                (unit_dir / name).write_text(name, encoding="utf-8")
            with (
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}, clear=False),
                mock.patch.object(installer, "systemctl"),
                mock.patch.object(migration, "legacy_owner_exists", return_value=False),
            ):
                removed = installer.uninstall_user_services(layout)

            self.assertEqual(set(removed), set(expected))
            self.assertTrue((unit_dir / "unrelated.service").is_file())

    def release_archive(self, root: Path, version: str = "v1.5.0") -> Path:
        source = root / f"faryo-{version}"
        release = source / "apps/owner/RELEASE"
        release.parent.mkdir(parents=True)
        release.write_text(f"repo=faryo/apps/owner\nversion={version}\nrole=endpoint-runtime\npackage=faryo\n", encoding="utf-8")
        (source / "pyproject.toml").write_text(
            f'[project]\nname = "faryo"\nversion = "{version.removeprefix("v")}"\n',
            encoding="utf-8",
        )
        archive = root / f"faryo-{version}.tar.gz"
        with tarfile.open(archive, "w:gz") as handle:
            handle.add(source, arcname=source.name)
        return archive

    def test_release_checksum_and_metadata_gate_local_update(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = self.release_archive(root)
            checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
            layout = self.layout(root)

            def install(selected, **kwargs):
                self.assertTrue((selected.source_root / "apps/owner/RELEASE").is_file())
                self.assertEqual(kwargs["version"], "v1.5.0")
                return "v1.5.0"

            with mock.patch.object(updates, "install_versioned_application", side_effect=install) as install_mock:
                result = updates.update_application(
                    layout,
                    version="1.5.0",
                    archive=str(archive),
                    checksum=checksum,
                )

            self.assertEqual(result, "v1.5.0")
            install_mock.assert_called_once()

    def test_release_checksum_mismatch_stops_before_install(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = self.release_archive(root)
            layout = self.layout(root)
            with mock.patch.object(updates, "install_versioned_application") as install_mock:
                with self.assertRaisesRegex(operations.OperationError, "checksum mismatch"):
                    updates.update_application(
                        layout,
                        version="v1.5.0",
                        archive=str(archive),
                        checksum="0" * 64,
                    )
            install_mock.assert_not_called()

    def test_checksum_manifest_rejects_paths_and_wrong_asset(self) -> None:
        digest = "a" * 64
        self.assertEqual(updates.parse_checksum(f"{digest}  faryo-v1.5.0.tar.gz\n", "faryo-v1.5.0.tar.gz"), digest)
        for body in (
            f"{digest}  ../faryo-v1.5.0.tar.gz\n",
            f"{digest}  other.tar.gz\n",
            f"{digest}  faryo-v1.5.0.tar.gz\n{digest}  second.tar.gz\n",
        ):
            with self.assertRaisesRegex(operations.OperationError, "manifest"):
                updates.parse_checksum(body, "faryo-v1.5.0.tar.gz")

    def test_safe_extract_rejects_traversal_and_links(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, member in (
                ("traversal.tar", tarfile.TarInfo("../outside")),
                ("link.tar", tarfile.TarInfo("root/link")),
            ):
                archive = root / name
                if name == "link.tar":
                    member.type = tarfile.SYMTYPE
                    member.linkname = "target"
                else:
                    member.size = 1
                with tarfile.open(archive, "w") as handle:
                    handle.addfile(member, io.BytesIO(b"x") if member.size else None)
                with self.assertRaises(operations.OperationError):
                    application.safe_extract(archive, root / f"extract-{name}")

    def test_release_download_allows_only_github_https_hosts(self) -> None:
        self.assertTrue(updates.trusted_release_url("https://github.com/SongJunguo/faryo-codex-web-ui/releases"))
        self.assertTrue(updates.trusted_release_url("https://release-assets.githubusercontent.com/file"))
        self.assertFalse(updates.trusted_release_url("http://github.com/file"))
        self.assertFalse(updates.trusted_release_url("https://github.com.example.invalid/file"))

    def test_default_update_repository_uses_the_standalone_slug(self) -> None:
        self.assertEqual(updates.DEFAULT_REPOSITORY, "SongJunguo/faryo-codex-web-ui")
        archive, _checksum = updates.release_asset_names("v1.5.0")
        self.assertEqual(
            updates.release_asset_url("v1.5.0", archive),
            "https://github.com/SongJunguo/faryo-codex-web-ui/releases/download/v1.5.0/faryo-v1.5.0.tar.gz",
        )


if __name__ == "__main__":
    unittest.main()
