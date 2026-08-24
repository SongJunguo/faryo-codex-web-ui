"""One authoritative short-name namespace across Faryo session backends."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable


SESSION_NAME_RE = re.compile(r"^faryo(?P<number>[1-9][0-9]*)$")
APP_SERVER_OWNER = "app-server"
TERMINAL_OWNER = "terminal"


class SessionNamespaceConflict(RuntimeError):
    """Raised when one public short name is owned by more than one backend."""


def normalized_names(values: Iterable[object]) -> set[str]:
    return {
        name
        for value in values
        if (name := str(value or "")) and SESSION_NAME_RE.fullmatch(name)
    }


def next_name(*groups: Iterable[object]) -> str:
    used = set().union(*(normalized_names(group) for group in groups))
    number = 1
    while f"faryo{number}" in used:
        number += 1
    return f"faryo{number}"


class SessionNamespace:
    """Resolve and reserve public names without backend-priority guessing."""

    def __init__(
        self,
        *,
        terminal_names: Callable[[], Iterable[object]],
        app_server_names: Callable[[], Iterable[object]],
    ) -> None:
        self._terminal_names = terminal_names
        self._app_server_names = app_server_names

    def terminal_names(self) -> set[str]:
        return normalized_names(self._terminal_names())

    def app_server_names(self) -> set[str]:
        return normalized_names(self._app_server_names())

    def owner(self, name: object) -> str | None:
        selected = str(name or "")
        terminal = selected in self.terminal_names()
        app_server = selected in self.app_server_names()
        if terminal and app_server:
            raise SessionNamespaceConflict(
                f"session short name is owned by multiple backends: {selected}"
            )
        if app_server:
            return APP_SERVER_OWNER
        if terminal:
            return TERMINAL_OWNER
        return None

    def reserved_for_terminal(self) -> list[str]:
        return sorted(self.app_server_names())

    def reserved_for_app_server(self) -> list[str]:
        return sorted(self.terminal_names())

    def collisions(self) -> set[str]:
        return self.terminal_names() & self.app_server_names()
