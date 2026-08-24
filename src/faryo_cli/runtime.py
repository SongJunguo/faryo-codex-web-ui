"""Direct Owner and Gateway process specifications for systemd services."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Mapping

from faryo_cli import codex_runtime
from faryo_cli import appserver_workers
from faryo_cli.diagnostics import LOOPBACK_HOSTS, Layout, private_file_state, read_env
from faryo_cli.operations import OperationError


@dataclass(frozen=True)
class ProcessSpec:
    argv: list[str]
    cwd: Path
    environment: dict[str, str]


def append_no_proxy(values: dict[str, str], entry: str) -> None:
    current = values.get("NO_PROXY") or values.get("no_proxy") or ""
    parts = [item.strip() for item in current.split(",") if item.strip()]
    if entry not in parts:
        parts.append(entry)
    values["NO_PROXY"] = ",".join(parts)
    values["no_proxy"] = values["NO_PROXY"]


def normalized_environment(config: Mapping[str, str], home: Path) -> dict[str, str]:
    values = dict(os.environ)
    values.update({str(key): str(value) for key, value in config.items()})
    for name in ("NO_COLOR", "CODEX_THREAD_ID", "CODEX_CI", "CODEX_SANDBOX_NETWORK_DISABLED"):
        values.pop(name, None)
    values.update({
        "FARYO_PYTHON": sys.executable,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "LC_CTYPE": "C.UTF-8",
        "COLORTERM": "truecolor",
        "TERM": values.get("TERM") if values.get("TERM") not in {None, "", "dumb"} else "xterm-256color",
    })
    prefixes = [home / ".local/share/npm-global/bin", home / ".local/bin", Path("/usr/local/bin")]
    current_path = values.get("PATH") or "/usr/bin:/bin"
    values["PATH"] = ":".join([*(str(path) for path in prefixes), current_path])
    for entry in ("localhost", "127.0.0.1", "::1", "*.local", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
        append_no_proxy(values, entry)
    return values


def require_private_config(path: Path, label: str) -> dict[str, str]:
    state = private_file_state(path)
    if state != "ok":
        raise OperationError(f"{label} config is {state}")
    values = read_env(path)
    if not values:
        raise OperationError(f"{label} config is empty")
    return values


def loopback(values: Mapping[str, str], key: str, default: str) -> str:
    host = str(values.get(key) or default)
    if host not in LOOPBACK_HOSTS:
        raise OperationError(f"{key} must remain loopback")
    return host


def port(values: Mapping[str, str], key: str, default: int) -> int:
    try:
        result = int(values.get(key) or default)
    except (TypeError, ValueError) as exc:
        raise OperationError(f"{key} is invalid") from exc
    if not 1 <= result <= 65535:
        raise OperationError(f"{key} is invalid")
    return result


def owner_process(layout: Layout | None = None) -> ProcessSpec:
    selected = layout or Layout.from_environment()
    if selected.source_root is None:
        raise OperationError("Faryo application files are unavailable")
    values = require_private_config(selected.owner_env, "Owner")
    token = values.get("FARYO_OWNER_TOKEN") or ""
    if not token:
        raise OperationError("Owner token is missing")
    host = loopback(values, "FARYO_OWNER_HOST", "127.0.0.1")
    owner_port = port(values, "FARYO_OWNER_PORT", 8765)
    runner = selected.source_root / "apps/owner/local-tmux-owner/run_owner_asgi.py"
    if not runner.is_file():
        raise OperationError("Owner application is unavailable")
    environment = normalized_environment(values, selected.home)
    environment["FARYO_HOME"] = str(selected.faryo_home)
    return ProcessSpec(
        argv=[
            sys.executable,
            str(runner),
            "--session",
            values.get("FARYO_OWNER_DIRECT_SESSION") or "__faryo_no_default__",
            "--host",
            host,
            "--port",
            str(owner_port),
        ],
        cwd=runner.parent,
        environment=environment,
    )


def gateway_process(layout: Layout | None = None) -> ProcessSpec:
    selected = layout or Layout.from_environment()
    if selected.source_root is None:
        raise OperationError("Faryo application files are unavailable")
    values = require_private_config(selected.gateway_env, "Gateway")
    if private_file_state(selected.gateway_auth) != "ok":
        raise OperationError("Gateway auth config is unavailable or unsafe")
    host = loopback(values, "GATEWAY_HOST", "127.0.0.1")
    gateway_port = port(values, "GATEWAY_PORT", 8780)
    runner = selected.source_root / "apps/gateway/server/run_asgi.py"
    gateway_env = selected.gateway_env
    portal_dir = selected.faryo_home / "gateway/portal"
    secret_file = selected.faryo_home / "gateway/state/gateway-cookie-secret"
    if not runner.is_file() or private_file_state(gateway_env) != "ok":
        raise OperationError("Gateway application inputs are unavailable")
    environment = normalized_environment(values, selected.home)
    environment["FARYO_HOME"] = str(selected.faryo_home)
    environment["FARYO_GATEWAY_SESSION_HOURS"] = values.get("FARYO_GATEWAY_SESSION_HOURS") or "720"
    return ProcessSpec(
        argv=[
            sys.executable,
            str(runner),
            "--host",
            host,
            "--port",
            str(gateway_port),
            "--auth-config",
            str(selected.gateway_auth),
            "--owner-env",
            str(gateway_env),
            "--portal-dir",
            str(portal_dir),
            "--secret-file",
            str(secret_file),
        ],
        cwd=runner.parent,
        environment=environment,
    )


def appserver_socket_path(layout: Layout, values: Mapping[str, str]) -> Path:
    root = (layout.faryo_home / "owner/runtime").resolve(strict=False)
    configured = str(values.get("FARYO_CODEX_APP_SERVER_SOCKET") or "").strip()
    path = Path(configured).expanduser() if configured else root / "codex-app-server.sock"
    if not path.is_absolute():
        raise OperationError("App Server socket path must be absolute")
    resolved = path.resolve(strict=False)
    if resolved.parent != root:
        raise OperationError("App Server socket must remain in the private Faryo runtime directory")
    return resolved


def prepare_appserver_runtime(layout: Layout, socket_path: Path) -> None:
    expected = (layout.faryo_home / "owner/runtime").resolve(strict=False)
    if socket_path.parent != expected:
        raise OperationError("App Server runtime path is invalid")
    try:
        expected.mkdir(parents=True, exist_ok=True, mode=0o700)
        expected.chmod(0o700)
    except OSError as exc:
        raise OperationError("App Server runtime directory is unavailable") from exc


def appserver_process(layout: Layout | None = None) -> ProcessSpec:
    selected = layout or Layout.from_environment()
    values = require_private_config(selected.owner_env, "Owner")
    environment_values = dict(os.environ)
    environment_values.update(values)
    executable = codex_runtime.resolve_codex(
        values.get("FARYO_CODEX_BIN") or "",
        selected.home,
        environment_values,
    )
    if not executable:
        raise OperationError("Codex CLI is unavailable")
    socket_path = appserver_socket_path(selected, values)
    argv = codex_runtime.codex_argv(
        executable,
        "app-server",
        "--listen",
        f"unix://{socket_path}",
    )
    environment = codex_runtime.codex_environment(
        argv,
        normalized_environment(values, selected.home),
    )
    return ProcessSpec(argv=argv, cwd=selected.home, environment=environment)


def appserver_worker_process(worker_id: str, layout: Layout | None = None) -> ProcessSpec:
    selected_worker = appserver_workers.validate_worker_id(worker_id)
    selected = layout or Layout.from_environment()
    values = require_private_config(selected.owner_env, "Owner")
    environment_values = dict(os.environ)
    environment_values.update(values)
    executable = codex_runtime.resolve_codex(
        values.get("FARYO_CODEX_BIN") or "",
        selected.home,
        environment_values,
    )
    if not executable:
        raise OperationError("Codex CLI is unavailable")
    appserver_workers.prepare_worker_runtime(selected)
    socket_path = appserver_workers.worker_socket_path(selected, selected_worker)
    argv = codex_runtime.codex_argv(
        executable,
        "app-server",
        "--listen",
        f"unix://{socket_path}",
    )
    environment = codex_runtime.codex_environment(
        argv,
        normalized_environment(values, selected.home),
    )
    return ProcessSpec(argv=argv, cwd=selected.home, environment=environment)


def exec_process(spec: ProcessSpec) -> None:
    os.chdir(spec.cwd)
    os.execve(spec.argv[0], spec.argv, spec.environment)
