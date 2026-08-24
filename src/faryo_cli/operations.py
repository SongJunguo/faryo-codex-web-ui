"""Controlled service operations behind the public Faryo CLI."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Literal
import webbrowser

from faryo_cli.diagnostics import Layout, http_status, read_env, run_command


ServiceAction = Literal["start", "stop", "restart"]
SERVICE_ACTIONS = {"start", "stop", "restart"}


class OperationError(Exception):
    pass


def runtime_environment() -> dict[str, str]:
    values = dict(os.environ)
    values["FARYO_PYTHON"] = sys.executable
    return values


def systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["systemctl", "--user", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OperationError("systemd user manager is unavailable") from exc
    if check and result.returncode != 0:
        raise OperationError("systemd operation failed")
    return result


def unit_exists(name: str) -> bool:
    return systemctl("cat", name, check=False).returncode == 0


def control_service(name: str, action: ServiceAction) -> None:
    if action not in SERVICE_ACTIONS:
        raise OperationError("unsupported service action")
    systemctl(action, name)


def legacy_script(layout: Layout, name: str) -> Path:
    if layout.source_root is None:
        raise OperationError("legacy Owner requires a source installation")
    path = layout.source_root / "apps/owner/scripts" / name
    if not path.is_file() or not os.access(path, os.X_OK):
        raise OperationError("legacy Owner helper is unavailable")
    return path


def run_legacy_owner(layout: Layout, action: Literal["start", "stop", "restart"]) -> None:
    if action in {"start", "restart"}:
        if unit_exists("faryo-owner-keepalive.timer"):
            control_service("faryo-owner-keepalive.timer", "start")
        helper = legacy_script(layout, "start-web-owner.sh")
    else:
        if unit_exists("faryo-owner-keepalive.timer"):
            control_service("faryo-owner-keepalive.timer", "stop")
        helper = legacy_script(layout, "stop-web-owner.sh")
    try:
        result = subprocess.run(
            [str(helper)],
            cwd=layout.source_root,
            env=runtime_environment(),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OperationError("legacy Owner operation failed") from exc
    if result.returncode != 0:
        raise OperationError("legacy Owner operation failed")


def endpoint(layout: Layout, component: Literal["owner", "gateway"]) -> tuple[str, int, str]:
    values = read_env(layout.owner_env if component == "owner" else layout.gateway_env)
    if component == "owner":
        host = values.get("FARYO_OWNER_HOST") or "127.0.0.1"
        port_value = values.get("FARYO_OWNER_PORT") or "8765"
        path = "/health"
    else:
        host = values.get("GATEWAY_HOST") or "127.0.0.1"
        port_value = values.get("GATEWAY_PORT") or "8780"
        path = "/login"
    try:
        port = int(port_value)
    except ValueError as exc:
        raise OperationError(f"{component} port is invalid") from exc
    return host, port, path


def wait_for_health(layout: Layout, timeout: float = 12.0) -> None:
    targets = [endpoint(layout, "owner"), endpoint(layout, "gateway")]
    deadline = time.monotonic() + timeout
    pending = set(range(len(targets)))
    while pending and time.monotonic() < deadline:
        for index in list(pending):
            host, port, path = targets[index]
            if http_status(host, port, path) == 200:
                pending.discard(index)
        if pending:
            time.sleep(0.1)
    if pending:
        raise OperationError("Faryo services did not become healthy")


def wait_for_appserver(layout: Layout, timeout: float = 12.0) -> None:
    from faryo_cli.runtime import appserver_socket_path

    socket_path = appserver_socket_path(layout, read_env(layout.owner_env))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if socket_path.is_socket():
                return
        except OSError:
            pass
        time.sleep(0.05)
    raise OperationError("Codex App Server did not become ready")


def service_operation(action: ServiceAction, layout: Layout | None = None) -> str:
    from faryo_cli.appserver_workers import listed_worker_units

    selected = layout or Layout.from_environment()
    direct_owner = unit_exists("faryo-owner.service")
    if action == "stop":
        if unit_exists("faryo-gateway.service"):
            control_service("faryo-gateway.service", "stop")
        if direct_owner:
            control_service("faryo-owner.service", "stop")
        else:
            run_legacy_owner(selected, "stop")
        workers = listed_worker_units(systemctl)
        if workers:
            systemctl("stop", *workers, check=False)
        if unit_exists("faryo-appserver.service"):
            control_service("faryo-appserver.service", "stop")
        return "stopped"

    if not unit_exists("faryo-gateway.service"):
        raise OperationError("Gateway service is not installed")
    if unit_exists("faryo-appserver.service"):
        # Owner restarts must not interrupt an active App Server turn.
        control_service("faryo-appserver.service", "start")
        wait_for_appserver(selected)
    if direct_owner:
        control_service("faryo-owner.service", action)
    elif action == "start" and http_status(*endpoint(selected, "owner")) == 200:
        if unit_exists("faryo-owner-keepalive.timer"):
            control_service("faryo-owner-keepalive.timer", "start")
    else:
        run_legacy_owner(selected, action)
    control_service("faryo-gateway.service", action)
    wait_for_health(selected)
    return "started" if action == "start" else "restarted"


def gateway_url(layout: Layout | None = None) -> str:
    selected = layout or Layout.from_environment()
    host, port, _path = endpoint(selected, "gateway")
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise OperationError("Gateway is not configured for loopback")
    display_host = "127.0.0.1" if host == "::1" else host
    return f"http://{display_host}:{port}/"


def open_gateway(layout: Layout | None = None, *, print_only: bool = False) -> str:
    url = gateway_url(layout)
    if not print_only and webbrowser.open(url, new=2):
        return f"opened {url}"
    return url


def journal(component: Literal["appserver", "owner", "gateway"], lines: int = 120) -> str:
    unit = f"faryo-{component}.service"
    if component == "owner" and not unit_exists(unit):
        raise OperationError("Owner journal is unavailable until direct service migration")
    bounded = max(20, min(500, int(lines)))
    try:
        result = run_command(["journalctl", "--user", "-u", unit, "-n", str(bounded), "--no-pager"], timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OperationError("journal is unavailable") from exc
    if result.returncode != 0:
        raise OperationError("journal is unavailable")
    return result.stdout
