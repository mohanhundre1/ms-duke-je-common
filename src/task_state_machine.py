"""Shared A2A Task State Machine (R24).

Defines valid A2A task state transitions and provides helpers for
emitting WORKING state updates consistently across agent executors.

Valid A2A transition graph:

    submitted -> working -> completed (terminal)
                         -> failed (terminal)
                         -> canceled (terminal)
                         -> input-required -> working
    submitted -> canceled (terminal)
"""

from __future__ import annotations

# A.2.2 relocated the a2a-touching lifecycle helpers into transport/a2a; re-export
# them so existing `task_state_machine.emit_working` / `ensure_task_created`
# call sites keep working. This module now holds only the SDK-free transition table.
from .transport.a2a.task_lifecycle import emit_working, ensure_task_created


# ---------------------------------------------------------------------------
# State transition table (R24)
# ---------------------------------------------------------------------------
VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "submitted": frozenset(("working", "canceled")),
    "working": frozenset(("completed", "failed", "canceled", "input-required")),
    "input-required": frozenset(("working", "canceled")),
    # terminal states: no outgoing transitions
    "completed": frozenset(),
    "failed": frozenset(),
    "canceled": frozenset(),
}

TERMINAL_STATES: frozenset[str] = frozenset(("completed", "failed", "canceled"))


class InvalidTransition(Exception):
    """Raised when a requested A2A state transition is not allowed."""

    def __init__(self, task_id: str, current: str, requested: str) -> None:
        self.task_id = task_id
        self.current = current
        self.requested = requested
        super().__init__(
            f"Task {task_id!r}: invalid transition {current!r} -> {requested!r}"
        )


def validate_transition(task_id: str, current: str, requested: str) -> None:
    """Raise :class:`InvalidTransition` if the transition is not allowed.

    Parameters
    ----------
    task_id:
        Identifier of the task (used in error messages only).
    current:
        The current A2A state string (lower-case, e.g. ``"submitted"``).
    requested:
        The desired next A2A state string.
    """
    allowed = VALID_TRANSITIONS.get(current, frozenset())
    if requested not in allowed:
        raise InvalidTransition(task_id, current, requested)


__all__ = [
    "VALID_TRANSITIONS",
    "TERMINAL_STATES",
    "InvalidTransition",
    "validate_transition",
    "emit_working",
    "ensure_task_created",
]
