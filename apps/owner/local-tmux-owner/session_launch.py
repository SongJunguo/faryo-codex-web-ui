"""Terminal-managed Codex discovery, launch, resume, and lifecycle service."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from pathlib import Path
from typing import Any

import codex_command_policy
import session_namespace


class SessionLaunchService:
    """Own terminal-session policy behind an injected composition runtime.

    The runtime is the Owner composition root. All cross-boundary calls are
    resolved at use time, so tests and alternate adapters can replace one
    capability without this service importing ASGI or the monolithic server.
    """

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def agent_launch_executable(self, command: str) -> str:
        r = self.runtime
        if command == "codex":
            configured = os.environ.get("FARYO_CODEX_BIN", "").strip()
            resolved = r.codex_runtime.resolve_codex(configured, Path.home())
            if resolved:
                return resolved
            if os.environ.get("FARYO_CODEX_BIN_PINNED", "").strip().lower() in {"1", "true", "yes", "on"}:
                raise r.OwnerError("pinned Codex executable is missing or not executable", HTTPStatus.BAD_GATEWAY)
        executable = shutil.which(command)
        if not executable:
            raise r.OwnerError("Codex executable was not found in the Owner environment", HTTPStatus.BAD_GATEWAY)
        return executable

    def codex_cli_argv(self, *args: str) -> list[str]:
        """Resolve Codex dynamically, then freeze one matching runtime per exec."""
        r = self.runtime
        return r.codex_runtime.codex_argv(r.agent_launch_executable("codex"), *args)

    @staticmethod
    def codex_auto_update_enabled() -> bool:
        return os.environ.get("FARYO_CODEX_AUTO_UPDATE", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def codex_context_window_args(self, context_window_k: int = 0) -> list[str]:
        if not context_window_k:
            return []
        tokens = context_window_k * 1000
        compact_tokens = tokens * self.runtime.CONTEXT_WINDOW_COMPACT_PERCENT // 100
        return [
            "-c",
            f"model_context_window={tokens}",
            "-c",
            f"model_auto_compact_token_limit={compact_tokens}",
        ]

    def managed_codex_launch_argv(self, name: str, *args: str) -> tuple[list[str], bool]:
        r = self.runtime
        if not r.codex_auto_update_enabled():
            return r.codex_cli_argv(*args), False
        launch = r.codex_cli_argv(
            "-c",
            "check_for_update_on_startup=false",
            *args,
        )
        return (
            [
                sys.executable,
                "-I",
                str(r.CODEX_UPDATE_PREFLIGHT),
                "--session",
                name,
                "--state-dir",
                str(r.CODEX_UPDATE_STATE_DIR),
                "--",
                *launch,
            ],
            True,
        )

    def agent_start_ready_timeout(self, config: Any, name: str) -> float:
        r = self.runtime
        update_status = r.tmux_session_option(config, name, "@faryo_codex_update")
        return (
            r.CODEX_UPDATE_START_READY_TIMEOUT
            if update_status in r.CODEX_UPDATE_SESSION_STATES
            else r.AGENT_START_READY_TIMEOUT
        )

    def installed_codex_version(self) -> str:
        r = self.runtime
        try:
            argv = r.codex_cli_argv("--version")
            result = r.run_cmd(
                argv,
                timeout=4,
                environment=r.codex_runtime.codex_environment(argv),
            )
        except (OSError, r.OwnerError, subprocess.TimeoutExpired):
            return ""
        if result.returncode != 0:
            return ""
        match = re.search(r"\b(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?)\b", result.stdout)
        return match.group(1) if match else ""

    def refresh_command_catalog(self) -> None:
        r = self.runtime
        try:
            script = r.APP_DIR / "tests" / "codex-command-inventory.sh"
            environment = r.codex_runtime.codex_environment(r.codex_cli_argv())
            environment["FARYO_CODEX_COMMAND_CATALOG"] = str(codex_command_policy.DEFAULT_RUNTIME_CATALOG)
            result = subprocess.run(
                [str(script), "--write-cache"],
                cwd=str(r.APP_DIR),
                env=environment,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=45,
                check=False,
            )
            if result.returncode == 0:
                codex_command_policy.reload_default_catalog()
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            with r._command_catalog_refresh_lock:
                r._command_catalog_refreshing = False

    def refresh_command_catalog_if_needed(self) -> bool:
        r = self.runtime
        version = r.installed_codex_version()
        catalog = codex_command_policy.default_catalog()
        if not version or version in {
            catalog.tested_codex_version,
            catalog.observed_codex_version,
        }:
            return False
        with r._command_catalog_refresh_lock:
            if r._command_catalog_refreshing:
                return False
            r._command_catalog_refreshing = True
        threading.Thread(
            target=r.refresh_command_catalog,
            name="faryo-command-catalog",
            daemon=True,
        ).start()
        return True

    def agent_login_shell(self) -> str:
        candidates = [
            os.environ.get("FARYO_AGENT_SHELL", "").strip(),
            os.environ.get("SHELL", "").strip(),
            shutil.which("zsh") or "",
            shutil.which("bash") or "",
            shutil.which("sh") or "",
            "/bin/bash",
            "/bin/sh",
        ]
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate).expanduser() if "/" in candidate else Path(shutil.which(candidate) or "")
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
        raise self.runtime.OwnerError(
            "no executable login shell is available",
            HTTPStatus.BAD_GATEWAY,
        )

    def next_faryo_session_name(self, config: Any, reserved_names: Any = ()) -> str:
        r = self.runtime
        reserved = reserved_names() if callable(reserved_names) else reserved_names
        return session_namespace.next_name(r.tmux_sessions(config), reserved or ())

    def managed_launch_session(self, config: Any, launch_id: str) -> str:
        r = self.runtime
        clean_id = r.clean_client_launch_id(launch_id)
        if not clean_id:
            return ""
        for name in r.tmux_sessions(config):
            if r.tmux_session_option(config, name, "@faryo_launch_id") == clean_id and r.managed_session(config, name):
                return name
        return ""

    def wait_for_agent_runtime_ready(
        self,
        config: Any,
        name: str,
        *,
        created_here: bool,
        cleanup_on_failure: bool = True,
        expected_pane_pid: int = 0,
    ) -> str:
        r = self.runtime
        target = r.Config(name, config.token, config.pane_width)
        deadline = time.monotonic() + r.agent_start_ready_timeout(config, name)
        ready_since: float | None = None
        while time.monotonic() < deadline:
            r.reconcile_managed_codex_update(config, name)
            if expected_pane_pid and r.get_pane_pid(target) != expected_pane_pid:
                raise r.OwnerError("agent runtime identity changed", HTTPStatus.CONFLICT)
            if r.has_session(target) and r.codex_cli_in_pane(target):
                pending = r.codex_tui_interactions.detect_interaction(r.tmux_current_capture(target))
                if not created_here or pending is not None:
                    r.tmux_session_option(config, name, "@faryo_start_error", "")
                    r.tmux_session_option(config, name, "@faryo_starting_at", "")
                    r.ensure_pane_width(target)
                    return name
                ready = r.agent_ready_for_input(target, r.CODEX_PROFILE)
                now = time.monotonic()
                if ready:
                    ready_since = now if ready_since is None else ready_since
                    if now - ready_since >= r.AGENT_START_READY_STABLE_SECONDS:
                        r.tmux_session_option(config, name, "@faryo_start_error", "")
                        r.tmux_session_option(config, name, "@faryo_starting_at", "")
                        r.ensure_pane_width(target)
                        return name
                else:
                    ready_since = None
            else:
                ready_since = None
            time.sleep(0.2)
        if created_here and cleanup_on_failure:
            r.tmux(config, ["kill-session", "-t", name], timeout=3)
        raise r.OwnerError("agent runtime did not become ready", HTTPStatus.BAD_GATEWAY)

    def monitor_agent_runtime(self, config: Any, name: str, pane_pid: int) -> None:
        r = self.runtime
        try:
            r.wait_for_agent_runtime_ready(
                config,
                name,
                created_here=True,
                cleanup_on_failure=False,
                expected_pane_pid=pane_pid,
            )
        except r.OwnerError:
            target = r.Config(name, config.token, config.pane_width)
            if r.has_session(target) and r.get_pane_pid(target) == pane_pid:
                r.tmux_session_option(config, name, "@faryo_start_error", "not-ready")
                r.tmux_session_option(config, name, "@faryo_starting_at", "")
        finally:
            with r.AGENT_START_MONITOR_LOCK:
                if r.AGENT_START_MONITORS.get(name) == pane_pid:
                    r.AGENT_START_MONITORS.pop(name, None)

    def ensure_agent_start_monitor(self, config: Any, name: str) -> bool:
        r = self.runtime
        if (
            not r.managed_session(config, name)
            or not r.tmux_session_option(config, name, "@faryo_starting_at")
            or r.tmux_session_option(config, name, "@faryo_start_error")
        ):
            return False
        pane_pid = r.get_pane_pid(r.Config(name, config.token, config.pane_width)) or 0
        if not pane_pid:
            return False
        with r.AGENT_START_MONITOR_LOCK:
            if r.AGENT_START_MONITORS.get(name) == pane_pid:
                return False
            r.AGENT_START_MONITORS[name] = pane_pid
        threading.Thread(
            target=r._monitor_agent_runtime,
            args=(config, name, pane_pid),
            name=f"faryo-start-{name}",
            daemon=True,
        ).start()
        return True

    def start_agent_runtime(
        self,
        config: Any,
        cwd: Path,
        command: str,
        args: list[str],
        max_running: int = 0,
        wait_ready: bool = True,
        agent_id: str = "",
        title: str = "",
        launch_id: str = "",
        context_window_k: int = 0,
        reserved_names: Any = (),
    ) -> str:
        r = self.runtime
        clean_launch_id = r.clean_client_launch_id(launch_id)
        created_here = False
        r.scrub_tmux_global_environment(config)
        with r.RUNTIME_LOCK:
            name = r.managed_launch_session(config, clean_launch_id) if clean_launch_id else ""
            if not name:
                if max_running and r.active_agent_count(config) >= max_running:
                    raise r.OwnerError("running agent limit reached", HTTPStatus.CONFLICT)
                name = r.next_faryo_session_name(config, reserved_names)
                shell = r.agent_login_shell()
                if command == "codex":
                    argv, auto_update = r.managed_codex_launch_argv(
                        name,
                        *r.codex_context_window_args(context_window_k),
                        *args,
                    )
                else:
                    argv, auto_update = [r.agent_launch_executable(command), *args], False
                launch = f"{shlex.join(argv)}; exec {shlex.quote(shell)} -l"
                result = r.tmux(
                    config,
                    ["new-session", "-d", "-s", name, "-c", str(cwd), shell, "-lc", launch],
                    timeout=5,
                )
                if result.returncode != 0:
                    raise r.OwnerError(
                        result.stderr.strip() or "tmux session start failed",
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                created_here = True
                r.tmux_session_option(config, name, "@faryo_managed", "1")
                r.tmux_session_option(config, name, "@faryo_starting_at", str(time.time()))
                r.tmux_session_option(config, name, "@faryo_start_error", "")
                if git_root := r.git_root_for_cwd(cwd):
                    r.tmux_session_option(config, name, r.SESSION_GIT_ROOT_OPTION, git_root)
                if source := r.AGENT_SOURCE_BY_COMMAND.get(command):
                    r.tmux_session_option(config, name, "@faryo_agent_source", source)
                if auto_update and r.tmux_session_option(config, name, "@faryo_codex_update") not in r.CODEX_UPDATE_SESSION_STATES:
                    r.tmux_session_option(config, name, "@faryo_codex_update", "pending")
                if clean_launch_id:
                    r.tmux_session_option(config, name, "@faryo_launch_id", clean_launch_id)
                if context_window_k:
                    r.tmux_session_option(config, name, r.SESSION_CONTEXT_WINDOW_OPTION, str(context_window_k))
                if title:
                    r.tmux_session_option(config, name, "@faryo_session_title", r.clean_session_title(title))
                if agent_id:
                    r.tmux_session_option(config, name, "@faryo_agent_session_id", agent_id)
        if not wait_ready:
            return name
        return r.wait_for_agent_runtime_ready(
            config,
            name,
            created_here=created_here,
            cleanup_on_failure=True,
        )

    def start_agent_runtime_async(
        self,
        config: Any,
        cwd: Path,
        command: str,
        args: list[str],
        max_running: int = 0,
        agent_id: str = "",
        title: str = "",
        launch_id: str = "",
        context_window_k: int = 0,
        reserved_names: Any = (),
    ) -> str:
        r = self.runtime
        name = r.start_agent_runtime(
            config,
            cwd,
            command,
            args,
            max_running,
            wait_ready=False,
            agent_id=agent_id,
            title=title,
            launch_id=launch_id,
            context_window_k=context_window_k,
            reserved_names=reserved_names,
        )
        r.ensure_agent_start_monitor(config, name)
        return name

    def codex_resume_directory_requirement(
        self,
        config: Any,
        thread_id: str,
        history_root: str | None = None,
    ) -> dict[str, Any] | None:
        r = self.runtime
        clean_id = r.clean_agent_session_id(thread_id)
        if not clean_id:
            raise r.OwnerError("invalid agent session id")
        if r.active_codex_thread_map(config).get(clean_id):
            return None
        thread = r.codex_thread_by_id(clean_id)
        if not thread or (
            history_root is not None
            and not r.path_under_root(str(thread.get("cwd") or ""), history_root)
        ):
            raise r.OwnerError("agent session not found", HTTPStatus.NOT_FOUND)
        recorded = Path(str(thread.get("cwd") or "")).expanduser()
        if recorded.is_dir():
            return None
        return {
            "requiresWorkingDirectory": True,
            "reason": "recorded-directory-unavailable",
            "recordedDisplayCwd": r.short_path(str(recorded)) or "Unavailable directory",
        }

    def resume_codex_thread_session(
        self,
        config: Any,
        thread_id: str,
        max_running: int = 0,
        history_root: str | None = None,
        cwd_override: Path | None = None,
        async_ready: bool = False,
        context_window_k: int = 0,
        remote_app_server: bool = False,
        reserved_names: Any = (),
    ) -> str:
        r = self.runtime
        clean_id = r.clean_agent_session_id(thread_id)
        if not clean_id:
            raise r.OwnerError("invalid agent session id")
        with r.RUNTIME_LOCK:
            active = r.active_codex_thread_map(config).get(clean_id)
            if active:
                return active
            thread = r.codex_thread_by_id(clean_id)
            if not thread:
                raise r.OwnerError("agent session not found", HTTPStatus.NOT_FOUND)
            if history_root is not None and not r.path_under_root(str(thread.get("cwd") or ""), history_root):
                raise r.OwnerError("agent session not found", HTTPStatus.NOT_FOUND)
            cwd = cwd_override or Path(str(thread.get("cwd") or "")).expanduser()
            if not cwd.is_dir():
                raise r.OwnerError("working directory selection is required", HTTPStatus.CONFLICT)
            starter = r.start_agent_runtime_async if async_ready else r.start_agent_runtime
            args = ["resume", "-C", str(cwd), clean_id]
            if remote_app_server:
                args = ["--remote", f"unix://{r.APP_SERVER_SOCKET}", *args]
            session = starter(
                config,
                cwd,
                "codex",
                args,
                max_running,
                agent_id=clean_id,
                context_window_k=context_window_k,
                reserved_names=reserved_names,
            )
            if remote_app_server:
                r.tmux_session_option(config, session, "@faryo_codex_remote", "1")
            return session

    def resume_agent_session(
        self,
        config: Any,
        session_id: str,
        source: str,
        max_running: int = 0,
        history_root: str | None = None,
        cwd_override: Path | None = None,
        async_ready: bool = False,
        context_window_k: int = 0,
        remote_app_server: bool = False,
        reserved_names: Any = (),
    ) -> str:
        r = self.runtime
        if source == "codex-cli":
            return r.resume_codex_thread_session(
                config,
                session_id,
                max_running,
                history_root,
                cwd_override,
                async_ready,
                context_window_k,
                remote_app_server,
                reserved_names,
            )
        raise r.OwnerError("unsupported agent source", HTTPStatus.BAD_REQUEST)

    def target_config(self, config: Any, session: str | None) -> Any:
        r = self.runtime
        if not session or session == config.session:
            return config
        sessions = r.tmux_sessions(config)
        if session in sessions:
            return r.Config(session, config.token, config.pane_width)
        active_session = r.active_codex_thread_map(config).get(session, "")
        if active_session in sessions:
            return r.Config(active_session, config.token, config.pane_width)
        raise r.OwnerError(f"tmux session not found: {session}", HTTPStatus.NOT_FOUND)

    def managed_session(self, config: Any, name: str | None) -> bool:
        r = self.runtime
        if not name or name not in r.tmux_sessions(config):
            return False
        return r.tmux_session_option(config, name, "@faryo_managed") == "1"

    def agent_session_lifecycle(
        self,
        config: Any,
        name: str,
        profile: Any = None,
        is_managed: bool | None = None,
        now: float | None = None,
    ) -> tuple[str, bool]:
        r = self.runtime
        target = r.Config(name, config.token, config.pane_width)
        r.reconcile_managed_codex_update(config, name)
        managed = r.managed_session(config, name) if is_managed is None else is_managed
        detected = r.agent_profile_in_pane(target) if profile is None else profile
        if managed and r.tmux_session_option(config, name, "@faryo_start_error"):
            return "exited", False
        try:
            started_at = float(r.tmux_session_option(config, name, "@faryo_starting_at") or 0)
        except ValueError:
            started_at = 0.0
        current = time.time() if now is None else now
        if started_at and current - started_at <= r.agent_start_ready_timeout(config, name) + r.AGENT_START_STATE_GRACE_SECONDS:
            r.ensure_agent_start_monitor(config, name)
            return "starting", False
        if detected is not None:
            running = not r.agent_ready_for_input(target, detected)
            if not managed:
                return "desktop", running
            return ("running" if running else "waiting"), running
        if not managed:
            return "", False
        return "exited", False

    def session_idle_seconds(self, config: Any) -> float:
        r = self.runtime
        result = r.tmux(
            config,
            ["display-message", "-p", "-t", r.tmux_target(config), "#{session_activity}"],
            timeout=2,
        )
        try:
            return max(0.0, time.time() - float(result.stdout.strip())) if result.returncode == 0 else 0.0
        except ValueError:
            return 0.0

    def session_created_ts(self, config: Any) -> float:
        r = self.runtime
        result = r.tmux(
            config,
            ["display-message", "-p", "-t", r.tmux_target(config), "#{session_created}"],
            timeout=2,
        )
        try:
            return float(result.stdout.strip()) if result.returncode == 0 else 0.0
        except ValueError:
            return 0.0

    def iso_from_ts(self, value: float) -> str:
        r = self.runtime
        return r._dt.datetime.fromtimestamp(value, r._dt.timezone.utc).astimezone().isoformat(timespec="seconds") if value else ""

    def cleanup_managed_sessions(self, config: Any, agent_idle_seconds: int = 0) -> None:
        r = self.runtime
        for name in r.tmux_sessions(config):
            target = r.Config(name, config.token, config.pane_width)
            if not r.managed_session(config, name):
                continue
            profile = r.agent_profile_in_pane(target)
            has_agent = profile is not None
            idle = r.session_idle_seconds(target)
            if (
                not has_agent and idle >= r.EMPTY_MANAGED_SESSION_TTL_SECONDS
            ) or (
                agent_idle_seconds
                and profile
                and r.agent_ready_for_input(target, profile)
                and idle >= agent_idle_seconds
            ):
                r.tmux(config, ["kill-session", "-t", name], timeout=3)

    def active_agent_count(self, config: Any) -> int:
        r = self.runtime
        r.cleanup_managed_sessions(config)
        count = 0
        now = time.time()
        for name in r.tmux_sessions(config):
            target = r.Config(name, config.token, config.pane_width)
            profile = r.agent_profile_in_pane(target)
            if profile is not None:
                count += 1
                continue
            if r.managed_session(config, name) and r.agent_session_lifecycle(config, name, None, True, now)[0] == "starting":
                count += 1
        return count

    @staticmethod
    def bounded_max_running(payload: dict[str, Any]) -> int:
        return int(payload.get("max_running") or payload.get("maxRunning") or 0)

    def bounded_context_window_k(self, payload: dict[str, Any]) -> int:
        r = self.runtime
        value = payload.get("context_window_k")
        if value is None:
            value = payload.get("contextWindowK")
        raw = str(value if value is not None else "").strip()
        if not raw or raw == "0":
            return 0
        if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,4}", raw):
            raise r.OwnerError(
                f"context window must be a whole number from {r.CONTEXT_WINDOW_MIN_K} to {r.CONTEXT_WINDOW_MAX_K} K"
            )
        context_window_k = int(raw)
        if not r.CONTEXT_WINDOW_MIN_K <= context_window_k <= r.CONTEXT_WINDOW_MAX_K:
            raise r.OwnerError(
                f"context window must be a whole number from {r.CONTEXT_WINDOW_MIN_K} to {r.CONTEXT_WINDOW_MAX_K} K"
            )
        return context_window_k

    def close_shell_session(self, config: Any, session: str | None) -> None:
        r = self.runtime
        name = r.clean_tmux_session_name(session)
        if not r.managed_session(config, name):
            raise r.OwnerError("tmux session not found", HTTPStatus.NOT_FOUND)
        result = r.tmux(config, ["kill-session", "-t", name], timeout=3)
        if result.returncode != 0:
            raise r.OwnerError(
                result.stderr.strip() or "tmux kill-session failed",
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def clean_agent_launch_command(self, value: str | None) -> str | None:
        command = Path(str(value or "").strip()).name.lower()
        return command if command in self.runtime.AGENT_LAUNCH_COMMANDS else None
