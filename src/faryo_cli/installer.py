"""Atomic user-service installation for the unified Faryo CLI."""

from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import tempfile
from typing import Iterable

from faryo_cli.diagnostics import Layout
from faryo_cli.operations import OperationError, systemctl


UNIT_NAMES = {
    "appserver": "faryo-appserver.service",
    "appserver-worker": "faryo-appserver-worker@.service",
    "owner": "faryo-owner.service",
    "gateway": "faryo-gateway.service",
}
ENABLED_UNIT_NAMES = (
    UNIT_NAMES["appserver"],
    UNIT_NAMES["owner"],
    UNIT_NAMES["gateway"],
)
LEGACY_UNIT_NAMES = (
    "faryo-owner-keepalive.service",
    "faryo-owner-keepalive.timer",
)


def unit_escape(value: str) -> str:
    if any(character in value for character in ("\n", "\r", "\x00")):
        raise OperationError("service path contains control characters")
    return value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')


def unit_path_escape(value: str) -> str:
    if not value.startswith("/"):
        raise OperationError("service path must be absolute")
    if any(character in value for character in ("\n", "\r", "\x00")):
        raise OperationError("service path contains control characters")
    return value.replace("%", "%%").replace("\\", "\\x5c").replace(" ", "\\x20").replace("\t", "\\x09")


def rendered_unit(component: str, layout: Layout, python: str) -> str:
    if component not in UNIT_NAMES:
        raise OperationError("unsupported service component")
    if layout.source_root is None:
        raise OperationError("Faryo application files are unavailable")
    template = layout.source_root / "deploy/user-systemd" / UNIT_NAMES[component]
    try:
        source = template.read_text(encoding="utf-8")
    except OSError as exc:
        raise OperationError("service template is unavailable") from exc
    replacements = {
        "@FARYO_ROOT_PATH@": unit_path_escape(str(layout.source_root)),
        "@FARYO_ROOT@": unit_escape(str(layout.source_root)),
        "@FARYO_HOME@": unit_escape(str(layout.faryo_home)),
        "@FARYO_PYTHON@": unit_escape(os.path.abspath(python)),
    }
    for marker, value in replacements.items():
        source = source.replace(marker, value)
    if "@FARYO_" in source:
        raise OperationError("service template has unresolved placeholders")
    return source


def source_supports_worker_units(layout: Layout) -> bool:
    return bool(
        layout.source_root
        and (layout.source_root / "deploy/user-systemd" / UNIT_NAMES["appserver-worker"]).is_file()
    )


def install_components(layout: Layout) -> tuple[str, ...]:
    base = ("appserver", "owner", "gateway")
    return ("appserver", "appserver-worker", "owner", "gateway") if source_supports_worker_units(layout) else base


def appserver_registry_path(layout: Layout) -> Path:
    from faryo_cli.diagnostics import read_env

    values = read_env(layout.owner_env)
    root = (layout.faryo_home / "owner/state").resolve(strict=False)
    configured = str(values.get("FARYO_CODEX_APP_SERVER_REGISTRY") or "").strip()
    path = Path(configured).expanduser() if configured else root / "appserver-sessions.json"
    resolved = path.resolve(strict=False)
    if resolved.parent != root:
        raise OperationError("App Server registry must remain in the private Faryo state directory")
    return resolved


def registry_session_count(path: Path) -> int:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0
    except (OSError, json.JSONDecodeError) as exc:
        raise OperationError("App Server registry is unreadable") from exc
    sessions = value.get("sessions") if isinstance(value, dict) else None
    if not isinstance(sessions, list):
        raise OperationError("App Server registry is invalid")
    return len([item for item in sessions if isinstance(item, dict)])


