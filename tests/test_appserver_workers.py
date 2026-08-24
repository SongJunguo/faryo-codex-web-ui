from __future__ import annotations

from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest

from faryo_cli import appserver_workers
from faryo_cli.diagnostics import Layout
from faryo_cli.operations import OperationError


class FakeSystemd:
    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.state = "inactive"
        self.socket: socket.socket | None = None
        self.calls: list[tuple[tuple[str, ...], bool]] = []

    def __call__(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, check))
        action = args[0]
        if action in {"start", "restart"}:
            if self.socket is not None:
                self.socket.close()
            self.socket_path.unlink(missing_ok=True)
            self.socket_path.parent.mkdir(parents=True, exist_ok=True)
            self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.socket.bind(str(self.socket_path))
            self.state = "active"
            return subprocess.CompletedProcess(["systemctl"], 0, "", "")
        if action == "stop":
            if self.socket is not None:
                self.socket.close()
                self.socket = None
            self.socket_path.unlink(missing_ok=True)
            self.state = "inactive"
            return subprocess.CompletedProcess(["systemctl"], 0, "", "")
        if action == "is-active":
            return subprocess.CompletedProcess(["systemctl"], 0, self.state + "\n", "")
        raise AssertionError(args)


class AppServerWorkersTest(unittest.TestCase):
    @staticmethod
    def layout(root: Path) -> Layout:
        return Layout(
            home=root,
            faryo_home=root / ".faryo",
            owner_env=root / ".faryo/owner/config/faryo.env",
            gateway_env=root / ".faryo/gateway/config/faryo.env",
            gateway_auth=root / ".faryo/gateway/config/gateway-auth.json",
            source_root=None,
        )

    def test_worker_identity_and_socket_are_strictly_bounded(self) -> None:
        worker_id = "c" * 24
        with tempfile.TemporaryDirectory() as temp:
            layout = self.layout(Path(temp))
            path = appserver_workers.worker_socket_path(layout, worker_id)
            root = appserver_workers.prepare_worker_runtime(layout)
            mode = root.stat().st_mode & 0o777

        self.assertEqual(appserver_workers.worker_unit_name(worker_id), f"faryo-appserver-worker@{worker_id}.service")
        self.assertEqual(appserver_workers.worker_id_from_unit(f"faryo-appserver-worker@{worker_id}.service"), worker_id)
        self.assertEqual(path.parent, root)
        self.assertEqual(mode, 0o700)
        for invalid in ("", "../owner", "c" * 23, "C" * 24, "c" * 24 + ".service"):
            with self.subTest(invalid=invalid), self.assertRaises(OperationError):
                appserver_workers.worker_unit_name(invalid)

    def test_manager_starts_restarts_and_stops_only_one_worker(self) -> None:
        worker_id = "d" * 24
        with tempfile.TemporaryDirectory() as temp:
            layout = self.layout(Path(temp))
            path = appserver_workers.worker_socket_path(layout, worker_id)
            systemd = FakeSystemd(path)
            manager = appserver_workers.WorkerServiceManager(layout, systemd)

            self.assertEqual(manager.start(worker_id, timeout=1), path)
            self.assertEqual(manager.restart(worker_id, timeout=1), path)
            manager.stop(worker_id, timeout=1)

        units = [args[1] for args, _check in systemd.calls if args[0] in {"start", "restart", "stop"}]
        self.assertEqual(set(units), {f"faryo-appserver-worker@{worker_id}.service"})
        self.assertEqual(systemd.state, "inactive")

    def test_unit_listing_rejects_unrelated_or_malformed_targets(self) -> None:
        worker_id = "e" * 24

        def systemctl(*_args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                ["systemctl"],
                0,
                (
                    f"faryo-appserver-worker@{worker_id}.service loaded active running Worker\n"
                    "faryo-owner.service loaded active running Owner\n"
                    "faryo-appserver-worker@../../bad.service loaded active running Bad\n"
                ),
                "",
            )

        self.assertEqual(
            appserver_workers.listed_worker_units(systemctl),
            [f"faryo-appserver-worker@{worker_id}.service"],
        )

    def test_manager_does_not_treat_deactivating_worker_as_stopped(self) -> None:
        worker_id = "f" * 24
        with tempfile.TemporaryDirectory() as temp:
            layout = self.layout(Path(temp))
            calls = []

            def systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
                calls.append((args, check))
                if args[0] == "is-active":
                    return subprocess.CompletedProcess(["systemctl"], 3, "deactivating\n", "")
                return subprocess.CompletedProcess(["systemctl"], 0, "", "")

            manager = appserver_workers.WorkerServiceManager(
                layout,
                systemctl,
                sleep=lambda _delay: time.sleep(0),
            )
            with self.assertRaisesRegex(OperationError, "did not stop"):
                manager.stop(worker_id, timeout=0.001)

        targeted = [args[1] for args, _check in calls if args[0] in {"stop", "is-active"}]
        self.assertEqual(set(targeted), {f"faryo-appserver-worker@{worker_id}.service"})


if __name__ == "__main__":
    unittest.main()
