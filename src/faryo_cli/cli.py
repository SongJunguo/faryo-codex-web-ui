"""Command parser and presentation for the unified Faryo CLI."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from faryo_cli import __version__
from faryo_cli.application import install_versioned_application
from faryo_cli.diagnostics import Layout, build_report, compact_status, read_env
from faryo_cli.maintenance import rollback_application, uninstall_application
from faryo_cli.operations import OperationError, journal, open_gateway, service_operation
from faryo_cli.runtime import (
    appserver_process,
    appserver_worker_process,
    appserver_socket_path,
    exec_process,
    gateway_process,
    owner_process,
    prepare_appserver_runtime,
)
from faryo_cli.updates import update_application


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="faryo", description="Manage the local Faryo App Server, Owner, and Gateway")
    root.add_argument("--version", action="version", version=f"Faryo {__version__}")
    commands = root.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for name, help_text in (
        ("doctor", "Check runtime, configuration, services, and loopback health"),
        ("status", "Show a compact read-only service summary"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--json", action="store_true", help="Print privacy-safe machine-readable JSON")
    for name, help_text in (
        ("start", "Start App Server, Owner, and Gateway"),
        ("stop", "Stop Faryo services without stopping Codex tmux sessions"),
        ("restart", "Keep App Server alive; restart Owner and Gateway, then wait for health"),
    ):
        commands.add_parser(name, help=help_text)
    open_command = commands.add_parser("open", help="Open the local Gateway")
    open_command.add_argument("--print", action="store_true", dest="print_only", help="Print the URL without opening a browser")
    logs = commands.add_parser("logs", help="Show bounded systemd journal output")
    logs.add_argument("component", choices=("appserver", "owner", "gateway"))
    logs.add_argument("--lines", type=int, default=120)
    install = commands.add_parser("install", help="Install user services from the current verified application")
    install.add_argument("--dry-run", action="store_true", help="Validate without writing service units")
    install.add_argument("--no-start", action="store_true", help="Install units without changing running services")
    install.add_argument("--migrate-owner", action="store_true", help="Replace legacy Owner tmux supervision after rollback checks")
    install.add_argument("--python", dest="bootstrap_python", help="Python 3.10+ interpreter used to create the private venv")
    install.add_argument("--workspace", help="Initial allowed workspace directory for a fresh private config")
    update = commands.add_parser("update", help="Install a verified release and switch after health checks")
    update.add_argument("--version", help="Exact release tag; defaults to the latest stable GitHub release")
    update.add_argument("--archive", help="Use a local release archive instead of downloading")
    update.add_argument("--checksum", help="SHA-256 value or checksum file for --archive")
    update.add_argument("--python", dest="bootstrap_python", help="Python 3.10+ interpreter used for the new private venv")
    commands.add_parser("rollback", help="Switch back to the previously active healthy version")
    uninstall = commands.add_parser("uninstall", help="Remove Faryo services and program files while preserving private data")
    uninstall.add_argument("--purge-data", action="store_true", help="Also remove private Faryo config, history cache, and attachments")
    uninstall.add_argument("--yes", action="store_true", help="Confirm irreversible private-data deletion")
    internal = commands.add_parser("internal")
    commands._choices_actions = [action for action in commands._choices_actions if action.dest != "internal"]
    internal_commands = internal.add_subparsers(dest="internal_command", required=True)
    internal_commands.add_parser("run-owner")
    internal_commands.add_parser("run-gateway")
    internal_commands.add_parser("run-appserver")
    worker = internal_commands.add_parser("run-appserver-worker")
    worker.add_argument("worker_id")
    return root


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def print_doctor(report: dict[str, Any]) -> None:
    labels = {"ok": "OK", "warn": "WARN", "error": "FAIL"}
    for item in report["checks"]:
        print(f"{labels[item['status']]:<4} {item['id']:<20} {item['detail']}")
    counts = report["counts"]
    print(f"\nFaryo doctor: {counts['ok']} ok, {counts['warn']} warning, {counts['error']} failed")


def print_status(status: dict[str, Any]) -> None:
    print(f"Owner:  {status['owner']['service']} · health {status['owner']['health']}")
    print(f"Gateway: {status['gateway']['service']} · health {status['gateway']['health']}")
    print(f"App Server: {status['appserver']['service']} · socket {status['appserver']['socket']}")
    print(f"tmux sessions: {status['tmuxSessions']}")
    if status["legacyOwner"]:
        print("Migration: legacy Owner tmux/keepalive is still active")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if arguments.command == "internal":
        try:
            if arguments.internal_command == "run-owner":
                spec = owner_process()
            elif arguments.internal_command == "run-gateway":
                spec = gateway_process()
            elif arguments.internal_command == "run-appserver-worker":
                spec = appserver_worker_process(arguments.worker_id)
            else:
                layout = Layout.from_environment()
                spec = appserver_process(layout)
                prepare_appserver_runtime(layout, appserver_socket_path(layout, read_env(layout.owner_env)))
            exec_process(spec)
        except OperationError as exc:
            print(f"Faryo service failed: {exc}", file=sys.stderr)
            return 1
        return 0
    if arguments.command == "install":
        try:
            result = install_versioned_application(
                bootstrap_python=arguments.bootstrap_python,
                dry_run=arguments.dry_run,
                no_start=arguments.no_start,
                migrate_owner=arguments.migrate_owner,
                workspace=arguments.workspace,
            )
        except OperationError as exc:
            print(f"Faryo install failed: {exc}", file=sys.stderr)
            return 1
        print(f"Faryo install: {result}")
        return 0
    if arguments.command == "rollback":
        try:
            result = rollback_application()
        except OperationError as exc:
            print(f"Faryo rollback failed: {exc}", file=sys.stderr)
            return 1
        print(f"Faryo rollback: {result}")
        return 0
    if arguments.command == "update":
        try:
            result = update_application(
                version=arguments.version,
                archive=arguments.archive,
                checksum=arguments.checksum,
                bootstrap_python=arguments.bootstrap_python,
            )
        except OperationError as exc:
            print(f"Faryo update failed: {exc}", file=sys.stderr)
            return 1
        print(f"Faryo update: {result}")
        return 0
    if arguments.command == "uninstall":
        try:
            result = uninstall_application(purge_data=arguments.purge_data, confirmed=arguments.yes)
        except OperationError as exc:
            print(f"Faryo uninstall failed: {exc}", file=sys.stderr)
            return 1
        print(f"Faryo {result}")
        return 0
    if arguments.command in {"start", "stop", "restart"}:
        try:
            result = service_operation(arguments.command)
        except OperationError as exc:
            print(f"Faryo {arguments.command} failed: {exc}", file=sys.stderr)
            return 1
        print(f"Faryo {result}")
        return 0
    if arguments.command == "open":
        try:
            print(open_gateway(print_only=arguments.print_only))
        except OperationError as exc:
            print(f"Faryo open failed: {exc}", file=sys.stderr)
            return 1
        return 0
    if arguments.command == "logs":
        try:
            print(journal(arguments.component, arguments.lines), end="")
        except OperationError as exc:
            print(f"Faryo logs failed: {exc}", file=sys.stderr)
            return 1
        return 0
    report = build_report()
    if arguments.command == "doctor":
        if arguments.json:
            print_json(report)
        else:
            print_doctor(report)
        return 0 if report["ok"] else 1
    status = compact_status(report)
    if arguments.json:
        print_json(status)
    else:
        print_status(status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
