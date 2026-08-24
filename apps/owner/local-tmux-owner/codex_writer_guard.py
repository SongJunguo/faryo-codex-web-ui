"""Advisory probes for Codex's process-held per-thread writer locks."""

from __future__ import annotations

from dataclasses import dataclass
import errno
import fcntl
import os
from pathlib import Path
import re


THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")


@dataclass(frozen=True)
class WriterProbe:
    """A bounded, non-mutating view of one Codex thread writer lock."""

    state: str

    @property
    def held(self) -> bool:
        return self.state == "held"

    @property
    def available(self) -> bool:
        return self.state == "available"


def codex_home(values: dict[str, str] | None = None) -> Path:
    environment = os.environ if values is None else values
    home = Path(environment.get("HOME") or Path.home()).expanduser()
    return Path(environment.get("CODEX_HOME") or home / ".codex").expanduser()


def writer_lock_path(thread_id: str, *, root: Path | None = None) -> Path | None:
    clean_id = str(thread_id or "").strip()
    if not THREAD_ID_RE.fullmatch(clean_id):
        return None
    selected_root = (root or codex_home()).expanduser()
    return selected_root / "thread-writer-locks" / f"{clean_id}.lock"


def probe_thread_writer(thread_id: str, *, root: Path | None = None) -> WriterProbe:
    """Return ``available``, ``held`` or ``unknown`` without deleting a lock.

    Codex owns these files and protects the actual writer with ``flock``.  The
    pathname alone is not evidence of a live owner, so Faryo briefly attempts
    the same non-blocking lock and immediately releases it on success.
    """

    path = writer_lock_path(thread_id, root=root)
    if path is None:
        return WriterProbe("unknown")
    try:
        descriptor = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return WriterProbe("available")
    except OSError:
        return WriterProbe("unknown")
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                return WriterProbe("held")
            return WriterProbe("unknown")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            return WriterProbe("unknown")
        return WriterProbe("available")
    finally:
        os.close(descriptor)