def active_appserver_session_count(layout: Layout) -> int:
    from faryo_cli.diagnostics import LOOPBACK_HOSTS, read_env

    values = read_env(layout.owner_env)
    host = values.get("FARYO_OWNER_HOST") or "127.0.0.1"
    token = values.get("FARYO_OWNER_TOKEN") or ""
    try:
        port = int(values.get("FARYO_OWNER_PORT") or 8765)
    except ValueError as exc:
        raise OperationError("Owner endpoint is invalid") from exc
    if host not in LOOPBACK_HOSTS or not token or not 1 <= port <= 65535:
        raise OperationError("Owner endpoint is unavailable for App Server migration")
    connection = http.client.HTTPConnection(host, port, timeout=3)
    try:
        connection.request(
            "GET",
            "/api/agent-sessions?view=split&limit=100",
            headers={"X-Owner-Token": token, "Accept": "application/json"},
        )
        response = connection.getresponse()
        body = response.read(4 * 1024 * 1024)
    except OSError as exc:
        raise OperationError("Owner is unavailable for App Server migration") from exc
    finally:
        connection.close()
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OperationError("Owner returned an invalid migration status") from exc
    if response.status != 200 or not isinstance(payload, dict) or not payload.get("ok"):
        raise OperationError("Owner rejected the App Server migration check")
    active = payload.get("activeSessions")
    if not isinstance(active, list):
        raise OperationError("Owner omitted App Server migration status")
    busy_states = {"starting", "running", "pending_interaction"}
    return sum(
        1
        for item in active
        if isinstance(item, dict)
        and str(item.get("backend") or "") == "web-managed"
        and (bool(item.get("agentRunning")) or str(item.get("state") or "") in busy_states)
    )


def require_idle_appserver_transition(layout: Layout) -> None:
    if registry_session_count(appserver_registry_path(layout)) == 0:
        return
    if active_appserver_session_count(layout):
        raise OperationError("App Server sessions are active; wait for them to become idle before upgrading")


def rewrite_registry_schema(path: Path, version: int) -> None:
    if version not in {1, 2}:
        raise OperationError("unsupported App Server registry schema")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, json.JSONDecodeError) as exc:
        raise OperationError("App Server registry is unreadable") from exc
    if not isinstance(value, dict) or value.get("schemaVersion") not in {1, 2}:
        raise OperationError("App Server registry is invalid")
    value["schemaVersion"] = version
    atomic_write(
        path,
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        0o600,
    )


