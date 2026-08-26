"""Canonical robot-status vocabulary for every YantraFleet component.

v0.1 shipped with three diverging vocabularies (sim: working/moving/
safety_stop, connector: estop/active, console: fault/paused/...). v0.2
fixes that at the source: every writer stores ONLY these statuses and
every reader may assume them. The DB enforces it with a CHECK constraint
(supabase/0002_commands.sql).

Canonical set
-------------
active     robot is doing useful work or driving to it
idle       powered, healthy, no task
charging   at a charger, charging
paused     deliberately held by an operator (resumable)
estop      emergency/safety stop (operator or safety field)
degraded   running, but with WARNING-level errors
fault      FATAL error; not operating
"""
from __future__ import annotations

CANONICAL: frozenset[str] = frozenset(
    {"active", "idle", "charging", "paused", "estop", "degraded", "fault"}
)

#: Every legacy / internal spelling seen in v0.1 writers -> canon.
LEGACY_MAP: dict[str, str] = {
    "working": "active",
    "moving": "active",
    "to_charger": "active",
    "driving": "active",
    "safety_stop": "estop",
    "estopped": "estop",
    "e-stop": "estop",
    "error": "fault",
    "faulted": "fault",
    "offline": "fault",
}

#: Statuses that count as "not operating" for copilot/analytics purposes.
NOT_OPERATING: frozenset[str] = frozenset({"fault", "estop"})


def normalize(status: str | None) -> str:
    """Map any known spelling to the canonical vocabulary.

    Unknown or empty statuses become ``idle`` (the least alarming guess) —
    readers must never crash on a stranger's status string.
    """
    s = (status or "").strip().lower()
    if s in CANONICAL:
        return s
    return LEGACY_MAP.get(s, "idle")
