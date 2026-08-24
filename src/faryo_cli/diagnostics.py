"""Privacy-safe, read-only deployment diagnostics for the Faryo CLI."""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
from typing import Any, Mapping

from . import codex_runtime


SCHEMA_VERSION = 1
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
WORKER_UNIT_RE = re.compile(r"^faryo-appserver-worker@[a-f0-9]{24}\.service$")


@dataclass(frozen=True)
class Layout:
    home: Path
    faryo_home: Path
    owner_env: Path
    gateway_env: Path
    gateway_auth: Path
    source_root: Path | None

    @classmethod
    def from_environment(cls, values: Mapping[str, str] | None = None) -> "Layout":
        env = dict(os.environ if values is None else values)
        home = Path(env.get("HOME") or str(Path.home())).expanduser()
        faryo_home = Path(env.get("FARYO_HOME") or home / ".faryo").expanduser()
        source_root = discover_source_root(env)
        return cls(
            home=home,
            faryo_home=faryo_home,
            owner_env=Path(env.get("FARYO_OWNER_ENV") or faryo_home / "owner/config/faryo.env").expanduser(),
            gateway_env=Path(env.get("FARYO_GATEWAY_ENV") or faryo_home / "gateway/config/faryo.env").expanduser(),
            gateway_auth=Path(env.get("GATEWAY_AUTH_CONFIG") or faryo_home / "gateway/config/gateway-auth.json").expanduser(),
            source_root=source_root,
        )


def discover_source_root(values: Mapping[str, str] | None = None, *, module_file: Path | None = None) -> Path | None:
    env = dict(os.environ if values is None else values)
    explicit = env.get("FARYO_INSTALL_ROOT") or env.get("FARYO_ROOT")
    if explicit:
        candidates = [Path(explicit).expanduser()]
    else:
        home = Path(env.get("HOME") or str(Path.home())).expanduser()
        program = Path(env.get("FARYO_PROGRAM_HOME") or home / ".local/share/faryo").expanduser()
        candidates = [*(module_file or Path(__file__)).resolve().parents, program / "current/app"]
    for candidate in candidates:
        if (
            (candidate / "apps/owner/local-tmux-owner/server.py").is_file()
            and (candidate / "apps/owner/local-tmux-owner/run_owner_asgi.py").is_file()
            and (candidate / "apps/gateway/server/run_asgi.py").is_file()
        ):
            return candidate
    return None


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw = stripped.split("=", 1)
        try:
            parsed = shlex.split(raw, posix=True)
        except ValueError:
            parsed = []
        values[key] = parsed[0] if parsed else raw.strip().strip("'\"")
    return values


def private_file_state(path: Path) -> str:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return "missing"
    return "ok" if mode & 0o077 == 0 else "unsafe"


def run_command(argv: list[str], timeout: float = 3.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout)


def command_version(command: str, *args: str, configured: str = "") -> str | None:
    executable = ""
    if configured:
        executable = str(Path(configured).expanduser()) if "/" in configured else (shutil.which(configured) or "")
    if not executable:
        executable = shutil.which(command) or ""
    if not executable:
        return None
    return argv_version([executable, *args])


def argv_version(argv: list[str]) -> str | None:
    try:
        result = run_command(argv)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (result.stdout or result.stderr).strip().splitlines()
    return text[0][:96] if result.returncode == 0 and text else None


def resolve_codex(configured: str, home: Path, *, pinned: bool | None = None) -> str:
    values = dict(os.environ)
    if pinned is not None:
        values["FARYO_CODEX_BIN_PINNED"] = "1" if pinned else "0"
    return codex_runtime.resolve_codex(configured, home, values)


def codex_argv(executable: str, *args: str) -> list[str]:
    return codex_runtime.codex_argv(executable, *args)


def service_state(name: str) -> str:
    executable = shutil.which("systemctl")
    if not executable:
        return "unavailable"
    try:
        result = run_command([executable, "--user", "is-active", name])
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    value = result.stdout.strip()
    return value if value in {"active", "activating", "deactivating", "failed", "inactive"} else "unknown"


