#!/usr/bin/env python3
"""Opt-in user-systemd TERM-to-KILL and lock-release acceptance probe."""

from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import time


def child(lock_path: Path, ready_path: Path) -> int:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with lock_path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        ready_path.write_text(str(os.getpid()), encoding="ascii")
        while True:
            time.sleep(60)


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(args),
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if check and result.returncode:
        raise RuntimeError("user systemd probe command failed")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--lock", default="")
    parser.add_argument("--ready", default="")
    args = parser.parse_args()
    if args.child:
        return child(Path(args.lock), Path(args.ready))

    unit = f"faryo-worker-kill-probe-{secrets.token_hex(6)}.service"
    with tempfile.TemporaryDirectory(prefix="faryo-worker-kill-probe-") as temporary:
        root = Path(temporary)
        lock_path = root / "writer.lock"
        ready_path = root / "ready"
        try:
            run(
                "systemd-run",
                "--user",
                f"--unit={unit}",
                "--collect",
                "--property=Type=simple",
                "--property=KillMode=mixed",
                "--property=TimeoutStopSec=1s",
                sys.executable,
                str(Path(__file__).resolve()),
                "--child",
                "--lock",
                str(lock_path),
                "--ready",
                str(ready_path),
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not ready_path.is_file():
                time.sleep(0.02)
            if not ready_path.is_file():
                raise RuntimeError("TERM-resistant probe did not become ready")
            process_id = int(ready_path.read_text(encoding="ascii"))
            started = time.monotonic()
            run("systemctl", "--user", "stop", unit)
            elapsed = time.monotonic() - started
            if elapsed > 5:
                raise RuntimeError("TERM-to-KILL escalation exceeded its bound")
            try:
                os.kill(process_id, 0)
            except ProcessLookupError:
                pass
            else:
                raise RuntimeError("TERM-resistant worker survived systemd stop")
            with lock_path.open("a", encoding="utf-8") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RuntimeError("worker writer lock remained held after KILL") from exc
        finally:
            run("systemctl", "--user", "stop", unit, check=False)
            run("systemctl", "--user", "reset-failed", unit, check=False)
    print("systemd-worker-kill=PASS term=ignored kill=bounded process=gone lock=released")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
