"""Validated systemd and socket identities for per-session App Server workers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import time
from typing import Any, Callable

from faryo_cli.diagnostics import Layout
from faryo_cli.operations import OperationError


WORKER_ID_RE = re.compile(r"^[a-f0-9]{24}$")
WORKER_UNIT_RE = re.compile(r"^faryo-appserver-worker@([a-f0-9]{24})\.service$")
WORKER_UNIT_TEMPLATE = "faryo-appserver-worker@.service"


def validate_worker_id(value: str) -> str:
    selected = str(value or "").strip()
    if not WORKER_ID_RE.fullmatch(selected):
        raise OperationError("App Server worker id is invalid")
    return selected


def worker_unit_name(worker_id: str) -> str:
    return f"faryo-appserver-worker@{validate_worker_id(worker_id)}.service"


def worker_id_from_unit(unit: str) -> str | None:
    match = WORKER_UNIT_RE.fullmatch(str(unit or "").strip())
    return match.group(1) if match else None


def worker_runtime_root(layout: Layout) -> Path:
    return (layout.faryo_home / "owner/runtime/appserver-workers").resolve(strict=False)


def worker_socket_path(layout: Layout, worker_id: str) -> Path:
    root = worker_runtime_root(layout)
    path = (root / f"{validate_worker_id(worker_id)}.sock").resolve(strict=False)
    if path.parent != root:
        raise OperationError("App Server worker socket path is invalid")
    return path


def prepare_worker_runtime(layout: Layout) -> Path:
    root = worker_runtime_root(layout)
    expected = (layout.faryo_home / "owner/runtime/appserver-workers").resolve(strict=False)
    if root != expected:
        raise OperationError("App Server worker runtime path is invalid")
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
    except OSError as exc:
        raise OperationError("App Server worker runtime directory is unavailable") from exc
    return root


def listed_worker_units(systemctl_call: Callable[..., Any]) -> list[str]:
    result = systemctl_call(
        "list-units",
        "--all",
        "--plain",
        "--no-legend",
        "--type=service",
        "faryo-appserver-worker@*.service",
        check=False,
    )
    if getattr(result, "returncode", 1) != 0:
        return []
    units = []
    for line in str(getattr(result, "stdout", "") or "").splitlines():
        candidate = line.split(maxsplit=1)[0] if line.strip() else ""
        if worker_id_from_unit(candidate) is not None:
            units.append(candidate)
    return sorted(set(units))


@dataclass
class WorkerServiceManager:
    layout: Layout
    systemctl_call: Callable[..., Any]
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep

    def socket_path(self, worker_id: str) -> Path:
        return worker_socket_path(self.layout, worker_id)

    def state(self, worker_id: str) -> str:
        result = self.systemctl_call("is-active", worker_unit_name(worker_id), check=False)
        value = str(getattr(result, "stdout", "") or "").strip()
        return value if value in {"active", "activating", "deactivating", "failed", "inactive"} else "unknown"

    def start(self, worker_id: str, *, timeout: float = 12.0) -> Path:
        selected = validate_worker_id(worker_id)
        prepare_worker_runtime(self.layout)
        unit = worker_unit_name(selected)
        self.systemctl_call("start", unit)
        path = self.socket_path(selected)
        deadline = self.monotonic() + max(0.1, timeout)
        while self.monotonic() < deadline:
            state = self.state(selected)
            try:
                if path.is_socket() and state == "active":
                    return path
            except OSError:
                pass
            if state in {"failed", "inactive"}:
                break
            self.sleep(0.05)
        self.systemctl_call("stop", unit, check=False)
        raise OperationError("App Server worker did not become ready")

    def stop(self, worker_id: str, *, timeout: float = 12.0) -> None:
        selected = validate_worker_id(worker_id)
        unit = worker_unit_name(selected)
        self.systemctl_call("stop", unit, check=False)
        deadline = self.monotonic() + max(0.1, timeout)
        while self.monotonic() < deadline:
            if self.state(selected) in {"inactive", "failed"}:
                return
            self.sleep(0.05)
        raise OperationError("App Server worker did not stop")

    def restart(self, worker_id: str, *, timeout: float = 12.0) -> Path:
        selected = validate_worker_id(worker_id)
        prepare_worker_runtime(self.layout)
        self.systemctl_call("restart", worker_unit_name(selected))
        path = self.socket_path(selected)
        deadline = self.monotonic() + max(0.1, timeout)
        while self.monotonic() < deadline:
            state = self.state(selected)
            try:
                if path.is_socket() and state == "active":
                    return path
            except OSError:
                pass
            if state in {"failed", "inactive"}:
                break
            self.sleep(0.05)
        raise OperationError("App Server worker did not restart")

    def units(self) -> list[str]:
        return listed_worker_units(self.systemctl_call)
