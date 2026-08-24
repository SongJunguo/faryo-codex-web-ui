"""Private metadata registry for Faryo Codex App Server sessions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
from typing import Any, Iterable

from faryo_cli import session_backend
import session_namespace


REGISTRY_SCHEMA_VERSION = 2
LEGACY_REGISTRY_SCHEMA_VERSION = 1
SESSION_NAME_RE = session_namespace.SESSION_NAME_RE
WORKER_ID_RE = re.compile(r"^[a-f0-9]{24}$")
WORKER_STATES = {"unknown", "starting", "ready", "reconnecting", "degraded", "stopping", "exited"}


def new_worker_id(existing: Iterable[str] = ()) -> str:
    used = {str(value) for value in existing}
    while True:
        value = secrets.token_hex(12)
        if value not in used:
            return value


@dataclass
class WebSessionRecord:
    name: str
    thread_id: str
    cwd: str
    title: str = ""
    model: str = ""
    launch_id: str = ""
    created_at: int = 0
    updated_at: int = 0
    backend: str = session_backend.APP_SERVER.value
    worker_id: str = ""
    worker_generation: int = 0
    worker_state: str = "unknown"

    @classmethod
    def from_value(
        cls,
        value: Any,
        *,
        allow_missing_worker: bool = False,
        existing_workers: Iterable[str] = (),
    ) -> "WebSessionRecord | None":
        if not isinstance(value, dict):
            return None
        name = str(value.get("name") or "")
        thread_id = str(value.get("thread_id") or value.get("threadId") or "")
        cwd = str(value.get("cwd") or "")
        if not SESSION_NAME_RE.fullmatch(name) or not thread_id or not cwd or "\x00" in cwd:
            return None
        worker_id = str(value.get("worker_id") or value.get("workerId") or "")
        if not worker_id and allow_missing_worker:
            worker_id = new_worker_id(existing_workers)
        if not WORKER_ID_RE.fullmatch(worker_id):
            return None
        try:
            created_at = max(0, int(value.get("created_at") or value.get("createdAt") or 0))
            updated_at = max(0, int(value.get("updated_at") or value.get("updatedAt") or 0))
            worker_generation = max(0, int(value.get("worker_generation") or value.get("workerGeneration") or 0))
        except (TypeError, ValueError):
            return None
        worker_state = str(value.get("worker_state") or value.get("workerState") or "unknown")
        if worker_state not in WORKER_STATES:
            worker_state = "unknown"
        return cls(
            name=name,
            thread_id=thread_id,
            cwd=cwd,
            title=str(value.get("title") or "")[:240],
            model=str(value.get("model") or "")[:160],
            launch_id=str(value.get("launch_id") or value.get("launchId") or "")[:160],
            created_at=created_at,
            updated_at=updated_at,
            worker_id=worker_id,
            worker_generation=worker_generation,
            worker_state=worker_state,
        )

    def public(self) -> dict[str, Any]:
        return {
            "session": self.name,
            "threadId": self.thread_id,
            "cwd": self.cwd,
            "title": self.title,
            "model": self.model,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "backend": self.backend,
            "workerState": self.worker_state,
        }


class WebSessionRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.records: dict[str, WebSessionRecord] = {}
        self.load_errors = 0
        self.lock = threading.RLock()
        self.load()

    def load(self) -> None:
        with self.lock:
            self.records = {}
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return
            except (OSError, json.JSONDecodeError):
                self.load_errors += 1
                return
            if not isinstance(value, dict) or value.get("schemaVersion") not in {
                LEGACY_REGISTRY_SCHEMA_VERSION,
                REGISTRY_SCHEMA_VERSION,
            }:
                self.load_errors += 1
                return
            legacy = value.get("schemaVersion") == LEGACY_REGISTRY_SCHEMA_VERSION
            workers: set[str] = set()
            for item in value.get("sessions") or []:
                record = WebSessionRecord.from_value(
                    item,
                    allow_missing_worker=legacy,
                    existing_workers=workers,
                )
                if record is not None and record.name not in self.records and record.worker_id not in workers:
                    self.records[record.name] = record
                    workers.add(record.worker_id)
            if legacy:
                self.save()

    def save(self) -> None:
        with self.lock:
            body = json.dumps(
                {
                    "schemaVersion": REGISTRY_SCHEMA_VERSION,
                    "sessions": [asdict(record) for record in sorted(self.records.values(), key=lambda item: item.name)],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ) + "\n"
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.path.parent.chmod(0o700)
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.chmod(0o600)
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)

    def values(self) -> list[WebSessionRecord]:
        with self.lock:
            return list(self.records.values())

    def next_name(self, reserved: Iterable[str] = ()) -> str:
        with self.lock:
            return session_namespace.next_name(self.records, reserved)

    def reassign_conflicts(self, reserved: Iterable[str] = ()) -> dict[str, str]:
        """Move persisted Web sessions away from names owned by another backend."""
        with self.lock:
            external = {str(value) for value in reserved}
            conflicts = sorted(
                (record for record in self.records.values() if record.name in external),
                key=lambda record: (record.created_at, record.name),
            )
            if not conflicts:
                return {}
            used = set(self.records) | external
            renamed: dict[str, str] = {}
            for record in conflicts:
                old_name = record.name
                new_name = session_namespace.next_name(used)
                del self.records[old_name]
                record.name = new_name
                self.records[new_name] = record
                used.add(new_name)
                renamed[old_name] = new_name
            self.save()
            return renamed

    def add(
        self,
        *,
        thread_id: str,
        cwd: str,
        title: str = "",
        model: str = "",
        launch_id: str = "",
        reserved: Iterable[str] = (),
        name: str = "",
        worker_id: str = "",
        now: int | None = None,
    ) -> WebSessionRecord:
        with self.lock:
            selected_name = name or self.next_name(reserved)
            if not SESSION_NAME_RE.fullmatch(selected_name) or selected_name in self.records:
                raise ValueError("web session name is unavailable")
            selected_worker = worker_id or new_worker_id(record.worker_id for record in self.records.values())
            if not WORKER_ID_RE.fullmatch(selected_worker):
                raise ValueError("app server worker id is invalid")
            if any(record.worker_id == selected_worker for record in self.records.values()):
                raise ValueError("app server worker id is unavailable")
            timestamp = int(time.time() if now is None else now)
            record = WebSessionRecord(
                name=selected_name,
                thread_id=thread_id,
                cwd=cwd,
                title=title[:240],
                model=model[:160],
                launch_id=launch_id[:160],
                created_at=timestamp,
                updated_at=timestamp,
                worker_id=selected_worker,
            )
            self.records[record.name] = record
            self.save()
            return record

    def get(self, name: str) -> WebSessionRecord | None:
        with self.lock:
            return self.records.get(name)

    def by_thread(self, thread_id: str) -> WebSessionRecord | None:
        with self.lock:
            return next((record for record in self.records.values() if record.thread_id == thread_id), None)

    def by_launch(self, launch_id: str) -> WebSessionRecord | None:
        if not launch_id:
            return None
        with self.lock:
            return next((record for record in self.records.values() if record.launch_id == launch_id), None)

    def touch(self, name: str, now: int | None = None) -> None:
        with self.lock:
            record = self.records.get(name)
            if record is None:
                return
            record.updated_at = int(time.time() if now is None else now)
            self.save()

    def update_metadata(self, name: str, *, title: str | None = None, model: str | None = None) -> None:
        with self.lock:
            record = self.records.get(name)
            if record is None:
                return
            if title is not None:
                record.title = str(title)[:240]
            if model is not None:
                record.model = str(model)[:160]
            record.updated_at = int(time.time())
            self.save()

    def update_worker_state(self, name: str, state: str, *, increment_generation: bool = False) -> None:
        if state not in WORKER_STATES:
            raise ValueError("app server worker state is invalid")
        with self.lock:
            record = self.records.get(name)
            if record is None:
                return
            record.worker_state = state
            if increment_generation:
                record.worker_generation += 1
            record.updated_at = int(time.time())
            self.save()

    def remove(self, name: str) -> bool:
        with self.lock:
            removed = self.records.pop(name, None)
            if removed is None:
                return False
            self.save()
            return True