def systemd_user_available() -> bool:
    executable = shutil.which("systemctl")
    if not executable:
        return False
    try:
        return run_command([executable, "--user", "show-environment"]).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def appserver_worker_service_counts() -> tuple[int, int, int]:
    executable = shutil.which("systemctl")
    if not executable:
        return (0, 0, 0)
    try:
        result = run_command(
            [
                executable,
                "--user",
                "list-units",
                "--all",
                "--plain",
                "--no-legend",
                "--type=service",
                "faryo-appserver-worker@*.service",
            ],
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return (0, 0, 0)
    if result.returncode != 0:
        return (0, 0, 0)
    total = active = failed = 0
    for line in result.stdout.splitlines():
        parts = line.split(maxsplit=4)
        if len(parts) < 4 or not WORKER_UNIT_RE.fullmatch(parts[0]):
            continue
        total += 1
        active += int(parts[2] == "active")
        failed += int(parts[2] == "failed" or parts[3] == "failed")
    return total, active, failed


def tmux_session_exists(name: str) -> bool:
    executable = shutil.which("tmux")
    if not executable:
        return False
    try:
        return run_command([executable, "has-session", "-t", name], timeout=2).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def tmux_session_count() -> int:
    executable = shutil.which("tmux")
    if not executable:
        return 0
    try:
        result = run_command([executable, "list-sessions", "-F", "#{session_name}"], timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return 0
    return len([line for line in result.stdout.splitlines() if line.strip()]) if result.returncode == 0 else 0


def http_status(host: str, port: int, path: str) -> int | None:
    if host not in LOOPBACK_HOSTS or not 1 <= port <= 65535:
        return None
    connection = http.client.HTTPConnection(host, port, timeout=2)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        response.read(4096)
        return response.status
    except OSError:
        return None
    finally:
        connection.close()


def check(identifier: str, status_value: str, detail: str) -> dict[str, str]:
    return {"id": identifier, "status": status_value, "detail": detail}


def python_environment_kind() -> str:
    if os.environ.get("CONDA_PREFIX") or (Path(sys.prefix) / "conda-meta").is_dir():
        return "conda"
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        return "venv"
    return "system"


def configured_python_state(owner: Mapping[str, str], gateway: Mapping[str, str]) -> str:
    configured = owner.get("FARYO_PYTHON") or gateway.get("FARYO_PYTHON")
    if not configured:
        return "missing"
    executable = Path(configured).expanduser() if "/" in configured else Path(shutil.which(configured) or "")
    return "ok" if executable.is_file() and os.access(executable, os.X_OK) else "invalid"


def build_report(layout: Layout | None = None) -> dict[str, Any]:
    selected = layout or Layout.from_environment()
    owner = read_env(selected.owner_env)
    gateway = read_env(selected.gateway_env)
    checks: list[dict[str, str]] = []

    python_ok = sys.version_info >= (3, 10)
    checks.append(check("python", "ok" if python_ok else "error", f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"))
    try:
        venv_available = importlib.util.find_spec("venv") is not None
    except (ImportError, ValueError):
        venv_available = False
    checks.append(check("venv", "ok" if venv_available else "error", "standard venv available" if venv_available else "python venv module missing"))
    environment_kind = python_environment_kind()
    checks.append(check("environment", "ok" if environment_kind in {"conda", "venv"} else "warn", environment_kind))

    tmux_version = command_version("tmux", "-V")
    checks.append(check("tmux", "ok" if tmux_version else "error", tmux_version or "not found"))
    pinned_codex = (owner.get("FARYO_CODEX_BIN_PINNED") or "0").strip().lower() in {"1", "true", "yes", "on"}
    codex_path = resolve_codex(
        owner.get("FARYO_CODEX_BIN") or "",
        selected.home,
        pinned=pinned_codex,
    )
    codex_version = argv_version(codex_argv(codex_path, "--version")) if codex_path else None
    checks.append(check("codex", "ok" if codex_version else "error", codex_version or "not found or not on PATH"))
    checks.append(check("codex-discovery", "ok", "pinned override" if pinned_codex else "dynamic per launch"))
    auto_update = (owner.get("FARYO_CODEX_AUTO_UPDATE") or "1").strip().lower() in {"1", "true", "yes", "on"}
    update_result = "not checked"
    try:
        update_state = json.loads((selected.faryo_home / "owner/state/codex-auto-update.json").read_text(encoding="utf-8"))
        if isinstance(update_state, dict) and update_state.get("result") in {"current", "updated", "failed"}:
            update_result = str(update_state["result"])
    except (OSError, json.JSONDecodeError):
        pass
    checks.append(check("codex-auto-update", "ok", f"{'enabled' if auto_update else 'disabled'}; {update_result}"))
    checks.append(check("curl", "ok" if shutil.which("curl") else "error", "available" if shutil.which("curl") else "not found"))
    missing_modules = [name for name in ("anyio", "bcrypt", "starlette", "uvicorn", "websockets") if importlib.util.find_spec(name) is None]
    checks.append(check("runtime-dependencies", "error" if missing_modules else "ok", "missing runtime packages" if missing_modules else "available"))
    systemd_available = systemd_user_available()
    checks.append(check("systemd-user", "ok" if systemd_available else "error", "available" if systemd_available else "unavailable"))

    owner_config_state = private_file_state(selected.owner_env)
    gateway_config_state = private_file_state(selected.gateway_env)
    auth_state = private_file_state(selected.gateway_auth)
    checks.append(check("owner-config", "ok" if owner_config_state == "ok" else "error", owner_config_state))
    checks.append(check("gateway-config", "ok" if gateway_config_state == "ok" else "error", gateway_config_state))
    checks.append(check("gateway-auth", "ok" if auth_state == "ok" else "error", auth_state))
    runtime_state = configured_python_state(owner, gateway)
    checks.append(check("configured-python", "ok" if runtime_state == "ok" else "error", runtime_state))

    owner_host = owner.get("FARYO_OWNER_HOST") or "127.0.0.1"
    try:
        owner_port = int(owner.get("FARYO_OWNER_PORT") or 8765)
    except ValueError:
        owner_port = 0
    owner_loopback = owner_host in LOOPBACK_HOSTS
    checks.append(check("owner-bind", "ok" if owner_loopback else "error", "loopback" if owner_loopback else "not loopback"))
    owner_http = http_status(owner_host, owner_port, "/health")
    checks.append(check("owner-health", "ok" if owner_http == 200 else "error", "healthy" if owner_http == 200 else "unavailable"))

    gateway_host = gateway.get("GATEWAY_HOST") or "127.0.0.1"
    try:
        gateway_port = int(gateway.get("GATEWAY_PORT") or 8780)
    except ValueError:
        gateway_port = 0
    gateway_loopback = gateway_host in LOOPBACK_HOSTS
    checks.append(check("gateway-bind", "ok" if gateway_loopback else "error", "loopback" if gateway_loopback else "not loopback"))
    gateway_http = http_status(gateway_host, gateway_port, "/login")
    checks.append(check("gateway-health", "ok" if gateway_http == 200 else "error", "healthy" if gateway_http == 200 else "unavailable"))

    try:
        session_hours = int(gateway.get("FARYO_GATEWAY_SESSION_HOURS") or 720)
    except ValueError:
        session_hours = 0
    checks.append(check("gateway-session", "ok" if 1 <= session_hours <= 720 else "error", "valid" if 1 <= session_hours <= 720 else "invalid"))

    gateway_service = service_state("faryo-gateway.service")
    owner_service = service_state("faryo-owner.service")
    appserver_service = service_state("faryo-appserver.service")
    worker_total, worker_active, worker_failed = appserver_worker_service_counts()
    keepalive = service_state("faryo-owner-keepalive.timer")
    legacy_tmux = tmux_session_exists("local-tmux-owner")
    checks.append(check("gateway-service", "ok" if gateway_service == "active" else "warn", gateway_service))
    checks.append(check("owner-service", "ok" if owner_service == "active" else "warn", owner_service))
    socket_path = selected.faryo_home / "owner/runtime/codex-app-server.sock"
    try:
        appserver_socket_ready = socket_path.is_socket()
    except OSError:
        appserver_socket_ready = False
    checks.append(check("appserver-service", "ok" if appserver_service == "active" else "warn", appserver_service))
    checks.append(check("appserver-socket", "ok" if appserver_socket_ready else "warn", "ready" if appserver_socket_ready else "unavailable"))
    worker_status = "warn" if worker_failed or worker_active != worker_total else "ok"
    checks.append(check("appserver-workers", worker_status, f"{worker_active} active, {worker_total} registered"))
    checks.append(check("legacy-owner", "warn" if legacy_tmux or keepalive == "active" else "ok", "legacy supervision active" if legacy_tmux or keepalive == "active" else "retired"))

    status_counts = {value: sum(1 for item in checks if item["status"] == value) for value in ("ok", "warn", "error")}
    return {
        "schemaVersion": SCHEMA_VERSION,
        "ok": status_counts["error"] == 0,
        "checks": checks,
        "counts": status_counts,
        "services": {"appserver": appserver_service, "owner": owner_service, "gateway": gateway_service, "legacyKeepalive": keepalive},
        "runtime": {
            "appServerWorkers": {"active": worker_active, "failed": worker_failed, "total": worker_total},
            "environment": environment_kind,
            "tmuxSessions": tmux_session_count(),
        },
    }


def compact_status(report: Mapping[str, Any]) -> dict[str, Any]:
    checks = {str(item.get("id")): item for item in report.get("checks") or [] if isinstance(item, dict)}
    return {
        "schemaVersion": report.get("schemaVersion", SCHEMA_VERSION),
        "ok": bool(report.get("ok")),
        "owner": {"service": (report.get("services") or {}).get("owner"), "health": (checks.get("owner-health") or {}).get("status")},
        "gateway": {"service": (report.get("services") or {}).get("gateway"), "health": (checks.get("gateway-health") or {}).get("status")},
        "appserver": {
            "service": (report.get("services") or {}).get("appserver"),
            "socket": (checks.get("appserver-socket") or {}).get("status"),
            "workers": dict((report.get("runtime") or {}).get("appServerWorkers") or {}),
        },
        "legacyOwner": (checks.get("legacy-owner") or {}).get("status") == "warn",
        "tmuxSessions": int((report.get("runtime") or {}).get("tmuxSessions") or 0),
    }
