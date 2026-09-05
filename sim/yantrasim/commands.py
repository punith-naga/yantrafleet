"""Operator-command execution (v0.2).

Commands arrive through the ``commands`` table (console writes ``pending``,
a human approves -> ``approved``); the simulator polls approved rows,
applies them to the world, and reports ``executed`` / ``failed``. This is
the sim-side half of the human-in-the-loop gate — a real robot adapter
implements the same four verbs against the vendor API.

Verbs: ``pause`` | ``resume`` | ``charge`` | ``estop`` | ``cancel_order``.
``cancel_order`` is the sim-side handler for the VDA 5050 standard
``cancelOrder`` instantAction (see ``transports.mqtt.ACTION_VERBS``): it is
not exposed through the ``commands`` table gate (only reachable via MQTT
instantActions), but shares the same apply/report contract.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from . import world

if TYPE_CHECKING:  # pragma: no cover
    from .sim import FleetSim, Robot

VERBS = ("pause", "resume", "charge", "estop", "cancel_order")

#: statuses an operator may pause from (not faults, not already held)
_PAUSABLE = ("idle", "moving", "working", "to_charger", "charging")


def _find(sim: "FleetSim", robot_id: str) -> "Robot | None":
    for r in sim.robots:
        if r.robot_id == robot_id:
            return r
    return None


def apply_command(sim: "FleetSim", robot_id: str, cmd: str) -> tuple[bool, str]:
    """Apply one approved command. Returns ``(ok, detail)``.

    Never raises: a bad robot id / verb / state returns ``(False, why)``
    so the poller can mark the row ``failed`` with a reason.
    """
    r = _find(sim, robot_id)
    if r is None:
        return False, f"unknown robot {robot_id!r}"
    if cmd not in VERBS:
        return False, f"unknown command {cmd!r}"

    if cmd == "pause":
        if r.status == "fault":
            return False, f"{robot_id} is faulted; cannot pause"
        if r.status in ("paused", "estopped"):
            return True, f"{robot_id} already held ({r.status})"
        r.resume_status = r.status
        r.status = "paused"
        r.speed = 0.0
        return True, f"{robot_id} paused (was {r.resume_status})"

    if cmd == "estop":
        if r.status != "estopped":
            r.resume_status = r.status if r.status != "fault" else "idle"
            r.status = "estopped"
            r.speed = 0.0
        return True, f"{robot_id} emergency-stopped"

    if cmd == "resume":
        if r.status not in ("paused", "estopped"):
            return False, f"{robot_id} is not held (status={r.status})"
        r.status = r.resume_status or "idle"
        # A held robot's stale path may reference its old task; restart clean.
        if r.status in ("moving", "working"):
            r.status = "idle"
            r.path = []
            r.task_kind = None
        return True, f"{robot_id} resumed"

    if cmd == "cancel_order":
        if r.status == "fault":
            return False, f"{robot_id} is faulted; cannot cancel order"
        order_id = r.order_id or "(none)"
        had_order = bool(r.path) or r.status in ("moving", "working", "to_charger")
        r.path = []
        r.task_kind = None
        r.task_mission = None
        r.current_action_id = None
        if r.status in ("moving", "working", "to_charger"):
            r.status = "idle"
        if not had_order:
            return True, f"{robot_id} had no active order to cancel"
        return True, f"{robot_id} order {order_id} cancelled"

    # cmd == "charge"
    if r.status == "fault":
        return False, f"{robot_id} is faulted; clear fault first"
    if r.status == "charging":
        return True, f"{robot_id} already charging"
    if r.status in ("paused", "estopped"):
        r.status = r.resume_status or "idle"
    sim._route_to(r, world.nearest_charger(r.node), "to_charger")
    r.task_kind = None
    return True, f"{robot_id} routed to charger {r.path[-1] if r.path else r.node}"