def atomic_write(path: Path, body: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        temp.chmod(mode)
        temp.replace(path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def unit_directory(layout: Layout, values: dict[str, str] | None = None) -> Path:
    env = os.environ if values is None else values
    configured = env.get("XDG_CONFIG_HOME")
    return (Path(configured).expanduser() if configured else layout.home / ".config") / "systemd/user"


def backup_unit(path: Path, layout: Layout) -> None:
    if not path.is_file():
        return
    backup = layout.home / ".local/share/faryo/state/unit-backups" / f"{path.name}.previous"
    try:
        body = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OperationError("existing service unit is unreadable") from exc
    atomic_write(backup, body, 0o600)


def install_user_units(
    layout: Layout | None = None,
    *,
    components: Iterable[str] | None = None,
    python: str,
    reload: bool = True,
) -> list[str]:
    selected = layout or Layout.from_environment()
    target_dir = unit_directory(selected)
    installed: list[str] = []
    for component in components or install_components(selected):
        name = UNIT_NAMES.get(component)
        if not name:
            raise OperationError("unsupported service component")
        target = target_dir / name
        body = rendered_unit(component, selected, python)
        current = target.read_text(encoding="utf-8") if target.is_file() else None
        if current != body:
            backup_unit(target, selected)
            atomic_write(target, body, 0o644)
        installed.append(name)
    if reload:
        systemctl("daemon-reload")
    return installed


def install_services(
    layout: Layout | None = None,
    *,
    python: str,
    dry_run: bool = False,
    no_start: bool = False,
    migrate_owner: bool = False,
) -> str:
    from faryo_cli import migration
    from faryo_cli.appserver_workers import listed_worker_units
    from faryo_cli.operations import control_service, wait_for_health
    from faryo_cli.runtime import appserver_process, gateway_process, owner_process

    selected = layout or Layout.from_environment()
    # Validate application/config/loopback contracts before writing units.
    owner_process(selected)
    gateway_process(selected)
    appserver_process(selected)
    components = install_components(selected)
    for component in components:
        rendered_unit(component, selected, python)
    legacy = migration.legacy_owner_exists()
    if dry_run:
        return "dry-run"
    if legacy and not migrate_owner and not no_start:
        raise OperationError("legacy Owner migration requires --migrate-owner or --no-start")

    target_dir = unit_directory(selected)
    previous = {
        component: (target_dir / name).read_text(encoding="utf-8") if (target_dir / name).is_file() else None
        for component, name in UNIT_NAMES.items()
    }
    registry_path = appserver_registry_path(selected)
    registered_sessions = registry_session_count(registry_path)
    supports_workers = "appserver-worker" in components
    had_worker_template = previous["appserver-worker"] is not None
    topology_upgrade = bool(not legacy and supports_workers and not had_worker_template and registered_sessions)
    topology_downgrade = bool(not legacy and not supports_workers and had_worker_template and registered_sessions)
    topology_transition = topology_upgrade or topology_downgrade
    if topology_transition:
        require_idle_appserver_transition(selected)
    install_user_units(selected, python=python)
    if no_start:
        return "units-installed"
    tmux_before = migration.tmux_process_snapshot()
    try:
        if legacy:
            control_service("faryo-appserver.service", "start")
            migration.migrate_owner(selected)
        elif topology_transition:
            control_service("faryo-owner.service", "stop")
            if topology_downgrade:
                worker_units = listed_worker_units(systemctl)
                if worker_units:
                    systemctl("stop", *worker_units, check=False)
                rewrite_registry_schema(registry_path, 1)
            control_service("faryo-appserver.service", "restart")
            control_service("faryo-owner.service", "start")
        else:
            control_service("faryo-appserver.service", "start")
            control_service("faryo-owner.service", "restart")
        systemctl("enable", *ENABLED_UNIT_NAMES)
        control_service("faryo-gateway.service", "restart")
        wait_for_health(selected)
        migration.verify_process_snapshot(tmux_before, migration.tmux_process_snapshot())
        if not supports_workers:
            worker_template = target_dir / UNIT_NAMES["appserver-worker"]
            if worker_template.exists() or worker_template.is_symlink():
                worker_template.unlink()
                systemctl("daemon-reload")
    except Exception as exc:
        try:
            systemctl("disable", "--now", *ENABLED_UNIT_NAMES, check=False)
            if topology_transition:
                worker_units = listed_worker_units(systemctl)
                if worker_units:
                    systemctl("stop", *worker_units, check=False)
                rewrite_registry_schema(
                    registry_path,
                    2 if had_worker_template else 1,
                )
            for component, name in UNIT_NAMES.items():
                target = target_dir / name
                body = previous[component]
                if body is None:
                    target.unlink(missing_ok=True)
                else:
                    atomic_write(target, body, 0o644)
            systemctl("daemon-reload")
            if legacy and not migration.legacy_owner_exists():
                migration.restore_legacy(selected)
            if previous["appserver"] is not None:
                systemctl("start", "faryo-appserver.service", check=False)
            if not legacy and previous["owner"] is not None:
                systemctl("restart", "faryo-owner.service", check=False)
            if previous["gateway"] is not None:
                systemctl("restart", "faryo-gateway.service", check=False)
        except Exception as rollback_exc:
            raise OperationError("service install and rollback both failed") from rollback_exc
        if isinstance(exc, OperationError):
            raise
        raise OperationError("service install failed") from exc
    return "installed"


def uninstall_user_services(layout: Layout | None = None) -> list[str]:
    from faryo_cli import migration
    from faryo_cli.appserver_workers import listed_worker_units

    selected = layout or Layout.from_environment()
    names = [*UNIT_NAMES.values(), *LEGACY_UNIT_NAMES]
    worker_units = listed_worker_units(systemctl)
    if worker_units:
        systemctl("stop", *worker_units, check=False)
    systemctl("disable", "--now", *names, check=False)
    if migration.legacy_owner_exists():
        migration.stop_legacy_owner()
    target_dir = unit_directory(selected)
    removed: list[str] = []
    for name in names:
        target = target_dir / name
        if target.is_symlink() or target.is_file():
            target.unlink()
            removed.append(name)
    systemctl("daemon-reload")
    systemctl("reset-failed", *names, *worker_units, check=False)
    return removed
