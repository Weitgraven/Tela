"""Action registry. Every privileged operation is a named, registered function."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Handler = Callable[[dict[str, Any]], Any]

_ACTIONS: dict[str, Handler] = {}


class ActionError(Exception):
    """Raised by an action for an expected failure; the message is sent to the caller."""


def action(name: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        if name in _ACTIONS:
            raise RuntimeError(f"duplicate action {name!r}")
        _ACTIONS[name] = fn
        return fn

    return register


def get(name: str) -> Handler | None:
    return _ACTIONS.get(name)


def names() -> list[str]:
    return sorted(_ACTIONS)
